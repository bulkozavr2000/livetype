"""End-to-end with the real UI: Electron shell (ui/) + Python engine, a WAV instead of the microphone.

python tests/e2e_ui.py — hotkey, capsule, live typing, final fix in place.
Settings live in a temporary folder, so the user's own settings are not touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ptr = Path(os.environ.get("APPDATA", "")) / "LiveType" / "home.txt"
if "LIVETYPE_HOME" not in os.environ and _ptr.is_file():  # keep using the real model folder
    os.environ["LIVETYPE_HOME"] = _ptr.read_text(encoding="utf-8").strip()
TMP = Path(tempfile.mkdtemp(prefix="livetype-ui-"))
os.environ["APPDATA"] = str(TMP)  # before livetype.config is imported (also isolates Electron's userData)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PIL import ImageGrab  # noqa: E402

from e2e_desktop import TITLE, find_window, focus, hotkey, read, work_area  # noqa: E402
from livetype import win  # noqa: E402
from livetype.textops import wer  # noqa: E402

OUT = ROOT / "tests" / "out"
WAV = ROOT / "tests" / "data" / "ru_sample.wav"
REF = (ROOT / "tests" / "data" / "ru_sample.txt").read_text(encoding="utf-8").replace("|", " ").strip()
from livetype.config import DATA_DIR  # noqa: E402

ELECTRON = Path(os.environ.get("LIVETYPE_ELECTRON", ROOT / "ui" / "node_modules" / "electron" / "dist" / "electron.exe"))
LOG = DATA_DIR / "logs" / "livetype.log"


def new_log(start: int) -> str:
    try:
        with open(LOG, "rb") as f:
            f.seek(start)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def wait_new_log(start: int, needle: str, timeout: float) -> str | None:
    t_end = time.time() + timeout
    while time.time() < t_end:
        for line in new_log(start).splitlines():
            if needle in line:
                return line
        time.sleep(0.1)
    return None


def main() -> int:
    win.set_dpi_aware()
    OUT.mkdir(parents=True, exist_ok=True)
    L, T, R, B = work_area()
    sw, sh = win.user32.GetSystemMetrics(0), win.user32.GetSystemMetrics(1)
    w, h = 900, 200
    x, y = L + (R - L - w) // 2, max(T + 20, sh - 115 - 119 - 150 - h - 60)
    bbox = (max(0, x - 60), max(0, y - 40), min(sw, x + w + 60), sh - 60)
    field = OUT / "ui_e2e_field.txt"
    target = subprocess.Popen([sys.executable, str(ROOT / "tests" / "target_window.py"), str(field),
                               str(x), str(y), str(w), str(h)])
    log0 = LOG.stat().st_size if LOG.exists() else 0
    env = os.environ.copy()
    env["LIVETYPE_ENGINE_ARGS"] = json.dumps(["--source", str(WAV)])
    ui = None
    checks: dict[str, bool] = {}
    info: dict[str, object] = {}
    try:
        hwnd = find_window(TITLE)
        time.sleep(0.6)
        assert focus(hwnd)
        app_exe = sys.argv[sys.argv.index("--app") + 1] if "--app" in sys.argv else None  # installed LiveType.exe
        ui = subprocess.Popen([app_exe, "--hidden"] if app_exe else [str(ELECTRON), str(ROOT / "ui"), "--hidden"],
                              env=env)
        assert wait_new_log(log0, "models loaded", 90), "engine did not start:\n" + new_log(log0)
        time.sleep(2.0)
        info["focus_kept_after_ui_start"] = win.foreground() == hwnd
        assert focus(hwnd)
        hotkey()
        t0 = time.time()
        time.sleep(7.0)
        ImageGrab.grab(bbox=bbox).save(OUT / "ui_e2e_1_listening.png")
        info["field_at_7s"] = read(field)
        info["fg_is_target_while_listening"] = win.foreground() == hwnd
        time.sleep(max(0.0, 15.0 - (time.time() - t0)))
        ImageGrab.grab(bbox=bbox).save(OUT / "ui_e2e_2_listening.png")
        time.sleep(max(0.0, 26.5 - (time.time() - t0)))
        hotkey()
        time.sleep(0.15)
        ImageGrab.grab(bbox=bbox).save(OUT / "ui_e2e_3_processing.png")
        done = wait_new_log(log0, "EVENT done", 60)
        time.sleep(0.8)
        ImageGrab.grab(bbox=bbox).save(OUT / "ui_e2e_4_done.png")
        content = ""
        for _ in range(20):  # the test window rewrites its file every 150 ms: don't catch it mid-write
            content = read(field)
            if content:
                break
            time.sleep(0.1)
        info.update(done=done, field=content, start=wait_new_log(log0, "EVENT start", 1))
        checks = {
            "engine started by the UI and the hotkey worked": bool(info["start"]),
            "words typed while speaking (7 s)": len(str(info["field_at_7s"]).split()) >= 4,
            "UI never stole focus": bool(info["fg_is_target_while_listening"]),
            "final fix applied in place": bool(done) and "outcome=fixed" in done,
            "comma fixed by the final pass": "посмотрим, насколько" in content,
            "text accurate": wer(REF, content) <= 0.05,
        }
    finally:
        if ui is not None:
            subprocess.run(["taskkill", "/PID", str(ui.pid), "/T", "/F"], capture_output=True)
        target.kill()
        (OUT / "ui_e2e_result.json").write_text(json.dumps({"checks": checks, **info}, ensure_ascii=False,
                                                           indent=2, default=str), encoding="utf-8")
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    ok = bool(checks) and all(checks.values())
    print("RESULT:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
