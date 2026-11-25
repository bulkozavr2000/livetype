"""Audio sources: microphone (real use) and WAV file (tests and demos)."""

from __future__ import annotations

import logging
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Protocol

import numpy as np

from livetype.config import SAMPLE_RATE

BLOCK = 512  # 32 ms
log = logging.getLogger("livetype")


class Source(Protocol):
    q: queue.Queue[np.ndarray]
    finished: threading.Event

    def start(self) -> None: ...
    def stop(self) -> None: ...


def load_wav(path: str | Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("Нужен WAV 16 кГц, моно, 16 бит")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


class _Resampler:
    """Fallback for microphones that refuse 16 kHz: low-pass + linear interpolation."""

    def __init__(self, src_rate: int):
        self.ratio = SAMPLE_RATE / src_rate
        n = np.arange(-24, 25)
        cutoff = 0.45 * SAMPLE_RATE / src_rate
        self.taps = (np.sinc(2 * cutoff * n) * 2 * cutoff * np.hamming(len(n))).astype(np.float32)
        self.hist = np.zeros(len(n) - 1, dtype=np.float32)
        self.pos = 0.0  # fractional read position carried between blocks

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y = np.convolve(np.concatenate([self.hist, x]), self.taps, mode="valid")
        self.hist = np.concatenate([self.hist, x])[-(len(self.taps) - 1) :]
        idx = np.arange(self.pos, len(y), 1 / self.ratio)
        self.pos = idx[-1] + 1 / self.ratio - len(y) if len(idx) else self.pos - len(y)
        return np.interp(idx, np.arange(len(y)), y).astype(np.float32)


def resolve_input_device(device: int | str | None) -> int | None:
    """The panel stores a microphone by its name in the MME list (what ipc.list_devices shows).

    sounddevice would match a name against every host API and fail on duplicates, so the name is
    turned into the MME device index here. An unplugged microphone falls back to the default one.
    """
    if device is None or isinstance(device, int):
        return device
    import sounddevice as sd

    try:
        mme = next((i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME"), None)
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and d["name"] == device and (mme is None or d["hostapi"] == mme):
                return i
    except Exception:
        log.exception("could not list microphones")
    log.info("microphone %r not found, using the default one", device)
    return None


class MicSource:
    def __init__(self, device: int | str | None = None):
        self.device = device
        self.q: queue.Queue[np.ndarray] = queue.Queue()
        self._stream = None
        self._resample: _Resampler | None = None
        self.finished = threading.Event()  # never set for a microphone
        self.overflows = 0  # callbacks where PortAudio reported lost input (input_overflow)

    def start(self) -> None:
        import sounddevice as sd

        self.device = resolve_input_device(self.device)
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK, device=self.device, callback=self._cb
            )
        except Exception:
            rate = int(sd.query_devices(self.device, "input")["default_samplerate"])
            self._resample = _Resampler(rate)
            self._stream = sd.InputStream(
                samplerate=rate, channels=1, dtype="float32", blocksize=int(rate * 0.032), device=self.device, callback=self._cb
            )
        self._stream.start()
        self._log_opened()

    def _log_opened(self) -> None:
        """Once per session: what the microphone really gave us (no audio is logged)."""
        s = self._stream
        try:
            latency = float(s.latency) * 1000
        except (TypeError, ValueError):
            latency = float("nan")
        log.info("microphone opened: device=%s rate=%s Hz latency=%.0f ms block=%s%s",
                 self.device if self.device is not None else "default", getattr(s, "samplerate", "?"), latency,
                 getattr(s, "blocksize", "?"), " (resampled to 16 kHz)" if self._resample else "")

    def _cb(self, indata, frames, time_info, status) -> None:  # noqa: ANN001 (sounddevice callback)
        if status and getattr(status, "input_overflow", False):
            self.overflows += 1  # PortAudio dropped input: the recording has a gap here
        x = indata[:, 0].copy()
        self.q.put(self._resample(x) if self._resample else x)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            log.info("microphone closed: input overflows=%d", self.overflows)


class FileSource:
    """Plays a WAV into the engine as if it were the microphone."""

    def __init__(self, path: str | Path, realtime: bool = True):
        self.audio = load_wav(path)
        self.realtime = realtime
        self.q: queue.Queue[np.ndarray] = queue.Queue()
        self.finished = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="file-source", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        t0 = time.perf_counter()
        for i in range(0, len(self.audio), BLOCK):
            if self._stop.is_set():
                break
            if self.realtime:
                delay = t0 + i / SAMPLE_RATE - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
            self.q.put(self.audio[i : i + BLOCK])
        self.finished.set()

    def stop(self) -> None:
        self._stop.set()
