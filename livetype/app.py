"""Controller: hotkey -> listen -> type words live -> correct them in place -> done
(-> optional language-model pass in the background)."""

from __future__ import annotations

import difflib
import inspect
import json
import logging
import math
import os
import queue
import struct
import threading
import time
import uuid
import wave
import winsound
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from livetype import win
from livetype.asr import Recognizer, StreamingVad
from livetype.audio import Source
from livetype.config import DATA_DIR, HISTORY_PATH, SAMPLE_RATE, Config
from livetype.segment_engine import SegmentEngine as DictationEngine  # tests replace this name
from livetype.textops import (
    common_affixes,
    common_suffix,
    fold,
    has_line_break,
    merge_user_edits,
    one_line,
    plan_tail_rewrite,
    remap,
)

log = logging.getLogger("livetype")

HISTORY_KEEP = 200  # dictations kept in history.jsonl
_HISTORY_LOCK = threading.Lock()

# terminals: input goes to the prompt only, Shift+Left does not select text, Ctrl+End deletes in
# cmd/PowerShell. Recognized by the window class (classic consoles report the client process,
# e.g. powershell.exe, which can also own ordinary GUI windows) or by the terminal app's process.
TERMINAL_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "mintty", "PuTTY", "VirtualConsoleClass"}
TERMINAL_PROCESSES = {
    "windowsterminal.exe", "openconsole.exe", "conhost.exe", "mintty.exe", "putty.exe", "kitty.exe",
    "alacritty.exe", "wezterm-gui.exe", "conemu.exe", "conemu64.exe", "tabby.exe", "hyper.exe",
}


def is_terminal(hwnd: int) -> bool:
    return win.window_class(hwnd) in TERMINAL_CLASSES or win.window_process(hwnd) in TERMINAL_PROCESSES


def count_changes(old: str, new: str) -> int:
    sm = difflib.SequenceMatcher(a=old.split(), b=new.split(), autojunk=False)
    return sum(1 for op in sm.get_opcodes() if op[0] != "equal")


def _takes(fn, name: str | None = None) -> bool:
    """Does `fn` take a parameter called `name` (or, with name None, any positional one)?"""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    if name is not None:
        return any(p.name == name for p in params)
    return any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params)


class Typer(threading.Thread):
    """Types the dictation into the field where it started and keeps that text equal to the
    engine's current text.

    Ops, in order: add(delta) - words appended at the end; revise(target) - the engine's whole
    current text changed (a chunk got its final text); finish(target) - the last revision, at
    stop. A revision diffs our own model of what we typed against the full target and replaces
    only the differing middle:
      1. UI Automation, when the locator is trusted (a probe found exactly our words; it is
         re-probed before each revision): our text is located again, merged three-way with the
         user's own edits, replaced and confirmed; words not typed yet then follow as usual;
      2. keys: Backspace over the changed tail plus the new text, in one SendInput batch, only
         for a tail we own, with the caret still right after it, in the same window and focused
         control, with no modifier held, at most rewrite_max_chars long and with no character
         that editors delete differently (an emoji sequence, a combining mark);
      3. otherwise skipped: the next revision diffs against the full target again.

    * Another window in front -> waits (never types into the wrong app).
    * You type or click in the window -> waits until you pause; what was typed before that is no
      longer ours to rewrite with keys (UI Automation may still merge into it). The same when an
      app moves the focus to another control.
    * Our text is gone from the field: the words go on at the cursor (there is no "lost" mode).
      Words wait only while the focus is outside the dictation's field or text is selected there.
    * No text field at the start (Chromium builds its accessibility tree late): plain keys, and
      the field is looked for again once our first words are in.
    * Line breaks are typed as a space, never as Enter.
    * The clipboard gets only words that could never be typed (the window did not come back).
    """

    def __init__(self, cfg: Config, target: int, watcher: win.InputWatcher | None, ui,
                 restore_ui: Callable[[], None], live: bool = True):
        super().__init__(name="typer", daemon=True)
        self.cfg, self.target, self.watcher, self.ui = cfg, target, watcher, ui
        self.restore_ui = restore_ui
        self.live = live  # False: keep words until the end and insert the final text once (Type's style)
        self.q: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.raw = ""  # the engine's current text, as received
        self.goal = ""  # the same, as it is typed (one line)
        self.base = ""  # our own model: everything we typed, with our corrections (never the user's edits)
        self.covered = 0  # base is our rendition of goal[:covered]; goal[covered:] is not typed yet
        self.seg_start = 0  # base[:seg_start] is gone from the field (deleted, a message sent)...
        self.gone_at = 0  # ...and stood for goal[:gone_at]
        self.owned = 0  # the last `owned` characters of base were typed after the user's last input
        self.at_end = True  # the caret is right after our text (we typed last and left it there)
        self.user_edits = 0
        self.mode = "keys"  # "uia" | "keys"
        self.terminal = is_terminal(target)
        self.uia = None
        self.anchor = None
        self.uia_trusted = False  # the locator once found exactly our text in this field
        self.applied = 0  # revisions applied
        self.skipped = 0  # revisions skipped
        self.skip_reason = ""
        self.merged_edits = False  # a revision kept the user's own edits
        self.kept_live = False  # the last revision was withheld: the new text lost over half the words
        self.result = ""  # outcome: done | partial | undelivered | cancelled | error
        self.detail = ""  # same | fixed | merged | kept_live
        self.undelivered = ""
        self.wait_reason = ""  # why words last had to wait: "window" (another one in front) | "field"
        self.keep_open = False  # after done, wait for the language model's version (polish)
        self.polish_result = ""
        self.ready = threading.Event()
        self.done = threading.Event()
        self.polished = threading.Event()
        self._seen = watcher.count if watcher else 0
        self._done_seen = 0
        self._focus = 0  # the focused control when we last typed
        self._want_revision = False
        self._finishing = False
        self._to_end = False  # keys mode: press Ctrl+End before typing on (after the user's edit)
        self._paused_note = False
        self._held_note = False
        self._probe_due = False
        self._probes = 0
        self._attach_tries = 0
        self._last_typed = 0.0

    @property
    def typed(self) -> str:
        return self.base

    # ------------------------------------------------------------------ ops (any thread)
    def add(self, delta: str) -> None:
        if self.live and delta:
            self.q.put(("add", delta))

    def revise(self, target: str) -> None:
        if self.live and target is not None:
            self.q.put(("revise", target))

    def finish(self, final_text: str | None = None) -> None:
        self.q.put(("finish", final_text))

    def cancel(self) -> None:
        self.q.put(("cancel", None))

    def polish(self, text: str) -> None:
        """After done: the language model's version of the text (applied only if nothing moved)."""
        self.q.put(("polish", text))

    def close(self) -> None:
        self.q.put(("close", None))

    # ------------------------------------------------------------------ main loop
    def _init_uia(self) -> None:
        # terminals expose their whole screen buffer through UI Automation, but input only goes
        # to the prompt: selecting a range there does nothing, so they use plain keystrokes
        if self.uia is not None or not self.cfg.use_uia or self.terminal:
            return
        try:
            from livetype.uia import Anchor, Uia

            self.uia = Uia()
            # Chromium/Electron build their accessibility tree only after the first UIA request,
            # so the text field may appear a moment later
            field_ = None
            for _ in range(8):
                field_ = self.uia.text_field()
                if field_ is not None:
                    break
                time.sleep(0.12)
            if field_ is not None:
                self.anchor = Anchor(field_)
                self.mode = "uia"
        except Exception:
            log.exception("UI Automation is not available")

    def run(self) -> None:
        self._init_uia()
        self._focus = self._focused_control()
        self.ready.set()
        deadline = 0.0
        while True:
            for kind, arg in self._take(0.04):
                if kind == "cancel":
                    self.result = "cancelled"
                    self.done.set()
                    return
                if kind in ("add", "revise", "finish"):
                    if kind == "finish" and not self._finishing:
                        deadline = time.monotonic() + self.cfg.undelivered_wait_s
                    self.feed(kind, arg)
            try:
                self.step()
            except Exception:
                log.exception("typing step failed")
                time.sleep(0.1)
            if self._finishing and (self.settled() or time.monotonic() > deadline):
                try:
                    self.conclude()
                except Exception:
                    log.exception("could not conclude the dictation")
                    self.result = "error"
                self.done.set()
                if self.keep_open:
                    self._after_done()
                return

    def _take(self, timeout: float) -> list[tuple[str, str | None]]:
        items = []
        try:
            items.append(self.q.get(timeout=timeout))
            while True:
                items.append(self.q.get_nowait())
        except queue.Empty:
            pass
        return items

    def feed(self, kind: str, arg: str | None) -> None:
        """Take one op into our state (nothing is typed here)."""
        if kind == "add":
            self.raw += arg or ""
            self.goal = one_line(self.raw)
            return
        if arg is not None:  # revise / finish with the engine's whole current text
            new = one_line(arg)
            old = self.goal
            self.covered = remap(self.covered, old, new)
            self.gone_at = min(remap(self.gone_at, old, new), self.covered)
            self.raw, self.goal = arg, new
        self._want_revision = True
        if kind == "finish":
            self._finishing = True

    def settled(self) -> bool:
        return self.covered >= len(self.goal) and not self._want_revision

    def step(self) -> None:
        """One round: apply a requested revision, then type the words not typed yet."""
        if self.covered >= len(self.goal) and not self._want_revision:
            self._maybe_probe()
            return
        if not self._ready():
            return
        if self._want_revision:
            self._want_revision = False
            if self._covered_differs():
                self._revise()  # keys: new words included; UI Automation: only what we typed
        if self.covered < len(self.goal):
            self._type(self.goal[self.covered:])

    def conclude(self) -> str:
        """The outcome, at the end: done, partial (a correction could not be applied: the field
        keeps the live text there, the final text is in the history) or undelivered (words could
        never be typed: they go to the clipboard). A final text that lost over half of the words
        is not applied on purpose: done, detail kept_live."""
        rest = self.goal[self.covered:]
        differs = self._covered_differs()
        if rest.strip():
            self.undelivered = rest if self.base.strip() else rest.lstrip()
            win.set_clipboard(self.undelivered)
            self.result = "undelivered"
        elif self._gone_differs() or (differs and not self.kept_live):
            self.result = "partial"
        else:
            self.result = "done"
        if differs and self.kept_live:
            self.detail = "kept_live"
        else:
            self.detail = "merged" if self.merged_edits else ("fixed" if self.applied else "same")
        return self.result

    def _covered_differs(self) -> bool:
        return self.base[self.seg_start:] != self.goal[self.gone_at:self.covered]

    def _gone_differs(self) -> bool:
        """Our earlier text left the field and its correction could not be applied any more."""
        return bool(self.seg_start) and self.base[:self.seg_start].split() != self.goal[:self.gone_at].split()

    def _ready(self) -> bool:
        if win.foreground() != self.target:
            self.wait_reason = "window"
            if not self._paused_note:
                self._paused_note = True
                self.ui.notice("Пауза: открыто другое окно", "вернитесь в окно, где начали диктовку", kind="warning")
            return False
        if self._paused_note:
            self._paused_note = False
            self.restore_ui()
        if not self._user_input_settled():
            return False
        return not win.modifiers_down()

    def _user_input_settled(self) -> bool:
        """The user's own input in the window: wait until they pause, then take it into account."""
        w = self.watcher
        if w is None or w.count == self._seen:
            return True
        if time.monotonic() - w.last_time < self.cfg.user_idle_s:
            return False  # you are typing/clicking right now
        self._take_user_input()
        return True

    def _take_user_input(self) -> None:
        """Real input in the window: everything typed so far is no longer ours to rewrite with keys,
        and the caret may be anywhere."""
        self._seen = self.watcher.count if self.watcher is not None else self._seen
        self.user_edits += 1
        self.owned = 0
        if self.mode != "uia":
            self._attach_uia()  # our text is still intact: go on right after it, not at the field's end
        if self.mode == "uia":
            self.at_end = False
        else:  # plain keystrokes: go on at the end of the field ("end") or at the cursor ("caret")
            self._to_end = self.cfg.after_user_edit == "end"
            self.at_end = True

    def _focused_control(self) -> int:
        try:
            return win.focused_control(self.target)
        except Exception:
            return 0

    def _check_focus(self) -> None:
        """Another control got the focus without input from the user (an app can move it): what
        we typed before went to the old one and is not ours to rewrite with Backspace here."""
        f = self._focused_control()
        if f != self._focus:
            if self.owned:
                log.info("the focused control changed")
            self.owned = 0
            self._focus = f

    def _mark_typed(self) -> None:
        self._last_typed = time.monotonic()
        self._probe_due = True
        self._focus = self._focused_control()

    def _maybe_probe(self) -> None:
        """Once the first words are in, check that the locator finds exactly them: from then on a
        difference in our text is the user's own edit, not a lookup error. Without a field at the
        start (Chromium builds its accessibility tree late), try to find one now."""
        if (not self._probe_due or self._probes >= 4 or time.monotonic() - self._last_typed < 0.3
                or self.uia_trusted):
            return
        if self.mode != "uia":
            if self.uia is not None:
                self._probe_due = False  # once per batch of typed words (bounded in _attach_uia)
                self._attach_uia()
            return
        if self.anchor is None:
            return
        self._probe_due = False
        self._probes += 1
        raw, span, region = self.anchor.find()
        if region is not None and self._finds_ours(raw[span[0]:span[1]]):
            self.uia_trusted = True
            log.info("UIA locator trusted")

    def _finds_ours(self, current: str) -> bool:
        """The located text holds exactly our text (folded), once: the locator works in this field.
        Text around it is the user's (typed next to ours); a merge keeps it as it is."""
        ours, cur = fold(self.anchor.text).s, fold(current).s
        return cur == ours or (len(ours.strip()) >= 3 and cur.count(ours) == 1)

    def _attach_uia(self) -> bool:
        """Plain-keystroke mode although UI Automation works (no text field was found when the
        dictation started): if the focused field now holds exactly our text, once, anchor it there
        and trust the locator. Nothing changes otherwise."""
        if (self.mode == "uia" or self.uia is None or self.terminal or not self.cfg.use_uia
                or self._attach_tries >= 30):
            return False
        ours = self.base[self.seg_start:]
        if len(ours.strip()) < 3:
            return False
        self._attach_tries += 1
        try:
            from livetype.uia import Anchor

            field_ = self.uia.text_field()
            if field_ is None:
                return False
            raw = field_.read()
            hay, needle = fold(raw), fold(ours).s
            k = hay.s.rfind(needle) if needle else -1
            if k < 0 or hay.s.find(needle) != k:
                return False  # not there, or more than once
            start, end = hay.span(k, k + len(needle))
            self.anchor = Anchor.around(field_, raw, start, end)
            self.mode, self.uia_trusted = "uia", True
            if self._to_end:
                self._to_end, self.at_end = False, False  # find our end instead of Ctrl+End
            log.info("UIA field found during the dictation; locator trusted")
            return True
        except Exception as e:
            log.info("UI Automation field lookup failed (%s)", type(e).__name__)
            return False

    # ------------------------------------------------------------------ typing new words
    def _typed(self, text: str, shown: str | None = None) -> None:
        """Bookkeeping after typing goal[covered:covered+len(text)] (`shown` = what went into the
        field, without a leading space that would follow nothing)."""
        self.base += text
        self.covered += len(text)
        self.owned += len(text if shown is None else shown)
        self._mark_typed()

    def _type(self, text: str) -> None:
        self._check_focus()
        if self.mode == "uia":
            try:
                if self._type_uia(text):
                    self._resumed()
                return
            except Exception:
                log.exception("UIA typing failed; going on with plain keystrokes")
                self.mode, self.uia, self.anchor = "keys", None, None
                if not self.at_end:  # the words go on at the user's cursor, away from our text
                    self.owned, self.at_end = 0, True
                return
        if self._to_end:
            # in cmd and PowerShell Ctrl+End deletes to the end of the line
            win.press(win.VK_END, ctrl=not self.terminal)
            self._to_end = False
        win.type_text(text)
        self._typed(text)
        self.at_end = True

    def _type_uia(self, text: str) -> bool:
        """Type `text` right after our text. False: the words wait (see _go_on_at_caret)."""
        from livetype.uia import END, Caret, point

        a = self.anchor
        if self.at_end and a.field.has_focus():
            win.type_text(text)  # the cursor is still right after our text
            a.text += text
            self._typed(text)
            return True
        for _ in range(1 if self._held_note else 3):  # a read may come while the field is updating
            raw, span, region = a.find()
            if region is not None:
                break
            time.sleep(0.1)
        else:
            return self._go_on_at_caret(text)
        current = raw[span[0]:span[1]]  # with the user's edits inside it
        if self._finds_ours(current):
            self.uia_trusted = True
        a.text = current
        shown = text
        if not current and (span[0] == 0 or raw[span[0] - 1].isspace()):
            shown = text.lstrip()  # everything before was deleted: no leading space
        elif current and not self.base[self.seg_start:] and not current[-1].isspace() and text[:1].isalnum():
            shown = " " + text  # the user typed here before our first word: never glue the two
        end = point(region, END)
        user_field = self.uia.text_field()
        caret = Caret(user_field) if user_field is not None else None
        at_our_end, was_after = self._caret_vs(caret, user_field, end, span[1])
        if not a.field.has_focus():
            a.field.focus()
        self._select(end)
        win.type_text(shown)
        a.text += shown
        self._typed(text, shown if len(shown) <= len(text) else text)
        try:
            self._wait_visible()
            if caret is not None and not at_our_end:
                if not caret.restore(shifted_by=len(shown), was_after_insert=was_after):
                    log.info("could not put the user's cursor back")
                self.at_end = False
            else:
                self.at_end = True
        except Exception:
            log.exception("could not check the typed words")
            self.at_end = False
        return True

    def _caret_vs(self, caret, user_field, end, end_at: int) -> tuple[bool, bool]:
        """Is the user's cursor right at the end of our text / after it? Compared by offsets in
        the field's text when possible: two ranges at the same place may not compare equal
        (Chromium: the end of a text node vs. the start of the next one)."""
        if caret is None or user_field is None or not user_field.same_as(self.anchor.field.el):
            return False, False
        if caret.span is not None:
            return caret.span == (end_at, end_at), caret.span[0] > end_at
        return caret.at(end), caret.after_point(end)

    def _select(self, rng) -> bool:
        """Select through UI Automation; keystrokes must not go out before the field has it."""
        if self.anchor.field.select(rng):
            return True
        log.info("the field did not take our selection in time; trying again")
        return self.anchor.field.select(rng, timeout=0.6)

    def _go_on_at_caret(self, text: str) -> bool:
        """Our text and what was around it are gone from the field (deleted, a message sent). If
        the cursor is still in that field, the dictation goes on at the cursor; only that part
        can be corrected in place from now on. Elsewhere (another element, or text selected that
        typing would replace) the words wait: typed when the cursor is back, else undelivered."""
        from livetype.uia import Anchor

        field_ = self.uia.text_field()
        if field_ is None or not field_.same_as(self.anchor.field.el):
            return self._hold("the focus is outside the dictation's field")
        if field_.selected_text():
            return self._hold("text is selected in the field")
        log.info("our text is no longer found in the field: going on at the cursor")
        a = Anchor(field_)
        shown = text.lstrip() if not a.head or a.head[-1].isspace() else text
        lead = len(text) - len(shown)
        self.base += text[:lead]
        self.covered += lead
        self.seg_start, self.gone_at = len(self.base), self.covered
        self.owned = 0
        self.anchor = a
        win.type_text(shown)
        a.text += shown
        self._typed(shown)
        self.at_end = True
        self.ui.notice("Продолжаю там, где курсор", "прежний текст не найден в поле", kind="warning")
        return True

    def _hold(self, why: str) -> bool:
        self.wait_reason = "field"
        if not self._held_note:
            self._held_note = True
            log.info("words wait: %s", why)
            self.ui.notice("Пауза: курсор не в поле диктовки", "кликните в это поле, и слова допечатаются",
                           kind="warning")
        return False

    def _resumed(self) -> None:
        if self._held_note:
            self._held_note = False
            self.restore_ui()

    def _wait_visible(self, timeout: float = 0.6) -> bool:
        """Keystrokes are processed asynchronously: wait until our text shows up in the field."""
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            if self.anchor is not None and self.anchor.visible():
                return True
            time.sleep(0.02)
        return False

    # ------------------------------------------------------------------ revisions
    def _revise(self) -> bool:
        """Bring our text in the field from our model to the target. False: skipped; the next
        revision diffs against the full target again."""
        self._attach_uia()  # re-probe: a text field may have shown up since the start
        fb = self.base[self.seg_start:]
        typed = self.goal[self.gone_at:self.covered]  # what the words we typed have become
        if len(typed.strip()) < 0.5 * len(fb.strip()):
            self.kept_live = True  # the final text lost words: ours stays
            return self._skipped("the new text lost over half of the words")
        why = []
        if self.mode == "uia" and self.anchor is not None:
            # only the typed part is merged: words not typed yet follow by ordinary typing (merged
            # in, they could clash with text the user added after ours and get dropped)
            r = self._revise_uia(fb, typed)
            if r is True:
                return self._applied()
            why.append(r)
        r = self._revise_keys(fb, self.goal[self.gone_at:])  # the new words in the same batch
        if r is True:
            self.covered = len(self.goal)
            return self._applied()
        why.append(r)
        return self._skipped("; ".join(why))

    def _applied(self) -> bool:
        self.applied += 1
        self.kept_live = False
        return True

    def _skipped(self, why: str) -> bool:
        self.skipped += 1
        self.skip_reason = why
        log.info("revision skipped: %s", why)
        return False

    def _revise_uia(self, fb: str, fg: str):
        """Method 1: locate our text again, merge with the user's edits, verified replace.
        True, or why it was not used."""
        try:
            raw, span, region = self.anchor.find()
            if region is None:
                return "uia: our text not found"
            current = raw[span[0]:span[1]]
            if self._finds_ours(current):
                self.uia_trusted = True
            elif not self.uia_trusted:
                return "uia: locator not trusted"
            merged = merge_user_edits(fb, current, fg)
            if merged != current:
                why = self._replace(region, span[1], current, merged, fg)
                if why:
                    return why
            if fold(current).s != fold(fb).s:  # U+00A0, "…" for "..." and the like are no edits
                self.merged_edits = True
            self.anchor.text = merged
            self.base = self.base[: self.seg_start] + fg  # our model never takes in the user's edits
            return True
        except Exception:
            log.exception("UIA revision failed")
            return "uia: error"

    def _replace(self, region, end_at: int, current: str, merged: str, fg: str) -> str | None:
        """Turn our text in the field (`region`, now `current`, ending at offset `end_at` of the
        field's text) into `merged`, retyping only the changed middle; then the user's cursor goes
        back where it was, or to the end of our text if it was there. None, or why not."""
        from livetype.uia import CHAR, END, START, Caret, point

        a = self.anchor
        p, s = common_affixes(current, merged)
        sub = region.Clone()
        sub.MoveEndpointByUnit(START, CHAR, p)
        sub.MoveEndpointByUnit(END, CHAR, -s)
        if sub.GetText(-1) != current[p : len(current) - s]:
            sub, p, s = region, 0, 0
        insert = merged[p : len(merged) - s]
        if has_line_break(insert) or has_line_break(current[p : len(current) - s]):
            # a line break the user made inside our text: retyping would make it a space
            return "uia: the change spans a line break"
        user_field = self.uia.text_field()
        caret = Caret(user_field) if user_field is not None else None
        at_our_end, was_after = self._caret_vs(caret, user_field, point(region, END), end_at)
        if caret is None or caret.rng is None:
            at_our_end = self.at_end
        if not a.field.has_focus():
            a.field.focus()
        if not self._select(sub):
            return "uia: the field did not take the selection"  # typing now would land at the old cursor
        if insert:
            win.type_text(insert)
        else:
            win.press(win.VK_BACK)
        # ours: the untouched part of the owned tail, or all of it from where we retyped
        o = len(current) - min(self.owned, len(current))
        owned = len(merged) - o if p >= o else min(self.owned, s)
        self.owned = max(0, min(owned, common_suffix(merged, fg)))
        a.text = merged
        self._mark_typed()
        if not self._confirmed(merged):
            # the field does not show what we typed (slow editor, an input rule we do not know):
            # from now on a difference there is not taken for the user's edit
            log.info("the corrected text was not confirmed in the field")
            self.uia_trusted = False
        if caret is not None and caret.rng is not None and not at_our_end:
            caret.restore(shifted_by=len(merged) - len(current), was_after_insert=was_after)
            self.at_end = False
        else:
            self.at_end = s == 0 or self._caret_to_end()
        return None

    def _confirmed(self, text: str, timeout: float = 0.6) -> bool:
        """After a replacement: our text in the field reads exactly `text` (folded). Keystrokes are
        processed asynchronously, so this waits a little."""
        want = fold(text).s
        t_end = time.monotonic() + timeout
        while True:
            try:
                raw, span = self.anchor.locate()
                if span is not None and fold(raw[span[0]:span[1]]).s == want:
                    return True
            except Exception:
                log.debug("could not read the field", exc_info=True)
            if time.monotonic() >= t_end:
                return False
            time.sleep(0.02)

    def _caret_to_end(self) -> bool:
        from livetype.uia import END, point

        raw, span, region = self.anchor.find()
        return region is not None and self._select(point(region, END))

    def _revise_keys(self, fb: str, fg: str):
        """Method 2: Backspace over the changed tail plus the new text, in one batch. True, or why
        it was not allowed."""
        plan = plan_tail_rewrite(fb, fg)
        start = self.seg_start + plan.keep
        owned_start = len(self.base) - self.owned
        if start < owned_start:
            return "keys: the change is in text that is no longer ours (your input or another control since)"
        if not self.at_end:
            return "keys: the cursor is not after our text"
        if len(plan.delete) > self.cfg.rewrite_max_chars:
            return "keys: the changed part is too long"
        if plan.editor_dependent:
            return "keys: how many Backspaces delete this text depends on the editor"
        if win.foreground() != self.target or self._focused_control() != self._focus:
            return "keys: the focus moved"
        if win.modifiers_down():
            return "keys: a modifier key is held"
        if not self._field_ends_with(plan.delete):
            return "keys: the field does not end with our text"
        if self.watcher is not None and self.watcher.count != self._seen:
            return "keys: new input from the user"
        if self._to_end:
            win.press(win.VK_END, ctrl=not self.terminal)
            self._to_end = False
        win.rewrite(plan.backspaces, plan.insert)
        if self.anchor is not None:
            t = self.anchor.text
            if plan.delete and not t.endswith(plan.delete):
                self.uia_trusted = False  # our picture of the field was off
            self.anchor.text = (t[: len(t) - len(plan.delete)] if t.endswith(plan.delete) else t) + plan.insert
        self.base = self.base[:start] + plan.insert
        self.owned = len(self.base) - owned_start
        self.at_end = True
        self._mark_typed()
        return True

    def _field_ends_with(self, text: str) -> bool:
        """When the focused field can be read: the text right before the caret is `text`, what
        Backspace is about to delete (an editor that made "..." into "…" fails this)."""
        if not text or self.uia is None:
            return True
        try:
            f = self.uia.text_field()
            if f is None:
                return True
            want = text.replace("\xa0", " ")
            for _ in range(3):  # the field may show our last keystrokes a moment later
                sel = f.selection()
                if sel is None:
                    return True
                if sel.GetText(-1):
                    return False  # text is selected: typing would replace it
                if f.context(sel, len(text) + 1, before=True).replace("\xa0", " ").endswith(want):
                    return True
                time.sleep(0.1)
            return False
        except Exception:
            log.debug("could not read the text before the caret", exc_info=True)
            return True

    # ------------------------------------------------------------------ after done
    def _after_done(self) -> None:
        """Wait for the language model's version of the text, or for close."""
        self._done_seen = self.watcher.count if self.watcher is not None else 0
        while True:
            kind, arg = self.q.get()
            if kind in ("close", "cancel"):
                return
            if kind == "polish":
                try:
                    self.polish_result = self.apply_polish(arg or "")
                except Exception:
                    log.exception("could not apply the processed text")
                    self.polish_result = "error"
                self.polished.set()
                return

    def apply_polish(self, text: str) -> str:
        """The safe rules for the language model's text: the window in front, no input from the
        user since done, and the whole change in text we still own. "applied" or why not."""
        w = self.watcher
        if w is not None and w.count != self._done_seen:
            return "user input since done"
        if w is not None and w.count != self._seen:
            self._seen = w.count  # input during dictation that we had not looked at yet
            self.owned = 0
            self.at_end = self.mode != "uia" and self.at_end
        if win.foreground() != self.target:
            return "another window in front"
        if win.modifiers_down():
            return "a modifier key is held"
        if self.seg_start or self.covered < len(self.goal):
            return "the dictated text is not all in the field"
        new = one_line(text)
        plan = plan_tail_rewrite(self.base, new)
        if plan.keep < len(self.base) - self.owned:
            return "the text is no longer ours"
        saved = (self.raw, self.goal, self.covered, self.applied)
        self.raw, self.goal, self.covered = text, new, len(new)
        if self._revise():
            return "applied"
        self.raw, self.goal, self.covered, self.applied = saved
        return "skipped: " + self.skip_reason


class Sounds:
    """Fallback cues for the standalone (no Electron UI) mode; the UI plays Type's own sounds."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.dir = DATA_DIR / "sounds"
        if enabled:
            self._make("start.wav", [(620, 0.06), (880, 0.07)])
            self._make("stop.wav", [(880, 0.06), (620, 0.07)])

    def _make(self, name: str, notes: list[tuple[float, float]]) -> None:
        path = self.dir / name
        if path.exists():
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        sr, frames = 22050, bytearray()
        for freq, dur in notes:
            n = int(sr * dur)
            for i in range(n):
                env = min(1.0, i / (0.006 * sr), (n - i) / (0.012 * sr))
                frames += struct.pack("<h", int(0.22 * 32767 * env * math.sin(2 * math.pi * freq * i / sr)))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(bytes(frames))

    def play(self, name: str) -> None:
        if self.enabled:
            winsound.PlaySound(str(self.dir / name), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


@dataclass
class SessionReport:
    live_text: str = ""
    final_text: str = ""
    outcome: str = ""
    detail: str = ""
    user_edits: int = 0
    mode: str = ""
    seconds: float = 0.0
    final_pass_s: float = 0.0
    stop_to_done_s: float = 0.0
    revisions_applied: int = 0
    revisions_skipped: int = 0
    decode_avg_ms: float = 0.0
    history_id: str | None = None
    commit_log: list[tuple[float, str]] = field(default_factory=list)


def level_01(rms: float) -> float:
    return min(1.0, max(0.0, (20 * math.log10(rms + 1e-9) + 55) / 40))


class PolishJob(threading.Thread):
    """The language-model pass, after done and in the background: never on the stop path.
    Applied in place only under the typer's safe rules; otherwise saved in the history as the
    processed text. A new dictation cancels it."""

    def __init__(self, app: App, typer: Typer, watcher, text: str, model: str, rules: list, history_id: str | None):
        super().__init__(name="text-processing", daemon=True)
        self.app, self.typer, self.watcher = app, typer, watcher
        self.text, self.model, self.rules, self.history_id = text, model, rules, history_id
        self.cancelled = threading.Event()
        self.result = ""  # applied | saved | rejected | cancelled | error

    def cancel(self) -> None:
        self.cancelled.set()

    def run(self) -> None:
        from livetype import llm

        t0 = time.monotonic()
        try:
            budget = llm.deadline_s(self.model, self.text)
            out = self.app.text_processor.process(self.text, self.rules, model=self.model, timeout=budget,
                                                  cancel=self.cancelled)
            spent = time.monotonic() - t0
            if self.cancelled.is_set():
                self.result = "cancelled"
            elif not out or one_line(out).strip() == one_line(self.text).strip():
                self.result = "rejected" if not out else "same"
            else:
                self.typer.polish(out)
                self.typer.polished.wait(5.0)
                why = self.typer.polish_result or "timeout"
                applied = why == "applied"
                self.result = "applied" if applied else "saved"
                App._update_history(self.history_id, processed=out, processed_applied=applied, text=out)
                if not applied:
                    log.info("processed text saved to history only: %s", why)
                    self.app.ui.notice("Обработанный текст — в истории", "в поле остался распознанный текст",
                                       kind="info")
            self.app._event(f"EVENT processed result={self.result} model={self.model} llm={spent:.1f}s")
        except Exception:
            log.exception("text processing failed")
            self.result = "error"
        finally:
            self.typer.close()
            self.typer.join(2.0)
            if self.watcher is not None and self.watcher.is_alive():
                self.watcher.stop()


class App:
    def __init__(self, cfg: Config, ui, source_factory: Callable[[], Source], *,
                 auto: bool = False, once: bool = False, debug: bool = False, play_sounds: bool = True):
        self.cfg, self.ui, self.source_factory = cfg, ui, source_factory
        self.auto, self.once, self.debug = auto, once, debug
        self.cmds: queue.Queue[str] = queue.Queue()
        self.phase = "idle"
        self.last: SessionReport | None = None
        self.text_processor = None  # set when the language-model pass is available
        self.polish_job: PolishJob | None = None
        self.on_text_settings_changed: Callable[[], None] = lambda: None
        self.arm_cancel: Callable[[bool], None] = lambda on: None  # Esc cancels while dictating
        t = time.perf_counter()
        self.live = None
        self.final = self._load_recognizer(cfg)
        self.vad = StreamingVad(cfg.models_dir)
        log.info("models loaded in %.1f s (%s)", time.perf_counter() - t, self.recognizer_name)
        self.hotkey_vk = 0
        self.hold_down = False  # hold mode: the hotkey is held right now
        self.sounds = Sounds(cfg.sounds and play_sounds)
        self.thread = threading.Thread(target=self._worker, name="controller", daemon=True)

    def _load_recognizer(self, cfg: Config):
        """Type's own recognizer and model when Type is installed (the same text Type types),
        else our GigaAM v3 RNNT."""
        if getattr(cfg, "recognizer", "type") == "type":
            from livetype import type_runtime
            if type_runtime.available():
                try:
                    rec = type_runtime.TypeAsr()
                    rec.text(np.zeros(SAMPLE_RATE, dtype=np.float32))  # warm-up
                    self.recognizer_name = "type"
                    return rec
                except Exception:
                    log.exception("Type's recognizer failed to start, using GigaAM v3 RNNT")
        self.recognizer_name = cfg.final_model
        return Recognizer(cfg.final_model, cfg.models_dir, cfg.quantization, cfg.final_threads)

    def start(self) -> None:
        self.thread.start()

    # hotkey events: toggle mode uses press only; hold mode starts on press, stops on release
    def toggle(self) -> None:
        self.cmds.put("toggle")

    def hotkey_down(self) -> None:
        if self.cfg.activation_mode == "hold":
            self.hold_down = True
            if self.phase == "idle":
                self.cmds.put("toggle")
        else:
            self.cmds.put("toggle")

    def hotkey_up(self) -> None:
        if self.cfg.activation_mode == "hold":
            self.hold_down = False
            # a short press may end before the microphone is even open ("starting")
            if self.phase in ("starting", "listening"):
                self.cmds.put("toggle")

    def cancel(self) -> None:
        self.cmds.put("cancel")

    def quit(self) -> None:
        self.cmds.put("quit")

    def _event(self, msg: str) -> None:
        log.info(msg)
        if self.debug:
            print(msg, flush=True)

    def _worker(self) -> None:
        while True:
            cmd = self.cmds.get()
            if cmd == "quit":
                self._end_polish(wait=False)
                close = getattr(self.final, "close", None)
                if close:
                    close()  # Type's recognizer runs in a process of its own
                return
            if cmd != "toggle":
                continue
            try:
                self.phase = "starting"
                self._session()
            except Exception as e:  # keep the app alive
                log.exception("session failed")
                self.ui.notice("Ошибка", str(e)[:120], kind="error")
            self.phase = "idle"
            # presses made while the final check was running must not start a new dictation
            if "quit" in self._drain(self.cmds, 0):
                return
            if self.once:
                job = self.polish_job
                if job is not None:
                    job.join(70)
                time.sleep(1.8)
                self.ui.quit()
                return

    def _restore_ui(self) -> None:
        if self.phase == "listening":
            self.ui.listening()
        elif self.phase == "processing":
            self.ui.processing()

    def _set_level(self, rms: float) -> None:
        self.ui.level = level_01(rms) if getattr(self.ui, "wants_unit_level", False) else rms

    @staticmethod
    def _drain(q: queue.Queue, timeout: float) -> list:
        items = []
        try:
            items.append(q.get(timeout=timeout) if timeout else q.get_nowait())
            while True:
                items.append(q.get_nowait())
        except queue.Empty:
            pass
        return items

    def _end_polish(self, wait: bool = True) -> None:
        """A new dictation (or quit) cancels the language-model pass of the previous one."""
        job = self.polish_job
        if job is None:
            return
        job.cancel()
        if wait:
            job.join(3.0)
        if not job.is_alive():
            self.polish_job = None

    def _make_engine(self, typer: Typer):
        """Segments cut at pauses, each recognized as soon as it closes and typed at once
        (SegmentEngine never revises; on_revise is passed for engines that do)."""
        kwargs = {"on_commit": typer.add, "on_level": self._set_level}
        revising = _takes(DictationEngine, "on_revise")
        if revising:
            kwargs["on_revise"] = typer.revise
        engine = DictationEngine(self.cfg, self.live, self.vad, self.final, **kwargs)
        if not revising and hasattr(engine, "on_revise"):
            engine.on_revise = typer.revise
            revising = True
        return engine, revising

    def _finish_engine(self, engine) -> str:
        if _takes(engine.finish, "budget_s"):
            return engine.finish(budget_s=float(getattr(self.cfg, "stop_budget_s", 0.8)))
        return engine.finish()

    def _polish_wanted(self, final: str) -> bool:
        tp = self.text_processor
        return bool(self.cfg.text_processing and tp is not None and final.strip() and tp.ready(self.cfg.text_model))

    def _session(self) -> None:
        cfg = self.cfg
        self._end_polish(wait=True)
        target = win.foreground()
        if not target or win.is_own_window(target):
            self.ui.notice("Куда печатать?", "кликните в поле для текста и нажмите клавишу снова", kind="warning")
            self._event("EVENT refused target=self")
            return
        tp = self.text_processor
        if cfg.text_processing and tp is not None:
            try:  # at the hotkey, in the background: the server is never started on the stop path
                tp.idle_stop_s = getattr(cfg, "llm_idle_stop_s", tp.idle_stop_s)
                tp.start_async(cfg.text_model)
            except Exception:
                log.exception("could not start the language model")
        watcher = typer = engine = source = None
        ended = False  # the capsule was put back to idle (done/hide) by the normal path
        handed_off = False  # the typer and the watcher went on to the language-model pass
        try:
            watcher = win.InputWatcher(target=target, hotkey=cfg.hotkey)
            watcher.start()
            watcher.ready.wait(1.0)
            typer = Typer(cfg, target, watcher, self.ui, self._restore_ui, live=cfg.live_typing)
            typer.start()
            typer.ready.wait(2.0)
            engine, revising = self._make_engine(typer)
            self._event(f"EVENT start target={win.window_process(target)} mode={typer.mode} revise={int(revising)}")
            source = self.source_factory()
            self.phase = "listening"
            self.ui.listening()
            self.sounds.play("start.wav")
            if cfg.activation_mode == "hold" and not self.hold_down and not self.auto:
                self.cmds.put("toggle")  # released while we were starting
            t0 = time.monotonic()
            cancelled = False
            source.start()
            self.arm_cancel(True)
            try:
                while True:
                    cmds = self._drain(self.cmds, 0) if not self.cmds.empty() else []
                    if "quit" in cmds:
                        self.cmds.put("quit")
                        break
                    if "cancel" in cmds:
                        cancelled = True
                        break
                    if "toggle" in cmds:
                        break
                    for chunk in self._drain(source.q, 0.03):
                        engine.feed(chunk)
                    engine.poll()
                    if self.auto and source.finished.is_set() and source.q.empty():
                        break
            finally:
                source.stop()
                self.arm_cancel(False)
            if cancelled:
                typer.cancel()
                self.ui.hide()
                ended = True
                self._event("EVENT cancelled")
                return
            for chunk in self._drain(source.q, 0):
                engine.feed(chunk)
            self.phase = "processing"
            self.ui.processing()
            self.sounds.play("stop.wav")
            self._event("EVENT stop")
            t1 = time.monotonic()
            final = self._finish_engine(engine) or ""  # without the final check this is the live text
            final_s = time.monotonic() - t1
            polish = self._polish_wanted(final)
            typer.keep_open = polish
            typer.finish(final if final.strip() else None)
            typer.done.wait(cfg.undelivered_wait_s + 4.0)
            outcome = typer.result or "timeout"
            stop_to_done = time.monotonic() - t1
            changes = count_changes(engine.live_text, final)
            self._report(outcome, typer.detail, changes, typer.wait_reason)  # "done" right after the final apply
            ended = True
            dec = engine.decode_times
            best = final if final.strip() and typer.detail != "kept_live" else engine.live_text
            hid = self._save_history({
                "text": best, "live": engine.live_text, "final": final, "processed": None, "outcome": outcome,
            })
            self.last = SessionReport(
                live_text=engine.live_text, final_text=final, outcome=outcome, detail=typer.detail,
                user_edits=typer.user_edits, mode=typer.mode, seconds=time.monotonic() - t0, final_pass_s=final_s,
                stop_to_done_s=stop_to_done, revisions_applied=typer.applied, revisions_skipped=typer.skipped,
                decode_avg_ms=1000 * sum(dec) / len(dec) if dec else 0.0, history_id=hid,
                commit_log=list(engine.commit_log),
            )
            self._event(
                f"EVENT done outcome={outcome} how={typer.detail} mode={typer.mode} edits={typer.user_edits} "
                f"revisions={typer.applied}/{typer.skipped} audio={engine.audio_time:.1f}s final_pass={final_s:.2f}s "
                f"stop_to_done={stop_to_done:.2f}s decode_avg={self.last.decode_avg_ms:.0f}ms changes={changes}"
            )
            if self.debug:
                print("LIVE: " + engine.live_text, flush=True)
                print("FINAL: " + final, flush=True)
            if polish and outcome in ("done", "partial") and typer.done.is_set():
                self.polish_job = PolishJob(self, typer, watcher, final, cfg.text_model, cfg.text_rules, hid)
                self.polish_job.start()
                handed_off = True
        finally:
            # whatever happened (a microphone error, a crash in the engine), leave nothing running
            # and never leave the capsule stuck on screen
            if source is not None:
                source.stop()
            self.arm_cancel(False)
            if typer is not None and not handed_off:
                typer.cancel() if not typer.done.is_set() else typer.close()
            if watcher is not None and not handed_off and watcher.is_alive():
                watcher.stop()
            if engine is not None:
                engine.close()
            if not ended:
                self.ui.finish()

    def _report(self, outcome: str, how: str, changes: int, wait_reason: str = "") -> None:
        if outcome == "done":
            if how in ("fixed", "merged"):
                sub = f"исправлено мест: {changes}" + (" · ваши правки сохранены" if how == "merged" else "")
                self.ui.done("Текст проверен", sub)
            else:
                self.ui.done("Готово", "ошибок не найдено" if how == "same" else "")
            return
        if outcome == "partial":
            self.ui.notice("Исправлено не всё", "полный текст — в истории диктовок", kind="warning")
        elif outcome == "undelivered":
            why = "курсор был не в поле диктовки" if wait_reason == "field" else "окно было неактивно"
            self.ui.notice("Текст — в буфере обмена (Ctrl+V)", f"{why} · полный текст — в истории", kind="warning")
        else:
            self.ui.notice("Не удалось применить правку", outcome, kind="warning")
        self.ui.finish()

    # ------------------------------------------------------------------ history
    @staticmethod
    def _save_history(entry: dict | str, keep: int = HISTORY_KEEP) -> str | None:
        """History lives only on this computer and keeps the last `keep` dictations. An entry has
        the live text, the final ASR text, the processed text and the outcome; `text` is the best
        of them (what the panel shows and copies)."""
        rec = {"text": entry} if isinstance(entry, str) else dict(entry)
        if not str(rec.get("text") or "").strip():
            return None
        rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "id": uuid.uuid4().hex[:12], **rec}
        try:
            with _HISTORY_LOCK:
                HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
                with HISTORY_PATH.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
                if len(lines) > keep + 20:
                    App._write_history(lines[-keep:])
        except OSError:
            log.exception("history write failed")
        return rec["id"]

    @staticmethod
    def _write_history(lines: list[str]) -> None:
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, HISTORY_PATH)

    @staticmethod
    def _update_history(history_id: str | None, **fields: object) -> bool:
        """Add fields to a saved entry (the processed text arrives after done)."""
        if not history_id:
            return False
        try:
            with _HISTORY_LOCK:
                if not HISTORY_PATH.is_file():
                    return False
                lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
                for i in range(len(lines) - 1, -1, -1):
                    try:
                        rec = json.loads(lines[i])
                    except ValueError:
                        continue
                    if rec.get("id") == history_id:
                        rec.update(fields)
                        lines[i] = json.dumps(rec, ensure_ascii=False)
                        App._write_history(lines)
                        return True
        except OSError:
            log.exception("history update failed")
        return False
