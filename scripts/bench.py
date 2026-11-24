"""Замер скорости моделей на этом компьютере: python scripts/bench.py tests/data/ru_sample.wav"""

import sys
import time
import wave
from pathlib import Path

import numpy as np
import onnx_asr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from livetype.config import Config  # noqa: E402

MODELS_DIR = Path(Config().models_dir)


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


def timed(fn, repeat=1):
    best = 1e9
    out = None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - t)
    return out, best


def main(path: str) -> None:
    audio = load_wav(path)
    dur = len(audio) / 16000
    print(f"audio: {dur:.1f} s")

    t = time.perf_counter()
    ctc = onnx_asr.load_model("gigaam-v3-e2e-ctc", MODELS_DIR / "gigaam-v3-e2e-ctc", quantization="int8")
    rnnt = onnx_asr.load_model("gigaam-v3-e2e-rnnt", MODELS_DIR / "gigaam-v3-e2e-rnnt", quantization="int8")
    vad = onnx_asr.load_vad("silero", MODELS_DIR / "silero")
    print(f"load all: {time.perf_counter() - t:.1f} s")

    # warm-up
    ctc.recognize(audio[:16000], sample_rate=16000)
    rnnt.recognize(audio[:16000], sample_rate=16000)

    for name, m in (("CTC ", ctc), ("RNNT", rnnt)):
        segs, sec = timed(lambda m=m: list(m.with_vad(vad).recognize(audio, sample_rate=16000)))
        text = " ".join(s.text for s in segs)
        print(f"{name} full+VAD: {sec:.2f} s (RTF {sec / dur:.3f})  {len(segs)} segs\n   {text}")

    print("live re-decode cost (CTC, growing buffer):")
    for L in (1, 2, 4, 6, 8, 10, 15, 20):
        if L > dur:
            break
        _, sec = timed(lambda L=L: ctc.recognize(audio[: L * 16000], sample_rate=16000), repeat=3)
        print(f"  {L:>2} s buffer -> {sec * 1000:6.0f} ms")


if __name__ == "__main__":
    main(sys.argv[1])
