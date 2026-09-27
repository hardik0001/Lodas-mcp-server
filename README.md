# datev-mcp

MCP server that automates data entry in DATEV LODAS (a Windows payroll
desktop app with no API) via a computer-use model.

An orchestrator LLM calls the MCP tools below with structured input; this
server validates it, drives a computer-use session against the live DATEV
screen, reads the form back to verify what was actually entered, and logs a
full trace (steps + screenshots) per call.

## How it works

Three components, two of them LLMs, each with a narrow job:

```
Orchestrator LLM  --MCP tool call-->  This server  --screenshots + actions-->  Computer-use model
   (what to do)                    (validate, drive, verify, log)              (sees pixels, clicks)
```

- **Orchestrator** (not part of this repo) decides business intent — "set
  employee 4711's commute distance to 29 km" — and calls an MCP tool with
  structured arguments. It never sees a screenshot and knows nothing about
  DATEV's UI layout.
- **This server** is deterministic code: it validates input, builds a
  goal-oriented prompt describing exactly what to fill in and where, hands
  that to a computer-use session, and checks what actually landed on screen
  against what was asked for.
- **Computer-use model** (Claude by default) only sees screenshots and a
  goal. It decides clicks/keystrokes turn by turn; it has no knowledge of
  payroll rules and makes no business decisions.

The server is the trust boundary: an LLM's job stops at "here's what should
happen" on one side and "here's what I see on screen" on the other, and only
code in between is allowed to decide whether that counts as success.

### Anatomy of one mutating tool call

Using `datev_set_commute_parameters` as the example (`tools/commute.py`):

1. **Structural validation** — FastMCP checks the call against the tool's
   Python type hints (e.g. `distance_km: int`) before the function body even
   runs. Bad types/missing args never reach step 2.
2. **Plausibility validation** — `validation/schemas.py` runs domain rules
   cross-checking the fields (e.g. `work_days_per_month > 23` → warn,
   `usage_end` in the past → error). Hard errors return immediately as
   `{"success": false, "error": "validation", ...}` with **no computer-use
   session started** — nothing costs an API call until the input is sane.
3. **Goal prompt** — the tool builds a plain-English instruction naming the
   exact tree path and field values to set, and asks the model to read the
   fields back as JSON once done, but explicitly **not** to save yet.
4. **Computer-use loop** (`computer_use/worker.py`, `DATEVWorker.run`) —
   screenshot → model decides one action → action executes → repeat, bounded
   by `DATEV_MAX_STEPS` and `DATEV_TIMEOUT_S`. See below for the loop detail.
5. **Read-back verification** (`verify_readback` in `validation/schemas.py`)
   — the JSON the model read back from the form is compared field-by-field
   against what was asked for, with light normalization (`"29,00"` ==
   `"29"`, `"Ja"` == `true`) so formatting differences don't register as
   false mismatches. A real mismatch is reported per-field, not just
   pass/fail.
6. **Response** — the tool returns `success`, `values_after`, a
   `verification` breakdown, any validation `warnings`, a final screenshot
   path, and a `trace_id` for looking up the full step-by-step record.

Nothing is saved by this flow — see [`datev_save_and_confirm`](#tools)
below. Filling several sections and inspecting their `verification` results
before committing anything is the intended usage.

### The computer-use loop

`DATEVWorker.run()` owns the screenshot/action loop and supports three
interchangeable providers (`DATEV_COMPUTER_USE_PROVIDER`):

- **`anthropic`** (default) — Claude's computer-use toolset. Screenshots are
  downscaled to `DATEV_ANTHROPIC_MAX_DIM` before being sent (coordinate
  grounding degrades at native 4K/2K resolutions) and the model's returned
  coordinates are scaled back up before `computer_use/actions.py` executes
  them against the real screen.
- **`openai`** — OpenAI's `computer-use-preview` Responses API tool. Gated
  access; most API keys don't have it enabled.
- **`openai_chat`** — a DIY fallback for keys without that access: a normal
  vision + function-calling model (`gpt-4o` by default) with a hand-rolled
  `computer_action` function tool whose schema mirrors the action-dict shape
  `actions.execute()` already accepts, so it needs no separate translation
  layer.

All three converge on the same `execute()` call in `computer_use/actions.py`
(clicks, typing, scrolling, key combos — via `pyautogui`, or raw Win32
`SendInput` when `DATEV_INPUT_BACKEND=win32` for controls that ignore
synthetic events) and the same `WorkerResult`/trace contract, so a tool file
never needs to know which provider is running underneath it.

If `DATEV_TARGET_WINDOW_TITLE` is set, the worker re-focuses that window
before every single action (not just once at the start) — a multi-second API
round-trip between actions is enough for the window to lose foreground focus
and silently drop keyboard input.

### Failure handling

No tool auto-retries. A partially-filled form retried blindly risks
double-entering values, and for payroll data "fail loud" beats "retry
silently" — the orchestrator decides whether a retry is safe after seeing
the error. Failure modes are surfaced distinctly: validation errors (before
any UI interaction), timeouts/step-limit exhaustion, an unrecognized dialog
the model reports instead of dismissing, and read-back mismatches — each
carries enough detail (screenshots, which fields matched) to act on without
re-running against live DATEV.

## Tools

| Tool | Purpose |
|---|---|
| `datev_navigate_to_employee` | Open an employee record by personnel number |
| `datev_read_current_values` | Read fields from a section without changing anything |
| `datev_set_commute_parameters` | Home/Workplace (Wohnung/Tätigkeitsstätte) |
| `datev_set_travel_subsidy` | Commute Allowance (Fahrtkostenzuschuss) |
| `datev_set_company_car` | Company Car/Company Car Provision (Firmenwagen) |
| `datev_set_salary_details` | Salary (Festbezüge) |
| `datev_save_and_confirm` | Click Save and read back any confirmation/error dialog |
| `datev_get_session_status` | Cheap pre-flight check: is DATEV reachable, what's on screen |

The `set_*` tools fill fields and read them back for verification but do
**not** save — call `datev_save_and_confirm` once you're done filling
whichever sections you intend to commit together.

## Setup

```bash
uv sync                      # or: pip install -r requirements.txt
```

Create `.env` next to `main.py` with whichever computer-use provider you're
using:

```
ANTHROPIC_API_KEY=...        # default provider
# or
OPENAI_API_KEY=...
```

## Running

```bash
uv run main.py                # stdio MCP server
```

Point an MCP client (Claude Desktop, etc.) at this command, or switch
`mcp.run(...)` in `main.py` to `transport="http"` for a remote client.

### Trying it against the bundled mock

`mock_datev/index.html` is a static stand-in for the DATEV LODAS UI (no
backend — state lives in `localStorage`) for exercising the tools without a
real DATEV install. Open it in a browser, then set:

```
DATEV_TARGET_WINDOW_TITLE=DATEV LODAS   # matches the mock page's title
```

so the worker focuses that browser window before each action.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `DATEV_COMPUTER_USE_PROVIDER` | `anthropic` | `anthropic`, `openai`, or `openai_chat` (fallback for keys without computer-use-preview access) |
| `DATEV_COMPUTER_USE_MODEL` | `claude-sonnet-5` | Anthropic model |
| `DATEV_OPENAI_MODEL` | `computer-use-preview` | OpenAI computer-use model |
| `DATEV_OPENAI_CHAT_MODEL` | `gpt-4o` | Model for the `openai_chat` fallback |
| `DATEV_ANTHROPIC_MAX_DIM` | `1280` | Screenshot downscale target (fixes coordinate drift at high resolutions) |
| `DATEV_MAX_STEPS` | `20` | Max actions per tool call before aborting |
| `DATEV_TIMEOUT_S` | `120` | Wall-clock timeout per tool call |
| `DATEV_TARGET_WINDOW_TITLE` | *(unset)* | Window to focus before each action; unset = whatever's already focused |
| `DATEV_INPUT_BACKEND` | `pyautogui` | `pyautogui` or `win32` (raw `SendInput`, for controls that ignore synthetic events) |

## Traces

Every tool call that drives a computer-use session writes
`~/.datev_mcp_traces/<trace_id>/trace.json` plus one screenshot per step —
the audit trail for debugging and reproducing failures offline.
