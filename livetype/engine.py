"""Dictation engine: microphone samples in -> words typed while you speak -> corrected text.

Live pass   Silero VAD splits speech into phrases at pauses. The open phrase is re-recognized
            every ~0.3 s on the live session; words that two consecutive guesses agree on are
            typed at once (LocalAgreement-2). A settled word is typed without its trailing
            punctuation mark: the mark comes with the next word, taken from the newer guess, so
            the mark and the next capital letter agree. The last word of a phrase settles once
            two guesses agree on it and the VAD has heard 0.2 s of silence. A phrase that reaches
            max_live_segment_s is cut at a settled word boundary whose timing is reliable (word
            times near the end of the decoded window come out early); the settled words after
            the cut stay as context of the next phrase and are not typed again.
Final pass  phrases are grouped into chunks. A chunk closes at a pause >= chunk_gap_s, at the
            first pause after chunk_soft_max_s, and never grows past chunk_soft_max_s + 0.5 s
            with its pads (it is then closed at its last phrase boundary). Each closed chunk gets
            chunk_pad_s of audio on each side (never into the previous chunk) and is
            re-recognized at once by the final model (its own session, final_threads), in the
            background. Across a cut inside speech there is no pad: both chunks are decoded with
            CUT_OVERLAP_S of audio across the cut and the words of the other side are dropped
            again (stitch). When a phrase closes at a pause, its chunk is decoded right away as
            if that pause closes it (speculation); the result is used if it does. When a chunk's
            final text differs from what was typed for it, poll() reports the corrected full text
            through on_revise.
Stop        finish() does not run another live decode: it decodes the open chunk once with the
            final model (on a thread of its own, so it never queues behind an older chunk job),
            waits at most stop_budget_s for it and for older chunk jobs (late ones keep their live
            text), reports the last revision and returns the final text. So every word of the
            result has passed through the final model, unless a chunk was late or its final text
            failed the guard (under 50% of the typed words).

Contract for the typer. All callbacks run on the thread that calls feed/poll/finish, in order:
  on_commit(delta)   `delta` is appended to the end of the current text (engine.text)
  on_revise(target)  the whole current text is now `target`; later deltas extend it
engine.text is the current text: the final text of every corrected chunk followed by the live
text of the rest; engine.live_text is what the live pass alone produced (diagnostics).
Without an on_revise consumer nothing is revised while dictating (the deltas are exactly the
live text) and finish() returns the full corrected text, as in 0.2.
"""

from __future__ import annotations

import difflib
import logging
import statistics
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass

import numpy as np

from livetype.asr import Recognizer, StreamingVad
from livetype.config import SAMPLE_RATE, Config
from livetype.stabilizer import Stabilizer, Word, norm, split_trail
from livetype.textops import SENTENCE_END, join_delta

log = logging.getLogger("livetype")

FRAME = StreamingVad.FRAME
EARLY_SILENCE_S = 0.2  # the last word of a phrase may settle after this much VAD silence
STABLE_EDGE_S = 1.0  # word times are reliable only this far before the end of the decoded window
CHUNK_CAP_EXTRA_S = 0.5  # a decoded chunk (pads included) is never longer than chunk_soft_max_s + this
MIN_CHUNK_S = 0.2  # shorter chunks are not sent to the final model
# a chunk boundary at a cut inside speech: both chunks are decoded with this much audio across the cut
# (a hard boundary garbles the word next to it), and the words of the other side are dropped again.
# Final WER clean/laptop on the bench dictations: 0.073/0.156 with no overlap, 0.056/0.142 at 0.5 s,
# 0.052/0.132 at 1.0 s, no better at 1.2 s (D:/LiveType/bench/scripts/ship_eval.py replay --const ...)
CUT_OVERLAP_S = 1.0
STITCH_CONTEXT = 4  # typed words of the other side used to find those words
# when a phrase closes at a pause, decode its chunk right away as if the pause will close the chunk
# (it does unless speech resumes within chunk_gap_s): the result is ready ~0.5 s sooner at stop
SPECULATE = True
WARMUP_S = 0.3
QUIET_DBFS = -50.0  # quiet input: speech below this level gets gain before recognition (cfg.quiet_gain)
GAIN_TARGET_DBFS = -26.0
GAIN_MAX_DB = 30.0


def speech_level_dbfs(x: np.ndarray) -> float:
    """Active speech level: mean power of the louder half of the 20 ms frames, in dBFS."""
    f = SAMPLE_RATE // 50
    n = len(x) // f
    if n < 3:
        p = float(np.mean(np.square(x, dtype=np.float64))) if len(x) else 0.0
        return float(10 * np.log10(p + 1e-12))
    p = np.mean(np.square(x[: n * f].reshape(n, f), dtype=np.float64), axis=1)
    act = p[p >= np.median(p)]
    return float(10 * np.log10(np.mean(act) + 1e-12))


def quiet_gain(level_dbfs: float) -> float:
    """Linear gain for a decode: 1.0 at normal levels; below QUIET_DBFS towards GAIN_TARGET_DBFS,
    at most GAIN_MAX_DB."""
    if level_dbfs >= QUIET_DBFS:
        return 1.0
    return float(10 ** (min(GAIN_MAX_DB, GAIN_TARGET_DBFS - level_dbfs) / 20))


def _opcodes(words: Sequence[str], typed: Sequence[str]):
    return difflib.SequenceMatcher(None, [norm(w) for w in words], [norm(w) for w in typed],
                                   autojunk=False).get_opcodes()


def stitch(words: Sequence[str], left: Sequence[str], own: Sequence[str], right: Sequence[str],
           left_cut: bool, right_cut: bool, rescue: int = 0) -> list[str]:
    """Words of a chunk's final text that belong to the chunk itself.

    The chunk was decoded with extra audio across a cut (left_cut / right_cut), so its final text
    also has words of the neighbouring chunk. `own` are the words typed for the chunk, `left` /
    `right` the typed words just before / after it. The final words are aligned to left+own+right
    by their bare form; words aligned outside `own` are dropped, and so are unaligned words at a
    cut edge (a word the other chunk also heard, or half of one). rescue: the last `rescue` left
    words count as own (the previous chunk's final text lost them at its end).
    """
    lo, hi = len(left) - min(rescue, len(left)), len(left) + len(own)
    keep: list[str] = []
    for tag, i1, i2, j1, j2 in _opcodes(words, (*left, *own, *right)):
        for i in range(i1, i2):
            if tag == "equal":
                ok = lo <= j1 + (i - i1) < hi
            elif tag == "replace":  # changed words: map them proportionally
                ok = lo <= j1 + (i - i1 + 0.5) * (j2 - j1) / (i2 - i1) < hi
            else:  # "delete": a final word nobody typed, at typed-word position j1
                ok = lo <= j1 <= hi and not (j1 == lo and left_cut) and not (j1 == hi and right_cut)
            if ok:
                keep.append(words[i])
    return keep


def tail_missing(final_words: Sequence[str], own: Sequence[str]) -> int:
    """How many of the last typed words of a chunk its final text has no word for (the model
    dropped the end of the audio before a cut: a known RNNT failure)."""
    covered = -1
    for tag, i1, i2, j1, j2 in _opcodes(final_words, own):
        if tag in ("equal", "replace") and j2 > j1:
            covered = max(covered, j2 - 1)
    return len(own) - 1 - covered


def mark_of(words: Sequence[str], typed: Sequence[str], index: int) -> str | None:
    """Trailing punctuation of the final word that matches typed[index] exactly, None if none does."""
    for tag, i1, i2, j1, j2 in _opcodes(words, typed):
        if tag == "equal" and j1 <= index < j2:
            return split_trail(words[i1 + index - j1])[1]
    return None


def set_last_mark(text: str, mark: str) -> str:
    """`text` with the trailing punctuation of its last word replaced by `mark`."""
    head, _, last = text.rpartition(" ")
    core, _trail = split_trail(last)
    if not norm(core):
        return text
    return f"{head} {core}{mark}" if head else core + mark


def _ends_sentence(text: str) -> bool:
    t = text.rstrip()
    return bool(t) and t[-1] in SENTENCE_END


def _agree_case(before: str, piece: str, sent_start: bool) -> str:
    """First live word after a corrected chunk: its capital letter follows the corrected text.

    sent_start: the live text had a sentence end before this word (so its capital letter came
    from that, not from the word itself, like a name).
    """
    if _ends_sentence(before):
        return piece[:1].upper() + piece[1:] if piece[:1].islower() else piece
    if sent_start and piece[:1].isupper() and not piece[1:2].isupper():
        return piece[:1].lower() + piece[1:]
    return piece


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


@dataclass
class _Phrase:
    """One live segment: from a VAD onset (or a cut) to a pause (or the next cut)."""

    start: int
    end: int | None = None
    cut: bool = False  # ended at a cut inside speech, not at a pause
    chunk: int = -1  # closed chunk it belongs to; -1 = the open chunk
    closed: bool = False  # ended at a VAD pause (not at a cut, not at stop)


@dataclass
class _Tok:
    """One typed word, as typed (a deferred punctuation mark is appended when it is typed)."""

    text: str
    phrase: int
    sent_start: bool  # the live text before it ended a sentence (or it is the first word)


@dataclass
class _Chunk:
    """A final unit: whole phrases, decoded once by the final model."""

    first: int  # first and last phrase
    last: int
    a: int  # decoded span in samples, pads (and overlap across a cut) included
    b: int
    cut: bool  # ends at a cut inside speech
    left_cut: bool = False  # starts at a cut inside speech
    tail_missing: int = 0  # own words at the cut that its final text lost (the next chunk keeps them)
    job: Future | None = None
    final: str | None = None  # final text in use; None = the live text stays
    state: str = "decoding"  # decoding | final | kept_live | late | skipped | failed
    level: float | None = None  # speech level, dBFS
    decode_s: float = 0.0


class DictationEngine:
    def __init__(
        self,
        cfg: Config,
        live: Recognizer,
        vad: StreamingVad,
        final: Recognizer | None = None,
        on_commit: Callable[[str], None] | None = None,
        on_level: Callable[[float], None] | None = None,
        on_revise: Callable[[str], None] | None = None,
        warm_up: bool = True,
    ):
        self.cfg = cfg
        self.live = live
        self.vad = vad
        self.final = final if cfg.final_pass else None
        self.on_commit = on_commit
        self.on_level = on_level
        self.on_revise = on_revise

        self.vad.reset()
        self.audio = AudioBuffer()
        self._rest = np.zeros(0, dtype=np.float32)
        self.live_text = ""  # what the live pass typed, before any correction (diagnostics)
        self._text = ""  # the current text as the consumer has it (see `text`)
        # live pass
        self.in_speech = False
        self.seg_start = 0
        self.last_seg_end = 0
        self.last_speech_end = 0
        self.silence = 0
        self.last_decode_at = 0
        self.stab = Stabilizer()
        self.continuation = False
        self._first_emit_in_segment = True
        self._held = ""  # trailing mark of the last typed word, typed together with the next word
        self._phrases: list[_Phrase] = []
        self._toks: list[_Tok] = []
        # final pass
        self._chunks: list[_Chunk] = []
        self._collected = 0  # chunks before this index have their result handled
        self._open_first = 0  # first phrase of the open chunk (== len(self._phrases) when it is empty)
        self._prev_chunk_end = 0  # padded end of the last closed chunk
        self._prev_cut: int | None = None  # the last closed chunk ended at this cut inside speech
        self._spec: tuple[int, int, int, Future] | None = None  # speculative decode: (phrases, a, b, job)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="final-pass") if self.final else None
        self._stop_pool: ThreadPoolExecutor | None = None
        self._finished = False
        # diagnostics (no text is logged anywhere by default)
        self.commit_log: list[tuple[float, str]] = []
        self.revise_log: list[tuple[float, str]] = []
        self.decode_times: list[float] = []
        self.levels: list[float] = []  # speech level (dBFS) of every decoded chunk
        self.gain_count = 0  # decodes that got the quiet-input gain
        self.dedup_drops = 0
        self.stop_info: dict = {}
        if warm_up:
            self._warm_up()

    def _s(self, seconds: float) -> int:
        return int(seconds * SAMPLE_RATE)

    @property
    def audio_time(self) -> float:
        return self.audio.size / SAMPLE_RATE

    @property
    def text(self) -> str:
        """The current text: final text of every corrected chunk, then the live text of the rest."""
        return self._text

    @property
    def chunk_spans(self) -> list[tuple[float, float, str]]:
        """(start s, end s, state) of every final chunk as decoded (pads and overlaps included)."""
        return [(c.a / SAMPLE_RATE, c.b / SAMPLE_RATE, c.state) for c in self._chunks]

    @property
    def chunk_info(self) -> list[dict]:
        """Diagnostics per final chunk (no text)."""
        return [{"a": c.a / SAMPLE_RATE, "b": c.b / SAMPLE_RATE, "state": c.state, "cut": c.cut,
                 "left_cut": c.left_cut, "level": c.level, "decode_s": c.decode_s} for c in self._chunks]

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
        """Report finished chunk corrections, then re-recognize the open phrase if enough new audio
        arrived. At most one live decode per call, always over the newest audio: a decode that took
        longer than decode_interval_s is simply followed by the next one (no backlog)."""
        if self.on_revise is not None and self._collected < len(self._chunks):
            self._collect()
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
                self.silence = 0
                self.last_speech_end = end
                self.last_decode_at = start
                self._drop_spec()  # speech resumed before the pause closed the chunk
                self._new_phrase(max(start - self._s(cfg.preroll_s), self.last_seg_end), continuation=False)
            elif self._open_first < len(self._phrases) and end - self.last_seg_end >= self._s(cfg.chunk_gap_s):
                self._close_chunk(len(self._phrases) - 1)
            return
        if p < cfg.vad_neg_threshold:
            self.silence += len(frame)
        else:
            self.silence = 0
            self.last_speech_end = end
        if self.silence >= self._s(cfg.live_min_silence_s):
            self._close_segment(min(self.last_speech_end + self._s(cfg.tail_pad_s), end))
        else:
            self._check_cap()

    # ---------------------------------------------------------------- live pass
    def _new_phrase(self, start: int, continuation: bool) -> None:
        self._phrases.append(_Phrase(start))
        self.seg_start = start
        self.stab = Stabilizer()
        self.continuation = continuation
        self._first_emit_in_segment = True

    def _prepare(self, audio: np.ndarray) -> tuple[np.ndarray, float, bool]:
        """Audio for a recognizer (quiet-input gain if enabled), its speech level, gain applied?"""
        level = speech_level_dbfs(audio)
        if self.cfg.quiet_gain:
            g = quiet_gain(level)
            if g != 1.0:
                return np.clip(audio * g, -1.0, 1.0).astype(np.float32), level, True
        return audio, level, False

    def _recognize(self, a: int, b: int) -> list[Word]:
        x = self.audio.view(a, b)
        if self.cfg.quiet_gain:
            x, _, gained = self._prepare(x)
            self.gain_count += gained
        t = time.perf_counter()
        words = self.live.words(x)
        self.decode_times.append(time.perf_counter() - t)
        return words

    def _decode_open(self) -> None:
        words = self._recognize(self.seg_start, self.audio.size)
        texts = [w.text for w in words]
        off = self.stab.offset(texts)
        prev = texts[off - 1] if 1 <= off <= len(texts) else None
        quiet = self.silence >= self._s(EARLY_SILENCE_S)  # early last word
        self._emit_words(self.stab.update(texts, settle_last=quiet), prev, final=False)
        self.last_decode_at = self.audio.size
        if self.audio.size - self.seg_start >= self._s(self.cfg.max_live_segment_s) or self._phrase_over_cap():
            self._force_cut(words, texts)

    def _cut_limit_s(self) -> float:
        """Latest cut (seconds into the open phrase) that keeps its chunk, decoded with the overlap
        across the cut, within the cap."""
        if self._pool is None or self._open_first != len(self._phrases) - 1:
            return float("inf")  # no final pass, or earlier phrases close the chunk first (_check_cap)
        return (self._chunk_start() + self._cap() - self._s(CUT_OVERLAP_S) - self.seg_start) / SAMPLE_RATE

    def _force_cut(self, words: list[Word], texts: list[str]) -> None:
        """Long phrase without pauses: cut it at a settled word boundary with reliable timing."""
        off = self.stab.offset(texts)
        win = (self.audio.size - self.seg_start) / SAMPLE_RATE
        latest = min(win - STABLE_EDGE_S, self._cut_limit_s())
        j = min(off, len(words) - 1)
        while j > 0 and words[j].start > latest:
            j -= 1
        if 0 < j <= off:
            cut = self.seg_start + self._s((words[j - 1].end + words[j].start) / 2)
            if cut > self.seg_start:
                # the settled words after the cut stay as context of the next phrase, not typed again
                self._cut_phrase(cut, list(texts[j:off]))
                return
        if self._phrase_over_cap():
            self._overflow_cut(words, texts, off)

    def _overflow_cut(self, words: list[Word], texts: list[str], off: int) -> None:
        """Nothing settled at a reliable boundary and the phrase is getting too long for its chunk
        (or, without the final pass, twice max_live_segment_s): cut at the last word boundary that
        fits, settling the words before it."""
        win = (self.audio.size - self.seg_start) / SAMPLE_RATE
        limit = min(win, self._cut_limit_s())
        j = len(words) - 1
        while j > 0 and (words[j - 1].end + words[j].start) / 2 > limit:
            j -= 1
        if j > 0:
            cut = self.seg_start + self._s((words[j - 1].end + words[j].start) / 2)
            seed: list[str] = []
            if j > off:
                new = list(texts[off:j])
                self.stab.committed.extend(new)
                self._emit_words(new, texts[off - 1] if off >= 1 else None, final=False)
            else:
                seed = list(texts[j:off])
            self._cut_phrase(max(cut, self.seg_start + 1), seed)
            return
        prev = texts[off - 1] if 1 <= off <= len(texts) else None
        self._emit_words(self.stab.finalize(texts), prev, final=False)
        end_s = words[-1].end + 0.05 if words else 0.0
        cut = min(self.audio.size, self.seg_start + self._s(max(end_s, min(limit, win))))
        self._cut_phrase(max(cut, self.seg_start + 1), [])

    def _cut_phrase(self, cut: int, seed: list[str]) -> None:
        old = len(self._phrases) - 1
        self._phrases[old].end = cut
        self._phrases[old].cut = True
        self._new_phrase(cut, continuation=True)
        if seed:
            self.stab.seed(seed)
            # those words were typed in the old phrase, but their audio is after the cut
            k = len(seed)
            for t in reversed(self._toks):
                if k == 0 or t.phrase != old:
                    break
                t.phrase = old + 1
                k -= 1
        if self._pool is not None:
            long_enough = cut - self._phrases[self._open_first].start >= self._s(self.cfg.chunk_soft_max_s)
            if long_enough or self.audio.size + self._pad() - self._chunk_start() > self._cap():
                self._close_chunk(old)

    def _close_segment(self, seg_end: int) -> None:
        self._phrases[-1].end = seg_end
        self._phrases[-1].closed = True
        if self._pool is not None:  # start the final decode first: it runs during the closing live decode
            if seg_end - self._phrases[self._open_first].start >= self._s(self.cfg.chunk_soft_max_s):
                self._close_chunk(len(self._phrases) - 1)
            elif SPECULATE:  # the chunk as it will be if this pause closes it
                a, b = self._chunk_start(), min(self.audio.size, seg_end + self._pad())
                if b - a >= self._s(MIN_CHUNK_S):
                    job = self._pool.submit(self._final_job, self.final, self.audio.view(a, b).copy())
                    self._spec = (len(self._phrases), a, b, job)
        words = self._recognize(self.seg_start, seg_end)
        texts = [w.text for w in words]
        off = self.stab.offset(texts)
        prev = texts[off - 1] if 1 <= off <= len(texts) else None
        self._emit_words(self.stab.finalize(texts), prev, final=True)
        self.in_speech = False
        self.silence = 0
        self.last_seg_end = seg_end

    def _drop_spec(self) -> None:
        if self._spec is not None:
            self._spec[3].cancel()  # not started: never runs; running: its result is not used
            self._spec = None

    def _take_spec(self, c: _Chunk) -> Future | None:
        """The speculative job, if it decoded exactly this chunk."""
        spec, self._spec = self._spec, None
        if spec is not None and spec[0] == c.last + 1 and spec[1] == c.a and spec[2] == c.b:
            return spec[3]
        if spec is not None:
            spec[3].cancel()
        return None

    # ---------------------------------------------------------------- typing (live words -> deltas)
    def _emit_words(self, new: Sequence[str], prev: str | None, final: bool) -> None:
        """Deferred punctuation: the trailing mark of the last settled word is held back and typed
        with the next word (or at the end of the phrase), taken from the newer guess (`prev` is the
        previous word as that guess spells it)."""
        punct = split_trail(prev)[1] if prev is not None else self._held
        words = [w for w in new if w]
        if words and not final:
            words[-1], self._held = split_trail(words[-1])
        elif final:
            self._held = ""
        if punct and self._toks and (words or final) and not self.live_text.rstrip().endswith(punct):
            self._append_mark(punct)
        if words:
            self._emit(words)

    def _emit(self, words: list[str]) -> None:
        words = [w for w in words if w]
        if words and self.continuation and self._first_emit_in_segment and self._toks:
            first = norm(words[0])
            if first and first == norm(self._toks[-1].text):
                words = words[1:]  # safety net: after a cut the model heard the last typed word again
                self.dedup_drops += 1
        if not words:
            return
        delta = join_delta(self.live_text, words, continuation=self.continuation and self._first_emit_in_segment)
        self._first_emit_in_segment = False
        if not delta:
            return
        body = delta[1:] if delta.startswith(" ") else delta
        words = [body[: len(words[0])], *words[1:]]  # join_delta may have lowered the first letter
        sent = not self.live_text.strip() or _ends_sentence(self.live_text)
        phrase = len(self._phrases) - 1
        for w in words:
            self._toks.append(_Tok(w, phrase, sent))
            sent = _ends_sentence(w)
        self.live_text += delta
        self._deliver()

    def _append_mark(self, mark: str) -> None:
        self._toks[-1].text += mark
        self.live_text += mark
        self._deliver()

    def _deliver(self) -> None:
        """Send the consumer what the live words added to the current text."""
        new = self._render()
        old = self._text
        if new == old:
            return  # e.g. a mark for a word whose chunk was already corrected
        self._text = new
        if new.startswith(old):
            delta = new[len(old) :]
            self.commit_log.append((self.audio_time, delta))
            if self.on_commit:
                self.on_commit(delta)
        else:  # not expected: keep the consumer in sync anyway
            log.warning("engine: live words did not extend the current text")
            self._send_revise(new)

    def _render(self) -> str:
        """The current text: per chunk its final text (if in use) or the words typed for it.

        Sentence ends at a pause between chunks: a final text often ends without a mark (the
        model sees no next word), while the next chunk starts with a capital letter. After a final
        text with no trailing mark, a pause (not a cut) and a capital letter that the next final
        text, or the live pass, put at a sentence start, a period is added. At stop the text
        gets one too if its last phrase ended at a pause.
        """
        toks, phrases = self._toks, self._phrases
        out = ""
        after_final = False

        def add(piece: str, final: bool, first: _Tok | None, pause_before: bool) -> None:
            nonlocal out, after_final
            piece = piece.strip()
            if not piece:
                return
            if out and after_final:
                starts = final or (first is not None and first.sent_start)
                if pause_before and starts and piece[:1].isupper() and not split_trail(out)[1]:
                    out += "."
                if not final and first is not None:
                    piece = _agree_case(out, piece, first.sent_start)
            out = f"{out} {piece}" if out else piece
            after_final = final

        i, n = 0, len(toks)
        for ci, c in enumerate(self._chunks):
            j = i
            while j < n and phrases[toks[j].phrase].chunk == ci:
                j += 1
            pause = ci > 0 and not self._chunks[ci - 1].cut
            if c.final is not None:
                add(c.final, True, None, pause)
            else:
                add(" ".join(t.text for t in toks[i:j]), False, toks[i] if j > i else None, pause)
            i = j
        add(" ".join(t.text for t in toks[i:]), False, toks[i] if i < n else None,
            bool(self._chunks) and not self._chunks[-1].cut)
        if (self._finished and after_final and out and not split_trail(out)[1]
                and phrases[self._chunks[-1].last].closed):
            out += "."
        return out

    def _send_revise(self, target: str) -> None:
        self.revise_log.append((self.audio_time, target))
        if self.on_revise:
            self.on_revise(target)

    # ---------------------------------------------------------------- final pass (chunks)
    def _pad(self) -> int:
        return self._s(self.cfg.chunk_pad_s)

    def _cap(self) -> int:
        return self._s(self.cfg.chunk_soft_max_s + CHUNK_CAP_EXTRA_S)

    def _chunk_start(self) -> int:
        """Decoded start of the open chunk: the left pad never reaches into the previous chunk,
        except across a cut inside speech (overlap; those words are dropped again by stitch())."""
        if self._prev_cut is not None:
            return max(0, self._prev_cut - self._s(CUT_OVERLAP_S))
        return max(self._prev_chunk_end, self._phrases[self._open_first].start - self._pad())

    def _phrase_over_cap(self) -> bool:
        if self._pool is None:
            return self.audio.size - self.seg_start >= self._s(2 * self.cfg.max_live_segment_s)
        return (self._open_first == len(self._phrases) - 1
                and self.audio.size + self._pad() - self._chunk_start() > self._cap())

    def _check_cap(self) -> None:
        """During speech: if the open chunk would outgrow the cap, close it at its last phrase boundary."""
        if self._pool is None or self._open_first >= len(self._phrases) - 1:
            return
        if self.audio.size + self._pad() - self._chunk_start() > self._cap():
            self._close_chunk(len(self._phrases) - 2)

    def _close_chunk(self, last: int, submit: bool = True) -> _Chunk | None:
        """Close the open chunk after phrase `last` and send it to the final session."""
        first = self._open_first
        if self._pool is None or last < first:
            return None
        ph = self._phrases[last]
        end = ph.end if ph.end is not None else self.audio.size
        a = self._chunk_start()
        if ph.cut:  # no pad at a cut inside speech (it would slice the next word), only the overlap
            b = max(end, min(end + self._s(CUT_OVERLAP_S), self.audio.size, a + self._cap()))
        else:
            nxt = self._phrases[last + 1].start if last + 1 < len(self._phrases) else self.audio.size
            b = min(self.audio.size, end + self._pad(), max(end, nxt))
        idx = len(self._chunks)
        for p in self._phrases[first : last + 1]:
            p.chunk = idx
        c = _Chunk(first, last, a, b, ph.cut, left_cut=self._prev_cut is not None)
        self._chunks.append(c)
        self._open_first = last + 1
        self._prev_chunk_end = b
        self._prev_cut = end if ph.cut and CUT_OVERLAP_S > 0 else None
        if b - a < self._s(MIN_CHUNK_S):
            c.state = "skipped"
            self._drop_spec()
        elif submit:
            c.job = self._take_spec(c) or self._pool.submit(self._final_job, self.final, self.audio.view(a, b).copy())
        return c

    def _final_job(self, rec: Recognizer, audio: np.ndarray) -> tuple[str, float, bool, float]:
        """Runs on a worker thread: (text, speech level dBFS, gain applied, seconds)."""
        t = time.perf_counter()
        x, level, gained = self._prepare(audio)
        text = rec.text(x)
        return text, level, gained, time.perf_counter() - t

    def _chunk_words(self, ci: int) -> list[str]:
        return [t.text for t in self._toks if self._phrases[t.phrase].chunk == ci and norm(t.text)]

    def _apply(self, ci: int, c: _Chunk, extra: Sequence[str] = ()) -> bool:
        """Take a finished chunk job; True if its final text is now in use.

        extra: words of the chunk not typed yet (the open phrase at stop)."""
        try:
            text, level, gained, dt = c.job.result()
        except Exception:
            log.exception("final pass failed on a chunk")
            c.state = "failed"
            return False
        c.level, c.decode_s = level, dt
        self.levels.append(level)
        self.gain_count += gained
        live_words = self._chunk_words(ci) + [w for w in extra if norm(w)]
        words = text.split()
        prev, mark = None, None
        if CUT_OVERLAP_S > 0 and (c.cut or c.left_cut):
            left, right = [], []
            for t in self._toks:
                k = self._phrases[t.phrase].chunk
                if norm(t.text) and k != ci:
                    (left if -1 < k < ci else right).append(t.text)
            left = left[-STITCH_CONTEXT:] if c.left_cut else []
            right = right[:STITCH_CONTEXT] if c.cut else []
            if c.left_cut and ci > 0 and self._chunks[ci - 1].final is not None:
                prev = self._chunks[ci - 1]
            rescue = min(prev.tail_missing, len(left)) if prev is not None else 0
            if prev is not None and len(left) - 1 - rescue >= 0:
                # the punctuation after the previous chunk's last word: this decode heard what follows
                mark = mark_of(words, (*left, *live_words, *right), len(left) - 1 - rescue)
            words = stitch(words, left, live_words, right, c.left_cut, c.cut, rescue)
            if c.cut:
                c.tail_missing = tail_missing(words, live_words)
        if len([w for w in words if norm(w)]) < 0.5 * len(live_words):
            c.state = "kept_live"  # a burst of deleted words: keep what was typed
            return False
        if prev is not None and mark is not None and prev.final:
            prev.final = set_last_mark(prev.final, mark)
        c.final = " ".join(words)
        c.state = "final"
        return True

    def _collect(self) -> None:
        """poll(): take the chunk results that are ready (in order) and report the new text."""
        changed = False
        while self._collected < len(self._chunks):
            c = self._chunks[self._collected]
            if c.state == "decoding":
                if c.job is None or not c.job.done():
                    break
                changed |= self._apply(self._collected, c)
            self._collected += 1
        if changed:
            new = self._render()
            if new != self._text:
                self._text = new
                self._send_revise(new)

    # ---------------------------------------------------------------- stop
    def finish(self, budget_s: float | None = None) -> str:
        """Stop: return the final text of the whole recording (also reported through on_revise).

        The open chunk is decoded once by the final model (the speculative decode of it is used
        if it is already running or done). That decode and older chunk jobs get at most
        `budget_s` (cfg.stop_budget_s) together; late chunks keep their live text. Without the
        final pass the last phrase is settled by one more live decode, as before.
        """
        if self._finished:
            return self._text
        self._finished = True
        t0 = time.perf_counter()
        budget = self.cfg.stop_budget_s if budget_s is None else budget_s
        deadline = t0 + max(0.0, budget)
        if len(self._rest):
            self.audio.append(self._rest)
            self._rest = np.zeros(0, dtype=np.float32)
        if self._pool is None:
            if self.in_speech:
                self._close_segment(self.audio.size)
            self.stop_info = {"finish_s": time.perf_counter() - t0}
            self._log_session()
            return self._text

        pending = list(self.stab.pending) if self.in_speech else []  # last guess, not typed yet
        stop_chunk = None
        reused = False
        if self._open_first < len(self._phrases):
            if self.in_speech:
                self._phrases[-1].end = self.audio.size
                self.in_speech = False
            stop_chunk = self._close_chunk(len(self._phrases) - 1, submit=False)
            if stop_chunk is not None and stop_chunk.state == "decoding":
                spec = self._take_spec(stop_chunk)
                if spec is not None and spec.cancel():
                    spec = None  # still queued behind an older chunk job: decode it now instead
                reused = spec is not None
                if spec is None:  # the final session, from a thread of its own (concurrent runs are safe)
                    self._stop_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop-decode")
                    spec = self._stop_pool.submit(
                        self._final_job, self.final, self.audio.view(stop_chunk.a, stop_chunk.b).copy())
                stop_chunk.job = spec
        self._drop_spec()
        jobs = [c.job for c in self._chunks[self._collected :] if c.state == "decoding" and c.job is not None]
        if jobs:
            wait_futures(jobs, timeout=max(0.0, deadline - time.perf_counter()))
        waited = time.perf_counter() - t0

        for ci in range(self._collected, len(self._chunks)):  # in order: a chunk may fix its predecessor
            c = self._chunks[ci]
            if c is stop_chunk:
                ready = c.state == "decoding" and c.job is not None and c.job.done()
                if not (ready and self._apply(ci, c, pending)):
                    if c.state == "decoding":
                        c.state = "late"
                    self._emit_words(pending, None, final=True)  # live text + the last guess's words
                continue
            if c.state != "decoding":
                continue
            if c.job is not None and c.job.done():
                self._apply(ci, c)
            else:
                c.state = "late"  # its result is dropped when it arrives
                if c.job is not None:
                    c.job.cancel()
        self._collected = len(self._chunks)

        target = self._render()
        if target != self._text:
            self._text = target
            self._send_revise(target)
        self.stop_info = {
            "finish_s": time.perf_counter() - t0,
            "waited_s": waited,
            "open_state": stop_chunk.state if stop_chunk else None,
            "open_audio_s": (stop_chunk.b - stop_chunk.a) / SAMPLE_RATE if stop_chunk else 0.0,
            "open_decode_s": stop_chunk.decode_s if stop_chunk else 0.0,
            "open_speculative": reused,  # the open chunk's decode had started before stop
            "late": sum(1 for c in self._chunks if c.state == "late"),
        }
        self._log_session()
        return target

    def close(self) -> None:
        for pool in (self._pool, self._stop_pool):
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)

    # ---------------------------------------------------------------- helpers
    def _warm_up(self) -> None:
        """Paged-out weights cost 0.1-0.6 s on the first decode: decode 0.3 s of silence in each
        session now, off the session thread, while the user starts speaking."""
        z = np.zeros(int(WARMUP_S * SAMPLE_RATE), dtype=np.float32)

        def run(fn: Callable[[np.ndarray], object]) -> None:
            try:
                fn(z)
            except Exception:
                log.exception("warm-up decode failed")

        threading.Thread(target=run, args=(self.live.words,), name="warm-up", daemon=True).start()
        if self._pool is not None and self.final is not None:
            self._pool.submit(run, self.final.text)  # runs before any chunk job

    def _log_session(self) -> None:
        states = Counter(c.state for c in self._chunks)
        lens = [(c.b - c.a) / SAMPLE_RATE for c in self._chunks]
        lv = self.levels
        level = (f"median {statistics.median(lv):.1f} min {min(lv):.1f} dBFS (n={len(lv)})" if lv else "n/a")
        log.info("engine: audio=%.1fs phrases=%d chunks=%d %s max_chunk=%.1fs speech_level %s quiet_gain=%s/%d "
                 "dedup=%d stop=%.2fs", self.audio_time, len(self._phrases), len(self._chunks),
                 dict(states), max(lens, default=0.0), level, self.cfg.quiet_gain, self.gain_count,
                 self.dedup_drops, self.stop_info.get("finish_s", 0.0))
