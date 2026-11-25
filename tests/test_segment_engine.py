"""SegmentEngine: cut at pauses, recognize every segment once, type the texts in order."""

from __future__ import annotations

import time

import numpy as np

from livetype.config import SAMPLE_RATE, Config
from livetype.segment_engine import FRAME, SegmentEngine


class LevelVad:
    """Speech = a loud frame (the test audio is 0.5 for speech and 0 for silence)."""

    def reset(self) -> None:
        pass

    def prob(self, frame: np.ndarray) -> float:
        return 0.9 if float(np.abs(frame).mean()) > 0.1 else 0.0


class SpanAsr:
    """Returns one word per speech span it hears, named after the span's start second."""

    def __init__(self, delay: float = 0.0):
        self.calls: list[float] = []
        self.delay = delay

    def text(self, audio: np.ndarray) -> str:
        self.calls.append(len(audio) / SAMPLE_RATE)
        if self.delay:
            time.sleep(self.delay)
        loud = np.abs(audio) > 0.1
        words, i = [], 0
        while i < len(loud):
            if loud[i]:
                j = i
                while j < len(loud) and loud[j]:
                    j += 1
                words.append(f"w{j - i}")
                i = j
            else:
                i += 1
        return " ".join(words)


def audio_of(plan: list[tuple[str, float]]) -> np.ndarray:
    parts = [np.full(int(s * SAMPLE_RATE), 0.5 if kind == "s" else 0.0, dtype=np.float32) for kind, s in plan]
    return np.concatenate(parts)


def no_final() -> Config:
    cfg = Config()
    cfg.final_check = False
    return cfg


def run(plan, cfg=None, asr=None, poll=True):
    cfg = cfg or no_final()
    asr = asr or SpanAsr()
    typed: list[str] = []
    eng = SegmentEngine(cfg, None, LevelVad(), asr, on_commit=typed.append)
    x = audio_of(plan)
    for i in range(0, len(x), FRAME):
        eng.feed(x[i : i + FRAME])
        if poll:
            eng.poll()
    text = eng.finish()
    eng.close()
    return eng, typed, text, asr


def test_cut_at_pause_after_min_length():
    eng, typed, text, asr = run([("q", 0.5), ("s", 2.5), ("q", 0.6), ("s", 1.0), ("q", 0.3)])
    assert len(eng.segments) == 2
    assert not any(g["forced"] for g in eng.segments)
    assert len(text.split()) == 2  # every speech span heard exactly once
    assert "".join(typed) == text


def test_short_phrase_waits_for_min_length():
    # 1 s of speech, a pause, 1.5 s more: the first pause comes before segment_min_s, no cut there
    eng, _, text, _ = run([("s", 1.0), ("q", 0.6), ("s", 1.5), ("q", 0.6), ("s", 1.0)])
    assert eng.segments[0]["b"] > 2.5
    assert len(text.split()) == 3


def test_soft_max_takes_a_short_pause():
    cfg = no_final()
    # speech with only 0.25 s gaps: no cut at 0.4 s pauses; after soft max a 0.25 s gap closes it
    plan = []
    for _ in range(6):
        plan += [("s", 1.2), ("q", 0.25)]
    eng, _, text, _ = run(plan, cfg)
    first = eng.segments[0]
    assert cfg.segment_soft_max_s <= first["b"] - first["a"] <= cfg.segment_soft_max_s + 1.6
    assert not first["forced"]
    assert len(text.split()) == 6


def test_no_pause_is_cut_at_max():
    cfg = no_final()
    cfg.segment_max_s = 6.0
    eng, _, _, _ = run([("s", 13.0), ("q", 0.5)], cfg)
    assert eng.segments[0]["forced"]
    assert abs((eng.segments[0]["b"] - eng.segments[0]["a"]) - 6.0) < 0.2
    total = sum(g["b"] - g["a"] for g in eng.segments)
    assert abs(total - 13.25) < 0.3  # no audio lost or heard twice


def test_silence_only_is_never_sent():
    eng, typed, text, asr = run([("q", 5.0)])
    assert asr.calls == [] and typed == [] and text == ""


def test_leading_silence_is_dropped():
    eng, _, _, asr = run([("q", 4.0), ("s", 1.0), ("q", 0.5)])
    assert len(asr.calls) == 1
    assert asr.calls[0] < 1.8  # 0.3 s preroll + 1 s speech + 0.25 s tail


def test_segments_typed_in_order_while_listening():
    asr = SpanAsr(delay=0.05)
    typed: list[str] = []
    eng = SegmentEngine(no_final(), None, LevelVad(), asr, on_commit=typed.append)
    x = audio_of([("s", 2.2), ("q", 0.6), ("s", 2.2), ("q", 0.6), ("s", 0.8)])
    for i in range(0, len(x), FRAME):
        eng.feed(x[i : i + FRAME])
        eng.poll()
        time.sleep(0.0005)
    typed_before_stop = len(typed)
    text = eng.finish()
    eng.close()
    assert typed_before_stop >= 1  # text appears while the user still speaks
    assert "".join(typed) == text
    assert text.split() == ["w35200", "w35200", "w12800"]


def test_failed_segment_does_not_stop_the_rest():
    class Flaky(SpanAsr):
        def text(self, audio):
            if not self.calls:
                self.calls.append(0)
                raise RuntimeError("boom")
            return super().text(audio)

    _, _, text, _ = run([("s", 2.2), ("q", 0.6), ("s", 1.0), ("q", 0.3)], asr=Flaky())
    assert text.split() == ["w16000"]


class WholeAsr(SpanAsr):
    """Segments get one word per speech span; a recording with more than one span is the
    whole-recording pass and gets the text made by `whole(words)`."""

    def __init__(self, whole):
        super().__init__()
        self.whole = whole

    def text(self, audio):
        words = super().text(audio).split()
        return self.whole(words) if len(words) > 2 else " ".join(words)


PLAN3 = [("q", 0.5), ("s", 2.2), ("q", 0.6), ("s", 2.2), ("q", 0.6), ("s", 0.8), ("q", 0.3)]


def test_final_pass_takes_punctuation_from_the_whole_recording():
    asr = WholeAsr(lambda w: f"{w[0].upper()}, {w[1]} {w[2]}.")
    eng, typed, final, asr = run(PLAN3, Config(), asr)
    assert "".join(typed) == "w35200 w35200 w12800"  # every segment is typed, the last one too
    assert final == "W35200, w35200 w12800."
    whole = asr.calls[-1]
    assert abs(whole - (0.3 + 2.2 + 0.6 + 2.2 + 0.6 + 0.8 + 0.25)) < 0.1  # from the first speech to the end
    assert eng.stop_info["final_s"] >= 0


def test_final_pass_keeps_the_words_of_the_segments():
    # the whole-recording pass hears one word differently and drops another: the segment words stay
    asr = WholeAsr(lambda w: f"{w[0].upper()}, other.")
    _, _, final, _ = run(PLAN3, Config(), asr)
    assert final == "W35200, w35200 w12800"


def test_final_pass_failure_keeps_the_typed_text():
    class Broken(SpanAsr):
        def text(self, audio):
            if len(super().text(audio).split()) > 2:
                raise RuntimeError("boom")
            return super().text(audio)

    _, typed, final, _ = run(PLAN3, Config(), Broken())
    assert final.split() == ["w35200", "w35200", "w12800"]
    assert "".join(typed) == final


def test_punctuate_from():
    from livetype.segment_engine import punctuate_from

    assert punctuate_from("Я хотел сказать. Что завтра приду.", "я хотел сказать, что завтра приду") ==         "я хотел сказать, что завтра приду"
    assert punctuate_from("Я хотел сказать. Что завтра приду.", "Я хотел сказать, что завтра пойду.") ==         "Я хотел сказать, что завтра приду."
    assert punctuate_from("Ёлка стоит.", "Елка стоит, да.") == "Елка стоит."
    assert punctuate_from("один два", "") == "один два"
