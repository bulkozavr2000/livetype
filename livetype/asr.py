"""Offline models: GigaAM v3 e2e (speech -> punctuated text) and Silero VAD (v6.2)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnx_asr
import onnxruntime as ort

from livetype.config import SAMPLE_RATE
from livetype.stabilizer import Word, words_from_tokens

MIN_SAMPLES = int(0.2 * SAMPLE_RATE)

# files needed offline (Hugging Face repos of onnx-asr; GigaAM: MIT, Silero VAD: MIT), pinned to a
# commit and checked by size and SHA-256
_GIGAAM = ("istupakov/gigaam-v3-onnx", "322c3b29492673eb7d0b434bfa9dfb8653e34d02")
_SILERO = ("istupakov/silero-vad-onnx", "b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6")
_GIGAAM_CONFIG = (135, "0641fcf73f4af791c73f05083e38a658ff5dcbee3534a0c61396c10f4b97f0fe")
MODEL_FILES: dict[str, tuple[tuple[str, str], dict[str, tuple[int, str]]]] = {
    "gigaam-v3-e2e-ctc": (_GIGAAM, {
        "config.json": _GIGAAM_CONFIG,
        "v3_e2e_ctc.int8.onnx": (224893347, "2e3fcb7a7b66030336fd10c2fcfb033bd1dc7e1bf238fe5cfd83b1d0cfc9d28e"),
        "v3_e2e_ctc_vocab.txt": (2007, "142de7570b3de5b3035ce111a89c228e80e6085273731d944093ddf24fa539cd"),
    }),
    "gigaam-v3-e2e-rnnt": (_GIGAAM, {
        "config.json": _GIGAAM_CONFIG,
        "v3_e2e_rnnt_decoder.int8.onnx": (1159170, "89014e134865615b91e037157e46e389b1271e6072460efc010ea08e61e23146"),
        "v3_e2e_rnnt_encoder.int8.onnx": (224570477, "4e0e076a6076cd110277e529b8ac8f32cd5297f7fbebad5341ae8ddb7d00817b"),
        "v3_e2e_rnnt_joint.int8.onnx": (687791, "ade116563dbf66e503b0994efab6b5861412743e52bf31c39fc3fffa3783d5d1"),
        "v3_e2e_rnnt_vocab.txt": (13354, "39abae20e692998290c574e606f11a9edef2902a1995463fcff63d1490cf22b7"),
    }),
    "silero": (_SILERO, {
        "config.json": (30, "1094039d370c82889582ba739a3d1caac5754c8b3a17a66a534200c9f72086e2"),
        "silero_vad.onnx": (2327524, "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"),
    }),
}
DOWNLOAD_MB = round(sum(size for _, files in MODEL_FILES.values() for size, _ in files.values()) / 1e6)


def _files(models_dir: str | Path):
    base = Path(models_dir)
    for name, ((repo, rev), files) in MODEL_FILES.items():
        for f, (size, sha) in files.items():
            yield base / name / f, f"https://huggingface.co/{repo}/resolve/{rev}/{f}", size, sha


def models_missing(models_dir: str | Path) -> bool:
    """Missing or wrong-sized files (a quick check on every start; the hash is checked on download)."""
    return any(not p.is_file() or p.stat().st_size != size for p, _, size, _ in _files(models_dir))


def ensure_models(models_dir: str | Path, progress=None) -> None:
    """Download the speech models once (~455 MB). After that everything works offline.

    progress(downloaded_mb, total_mb) is called while downloading.
    """
    from livetype.download import fetch

    todo = [(p, url, size, sha) for p, url, size, sha in _files(models_dir)
            if not p.is_file() or p.stat().st_size != size]
    total = sum(size for *_, size, _ in todo) / 1e6
    before = 0
    for p, url, size, sha in todo:
        fetch(url, p, size, sha, (lambda got, b=before: progress((b + got) / 1e6, total)) if progress else None)
        before += size


def repair_models(models_dir: str | Path, progress=None) -> bool:
    """Called when the models fail to load: re-download every file whose SHA-256 is wrong.

    Returns False if all files were fine (the problem is elsewhere).
    """
    from livetype.download import sha256_of

    bad = [p for p, _, size, sha in _files(models_dir)
           if p.is_file() and (p.stat().st_size != size or sha256_of(p) != sha)]
    for p in bad:
        p.unlink(missing_ok=True)
    if not bad and not models_missing(models_dir):
        return False
    ensure_models(models_dir, progress)
    return True


def _session_options(threads: int | None) -> ort.SessionOptions:
    so = ort.SessionOptions()
    if threads:
        so.intra_op_num_threads = threads
    so.log_severity_level = 3
    return so


class Recognizer:
    def __init__(self, name: str, models_dir: str | Path, quantization: str | None = "int8", threads: int | None = None):
        path = Path(models_dir) / name
        if not path.is_dir():
            ensure_models(models_dir)
        self.model = onnx_asr.load_model(name, path, quantization=quantization, sess_options=_session_options(threads))
        self._ts = self.model.with_timestamps()

    def words(self, audio: np.ndarray) -> list[Word]:
        """Recognize with per-word times (seconds from the start of `audio`)."""
        if len(audio) < MIN_SAMPLES:
            return []
        r = self._ts.recognize(audio, sample_rate=SAMPLE_RATE)
        return words_from_tokens(r.tokens or [], r.timestamps or [])

    def text(self, audio: np.ndarray) -> str:
        if len(audio) < MIN_SAMPLES:
            return ""
        return self.model.recognize(audio, sample_rate=SAMPLE_RATE).strip()


class StreamingVad:
    """Silero VAD fed frame by frame (512 samples = 32 ms).

    The shipped file (istupakov/silero-vad-onnx, pinned above) is byte-identical to Silero VAD v6.2
    (snakers4/silero-vad tag v6.2, sha256 1a153a22...).
    """

    FRAME = 512
    CONTEXT = 64

    def __init__(self, models_dir: str | Path):
        path = Path(models_dir) / "silero" / "silero_vad.onnx"
        self.session = ort.InferenceSession(str(path), sess_options=_session_options(1), providers=["CPUExecutionProvider"])
        self._sr = np.array([SAMPLE_RATE], dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.CONTEXT), dtype=np.float32)

    def prob(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame.reshape(1, -1).astype(np.float32)], axis=1)
        out, self._state = self.session.run(["output", "stateN"], {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -self.CONTEXT :]
        return float(out[0, 0])
