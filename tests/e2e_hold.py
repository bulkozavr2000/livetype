"""End-to-end: Right Alt in "hold" mode (Удерживать хоткей), like the user's Type setup.

python tests/e2e_hold.py — hold Right Alt for the whole recording, release -> final fix.
Uses a temporary settings folder, so the user's own config.json is not touched.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_ptr = Path(os.environ.get("APPDATA", "")) / "LiveType" / "home.txt"
if "LIVETYPE_HOME" not in os.environ and _ptr.is_file():  # keep using the real model folder
    os.environ["LIVETYPE_HOME"] = _ptr.read_text(encoding="utf-8").strip()
TMP = Path(tempfile.mkdtemp(prefix="livetype-hold-"))
os.environ["APPDATA"] = str(TMP)  # must happen before livetype.config is imported
(TMP / "LiveType").mkdir()
(TMP / "LiveType" / "config.json").write_text(
    json.dumps({"hotkey": "RightAlt", "activation_mode": "hold", "sounds": False}), encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from e2e_desktop import TITLE, USER, find_window, focus, read, wait_log, work_area  # noqa: E402
from livetype import win  # noqa: E402
from livetype.textops import wer  # noqa: E402

OUT = ROOT / "tests" / "out"
WAV = ROOT / "tests" / "data" / "ru_sample.wav"
REF = (ROOT / "tests" / "data" / "ru_sample.txt").read_text(encoding="utf-8").replace("|", " ").strip()


def right_alt(up: bool) -> None:
    win._send([win._key(0xA5, up=up, extra=USER)], pause=0)


def main() -> int:
    win.set_dpi_aware()
    OUT.mkdir(parents=True, exist_ok=True)
    L, T, R, B = work_area()
    w, h = 900, 220
    x, y = L + (R - L - w) // 2, B - 420
    field = OUT / "hold_field.txt"
    app_log = OUT / "hold_app.log"
    target = subprocess.Popen([sys.executable, str(ROOT / "tests" / "target_window.py"), str(field),
                               str(x), str(y), str(w), str(h)])
    app = None
    checks: dict[str, bool] = {}
    try:
        hwnd = find_window(TITLE)
        time.sleep(0.6)
        assert focus(hwnd)
        with open(app_log, "w", encoding="utf-8") as logf:
            app = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "livetype", "--source", str(WAV),
                                    "--once", "--debug", "--no-tray"], cwd=ROOT, stdout=logf,
                                   stderr=subprocess.STDOUT, env=os.environ.copy())
        assert wait_log(app_log, r"^READY hotkey_ok=True", 60), read(app_log)
        time.sleep(0.8)
        assert focus(hwnd)
        right_alt(up=False)  # press and HOLD
        t0 = time.time()
        time.sleep(8)
        mid = read(field)
        time.sleep(max(0.0, 26.5 - (time.time() - t0)))
        right_alt(up=True)  # release = stop
        done = wait_log(app_log, r"^EVENT done .*$", 60)
        app.wait(15)
        log_text = read(app_log)
        final = re.search(r"^FINAL: (.*)$", log_text, re.M).group(1)
        content = read(field)
        checks = {
            "hold: words typed while the key is held": len(mid.split()) >= 4,
            "release stopped dictation and fixed the text": bool(done) and "outcome=fixed" in done.group(0),
            "field == final corrected text": content == final,
            "final text accurate": wer(REF, final) <= 0.05,
            "no stray Alt effects (text intact)": content.startswith("Привет"),
        }
    finally:
        if app and app.poll() is None:
            app.kill()
        target.kill()
    for k, v in checks.items():
        print(("PASS " if v else "FAIL ") + k)
    ok = bool(checks) and all(checks.values())
    print("RESULT:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
