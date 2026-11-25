"""The typer without a desktop: revisions (UI Automation, Backspace tail, skip), ownership,
newlines, the clipboard policy, and App._session driven by stub engines of both contracts."""

from __future__ import annotations

import json
import queue
import random
import threading
import time

import pytest
from typer_sim import FakeLlama, FakeSource, FakeUi, FakeUia, FakeWatcher, FakeWin, Field, NewEngine, OldEngine

from livetype import app as app_mod
from livetype import config, win
from livetype.app import App, Typer
from livetype.stabilizer import norm
from livetype.textops import graphemes, one_line, plan_tail_rewrite
from livetype.uia import Anchor

FAMILY = "\U0001F468‍\U0001F469‍\U0001F467"
THUMB = "\U0001F44D\U0001F3FD"  # thumbs up + skin tone
FLAG = "\U0001F1F7\U0001F1FA"


def wait_for(cond, timeout: float = 3.0) -> bool:
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# --------------------------------------------------------------------------- planning with graphemes
@pytest.mark.parametrize("old,new", [
    ("Привет мир", "Привет, мир"),
    ("привет мир как дела", "Привет, мир! Как дела?"),
    ("ok " + FAMILY + " конец", "ok " + FAMILY + ", конец"),
    ("семья " + FAMILY, "семья \U0001F468‍\U0001F469‍\U0001F466"),  # a ZWJ family changed inside
    ("класс " + THUMB, "класс \U0001F44D\U0001F3FB"),  # another skin tone
    ("флаг " + FLAG, "флаг \U0001F1FA\U0001F1F8"),
    ("кофе ☕", "кофе ☕️"),  # a variation selector joins the emoji before it
    ("ударение е́", "ударение ѐ"),  # a combining mark changed: the whole letter goes
    ("ёжик", "ежик"),
    ("", "новый текст"),
    ("старый текст", ""),
])
def test_tail_rewrite_deletes_whole_characters(old, new):
    plan = plan_tail_rewrite(old, new)
    assert plan.backspaces == len(graphemes(plan.delete))
    assert old[: plan.keep] == new[: plan.keep] and plan.delete == old[plan.keep:] and plan.insert == new[plan.keep:]
    f = Field("Текст до диктовки: " + old)
    for _ in range(plan.backspaces):
        f.backspace()  # one press = one user-perceived character
    f.type(plan.insert)
    assert f.text == "Текст до диктовки: " + new


def test_tail_rewrite_fuzz_with_emoji_and_combining_marks():
    rnd = random.Random(11)
    alphabet = ["а", "б", "в", " ", ",", ".", "ё", "е́", FAMILY, THUMB, FLAG, "☕️", "ẍ"]
    for _ in range(2000):
        old = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 12)))
        new = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 12)))
        plan = plan_tail_rewrite(old, new)
        f = Field("Пользовательский текст: " + old)
        for _ in range(plan.backspaces):
            f.backspace()
        f.type(plan.insert)
        assert f.text == "Пользовательский текст: " + new, (old, new)


def test_rewrite_is_one_batch_of_backspaces_and_text():
    events = win.rewrite_events(3, "мир " + THUMB)
    downs = [e for e in events if not e.ki.dwFlags & win.KEYEVENTF_KEYUP]
    assert [e.ki.wVk for e in downs[:3]] == [win.VK_BACK] * 3
    assert len(downs) == 3 + len("мир ") + 4  # the emoji: two code points, four UTF-16 units


# --------------------------------------------------------------------------- newlines
def test_line_breaks_are_never_sent_as_enter():
    events = win._text_events("Один.\n\nДва\r\nтри четыре")
    assert all(e.ki.wVk != win.VK_RETURN for e in events)
    text = "".join(chr(e.ki.wScan) for e in events if not e.ki.dwFlags & win.KEYEVENTF_KEYUP)
    assert text == "Один. Два три четыре"
    assert all(e.ki.wVk != win.VK_RETURN for e in win.rewrite_events(2, "а\nб"))
    assert one_line("Абзац один.\n\nАбзац два.") == "Абзац один. Абзац два."


def test_newlines_in_revisions_become_a_space(monkeypatch):
    t, typer, fw = _keys_typer(monkeypatch)
    _do(typer, "add", "первый абзац второй абзац")
    _do(typer, "revise", "Первый абзац.\n\nВторой абзац.")
    assert t.text == "Первый абзац. Второй абзац." and fw.returns == 0


# --------------------------------------------------------------------------- ownership
def test_watcher_counts_only_the_users_input_in_the_target(monkeypatch):
    held: set[int] = set()
    w = win.InputWatcher(target=100, hotkey="ctrl+alt+d", held=lambda vk: vk in held)
    monkeypatch.setattr(win, "foreground", lambda: 100)
    inj = win.LLKHF_INJECTED
    assert not w.key_is_user(0x41, inj, win.MAGIC)  # our own keystroke
    assert w.key_is_user(0x41, inj, 0)  # injected by someone else (on-screen keyboard, tests)
    assert w.key_is_user(0x41, 0, 0)  # a real key
    assert not w.key_is_user(0xA2, 0, 0) and not w.key_is_user(0x5B, 0, 0)  # bare Ctrl, Win
    assert w.key_is_user(0x44, 0, 0)  # a plain D is the user's typing ("в" in the Russian layout)
    held |= {0xA2, 0xA5}  # left Ctrl + right Alt held: now D is the dictation hotkey
    assert not w.key_is_user(0x44, 0, 0)
    held.discard(0xA5)  # Ctrl+D is not our hotkey
    assert w.key_is_user(0x44, 0, 0)
    monkeypatch.setattr(win, "foreground", lambda: 200)
    assert not w.key_is_user(0x41, 0, 0)  # typed into another window
    w.target_pid, w.own = 555, {777}
    monkeypatch.setattr(win, "window_at", lambda x, y: (100, 555))
    assert w.click_is_user(0, 0, 10, 10)  # inside the target
    assert not w.click_is_user(win.LLMHF_INJECTED, win.MAGIC, 10, 10)
    monkeypatch.setattr(win, "window_at", lambda x, y: (300, 556))
    assert not w.click_is_user(0, 0, 10, 10)  # another app's window
    monkeypatch.setattr(win, "window_at", lambda x, y: (301, 555))
    assert w.click_is_user(0, 0, 10, 10)  # a popup of the target app (autocomplete...)
    monkeypatch.setattr(win, "window_at", lambda x, y: (302, 777))
    assert w.click_is_user(0, 0, 10, 10)  # our click-through overlay: the click went on below


def test_hotkey_keys_are_recognised_for_every_kind_of_hotkey():
    def watcher(spec, held=()):
        return win.InputWatcher(target=0, hotkey=spec, held=lambda vk: vk in held)

    assert not watcher("RightAlt").key_is_user(0xA5, 0, 0)  # a modifier on its own: never typing
    assert not watcher("F9").key_is_user(0x78, 0, 0)  # a single-key hotkey is always the hotkey
    assert watcher("F9").key_is_user(0x77, 0, 0)  # F8 is not
    assert not watcher("ctrl+win").key_is_user(0x5C, 0, 0)
    assert not watcher("ctrl+shift+space", held={0xA3, 0xA0}).key_is_user(0x20, 0, 0)
    assert watcher("ctrl+shift+space", held={0xA3}).key_is_user(0x20, 0, 0)  # Ctrl+Space: typing
    assert win.hotkey_parts("nonsense+") == ([], ())
    assert watcher("nonsense+").key_is_user(0x44, 0, 0)  # a bad spec ignores nothing but modifiers


def test_user_input_releases_earlier_text_from_key_rewrites(monkeypatch):
    t, typer, fw = _keys_typer(monkeypatch)
    _do(typer, "add", "привет мир как дела")
    typer.watcher.user()  # e.g. a click in the field
    _do(typer, "add", " у тебя")
    assert t.text == "привет мир как дела у тебя"
    assert fw.sent[-2] == ("vk", win.VK_END, 1, True)  # no UI Automation: go on at the end
    _do(typer, "revise", "Привет, мир! Как дела у тебя")
    assert t.text == "привет мир как дела у тебя" and typer.skipped == 1 and not fw.rewrites()
    # a change inside what was typed after the input is still ours
    _do(typer, "revise", "привет мир как дела у тебя?")
    assert t.text == "привет мир как дела у тебя?" and typer.applied == 1
    assert _finish(typer, "Привет, мир! Как дела у тебя?") == "partial"
    assert t.text == "привет мир как дела у тебя?" and fw.clipboard == "the user's own clipboard"


def test_key_rewrite_stops_at_new_input_right_before_the_batch(monkeypatch):
    t, typer, fw = _keys_typer(monkeypatch)
    _do(typer, "add", "раз два")
    typer.feed("revise", "Раз, два")
    assert typer._ready()
    typer.watcher.user()  # the user presses a key between the check and the batch
    typer._want_revision = False
    assert typer._revise() is False and t.text == "раз два" and not fw.rewrites()


def test_revision_through_ui_automation_merges_into_released_text(monkeypatch):
    t = Field("")
    fw = FakeWin(monkeypatch, t)
    typer = Typer(config.Config(), 1, FakeWatcher(), FakeUi(), lambda: None)
    typer.uia, typer.mode = FakeUia(t), "uia"
    typer.anchor = Anchor(typer.uia.field)
    _do(typer, "add", "давай посмотрим насколько быстро")
    typer.uia_trusted = True
    t.user_edit(len("давай "), "ещё раз ")  # the user inserted words; their caret is after them
    typer.watcher.user()
    _do(typer, "revise", "Давай посмотрим, насколько быстро.")
    assert t.text == "Давай ещё раз посмотрим, насколько быстро."
    assert t.sel == (len("Давай ещё раз "),) * 2  # their cursor is back where it was
    assert not fw.rewrites() and typer.merged_edits


# --------------------------------------------------------------------------- skip and retry
def test_skipped_revision_is_retried_by_the_next_one(monkeypatch):
    t, typer, fw = _keys_typer(monkeypatch)
    _do(typer, "add", "раз два")
    fw.focus = 8  # another control has the focus (nothing typed by the user)
    _do(typer, "revise", "Раз, два")
    assert t.text == "раз два" and typer.skipped == 1 and "focus" in typer.skip_reason
    fw.focus = 7
    _do(typer, "add", " три")  # new words still go on while a correction waits
    assert t.text == "раз два три"
    _do(typer, "revise", "Раз, два, три")  # diffs against the whole target: both fixes at once
    assert t.text == "Раз, два, три" and typer.applied == 1
    assert _finish(typer, "Раз, два, три.") == "done" and t.text == "Раз, два, три."


def test_too_long_tail_is_not_rewritten_with_keys(monkeypatch):
    cfg = config.Config()
    cfg.rewrite_max_chars = 10
    t, typer, fw = _keys_typer(monkeypatch, cfg)
    _do(typer, "add", "очень длинная фраза без запятых")
    _do(typer, "revise", "Очень длинная фраза, без запятых")
    assert typer.skipped == 1 and "too long" in typer.skip_reason
    _do(typer, "revise", "очень длинная фраза без запятых.")  # a short tail is fine
    assert t.text == "очень длинная фраза без запятых."


def test_editor_that_changed_our_text_blocks_the_key_rewrite(monkeypatch):
    # UI Automation can read the field but the locator is not trusted: before Backspace, the text
    # before the caret must be exactly ours ("..." became "…" here, Backspace would eat too much)
    t = Field("")
    fw = FakeWin(monkeypatch, t)
    typer = Typer(config.Config(), 1, FakeWatcher(), FakeUi(), lambda: None)
    typer.uia, typer.mode = FakeUia(t), "uia"
    typer.anchor = Anchor(typer.uia.field)
    _do(typer, "add", "ну и вот...")
    t.text = "ну и вот…"  # the editor's input rule
    t.sel = (len(t.text),) * 2
    typer.anchor.text = "?"  # keep the locator untrusted
    _do(typer, "revise", "Ну и вот...")
    assert typer.skipped == 1 and "does not end with our text" in typer.skip_reason
    assert t.text == "ну и вот…" and not fw.rewrites()


def test_emoji_in_a_key_revision(monkeypatch):
    # an emoji before the change is kept as it is; the Backspaces run over plain characters only
    t, typer, fw = _keys_typer(monkeypatch)
    _do(typer, "add", "Отлично " + THUMB + " идём дальше")
    _do(typer, "revise", "Отлично " + THUMB + ", идём дальше.")
    assert t.text == "Отлично " + THUMB + ", идём дальше."
    assert fw.rewrites()[-1] == ("rewrite", len(" идём дальше"), ", идём дальше.")
    _do(typer, "add", " кофе ☕")  # U+2615 alone: one code point, one press
    _do(typer, "revise", "Отлично " + THUMB + ", идём дальше. Кофе ☕️")
    assert t.text == "Отлично " + THUMB + ", идём дальше. Кофе ☕️"


@pytest.mark.parametrize("cluster", [THUMB, FAMILY, FLAG, "е́"])
def test_key_rewrite_never_runs_over_a_cluster_of_several_code_points(monkeypatch, cluster):
    # editors disagree on how many presses delete such a cluster (Chromium deletes a combining mark
    # without its letter): Backspace is not used over it; a trusted UI Automation locator is
    t, typer, fw = _keys_typer(monkeypatch)
    _do(typer, "add", "отлично " + cluster + " идём")
    _do(typer, "revise", "Отлично " + cluster + " идём")
    assert typer.skipped == 1 and "depends on the editor" in typer.skip_reason and not fw.rewrites()
    assert t.text == "отлично " + cluster + " идём"
    assert _finish(typer, "Отлично " + cluster + " идём") == "partial"

    t2 = Field("")
    fw2 = FakeWin(monkeypatch, t2)
    typer2 = Typer(config.Config(), 1, FakeWatcher(), FakeUi(), lambda: None)
    typer2.uia, typer2.mode, typer2._focus = FakeUia(t2), "uia", fw2.focus
    typer2.anchor = Anchor(typer2.uia.field)
    _do(typer2, "add", "отлично " + cluster + " идём")
    _do(typer2, "revise", "Отлично " + cluster + " идём")
    assert t2.text == "Отлично " + cluster + " идём" and not fw2.rewrites()
    assert _finish(typer2, "Отлично " + cluster + " идём") == "done"


# --------------------------------------------------------------------------- clipboard
def test_clipboard_only_for_words_that_were_never_typed(monkeypatch):
    t, typer, fw = _keys_typer(monkeypatch)
    _do(typer, "add", "раз два")
    fw.fg = 2  # another window in front for the rest of the dictation
    _do(typer, "add", " три четыре")
    assert t.text == "раз два"
    typer.feed("finish", "Раз, два, три, четыре.")
    typer.step()
    assert typer.conclude() == "undelivered"
    assert fw.clipboard == " три, четыре." and t.text == "раз два"


def test_partial_and_done_leave_the_clipboard_alone(monkeypatch):
    t, typer, fw = _keys_typer(monkeypatch)
    _do(typer, "add", "раз два")
    typer.watcher.user()
    _do(typer, "add", " три")
    assert _finish(typer, "Раз, два, три.") == "partial" and fw.clipboard == "the user's own clipboard"
    t2, typer2, fw2 = _keys_typer(monkeypatch)
    _do(typer2, "add", "раз два")
    assert _finish(typer2, "Раз, два.") == "done" and fw2.clipboard == "the user's own clipboard"


# --------------------------------------------------------------------------- ownership, focus, late UI Automation
def test_focus_moved_to_another_control_releases_our_text(monkeypatch):
    t, typer, fw = _keys_typer(monkeypatch)
    _do(typer, "add", "привет мир")
    fw.focus = 8  # the app moved the focus to another control (no key or click from the user)
    _do(typer, "add", " как дела")
    _do(typer, "revise", "Привет, мир! Как дела")
    assert typer.skipped == 1 and "no longer ours" in typer.skip_reason and not fw.rewrites()
    _do(typer, "revise", "привет мир как дела?")  # a change in what went to the new control is fine
    assert t.text == "привет мир как дела?" and fw.rewrites() == [("rewrite", 0, "?")]


def test_ui_automation_field_found_later_is_anchored_and_trusted(monkeypatch):
    # no text field when the dictation started (Chromium builds its accessibility tree late):
    # plain keys first; once the field holds exactly our words, they are anchored there
    t = Field("Заметка: ")
    fw = FakeWin(monkeypatch, t)
    typer = Typer(config.Config(), 1, FakeWatcher(), FakeUi(), lambda: None)
    typer.uia, typer._focus = FakeUia(t), fw.focus  # UI Automation works, no anchor yet
    _do(typer, "add", "встреча в пятницу")
    assert typer.mode == "keys"
    _idle_probe(typer)
    assert typer.mode == "uia" and typer.uia_trusted and typer.anchor.head == "Заметка: "
    t.sel = (0, 0)  # the user clicks at the start of the note
    typer.watcher.user()
    _do(typer, "add", " в десять")  # after our words, not at the cursor; the cursor stays
    assert t.text == "Заметка: встреча в пятницу в десять" and t.sel == (0, 0)
    assert _finish(typer, "Встреча в пятницу, в десять.") == "done" and typer.detail == "fixed"
    assert t.text == "Заметка: Встреча в пятницу, в десять." and t.sel == (0, 0) and not fw.rewrites()


def test_late_field_is_not_anchored_when_our_words_are_there_twice(monkeypatch):
    t = Field("раз два. ")
    fw = FakeWin(monkeypatch, t)
    typer = Typer(config.Config(), 1, FakeWatcher(), FakeUi(), lambda: None)
    typer.uia, typer._focus = FakeUia(t), fw.focus
    _do(typer, "add", "раз два.")
    _idle_probe(typer)
    assert typer.mode == "keys" and typer.anchor is None  # ambiguous: keys stay


def test_final_text_that_lost_most_words_keeps_the_live_text(monkeypatch):
    t, typer, fw = _keys_typer(monkeypatch)
    _do(typer, "add", "раз два три четыре пять шесть семь")
    assert _finish(typer, "Раз.") == "done" and typer.detail == "kept_live"
    assert t.text == "раз два три четыре пять шесть семь" and not fw.rewrites()


def test_uia_revision_never_drops_new_words_after_text_the_user_added(monkeypatch):
    # dictating in the middle of a text; the user types right after our words; the chunk's final
    # text arrives with words not typed yet. Merged together with the user's insertion they used
    # to be dropped while the typer counted them as typed.
    t = Field("До.  После.", caret=4)
    typer, fw = _uia_typer(monkeypatch, t)
    _do(typer, "add", "привет мир")
    _idle_probe(typer)
    assert t.text == "До. привет мир После." and typer.uia_trusted
    t.user_edit(t.text.index(" После"), " ой")
    typer.watcher.user()
    typer.feed("add", " как дела")  # not typed yet...
    _do(typer, "revise", "Привет, мир! Как дела")  # ...when the revision comes
    assert t.text == "До. Привет, мир ой Как дела После."
    assert _finish(typer, "Привет, мир! Как дела") == "done" and typer.detail == "merged"


def test_locator_is_trusted_when_our_words_are_there_with_the_users_next_to_them(monkeypatch):
    # the user typed right after our words before the locator was ever probed: our exact words
    # are inside the located text, so the locator is trusted; their word is kept by the merge
    t = Field("Текст до.  После.", caret=10)
    typer, fw = _uia_typer(monkeypatch, t)
    _do(typer, "add", "привет как дела")
    t.user_edit(t.text.index(" После"), " Ж")
    typer.watcher.user()
    assert _finish(typer, "Привет, как дела?") == "done"
    assert t.text == "Текст до. Привет, как дела Ж После."  # "?" belonged before their word: not added


def test_first_words_never_glue_to_text_the_user_typed_just_before(monkeypatch):
    t = Field("")
    typer, fw = _uia_typer(monkeypatch, t)
    typer.feed("add", "встреча")  # recognized, not typed yet...
    t.user_edit(0, "Итак")  # ...when the user types into the empty field
    typer.watcher.user()
    typer.step()
    assert t.text == "Итак встреча"
    assert _finish(typer, "Встреча.") == "done" and t.text == "Итак Встреча."


def test_uia_revision_never_retypes_a_line_break_the_user_made(monkeypatch):
    t = Field("")
    typer, fw = _uia_typer(monkeypatch, t)
    _do(typer, "add", "раз два три")
    _idle_probe(typer)
    t.user_edit(3, "\n", delete=1)  # Shift+Enter in place of the space after "раз"
    typer.watcher.user()
    _do(typer, "revise", "Раз, два, три.")
    assert t.text == "раз\nдва три" and typer.skipped == 1 and "line break" in typer.skip_reason
    assert fw.returns == 0 and not fw.rewrites()


def test_undelivered_toast_says_why(monkeypatch):
    ui = FakeUi()
    a = App.__new__(App)
    a.ui = ui
    a._report("undelivered", "", 0, "field")
    a._report("undelivered", "", 0, "window")
    subs = [e[2] for e in ui.events if e[0] == "notice"]
    assert subs[0].startswith("курсор был не в поле диктовки") and subs[1].startswith("окно было неактивно")
    assert all("полный текст — в истории" in s for s in subs)


# --------------------------------------------------------------------------- random dictations
WORDS = ["привет", "мир", "как", "дела", "у", "тебя", "сегодня", "завтра", "встреча", "в", "десять",
         "проект", "ёлка", "ok", "\U0001F44D"]


def _chunk_final(rnd, words):
    out = list(words)
    if rnd.random() < 0.4:
        out[rnd.randrange(len(out))] = rnd.choice(WORDS)  # a word recognized differently
    if rnd.random() < 0.5 and len(out) > 2:
        out[rnd.randrange(len(out) - 1)] += ","
    out[0] = out[0][:1].upper() + out[0][1:]
    return " ".join(out) + rnd.choice([".", "?", "!", "."])


def _words(text: str) -> list[str]:
    return [w for w in (norm(x) for x in text.split()) if w]


@pytest.mark.parametrize("seed", range(160))
def test_random_dictations_converge_to_the_final_text(monkeypatch, seed):
    """Live words, chunk revisions at random moments (the 0.3 engine contract), the window going
    away and back, clicks and edits in the user's own text, and the user typing right after our
    words. Undisturbed, the field must end as the user's text + the final text. With the user's
    typing: their text survives untouched; in UI Automation mode every word of the final text is
    there too (punctuation next to their words may stay as dictated); with plain keys the
    corrections before their input may be skipped (partial). Never Enter, never the clipboard."""
    rnd = random.Random(seed)
    use_uia = seed % 2 == 1
    chunks = [[rnd.choice(WORDS) for _ in range(rnd.randint(1, 6))] for _ in range(rnd.randint(2, 6))]
    finals = [_chunk_final(rnd, c) for c in chunks]
    seps = [rnd.choice([" ", " ", "\n\n"]) for _ in finals]  # paragraphs from the engine: never Enter
    prefix = rnd.choice(["", "Текст до. ", "Заметка: "])
    suffix = rnd.choice(["", " После."]) if use_uia else ""  # dictating in the middle of a text
    t = Field(prefix + suffix, caret=len(prefix))
    cfg = config.Config()
    cfg.rewrite_max_chars = 10_000  # this test is about convergence; the limit has its own test
    if use_uia:
        typer, fw = _uia_typer(monkeypatch, t, cfg)
    else:
        fw = FakeWin(monkeypatch, t)
        typer = Typer(cfg, 1, FakeWatcher(), FakeUi(), lambda: None)
        typer._focus = fw.focus
    committed: list[list[str]] = [[] for _ in chunks]
    finished = 0
    user_prefix = prefix
    marks = 0  # " Ж" typed by the user right after our words

    def target(n_final: int) -> str:
        s = ""
        for i in range(n_final):
            s += (seps[i] if i else "") + finals[i]
        rest = [w for c in committed[n_final:] for w in c]
        if rest:
            s += (" " if s else "") + " ".join(rest)
        return s

    def steps():
        for _ in range(rnd.randint(0, 2)):
            if rnd.random() < 0.3:
                typer._last_typed -= 1  # time passes: the typer is idle (the locator probe runs)
            typer.step()

    def maybe_disturb():
        nonlocal user_prefix, marks
        r = rnd.random()
        if r < 0.08:
            fw.fg = 2  # another window in front for a moment
            steps()
            fw.fg = 1
        elif use_uia and r < 0.14:
            k = rnd.choice([rnd.randint(0, len(user_prefix)), rnd.randint(len(t.text) - len(suffix), len(t.text))])
            t.sel = (k, k)  # a click in the user's own text, before or after ours
            typer.watcher.user()
        elif use_uia and user_prefix and r < 0.19:
            t.user_edit(0, "Х", caret=rnd.randint(0, len(user_prefix) + 1))  # an edit before ours
            user_prefix = "Х" + user_prefix
            typer.watcher.user()
        elif r < 0.23 and any(committed):
            t.user_edit(len(t.text) - len(suffix), " Ж")  # the user types right after our words
            marks += 1
            typer.watcher.user()

    for ci, chunk in enumerate(chunks):
        for w in chunk:
            any_before = any(committed)
            committed[ci].append(w)
            typer.feed("add", (" " if any_before else "") + w)
            steps()
            maybe_disturb()
            if finished < ci and rnd.random() < 0.5:  # an earlier chunk gets its final text
                finished += 1
                typer.feed("revise", target(finished))
                steps()
    final = target(len(chunks))
    typer.feed("finish", final)
    for _ in range(20):
        typer.step()
        if typer.settled():
            break
    outcome = typer.conclude()
    assert fw.returns == 0 and fw.clipboard == "the user's own clipboard"
    assert t.text.startswith(user_prefix) and t.text.endswith(suffix) and t.text.count("Ж") == marks
    if not marks:
        assert outcome == "done", (typer.skip_reason, t.text)
        assert t.text == user_prefix + one_line(final) + suffix
    elif use_uia:
        assert outcome == "done", (typer.skip_reason, t.text)
        assert _words(t.text.replace("Ж", "")) == _words(user_prefix + final + suffix), t.text
    else:
        assert outcome in ("done", "partial")


# --------------------------------------------------------------------------- App._session with stub engines
@pytest.fixture
def session(monkeypatch, tmp_path):
    """Build an App (no models) whose session types into a simulated field."""

    def make(engine_cls, script, final, *, use_uia=False, field=None, cfg=None, blocks=40):
        cfg = cfg or config.Config()
        cfg.use_uia = use_uia
        cfg.undelivered_wait_s = 1.0
        f = field if field is not None else Field("")
        fw = FakeWin(monkeypatch, f)
        engines, watchers = [], []

        class Engine(engine_cls):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                engines.append(self)

        Engine.script, Engine.final = list(script), final

        def make_watcher(*a, **k):
            w = FakeWatcher(*a, **k)
            watchers.append(w)
            return w

        monkeypatch.setattr(app_mod, "DictationEngine", Engine)
        monkeypatch.setattr(win, "InputWatcher", make_watcher)
        monkeypatch.setattr(app_mod, "HISTORY_PATH", tmp_path / "history.jsonl")
        if use_uia:
            def init_uia(self):
                self.uia = FakeUia(f)
                self.anchor = Anchor(self.uia.field)
                self.mode = "uia"

            monkeypatch.setattr(Typer, "_init_uia", init_uia)
        a = App.__new__(App)
        a.cfg, a.ui = cfg, FakeUi()
        a.source_factory = lambda: FakeSource(blocks=blocks)
        a.auto, a.once, a.debug = True, False, False
        a.cmds, a.phase, a.last = queue.Queue(), "idle", None
        a.text_processor, a.polish_job = None, None
        a.on_text_settings_changed, a.arm_cancel = (lambda: None), (lambda on: None)
        a.live = a.final = a.vad = None
        a.hotkey_vk, a.hold_down = 0, False
        a.sounds = app_mod.Sounds(False)
        return a, f, fw, engines, watchers

    return make


def _history(tmp_path) -> list[dict]:
    p = tmp_path / "history.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


@pytest.mark.parametrize("use_uia", [False, True])
def test_old_engine_final_text_is_applied_at_stop(session, tmp_path, use_uia):
    final = "Привет, мир! Как дела у тебя?"
    script = [("commit", "привет мир"), None, None, ("commit", " как дела"), None, ("commit", " у тебя")]
    app, f, fw, engines, _ = session(OldEngine, script, final, use_uia=use_uia)
    app._session()
    assert f.text == final
    assert app.last.outcome == "done" and app.last.detail == "fixed"
    assert app.ui.titles("done") == ["Текст проверен"]
    assert fw.clipboard == "the user's own clipboard" and fw.returns == 0
    assert bool(fw.rewrites()) is not use_uia  # Backspace only without UI Automation
    h = _history(tmp_path)[-1]
    assert (h["live"], h["final"], h["outcome"], h["text"]) == ("привет мир как дела у тебя", final, "done", final)
    assert engines[0].closed


@pytest.mark.parametrize("use_uia", [False, True])
def test_new_engine_revisions_land_at_pauses_and_at_stop(session, use_uia):
    holder = {}

    def field_is(text):
        def check():
            assert wait_for(lambda: holder["f"].text == text), holder["f"].text
        return check

    script = [
        ("commit", "привет мир"), None, ("commit", " как дела"),
        ("revise", "Привет, мир! Как дела"), field_is("Привет, мир! Как дела"),  # a chunk's final text, at a pause
        ("commit", " у тебя"), field_is("Привет, мир! Как дела у тебя"),
        ("commit", " всё хорошо"),
    ]
    final = "Привет, мир! Как дела у тебя? Всё хорошо."
    app, f, fw, engines, _ = session(NewEngine, script, final, use_uia=use_uia)
    holder["f"] = f
    app._session()
    assert f.text == final and app.last.outcome == "done"
    assert app.last.revisions_applied >= 2
    assert engines[0].finish_args == (0.8,)  # finish(budget) on the 0.3 engine
    assert fw.clipboard == "the user's own clipboard" and fw.returns == 0
    assert bool(fw.rewrites()) is not use_uia  # trusted UI Automation first, Backspace otherwise


def test_new_engine_with_a_user_click_keeps_the_live_text_and_reports_partial(session, tmp_path):
    holder = {}

    def click():
        holder["w"][0].user()

    script = [("commit", "привет мир"), ("commit", " как дела"), None, click, None, ("commit", " у тебя"),
              ("revise", "Привет, мир! Как дела у тебя")]
    app, f, fw, engines, watchers = session(NewEngine, script, "Привет, мир! Как дела у тебя?")
    holder["w"] = watchers
    app._session()
    assert f.text == "привет мир как дела у тебя"  # nothing rewritten across the user's input
    assert app.last.outcome == "partial" and fw.clipboard == "the user's own clipboard"
    assert app.ui.titles("notice")[-1] == "Исправлено не всё"
    assert _history(tmp_path)[-1]["text"] == "Привет, мир! Как дела у тебя?"  # the full text is there


def test_window_never_comes_back_puts_only_the_rest_in_the_clipboard(session, tmp_path):
    holder = {}
    script = [("commit", "раз два"), lambda: setattr(holder["fw"], "fg", 2), ("commit", " три четыре")]
    app, f, fw, engines, _ = session(OldEngine, script, "Раз, два, три, четыре.")
    holder["fw"] = fw
    app._session()
    assert app.last.outcome == "undelivered" and f.text == "раз два"
    assert fw.clipboard == " три, четыре."
    assert app.ui.titles("notice")[-1] == "Текст — в буфере обмена (Ctrl+V)"
    assert _history(tmp_path)[-1]["outcome"] == "undelivered"


def test_typing_once_at_the_end_when_live_typing_is_off(session):
    cfg = config.Config()
    cfg.live_typing = False
    app, f, fw, _, _ = session(OldEngine, [("commit", "раз два")], "Раз, два.", cfg=cfg)
    app._session()
    assert f.text == "Раз, два." and app.last.outcome == "done"


def test_cancel_types_nothing_more(session):
    app, f, fw, _, _ = session(OldEngine, [("commit", "раз два"), lambda: app.cmds.put("cancel")], "Раз, два.")
    app.auto = False
    app._session()
    assert app.last is None and ("hide",) in app.ui.events


def test_done_is_reported_right_after_the_final_apply(session):
    script = [("commit", "раз два три")]
    app, f, fw, _, watchers = session(OldEngine, script, "Раз, два, три.")
    t0 = time.monotonic()
    app._session()
    assert app.last.stop_to_done_s < 0.5 and app.ui.done_at - t0 < 3.0
    # the watcher knows the dictation's window and the hotkey (its main key alone is typing)
    assert watchers[0].target == 1 and watchers[0].hotkey == app.cfg.hotkey


class _KeywordBudgetEngine(NewEngine):
    def finish(self, *, budget_s: float = 0.8) -> str:  # a keyword-only budget
        return super().finish(budget_s)


def test_new_engine_with_a_keyword_only_budget(session):
    app, f, fw, engines, _ = session(_KeywordBudgetEngine, [("commit", "раз два")], "Раз, два.")
    app.cfg.stop_budget_s = 0.5
    app._session()
    assert engines[0].finish_args == (0.5,) and f.text == "Раз, два." and app.last.outcome == "done"


def test_final_text_that_lost_words_is_not_applied_and_history_keeps_the_live_text(session, tmp_path):
    live = "раз два три четыре пять шесть семь"
    app, f, fw, _, _ = session(OldEngine, [("commit", live)], "Раз.")
    app._session()
    assert f.text == live and app.last.outcome == "done" and app.last.detail == "kept_live"
    assert app.ui.titles("done") == ["Готово"]
    h = _history(tmp_path)[-1]
    assert (h["text"], h["final"], h["outcome"]) == (live, "Раз.", "done")


# --------------------------------------------------------------------------- the language model after done
class _SlowProcessor:
    """A TextProcessor stand-in: answers after `delay` (or gives up when cancelled)."""

    def __init__(self, answer: str | None, delay: float = 0.0):
        self.answer, self.delay = answer, delay
        self.started: list[str] = []
        self.calls: list[float] = []
        self.idle_stop_s = 300.0
        self.cancelled = threading.Event()

    def ready(self, model):
        return True

    def start_async(self, model):
        self.started.append(model)

    def process(self, text, rules, model=None, timeout=30.0, cancel=None):
        self.calls.append(timeout)
        t_end = time.monotonic() + self.delay
        while time.monotonic() < t_end:
            if cancel is not None and cancel.is_set():
                self.cancelled.set()
                return None
            time.sleep(0.01)
        self.returned_at = time.monotonic()
        return self.answer


def _with_llm(app, answer, delay=0.0):
    app.cfg.text_processing = True
    app.text_processor = _SlowProcessor(answer, delay)
    return app.text_processor


def test_language_model_runs_inside_the_stop_step(session, tmp_path):
    app, f, fw, _, _ = session(OldEngine, [("commit", "ну короче завтра в десять")], "Ну, короче, завтра в десять.")
    tp = _with_llm(app, "Завтра в десять.", delay=0.8)
    seen = {}
    threading.Timer(0.5, lambda: seen.setdefault("during", f.text)).start()
    app._session()
    assert seen["during"] == "Ну, короче, завтра в десять."  # the checked text is in place while the model works
    assert f.text == "Завтра в десять."  # then the processed text replaces it
    assert tp.started == ["light"]  # the server was started at the hotkey, in the background
    assert tp.calls == [pytest.approx(2.0 + 0.1 * 5)]  # light: 2 s + 0.1 s per word
    assert app.ui.done_at >= tp.returned_at  # "done" comes after the processed text
    h = _history(tmp_path)[-1]
    assert (h["text"], h["final"], h["processed"], h["processed_applied"]) == (
        "Завтра в десять.", "Ну, короче, завтра в десять.", "Завтра в десять.", True)
    assert not app.ui.titles("notice")


def test_a_hotkey_press_stops_the_language_model_and_starts_the_next_dictation(session, tmp_path):
    app, f, fw, _, _ = session(OldEngine, [("commit", "раз два")], "Раз, два.")
    tp = _with_llm(app, "Раз-два.", delay=10)
    def press_while_processing():
        while not tp.calls:
            time.sleep(0.01)
        time.sleep(0.2)
        app.cmds.put("toggle")

    threading.Thread(target=press_while_processing, daemon=True).start()
    app._session()
    assert tp.cancelled.is_set() and app.last.stop_to_done_s < 2.5
    assert f.text == "Раз, два."  # the checked text stays
    assert app._restart  # the press starts the next dictation
    h = _history(tmp_path)[-1]
    assert h["processed"] is None and "processed_applied" not in h


def test_language_model_end_to_end_with_a_fake_llama_server(session, tmp_path):
    # the real TextProcessor over HTTP: the server is started at the hotkey (in the background),
    # the answer is one line and replaces the text before "done"
    srv = FakeLlama(answer="Завтра в десять.\n\nВ офисе.", delay=0.6)
    final = "Ну, короче, завтра в десять в офисе."
    app, f, fw, _, _ = session(OldEngine, [("commit", "ну короче завтра в десять в офисе")], final)
    app.cfg.text_processing = True
    app.text_processor = tp = srv.processor()
    try:
        app._session()
        assert f.text == "Завтра в десять. В офисе"
        assert srv.launched == ["light"]
        assert len(srv.requests) == 1 and fw.returns == 0
        h = _history(tmp_path)[-1]
        assert (h["final"], h["processed"], h["processed_applied"]) == (final, "Завтра в десять. В офисе", True)
    finally:
        tp.stop()
        srv.close()


def test_language_model_deadline_with_a_fake_llama_server(session, tmp_path):
    srv = FakeLlama(answer="Раз, два, три.", delay=6.0)  # slower than the light model's 2.3 s
    app, f, fw, _, _ = session(OldEngine, [("commit", "раз два три")], "Раз, два, три.")
    app.cfg.text_processing = True
    app.text_processor = tp = srv.processor()
    try:
        t0 = time.monotonic()
        app._session()
        assert time.monotonic() - t0 < 4.5  # given up at 2 s + 0.1 s per word
        assert f.text == "Раз, два, три."
        assert _history(tmp_path)[-1]["processed"] is None
    finally:
        tp.stop()
        srv.close()


def test_rejected_language_model_answer_changes_nothing(session, tmp_path):
    app, f, fw, _, _ = session(OldEngine, [("commit", "раз два")], "Раз, два.")
    _with_llm(app, None, delay=0.05)  # the guards rejected it (or it came too late)
    app._session()
    assert f.text == "Раз, два."
    assert _history(tmp_path)[-1]["processed"] is None


# --------------------------------------------------------------------------- helpers
def _keys_typer(monkeypatch, cfg=None):
    t = Field("")
    fw = FakeWin(monkeypatch, t)
    typer = Typer(cfg or config.Config(), 1, FakeWatcher(), FakeUi(), lambda: None)
    typer._focus = fw.focus
    return t, typer, fw


def _uia_typer(monkeypatch, t, cfg=None):
    fw = FakeWin(monkeypatch, t)
    typer = Typer(cfg or config.Config(), 1, FakeWatcher(), FakeUi(), lambda: None)
    typer.uia, typer.mode, typer._focus = FakeUia(t), "uia", fw.focus
    typer.anchor = Anchor(typer.uia.field)
    return typer, fw


def _idle_probe(typer):
    typer._last_typed -= 1  # 0.3 s after the last words
    typer.step()


def _do(typer, kind, arg=None):
    typer.feed(kind, arg)
    typer.step()


def _finish(typer, final):
    typer.feed("finish", final)
    typer.step()
    return typer.conclude()
