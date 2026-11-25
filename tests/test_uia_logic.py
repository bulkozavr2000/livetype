"""UI Automation logic against a fake text provider (no desktop): placing text offsets as ranges
when the provider's own search is wrong, finding our text again in rich-editor fields, and the
Typer's UI Automation path (typing after our text, revisions merged with the user's edits)."""

from typer_sim import FakeUia, FakeWatcher, FakeWin, Field, text_field

from livetype import config
from livetype.app import Typer
from livetype.uia import Anchor

NBSP = "\xa0"


def test_range_at_does_not_trust_a_shifted_search():
    text = "Первая строка.\nВторая строка, где диктовка.\nТретья."
    _, f = text_field(text, shifted=True)
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
    _, f = text_field(text, shifted=True)  # and the provider's search is shifted, so it moves
    i = text.index("где диктовка")
    r = f.range_at(text, i, i + len("где диктовка"))
    assert r is not None and (r.s, r.e) == (i, i + len("где диктовка"))


def test_range_at_uses_an_exact_search_and_repeated_text():
    text = "раз два. раз два. раз два."
    _, f = text_field(text)
    k = text.index("раз два", 5)  # the second occurrence
    r = f.range_at(text, k, k + 7)
    assert (r.s, r.e) == (k, k + 7)


def test_anchor_in_an_empty_rich_composer():
    # tiptap/ProseMirror: an empty field reads as "\n" (the empty paragraph's line break)
    t, f = text_field("\n", caret=0)
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
    t, f = text_field(before + after, caret=len(before))
    a = Anchor(f)
    assert a.head == before[-Anchor.CTX:] and a.after == after
    ours = "Привет мир"
    t.text = before + ours + " КОНЕЦ ДОКУМЕНТА."
    a.text = ours
    assert a.region().GetText(-1) == ours
    end = a.end()
    assert (end.s, end.e) == (len(before + ours),) * 2


def test_select_accepts_the_same_place_reported_differently(monkeypatch):
    # Chromium may report a selection made through UI Automation at an equivalent position that
    # does not compare equal (the end of one text node vs. the start of the next): offsets decide
    t, f = text_field("Первая. Вторая.", caret=0)
    sel_cls, rng_cls = type(f.tp.GetSelection()), type(f.document())

    class Odd(rng_cls):
        def CompareEndpoints(self, ep, other, other_ep):  # noqa: N802
            return 1  # never "equal"

    monkeypatch.setattr(f.tp, "GetSelection", lambda: sel_cls(Odd(t, *t.sel)))
    k = t.text.index("Вторая")
    assert f.select(rng_cls(t, k, k)) and t.sel == (k, k)
    # a provider that ignores the request is still caught: no false success
    monkeypatch.setattr(rng_cls, "Select", lambda self: None)
    assert not f.select(rng_cls(t, 3, 3), timeout=0.05)


def test_anchor_around_text_found_later():
    raw = "Начало заметки. встреча в пятницу Конец."
    t, f = text_field(raw)
    k = raw.index("встреча")
    a = Anchor.around(f, raw, k, k + len("встреча в пятницу"))
    assert (a.head, a.text, a.after) == ("Начало заметки. ", "встреча в пятницу", " Конец.")
    t.text = "Правка. " + raw  # the user edits before it: still found by its context
    assert a.region().GetText(-1) == "встреча в пятницу"


# --------------------------------------------------------------------------- the Typer itself
def _typer(monkeypatch, t: Field, cfg=None):
    """A Typer in UI Automation mode over the fake field; its keystrokes edit the field like an
    editor (typing replaces the selection; an empty paragraph's "\\n" goes with the first letter)."""
    fw = FakeWin(monkeypatch, t)
    typer = Typer(cfg or config.Config(), 1, FakeWatcher(), _Ui(), lambda: None)
    typer.uia = FakeUia(t)
    typer.anchor = Anchor(typer.uia.field)
    typer.mode = "uia"
    typer._focus = fw.focus
    return typer, fw


class _Ui:
    def __init__(self):
        self.notices = []

    def notice(self, title, *a, **k):
        self.notices.append(title)


def _do(typer, kind, arg=None):
    typer.feed(kind, arg)
    typer.step()


def _finish(typer, final):
    typer.feed("finish", final)
    typer.step()
    return typer.conclude()


def test_typer_after_the_user_emptied_the_composer(monkeypatch):
    # the Claude app case: an empty composer reads as "\n"; mid-way the message is sent (the
    # composer is emptied); the rest of the dictation goes into the empty composer, is fixed in
    # place, and the user's cursor stays after our words (it used to be put back at the start)
    t = Field("\n", 0)
    typer, fw = _typer(monkeypatch, t)
    _do(typer, "add", "Привет мир.")
    assert t.text == "Привет мир."
    t.text, t.sel = "\n", (0, 0)  # sent
    typer.watcher.user()
    _do(typer, "add", " Как дела")
    assert t.text == "Как дела" and t.sel == (8, 8) and typer.at_end
    _do(typer, "add", " у тебя")
    assert t.text == "Как дела у тебя"
    # the user's deletion (the sent message) is kept like any edit of theirs; the rest is fixed
    assert _finish(typer, "Привет, мир. Как дела у тебя?") == "done" and typer.detail == "merged"
    assert t.text == "Как дела у тебя?" and t.sel == (len(t.text),) * 2
    assert fw.clipboard == "the user's own clipboard"


def test_typer_keeps_our_words_together_and_puts_the_cursor_back(monkeypatch):
    t = Field("НАЧАЛО. Текст до.  КОНЕЦ.")  # the cursor between the two spaces
    t.sel = (len("НАЧАЛО. Текст до. "),) * 2
    typer, fw = _typer(monkeypatch, t)
    _do(typer, "add", "Привет мир")
    t.user_edit(0, "[правка] ")  # the user typed at the very start
    typer.watcher.user()
    _do(typer, "add", " и всё")
    assert t.text == "[правка] НАЧАЛО. Текст до. Привет мир и всё КОНЕЦ." and t.sel == (9, 9)
    assert _finish(typer, "Привет, мир, и всё.") == "done" and typer.detail == "fixed"
    assert t.text == "[правка] НАЧАЛО. Текст до. Привет, мир, и всё. КОНЕЦ." and t.sel == (9, 9)
    assert not fw.rewrites()  # done through UI Automation, not Backspace


def test_revision_while_dictating_keeps_a_word_the_user_changed(monkeypatch):
    # a chunk's final text arrives at a pause; meanwhile the user fixed a word in our text by hand
    t = Field("")
    typer, fw = _typer(monkeypatch, t)
    _do(typer, "add", "встреча в пятницу в десять")
    typer._last_typed -= 1
    typer.step()  # idle: the probe finds exactly our words, the locator is trusted
    assert typer.uia_trusted
    k = t.text.index("пятницу")
    t.user_edit(k, "субботу", delete=len("пятницу"))
    typer.watcher.user()
    _do(typer, "revise", "Встреча в пятницу, в десять.")
    assert t.text == "Встреча в субботу в десять."
    assert typer.base == "Встреча в пятницу, в десять."  # our model keeps our text, not the edit
    # a later revision must not undo the user's word either
    _do(typer, "add", " Принесу отчёт")
    _do(typer, "revise", "Встреча в пятницу, в десять. Принесу отчёт.")
    assert t.text == "Встреча в субботу в десять. Принесу отчёт."
    assert _finish(typer, "Встреча в пятницу, в десять. Принесу отчёт.") == "done" and typer.detail == "merged"


def test_untrusted_locator_falls_back_to_keys_for_our_own_tail(monkeypatch):
    # the locator never found exactly our text (e.g. an editor that rewrites what we type): a
    # difference is not taken for the user's edit; our own tail is corrected with Backspace
    t = Field("")
    typer, fw = _typer(monkeypatch, t)
    _do(typer, "add", "привет мир")
    typer.anchor.text = "что-то другое"  # what the locator would compare with
    _do(typer, "revise", "привет, мир")
    assert t.text == "привет, мир" and fw.rewrites() == [("rewrite", 4, ", мир")]


def test_focus_outside_the_field_holds_words_then_types_them(monkeypatch):
    # our text cannot be found and the focus is on another element: the words wait (no "lost"
    # mode, nothing typed elsewhere) and are typed once the cursor is back in the field
    t = Field("До.  После.", caret=4)
    typer, fw = _typer(monkeypatch, t)
    _do(typer, "add", "раз два")
    assert t.text == "До. раз два После."
    t.text, t.sel = "", (0, 0)  # the user deleted everything, our words and the text around them
    typer.uia.focused = object()  # ...and clicked a button
    typer.watcher.user()
    _do(typer, "add", " три")
    assert t.text == "" and typer.covered == len("раз два") and typer._held_note
    typer.step()  # still waiting
    assert t.text == ""
    typer.uia.focused = typer.uia.el  # back in the field
    typer.step()
    assert t.text == "три" and typer.mode == "uia" and typer.base[typer.seg_start:] == "три"
    assert _finish(typer, "Раз, два, три.") == "partial"  # the deleted part's fix had nowhere to go
    assert t.text == "три." and fw.clipboard == "the user's own clipboard"
