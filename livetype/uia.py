"""UI Automation: keep dictated words together right after *our* text, wherever the user's
cursor is, and find our text again for the final fix even after the user edited around it.

Works in fields that expose the UIA TextPattern: browsers and Electron apps (Chrome, Edge,
Claude, Obsidian, VS Code…), Word, Notepad, WPF/WinForms, Qt apps. Elsewhere the caller
falls back to plain keystrokes.

Our text is found by reading the field's whole text and matching it in Python
(textops.locate_region), not with the provider's own search: Chromium returns FindText ranges
shifted by one character per paragraph break before the match in editors built of <p> blocks
(ProseMirror, tiptap — the Claude app's composer), and its search is exact while a plain
contenteditable keeps turning spaces into U+00A0 and back. Text offsets become ranges through a
search whose match must read back exactly, or through character moves that are measured.
"""

from __future__ import annotations

import logging
import time

import comtypes
import comtypes.client
from comtypes import COMError

from livetype.textops import fold, locate_region

log = logging.getLogger("livetype")

UIA_TextPatternId = 10014
START, END = 0, 1  # TextPatternRangeEndpoint
CHAR = 0  # TextUnit_Character
_ERRORS = (COMError, OSError, ValueError, AttributeError)
NEAR = 2000  # characters: a point farther than this from both ends of the field is reached from the caret

_module = None


def _uia_module():
    global _module
    if _module is None:
        _module = comtypes.client.GetModule("UIAutomationCore.dll")
    return _module


def ptr(x):
    """comtypes returns a NULL interface pointer, not None, when a provider returns nothing
    (FindText without a match, no focused element...): calling it raises ValueError."""
    return x if x else None


class Uia:
    """UIA client for the current thread (create it inside the thread that uses it)."""

    def __init__(self) -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass  # this thread already has COM
        self.m = _uia_module()
        self.client = comtypes.client.CreateObject(self.m.CUIAutomation, interface=self.m.IUIAutomation)

    def focused_element(self):
        try:
            return ptr(self.client.GetFocusedElement())
        except _ERRORS:
            return None

    def text_field(self, element=None) -> TextField | None:
        el = element if element is not None else self.focused_element()
        if el is None:
            return None
        try:
            pat = el.GetCurrentPattern(UIA_TextPatternId)
            if not pat:
                return None
            return TextField(self, el, pat.QueryInterface(self.m.IUIAutomationTextPattern))
        except _ERRORS as e:
            log.debug("no TextPattern: %s", e)
            return None


def point(rng, endpoint: int):
    """Empty range at the start or end of `rng`."""
    r = rng.Clone()
    if endpoint == END:
        r.MoveEndpointByRange(START, r, END)
    else:
        r.MoveEndpointByRange(END, r, START)
    return r


def _between(a, b):
    """Range from the empty range `a` to the empty range `b`."""
    r = a.Clone()
    r.MoveEndpointByRange(END, b, START)
    return r


def _move(p, n: int) -> None:
    """Move the empty range `p` by n characters."""
    if n > 0:
        p.MoveEndpointByUnit(END, CHAR, n)
        p.MoveEndpointByRange(START, p, END)
    elif n < 0:
        p.MoveEndpointByUnit(START, CHAR, n)
        p.MoveEndpointByRange(END, p, START)


class TextField:
    def __init__(self, uia: Uia, element, pattern):
        self.uia, self.el, self.tp = uia, element, pattern

    def same_as(self, element) -> bool:
        try:
            return bool(self.uia.client.CompareElements(self.el, element))
        except _ERRORS:
            return False

    def has_focus(self) -> bool:
        el = self.uia.focused_element()
        return el is not None and self.same_as(el)

    def focus(self) -> None:
        try:
            self.el.SetFocus()
        except _ERRORS:
            pass

    def document(self):
        return self.tp.DocumentRange

    def read(self) -> str:
        """The field's whole text."""
        return self.document().GetText(-1)

    def selection(self):
        try:
            arr = self.tp.GetSelection()
            return ptr(arr.GetElement(0)) if arr and arr.Length else None
        except _ERRORS:
            return None

    def selected_text(self) -> str:
        sel = self.selection()
        try:
            return sel.GetText(-1) if sel is not None else ""
        except _ERRORS:
            return ""

    def select(self, rng, timeout: float = 0.3) -> bool:
        """Select `rng` and wait until the field reports it: Chromium applies a selection made
        through UI Automation asynchronously, and keystrokes sent too early land at the old place.
        The same place may not compare equal in Chromium (the end of a text node vs. the start of
        the next one), so a selection that does not compare equal is also checked by its offsets
        in the field's text."""
        rng.Select()
        want = None
        t_end = time.monotonic() + timeout
        while True:
            sel = self.selection()
            try:
                if sel is not None:
                    if sel.CompareEndpoints(START, rng, START) == 0 and sel.CompareEndpoints(END, rng, END) == 0:
                        return True
                    if want is None:
                        want = self.offsets(rng)
                    if self.offsets(sel) == want:
                        return True
            except _ERRORS:
                pass
            if time.monotonic() > t_end:
                return False
            time.sleep(0.01)

    def find(self, text: str, last: bool = True):
        """Range of the last (or first) occurrence of `text` in the field, or None."""
        raw = self.read()
        hay, needle = fold(raw), fold(text).s
        if not needle:
            return None
        k = hay.s.rfind(needle) if last else hay.s.find(needle)
        return self.range_at(raw, *hay.span(k, k + len(needle))) if k >= 0 else None

    @staticmethod
    def text_before(rng, n: int) -> str:
        r = point(rng, START)
        r.MoveEndpointByUnit(START, CHAR, -n)
        return r.GetText(-1)

    @staticmethod
    def text_after(rng, n: int) -> str:
        r = point(rng, END)
        r.MoveEndpointByUnit(END, CHAR, n)
        return r.GetText(-1)

    def context(self, rng, n: int, before: bool) -> str:
        """Up to n characters of the field's text before/after `rng`, never beyond the field:
        in Chromium a range moves on out of a contenteditable into the page (menus, buttons)."""
        doc = self.document()
        if before:
            r = point(rng, START)
            r.MoveEndpointByUnit(START, CHAR, -n)
            if r.CompareEndpoints(START, doc, START) < 0:
                r.MoveEndpointByRange(START, doc, START)
        else:
            r = point(rng, END)
            r.MoveEndpointByUnit(END, CHAR, n)
            if r.CompareEndpoints(END, doc, END) > 0:
                r.MoveEndpointByRange(END, doc, END)
        return r.GetText(-1)

    def offsets(self, rng) -> tuple[int, int]:
        """Where `rng` is in the field's text, as [start, end) indices."""
        pre = self.document().Clone()
        pre.MoveEndpointByRange(END, rng, START)
        s = len(pre.GetText(-1))
        return s, s + len(rng.GetText(-1))

    # ------------------------------------------------------------------ text offsets -> ranges
    def range_at(self, raw: str, start: int, end: int):
        """Range covering raw[start:end], where `raw` is the field's text as read() returned it;
        None if it cannot be placed exactly."""
        doc = self.document()
        r = self._search(doc, raw, start, end) if len(raw[start:end].strip()) >= 3 else None
        return r if r is not None else self._walk(doc, raw, start, end)

    @staticmethod
    def _search(doc, raw: str, start: int, end: int):
        """The provider's own search for that exact text, trusted only if the match reads back
        exactly (Chromium shifts matches that follow paragraph breaks in <p>-based editors)."""
        want = raw[start:end]
        hits, k = [], raw.find(want)
        while k >= 0 and k <= start and len(hits) <= 20:  # FindText goes on after the previous match
            hits.append(k)
            k = raw.find(want, k + len(want))
        if not hits or hits[-1] != start:
            return None
        within, m = doc, None
        for _ in hits:
            m = ptr(within.FindText(want, False, False))
            if m is None:
                return None
            within = doc.Clone()
            within.MoveEndpointByRange(START, m, END)
        return m if m.GetText(-1) == want else None

    def _walk(self, doc, raw: str, start: int, end: int):
        s = self._point(doc, raw, start)
        if s is None or end == start:
            return s
        e = self._point(doc, raw, end, s, start)
        if e is None:
            return None
        r = _between(s, e)
        return r if r.GetText(-1) == raw[start:end] else None

    def _point(self, doc, raw: str, i: int, near=None, near_at: int = 0):
        """Empty range at index i of the field's text: character moves from the nearest known
        place (the field's start or end, `near`, the caret), measured by reading the text in
        between (a character move may step over a whole emoji)."""
        refs = [(0, point(doc, START)), (len(raw), point(doc, END))]
        if near is not None:
            refs.append((near_at, near))
        at, ref = min(refs, key=lambda x: abs(x[0] - i))
        if abs(at - i) > NEAR:
            caret = self.selection()
            if caret is not None:
                c = point(caret, START)
                c_at = self.offsets(c)[0]
                if abs(c_at - i) < abs(at - i):
                    at, ref = c_at, c
        p = ref.Clone()
        for _ in range(4):
            n = i - at
            if n == 0:
                break
            ref = p.Clone()
            _move(p, n)
            a, b = (ref, p) if n > 0 else (p, ref)
            moved = len(_between(a, b).GetText(-1))
            if moved == 0:
                return None  # cannot move any further
            at += moved if n > 0 else -moved
        if at != i:
            return None
        k1, k2 = min(8, i), min(8, len(raw) - i)
        if not raw[:i].endswith(self.text_before(p, k1)) or not raw[i:].startswith(self.text_after(p, k2)):
            return None
        return p


class Anchor:
    """Where our dictated text lives in one field.

    When dictation starts we remember the field's text right before the cursor (`head`) and
    right after it (`after`). Our words are always inserted between the two, so our text can be
    found again even if the user edits inside it. `text` = our text as it is in the field now.
    """

    CTX = 40

    def __init__(self, field: TextField):
        self.field = field
        self.text = ""
        self.head = self.after = ""
        caret = field.selection()
        if caret is not None:
            try:
                self.head = field.context(caret, self.CTX, before=True)
                self.after = field.context(caret, self.CTX, before=False)
            except _ERRORS as e:
                log.debug("anchor context failed: %s", e)

    @classmethod
    def around(cls, field: TextField, raw: str, start: int, end: int) -> Anchor:
        """An anchor for text already in the field at raw[start:end] (`raw` = the field's text):
        found during the dictation rather than set up at its start."""
        a = cls.__new__(cls)
        a.field, a.text = field, raw[start:end]
        a.head, a.after = raw[max(0, start - cls.CTX):start], raw[end:end + cls.CTX]
        return a

    def locate(self) -> tuple[str, tuple[int, int] | None]:
        """The field's text and where our text is in it (None if it is gone)."""
        raw = self.field.read()
        return raw, locate_region(raw, self.head, self.after, self.text)

    def find(self):
        """(the field's text, [start, end) of our text in it, a range over it), or
        (text, None, None) if our text can no longer be found."""
        try:
            raw, span = self.locate()
            return raw, span, (self.field.range_at(raw, *span) if span is not None else None)
        except _ERRORS as e:
            log.debug("anchor region failed: %s", e)
            return "", None, None

    def region(self):
        """Range covering our text now, or None if it can no longer be found."""
        return self.find()[2]

    def end(self):
        """Empty range at the end of our text (where the next words go)."""
        r = self.region()
        return point(r, END) if r is not None else None

    def visible(self) -> bool:
        """Our text, down to its last characters, is in the field (keystrokes arrive late)."""
        try:
            raw, span = self.locate()
        except _ERRORS:
            return False
        return span is not None and fold(raw[span[0]:span[1]]).s.endswith(fold(self.text[-30:]).s)

    def resync(self) -> None:
        """Re-read our text from the field (picks up the user's edits inside it)."""
        r = self.region()
        if r is not None:
            try:
                self.text = r.GetText(-1)
            except _ERRORS:
                pass


class Caret:
    """The user's cursor/selection, saved so it can be put back after we type elsewhere."""

    def __init__(self, field: TextField):
        self.field = field
        self.rng = field.selection()
        self.before = self.after = ""
        self.span: tuple[int, int] | None = None  # where it is in the field's text
        self.size = 0  # length of the field's text then
        if self.rng is not None:
            try:
                self.before = TextField.text_before(self.rng, 20)
                self.after = TextField.text_after(self.rng, 20)
                self.span = field.offsets(self.rng)
                self.size = len(field.read())
            except _ERRORS:
                self.span = None

    def at(self, pos) -> bool:
        try:
            return (self.rng is not None and self.rng.CompareEndpoints(START, pos, START) == 0
                    and self.rng.CompareEndpoints(END, pos, END) == 0)
        except _ERRORS:
            return False

    def after_point(self, pos) -> bool:
        try:
            return self.rng is not None and self.rng.CompareEndpoints(START, pos, START) > 0
        except _ERRORS:
            return False

    def _matches(self) -> bool:
        sel = self.field.selection()
        if sel is None:
            return False
        try:
            return (fold(TextField.text_before(sel, 20)).s == fold(self.before).s
                    and fold(TextField.text_after(sel, 20)).s == fold(self.after).s)
        except _ERRORS:
            return False

    def restore(self, shifted_by: int = 0, was_after_insert: bool = False) -> bool:
        """Put the selection back where it was. Text we inserted before it moves it on by as
        much as the field's text grew (measured: an editor may store fewer characters than we
        typed, e.g. "…" for "...")."""
        if self.rng is None:
            return False
        try:
            if not self.field.has_focus():
                self.field.focus()
            if self.span is not None:
                raw = self.field.read()
                shift = (len(raw) - self.size) if was_after_insert else 0
                r = self.field.range_at(raw, self.span[0] + shift, self.span[1] + shift)
                if r is not None:
                    self.field.select(r)
                    return True
            # the field's text could not be read: the provider may keep the saved range in place
            self.field.select(self.rng)
            if self._matches():
                return True
            if was_after_insert and shifted_by:
                r = self.rng.Clone()
                r.Move(CHAR, shifted_by)
                self.field.select(r)
                if self._matches():
                    return True
        except _ERRORS as e:
            log.debug("caret restore failed: %s", e)
        return False
