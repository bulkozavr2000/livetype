"""Small desktop helper for UI checks (Windows, ctypes + Pillow).

    python ui/dev/wintool.py grab <out.png> [x y w h]     screenshot (physical pixels) of the screen or a region
    python ui/dev/wintool.py grabwin <title> <out.png> [pad]  screenshot of a window's rectangle
    python ui/dev/wintool.py fg                           foreground window as JSON
    python ui/dev/wintool.py screen                       primary screen size and work area (physical pixels)
    python ui/dev/wintool.py info <title>                 window rect / visibility / ex-style as JSON
    python ui/dev/wintool.py focus <title>                bring a window to the foreground
    python ui/dev/wintool.py serve                        the same commands as JSON lines on stdin (fast)
"""

from __future__ import annotations

import ctypes
import json
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor: physical pixels everywhere
except OSError:
    user32.SetProcessDPIAware()

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080

user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetForegroundWindow.restype = wintypes.HWND
user32.FindWindowW.restype = wintypes.HWND


def title_of(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def class_of(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def rect_of(hwnd) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def frame_of(hwnd) -> tuple[int, int, int, int]:
    """Visible bounds (without the invisible resize borders of Windows 10/11)."""
    r = wintypes.RECT()
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r))
    return (r.left, r.top, r.right, r.bottom) if hr == 0 else rect_of(hwnd)


def info(hwnd) -> dict:
    if not hwnd:
        return {"hwnd": 0}
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {
        "hwnd": int(hwnd),
        "title": title_of(hwnd),
        "class": class_of(hwnd),
        "pid": pid.value,
        "rect": rect_of(hwnd),
        "frame": frame_of(hwnd),
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "noactivate": bool(ex & WS_EX_NOACTIVATE),
        "transparent": bool(ex & WS_EX_TRANSPARENT),
        "layered": bool(ex & WS_EX_LAYERED),
        "topmost": bool(ex & WS_EX_TOPMOST),
        "toolwindow": bool(ex & WS_EX_TOOLWINDOW),
    }


def find(title: str):
    return user32.FindWindowW(None, title)


def grab(out: str, box: tuple[int, int, int, int] | None = None) -> None:
    from PIL import ImageGrab

    img = ImageGrab.grab(bbox=box, all_screens=True)
    img.save(out)


def click(x: int, y: int) -> None:
    MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.03)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def focus(hwnd) -> bool:
    VK_NONAME = 0xFC  # harmless key: lets this process move the foreground (as tests/e2e_desktop.py)
    for attempt in range(8):
        if user32.GetForegroundWindow() == hwnd:
            return True
        if attempt >= 3:
            # Some shell windows (a cloaked Search host, for one) keep the foreground against
            # SetForegroundWindow; a real click into the window always moves it.
            l, t, r, b = frame_of(hwnd)
            click((l + r) // 2, (t + b) // 2)
            time.sleep(0.3)
            continue
        user32.keybd_event(VK_NONAME, 0, 0, 0)
        user32.keybd_event(VK_NONAME, 0, 2, 0)
        fg = user32.GetForegroundWindow()
        me = kernel32.GetCurrentThreadId()
        other = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if other:
            user32.AttachThreadInput(me, other, True)
        user32.ShowWindow(hwnd, 5)
        user32.SetForegroundWindow(hwnd)
        if other:
            user32.AttachThreadInput(me, other, False)
        time.sleep(0.25)
    return user32.GetForegroundWindow() == hwnd


def ink(path: str, threshold: int = 200, top: float = 0.0, bottom: float = 1.0) -> int:
    """Pixels darker than `threshold` (0-255) in the horizontal band [top, bottom) of an image."""
    from PIL import Image

    im = Image.open(path).convert("L")
    w, h = im.size
    band = im.crop((0, int(h * top), w, max(int(h * top) + 1, int(h * bottom))))
    return sum(band.histogram()[: max(0, min(256, int(threshold)))])


def serve() -> int:
    """JSON lines on stdin -> JSON lines on stdout; keeps Pillow loaded so grabs take milliseconds."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        cmd, args = req.get("cmd"), req.get("args", [])
        try:
            if cmd == "grab":
                grab(args[0], tuple(int(v) for v in args[1:5]) if len(args) >= 5 else None)  # type: ignore[arg-type]
                res = {"ok": True}
            elif cmd == "fg":
                res = info(user32.GetForegroundWindow())
            elif cmd == "ink":
                res = {"ink": ink(args[0], *[float(a) for a in args[1:4]])}
            elif cmd == "info":
                res = info(find(args[0]))
            elif cmd == "focus":
                hwnd = find(args[0])
                res = {"ok": bool(hwnd) and focus(hwnd), "fg": info(user32.GetForegroundWindow())}
            elif cmd == "screen":
                r = wintypes.RECT()
                user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
                res = {"width": user32.GetSystemMetrics(0), "height": user32.GetSystemMetrics(1),
                       "work": [r.left, r.top, r.right, r.bottom]}
            else:
                res = {"error": f"unknown command {cmd}"}
        except Exception as exc:  # noqa: BLE001
            res = {"error": repr(exc)}
        sys.stdout.write(json.dumps({"id": req.get("id"), "result": res}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "serve":
        return serve()
    if cmd == "grab":
        box = tuple(int(v) for v in argv[2:6]) if len(argv) >= 6 else None
        grab(argv[1], box)  # type: ignore[arg-type]
        return 0
    if cmd == "grabwin":
        hwnd = find(argv[1])
        if not hwnd:
            print(json.dumps({"error": "not found"}))
            return 1
        pad = int(argv[3]) if len(argv) > 3 else 0
        l, t, r, b = rect_of(hwnd)
        grab(argv[2], (l - pad, t - pad, r + pad, b + pad))
        print(json.dumps(info(hwnd), ensure_ascii=False))
        return 0
    if cmd == "fg":
        print(json.dumps(info(user32.GetForegroundWindow()), ensure_ascii=False))
        return 0
    if cmd == "screen":
        r = wintypes.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
        print(json.dumps({"width": user32.GetSystemMetrics(0), "height": user32.GetSystemMetrics(1),
                          "work": [r.left, r.top, r.right, r.bottom]}))
        return 0
    if cmd == "info":
        print(json.dumps(info(find(argv[1])), ensure_ascii=False))
        return 0
    if cmd == "focus":
        hwnd = find(argv[1])
        ok = bool(hwnd) and focus(hwnd)
        print(json.dumps({"ok": ok, "fg": info(user32.GetForegroundWindow())}, ensure_ascii=False))
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
