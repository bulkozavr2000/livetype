"""Dictation engine: microphone samples in -> settled words out (live) + corrected text at the end.

Live pass:  speech is split into phrases by pauses (Silero VAD). The open phrase
            is re-recognized every ~0.3 s by the fast model; words that two
            consecutive guesses agree on are emitted immediately (on_commit).
            A phrase longer than ~7 s is cut at a word boundary (by word timing).
Final pass: phrases are grouped into chunks (split at pauses >= 1.2 s, max ~15 s)
            and each closed chunk is re-recognized by the accurate model in the
            background, so at stop only the last chunk is left to process.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

import numpy as np

from livetype.asr import Recognizer, StreamingVad
from livetype.config import SAMPLE_RATE, Config
from livetype.stabilizer import Stabilizer, Word
from livetype.textops import join_chunks, join_delta

FRAME = StreamingVad.FRAME


class AudioBuffer:
    """Growing float32 buffer with the whole recording (needed for the final pass)."""

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


class DictationEngine:
    def __init__(
        self,
        cfg: Config,
        live: Recognizer,
        vad: StreamingVad,
        final: Recognizer | None = None,
        on_commit: Callable[[str], None] | None = None,
        on_level: Callable[[float], None] | None = None,
    ):
        self.cfg = cfg
        self.live = live
        self.vad = vad
        self.final = final if cfg.final_pass else None
        self.on_commit = on_commit
        self.on_level = on_level

        self.vad.reset()
        self.audio = AudioBuffer()
        self._rest = np.zeros(0, dtype=np.float32)
        self.live_text = ""
        self.in_speech = False
        self.seg_start = 0
        self.last_seg_end = 0
        self.last_speech_end = 0
        self.silence = 0
        self.last_decode_at = 0
        self.stab = Stabilizer()
        self.continuation = False
        self._first_emit_in_segment = True
        self.chunk: list[int] | None = None
        self._final_jobs: list[Future[str]] = []
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="final-pass") if self.final else None
        # diagnostics (no text is logged anywhere by default)
        self.commit_log: list[tuple[float, str]] = []
        self.decode_times: list[float] = []

    def _s(self, seconds: float) -> int:
        return int(seconds * SAMPLE_RATE)

    @property
    def audio_time(self) -> float:
        return self.audio.size / SAMPLE_RATE

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
        """Re-recognize the open phrase if enough new audio arrived."""
        if self.in_speech and self.audio.size - self.last_decode_at >= self._s(self.cfg.decode_interval_s):
            self._decode_open()

    def _frame(self, frame: np.ndarray) -> None:
        cfg = self.cfg
        start = self.audio.size
        self.audio.append(frame)
        end = self.audio.size
        p = self.vad.prob(frame)
        if self.on_level:
            self.on_level(float(np.sqrt(np.mean(frame * frame))))
        if not self.in_speech:
            if p >= cfg.vad_threshold:
                self.in_speech = True
                self.seg_start = max(start - self._s(cfg.preroll_s), self.last_seg_end)
                self.silence = 0
                self.last_speech_end = end
                self.last_decode_at = start
                self._new_segment(continuation=False)
                if self.chunk is None:
                    self.chunk = [self.seg_start, end]
            elif self.chunk is not None and end - self.chunk[1] >= self._s(cfg.chunk_gap_s):
                self._close_chunk()
            return
        if p < cfg.vad_neg_threshold:
            self.silence += len(frame)
        else:
            self.silence = 0
            self.last_speech_end = end
        if self.silence >= self._s(cfg.live_min_silence_s):
            self._close_segment(min(self.last_speech_end + self._s(cfg.tail_pad_s), end))

    # ---------------------------------------------------------------- live pass
    def _new_segment(self, continuation: bool) -> None:
        self.stab = Stabilizer()
        self.continuation = continuation
        self._first_emit_in_segment = True

    def _recognize(self, a: int, b: int) -> list[Word]:
        t = time.perf_counter()
        words = self.live.words(self.audio.view(a, b))
        self.decode_times.append(time.perf_counter() - t)
        return words

    def _decode_open(self) -> None:
        words = self._recognize(self.seg_start, self.audio.size)
        texts = [w.text for w in words]
        self._emit(self.stab.update(texts))
        self.last_decode_at = self.audio.size
        if self.audio.size - self.seg_start >= self._s(self.cfg.max_live_segment_s):
            self._force_cut(words, texts)

    def _force_cut(self, words: list[Word], texts: list[str]) -> None:
        """Long phrase without pauses: drop its settled part, cut between words."""
        off = self.stab.offset(texts)
        if 0 < off < len(words):
            cut = self.seg_start + self._s((words[off - 1].end + words[off].start) / 2)
            if cut > self.seg_start:
                if self.chunk is not None and cut - self.chunk[0] >= self._s(self.cfg.chunk_soft_max_s):
                    self.chunk[1] = cut
                    self._close_chunk()
                    self.chunk = [cut, cut]
                self.seg_start = cut
                self._new_segment(continuation=True)
                return
        if self.audio.size - self.seg_start >= self._s(2 * self.cfg.max_live_segment_s):
            # nothing settled for too long: settle what we have and start over
            self._emit(self.stab.finalize(texts))
            self.seg_start = self.audio.size
            self._new_segment(continuation=True)

    def _close_segment(self, seg_end: int) -> None:
        words = self._recognize(self.seg_start, seg_end)
        self._emit(self.stab.finalize([w.text for w in words]))
        self.in_speech = False
        self.silence = 0
        self.last_seg_end = seg_end
        if self.chunk is None:
            self.chunk = [self.seg_start, seg_end]
        else:
            self.chunk[1] = seg_end
        if self.chunk[1] - self.chunk[0] >= self._s(self.cfg.chunk_soft_max_s):
            self._close_chunk()

    def _emit(self, words: list[str]) -> None:
        if not words:
            return
        delta = join_delta(self.live_text, words, continuation=self.continuation and self._first_emit_in_segment)
        self._first_emit_in_segment = False
        if not delta:
            return
        self.live_text += delta
        self.commit_log.append((self.audio_time, delta))
        if self.on_commit:
            self.on_commit(delta)

    # ---------------------------------------------------------------- final pass
    def _close_chunk(self) -> None:
        assert self.chunk is not None
        a, b = self.chunk
        self.chunk = None
        if self._pool is None or self.final is None or b - a < self._s(0.2):
            return
        self._final_jobs.append(self._pool.submit(self.final.text, self.audio.view(a, b).copy()))

    def finish(self) -> str:
        """Stop: settle the last phrase, then return the corrected text of the whole recording."""
        if len(self._rest):
            self.audio.append(self._rest)
            self._rest = np.zeros(0, dtype=np.float32)
        if self.in_speech:
            self._close_segment(self.audio.size)
        if self.chunk is not None:
            if self.chunk[1] - self.chunk[0] > 0:
                self._close_chunk()
            else:
                self.chunk = None
        if self._pool is None:
            return self.live_text
        texts = [job.result() for job in self._final_jobs]
        self._pool.shutdown(wait=True)
        return join_chunks(texts)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
