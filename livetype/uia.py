"""UI Automation: keep dictated words together right after *our* text, wherever the user's
cursor is, and find our text again for the final fix even after the user edited around it.

Works in fields that expose the UIA TextPattern: browsers and Electron apps (Chrome, Edge,
Claude, Obsidian, VS Code…), Word, Notepad, WPF/WinForms, Qt apps. Elsewhere the caller
falls back to plain keystrokes.
"""

from __future__ import annotations

import logging

import comtypes
import comtypes.client
from comtypes import COMError

log = logging.getLogger("livetype")

UIA_TextPatternId = 10014
START, END = 0, 1  # TextPatternRangeEndpoint
CHAR = 0  # TextUnit_Character
_ERRORS = (COMError, OSError, ValueError, AttributeError)

_module = None


def _uia_module():
    global _module
    if _module is None:
        _module = comtypes.client.GetModule("UIAutomationCore.dll")
    return _module


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
            return self.client.GetFocusedElement()
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

    def selection(self):
        try:
            arr = self.tp.GetSelection()
            return arr.GetElement(0) if arr and arr.Length else None
        except _ERRORS:
            return None

    def find_last(self, text: str, within=None):
        if not text:
            return None
        try:
            return (within or self.document()).FindText(text, True, False)
        except _ERRORS:
            return None

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


class Anchor:
    """Where our dictated text lives in one field.

    When dictation starts we remember the text right before the cursor (`head`) and right
    after it (`after`). Our words are always inserted between the two, so our text can be
    found again even if the user edits inside it. `text` = our text as it is in the field now.
    """

    CTX = 40

    def __init__(self, field: TextField):
        self.field = field
        self.text = ""
        caret = field.selection()
        self.head = TextField.text_before(caret, self.CTX) if caret is not None else ""
        self.after = TextField.text_after(caret, self.CTX) if caret is not None else ""

    def region(self):
        """Range covering our text now, or None if it can no longer be found."""
        try:
            for finder in (self._by_context, self._by_tail):
                r = finder()
                if r is not None and self._plausible(r):
                    return r
        except _ERRORS as e:
            log.debug("anchor region failed: %s", e)
        return None

    def end(self):
        """Empty range at the end of our text (where the next words go)."""
        r = self.region()
        return point(r, END) if r is not None else None

    def resync(self) -> None:
        """Re-read our text from the field (picks up the user's edits inside it)."""
        r = self.region()
        if r is not None:
            try:
                self.text = r.GetText(-1)
            except _ERRORS:
                pass

    def _plausible(self, r) -> bool:
        try:
            return len(r.GetText(-1)) <= 3 * len(self.text) + 200
        except _ERRORS:
            return False

    def _by_context(self):
        doc = self.field.document()
        if self.head:
            h = doc.FindText(self.head, False, False)
            if h is None:
                return None
            start = point(h, END)
        else:
            start = point(doc, START)
        rest = doc.Clone()
        rest.MoveEndpointByRange(START, start, START)
        if self.after:
            a = rest.FindText(self.after, False, False)
            if a is None:
                return None
            end = point(a, START)
        else:  # dictating at the very end: stop at our tail, not at text typed after it
            t = self._tail(rest)
            end = point(t, END) if t is not None else point(doc, END)
        region = start.Clone()
        region.MoveEndpointByRange(END, end, START)
        return region

    def _by_tail(self):
        t = self._tail(None)
        if t is None:
            return None
        end = point(t, END)
        region = end.Clone()
        region.MoveEndpointByUnit(START, CHAR, -len(self.text))
        return region

    def _tail(self, within):
        if not self.text:
            return None
        for n in (60, 30, 15):
            r = self.field.find_last(self.text[-n:], within=within)
            if r is not None:
                return r
            if len(self.text) <= n:
                break
        return None


class Caret:
    """The user's cursor/selection, saved so it can be put back after we type elsewhere."""

    def __init__(self, field: TextField):
        self.field = field
        self.rng = field.selection()
        self.before = self.after = ""
        if self.rng is not None:
            try:
                self.before = TextField.text_before(self.rng, 20)
                self.after = TextField.text_after(self.rng, 20)
            except _ERRORS:
                pass

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
            return (TextField.text_before(sel, 20) == self.before
                    and TextField.text_after(sel, 20) == self.after)
        except _ERRORS:
            return False

    def restore(self, shifted_by: int = 0, was_after_insert: bool = False) -> bool:
        """Put the selection back; if the provider did not move it with the text, shift it."""
        if self.rng is None:
            return False
        try:
            self.field.focus()
            self.rng.Select()
            if self._matches():
                return True
            if was_after_insert and shifted_by:
                r = self.rng.Clone()
                r.Move(CHAR, shifted_by)
                r.Select()
                if self._matches():
                    return True
        except _ERRORS as e:
            log.debug("caret restore failed: %s", e)
        return False
