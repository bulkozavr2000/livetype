"""Integration test: real models, synthesized Russian speech streamed frame by frame."""

from pathlib import Path

import pytest

from livetype.asr import Recognizer, StreamingVad
from livetype.audio import load_wav
from livetype.config import Config
from livetype.engine import DictationEngine
from livetype.stabilizer import norm
from livetype.textops import wer

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def models():
    cfg = Config()
    live = Recognizer(cfg.live_model, cfg.models_dir, cfg.quantization, cfg.live_threads)
    final = Recognizer(cfg.final_model, cfg.models_dir, cfg.quantization, cfg.final_threads)
    vad = StreamingVad(cfg.models_dir)
    return cfg, live, final, vad


def stream(models, name):
    cfg, live, final, vad = models
    if not (DATA / f"{name}.wav").exists():
        pytest.skip(f"run scripts/make_test_audio.ps1 for {name}")
    eng = DictationEngine(cfg, live, vad, final)
    audio = load_wav(DATA / f"{name}.wav")
    for i in range(0, len(audio), 512):  # like the microphone: 32 ms at a time
        eng.feed(audio[i : i + 512])
        eng.poll()
    live_before_stop = eng.live_text
    final_text = eng.finish()
    ref = (DATA / f"{name}.txt").read_text(encoding="utf-8").replace("|", " ").strip()
    return eng, ref, live_before_stop, final_text


def test_words_appear_while_speaking(models):
    eng, ref, live_before_stop, final_text = stream(models, "ru_sample")
    log = eng.commit_log
    print("\ncommits (audio time -> typed):")
    for t, d in log:
        print(f"  {t:5.2f}s  {d!r}")
    print("live :", eng.live_text)
    print("final:", final_text)
    print(f"decode: n={len(eng.decode_times)} avg={sum(eng.decode_times) / len(eng.decode_times) * 1000:.0f} ms")

    assert log[0][0] < 2.0, "first word must appear within 2 s"
    # the 1st sentence (spoken 0.2..4.4 s) is typed piece by piece, not in one go
    first_sentence = [d for t, d in log if t < 5.0]
    assert len(first_sentence) >= 3
    assert wer(ref, live_before_stop) <= 0.10, "almost everything is typed before you stop"
    assert wer(ref, final_text) <= 0.05
    # the final pass sees bigger chunks and fixes punctuation at pauses
    assert "посмотрим, насколько" in final_text


def test_long_phrase_is_cut_between_words(models):
    eng, ref, live_before_stop, final_text = stream(models, "ru_long")
    print("\nlive :", eng.live_text)
    print("final:", final_text)
    for t, d in eng.commit_log:
        print(f"  {t:5.2f}s  {d!r}")
    words = [norm(w) for w in eng.live_text.split()]
    assert all(a != b for a, b in zip(words, words[1:])), "no duplicated words at cut points"
    assert wer(ref, eng.live_text) <= 0.12
    assert wer(ref, final_text) <= 0.08
    # words keep coming during a long phrase without pauses
    assert sum(1 for t, _ in eng.commit_log if t < 8.0) >= 3
