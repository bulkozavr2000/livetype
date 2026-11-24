"""End-to-end check on the real desktop (it types into a real window!).

    python tests/e2e_desktop.py plain   # dictate, words appear live, final fix in place
    python tests/e2e_desktop.py edit    # same, but "the user" clicks into the text and edits mid-dictation

Instead of the microphone, the app plays tests/data/ru_sample.wav in real time.
The hotkey, typing, overlay and final fix are the real ones. Screenshots go to tests/out/.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

# default settings in a temporary folder (the user's own config.json is not used or touched),
# models from the real data folder
_ptr = Path(os.environ.get("APPDATA", "")) / "LiveType" / "home.txt"
if "LIVETYPE_HOME" not in os.environ and _ptr.is_file():
    os.environ["LIVETYPE_HOME"] = _ptr.read_text(encoding="utf-8").strip()
if not Path(os.environ.get("APPDATA", "")).name.startswith("livetype-"):  # e2e_hold/e2e_ui set their own
    os.environ["APPDATA"] = tempfile.mkdtemp(prefix="livetype-e2e-")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import ImageGrab  # noqa: E402

from livetype import win  # noqa: E402
from livetype.config import Config  # noqa: E402
from livetype.textops import wer  # noqa: E402

OUT = ROOT / "tests" / "out"
WAV = ROOT / "tests" / "data" / "ru_sample.wav"
REF = (ROOT / "tests" / "data" / "ru_sample.txt").read_text(encoding="utf-8").replace("|", " ").strip()
TITLE = "LiveType — тестовое окно"
USER = 0  # events without our tag look like the real user


def work_area() -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    win.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right, r.bottom


def find_window(title: str, timeout: float = 15) -> int:
    t_end = time.time() + timeout
    while time.time() < t_end:
        h = win.user32.FindWindowW(None, title)
        if h:
            return h
        time.sleep(0.1)
    raise TimeoutError(title)


def focus(hwnd: int) -> bool:
    for _ in range(10):
        if win.foreground() == hwnd:
            return True
        win.press(0xE8, extra=USER)  # unassigned key: lets this process move the foreground
        fg = win.foreground()
        me = win.kernel32.GetCurrentThreadId()
        other = win.user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if other:
            win.user32.AttachThreadInput(me, other, True)
        win.user32.SetForegroundWindow(hwnd)
        if other:
            win.user32.AttachThreadInput(me, other, False)
        time.sleep(0.25)
    return win.foreground() == hwnd


def hotkey() -> None:  # the configured hotkey, as if pressed by the user
    from livetype import keyhook

    mods, main = keyhook.parse(Config.load().hotkey)
    held, vk = [group[0] for group in mods], main[0]
    seq = [win._key(m, extra=USER) for m in held]
    seq += [win._key(vk, extra=USER), win._key(vk, up=True, extra=USER)]
    seq += [win._key(m, up=True, extra=USER) for m in reversed(held)]
    win._send(seq, pause=0)


def click(x: int, y: int) -> None:
    sw, sh = win.user32.GetSystemMetrics(0), win.user32.GetSystemMetrics(1)
    ax, ay = int(x * 65535 / (sw - 1)), int(y * 65535 / (sh - 1))
    seq = []
    for flags in (0x0001 | 0x8000, 0x0002 | 0x8000, 0x0004 | 0x8000):  # move, left down, left up (absolute)
        i = win.INPUT(type=0)
        i.mi = win.MOUSEINPUT(ax, ay, 0, flags, 0, USER)
        seq.append(i)
    win._send(seq, pause=0.02)


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError):
        return ""


def wait_log(log: Path, pattern: str, timeout: float) -> re.Match | None:
    t_end = time.time() + timeout
    while time.time() < t_end:
        m = re.search(pattern, read(log), re.M)
        if m:
            return m
        time.sleep(0.05)
    return None


def main(scenario: str) -> int:
    win.set_dpi_aware()
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob(f"{scenario}_*"):
        old.unlink()
    L, T, R, B = work_area()
    scale = win.user32.GetDpiForSystem() / 96 if hasattr(win.user32, "GetDpiForSystem") else 1.0
    w, h = min(int(980 * scale), R - L - 40), int(230 * scale)
    x = L + (R - L - w) // 2
    y = B - int(56 * scale) - int(28 * scale) - h - int(70 * scale)
    bbox = (x - 12, y - int(45 * scale), x + w + 12, B - 4)
    field = OUT / f"{scenario}_field.txt"
    app_log = OUT / f"{scenario}_app.log"
    saved_clip = win.get_clipboard()
    cursor = wintypes.POINT()
    win.user32.GetCursorPos(ctypes.byref(cursor))

    target = subprocess.Popen([sys.executable, str(ROOT / "tests" / "target_window.py"), str(field), str(x), str(y), str(w), str(h)])
    app = None
    results: dict[str, object] = {"scenario": scenario}
    ok = True
    try:
        hwnd = find_window(TITLE)
        time.sleep(0.6)
        assert focus(hwnd), "could not focus the test window"
        with open(app_log, "w", encoding="utf-8") as logf:
            app = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-m", "livetype", "--source", str(WAV), "--once", "--debug", "--no-tray"],
                cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
            )
        assert wait_log(app_log, r"^READY hotkey_ok=True", 60), "app did not start:\n" + read(app_log)
        time.sleep(1.0)
        results["focus_kept_after_app_start"] = win.foreground() == hwnd
        assert focus(hwnd)

        hotkey()  # start
        t0 = time.time()
        time.sleep(3.0)
        results["fg_is_target_while_overlay_shown"] = win.foreground() == hwnd
        if scenario == "edit":
            time.sleep(8.0 - (time.time() - t0))
            info = json.loads(read(Path(str(field) + ".json")))
            click(info["text_x"] + 14, info["text_y"] + int(16 * scale))
            time.sleep(0.15)
            win.press(win.VK_HOME, extra=USER)
            win.type_text("[правка] ", extra=USER)
            results["field_after_user_edit"] = read(field)
        time.sleep(max(0.0, 7.0 - (time.time() - t0)))
        ImageGrab.grab(bbox=bbox).save(OUT / f"{scenario}_1_listening.png")
        results["field_at_7s"] = read(field)
        time.sleep(max(0.0, 15.0 - (time.time() - t0)))
        results["field_at_15s"] = read(field)
        ImageGrab.grab(bbox=bbox).save(OUT / f"{scenario}_2_listening.png")

        time.sleep(max(0.0, 26.5 - (time.time() - t0)))  # the recording is 25.4 s
        results["field_before_stop"] = read(field)
        hotkey()  # stop
        time.sleep(0.12)
        ImageGrab.grab(bbox=bbox).save(OUT / f"{scenario}_3_processing.png")
        m = wait_log(app_log, r"^EVENT done .*$", 40)
        time.sleep(0.5)
        ImageGrab.grab(bbox=bbox).save(OUT / f"{scenario}_4_done.png")
        assert m, "no EVENT done:\n" + read(app_log)
        app.wait(15)
        time.sleep(0.4)

        log_text = read(app_log)
        live = re.search(r"^LIVE: (.*)$", log_text, re.M).group(1)
        final = re.search(r"^FINAL: (.*)$", log_text, re.M).group(1)
        content = read(field)
        results.update(event=m.group(0), live=live, final=final, field_final=content, clipboard=win.get_clipboard())

        checks = {
            "words typed while speaking (7 s)": len(results["field_at_7s"].split()) >= 4,
            "more words by 15 s": len(results["field_at_15s"].split()) > len(results["field_at_7s"].split()),
            "overlay never stole focus": bool(results["fg_is_target_while_overlay_shown"]),
            "live text close to what was said": wer(REF, live) <= 0.10,
            "final text accurate": wer(REF, final) <= 0.05,
        }
        if scenario == "plain":
            checks["field == final corrected text"] = content == final
            checks["comma fixed in place"] = "посмотрим, насколько" in content
            checks["outcome fixed"] = "outcome=fixed" in m.group(0)
        else:
            checks["user's edit kept at the start"] = content.startswith("[правка] ")
            checks["new words went to the end, in order"] = content == "[правка] " + live
            checks["no in-place fix after user edit"] = "outcome=clipboard_edited" in m.group(0)
            checks["corrected text in clipboard"] = results["clipboard"] == final
        results["checks"] = checks
        ok = all(checks.values())
    finally:
        if app and app.poll() is None:
            app.kill()
        target.kill()
        win.set_clipboard(saved_clip)
        win.user32.SetCursorPos(cursor.x, cursor.y)
        (OUT / f"{scenario}_result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for k, v in results.get("checks", {}).items():
        print(("PASS " if v else "FAIL ") + k)
    print("RESULT:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "plain"))
