"""Which recognized words are already "settled" and can be typed.

The live model re-recognizes the current phrase every ~0.3 s. Its guess for the
last words keeps changing while you speak. A word is typed only when two
consecutive guesses agree on it (LocalAgreement-2) and it is not the very last
word of the guess (that one may be cut off mid-word).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


@dataclass(frozen=True)
class Word:
    text: str
    start: float  # seconds from the start of the decoded audio
    end: float


def norm(word: str) -> str:
    """Compare words ignoring case, punctuation and ё/е."""
    return _NON_WORD.sub("", word).lower().replace("ё", "е")


def words_from_tokens(tokens: Sequence[str], timestamps: Sequence[float]) -> list[Word]:
    """Glue sub-word tokens (" при", "вет", ".") into words with start/end times."""
    words: list[Word] = []
    cur = ""
    t0: float | None = None
    t1 = 0.0
    for tok, t in zip(tokens, timestamps):
        if tok[:1] in (" ", "▁"):
            if cur and t0 is not None:
                words.append(Word(cur, t0, t1))
            cur, t0 = "", None
            tok = tok.lstrip(" ▁")
        if not tok:
            continue
        if t0 is None:
            t0 = float(t)
        cur += tok
        t1 = float(t)
    if cur and t0 is not None:
        words.append(Word(cur, t0, t1))
    return words


class Stabilizer:
    """LocalAgreement-2 over the words of one open phrase (segment)."""

    def __init__(self) -> None:
        self.committed: list[str] = []
        self.pending: list[str] = []  # tentative words after the settled ones
        self._prev_tail: list[str] | None = None

    def offset(self, hyp: Sequence[str]) -> int:
        """Index in `hyp` where the not-yet-settled part starts.

        The model may merge/split a settled word in a later guess, so the
        boundary is searched around the expected position.
        """
        n = len(self.committed)
        if n == 0:
            return 0
        last = norm(self.committed[-1])
        for d in (0, -1, 1, -2, 2):
            i = n - 1 + d
            if 0 <= i < len(hyp) and norm(hyp[i]) == last:
                return i + 1
        return min(n, len(hyp))

    def update(self, hyp: Sequence[str]) -> list[str]:
        """Feed a new guess for the open phrase. Returns newly settled words."""
        tail = list(hyp[self.offset(hyp) :])
        new: list[str] = []
        if self._prev_tail is not None:
            limit = len(tail) - 1  # never settle the last word of a guess
            for i in range(min(limit, len(self._prev_tail))):
                if norm(self._prev_tail[i]) != norm(tail[i]):
                    break
                new.append(tail[i])  # take the newest spelling/punctuation
        self.committed.extend(new)
        self.pending = tail[len(new) :]
        self._prev_tail = self.pending
        return new

    def finalize(self, hyp: Sequence[str]) -> list[str]:
        """The phrase ended (pause): everything left is settled."""
        new = list(hyp[self.offset(hyp) :])
        self.committed.extend(new)
        self.pending = []
        self._prev_tail = None
        return new
