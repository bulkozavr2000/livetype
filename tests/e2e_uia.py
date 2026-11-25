"""End-to-end check of "new words go right after our text" in fields with UI Automation.

    python tests/e2e_uia.py <scenario> [target]

scenario:
    middle   dictation starts in the MIDDLE of a document; mid-way the "user" clicks at the very
             beginning and types, and a bit later types more — our words must stay together in
             the middle, the user's cursor must be put back each time, the end untouched
    para     the same in a document of three paragraphs, dictating in the second one (Chromium
             shifts its search results after a paragraph break in ProseMirror/tiptap)
    inside   mid-way the "user" replaces one dictated word by hand — the final fix must keep it
             and still fix the rest
    empty    the field starts empty, like a chat composer; mid-way the "user" clicks into the
             field — the words go on after our text and the final fix is applied in place
    sent     the field starts empty; mid-way the "user" deletes everything (like a sent message)
             — the rest goes into the empty field and is fixed in place (outcome merged)
    cleared  the document has text; mid-way the "user" deletes everything, the text around ours
             too — the rest goes on at the cursor, that part is fixed in place and the whole
             corrected text is in the clipboard (outcome partial)

target:
    wpf          WPF TextBox (default)
    edge         <textarea> in Edge                                   tests/data/textarea.html
    edge-ce      plain contenteditable <div> in Edge                  tests/data/contenteditable.html
    edge-pm      ProseMirror (example setup) in Edge                  tests/data/prosemirror.html
    edge-tt      tiptap in Edge, like the Claude app's composer       tests/data/tiptap.html
    electron-ta / electron-ce / electron-pm / electron-tt
                 the same pages in a bare Electron window (LIVETYPE_ELECTRON=<electron.exe>,
                 default ui/node_modules/electron/dist/electron.exe)
    The ProseMirror and tiptap pages load the editors from esm.sh (pinned versions).

Real app process, real hotkey, typing and overlay; the microphone is replaced by a WAV file.
If the test window stops being the foreground window during a run (you clicked elsewhere), the
engine is killed at once and the run is reported as aborted.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PIL import ImageGrab  # noqa: E402

from e2e_desktop import USER, click, focus, hotkey, read, wait_log  # noqa: E402
from livetype import win  # noqa: E402
from livetype.textops import fold, wer  # noqa: E402

OUT = ROOT / "tests" / "out"
WAV = ROOT / "tests" / "data" / "ru_sample.wav"
REF = (ROOT / "tests" / "data" / "ru_sample.txt").read_text(encoding="utf-8").replace("|", " ").strip()
BEFORE = "НАЧАЛО ДОКУМЕНТА. Здесь уже был текст до диктовки. "
AFTER = " КОНЕЦ ДОКУМЕНТА, этот текст должен остаться в самом конце."
TITLE = "LiveType WPF test"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
ELECTRON = Path(os.environ.get("LIVETYPE_ELECTRON", ROOT / "ui" / "node_modules" / "electron" / "dist" / "electron.exe"))
WEB_TITLE = "LiveType Edge test"  # every test page sets it once its editor is ready
PAGES = {"ta": "textarea", "ce": "contenteditable", "pm": "prosemirror", "tt": "tiptap"}
SCENARIOS = ("middle", "para", "inside", "empty", "sent", "cleared")
# "para": the same as "middle" in a document of three paragraphs, dictating in the second one
PARA_BEFORE = "НАЧАЛО ДОКУМЕНТА.\nВторой абзац, здесь уже был текст до диктовки. "
PARA_AFTER = " КОНЕЦ ВТОРОГО АБЗАЦА.\nПоследний абзац должен остаться в самом конце."


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    win.user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    win.user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def clean(text: str) -> str:
    """Field text as the user sees it: rich editors report U+00A0 for some spaces, U+FFFC for an
    empty block and "\\n" for an empty paragraph."""
    text = text.replace("\r\n", "\n").replace("\xa0", " ").replace(chr(0xFFFC), "")
    return text.rstrip("\n")


def cursor_context(hwnd: int) -> tuple[str, str]:
    """Text right before and after the cursor in the focused field of the test window."""
    from livetype.uia import TextField, Uia

    if win.foreground() != hwnd:  # never read another app's field
        return "", ""
    try:
        f = Uia().text_field()
        sel = f.selection() if f else None
        if not sel:
            return "", ""
        return clean(TextField.text_before(sel, 30)), clean(TextField.text_after(sel, 30))
    except Exception:
        return "", ""


def replace_word_like_user(hwnd: int, old: str, new: str) -> bool:
    """Select a word in the focused field via UIA and retype it with untagged keystrokes."""
    from livetype.uia import Uia

    if win.foreground() != hwnd:
        return False
    u = Uia()
    f = u.text_field()
    r = f.find(old) if f else None
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


def find_visible_window(title: str) -> int:
    """Visible top-level window with this exact title and a real size (Chromium also has hidden ones)."""
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        if win.user32.IsWindowVisible(h) and (win.window_title(h) == title
                                              or win.window_title(h).startswith("LiveType editor failed")):
            left, top, right, bottom = window_rect(h)
            if right - left > 200 and bottom - top > 100:
                found.append(h)
        return True

    win.user32.EnumWindows(proc(cb), 0)
    return found[0] if found else 0


def kill_test_browsers() -> None:
    """Close only the Edge/Electron processes that use the throwaway test profiles."""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='electron.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*edge-test-profile*' -or $_.CommandLine -like '*electron-test-profile*' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                   capture_output=True)


class WebField:
    """Reads the focused editor of the test page through UI Automation (no file to save to)."""

    def __init__(self, hwnd: int):
        from livetype.uia import Uia

        self.u = Uia()
        self.field = None
        for i in range(60):  # Chromium builds its accessibility tree on the first request
            if win.foreground() != hwnd and i % 10 == 9:
                focus(hwnd)  # another window came to the front meanwhile
            # the focused field of the test window only (another app's composer can be focused too)
            f = self.u.text_field() if win.foreground() == hwnd else None
            if f is not None and self._inside(f.el, hwnd):
                self.field = f
                break
            time.sleep(0.2)
        assert self.field, "the editor was not found via UI Automation"
        r = self.field.el.CurrentBoundingRectangle
        self.rect = (r.left, r.top, r.right, r.bottom)

    @staticmethod
    def _inside(el, hwnd: int) -> bool:
        try:
            r = el.CurrentBoundingRectangle
            left, top, right, bottom = window_rect(hwnd)
            return left <= r.left < right and top <= r.top < bottom and r.right - r.left > 100
        except Exception:
            return False

    def read(self) -> str:
        try:
            return clean(self.field.document().GetText(-1))
        except Exception:
            return ""


class FocusGuard(threading.Thread):
    """Kills the engine the moment the test window stops being the foreground window."""

    def __init__(self, hwnd: int):
        super().__init__(name="focus-guard", daemon=True)
        self.hwnd, self.app = hwnd, None
        self.lost = ""
        self.children: list[str] = []  # moments a child window of the test window was in front
        self.t0 = time.time()
        self.stopped = threading.Event()

    def run(self) -> None:
        win.user32.GetAncestor.restype = wintypes.HWND
        while not self.stopped.is_set():
            fg = win.foreground()
            if fg != self.hwnd:
                if fg and win.user32.GetAncestor(fg, 2) == self.hwnd:  # GA_ROOT: still the test window
                    note = f"{time.time() - self.t0:.2f}s {win.window_class(fg)}"
                    if not self.children or not self.children[-1].endswith(win.window_class(fg)):
                        self.children.append(note)
                    time.sleep(0.03)
                    continue
                self.lost = f"{win.window_process(fg)} «{win.window_title(fg)[:60]}»"
                if self.app is not None and self.app.poll() is None:
                    self.app.kill()
                return
            if self.children and not self.children[-1].endswith("back"):
                self.children.append(f"{time.time() - self.t0:.2f}s back")
            time.sleep(0.03)

    def check(self) -> None:
        if self.lost:
            raise RuntimeError(f"ABORTED: focus moved to {self.lost}; the engine was stopped")


def main(scenario: str, target_kind: str = "wpf") -> int:
    win.set_dpi_aware()
    if scenario not in SCENARIOS:
        print(f"unknown scenario {scenario!r}; one of {', '.join(SCENARIOS)}")
        return 2
    host, _, editor = target_kind.partition("-")
    web = host in ("edge", "electron")
    if web:
        editor = editor or "ta"
        if editor not in PAGES:
            print(f"unknown target {target_kind!r}")
            return 2
        if host == "electron" and not ELECTRON.is_file():
            print(f"SKIPPED: no Electron at {ELECTRON} (set LIVETYPE_ELECTRON)")
            return 3
    elif host != "wpf":
        print(f"unknown target {target_kind!r}")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"uia_{scenario}" + {"wpf": "", "edge": "_edge"}.get(target_kind, f"_{target_kind}")
    for old in OUT.glob(f"{tag}_*"):
        old.unlink()
    before, after = (PARA_BEFORE, PARA_AFTER) if scenario == "para" else (BEFORE, AFTER)
    init_text, caret = ("", -1) if scenario in ("empty", "sent") else (before + after, len(before))
    field = OUT / f"{tag}_field.txt"
    init = OUT / f"{tag}_init.txt"
    # a WPF TextBox counts its own "\r\n" line breaks: give it those (the pages take "\n")
    init.write_text(init_text if web else init_text.replace("\n", "\r\n"), encoding="utf-8", newline="")
    wpf_caret = len(before.replace("\n", "\r\n")) if caret >= 0 else -1
    app_log = OUT / f"{tag}_app.log"
    saved_clip = win.get_clipboard()
    cursor = wintypes.POINT()
    win.user32.GetCursorPos(ctypes.byref(cursor))
    if web:
        from urllib.parse import quote

        kill_test_browsers()
        page = (ROOT / "tests" / "data" / f"{PAGES[editor]}.html").as_uri()
        url = f"{page}?caret={caret}&text={quote(init_text)}"
        if host == "edge":
            target = subprocess.Popen([EDGE, f"--user-data-dir={OUT / 'edge-test-profile'}", "--no-first-run",
                                       "--no-default-browser-check", "--disable-sync", "--hide-crash-restore-bubble",
                                       "--window-size=1000,320",
                                       f"--app={url}"])
        else:
            target = subprocess.Popen([str(ELECTRON), str(ROOT / "tests" / "electron_shell" / "main.js"),
                                       f"--url={url}", f"--profile={OUT / 'electron-test-profile'}"])
        title = WEB_TITLE
    else:
        target = subprocess.Popen(["powershell", "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                   str(ROOT / "tests" / "target_wpf.ps1"), "-Out", str(field), "-InitFile",
                                   str(init), "-Caret", str(wpf_caret)])
        title = TITLE
    app = None
    edge: WebField | None = None
    guard: FocusGuard | None = None
    results: dict[str, object] = {"scenario": scenario, "target": target_kind}
    ok = False
    t0 = time.time()

    def read_field() -> str:
        return edge.read() if edge else clean(read(field))

    def until(t: float) -> None:  # sleep until t seconds after the start, unless the focus is lost
        while time.time() - t0 < t:
            guard.check()
            time.sleep(0.03)
        guard.check()

    try:
        t_end = time.time() + 40
        hwnd = 0
        while not hwnd and time.time() < t_end:
            hwnd = find_visible_window(title)
            time.sleep(0.2)
        assert hwnd, "target window did not appear"
        assert not win.window_title(hwnd).startswith("LiveType editor failed"), \
            win.window_title(hwnd) + " (no internet for esm.sh?)"
        time.sleep(1.0)
        assert focus(hwnd), "could not focus the target window"
        if web:
            edge = WebField(hwnd)
            assert focus(hwnd)
        L, T, R, B = window_rect(hwnd)
        sw, sh = win.user32.GetSystemMetrics(0), win.user32.GetSystemMetrics(1)
        bbox = (max(0, L - 10), max(0, T - 10), min(sw, R + 10), min(sh, max(B + 10, sh - 40)))
        results["field_at_start"] = read_field()
        with open(app_log, "w", encoding="utf-8") as logf:
            app = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "livetype", "--source", str(WAV),
                                    "--once", "--debug", "--no-tray"], cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT)
        assert wait_log(app_log, r"^READY hotkey_ok=True", 60), "app did not start:\n" + read(app_log)
        time.sleep(0.8)
        assert focus(hwnd)
        guard = FocusGuard(hwnd)
        guard.app = app
        t0 = guard.t0 = time.time()
        guard.start()
        hotkey()
        m = wait_log(app_log, r"^EVENT start .*$", 10)
        results["start_event"] = m.group(0) if m else None
        if edge:
            fx, fy = edge.rect[0], edge.rect[1]
            bottom = (edge.rect[0] + 24, min(edge.rect[3], B) - 18)
        else:
            info = json.loads(read(Path(str(field) + ".json")))
            fx, fy = info["text_x"], info["text_y"]
            bottom = (L + 40, B - 30)
        if scenario in ("middle", "para"):
            until(8.0)
            click(fx + 14, fy + 16)
            time.sleep(0.15)
            guard.check()
            win.press(win.VK_HOME, ctrl=True, extra=USER)
            win.type_text("[правка] ", extra=USER)
            results["after_user_edit"] = read_field()
            until(12.0)  # our words went on in the middle meanwhile: the cursor must be back here
            win.type_text("ещё ", extra=USER)
        elif scenario == "inside":
            until(9.0)
            results["replaced"] = replace_word_like_user(hwnd, "приложение", "программу")
            results["after_user_edit"] = read_field()
        elif scenario == "empty":
            until(8.0)
            click(*bottom)  # below the text: the cursor lands at its end, as after a stray click
            results["after_user_edit"] = read_field()
        else:  # cleared / sent: select everything and delete it
            until(9.0)
            results["before_clear"] = read_field()
            click(*bottom)
            time.sleep(0.15)
            guard.check()
            win.press(ord("A"), ctrl=True, extra=USER)
            win.press(0x2E, extra=USER)  # Delete
            time.sleep(0.2)
            results["after_user_edit"] = read_field()
        until(15.0)
        results["field_at_15s"] = read_field()
        ImageGrab.grab(bbox=bbox).save(OUT / f"{tag}_1_listening.png")
        until(26.5)
        results["field_before_stop"] = read_field()
        hotkey()
        done = None
        t_stop = time.time()
        while done is None and time.time() - t_stop < 60:
            guard.check()
            done = wait_log(app_log, r"^EVENT done .*$", 0.2)
        time.sleep(0.6)
        guard.check()
        guard.stopped.set()
        ImageGrab.grab(bbox=bbox).save(OUT / f"{tag}_2_done.png")
        assert done, "no EVENT done:\n" + read(app_log)
        app.wait(15)
        time.sleep(0.4)
        log_text = read(app_log)
        live = re.search(r"^LIVE: (.*)$", log_text, re.M).group(1)
        final = re.search(r"^FINAL: (.*)$", log_text, re.M).group(1)
        content = read_field()
        clip = win.get_clipboard()
        results.update(event=done.group(0), live=live, final=final, field_final=content, clipboard=clip)
        results["cursor_after_final"] = cursor_context(hwnd)
        checks = {
            "UI Automation mode was used": "mode=uia" in (results["start_event"] or ""),
            "words typed while speaking": len(results.get("before_clear", results["field_at_15s"])) > len(init_text) + 40,
            "final text accurate": wer(REF, final) <= 0.05,
        }
        if scenario in ("middle", "para"):
            middle = content.removeprefix("[правка] ещё " + before).removesuffix(after)
            checks.update({
                "user's edits kept at the start (the cursor was put back)": content.startswith("[правка] ещё " + before),
                "end of the document untouched": content.endswith(after),
                "our words stayed together in the middle, corrected": middle == final,
                "the user's cursor is where they left it": results["cursor_after_final"][0].endswith("[правка] ещё "),
                "outcome done or fixed": re.search(r"outcome=(done|fixed)\b", done.group(0)) is not None,
            })
        elif scenario == "inside":
            middle = content.removeprefix(BEFORE).removesuffix(AFTER)
            checks.update({
                "the word was replaced by the 'user'": bool(results["replaced"]),
                "user's word kept after the final fix": "программу" in middle and "приложение" not in middle,
                "other corrections still applied": "посмотрим, насколько" in middle,
                "document start and end untouched": content.startswith(BEFORE) and content.endswith(AFTER),
                "outcome done or merged": re.search(r"outcome=(done|merged)\b", done.group(0)) is not None,
            })
        elif scenario == "empty":
            checks.update({
                "field == final corrected text": content == final,
                "outcome done or fixed": re.search(r"outcome=(done|fixed)\b", done.group(0)) is not None,
            })
        else:
            tail = fold(content).s.strip()
            checks.update({
                "the field was cleared mid-way": results["after_user_edit"] == "",
                "the rest went on at the cursor": len(results["field_at_15s"]) > 20,
                "that part is the corrected end of the text": bool(tail) and fold(final).s.endswith(tail),
            })
            if scenario == "cleared":  # the text around ours went too: a new piece at the cursor
                checks["clipboard not overwritten with the text"] = clip != final  # 0.3: partial keeps the clipboard
                checks["outcome partial or done"] = re.search(r"outcome=(partial|done)\b", done.group(0)) is not None
            else:  # sent: the field was empty before, so our place is still its start
                checks["outcome done or merged"] = re.search(r"outcome=(done|merged)\b", done.group(0)) is not None
        results["checks"] = checks
        ok = all(checks.values())
    except (AssertionError, RuntimeError) as e:
        results["error"] = str(e)
        print(str(e).splitlines()[0] if str(e) else repr(e))
    finally:
        if guard is not None:
            guard.stopped.set()
            if guard.children:
                results["child_window_in_front"] = guard.children
        if app and app.poll() is None:
            app.kill()
        target.kill()
        if web:
            kill_test_browsers()
        win.set_clipboard(saved_clip)
        win.user32.SetCursorPos(cursor.x, cursor.y)
        (OUT / f"{tag}_result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for k, v in results.get("checks", {}).items():
        print(("PASS " if v else "FAIL ") + k)
    print(f"[{scenario} {target_kind}]", results.get("event", ""))
    print("RESULT:", "OK" if ok else ("ABORTED" if str(results.get("error", "")).startswith("ABORTED") else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "middle", sys.argv[2] if len(sys.argv) > 2 else "wpf"))
