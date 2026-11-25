"""Unit tests for the pure logic (no models, no Windows input)."""

import random

from livetype.stabilizer import Stabilizer, words_from_tokens
from livetype.textops import (
    SimEditor,
    common_affixes,
    fold,
    join_chunks,
    join_delta,
    locate_region,
    merge_user_edits,
    plan_replacement,
    split_final,
    wer,
)

NBSP, OBJ = "\xa0", chr(0xFFFC)


def test_merge_without_user_edits_takes_final():
    base = "я думаю что это хорошо"
    assert merge_user_edits(base, base, "Я думаю, что это хорошо.") == "Я думаю, что это хорошо."


def test_merge_keeps_word_the_user_changed():
    base = "встреча в пятницу в десять"
    user = "встреча в субботу в десять"  # the user fixed a word by hand
    final = "Встреча в пятницу, в десять."  # the final pass "heard" пятницу again
    assert merge_user_edits(base, user, final) == "Встреча в субботу в десять."


def test_merge_keeps_user_insertion_and_applies_other_fixes():
    base = "давай посмотрим насколько быстро"
    user = "давай ещё раз посмотрим насколько быстро"
    final = "Давай посмотрим, насколько быстро."
    assert merge_user_edits(base, user, final) == "Давай ещё раз посмотрим, насколько быстро."


def test_merge_user_deleted_words():
    base = "это ну короче важно"
    user = "это важно"
    final = "Это, ну, короче, важно."
    assert merge_user_edits(base, user, final) == "Это важно."


def test_merge_never_glues_added_words_to_the_word_before():
    # the user typed after our last word: the corrected word keeps its space before theirs
    assert merge_user_edits("привет мир", "привет мир ой", "Привет, мир!") == "Привет, мир ой"
    assert merge_user_edits("привет мир", "привет мир ой", "Привет мир") == "Привет мир ой"
    # the final text adds a word at the end while the user changed another one
    assert merge_user_edits("раз два", "Раз два", "раз два три") == "Раз два три"


def test_common_affixes():
    assert common_affixes("abcXYZdef", "abcQdef") == (3, 3)
    assert common_affixes("aaa", "aaaa") == (3, 0)


def test_words_from_tokens_real_sample():
    # tokens exactly as returned by GigaAM v3 e2e CTC for the test phrase
    tokens = [" П", "ри", "ве", "т", ".", " ", "Я", " про", "вер", "я", "ю", " ", "но", "во", "е"]
    times = [0.0, 0.36, 0.48, 0.64, 1.36, 1.52, 1.6, 1.8, 1.92, 2.04, 2.2, 2.32, 2.36, 2.48, 2.6]
    words = words_from_tokens(tokens, times)
    assert [w.text for w in words] == ["Привет.", "Я", "проверяю", "новое"]
    assert (words[0].start, words[0].end) == (0.0, 1.36)
    assert (words[2].start, words[2].end) == (1.8, 2.2)


def test_stabilizer_settles_only_agreed_words_and_holds_last():
    st = Stabilizer()
    assert st.update("Привет как".split()) == []  # first guess: nothing agreed yet
    assert st.update("Привет, как дела".split()) == ["Привет,", "как"]  # newest punctuation wins
    assert st.pending == ["дела"]
    assert st.update("Привет, как дела у".split()) == ["дела"]
    assert st.update("Привет, как дела у меня".split()) == ["у"]
    assert st.finalize("Привет, как дела у меня всё хорошо.".split()) == ["меня", "всё", "хорошо."]
    assert st.committed == "Привет, как дела у меня всё хорошо.".split()


def test_stabilizer_does_not_settle_changing_word():
    st = Stabilizer()
    assert st.update("я прове".split()) == []
    assert st.update("я провер".split()) == ["я"]  # "я" agreed; the cut-off last word is held
    assert st.update("я проверяю это".split()) == []  # "провер" != "проверяю"
    assert st.update("я проверяю это слово".split()) == ["проверяю", "это"]


def test_stabilizer_survives_word_split_in_settled_part():
    st = Stabilizer()
    st.update("во время речи и".split())
    assert st.update("во время речи и паузы".split()) == ["во", "время", "речи", "и"]
    # model later splits a settled word differently: alignment must not duplicate words
    hyp = "вовремя речи и паузы тоже".split()
    assert st.update(hyp) == ["паузы"]


def test_join_delta_spacing_and_continuation_case():
    assert join_delta("", ["Привет."]) == "Привет."
    assert join_delta("Привет.", ["Как", "дела?"]) == " Как дела?"
    # split mid-sentence: model capitalized the first word -> lowered
    assert join_delta("я думаю", ["Что", "это"], continuation=True) == " что это"
    # after a sentence end, keep capital
    assert join_delta("Готово.", ["Что", "это"], continuation=True) == " Что это"
    # abbreviation-like word stays
    assert join_delta("я думаю", ["ООО", "Ромашка"], continuation=True) == " ООО Ромашка"


def test_join_chunks():
    assert join_chunks(["привет.", "", " как дела? "]) == "Привет. как дела?"


def test_replacement_examples():
    old = "Давай посмотрим. Насколько быстро это получается."
    new = "Давай посмотрим, насколько быстро это получается."
    r = plan_replacement(old, new)
    # only ". Н" -> ", н" is retyped; the unchanged ending is skipped with Left
    assert (r.left, r.select, r.insert) == (len("асколько быстро это получается."), len(". Н"), ", н")
    ed = SimEditor(old)
    ed.apply(r)
    assert ed.text == new and ed.caret == len(new)


def test_replacement_random_fuzz():
    rnd = random.Random(7)
    alphabet = "абв гд.,!?"
    for _ in range(3000):
        old = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 25)))
        new = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 25)))
        prefix = "Пользовательский текст: "  # text that existed before dictation must stay intact
        ed = SimEditor(prefix + old)
        ed.apply(plan_replacement(old, new))
        assert ed.text == prefix + new, (old, new, ed.text)
        assert ed.caret == len(prefix + new)


def test_wer():
    assert wer("Привет, как дела?", "привет как дела") == 0
    assert abs(wer("раз два три четыре", "раз два три") - 0.25) < 1e-9


# --------------------------------------------------------------------------- our text in a field
def _region(field, head, after, text):
    span = locate_region(field, head, after, text)
    return None if span is None else field[span[0]:span[1]]


def test_fold_keeps_track_of_the_original():
    raw = "Ab" + NBSP + "c" + chr(0x2026) + " d\r\nE" + OBJ + "x " + chr(0x2014) + " " + chr(0xAB) + "q" + chr(0xBB)
    f = fold(raw)
    assert f.s == 'ab c... d\nex -- "q"'
    i = f.s.index("...")
    assert f.span(i, i + 3) == (raw.index(chr(0x2026)), raw.index(chr(0x2026)) + 1)
    j = f.s.index("\n")
    assert raw[slice(*f.span(j, j + 1))] == "\r\n"
    assert f.span(0, 0) == (0, 0) and f.span(3, 3) == (3, 3)


def test_region_in_an_empty_chat_composer():
    # the Claude app's composer (tiptap): an empty field reads as "\n", and that "\n" is gone
    # as soon as there is text
    text = "Привет! Я проверяю новое приложение"
    assert _region(text, "", "\n", text) == text
    # an empty plain contenteditable reads as U+FFFC
    assert _region(text, "", OBJ, text) == text


def test_region_when_the_cursor_context_had_a_non_breaking_space():
    # a contenteditable stored the space after the cursor as U+00A0 and made it a plain space
    # once our words were typed before it
    field = "НАЧАЛО. Здесь текст до диктовки. Привет мир КОНЕЦ ДОКУМЕНТА"
    assert _region(field, "Здесь текст до диктовки. ", NBSP + "КОНЕЦ ДОКУМЕНТА", "Привет мир") == "Привет мир"
    field2 = "Здесь текст" + NBSP + "Привет мир и" + NBSP + "конец"  # ...and the other way round
    assert _region(field2, "Здесь текст ", "", "Привет мир и") == "Привет мир и"


def test_region_ignores_context_from_outside_the_field():
    # ProseMirror without role=textbox: the text before the cursor ran into the menu bar
    menu = "\n" + OBJ + "\nInsert\n" + OBJ + "\nType...\n" + OBJ
    assert _region("Привет мир", menu, "\n", "Привет мир") == "Привет мир"


def test_region_with_editor_typography_and_line_breaks():
    field = "Начало. Ну и вот" + chr(0x2026) + " конец"  # "..." became "…"
    assert _region(field, "Начало. ", "", "Ну и вот... конец") == "Ну и вот" + chr(0x2026) + " конец"
    crlf = "строка один\r\nПривет мир\r\nстрока три"  # WPF reports "\r\n"
    assert _region(crlf, "строка один\r\n", "\r\nстрока три", "Привет мир") == "Привет мир"
    # the cursor was in an empty paragraph: its "\n" disappeared with the first typed word
    assert _region("Первая.\nПривет мир\nТретья.", "Первая.\n\n", "\nТретья.", "Привет мир") == "Привет мир"


def test_region_keeps_the_users_edits_inside_our_text():
    field = "До диктовки. Привет ещё раз мир. После."
    assert _region(field, "До диктовки. ", " После.", "Привет мир.") == "Привет ещё раз мир."


def test_region_by_its_own_ends_when_the_context_changed():
    field = "Другое начало. Привет мир и всё. Другой конец."
    assert _region(field, "Старое начало. ", " Старый конец.", "Привет мир и всё.") == "Привет мир и всё."


def test_region_picks_the_context_right_before_our_text():
    field = "Итак. Раз. Итак. Привет мир."
    assert _region(field, "Итак. ", "", "Привет мир.") == "Привет мир."


def test_region_gone():
    assert locate_region("Совсем другой текст.", "До. ", " После.", "Привет мир и всё такое") is None


def test_place_for_new_words_after_our_text_was_deleted():
    # the composer was emptied (a message sent): new words go before its empty paragraph's "\n"
    assert locate_region("\n", "", "\n", "Привет мир") == (0, 0)
    # the user deleted our words that followed "Привет, ": new words go after the space
    assert locate_region("Привет, ", "Привет, ", "", "как дела") == (8, 8)
    assert locate_region("Привет,\xa0", "Привет,", "", "как дела") == (8, 8)


def test_split_final_where_the_live_text_was_split():
    base = "привет мир как дела у тебя"
    at = len("привет мир")
    assert split_final(base, at, "Привет, мир! Как дела у тебя?") == ("Привет, мир!", " Как дела у тебя?")
    # the new part was typed without a leading space: so is its share of the final text
    base2 = "привет мир" + "как дела"
    assert split_final(base2, len("привет мир"), "Привет, мир. Как дела?") == ("Привет, мир. ", "Как дела?")
    assert split_final(base, len(base), "Привет, мир!") == ("Привет, мир!", "")
