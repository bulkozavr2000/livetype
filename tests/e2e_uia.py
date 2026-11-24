"""End-to-end check of "new words go right after our text" in a field with UI Automation.

    python tests/e2e_uia.py middle   # dictation starts in the MIDDLE of a document; mid-way the
                                     # "user" clicks at the very beginning and types — our words must
                                     # stay together in the middle, the end of the document untouched
    python tests/e2e_uia.py inside   # mid-way the "user" replaces one dictated word by hand — the
                                     # final fix must keep it and still fix the rest

Real app process, real hotkey, typing and overlay; the microphone is replaced by a WAV file.
"""

from __future__ import annotations

import ctypes
import json
import re
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PIL import ImageGrab  # noqa: E402

from e2e_desktop import USER, click, focus, hotkey, read, wait_log  # noqa: E402
from livetype import win  # noqa: E402
from livetype.textops import wer  # noqa: E402

OUT = ROOT / "tests" / "out"
WAV = ROOT / "tests" / "data" / "ru_sample.wav"
REF = (ROOT / "tests" / "data" / "ru_sample.txt").read_text(encoding="utf-8").replace("|", " ").strip()
BEFORE = "НАЧАЛО ДОКУМЕНТА. Здесь уже был текст до диктовки. "
AFTER = " КОНЕЦ ДОКУМЕНТА, этот текст должен остаться в самом конце."
TITLE = "LiveType WPF test"


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    win.user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    win.user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def replace_word_like_user(old: str, new: str) -> bool:
    """Select a word in the focused field via UIA and retype it with untagged keystrokes."""
    from livetype.uia import Uia

    u = Uia()
    f = u.text_field()
    r = f.find_last(old) if f else None
    if r is None:
        return False
    # a real user selects with the mouse or keyboard, and LiveType sees that input and pauses;
    # a selection made through UI Automation produces no input at all, so signal it first
    win.press(0xE8, extra=USER)  # unassigned key: no effect in the app
    time.sleep(0.3)
    r.Select()
    time.sleep(0.1)
    win.type_text(new, extra=USER)
    return True


EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EDGE_TITLE = "LiveType Edge test"


def find_visible_window(title: str) -> int:
    """Visible top-level window with this exact title and a real size (Chromium also has hidden ones)."""
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if win.user32.IsWindowVisible(h) and win.window_title(h) == title:
            left, top, right, bottom = window_rect(h)
            if right - left > 200 and bottom - top > 100:
                found.append(h)
        return True

    win.user32.EnumWindows(proc(cb), 0)
    return found[0] if found else 0


def kill_test_edge() -> None:
    """Close only the Edge processes that use the throwaway test profile (never the user's Edge)."""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*edge-test-profile*' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                   capture_output=True)


class EdgeField:
    """Reads the textarea of the Edge test window through UI Automation (no file to save to)."""

    def __init__(self, hwnd: int):
        from livetype.uia import Uia

        self.u = Uia()
        root = self.u.client.ElementFromHandle(hwnd)
        cond = self.u.client.CreatePropertyCondition(30003, 50004)  # ControlType == Edit
        self.el = None
        for _ in range(50):  # the biggest Edit is the textarea (a hidden address field also exists)
            found = root.FindAll(4, cond)  # TreeScope_Descendants
            best, area = None, 0
            for i in range(found.Length if found else 0):
                e = found.GetElement(i)
                r = e.CurrentBoundingRectangle
                a = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                if a > area:
                    best, area = e, a
            if best is not None and area > 10000:
                self.el = best
                break
            time.sleep(0.2)
        assert self.el, "textarea not found via UI Automation"
        self.field = self.u.text_field(self.el)
        r = self.el.CurrentBoundingRectangle
        self.text_x, self.text_y = r.left, r.top

    def read(self) -> str:
        try:
            return self.field.document().GetText(-1).replace("\r\n", "\n")
        except Exception:
            return ""


def main(scenario: str, target_kind: str = "wpf") -> int:
    win.set_dpi_aware()
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"uia_{scenario}" + ("_edge" if target_kind == "edge" else "")
    for old in OUT.glob(f"{tag}_*"):
        old.unlink()
    field = OUT / f"{tag}_field.txt"
    init = OUT / f"{tag}_init.txt"
    init.write_text(BEFORE + AFTER, encoding="utf-8")
    app_log = OUT / f"{tag}_app.log"
    saved_clip = win.get_clipboard()
    cursor = wintypes.POINT()
    win.user32.GetCursorPos(ctypes.byref(cursor))
    if target_kind == "edge":
        from urllib.parse import quote

        kill_test_edge()
        page = (ROOT / "tests" / "data" / "textarea.html").as_uri()
        url = f"{page}?caret={len(BEFORE)}&text={quote(BEFORE + AFTER)}"
        target = subprocess.Popen([EDGE, f"--user-data-dir={Path(tempfile.gettempdir()) / 'livetype-edge-test-profile'}",
                                   "--no-first-run", "--no-default-browser-check", "--disable-sync",
                                   "--window-size=1000,320", f"--app={url}"])
        title = EDGE_TITLE
    else:
        target = subprocess.Popen(["powershell", "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                   str(ROOT / "tests" / "target_wpf.ps1"), "-Out", str(field), "-InitFile",
                                   str(init), "-Caret", str(len(BEFORE))])
        title = TITLE
    app = None
    edge: EdgeField | None = None
    results: dict[str, object] = {"scenario": scenario, "target": target_kind}
    ok = False

    def read_field() -> str:
        return edge.read() if edge else read(field)

    try:
        t_end = time.time() + 25
        hwnd = 0
        while not hwnd and time.time() < t_end:
            hwnd = find_visible_window(title)
            time.sleep(0.2)
        assert hwnd, "target window did not appear"
        time.sleep(1.0)
        assert focus(hwnd), "could not focus the target window"
        if target_kind == "edge":
            edge = EdgeField(hwnd)
            assert focus(hwnd)
        L, T, R, B = window_rect(hwnd)
        sw, sh = win.user32.GetSystemMetrics(0), win.user32.GetSystemMetrics(1)
        bbox = (max(0, L - 10), max(0, T - 10), min(sw, R + 10), min(sh, max(B + 10, sh - 40)))
        with open(app_log, "w", encoding="utf-8") as logf:
            app = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "livetype", "--source", str(WAV),
                                    "--once", "--debug", "--no-tray"], cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT)
        assert wait_log(app_log, r"^READY hotkey_ok=True", 60), "app did not start:\n" + read(app_log)
        time.sleep(0.8)
        assert focus(hwnd)
        hotkey()
        t0 = time.time()
        m = wait_log(app_log, r"^EVENT start .*$", 10)
        results["start_event"] = m.group(0) if m else None
        if scenario == "middle":
            time.sleep(max(0.0, 8.0 - (time.time() - t0)))
            info = ({"text_x": edge.text_x, "text_y": edge.text_y} if edge
                    else json.loads(read(Path(str(field) + ".json"))))
            click(info["text_x"] + 14, info["text_y"] + 16)
            time.sleep(0.15)
            win.press(win.VK_HOME, ctrl=True, extra=USER)
            win.type_text("[правка] ", extra=USER)
            results["after_user_edit"] = read_field()
        else:
            time.sleep(max(0.0, 9.0 - (time.time() - t0)))
            results["replaced"] = replace_word_like_user("приложение", "программу")
            results["after_user_edit"] = read_field()
        time.sleep(max(0.0, 15.0 - (time.time() - t0)))
        results["field_at_15s"] = read_field()
        ImageGrab.grab(bbox=bbox).save(OUT / f"{tag}_1_listening.png")
        time.sleep(max(0.0, 26.5 - (time.time() - t0)))
        results["field_before_stop"] = read_field()
        hotkey()
        done = wait_log(app_log, r"^EVENT done .*$", 60)
        time.sleep(0.6)
        ImageGrab.grab(bbox=bbox).save(OUT / f"{tag}_2_done.png")
        assert done, "no EVENT done:\n" + read(app_log)
        app.wait(15)
        time.sleep(0.4)
        log_text = read(app_log)
        live = re.search(r"^LIVE: (.*)$", log_text, re.M).group(1)
        final = re.search(r"^FINAL: (.*)$", log_text, re.M).group(1)
        content = read_field()
        results.update(event=done.group(0), live=live, final=final, field_final=content)
        checks = {
            "UI Automation mode was used": "mode=uia" in (results["start_event"] or ""),
            "words typed while speaking": len(results["field_at_15s"]) > len(BEFORE + AFTER) + 40,
            "final text accurate": wer(REF, final) <= 0.05,
        }
        if scenario == "middle":
            middle = content.removeprefix("[правка] " + BEFORE).removesuffix(AFTER)
            checks.update({
                "user's edit kept at the start": content.startswith("[правка] " + BEFORE),
                "end of the document untouched": content.endswith(AFTER),
                "our words stayed together in the middle, corrected": middle == final,
                "outcome fixed": "outcome=fixed" in done.group(0),
            })
        else:
            middle = content.removeprefix(BEFORE).removesuffix(AFTER)
            checks.update({
                "the word was replaced by the 'user'": bool(results["replaced"]),
                "user's word kept after the final fix": "программу" in middle and "приложение" not in middle,
                "other corrections still applied": "посмотрим, насколько" in middle,
                "document start and end untouched": content.startswith(BEFORE) and content.endswith(AFTER),
                "outcome merged": "outcome=merged" in done.group(0),
            })
        results["checks"] = checks
        ok = all(checks.values())
    finally:
        if app and app.poll() is None:
            app.kill()
        target.kill()
        if target_kind == "edge":
            kill_test_edge()
        win.set_clipboard(saved_clip)
        win.user32.SetCursorPos(cursor.x, cursor.y)
        (OUT / f"{tag}_result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for k, v in results.get("checks", {}).items():
        print(("PASS " if v else "FAIL ") + k)
    print("RESULT:", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "middle", sys.argv[2] if len(sys.argv) > 2 else "wpf"))
