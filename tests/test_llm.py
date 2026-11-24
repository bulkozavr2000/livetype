"""«Обработка текста»: rules logic (always) and the real light model (if downloaded)."""

import pytest

from livetype import llm
from livetype.config import DEFAULT_RULES


def rules(**on):
    out = [dict(r) for r in DEFAULT_RULES]
    for r in out:
        if r["id"] in on:
            r["enabled"] = on[r["id"]]
    return out


def test_rule_instructions_follow_type():
    # defaults: fillers removed; no final period requested
    assert llm.rule_instructions(rules()) == ["Убирай слова-паразиты.", "Не ставь точку в конце последнего предложения."]
    ins = llm.rule_instructions(rules(**{"remove-fillers": False, "final-period": True}))
    assert "Сохраняй слова-паразиты." in ins and "Сохраняй точку в конце последнего предложения." in ins
    custom = rules() + [{"id": "custom-1", "label": "Менять GitHub на гитхаб", "enabled": True, "custom": True}]
    assert llm.rule_instructions(custom)[-1] == "Менять GitHub на гитхаб"


def test_simple_rules():
    assert llm.apply_simple_rules("Ну, короче, завтра встречаемся.", rules(), light_model=True) == "Завтра встречаемся"
    r = rules(**{"lowercase-start": True, "final-period": True})
    assert llm.apply_simple_rules("Завтра встречаемся", r, light_model=False) == "завтра встречаемся."
    assert llm.apply_simple_rules("Правда?", rules(), light_model=False) == "Правда?"


@pytest.mark.skipif(not llm.TextProcessor().ready("light"), reason="light model not downloaded")
def test_light_model_cleans_dictation():
    tp = llm.TextProcessor()
    try:
        src = "ну короче я думаю что завтра мы встретимся в офисе в десять часов утра и обсудим проект"
        out = tp.process(src, rules(), model="light")
        print(out)
        assert out.startswith("Я думаю, что завтра")
        assert "ну" not in out.lower().split() and not out.endswith(".")
        # the model must not answer a question found in the text, only clean it up
        q_in = "а ты не знаешь сколько сейчас времени"
        q = tp.process(q_in, rules(), model="light")
        print(q)
        assert llm.wer(q_in, q) <= 0.2 and len(q) < 1.5 * len(q_in)
    finally:
        tp.stop()
