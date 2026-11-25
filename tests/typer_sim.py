"""A simulated desktop for typer tests (no windows, no real keystrokes).

Field      an editor's text field: text + selection; typing replaces the selection, Backspace
           deletes one grapheme cluster (or the selection), an empty rich composer reads as "\\n".
FakeWin    replaces livetype.win's input and window functions; keystrokes are built by the real
           event builders (win._text_events / win.rewrite_events) and replayed into the Field,
           so a VK_RETURN would show up as "\\n" and every batch is recorded.
FakeUia    a UI Automation client over the Field (the TextPattern fake of the rich-editor tests).
FakeWatcher / FakeUi / stub engines for App._session.
FakeLlama  a local HTTP stand-in for llama-server (/health, /completion) for the real TextProcessor.
"""

from __future__ import annotations

import http.server
import json
import queue
import threading
import time
from collections.abc import Callable

import numpy as np

from livetype import win
from livetype.textops import grapheme_starts
from livetype.uia import TextField


class Field:
    def __init__(self, text: str = "", caret: int | None = None, shifted_search: bool = False):
        self.text = text
        c = len(text) if caret is None else caret
        self.sel = (c, c)
        self.shifted_search = shifted_search  # Chromium in <p>-based editors (ProseMirror, tiptap)
        self.lock = threading.RLock()

    def type(self, s: str) -> None:
        with self.lock:
            i, j = self.sel
            if self.text == "\n":  # tiptap/ProseMirror: the empty paragraph's line break goes away
                i, j = 0, 1
            self.text = self.text[:i] + s + self.text[j:]
            self.sel = (i + len(s),) * 2

    def backspace(self) -> None:
        with self.lock:
            i, j = self.sel
            if i != j:
                self.text = self.text[:i] + self.text[j:]
                self.sel = (i, i)
            elif i > 0:
                k = grapheme_starts(self.text[:i])[-1]  # one user-perceived character
                self.text = self.text[:k] + self.text[i:]
                self.sel = (k, k)

    def user_edit(self, at: int, insert: str = "", delete: int = 0, caret: int | None = None) -> None:
        """The user changes the text by hand (the caret ends after the edit unless given)."""
        with self.lock:
            self.text = self.text[:at] + insert + self.text[at + delete:]
            c = at + len(insert) if caret is None else caret
            self.sel = (c, c)


class FakeWin:
    def __init__(self, monkeypatch, field: Field, target: int = 1):
        self.field = field
        self.target = target
        self.fg = target
        self.focus = 7  # focused control of the target window
        self.mods = False
        self.clipboard = "the user's own clipboard"
        self.sent: list[tuple] = []  # every batch: ("text", s) | ("vk", vk, n, ctrl) | ("rewrite", n, s)
        self.returns = 0  # VK_RETURN key downs ever sent
        mp = monkeypatch
        mp.setattr(win, "foreground", lambda: self.fg)
        mp.setattr(win, "focused_control", lambda hwnd: self.focus)
        mp.setattr(win, "modifiers_down", lambda: self.mods)
        mp.setattr(win, "wait_modifiers_released", lambda timeout=3.0: not self.mods)
        mp.setattr(win, "type_text", self.type_text)
        mp.setattr(win, "press", self.press)
        mp.setattr(win, "rewrite", self.rewrite)
        mp.setattr(win, "set_clipboard", self.set_clipboard)
        mp.setattr(win, "get_clipboard", lambda: self.clipboard)
        mp.setattr(win, "window_process", lambda h: "notepad.exe")
        mp.setattr(win, "window_class", lambda h: "Notepad")
        mp.setattr(win, "is_own_window", lambda h: False)

    def _replay(self, events) -> None:
        high = None
        for e in events:
            ki = e.ki
            if ki.dwFlags & win.KEYEVENTF_KEYUP:
                continue
            if ki.dwFlags & win.KEYEVENTF_UNICODE:
                cu = ki.wScan
                if 0xD800 <= cu < 0xDC00:
                    high = cu
                    continue
                ch = chr(0x10000 + ((high - 0xD800) << 10) + (cu - 0xDC00)) if high is not None else chr(cu)
                high = None
                self.field.type(ch)
            elif ki.wVk == win.VK_BACK:
                self.field.backspace()
            elif ki.wVk == win.VK_RETURN:
                self.returns += 1
                self.field.type("\n")

    def type_text(self, text: str, extra: int = win.MAGIC) -> None:
        self.sent.append(("text", text))
        with self.field.lock:
            self._replay(win._text_events(text, extra))

    def rewrite(self, backspaces: int, text: str, extra: int = win.MAGIC) -> None:
        self.sent.append(("rewrite", backspaces, text))
        with self.field.lock:  # one batch: nothing gets in between
            self._replay(win.rewrite_events(backspaces, text, extra))

    def press(self, vk: int, times: int = 1, *, shift: bool = False, ctrl: bool = False, extra: int = win.MAGIC) -> None:
        self.sent.append(("vk", vk, times, ctrl))
        with self.field.lock:
            for _ in range(times):
                if vk == win.VK_BACK:
                    self.field.backspace()
                elif vk == win.VK_END:
                    n = len(self.field.text)
                    self.field.sel = (n, n)
                elif vk == win.VK_RETURN:
                    self.returns += 1
                    self.field.type("\n")

    def set_clipboard(self, text: str) -> bool:
        self.clipboard = text
        return True

    def rewrites(self) -> list[tuple]:
        return [s for s in self.sent if s[0] == "rewrite"]


class FakeWatcher:
    """win.InputWatcher stand-in: the test calls user() for each key press or click."""

    def __init__(self, ignore_vks=None, target: int = 0, hotkey: str | None = None, held=None):
        self.target, self.hotkey = target, hotkey
        self.count = 0
        self.last_time = 0.0
        self.ready = threading.Event()
        self.ready.set()
        self._alive = False

    def start(self) -> None:
        self._alive = True

    def stop(self) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def user(self, settled: bool = True) -> None:
        self.count += 1
        self.last_time = time.monotonic() - (60.0 if settled else 0.0)


class FakeUi:
    wants_unit_level = True

    def __init__(self):
        self.events: list[tuple] = []
        self.level = 0.0
        self.done_at: float | None = None

    def notice(self, title, subtitle="", **kw):
        self.events.append(("notice", title, subtitle, kw.get("kind")))

    def done(self, title="Готово", subtitle="", ttl=1.6):
        self.done_at = time.monotonic()
        self.events.append(("done", title, subtitle))

    def listening(self):
        self.events.append(("listening",))

    def processing(self):
        self.events.append(("processing",))

    def hide(self):
        self.events.append(("hide",))

    def finish(self):
        self.events.append(("finish",))

    def quit(self):
        pass

    def titles(self, kind: str) -> list[str]:
        return [e[1] for e in self.events if e[0] == kind]


# --------------------------------------------------------------------------- UI Automation fakes
class _Range:
    """ITextRangeProvider over a Python string, character unit = one character, or one whole
    emoji sequence when the text has them (like Chromium's grapheme-based moves)."""

    def __init__(self, t, s: int, e: int):
        self.t, self.s, self.e = t, s, e

    def _step(self, pos: int, d: int) -> int:
        family = "\U0001F468" + chr(0x200D) + "\U0001F469" + chr(0x200D) + "\U0001F467"  # one grapheme, five code points
        k = self.t.text.find(family)
        if k >= 0:
            if d > 0 and pos == k:
                return k + len(family)
            if d < 0 and pos == k + len(family):
                return k
        return pos + d

    def Clone(self):  # noqa: N802 (UIA method names)
        return _Range(self.t, self.s, self.e)

    def GetText(self, n):  # noqa: N802
        return self.t.text[self.s:self.e]

    def MoveEndpointByRange(self, ep, other, other_ep):  # noqa: N802
        v = other.s if other_ep == 0 else other.e
        if ep == 0:
            self.s, self.e = v, max(self.e, v)
        else:
            self.e, self.s = v, min(self.s, v)

    def MoveEndpointByUnit(self, ep, unit, n):  # noqa: N802
        new, moved, d = (self.s if ep == 0 else self.e), 0, (1 if n > 0 else -1)
        while moved < abs(n) and 0 <= self._step(new, d) <= len(self.t.text):
            new, moved = self._step(new, d), moved + 1
        if ep == 0:
            self.s, self.e = new, max(self.e, new)
        else:
            self.e, self.s = new, min(self.s, new)
        return moved * d

    def CompareEndpoints(self, ep, other, other_ep):  # noqa: N802
        a = self.s if ep == 0 else self.e
        b = other.s if other_ep == 0 else other.e
        return (a > b) - (a < b)

    def FindText(self, s, backward, ignore_case):  # noqa: N802
        hay = self.t.text[self.s:self.e]
        k = hay.rfind(s) if backward else hay.find(s)
        if k < 0:
            return None
        k += self.s
        if self.t.shifted_search:  # one character further per paragraph break before the match
            k += self.t.text[:k].count("\n")
        return _Range(self.t, k, min(len(self.t.text), k + len(s)))

    def Move(self, unit, n):  # noqa: N802
        self.s = max(0, min(len(self.t.text), self.s + n))
        self.e = max(self.s, min(len(self.t.text), self.e + n))

    def Select(self):  # noqa: N802
        self.t.sel = (self.s, self.e)


class _Selection:
    def __init__(self, r):
        self.r, self.Length = r, 1

    def GetElement(self, i):  # noqa: N802
        return self.r


class _Pattern:
    def __init__(self, t):
        self.t = t

    @property
    def DocumentRange(self):  # noqa: N802
        return _Range(self.t, 0, len(self.t.text))

    def GetSelection(self):  # noqa: N802
        return _Selection(_Range(self.t, *self.t.sel))


class _Element:
    def SetFocus(self):  # noqa: N802
        pass


class _Client:
    def __init__(self, uia):
        self.uia = uia

    def CompareElements(self, a, b):  # noqa: N802
        return a is b

    def GetFocusedElement(self):  # noqa: N802
        return self.uia.focused


class FakeUia:
    """Uia over one fake field. `focused` = the focused element (the field's, another one, or None)."""

    def __init__(self, t):
        self.el = _Element()
        self.focused = self.el
        self.client = _Client(self)
        self.field = TextField(self, self.el, _Pattern(t))

    def focused_element(self):
        return self.focused

    def text_field(self, element=None):
        return self.field if (element or self.focused) is self.el else None


def text_field(text: str, caret: int = 0, shifted: bool = False) -> tuple[Field, TextField]:
    t = Field(text, caret, shifted)
    return t, TextField(None, None, _Pattern(t))


# --------------------------------------------------------------------------- engines and audio
class FakeSource:
    """Puts silent 32 ms blocks in the queue over time, like the microphone."""

    def __init__(self, blocks: int = 40, every: float = 0.01):
        self.q: queue.Queue = queue.Queue()
        self.finished = threading.Event()
        self.blocks, self.every = blocks, every
        self._stop = threading.Event()

    def start(self) -> None:
        def run():
            for _ in range(self.blocks):
                if self._stop.is_set():
                    break
                self.q.put(np.zeros(512, dtype=np.float32))
                time.sleep(self.every)
            self.finished.set()

        threading.Thread(target=run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


class ScriptedEngine:
    """Common part of the stub engines: one scripted event per poll() (None = nothing this time;
    a callable runs as is, e.g. a user edit), the rest at finish()."""

    script: list = []
    final = ""
    settle: Callable | None = None  # optional: called after each event (e.g. wait for the typer)

    def __init__(self, cfg, live, vad, final=None, on_commit=None, on_level=None):
        self.cfg = cfg
        self.on_commit = on_commit
        self.live_text = ""
        self._text = ""
        self.decode_times = [0.05]
        self.commit_log: list = []
        self.audio_time = 0.0
        self.closed = False
        self.finish_args: tuple = ()
        self._i = 0

    def feed(self, chunk) -> None:
        self.audio_time += len(chunk) / 16000

    def _event(self, ev) -> None:
        if ev is None:
            return
        if callable(ev):
            ev()
            return
        kind, arg = ev
        if kind == "commit":
            self.live_text += arg
            self._text += arg
            self.commit_log.append((self.audio_time, arg))
            self.on_commit(arg)
        else:
            self._revise(arg)
        if type(self).settle is not None:
            type(self).settle(self)

    def _revise(self, target: str) -> None:
        raise NotImplementedError

    def poll(self) -> None:
        if self._i < len(self.script):
            self._i += 1
            self._event(self.script[self._i - 1])

    def _rest(self) -> None:
        while self._i < len(self.script):
            self._i += 1
            self._event(self.script[self._i - 1])

    def close(self) -> None:
        self.closed = True


class OldEngine(ScriptedEngine):
    """The 0.2 contract: on_commit only; finish() returns the whole final text."""

    def _revise(self, target: str) -> None:
        pass  # this engine cannot tell anyone

    def finish(self) -> str:
        self._rest()
        return self.final


class NewEngine(ScriptedEngine):
    """The 0.3 contract: on_revise(target) in order with on_commit; finish(budget_s)."""

    def __init__(self, cfg, live, vad, final=None, on_commit=None, on_level=None, on_revise=None, warm_up=True):
        super().__init__(cfg, live, vad, final, on_commit, on_level)
        self.on_revise = on_revise

    @property
    def text(self) -> str:
        return self._text

    def _revise(self, target: str) -> None:
        self._text = target
        if self.on_revise:
            self.on_revise(target)

    def finish(self, budget_s: float | None = None) -> str:
        self.finish_args = (budget_s,)
        self._rest()
        if self.final != self._text:
            self._revise(self.final)
        return self._text



# --------------------------------------------------------------------------- language model server
class _FakeProc:
    def __init__(self):
        self.killed = False

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True


class FakeLlama:
    """llama-server stand-in on 127.0.0.1: /health is ok, /completion answers `answer` after
    `delay` seconds. processor() gives a real llm.TextProcessor wired to it (nothing is launched)."""

    def __init__(self, answer: str = "", delay: float = 0.0):
        self.answer, self.delay = answer, delay
        self.requests: list[dict] = []
        self.launched: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self._json({"status": "ok"} if self.path == "/health" else {})

            def do_POST(self):  # noqa: N802
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                time.sleep(outer.delay)
                try:
                    self._json({"content": outer.answer})
                except OSError:
                    pass  # the client went away (deadline or cancel)

            def _json(self, data):
                raw = json.dumps(data, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def processor(self):
        from livetype import llm

        tp = llm.TextProcessor()
        tp.ready = lambda model: True
        tp._free_port = lambda: self.srv.server_port
        tp._launch = lambda model, port: self.launched.append(model) or _FakeProc()
        return tp

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
