"""Unit tests for the pure logic (no models, no Windows input)."""

import random

from livetype.stabilizer import Stabilizer, words_from_tokens
from livetype.textops import (
    SimEditor,
    common_affixes,
    join_chunks,
    join_delta,
    merge_user_edits,
    plan_replacement,
    wer,
)


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
