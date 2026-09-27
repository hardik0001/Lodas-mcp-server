"""Screen capture and input simulation for the DATEV computer-use worker.

Two input backends are supported, selected via the DATEV_INPUT_BACKEND
env var:
  - "pyautogui" (default): simplest, portable across dev and prod.
  - "win32": raw SendInput via ctypes, for the DATEV .NET controls that
    sometimes ignore synthetic events posted through pyautogui's
    higher-level API (see architecture doc Section 4, tech-stack table).
"""
from __future__ import annotations

import base64
import ctypes
import io
import os
import time
from dataclasses import dataclass

import mss
import pyautogui
from PIL import Image

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02

_BACKEND = os.environ.get("DATEV_INPUT_BACKEND", "pyautogui").lower()
_TARGET_WINDOW = os.environ.get("DATEV_TARGET_WINDOW_TITLE", "")

_KEY_ALIASES = {
    "Return": "enter",
    "Enter": "enter",
    "Tab": "tab",
    "Escape": "esc",
    "BackSpace": "backspace",
    "Delete": "delete",
    "Down": "down",
    "Up": "up",
    "Left": "left",
    "Right": "right",
    "Home": "home",
    "End": "end",
    "Page_Down": "pagedown",
    "Page_Up": "pageup",
}


@dataclass
class ScreenSize:
    width: int
    height: int


def get_screen_size() -> ScreenSize:
    with mss.mss() as sct:
        mon = sct.monitors[1]
        return ScreenSize(width=mon["width"], height=mon["height"])


def take_screenshot_bytes() -> bytes:
    with mss.mss() as sct:
        mon = sct.monitors[1]
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def take_screenshot_b64() -> str:
    return b64(take_screenshot_bytes())


def scale_image(png_bytes: bytes, max_dim: int) -> tuple[bytes, float]:
    """Downscale a screenshot so its longer edge is at most `max_dim`,
    returning the resized PNG and the scale factor applied (<=1.0).

    Anthropic's computer-use coordinate grounding degrades at native 4K/2K
    resolutions — observed live as clicks landing many hundreds of pixels
    off target on a 2560x1440 screen, intermittently. Sending a smaller
    image and scaling the model's coordinates back up (the long-standing
    recommended pattern for this tool family) fixes it; a scale of 1.0
    means no resize was needed.
    """
    img = Image.open(io.BytesIO(png_bytes))
    longest = max(img.width, img.height)
    if longest <= max_dim:
        return png_bytes, 1.0
    scale = max_dim / longest
    new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
    resized = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    resized.save(buf, format="PNG")
    return buf.getvalue(), scale


def crop_region(png_bytes: bytes, region: list[int], upscale: float = 2.0) -> bytes:
    """Crop a screenshot to `region` ([x1, y1, x2, y2]) and upscale it, for
    the computer-use "zoom" action — the model asks to see a small area
    more clearly rather than changing anything on screen."""
    img = Image.open(io.BytesIO(png_bytes))
    x1, y1, x2, y2 = region
    x1, x2 = sorted((max(0, min(x1, img.width)), max(0, min(x2, img.width))))
    y1, y2 = sorted((max(0, min(y1, img.height)), max(0, min(y2, img.height))))
    if x2 <= x1 or y2 <= y1:
        return png_bytes
    cropped = img.crop((x1, y1, x2, y2))
    if upscale != 1.0:
        size = (max(1, int(cropped.width * upscale)), max(1, int(cropped.height * upscale)))
        cropped = cropped.resize(size, Image.LANCZOS)
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue()


def _find_window_by_title(title_substring: str):
    """Return the hwnd of the first visible window whose title contains
    `title_substring`, or None if no match was found. Shared by
    window_exists() (a passive check) and focus_window() (which also
    activates the match)."""
    user32 = ctypes.windll.user32
    found = {"hwnd": None}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if title_substring.lower() in buf.value.lower():
            found["hwnd"] = hwnd
            return False
        return True

    user32.EnumWindows(_callback, 0)
    return found["hwnd"]


def window_exists(title_substring: str) -> bool:
    """Check whether a visible window matching `title_substring` exists,
    without focusing it. Used as a cheap pre-flight reachability check
    (architecture doc Section 6, "DATEV not reachable") before spending a
    computer-use API call."""
    return _find_window_by_title(title_substring) is not None


def focus_window(title_substring: str) -> bool:
    """Bring the first visible window whose title contains `title_substring`
    to the foreground. Returns False if no match was found.

    Exists because a click can land on the wrong target — or just activate
    the window instead of clicking through to it — when that window isn't
    already foreground; a freshly-launched automation process is exactly
    the kind of thing that can steal focus right before the first action.
    """
    user32 = ctypes.windll.user32
    hwnd = _find_window_by_title(title_substring)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE — undo minimize without forcing maximize
    user32.SetForegroundWindow(hwnd)
    return True


# ---------------------------------------------------------------------------
# win32 SendInput backend — mouse move/click and unicode key events.
# Structure layout follows the standard SendInput ctypes pattern; only
# meaningful on Windows, so the user32 handle is resolved lazily.
# ---------------------------------------------------------------------------

_PUL = ctypes.POINTER(ctypes.c_ulong)


class _KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _PUL),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _PUL),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KeyBdInput), ("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _InputUnion)]


_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_KEYUP = 0x0002

_BUTTON_FLAGS = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}


class _Win32Input:
    def __init__(self):
        self._user32 = ctypes.windll.user32  # raises on non-Windows

    def _send(self, inp: _Input) -> None:
        self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    def move_and_click(self, x: int, y: int, button: str = "left") -> None:
        screen = get_screen_size()
        abs_x = int(x * 65535 / max(screen.width - 1, 1))
        abs_y = int(y * 65535 / max(screen.height - 1, 1))

        move = _Input(type=_INPUT_MOUSE)
        move.u.mi = _MouseInput(abs_x, abs_y, 0, _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, 0, None)
        self._send(move)

        down_flag, up_flag = _BUTTON_FLAGS[button]
        down = _Input(type=_INPUT_MOUSE)
        down.u.mi = _MouseInput(0, 0, 0, down_flag, 0, None)
        self._send(down)

        up = _Input(type=_INPUT_MOUSE)
        up.u.mi = _MouseInput(0, 0, 0, up_flag, 0, None)
        self._send(up)

    def type_text(self, text: str) -> None:
        for ch in text:
            for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
                inp = _Input(type=_INPUT_KEYBOARD)
                inp.u.ki = _KeyBdInput(0, ord(ch), flags, 0, None)
                self._send(inp)


_win32: _Win32Input | None = None


def _get_win32() -> _Win32Input:
    global _win32
    if _win32 is None:
        _win32 = _Win32Input()
    return _win32


# ---------------------------------------------------------------------------
# Public action executor — dispatches Claude computer-use tool actions.
# ---------------------------------------------------------------------------

def execute(action: dict) -> None:
    """Execute one Anthropic computer-use tool action against the live screen."""
    kind = action.get("action")

    if _TARGET_WINDOW and kind != "wait":
        # Re-assert foreground focus right before every action, not just once at
        # the start of a run: observed live that the target window can lose
        # focus during the multi-second gap between one action and the next
        # (e.g. an API round-trip), silently dropping any keyboard input even
        # though clicks — being position-based — land fine regardless.
        focus_window(_TARGET_WINDOW)

    if kind == "wait":
        time.sleep(min(float(action.get("duration", 1)), 5))
        return
    if kind in ("screenshot", "cursor_position"):
        return

    if kind in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
        x, y = action["coordinate"]
        button = {
            "left_click": "left", "right_click": "right", "middle_click": "middle",
            "double_click": "left", "triple_click": "left",
        }[kind]
        clicks = 2 if kind == "double_click" else (3 if kind == "triple_click" else 1)
        if _BACKEND == "win32":
            for _ in range(clicks):
                _get_win32().move_and_click(x, y, button)
        else:
            pyautogui.click(x, y, clicks=clicks, button=button)
        return

    if kind == "left_click_drag":
        start = action.get("start_coordinate")
        end = action["coordinate"]
        if start:
            pyautogui.moveTo(*start)
        pyautogui.dragTo(*end, button="left", duration=0.2)
        return

    if kind == "mouse_move":
        x, y = action["coordinate"]
        pyautogui.moveTo(x, y)
        return

    if kind == "type":
        text = action.get("text", "")
        if _BACKEND == "win32":
            _get_win32().type_text(text)
        else:
            pyautogui.write(text, interval=0.01)
        return

    if kind == "key":
        keys = action.get("text", "").split("+")
        mapped = [_KEY_ALIASES.get(k, k.lower()) for k in keys]
        if len(mapped) > 1:
            pyautogui.hotkey(*mapped)
        else:
            pyautogui.press(mapped[0])
        return

    if kind == "scroll":
        x, y = action.get("coordinate", [None, None])
        amount = int(action.get("scroll_amount", 3))
        direction = action.get("scroll_direction", "down")
        sign = -1 if direction == "down" else 1
        pyautogui.scroll(sign * amount, x=x, y=y)
        return

    raise ValueError(f"Unsupported computer-use action: {kind!r}")
