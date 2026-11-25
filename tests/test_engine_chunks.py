"""Engine logic with fake models (fast, deterministic): cuts, chunks, revisions, stop budget, config.

The fake models read the words back from synthetic audio: every word is a run of one constant
sample value (unique per word), pauses are zeros. Like GigaAM e2e, the live guesses start with a
capital letter and end with a period, and word times near the end of the decoded window come out
early (which is what made the old force cut slice the last typed word).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import types

import numpy as np
import pytest

from livetype.config import CONFIG_VERSION, Config, migrate
from livetype.engine import (
    CUT_OVERLAP_S,
    DictationEngine,
    mark_of,
    quiet_gain,
    set_last_mark,
    speech_level_dbfs,
    stitch,
    tail_missing,
)
from livetype.stabilizer import Stabilizer, Word, norm, split_trail

SR = 16000
SYL = ["ма", "ко", "ру", "ли", "не", "то", "са", "пе"]
VOCAB = [a + b + c for a in SYL for b in SYL for c in SYL]  # 512 distinct words
BASE, STEP = 0.2, 0.0004  # sample value of word k


class Dictation:
    """phrases: [(number of words, pause after it in seconds)]. Words are 0.42 s, 70 ms apart."""

    def __init__(self, phrases, gap_s=0.07, lead_s=0.6, tail_s=1.5):
        self.words: list[str] = []
        self.spans: list[tuple[int, int]] = []
        parts = [np.zeros(int(lead_s * SR), np.float32)]
        pos = len(parts[0])
        for n, pause in phrases:
            for i in range(n):
                k = len(self.words)
                w = VOCAB[k]
                if i == 0:
                    w = w[0].upper() + w[1:]
                if i == n - 1:
                    w += "."
                elif i % 5 == 4:
                    w += ","
                size = int((0.12 + 0.05 * len(norm(w))) * SR)
                parts.append(np.full(size, BASE + STEP * k, np.float32))
                self.words.append(w)
                self.spans.append((pos, pos + size))
                pos += size
                g = int((gap_s if i < n - 1 else pause) * SR)
                parts.append(np.zeros(g, np.float32))
                pos += g
        parts.append(np.zeros(int(tail_s * SR), np.float32))
        self.audio = np.concatenate(parts)

    @property
    def text(self) -> str:
        return " ".join(self.words)


class FakeAsr:
    """style "live": open-phrase guesses (no commas, capital first letter, period at the end);
    "plain": bare lowercase words. text() is a full decode: the script's punctuation, capital first
    letter, period at the end (then `transform`)."""

    def __init__(self, d: Dictation, style="live", early_s=0.0, text_delay_s=0.0, transform=None, delays=()):
        self.d, self.style, self.early_s = d, style, early_s
        self.text_delay_s, self.transform = text_delay_s, transform
        self.delays = list(delays)  # per text() call, in order; then text_delay_s
        self.calls = 0
        self.log: list[tuple[str, int, str]] = []  # (method, samples, thread name)

    def _found(self, audio: np.ndarray, early_s: float) -> list[Word]:
        nz = np.abs(audio) > 0.05
        if not nz.any():
            return []
        edges = np.flatnonzero(np.diff(np.concatenate([[0], nz.astype(np.int8), [0]])))
        n, out = len(audio), []
        for s, e in zip(edges[::2], edges[1::2]):
            k = int(round((float(np.median(audio[s:e])) - BASE) / STEP))
            if not 0 <= k < len(self.d.words):
                continue
            a, b = self.d.spans[k]
            if e - s < 0.5 * (b - a):
                continue  # less than half of the word in this window: not heard
            t0, t1 = s / SR, (e - 1) / SR
            dist = (n - e) / SR
            if early_s and dist < 1.25:  # times near the window end come out early
                sh = early_s * (1 - dist / 1.25)
                t0, t1 = max(0.0, t0 - sh), max(0.0, t1 - sh)
            out.append(Word(self.d.words[k], t0, t1))
        return out

    def words(self, audio):
        self.calls += 1
        self.log.append(("words", len(audio), threading.current_thread().name))
        out = self._found(audio, self.early_s)
        if self.style in ("live", "plain"):
            out = [Word(split_trail(w.text)[0].lower(), w.start, w.end) for w in out]
        if out and self.style == "live":
            f, last = out[0], out[-1]
            out[0] = Word(f.text[:1].upper() + f.text[1:], f.start, f.end)
            if not split_trail(out[-1].text)[1]:
                out[-1] = Word(out[-1].text + ".", last.start, last.end)
        return out

    def text(self, audio):
        self.log.append(("text", len(audio), threading.current_thread().name))
        delay = self.delays.pop(0) if self.delays else self.text_delay_s
        if delay:
            time.sleep(delay)
        ws = [w.text for w in self._found(audio, 0.0)]
        if not ws:
            return ""
        t = " ".join(ws)
        t = t[:1].upper() + t[1:]
        if not split_trail(t)[1]:
            t += "."
        return self.transform(t) if self.transform else t


class FakeVad:
    FRAME = 512

    def reset(self) -> None:
        pass

    def prob(self, frame: np.ndarray) -> float:
        return 1.0 if float(np.max(np.abs(frame))) > 0.05 else 0.0


def nwords(text: str) -> list[str]:
    return [norm(w) for w in text.split() if norm(w)]


def run(d, cfg=None, live=None, final=None, revise=True, budget=5.0, stop_at=None, yield_every=0):
    """Stream the dictation like the app (32 ms + poll), then finish. A fake typer field follows
    the callbacks and must always equal engine.text."""
    cfg = cfg or Config()
    live = live or FakeAsr(d, "live")
    final = final or FakeAsr(d, "final")
    field, events = [""], []
    me, feeding = threading.current_thread(), [False]

    def on_commit(delta):
        assert threading.current_thread() is me  # callbacks run on the caller's thread
        field[0] += delta
        events.append(("c", delta))
        assert field[0] == eng.text

    def on_revise(target):
        assert threading.current_thread() is me and not feeding[0]  # only from poll() / finish()
        field[0] = target
        events.append(("r", target))
        assert field[0] == eng.text

    eng = DictationEngine(cfg, live, FakeVad(), final, on_commit=on_commit,
                          on_revise=on_revise if revise else None, warm_up=False)
    x = d.audio if stop_at is None else d.audio[: int(stop_at * SR)]
    for k, i in enumerate(range(0, len(x), 512)):
        feeding[0] = True
        eng.feed(x[i : i + 512])
        feeding[0] = False
        eng.poll()
        if yield_every and k % yield_every == 0:
            time.sleep(0.0005)  # let the final-pass thread run
    t = time.perf_counter()
    out = eng.finish(budget)
    dt = time.perf_counter() - t
    eng.close()
    return eng, out, field[0], events, dt


def no_adjacent_dups(text: str) -> bool:
    w = nwords(text)
    return all(a != b for a, b in zip(w, w[1:]))


# --------------------------------------------------------------------------- cuts and chunks
def test_long_no_pause_input_has_no_adjacent_duplicate_words():
    d = Dictation([(110, 0)])  # ~54 s without a pause: the live phrase is cut many times
    eng, out, field, _, _ = run(d, live=FakeAsr(d, "live", early_s=0.2))
    assert len(eng._phrases) >= 6, "the long phrase must have been cut"
    assert no_adjacent_dups(eng.live_text), eng.live_text
    assert no_adjacent_dups(out), out
    assert nwords(eng.live_text) == nwords(d.text)  # every word typed exactly once
    assert nwords(out) == nwords(d.text)


SHAPES = [
    [(110, 0)],  # continuous speech
    [(6, 0.5), (9, 0.6), (14, 0.5), (4, 0.6), (20, 0.5), (3, 1.2), (12, 0.5), (7, 0)],  # short pauses only
    [(18, 0.3)] * 4,  # pauses shorter than a phrase close
    [(2, 0.6)] * 12,  # many short phrases, one chunk gathers several
]


@pytest.mark.parametrize("shape", SHAPES, ids=["continuous", "short-pauses", "tiny-pauses", "short-phrases"])
@pytest.mark.parametrize("stop", ["end", "mid-word"])
def test_every_chunk_is_8_5_s_or_shorter(shape, stop):
    d = Dictation(shape)
    stop_at = None if stop == "end" else (d.spans[-3][1] + d.spans[-2][0]) / 2 / SR  # between two words
    eng, out, field, _, _ = run(d, stop_at=stop_at)
    info = eng.chunk_info
    assert info
    assert max(c["b"] - c["a"] for c in info) <= 8.5 + 1e-9, info
    for c1, c2 in zip(info, info[1:]):
        if c1["cut"]:  # across a cut inside speech both chunks hear CUT_OVERLAP_S of the other side
            assert c2["left_cut"] and c1["b"] - c2["a"] <= 2 * CUT_OVERLAP_S + 1e-9
        else:  # a pad never reaches into the previous chunk
            assert not c2["left_cut"] and c2["a"] >= c1["b"] - 1e-9
    want = d.words if stop == "end" else d.words[: len(d.words) - 2]
    assert nwords(out) == nwords(" ".join(want))  # no word lost or doubled at any boundary
    assert field == out


def test_stitch_drops_the_words_of_the_neighbouring_chunk():
    own = "я хочу рассказать о том как работает новое приложение для голосового".split()
    final = "Я хочу рассказать о том, как работает новое приложение для голосового ввода, которое...".split()
    assert stitch(final, [], own, "ввода которое распознаёт".split(), False, True) == final[:11]
    left = "новое приложение для голосового".split()  # the next chunk, decoded from 0.5 s before the cut
    final2 = "...голосового ввода, которая распознаёт речь.".split()
    own2 = "ввода которое распознаёт речь".split()
    assert stitch(final2, left, own2, [], True, False) == ["ввода,", "которая", "распознаёт", "речь."]
    # half a word at a cut edge goes; a word the live pass missed inside the chunk stays
    assert stitch("для голосового вв".split(), [], "для голосового".split(), [], False, True) == ["для", "голосового"]
    assert stitch("а б в г".split(), [], "а в г".split(), ["д"], False, True) == ["а", "б", "в", "г"]


def test_a_word_the_previous_chunk_lost_at_the_cut_is_kept_by_the_next_one():
    # real case (dict_01, RNNT): the chunk before the cut dropped its last word "Львы"
    own = "В результате бригады пожарных потушили пожар к 23:35. Львы".split()
    final_k = "В результате бригады пожарных потушили пожар к 23:35.".split()
    assert tail_missing(final_k, own) == 1 and tail_missing(own, own) == 0
    final_k1 = "Львы— самые социальные кошки.".split()
    assert stitch(final_k1, own[-4:], "самые социальные кошки.".split(), [], True, False, rescue=1) == final_k1
    # the mark after the previous chunk's last word comes from the decode that heard what follows
    assert mark_of("подошву. Целые 20% воды".split(), "тонкую подошву целые 20 воды".split(), 1) == "."
    assert mark_of("подошву Целые".split(), "тонкую подошву".split(), 0) is None
    assert set_last_mark("довольно тонкую подошву", ".") == "довольно тонкую подошву."
    assert set_last_mark("пожар к 23:35.", "") == "пожар к 23:35"


class EndDropAsr(FakeAsr):
    """Like RNNT: when the audio ends inside speech, the words of its last 1.3 s are lost."""

    def text(self, audio):
        if float(np.max(np.abs(audio[-int(0.2 * SR) :]))) > 0.05:
            audio = audio.copy()
            audio[-int(1.3 * SR) :] = 0
        return super().text(audio)


def test_cut_boundaries_survive_a_final_model_that_drops_the_end_of_the_audio():
    d = Dictation([(60, 0)])  # ~30 s without a pause: every chunk boundary is a cut
    eng, out, *_ = run(d, final=EndDropAsr(d, "final"))
    assert sum(c["cut"] for c in eng.chunk_info) >= 2
    assert all(c["state"] == "final" for c in eng.chunk_info)
    assert out == d.text  # no word lost at a cut, and no false period where a decode ended


def test_a_period_the_final_model_left_out_at_a_pause_is_restored():
    # like RNNT: a chunk's text ends without a period; the next chunk starts with a capital letter
    d = Dictation([(6, 1.3), (7, 1.3), (5, 0.6), (6, 1.5)])
    eng, out, field, events, _ = run(d, final=FakeAsr(d, "final", transform=lambda t: t.rstrip(".")), yield_every=4)
    assert out == field == d.text, out  # every pause boundary and the end got their period back
    assert all("." not in w for w in d.words[:-1] if w.endswith(",")), "commas stay commas"


def test_finish_returns_within_budget_with_a_slow_final_model():
    d = Dictation([(8, 1.3), (8, 1.3), (8, 1.3), (10, 0)])  # a chunk closes at every 1.3 s pause
    slow = FakeAsr(d, "final", text_delay_s=1.0)
    stop_at = (d.spans[-3][1] + d.spans[-2][0]) / 2 / SR  # mid last phrase
    eng, out, field, events, dt = run(d, final=slow, budget=0.3, stop_at=stop_at)
    assert dt < 0.3 + 0.2, f"finish took {dt:.2f}s"
    states = [s for _, _, s in eng.chunk_spans]
    assert states == ["late"] * 4 and eng.stop_info["late"] == 4  # the open chunk too: it goes to the final model
    assert nwords(out) == nwords(" ".join(d.words[:-2])), "late chunks keep their live text (+ the last guess)"
    assert field == out == eng.live_text
    n = len(events)
    time.sleep(1.2)  # the late results arrive now and are dropped
    assert eng.text == out and len(events) == n


def _stream(eng, x):
    for i in range(0, len(x), 512):
        eng.feed(x[i : i + 512])
        eng.poll()


def test_the_open_chunk_is_decoded_by_the_final_model_at_stop():
    d = Dictation([(9, 1.3), (12, 0)])
    live, final = FakeAsr(d, "live"), FakeAsr(d, "final")
    eng = DictationEngine(Config(), live, FakeVad(), final, on_revise=lambda t: None, warm_up=False)
    stop_at = (d.spans[-4][1] + d.spans[-3][0]) / 2 / SR  # mid phrase: 3 words not heard yet
    _stream(eng, d.audio[: int(stop_at * SR)])
    out = eng.finish(2.0)
    eng.close()
    a, b, state = eng.chunk_spans[-1]
    assert state == "final" and not eng.stop_info["open_speculative"]
    calls = [(n, th) for m, n, th in final.log if m == "text"]
    assert (int(round((b - a) * SR)), "stop-decode_0") in calls  # once, on a thread of its own
    assert [m for m, *_ in live.log].count("text") == 0, "nothing at stop comes from the live model"
    assert nwords(out) == nwords(" ".join(d.words[:-3])) and "," in out.split(".")[-2]


def test_a_stop_after_the_pause_uses_the_decode_started_when_the_phrase_closed():
    d = Dictation([(9, 0.6), (8, 2.0)])
    live, final = FakeAsr(d, "live"), FakeAsr(d, "final")
    end = d.spans[-1][1] / SR
    stop_at = end + 0.7  # the phrase closed (0.45 s of silence), the chunk would close at ~0.95 s
    eng = DictationEngine(Config(), live, FakeVad(), final, on_revise=lambda t: None, warm_up=False)
    _stream(eng, d.audio[: int(stop_at * SR)])
    time.sleep(0.05)  # the speculative decode (fake: instant) is done
    out = eng.finish(1.0)
    eng.close()
    a, b, state = eng.chunk_spans[-1]
    assert state == "final" and eng.stop_info["open_speculative"] and nwords(out) == nwords(d.text)
    n = int(round((b - a) * SR))
    assert [c for c in final.log if c[0] == "text" and c[1] == n] == [("text", n, "final-pass_0")], "decoded once"
    assert [m for m, *_ in live.log].count("text") == 0, "no extra decode on the live session"


def test_a_speculative_decode_queued_behind_a_slow_chunk_is_not_waited_for():
    d = Dictation([(10, 1.3), (6, 2.0)])
    final = FakeAsr(d, "final", delays=[2.0])  # the first chunk's decode is slow
    eng = DictationEngine(Config(), FakeAsr(d, "live"), FakeVad(), final, on_revise=lambda t: None, warm_up=False)
    _stream(eng, d.audio[: int((d.spans[-1][1] / SR + 0.7) * SR)])  # phrase 2 closed: its decode is queued
    t = time.perf_counter()
    out = eng.finish(0.5)
    dt = time.perf_counter() - t
    eng.close()
    assert dt < 0.5 + 0.15
    assert [s for _, _, s in eng.chunk_spans] == ["late", "final"]  # the open chunk was decoded at once
    assert not eng.stop_info["open_speculative"] and nwords(out) == nwords(d.text)


def test_speech_that_resumes_within_the_gap_drops_the_speculative_decode():
    d = Dictation([(4, 0.6), (5, 0.6), (3, 1.5)])  # phrases close at each pause, the chunk does not
    final = FakeAsr(d, "final")
    eng, out, *_ = run(d, final=final, yield_every=2)
    assert [s for _, _, s in eng.chunk_spans] == ["final"]  # one chunk with all three phrases
    assert nwords(out) == nwords(d.text)
    full = int(round((eng.chunk_spans[0][1] - eng.chunk_spans[0][0]) * SR))
    assert ("text", full) in [(m, n) for m, n, _ in final.log], "the whole chunk was decoded in the end"


def test_the_guard_keeps_the_live_text_when_the_final_drops_words():
    d = Dictation([(10, 1.3), (6, 0)])
    eng, out, *_ = run(d, final=FakeAsr(d, "final", transform=lambda t: t.split()[0]))
    states = [s for _, _, s in eng.chunk_spans]
    assert states == ["kept_live", "kept_live"]
    assert out == eng.live_text


# --------------------------------------------------------------------------- revisions
def test_revise_targets_converge_to_the_finish_text():
    d = Dictation([(7, 1.2), (12, 0.6), (9, 1.3), (25, 0.5), (6, 0)])
    eng, out, field, events, _ = run(d, yield_every=4)
    revs = [t for k, t in events if k == "r"]
    assert len(revs) >= 2, "corrections arrive while dictating"
    assert revs[-1] == out == field == eng.text
    assert nwords(out) == nwords(d.text)
    assert "," in out and "," not in eng.live_text  # the final text brought the commas


def _feed(eng, x, a, b, settle=False):
    for i in range(a, b, 512):
        eng.feed(x[i : min(i + 512, b)])
        eng.poll()
    if settle:  # wait for the chunk result and let poll() report it
        t = time.monotonic()
        while not eng.revise_log and time.monotonic() - t < 3:
            time.sleep(0.005)
            eng.poll()


def _session(d, live, final):
    events = []
    field = [""]

    def oc(s):
        field[0] += s
        events.append(("c", s))

    def orv(t):
        field[0] = t
        events.append(("r", t))

    eng = DictationEngine(Config(), live, FakeVad(), final, on_commit=oc, on_revise=orv, warm_up=False)
    return eng, events, field


def test_deltas_after_a_revise_follow_the_corrected_text():
    # live words without capitals or periods; the final text ends the sentence
    d = Dictation([(4, 1.4), (4, 0)])
    eng, events, field = _session(d, FakeAsr(d, "plain"), FakeAsr(d, "final"))
    second = d.spans[4][0] - int(0.3 * SR)
    _feed(eng, d.audio, 0, second, settle=True)
    assert events[-1] == ("r", " ".join(d.words[:4]))  # "Мамама мамако мамару мамали."
    n = len(events)
    _feed(eng, d.audio, second, len(d.audio))
    first_delta = next(s for k, s in events[n:] if k == "c")
    assert first_delta.startswith(" ") and first_delta[1:2].isupper(), first_delta  # space + capital
    assert eng.finish(5) == field[0] == eng.text
    eng.close()


def test_deltas_after_a_revise_drop_a_capital_the_final_text_does_not_support():
    # live: "Мамама мамако мамару мамали." then "Мамане ..." (new guess, capital letter);
    # the final text continues the sentence there (a comma), so the next live word is lowered
    d = Dictation([(4, 1.4), (4, 0)])
    final = FakeAsr(d, "final", transform=lambda t: t.rstrip(".").lower() + ",")
    eng, events, field = _session(d, FakeAsr(d, "live"), final)
    second = d.spans[4][0] - int(0.3 * SR)
    _feed(eng, d.audio, 0, second, settle=True)
    assert events[-1][0] == "r" and events[-1][1].endswith(",")
    n = len(events)
    _feed(eng, d.audio, second, len(d.audio))
    first_delta = next(s for k, s in events[n:] if k == "c")
    assert first_delta.startswith(" ") and first_delta[1:2].islower(), first_delta
    eng.finish(5)
    eng.close()


def test_without_an_on_revise_consumer_nothing_is_revised_until_finish():
    d = Dictation([(7, 1.2), (9, 0.6), (8, 0)])
    deltas, texts = [], []
    live, final = FakeAsr(d, "live"), FakeAsr(d, "final")

    def oc(s):
        deltas.append(s)
        texts.append(eng.text)

    eng = DictationEngine(Config(), live, FakeVad(), final, on_commit=oc, warm_up=False)
    for k, i in enumerate(range(0, len(d.audio), 512)):
        eng.feed(d.audio[i : i + 512])
        eng.poll()
        if k % 4 == 0:
            time.sleep(0.0005)
    assert "".join(deltas) == eng.live_text == eng.text  # the typer has exactly the live text
    out = eng.finish(5)
    eng.close()
    assert out == eng.text != eng.live_text  # finish() still returns the corrected text
    assert nwords(out) == nwords(d.text) and "," in out


# --------------------------------------------------------------------------- live path details
def test_one_live_decode_per_poll_over_the_newest_audio():
    d = Dictation([(30, 0)])
    live = FakeAsr(d, "live")
    eng = DictationEngine(Config(), live, FakeVad(), None, warm_up=False)
    eng.feed(d.audio[: int(3.6 * SR)])  # 3 s of speech arrive at once (as after a slow decode)
    n = live.calls
    eng.poll()
    assert live.calls == n + 1  # one decode, not one per missed interval
    eng.poll()
    assert live.calls == n + 1
    eng.feed(d.audio[int(3.6 * SR) : int(3.7 * SR)])
    eng.poll()
    assert live.calls == n + 1  # less than decode_interval_s of new audio
    eng.feed(d.audio[int(3.7 * SR) : int(4.0 * SR)])
    eng.poll()
    assert live.calls == n + 2
    eng.close()


def test_deferred_punctuation_types_no_period_inside_a_phrase():
    d = Dictation([(9, 1.5)])  # every live guess ends with a period
    eng, out, *_ = run(d)
    assert eng.live_text.count(".") == 1 and eng.live_text.endswith("."), eng.live_text


def test_the_last_word_is_typed_before_the_phrase_closes():
    early = 0
    for lead in (0.6, 0.7, 0.8, 0.9, 1.0):
        d = Dictation([(6, 1.5)], lead_s=lead)
        eng, *_ = run(d)
        last = norm(d.words[-1])
        t = next(t for t, s in eng.commit_log if last in [norm(w) for w in s.split()])
        if t < d.spans[-1][1] / SR + Config().live_min_silence_s:
            early += 1
    assert early >= 3  # whenever a decode lands 0.2-0.45 s into the silence


def test_stabilizer_settles_the_last_word_only_when_told():
    st = Stabilizer()
    st.update("я проверяю".split())
    assert st.update("я проверяю".split()) == ["я"]
    assert st.update("я проверяю".split(), settle_last=True) == ["проверяю"]
    st.seed(["проверяю"])
    assert st.committed == ["проверяю"] and st.update("Проверяю новое слово".split()) == []
    assert split_trail("слово,") == ("слово", ",") and split_trail("—") == ("—", "")


def test_without_the_final_pass_stop_settles_the_last_phrase():
    d = Dictation([(8, 0)], tail_s=0.0)
    cfg = Config()
    cfg.final_check = False
    eng, out, field, *_ = run(d, cfg=cfg)
    assert out == eng.live_text == field and nwords(out) == nwords(d.text)


def test_warm_up_decodes_silence_in_both_sessions_off_the_session_thread():
    d = Dictation([(3, 0)])
    live, final = FakeAsr(d, "live"), FakeAsr(d, "final")
    eng = DictationEngine(Config(), live, FakeVad(), final)
    t = time.monotonic()
    while (len(live.log) < 1 or len(final.log) < 1) and time.monotonic() - t < 2:
        time.sleep(0.01)
    eng.close()
    me = threading.current_thread().name
    assert live.log[0][:2] == ("words", int(0.3 * SR)) and live.log[0][2] != me
    assert final.log[0][:2] == ("text", int(0.3 * SR)) and final.log[0][2] != me


# --------------------------------------------------------------------------- quiet input
def test_quiet_gain_only_below_minus_50_dbfs_and_at_most_30_db():
    t = np.arange(SR) / SR
    tone = np.sin(2 * np.pi * 200 * t).astype(np.float32)  # sine: level = -3 dBFS
    assert abs(speech_level_dbfs(tone) + 3.01) < 0.1
    quiet = tone * 10 ** (-57 / 20)  # -60 dBFS
    assert abs(speech_level_dbfs(quiet) + 60.0) < 0.1
    assert quiet_gain(-40.0) == 1.0 and quiet_gain(-49.9) == 1.0
    assert abs(20 * np.log10(quiet_gain(-52.0)) - 26.0) < 1e-6  # towards -26 dBFS
    assert abs(20 * np.log10(quiet_gain(-70.0)) - 30.0) < 1e-6  # capped


# --------------------------------------------------------------------------- microphone logging
def test_microphone_logs_the_stream_and_counts_input_overflows(monkeypatch, caplog):
    from livetype import audio

    streams = []

    class Stream:
        def __init__(self, samplerate, channels, dtype, blocksize, device, callback):
            self.samplerate, self.blocksize, self.device, self.callback = samplerate, blocksize, device, callback
            self.latency = 0.0213
            streams.append(self)

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=Stream))
    src = audio.MicSource(None)
    with caplog.at_level(logging.INFO, logger="livetype"):
        src.start()
        cb = streams[0].callback
        block = np.zeros((512, 1), np.float32)
        cb(block, 512, None, types.SimpleNamespace(input_overflow=True))
        cb(block, 512, None, types.SimpleNamespace(input_overflow=False))
        cb(block, 512, None, types.SimpleNamespace(input_overflow=True))
        src.stop()
    assert src.overflows == 2 and src.q.qsize() == 3
    assert "rate=16000 Hz latency=21 ms" in caplog.text and "input overflows=2" in caplog.text


# --------------------------------------------------------------------------- settings migration
V1_FILE = {  # what LiveType 0.2.x saved: every field, old defaults included
    "hotkey": "ctrl+alt+d", "activation_mode": "toggle", "final_check": True,
    "live_model": "gigaam-v3-e2e-ctc", "final_model": "gigaam-v3-e2e-rnnt", "quantization": "int8",
    "live_threads": 4, "final_threads": 2, "vad_threshold": 0.5, "vad_neg_threshold": 0.35,
    "live_min_silence_s": 0.45, "decode_interval_s": 0.3, "max_live_segment_s": 7.0, "preroll_s": 0.25,
    "tail_pad_s": 0.2, "chunk_gap_s": 1.2, "chunk_soft_max_s": 22.0, "use_uia": True,
}


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.read_bytes()


def test_old_settings_file_gets_the_new_defaults(tmp_path):
    path = tmp_path / "config.json"
    raw = _write(path, V1_FILE)
    cfg = Config.load(path)
    new = Config()
    for key in ("final_model", "final_threads", "vad_threshold", "vad_neg_threshold", "chunk_gap_s",
                "chunk_soft_max_s", "chunk_pad_s", "stop_budget_s"):
        assert getattr(cfg, key) == getattr(new, key), key
    assert (cfg.final_model, cfg.vad_threshold, cfg.chunk_gap_s, cfg.chunk_soft_max_s) == (
        "gigaam-v3-e2e-rnnt", 0.4, 0.75, 8.0)
    assert cfg.config_version == CONFIG_VERSION == 2
    assert path.read_bytes() == raw, "loading never writes the user's file"


def test_values_the_user_changed_survive_the_migration(tmp_path):
    path = tmp_path / "config.json"
    _write(path, {**V1_FILE, "hotkey": "RightAlt", "vad_threshold": 0.6, "chunk_gap_s": 1.0,
                  "max_live_segment_s": 10.0, "final_model": "gigaam-v3-e2e-ctc", "final_threads": 1})
    cfg = Config.load(path)
    assert (cfg.hotkey, cfg.vad_threshold, cfg.chunk_gap_s, cfg.max_live_segment_s) == ("RightAlt", 0.6, 1.0, 10.0)
    assert (cfg.final_model, cfg.final_threads) == ("gigaam-v3-e2e-ctc", 1)
    assert cfg.vad_neg_threshold == 0.25 and cfg.chunk_soft_max_s == 8.0  # still old defaults -> new


def test_a_current_settings_file_is_taken_as_is(tmp_path):
    path = tmp_path / "config.json"
    _write(path, {**V1_FILE, "config_version": 2})  # chosen on purpose under the new version
    cfg = Config.load(path)
    assert (cfg.vad_threshold, cfg.chunk_gap_s, cfg.final_model) == (0.5, 1.2, "gigaam-v3-e2e-rnnt")
    assert migrate({"config_version": 2, "vad_threshold": 0.5}) == {"config_version": 2, "vad_threshold": 0.5}


def test_save_writes_the_version_and_round_trips(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config.load(path)  # no file: defaults
    cfg.hotkey = "f9"
    cfg.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["config_version"] == CONFIG_VERSION and data["chunk_pad_s"] == 0.2
    again = Config.load(path)
    assert again.hotkey == "f9" and again.vad_threshold == 0.4 and again.stop_budget_s == 0.8
