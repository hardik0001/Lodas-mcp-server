"""Computer-use session runner for DATEV LODAS.

Owns the screenshot -> model computer-use -> action loop described in the
architecture doc (Section 4). One DATEVWorker.run() call is one bounded,
goal-oriented computer-use session; it does not decide business logic —
that lives in the MCP tool layer (tools/*.py), which builds the goal
prompt and validates inputs before this is ever invoked.

Three providers are supported, selected via DATEV_COMPUTER_USE_PROVIDER:
  - "anthropic" (default) — the architecture doc's tech-stack choice.
  - "openai" — OpenAI's Responses API computer-use-preview tool. This is a
    gated-access model separate from OpenAI account credit; most keys
    don't have it enabled.
  - "openai_chat" — DIY fallback for keys without computer-use-preview
    access: a standard vision + function-calling model (gpt-4o by default)
    with a hand-rolled "computer_action" tool whose schema matches
    computer_use/actions.py's action-dict shape directly, so it reuses the
    same executor with no translation layer. Lower fidelity than a
    purpose-built computer-use model — untested territory, not the
    architecture doc's recommended path.
All three share the same WorkerResult/trace contract.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import anthropic
import openai

from audit.logger import TraceRecorder
from computer_use import actions

log = logging.getLogger("datev_mcp.worker")

PROVIDER = os.environ.get("DATEV_COMPUTER_USE_PROVIDER", "anthropic").lower()
ANTHROPIC_MODEL = os.environ.get("DATEV_COMPUTER_USE_MODEL", "claude-sonnet-5")
OPENAI_MODEL = os.environ.get("DATEV_OPENAI_MODEL", "computer-use-preview")
OPENAI_CHAT_MODEL = os.environ.get("DATEV_OPENAI_CHAT_MODEL", "gpt-4o")
ANTHROPIC_MAX_DIM = int(os.environ.get("DATEV_ANTHROPIC_MAX_DIM", "1280"))
DEFAULT_MAX_STEPS = int(os.environ.get("DATEV_MAX_STEPS", "20"))
DEFAULT_TIMEOUT_S = int(os.environ.get("DATEV_TIMEOUT_S", "120"))
# claude-sonnet-5 only accepts the toolset form (confirmed live against the
# API — the model rejects computer_20250124 et al. and suggests this). The
# toolset needs no beta header and no display_width_px/display_height_px:
# dimensions come from the screenshot images themselves. Its tool_use blocks
# carry the member name directly (e.g. name="left_click") rather than a
# wrapping name="computer" + input.action, unlike the older single-tool API.
COMPUTER_TOOLSET_TYPE = "computer_toolset_20260801"

# Hand-rolled tool schema for the "openai_chat" fallback provider. Field
# names deliberately mirror the action dict computer_use/actions.py.execute()
# already accepts, so results from this tool need no translation.
COMPUTER_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "computer_action",
        "description": (
            "Perform exactly one action on the screen based on the most recent "
            "screenshot. Call this once per turn; a fresh screenshot follows "
            "every action."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "left_click", "right_click", "middle_click", "double_click",
                        "triple_click", "left_click_drag", "mouse_move", "type", "key",
                        "scroll", "wait", "screenshot",
                    ],
                },
                "coordinate": {
                    "type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
                    "description": "[x, y] pixel coordinate — required for click/move/drag-end/scroll actions",
                },
                "start_coordinate": {
                    "type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2,
                    "description": "[x, y] drag start point — left_click_drag only",
                },
                "text": {
                    "type": "string",
                    "description": "text to type (action='type'), or key name(s) joined with '+' e.g. 'ctrl+a' (action='key')",
                },
                "scroll_direction": {"type": "string", "enum": ["up", "down"]},
                "scroll_amount": {"type": "integer", "description": "scroll notches, default 3"},
                "duration": {"type": "number", "description": "seconds to wait — action='wait' only"},
            },
            "required": ["action"],
        },
    },
}

SYSTEM_PROMPT = (
    "You are operating a Windows desktop running DATEV LODAS, a German payroll "
    "application. You interact only through screenshots and the computer tool. "
    "Work efficiently — every action costs an API call, so minimize them: "
    "click a field once, then type its entire value in one 'type' action "
    "(never type one character at a time), and only re-check the screen when "
    "you actually need to see something new (after navigating, after a click "
    "whose result you can't predict) — not after every single keystroke you "
    "just sent yourself. Fill all the fields the goal asks for, then do ONE "
    "final read-back/verification pass at the end, not one after each field. "
    "Work carefully but not redundantly: read field labels before typing into "
    "them, clear a field before replacing its value, and never click "
    "'Speichern' / save unless the goal explicitly asks you to. When the goal "
    "is complete, stop taking actions and reply with plain text summarizing "
    "what you did or observed. If the goal asks you to read back values, "
    "reply with a single JSON object using exactly the field names given in "
    "the goal as the last line of your final message. If you encounter an "
    "error dialog or an unexpected screen, stop and describe exactly what you "
    "see instead of trying to dismiss it."
)


@dataclass
class WorkerResult:
    success: bool
    description: str = ""
    values: dict | None = None
    final_screenshot: str | None = None
    error: str | None = None
    steps_taken: int = 0
    trace_id: str | None = None


class DATEVWorker:
    def __init__(
        self,
        trace_dir: Path,
        max_steps: int = DEFAULT_MAX_STEPS,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        provider: str = PROVIDER,
    ):
        self.trace_dir = Path(trace_dir)
        self.max_steps = max_steps
        self.timeout_s = timeout_s
        self.provider = provider
        self.client = anthropic.Anthropic() if provider == "anthropic" else openai.OpenAI()

    def run(self, goal: str, action_id: str, tool_name: str = "computer_use") -> WorkerResult:
        target_window = os.environ.get("DATEV_TARGET_WINDOW_TITLE", "")
        if target_window:
            if actions.focus_window(target_window):
                time.sleep(0.5)  # let window-activation animation settle before the first action
            else:
                log.warning("DATEV_TARGET_WINDOW_TITLE=%r matched no window; proceeding unfocused", target_window)

        screen = actions.get_screen_size()
        recorder = TraceRecorder(self.trace_dir, tool=tool_name, action_id=action_id, input={"goal": goal})
        if self.provider == "openai":
            return self._run_openai(goal, recorder, screen)
        if self.provider == "openai_chat":
            return self._run_openai_chat(goal, recorder, screen)
        return self._run_anthropic(goal, recorder, screen)

    # ------------------------------------------------------------------
    # Anthropic backend (default — computer_toolset_20260801, Messages API)
    # ------------------------------------------------------------------
    def _run_anthropic(self, goal: str, recorder: TraceRecorder, screen) -> WorkerResult:
        # The toolset declares no display_width_px/display_height_px (unlike the
        # older single-tool API), and coordinate grounding at native 2560x1440
        # proved unreliable — clicks intermittently landed hundreds of pixels off
        # target. Sending a downscaled screenshot and telling the model exactly
        # those (scaled) dimensions, then scaling its coordinates back up before
        # execution, is Anthropic's long-standing recommended pattern for this
        # tool family and fixed it in testing.
        scale = min(1.0, ANTHROPIC_MAX_DIM / max(screen.width, screen.height))
        scaled_w = max(1, round(screen.width * scale))
        scaled_h = max(1, round(screen.height * scale))
        system_prompt = SYSTEM_PROMPT + (
            f"\n\nThe screenshots you are shown are {scaled_w}x{scaled_h} pixels. "
            f"Coordinates you give the computer tool must be in that exact pixel "
            f"space, [x, y] from the top-left."
        )
        messages: list[dict] = [{"role": "user", "content": goal}]
        tools = [{"type": COMPUTER_TOOLSET_TYPE}]

        start = time.monotonic()
        step = 0
        last_screenshot_path: str | None = None

        try:
            while True:
                if time.monotonic() - start > self.timeout_s:
                    return self._finish(recorder, WorkerResult(
                        success=False, error=f"timed out after {self.timeout_s}s",
                        final_screenshot=last_screenshot_path, steps_taken=step,
                    ))
                if step >= self.max_steps:
                    return self._finish(recorder, WorkerResult(
                        success=False, error=f"exceeded max_steps={self.max_steps}",
                        final_screenshot=last_screenshot_path, steps_taken=step,
                    ))

                response = self.client.beta.messages.create(
                    model=ANTHROPIC_MODEL,
                    max_tokens=1024,
                    system=system_prompt,
                    tools=tools,
                    messages=messages,
                )
                messages.append({"role": "assistant", "content": response.content})

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    final_text = "".join(b.text for b in response.content if b.type == "text")
                    values = self._extract_json(final_text)
                    return self._finish(recorder, WorkerResult(
                        success=True, description=final_text, values=values,
                        final_screenshot=last_screenshot_path, steps_taken=step,
                    ))

                tool_results = []
                for use in tool_uses:
                    step += 1
                    # computer_toolset_20260801 calls each member as its own tool
                    # (name="left_click", input={"coordinate": [...]}), not a wrapping
                    # name="computer" with input.action — reconstruct the action-dict
                    # shape computer_use/actions.py.execute() expects. Coordinates are
                    # in the scaled-screenshot space the model was shown, so map them
                    # back to real screen pixels before executing.
                    raw_action = {"action": use.name, **use.input}
                    action = self._rescale_action(raw_action, scale)
                    if use.name == "zoom":
                        # zoom doesn't touch the screen — it just asks for a closer look
                        # at a region of what's already there.
                        full_shot = actions.take_screenshot_bytes()
                        cropped = actions.crop_region(full_shot, action.get("region", [0, 0, screen.width, screen.height]))
                        shot_for_api, _ = actions.scale_image(cropped, ANTHROPIC_MAX_DIM)
                        log_shot = cropped
                    else:
                        try:
                            actions.execute(action)
                        except Exception as exc:  # surfaced back to the model, loop continues
                            log.warning("action failed: %s (%s)", action, exc)

                        time.sleep(0.3)  # let DATEV's UI settle before the next screenshot
                        full_shot = actions.take_screenshot_bytes()
                        shot_for_api, _ = actions.scale_image(full_shot, ANTHROPIC_MAX_DIM)
                        log_shot = full_shot
                    last_screenshot_path = recorder.log_step(step, action, screenshot_bytes=log_shot)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": use.id,
                        "toolset_name": use.toolset_name,
                        "content": [{
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": actions.b64(shot_for_api)},
                        }],
                    })
                messages.append({"role": "user", "content": tool_results})

        except anthropic.APIError as exc:
            return self._finish(recorder, WorkerResult(
                success=False, error=f"Anthropic API error: {exc}",
                final_screenshot=last_screenshot_path, steps_taken=step,
            ))

    # ------------------------------------------------------------------
    # OpenAI backend (Responses API, computer_use_preview tool)
    # ------------------------------------------------------------------
    def _run_openai(self, goal: str, recorder: TraceRecorder, screen) -> WorkerResult:
        tools = [{
            "type": "computer_use_preview",
            "display_width": screen.width,
            "display_height": screen.height,
            "environment": "windows",
        }]

        start = time.monotonic()
        step = 0
        last_screenshot_path: str | None = None

        try:
            response = self.client.responses.create(
                model=OPENAI_MODEL,
                tools=tools,
                input=f"{SYSTEM_PROMPT}\n\nTask: {goal}",
                truncation="auto",
            )

            while True:
                if time.monotonic() - start > self.timeout_s:
                    return self._finish(recorder, WorkerResult(
                        success=False, error=f"timed out after {self.timeout_s}s",
                        final_screenshot=last_screenshot_path, steps_taken=step,
                    ))
                if step >= self.max_steps:
                    return self._finish(recorder, WorkerResult(
                        success=False, error=f"exceeded max_steps={self.max_steps}",
                        final_screenshot=last_screenshot_path, steps_taken=step,
                    ))

                computer_calls = [item for item in response.output if item.type == "computer_call"]
                if not computer_calls:
                    final_text = response.output_text
                    values = self._extract_json(final_text)
                    return self._finish(recorder, WorkerResult(
                        success=True, description=final_text, values=values,
                        final_screenshot=last_screenshot_path, steps_taken=step,
                    ))

                call = computer_calls[0]
                step += 1
                action = self._openai_action_to_dict(call.action)
                try:
                    actions.execute(action)
                except Exception as exc:  # surfaced back to the model, loop continues
                    log.warning("action failed: %s (%s)", action, exc)

                time.sleep(0.3)  # let DATEV's UI settle before the next screenshot
                shot = actions.take_screenshot_bytes()
                last_screenshot_path = recorder.log_step(step, action, screenshot_bytes=shot)

                ack_checks = [
                    {"id": c.id, "code": c.code, "message": c.message}
                    for c in (call.pending_safety_checks or [])
                ]
                response = self.client.responses.create(
                    model=OPENAI_MODEL,
                    previous_response_id=response.id,
                    tools=tools,
                    input=[{
                        "type": "computer_call_output",
                        "call_id": call.call_id,
                        "acknowledged_safety_checks": ack_checks,
                        "output": {
                            "type": "computer_screenshot",
                            "image_url": f"data:image/png;base64,{actions.b64(shot)}",
                        },
                    }],
                    truncation="auto",
                )

        except openai.APIError as exc:
            return self._finish(recorder, WorkerResult(
                success=False, error=f"OpenAI API error: {exc}",
                final_screenshot=last_screenshot_path, steps_taken=step,
            ))

    # ------------------------------------------------------------------
    # openai_chat backend (DIY fallback — plain vision model + a hand-rolled
    # function tool, for accounts without computer-use-preview access)
    # ------------------------------------------------------------------
    def _run_openai_chat(self, goal: str, recorder: TraceRecorder, screen) -> WorkerResult:
        system_prompt = SYSTEM_PROMPT + (
            f"\n\nYou do not have a dedicated computer-use tool — instead call the "
            f"'computer_action' function exactly once per turn with one action, based "
            f"on the screenshot you were just shown. The screen is {screen.width}x"
            f"{screen.height} pixels; coordinates are [x, y] from the top-left. When "
            f"the goal is complete, do NOT call the function — reply with plain text "
            f"instead."
        )
        first_shot = actions.take_screenshot_bytes()
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": f"Task: {goal}"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{actions.b64(first_shot)}"}},
            ]},
        ]

        start = time.monotonic()
        step = 0
        last_screenshot_path = recorder.log_step(0, {"action": "screenshot"}, screenshot_bytes=first_shot)

        try:
            while True:
                if time.monotonic() - start > self.timeout_s:
                    return self._finish(recorder, WorkerResult(
                        success=False, error=f"timed out after {self.timeout_s}s",
                        final_screenshot=last_screenshot_path, steps_taken=step,
                    ))
                if step >= self.max_steps:
                    return self._finish(recorder, WorkerResult(
                        success=False, error=f"exceeded max_steps={self.max_steps}",
                        final_screenshot=last_screenshot_path, steps_taken=step,
                    ))

                response = self.client.chat.completions.create(
                    model=OPENAI_CHAT_MODEL,
                    messages=messages,
                    tools=[COMPUTER_ACTION_TOOL],
                    tool_choice="auto",
                    max_completion_tokens=1024,
                )
                msg = response.choices[0].message

                if not msg.tool_calls:
                    final_text = msg.content or ""
                    values = self._extract_json(final_text)
                    return self._finish(recorder, WorkerResult(
                        success=True, description=final_text, values=values,
                        final_screenshot=last_screenshot_path, steps_taken=step,
                    ))

                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                })

                last_shot = None
                for tc in msg.tool_calls:
                    step += 1
                    try:
                        action = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        action = {"action": "screenshot"}
                    try:
                        actions.execute(action)
                    except Exception as exc:  # surfaced back to the model, loop continues
                        log.warning("action failed: %s (%s)", action, exc)

                    time.sleep(0.3)  # let DATEV's UI settle before the next screenshot
                    shot = actions.take_screenshot_bytes()
                    last_screenshot_path = recorder.log_step(step, action, screenshot_bytes=shot)
                    last_shot = shot
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": "Action executed. See the following screenshot.",
                    })

                messages.append({
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{actions.b64(last_shot)}"}}],
                })

        except openai.APIError as exc:
            return self._finish(recorder, WorkerResult(
                success=False, error=f"OpenAI API error: {exc}",
                final_screenshot=last_screenshot_path, steps_taken=step,
            ))

    @staticmethod
    def _openai_action_to_dict(action) -> dict:
        """Translate an OpenAI computer_use_preview action into the same
        action-dict shape computer_use/actions.py already executes (the
        Anthropic computer-tool shape), so both providers share one
        execution path."""
        kind = action.type

        if kind == "click":
            button_map = {"left": "left_click", "right": "right_click", "wheel": "middle_click"}
            mapped = button_map.get(action.button)
            if mapped is None:  # "back" / "forward" — no OS-level equivalent here
                return {"action": "screenshot"}
            return {"action": mapped, "coordinate": [action.x, action.y]}
        if kind == "double_click":
            return {"action": "double_click", "coordinate": [action.x, action.y]}
        if kind == "drag":
            path = action.path
            return {
                "action": "left_click_drag",
                "start_coordinate": [path[0].x, path[0].y],
                "coordinate": [path[-1].x, path[-1].y],
            }
        if kind == "keypress":
            return {"action": "key", "text": "+".join(action.keys)}
        if kind == "move":
            return {"action": "mouse_move", "coordinate": [action.x, action.y]}
        if kind == "screenshot":
            return {"action": "screenshot"}
        if kind == "scroll":
            direction = "down" if action.scroll_y >= 0 else "up"
            amount = max(1, abs(action.scroll_y) // 20)  # OpenAI reports pixels, pyautogui wants notches
            return {"action": "scroll", "coordinate": [action.x, action.y], "scroll_direction": direction, "scroll_amount": amount}
        if kind == "type":
            return {"action": "type", "text": action.text}
        if kind == "wait":
            return {"action": "wait", "duration": 1}

        raise ValueError(f"Unsupported OpenAI computer action: {kind!r}")

    @staticmethod
    def _rescale_action(action: dict, scale: float) -> dict:
        """Map coordinate/region fields from the scaled-screenshot space the
        model was shown back to real screen pixels. No-op at scale=1.0."""
        if scale == 1.0:
            return action
        rescaled = dict(action)
        for key in ("coordinate", "start_coordinate"):
            if rescaled.get(key):
                rescaled[key] = [round(v / scale) for v in rescaled[key]]
        if rescaled.get("region"):
            rescaled["region"] = [round(v / scale) for v in rescaled["region"]]
        return rescaled

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _finish(recorder: TraceRecorder, result: WorkerResult) -> WorkerResult:
        record = recorder.finish(
            success=result.success,
            verification={"values": result.values} if result.values else None,
            error=result.error,
        )
        result.trace_id = record["trace_id"]
        return result
