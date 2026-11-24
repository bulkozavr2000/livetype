"""Windows plumbing (ctypes): typing via SendInput, window info, clipboard,
watching the user's own keyboard/mouse, global hotkey."""

from __future__ import annotations

import ctypes
import os
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
MAGIC = 0x4C567970  # tag on our own keystrokes, so the watcher can tell them from the user's

# ------------------------------------------------------------------ SendInput
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_BACK, VK_TAB, VK_RETURN = 0x08, 0x09, 0x0D
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_SPACE, VK_END, VK_HOME, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x20, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28
VK_LWIN, VK_RWIN = 0x5B, 0x5C
MODIFIER_VKS = {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}
_EXTENDED = {VK_END, VK_HOME, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, 0x21, 0x22, 0x2D, 0x2E}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD))


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short


def _key(vk: int, up: bool = False, extra: int = MAGIC) -> INPUT:
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, extra)
    return i


def _unicode(code_unit: int, up: bool = False, extra: int = MAGIC) -> INPUT:
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki = KEYBDINPUT(0, code_unit, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, extra)
    return i


def _send(inputs: list[INPUT], batch: int = 96, pause: float = 0.003) -> None:
    for k in range(0, len(inputs), batch):
        part = inputs[k : k + batch]
        arr = (INPUT * len(part))(*part)
        user32.SendInput(len(part), arr, ctypes.sizeof(INPUT))
        if pause:
            time.sleep(pause)


def type_text(text: str, extra: int = MAGIC) -> None:
    """Type any Unicode text into the focused field, independent of keyboard layout."""
    seq: list[INPUT] = []
    for ch in text:
        if ch == "\n":
            seq += [_key(VK_RETURN, extra=extra), _key(VK_RETURN, up=True, extra=extra)]
            continue
        data = ch.encode("utf-16-le")
        for k in range(0, len(data), 2):
            cu = int.from_bytes(data[k : k + 2], "little")
            seq += [_unicode(cu, extra=extra), _unicode(cu, up=True, extra=extra)]
    _send(seq)


def press(vk: int, times: int = 1, *, shift: bool = False, ctrl: bool = False, extra: int = MAGIC) -> None:
    if times <= 0:
        return
    seq: list[INPUT] = []
    if ctrl:
        seq.append(_key(VK_CONTROL, extra=extra))
    if shift:
        seq.append(_key(VK_SHIFT, extra=extra))
    for _ in range(times):
        seq += [_key(vk, extra=extra), _key(vk, up=True, extra=extra)]
    if shift:
        seq.append(_key(VK_SHIFT, up=True, extra=extra))
    if ctrl:
        seq.append(_key(VK_CONTROL, up=True, extra=extra))
    _send(seq, batch=200, pause=0.002)


_SIDE_MODIFIERS = (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, VK_LWIN, VK_RWIN)
IGNORED_MODIFIERS: set[int] = set()  # the dictation hotkey itself (swallowed, so apps never see it)


def modifiers_down() -> bool:
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in _SIDE_MODIFIERS if vk not in IGNORED_MODIFIERS)


def wait_modifiers_released(timeout: float = 3.0) -> bool:
    t_end = time.monotonic() + timeout
    while modifiers_down():
        if time.monotonic() > t_end:
            return False
        time.sleep(0.02)
    return True


# ------------------------------------------------------------------ windows
class RECT(ctypes.Structure):
    _fields_ = (("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG))


class GUITHREADINFO(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", RECT),
    )


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetParent.argtypes = (wintypes.HWND,)
user32.GetParent.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.GetWindowLongW.restype = wintypes.LONG
user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.LONG)
user32.SetWindowLongW.restype = wintypes.LONG
user32.SystemParametersInfoW.argtypes = (wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT)
user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
user32.FindWindowW.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetGUIThreadInfo.argtypes = (wintypes.DWORD, ctypes.POINTER(GUITHREADINFO))
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


def foreground() -> int:
    return user32.GetForegroundWindow() or 0


def is_own_window(hwnd: int) -> bool:
    """True if the window belongs to this LiveType process (never type into ourselves)."""
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    own = {kernel32.GetCurrentProcessId()}
    ui_pid = os.environ.get("LIVETYPE_UI_PID", "")  # the Electron UI that started this engine
    if ui_pid.isdigit():
        own.add(int(ui_pid))
    return pid.value in own


def focused_control(hwnd: int) -> int:
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if tid and user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
        return info.hwndFocus or 0
    return 0


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_process(hwnd: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


# ------------------------------------------------------------------ clipboard
kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
user32.OpenClipboard.argtypes = (wintypes.HWND,)
user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = (wintypes.UINT,)
user32.GetClipboardData.restype = wintypes.HANDLE
CF_UNICODETEXT = 13


def _open_clipboard() -> bool:
    for _ in range(20):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.03)
    return False


def set_clipboard(text: str) -> bool:
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        user32.CloseClipboard()


def get_clipboard() -> str:
    if not _open_clipboard():
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


# ------------------------------------------------------------------ message-loop threads
class MSG(ctypes.Structure):
    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    )


WM_QUIT, WM_HOTKEY = 0x0012, 0x0312
user32.GetMessageW.argtypes = (ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)


class _LoopThread(threading.Thread):
    def __init__(self, name: str):
        super().__init__(name=name, daemon=True)
        self.tid = 0
        self.ready = threading.Event()

    def _loop(self, on_msg: Callable[[MSG], None] | None = None) -> None:
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if on_msg:
                on_msg(msg)

    def stop(self) -> None:
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
        self.join(timeout=2)


# --- global hotkey
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_KEYS = {
    "space": VK_SPACE, "pause": 0x13, "scrolllock": 0x91, "insert": 0x2D, "home": VK_HOME, "end": VK_END,
    "pageup": 0x21, "pagedown": 0x22, "enter": VK_RETURN, "tab": VK_TAB, "`": 0xC0,
}
_KEYS |= {f"f{i}": 0x6F + i for i in range(1, 25)}


def parse_hotkey(spec: str) -> tuple[int, int]:
    mods, vk = 0, 0
    for part in spec.lower().replace(" ", "").split("+"):
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _KEYS:
            vk = _KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        else:
            raise ValueError(f"Непонятная клавиша: {part!r}")
    if not vk:
        raise ValueError(f"В сочетании {spec!r} нет основной клавиши")
    return mods, vk


class HotkeyThread(_LoopThread):
    def __init__(self, spec: str, callback: Callable[[], None]):
        super().__init__("hotkey")
        self.mods, self.vk = parse_hotkey(spec)
        self.callback = callback
        self.ok = False

    def run(self) -> None:
        self.tid = kernel32.GetCurrentThreadId()
        self.ok = bool(user32.RegisterHotKey(None, 1, self.mods | MOD_NOREPEAT, self.vk))
        self.ready.set()
        if not self.ok:
            return
        try:
            self._loop(lambda m: self.callback() if m.message == WM_HOTKEY else None)
        finally:
            user32.UnregisterHotKey(None, 1)


# --- the user's own typing and clicks (low-level hooks, only while dictating)
WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_BUTTONDOWNS = {0x0201, 0x0204, 0x0207, 0x020B}  # L, R, M, X


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = (
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = (
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class InputWatcher(_LoopThread):
    """Counts the user's own key presses and mouse clicks. Our tagged keystrokes,
    modifier keys and the dictation hotkey itself are ignored."""

    def __init__(self, ignore_vks: set[int] | None = None):
        super().__init__("input-watcher")
        self.ignore = set(ignore_vks or ()) | MODIFIER_VKS
        self.count = 0
        self.last_time = 0.0
        self.last_kind = ""
        self._procs = (HOOKPROC(self._on_key), HOOKPROC(self._on_mouse))  # keep refs alive

    def _mark(self, kind: str) -> None:
        self.count += 1
        self.last_time = time.monotonic()
        self.last_kind = kind

    def _on_key(self, code: int, wparam: int, lparam: int) -> int:
        if code == 0 and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            k = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            if k.dwExtraInfo != MAGIC and k.vkCode not in self.ignore:
                self._mark("key")
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_mouse(self, code: int, wparam: int, lparam: int) -> int:
        if code == 0 and wparam in WM_BUTTONDOWNS:
            m = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if m.dwExtraInfo != MAGIC:
                self._mark("click")
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def run(self) -> None:
        self.tid = kernel32.GetCurrentThreadId()
        hmod = kernel32.GetModuleHandleW(None)
        hooks = [
            user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._procs[0], hmod, 0),
            user32.SetWindowsHookExW(WH_MOUSE_LL, self._procs[1], hmod, 0),
        ]
        self.ready.set()
        try:
            self._loop()
        finally:
            for h in hooks:
                if h:
                    user32.UnhookWindowsHookEx(h)


# ------------------------------------------------------------------ misc
def set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


class SingleInstance:
    def __init__(self, name: str = "Local\\LiveTypeDictation"):
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        self.handle = kernel32.CreateMutexW(None, False, name)
        self.already_running = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
