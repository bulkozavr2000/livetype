"""«Обработка текста»: rules and guards (always), the server lifecycle against a fake
llama-server (always), and the real light model (if downloaded)."""

import http.server
import json
import threading
import time

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


# --------------------------------------------------------------------------- fillers: standalone only
@pytest.mark.parametrize("src,out", [
    ("Ну, короче, завтра встречаемся.", "Завтра встречаемся."),
    ("Он, короче, ушёл.", "Он ушёл."),
    ("Я думаю, ну, что да.", "Я думаю, что да."),  # the comma before "что" stays
    ("Завтра, короче.", "Завтра."),
    ("Эм, я думаю, э, что да.", "Я думаю, что да."),
    ("Я э думаю.", "Я думаю."),
    ("В общем, всё хорошо.", "Всё хорошо."),
    ("Это, типа, важно.", "Это важно."),
    # real words stay
    ("Это значит много.", "Это значит много."),
    ("Путь короче.", "Путь короче."),
    ("Ну и что?", "Ну и что?"),
    ("В общем случае это верно.", "В общем случае это верно."),
    ("Как бы то ни было, мы идём.", "Как бы то ни было, мы идём."),
    ("Типа того.", "Типа того."),
    ("Найди мне таблицу этого типа.", "Найди мне таблицу этого типа."),
])
def test_fillers_only_when_they_stand_alone(src, out):
    assert llm.remove_fillers(src) == out


# --------------------------------------------------------------------------- guards
LONG = ("сегодня мы обсудили новый проект и решили что первая версия выйдет в марте а вторая "
        "версия выйдет летом после тестов с командой")


def test_guards_accept_a_clean_up():
    # fillers, a stutter and two words joined are not "dropped words"
    assert llm.deleted_words("ну короче я я думаю что во время встречи", "Я думаю, что вовремя встречи") == []
    src = "ну короче я я думаю что " + LONG
    out = "Я думаю, что " + LONG.replace("решили что", "решили, что").capitalize()
    assert llm.check_output(src, out, rules()) is None
    assert llm.wer_without_fillers("ну, короче, " + LONG, LONG) == 0  # removing fillers is no error


def test_guards_reject_too_many_changes_and_dropped_words():
    changed = LONG.replace("марте", "мае").replace("летом", "осенью").replace("тестов", "проверок")
    assert llm.check_output(LONG, changed, rules()) is None  # 3 of 22 words: within 0.15
    assert llm.check_output(LONG, changed.replace("новый", "старый"), rules()) == "WER 0.18"
    dropped = LONG.replace(" с командой", "")  # WER under 0.15, but real words are gone
    assert llm.wer_without_fillers(LONG, dropped) <= 0.15
    assert llm.check_output(LONG, dropped, rules()) == "2 words dropped"
    assert llm.check_output(LONG, dropped, rules(shorter=True)) is None  # "Писать коротко" asks for it
    assert llm.check_output(LONG, "", rules()) == "empty"
    assert llm.check_output(LONG, "сегодня", rules()) == "much shorter"


def test_guard_knows_a_filler_from_a_real_word():
    # the same words inside a phrase are real: dropping them changes the meaning
    assert llm.deleted_words("Это значит много.", "Это много.") == ["значит"]
    assert llm.deleted_words("Путь короче.", "Путь.") == ["короче"]
    assert llm.deleted_words("Найди таблицу этого типа.", "Найди таблицу этого.") == ["типа"]
    # where fillers stand (sentence start, after a comma or another filler) they may go
    assert llm.deleted_words("ну короче завтра встречаемся", "Завтра встречаемся.") == []
    assert llm.deleted_words("Он, типа, ушёл.", "Он ушёл.") == []
    assert llm.deleted_words("Я э думаю.", "Я думаю.") == []  # a hesitation sound, anywhere
    # the user keeps fillers (the "Без «ну» и «короче»" rule is off): the model may not drop them
    src = "Ну, завтра встречаемся в десять часов утра у входа в главный офис компании"
    out = src.replace("Ну, з", "З")
    assert llm.check_output(src, out, rules()) is None
    assert llm.check_output(src, out, rules(**{"remove-fillers": False})) == "1 words dropped"


def test_answer_is_one_line():
    assert llm.clean_output("Вот исправленный текст: Первый абзац.\n\nВторой абзац.") == "Первый абзац. Второй абзац."


def test_deadlines():
    assert llm.deadline_s("light", "раз два три") == pytest.approx(2.3)
    assert llm.deadline_s("light", " ".join(["слово"] * 500)) == 15.0
    assert llm.deadline_s("accurate", "раз два три") == pytest.approx(21.2)
    assert llm.deadline_s("accurate", " ".join(["слово"] * 500)) == 150.0


# --------------------------------------------------------------------------- server lifecycle (fake llama-server)
class _FakeServer(http.server.BaseHTTPRequestHandler):
    answer = ""
    delay = 0.0
    requests: list = []

    def do_GET(self):  # noqa: N802
        self._json({"status": "ok"} if self.path == "/health" else {})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(body)
        time.sleep(self.delay)
        try:
            self._json({"content": self.answer})
        except OSError:
            pass  # the client went away

    def _json(self, data):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class _FakeProc:
    def __init__(self):
        self.killed = False

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True


@pytest.fixture
def fake_llama(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(_FakeServer, "requests", [])
    monkeypatch.setattr(_FakeServer, "delay", 0.0)
    tp = llm.TextProcessor()
    launched = []
    tp.ready = lambda model: True
    tp._free_port = lambda: srv.server_port
    tp._launch = lambda model, port: launched.append(model) or _FakeProc()
    yield tp, launched
    tp.stop()
    srv.shutdown()


def test_process_never_starts_the_server(fake_llama):
    tp, launched = fake_llama
    t0 = time.monotonic()
    assert tp.process("раз два три", rules(), model="light", timeout=5) is None
    assert time.monotonic() - t0 < 0.5 and launched == []


def test_server_started_at_the_hotkey_answers_later(fake_llama, monkeypatch):
    tp, launched = fake_llama
    monkeypatch.setattr(_FakeServer, "answer", "Я думаю, что завтра мы встретимся.\n\nВ офисе.")
    t0 = time.monotonic()
    tp.start_async("light")  # at the hotkey: returns at once
    assert time.monotonic() - t0 < 0.1
    out = tp.process("ну короче я думаю что завтра мы встретимся в офисе", rules(), model="light", timeout=5)
    assert out == "Я думаю, что завтра мы встретимся. В офисе"  # one line, no final period (rule)
    assert launched == ["light"] and len(_FakeServer.requests) == 1
    tp.start_async("light")  # already running: nothing new
    assert launched == ["light"]


def test_process_gives_up_at_the_deadline(fake_llama, monkeypatch):
    tp, _ = fake_llama
    assert tp.start("light")
    monkeypatch.setattr(_FakeServer, "answer", "Раз, два, три.")
    monkeypatch.setattr(_FakeServer, "delay", 2.0)
    t0 = time.monotonic()
    assert tp.process("раз два три", rules(), model="light", timeout=0.5) is None
    assert time.monotonic() - t0 < 1.0


def test_process_is_cancelled_by_a_new_dictation(fake_llama, monkeypatch):
    tp, _ = fake_llama
    assert tp.start("light")
    monkeypatch.setattr(_FakeServer, "answer", "Раз, два, три.")
    monkeypatch.setattr(_FakeServer, "delay", 3.0)
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    t0 = time.monotonic()
    assert tp.process("раз два три", rules(), model="light", timeout=10, cancel=cancel) is None
    assert time.monotonic() - t0 < 0.8


def test_rejected_answer_is_none(fake_llama, monkeypatch):
    tp, _ = fake_llama
    assert tp.start("light")
    monkeypatch.setattr(_FakeServer, "answer", LONG.replace(" с командой", ""))
    assert tp.process(LONG, rules(), model="light", timeout=5) is None


def test_server_stops_when_unused(fake_llama):
    tp, _ = fake_llama
    tp.idle_stop_s = 0.3
    assert tp.start("light")
    proc = tp.proc
    t_end = time.monotonic() + 3
    while tp.proc is not None and time.monotonic() < t_end:
        time.sleep(0.05)
    assert tp.proc is None and proc.killed


# --------------------------------------------------------------------------- the real light model
@pytest.mark.skipif(not llm.TextProcessor().ready("light"), reason="light model not downloaded")
def test_light_model_cleans_dictation():
    tp = llm.TextProcessor()
    try:
        assert tp.start("light")
        src = "ну короче я думаю что завтра мы встретимся в офисе в десять часов утра и обсудим проект"
        out = tp.process(src, rules(), model="light", timeout=60)
        print(out)
        assert out is not None and out.startswith("Я думаю, что завтра")
        assert "ну" not in out.lower().split() and not out.endswith(".") and "\n" not in out
        # the model must not answer a question found in the text, only clean it up
        q_in = "а ты не знаешь сколько сейчас времени"
        q = tp.process(q_in, rules(), model="light", timeout=60)
        print(q)
        assert q is None or (llm.wer(q_in, q) <= 0.2 and len(q) < 1.5 * len(q_in))
    finally:
        tp.stop()
