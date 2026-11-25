"""README media: demo GIF + screenshots of the installed app over a neutral background.

python scripts/make_screenshots.py [path\\to\\LiveType.exe]   -> docs/demo.gif, docs/*.png
Uses tests/data/ru_sample.wav instead of the microphone and temporary settings.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

_ptr = Path(os.environ.get("APPDATA", "")) / "LiveType" / "home.txt"
if "LIVETYPE_HOME" not in os.environ and _ptr.is_file():
    os.environ["LIVETYPE_HOME"] = _ptr.read_text(encoding="utf-8").strip()
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="livetype-shots-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PIL import Image, ImageGrab  # noqa: E402

from e2e_desktop import focus, hotkey  # noqa: E402
from livetype import win  # noqa: E402
from livetype.config import DATA_DIR  # noqa: E402

DOCS = ROOT / "docs"
WAV = ROOT / "tests" / "data" / "ru_sample.wav"
LOG = DATA_DIR / "logs" / "livetype.log"
BACKDROP = """
import tkinter as tk, ctypes
ctypes.windll.shcore.SetProcessDpiAwareness(2)
r = tk.Tk(); r.title("LiveType backdrop"); r.configure(bg="#E9ECF2")
r.geometry(f"{r.winfo_screenwidth()}x{r.winfo_screenheight()}+0+0"); r.overrideredirect(True)
r.after(180000, r.destroy); r.mainloop()
"""
NOTE = """
import sys, tkinter as tk, ctypes
ctypes.windll.shcore.SetProcessDpiAwareness(2)
x, y, w, h = map(int, sys.argv[1:5])
r = tk.Tk(); r.title("Заметка — LiveType demo"); r.geometry(f"{w}x{h}+{x}+{y}")
t = tk.Text(r, font=("Segoe UI", 15), wrap="word", padx=22, pady=18, relief="flat", bd=0)
t.pack(fill="both", expand=True); r.after(300, lambda: (r.lift(), r.focus_force(), t.focus_set()))
r.after(180000, r.destroy); r.mainloop()
"""


def find_title(title: str, timeout: float = 15) -> int:
    t_end = time.time() + timeout
    while time.time() < t_end:
        h = win.user32.FindWindowW(None, title)
        if h:
            return h
        time.sleep(0.1)
    raise TimeoutError(title)


def new_log(start: int) -> str:
    with open(LOG, "rb") as f:
        f.seek(start)
        return f.read().decode("utf-8", "replace")


def wait_log(start: int, needle: str, timeout: float) -> bool:
    t_end = time.time() + timeout
    while time.time() < t_end:
        if LOG.exists() and needle in new_log(start):
            return True
        time.sleep(0.1)
    return False


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    win.user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    win.user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def main() -> int:
    win.set_dpi_aware()
    DOCS.mkdir(exist_ok=True)
    exe = sys.argv[1] if len(sys.argv) > 1 else os.path.expandvars(r"%LOCALAPPDATA%\Programs\LiveType\LiveType.exe")
    sw, sh = win.user32.GetSystemMetrics(0), win.user32.GetSystemMetrics(1)
    w, h = 860, 250
    x, y = (sw - w) // 2, sh - 115 - 119 - 110 - h
    crop = (x - 70, y - 60, x + w + 70, sh - 95)
    procs = [subprocess.Popen([sys.executable, "-c", BACKDROP])]
    find_title("LiveType backdrop")
    procs.append(subprocess.Popen([sys.executable, "-c", NOTE, str(x), str(y), str(w), str(h)]))
    note = find_title("Заметка — LiveType demo")
    log0 = LOG.stat().st_size if LOG.exists() else 0
    env = os.environ.copy()
    env["LIVETYPE_ENGINE_ARGS"] = json.dumps(["--source", str(WAV)])
    app = subprocess.Popen([exe, "--hidden"], env=env)
    frames: list[Image.Image] = []
    try:
        assert wait_log(log0, "models loaded", 90), "engine did not start"
        time.sleep(2.0)
        assert focus(note)
        hotkey()
        t0 = time.time()
        while time.time() - t0 < 26.6:
            frames.append(ImageGrab.grab(bbox=crop))
            if 9.0 < time.time() - t0 < 9.3:
                frames[-1].save(DOCS / "screenshot-listening.png")
            time.sleep(max(0.0, 0.25 - 0.05))
        hotkey()
        for _ in range(10):
            frames.append(ImageGrab.grab(bbox=crop))
            time.sleep(0.2)
        assert wait_log(log0, "EVENT done", 60)
        time.sleep(0.6)
        done = ImageGrab.grab(bbox=crop)
        done.save(DOCS / "screenshot-done.png")
        frames += [done] * 8
        # control panel: a second launch opens it
        subprocess.Popen([exe], env=env)
        panel = find_title("LiveType", timeout=10)
        time.sleep(1.5)
        l, t, r, b = window_rect(panel)
        ImageGrab.grab(bbox=(l, t, r, b)).save(DOCS / "panel.png")
    finally:
        subprocess.run(["taskkill", "/PID", str(app.pid), "/T", "/F"], capture_output=True)
        subprocess.run(["taskkill", "/IM", "LiveType.exe", "/T", "/F"], capture_output=True)
        for p in procs:
            p.kill()
    scale = 760 / frames[0].width
    small = [f.resize((760, int(f.height * scale)), Image.LANCZOS).convert("P", palette=Image.ADAPTIVE, colors=128)
             for f in frames[::2]]
    small[0].save(DOCS / "demo.gif", save_all=True, append_images=small[1:], duration=400, loop=0, optimize=True)
    for f in sorted(DOCS.iterdir()):
        print(f"{f.stat().st_size / 1e6:6.2f} MB  {f.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
