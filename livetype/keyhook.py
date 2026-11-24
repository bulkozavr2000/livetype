"""Global hotkey listener — runs as its own small process (python -m livetype.keyhook <spec>),
like Type's windows-key-listener.exe: a low-level keyboard hook in a process that does nothing
else, so the keyboard never lags while the main app is busy recognizing speech.

Supports single keys (RightAlt, RightCtrl, F9…), combos (ctrl+alt+d) and modifier-only combos
(ctrl+win). The hotkey's own key is swallowed so it never reaches the app you type in.

stdin:  "set <spec>" | "capture" | "capture off" | "esc on|off" | "quit"
stdout: "ready <spec>" | "error <msg>" | "down" | "up" | "esc" | "captured <spec>" | "capture_cancelled"

Capturing a new hotkey ends with the first valid key or combo; Esc, "capture off" or 20 s of
waiting cancel it. A bare letter, digit, Space or Enter is not accepted: as a global hotkey it
would swallow that key everywhere.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MAGIC = 0x4C567970  # LiveType's own keystrokes (see win.py) are ignored
LRESULT = ctypes.c_ssize_t
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_QUIT = 0x12
MASK_VK = 0xE8  # unassigned key, sent to stop a swallowed Alt/Win from opening a menu
REPEAT_GAP_MS = 1500  # longer than the maximum keyboard repeat delay (1 s)
CAPTURE_TIMEOUT_MS = 20_000
SOLO_OK = set(range(0x70, 0x88)) | {0x13, 0x91, 0x2D, 0x5D}  # F1-F24, Pause, ScrollLock, Insert, Menu

SIDE = {  # single modifier keys
    "rightalt": 0xA5, "leftalt": 0xA4, "rightctrl": 0xA3, "leftctrl": 0xA2,
    "rightshift": 0xA1, "leftshift": 0xA0, "leftwin": 0x5B, "rightwin": 0x5C,
}
GENERIC = {  # modifiers in combos -> any side
    "ctrl": (0xA2, 0xA3), "control": (0xA2, 0xA3), "alt": (0xA4, 0xA5), "shift": (0xA0, 0xA1),
    "win": (0x5B, 0x5C), "super": (0x5B, 0x5C),
}
MOD_VKS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C}
KEYS = {
    "space": 0x20, "pause": 0x13, "scrolllock": 0x91, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "enter": 0x0D, "tab": 0x09, "capslock": 0x14, "menu": 0x5D, "`": 0xC0,
}
KEYS |= {f"f{i}": 0x6F + i for i in range(1, 25)}
NAMES = {v: k for k, v in KEYS.items()}
SIDE_NAMES = {0xA5: "RightAlt", 0xA4: "LeftAlt", 0xA3: "RightCtrl", 0xA2: "LeftCtrl",
              0xA1: "RightShift", 0xA0: "LeftShift", 0x5B: "LeftWin", 0x5C: "RightWin"}
GENERIC_OF = {0xA2: "ctrl", 0xA3: "ctrl", 0xA4: "alt", 0xA5: "alt", 0xA0: "shift", 0xA1: "shift",
              0x5B: "win", 0x5C: "win"}


def parse(spec: str) -> tuple[list[tuple[int, ...]], tuple[int, ...]]:
    """'ctrl+alt+d' -> ([ctrl vks, alt vks], (0x44,)); 'RightAlt' -> ([], (0xA5,))."""
    raw = spec.lower().replace(" ", "")
    parts = raw.split("+")
    if not raw or any(not p for p in parts):
        raise ValueError(f"bad hotkey {spec!r}")
    if len(parts) == 1 and parts[0] in GENERIC:  # "ctrl" alone would fire on every Ctrl press
        raise ValueError("use a side-specific key, e.g. RightCtrl")
    mods, main = [], None
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        if p in SIDE and (last or len(parts) == 1):
            main = (SIDE[p],)
        elif p in GENERIC and not last:
            mods.append(GENERIC[p])
        elif p in GENERIC and last:  # modifier-only combo, e.g. ctrl+win
            main = GENERIC[p]
        elif p in KEYS:
            main = (KEYS[p],)
        elif len(p) == 1 and p.isalnum():
            main = (ord(p.upper()),)
        else:
            raise ValueError(f"unknown key {p!r}")
    if main is None:
        raise ValueError("no main key")
    return mods, main


class KBD(ctypes.Structure):
    _fields_ = (("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t))


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t))


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32))
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _U))


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = LRESULT
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetTickCount.restype = wintypes.DWORD


def held(group: tuple[int, ...]) -> bool:
    return any(user32.GetAsyncKeyState(v) & 0x8000 for v in group)


def elapsed(now: int, since: int) -> int:
    return (now - since) & 0xFFFFFFFF  # tick counts wrap after 49.7 days


def out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


class Hook:
    def __init__(self, spec: str):
        self.set(spec)
        self.down: set[int] = set()
        self.active = False  # hotkey currently held
        self.esc_armed = False  # while dictating, Esc cancels (like Type)
        self.last_main = 0
        self.cap_started = 0
        self.capturing = False
        self.cap_mods: list[int] = []
        self.cap_other = False
        self.proc = HOOKPROC(self.on_key)

    def set(self, spec: str) -> None:
        self.mods, self.main = parse(spec)
        self.spec = spec

    def _mask(self) -> None:
        arr = (INPUT * 2)()
        for i, flags in enumerate((0, 2)):
            arr[i].type = 1
            arr[i].ki = KEYBDINPUT(MASK_VK, 0, flags, 0, MAGIC)
        user32.SendInput(2, arr, ctypes.sizeof(INPUT))

    def on_key(self, code: int, wparam: int, lparam: int) -> int:
        if code != 0:
            return user32.CallNextHookEx(None, code, wparam, lparam)
        k = ctypes.cast(lparam, ctypes.POINTER(KBD)).contents
        if k.dwExtraInfo == MAGIC:
            return user32.CallNextHookEx(None, code, wparam, lparam)
        vk, is_down = k.vkCode, wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        if k.scanCode == 0x21D and vk in (0xA2, 0x11):
            # layouts with AltGr send a fake Left Ctrl together with Right Alt: swallow it too,
            # otherwise apps think Ctrl is held and typed letters turn into shortcuts
            if self.capturing or 0xA5 in self.main:
                return 1
            return user32.CallNextHookEx(None, code, wparam, lparam)
        repeat = is_down and vk in self.down
        if is_down:
            self.down.add(vk)
        else:
            self.down.discard(vk)
        if self.capturing:
            r = self._capture(vk, is_down, repeat, k.time)
            if r != -1:
                return r
        if vk == 0x1B and self.esc_armed:
            if is_down and not repeat:
                out("esc")
            return 1
        if vk in self.main:
            if is_down:
                if self.active and elapsed(k.time, self.last_main) < REPEAT_GAP_MS:
                    self.last_main = k.time  # auto-repeat while the key is held
                    return 1
                if self.active:  # its release was lost (e.g. Ctrl+Alt+Del, lock screen): start over
                    self.active = False
                    out("up")
                # modifiers from the real key state: our own record goes stale when a release
                # happens on the secure desktop, and then a bare letter would fire the hotkey
                if all(held(m) for m in self.mods):
                    self.active, self.last_main = True, k.time
                    out("down")
                    if vk in (0x5B, 0x5C) or any(set(m) & {0xA4, 0xA5, 0x5B, 0x5C} for m in self.mods):
                        self._mask()
                    return 1
            elif self.active:
                self.active = False
                out("up")
                return 1  # swallow the release of our key
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def cancel_capture(self) -> None:
        if self.capturing:
            self.capturing = False
            self.cap_mods, self.cap_other = [], False
            out("capture_cancelled")

    def _capture(self, vk: int, is_down: bool, repeat: bool, t: int) -> int:
        if elapsed(t, self.cap_started) > CAPTURE_TIMEOUT_MS:
            self.cancel_capture()
            return -1
        if vk == 0x1B:  # Esc cancels
            if is_down:
                self.cancel_capture()
            return 1
        if is_down and not repeat:
            if vk in MOD_VKS:
                if vk not in self.cap_mods:
                    self.cap_mods.append(vk)
            else:
                name = NAMES.get(vk) or (chr(vk).lower() if 0x30 <= vk <= 0x5A else None)
                mods = []
                for m in self.cap_mods:
                    g = GENERIC_OF.get(m)
                    if g and g not in mods and m in self.down:
                        mods.append(g)
                # a bare letter/space/enter as a global hotkey would hijack normal typing
                if name and (mods or vk in SOLO_OK):
                    self._captured("+".join([*mods, name]))
                self.cap_other = True
            return 1
        if not is_down and vk in MOD_VKS:
            if not self.cap_other and self.cap_mods and vk == self.cap_mods[-1]:
                others = [GENERIC_OF[m] for m in self.cap_mods[:-1] if m in self.down and m in GENERIC_OF]
                spec = "+".join([*others, GENERIC_OF[vk]]) if others else SIDE_NAMES.get(vk, "")
                if spec:
                    self._captured(spec)
            return 1
        return 1

    def _captured(self, spec: str) -> None:
        self.capturing = False
        self.cap_mods, self.cap_other = [], False
        try:
            self.set(spec)
            out(f"captured {spec}")
        except ValueError as e:
            out(f"error {e}")


def main(spec: str | None = None) -> None:
    hook = Hook(spec or (sys.argv[1] if len(sys.argv) > 1 else "RightAlt"))
    tid = kernel32.GetCurrentThreadId()

    def commands() -> None:
        for line in sys.stdin:
            cmd, _, arg = line.strip().partition(" ")
            if cmd == "set":
                try:
                    hook.set(arg)
                    out(f"ready {arg}")
                except ValueError as e:
                    out(f"error {e}")
            elif cmd == "capture" and arg == "off":
                hook.cancel_capture()
            elif cmd == "capture":
                hook.down.clear()
                hook.cap_started = kernel32.GetTickCount()
                hook.capturing, hook.cap_mods, hook.cap_other = True, [], False
            elif cmd == "esc":
                hook.esc_armed = arg == "on"
            elif cmd == "quit":
                break
        user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)

    threading.Thread(target=commands, daemon=True).start()
    h = user32.SetWindowsHookExW(WH_KEYBOARD_LL, hook.proc, kernel32.GetModuleHandleW(None), 0)
    if not h:
        out(f"error SetWindowsHookEx failed ({ctypes.get_last_error()})")
        return
    out(f"ready {hook.spec}")
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        pass
    user32.UnhookWindowsHookEx(h)


if __name__ == "__main__":
    main()
