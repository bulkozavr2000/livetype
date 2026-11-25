"""Type's dictation, typed as you speak.

Type records, and when you press the key again it recognizes the recording with its model and
types the text. This engine does the same, only piece by piece: the recording is cut at pauses
into segments of a few seconds, every finished segment goes to the same recognizer at once (in the
background, listening goes on) and its text is typed as soon as it is ready. A typed segment is
never changed. At stop only the last segment is left to recognize.

Where a segment closes (VAD = Silero, frames of 32 ms):
  * after segment_min_s, at the first pause of segment_pause_s;
  * after segment_soft_max_s, at the first pause of SHORT_PAUSE_S;
  * at segment_max_s, in the quietest 0.1 s of its last HARD_CUT_SEARCH_S (rare: speech with no pause).
A segment is cut in the middle of its pause, so no audio is lost or heard twice.

Contract for the typer (callbacks run on the thread that calls feed/poll/finish, in order):
  on_commit(delta)  `delta` is appended to the end of the text typed so far (engine.text)
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

import numpy as np

from livetype.config import SAMPLE_RATE, Config
from livetype.textops import join_delta

log = logging.getLogger("livetype")

FRAME = 512
PREROLL_S = 0.3  # audio kept before the first speech of a segment
TAIL_S = 0.25  # at most this much of a pause stays at the end of a segment
SHORT_PAUSE_S = 0.2
HARD_CUT_SEARCH_S = 2.0


class _Buffer:
    def __init__(self, initial_s: float = 60.0):
        self._buf = np.zeros(int(initial_s * SAMPLE_RATE), dtype=np.float32)
        self.size = 0

    def append(self, x: np.ndarray) -> None:
        n = len(x)
        if self.size + n > len(self._buf):
            grown = np.zeros(max(2 * len(self._buf), self.size + n), dtype=np.float32)
            grown[: self.size] = self._buf[: self.size]
            self._buf = grown
        self._buf[self.size : self.size + n] = x
        self.size += n

    def view(self, a: int, b: int) -> np.ndarray:
        return self._buf[max(0, a) : min(self.size, b)]


class SegmentEngine:
    def __init__(
        self,
        cfg: Config,
        live,
        vad,
        final=None,
        on_commit: Callable[[str], None] | None = None,
        on_level: Callable[[float], None] | None = None,
        on_revise: Callable[[str], None] | None = None,
        warm_up: bool = True,
    ):
        self.cfg = cfg
        self.rec = final if final is not None else live  # anything with text(audio) -> str
        self.vad = vad
        self.on_commit = on_commit
        self.on_level = on_level
        self.on_revise = on_revise  # never called: typed segments are not changed
        self.vad.reset()
        self.audio = _Buffer()
        self._rest = np.zeros(0, dtype=np.float32)
        self.in_speech = False
        self.seg_start = 0
        self.seg_speech = False  # the open segment has speech in it
        self.last_speech_end = 0
        self.silence = 0  # samples since the last speech frame
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recognize")
        self._jobs: deque[tuple[int, int, Future]] = deque()
        self._text = ""
        self._closed = False
        # diagnostics (no text is logged)
        self.commit_log: list[tuple[float, str]] = []
        self.decode_times: list[float] = []
        self.segments: list[dict] = []
        self.stop_info: dict = {}

    def _s(self, seconds: float) -> int:
        return int(seconds * SAMPLE_RATE)

    @property
    def audio_time(self) -> float:
        return self.audio.size / SAMPLE_RATE

    @property
    def text(self) -> str:
        return self._text

    @property
    def live_text(self) -> str:  # name the app and the reports use
        return self._text

    # ---------------------------------------------------------------- input
    def feed(self, samples: np.ndarray) -> None:
        x = samples.astype(np.float32, copy=False).reshape(-1)
        if len(self._rest):
            x = np.concatenate([self._rest, x])
        n = len(x) // FRAME
        for i in range(n):
            self._frame(x[i * FRAME : (i + 1) * FRAME])
        self._rest = x[n * FRAME :].copy()

    def poll(self) -> None:
        """Type the text of every finished segment, in order."""
        while self._jobs and self._jobs[0][2].done():
            self._deliver(*self._jobs.popleft())

    def _frame(self, frame: np.ndarray) -> None:
        cfg = self.cfg
        self.audio.append(frame)
        end = self.audio.size
        p = self.vad.prob(frame)
        if self.on_level:
            self.on_level(float(np.sqrt(np.mean(frame * frame))))
        speech = p >= (cfg.vad_neg_threshold if self.in_speech else cfg.vad_threshold)
        self.in_speech = speech
        if speech:
            self.silence = 0
            self.last_speech_end = end
            self.seg_speech = True
            self._check_hard_cap()
            return
        self.silence += len(frame)
        if not self.seg_speech:  # no speech yet: keep only a short preroll
            self.seg_start = max(self.seg_start, end - self._s(PREROLL_S))
            return
        length = end - self.seg_start
        if length >= self._s(cfg.segment_min_s) and self.silence >= self._s(cfg.segment_pause_s):
            self._cut(self.last_speech_end + min(self.silence // 2, self._s(TAIL_S)))
        elif length >= self._s(cfg.segment_soft_max_s) and self.silence >= self._s(SHORT_PAUSE_S):
            self._cut(self.last_speech_end + self.silence // 2)
        self._check_hard_cap()

    def _check_hard_cap(self) -> None:
        if not self.seg_speech or self.audio.size - self.seg_start < self._s(self.cfg.segment_max_s):
            return
        # speech with no pause: cut in the quietest 0.1 s of the last HARD_CUT_SEARCH_S
        a = self.audio.size - self._s(HARD_CUT_SEARCH_S)
        x = self.audio.view(a, self.audio.size)
        win = self._s(0.1)
        hop = win // 2
        energy = [float(np.mean(x[i : i + win] ** 2)) for i in range(0, len(x) - win + 1, hop)]
        best = len(energy) - 1 - int(np.argmin(energy[::-1])) if energy else 0  # the latest quietest point
        self._cut(a + best * hop + win // 2, forced=True)

    def _cut(self, cut: int, forced: bool = False) -> None:
        a, b = self.seg_start, min(cut, self.audio.size)
        self.seg_start = b
        # the rest of the pause (or the speech after a forced cut) starts the next segment
        self.seg_speech = forced or self.last_speech_end > b
        self.segments.append({"a": a / SAMPLE_RATE, "b": b / SAMPLE_RATE, "forced": forced})
        self._submit(a, b)

    def _submit(self, a: int, b: int) -> None:
        audio = self.audio.view(a, b).copy()
        self._jobs.append((a, b, self._pool.submit(self._recognize, audio)))

    def _recognize(self, audio: np.ndarray) -> str:
        t = time.perf_counter()
        text = self.rec.text(audio)
        self.decode_times.append(time.perf_counter() - t)
        return text

    def _deliver(self, a: int, b: int, job: Future) -> None:
        try:
            text = job.result()
        except Exception:
            log.exception("segment %.1f-%.1f s failed", a / SAMPLE_RATE, b / SAMPLE_RATE)
            return
        delta = join_delta(self._text, text.split())
        if not delta:
            return
        self._text += delta
        self.commit_log.append((b / SAMPLE_RATE, delta))
        if self.on_commit:
            self.on_commit(delta)

    # ---------------------------------------------------------------- stop
    def finish(self, budget_s: float | None = None) -> str:
        """Recognize the last segment, type everything still pending, return the whole text.
        Like Type, it waits for the recognizer (budget_s is ignored: quality first)."""
        t0 = time.monotonic()
        if self.seg_speech:
            end = min(self.audio.size, self.last_speech_end + self._s(TAIL_S))
            if end > self.seg_start:
                self.segments.append({"a": self.seg_start / SAMPLE_RATE, "b": end / SAMPLE_RATE, "forced": False})
                self._submit(self.seg_start, end)
            self.seg_speech = False
        while self._jobs:
            a, b, job = self._jobs.popleft()
            try:
                job.result(timeout=120)
            except Exception:
                pass
            self._deliver(a, b, job)
        self.stop_info = {"stop_s": round(time.monotonic() - t0, 3), "segments": len(self.segments)}
        return self._text

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._pool.shutdown(wait=False, cancel_futures=True)
