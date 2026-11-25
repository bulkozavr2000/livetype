"""UI Automation logic against a fake text provider (no desktop): placing text offsets as ranges
when the provider's own search is wrong, and finding our text again in rich-editor fields."""

from livetype.uia import Anchor, TextField

NBSP = "\xa0"


class _Text:
    def __init__(self, text: str, shifted_search: bool = False):
        self.text = text
        self.shifted_search = shifted_search  # Chromium in <p>-based editors (ProseMirror, tiptap)
        self.sel = (0, 0)


class _Range:
    """ITextRangeProvider over a Python string, character unit = one character, or one whole
    emoji sequence when the text has them (like Chromium's grapheme-based moves)."""

    def __init__(self, t: _Text, s: int, e: int):
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

    def Select(self):  # noqa: N802
        self.t.sel = (self.s, self.e)


class _Selection:
    def __init__(self, r):
        self.r, self.Length = r, 1

    def GetElement(self, i):  # noqa: N802
        return self.r


class _Pattern:
    def __init__(self, t: _Text):
        self.t = t

    @property
    def DocumentRange(self):  # noqa: N802
        return _Range(self.t, 0, len(self.t.text))

    def GetSelection(self):  # noqa: N802
        return _Selection(_Range(self.t, *self.t.sel))


def _field(text: str, caret: int = 0, shifted: bool = False) -> tuple[_Text, TextField]:
    t = _Text(text, shifted)
    t.sel = (caret, caret)
    return t, TextField(None, None, _Pattern(t))


def test_range_at_does_not_trust_a_shifted_search():
    text = "Первая строка.\nВторая строка, где диктовка.\nТретья."
    _, f = _field(text, shifted=True)
    i = text.index("где диктовка")
    r = f.range_at(text, i, i + len("где диктовка"))
    assert (r.s, r.e) == (i, i + len("где диктовка"))
    j = text.index("Третья")
    p = f.range_at(text, j, j)  # an empty range (a point)
    assert (p.s, p.e) == (j, j)


def test_range_at_measures_moves_over_an_emoji():
    # a character move steps over a whole emoji sequence (five code points): offsets still land
    family = "\U0001F468" + chr(0x200D) + "\U0001F469" + chr(0x200D) + "\U0001F467"
    text = "Семья " + family + "\nтут где диктовка конец"
    _, f = _field(text, shifted=True)  # and the provider's search is shifted, so it moves
    i = text.index("где диктовка")
    r = f.range_at(text, i, i + len("где диктовка"))
    assert r is not None and (r.s, r.e) == (i, i + len("где диктовка"))


def test_range_at_uses_an_exact_search_and_repeated_text():
    text = "раз два. раз два. раз два."
    _, f = _field(text)
    k = text.index("раз два", 5)  # the second occurrence
    r = f.range_at(text, k, k + 7)
    assert (r.s, r.e) == (k, k + 7)


def test_anchor_in_an_empty_rich_composer():
    # tiptap/ProseMirror: an empty field reads as "\n" (the empty paragraph's line break)
    t, f = _field("\n", caret=0)
    a = Anchor(f)
    assert (a.head, a.after) == ("", "\n")
    t.text = "Привет! Я проверяю новое приложение"  # typed; the "\n" is gone
    a.text = t.text
    r = a.region()
    assert r is not None and r.GetText(-1) == t.text
    t.sel = (len(t.text), len(t.text))
    assert a.visible()


def test_anchor_context_stays_inside_the_field_and_survives_nbsp():
    # caret in the middle; the space after the cursor is stored as U+00A0 and later turned
    # into a plain space by the editor
    before, after = "Здесь уже был текст до диктовки. ", NBSP + "КОНЕЦ ДОКУМЕНТА."
    t, f = _field(before + after, caret=len(before))
    a = Anchor(f)
    assert a.head == before[-Anchor.CTX:] and a.after == after
    ours = "Привет мир"
    t.text = before + ours + " КОНЕЦ ДОКУМЕНТА."
    a.text = ours
    assert a.region().GetText(-1) == ours
    end = a.end()
    assert (end.s, end.e) == (len(before + ours),) * 2


# --------------------------------------------------------------------------- the Typer itself
class _Element:
    def SetFocus(self):  # noqa: N802
        pass


class _Client:
    def __init__(self, el):
        self.el = el

    def CompareElements(self, a, b):  # noqa: N802
        return a is b

    def GetFocusedElement(self):  # noqa: N802
        return self.el


class _FakeUia:
    """Uia over one fake field that has the focus."""

    def __init__(self, t: _Text):
        self.el = _Element()
        self.client = _Client(self.el)
        self.field = TextField(self, self.el, _Pattern(t))

    def focused_element(self):
        return self.el

    def text_field(self, element=None):
        return self.field


def _typer(monkeypatch, t: _Text):
    """A Typer in UI Automation mode whose keystrokes edit the fake field like an editor:
    typing replaces the selection; an empty paragraph's "\\n" goes away with the first letter."""
    from livetype import config, win
    from livetype.app import Typer

    def type_text(s, *a, **k):
        i, j = t.sel
        if t.text == "\n":  # tiptap/ProseMirror: the empty paragraph's line break
            i, j = 0, 1
        t.text = t.text[:i] + s + t.text[j:]
        t.sel = (i + len(s),) * 2

    monkeypatch.setattr(win, "type_text", type_text)
    monkeypatch.setattr(win, "foreground", lambda: 1)
    monkeypatch.setattr(win, "wait_modifiers_released", lambda timeout=0: True)
    monkeypatch.setattr(win, "window_process", lambda h: "msedge.exe")

    class Ui:
        def notice(self, *a, **k):
            pass

    typer = Typer(config.Config(), 1, None, Ui(), lambda: None)
    typer.uia = _FakeUia(t)
    typer.anchor = Anchor(typer.uia.field)
    typer.mode = "uia"
    return typer


def test_typer_after_the_user_emptied_the_composer(monkeypatch):
    # the Claude app case: an empty composer reads as "\n"; mid-way the message is sent (the
    # composer is emptied); the rest of the dictation goes into the empty composer, is fixed in
    # place, and the user's cursor stays after our words (it used to be put back at the start)
    t = _Text("\n")
    typer = _typer(monkeypatch, t)
    typer._type("Привет мир.")
    assert t.text == "Привет мир."
    t.text, t.sel = "\n", (0, 0)  # sent
    typer._user_moved = True
    typer._type(" Как дела")
    assert t.text == "Как дела" and t.sel == (8, 8) and typer.at_end
    typer._type(" у тебя")
    assert t.text == "Как дела у тебя"
    assert typer._final("Привет, мир. Как дела у тебя?", "") == "merged"
    assert t.text == "Как дела у тебя?" and t.sel == (len(t.text),) * 2


def test_typer_keeps_our_words_together_and_puts_the_cursor_back(monkeypatch):
    t = _Text("НАЧАЛО. Текст до.  КОНЕЦ.")  # the cursor between the two spaces
    t.sel = (len("НАЧАЛО. Текст до. "),) * 2
    typer = _typer(monkeypatch, t)
    typer._type("Привет мир")
    t.text, t.sel = "[правка] " + t.text, (9, 9)  # the user typed at the very start
    typer._user_moved = True
    typer._type(" и всё")
    assert t.text == "[правка] НАЧАЛО. Текст до. Привет мир и всё КОНЕЦ." and t.sel == (9, 9)
    assert typer._final("Привет, мир, и всё.", "") == "fixed"
    assert t.text == "[правка] НАЧАЛО. Текст до. Привет, мир, и всё. КОНЕЦ." and t.sel == (9, 9)
