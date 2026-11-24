"""Pure text helpers: joining live words, planning the final in-place fix, merging with the
user's own edits, WER."""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from livetype.stabilizer import norm

_TOKEN = re.compile(r"\S+\s*|\s+")


def common_affixes(old: str, new: str) -> tuple[int, int]:
    """Lengths of the common prefix and (non-overlapping) common suffix."""
    p, m = 0, min(len(old), len(new))
    while p < m and old[p] == new[p]:
        p += 1
    s = 0
    while s < m - p and old[-1 - s] == new[-1 - s]:
        s += 1
    return p, s


def merge_user_edits(base: str, user: str, final: str) -> str:
    """Apply the final pass's corrections (base→final) to the text as the user left it
    (base→user), skipping corrections that touch words the user changed by hand."""
    if user == base:
        return final
    if final == base:
        return user
    b, u, f = _TOKEN.findall(base), _TOKEN.findall(user), _TOKEN.findall(final)

    def ops(other: list[str]) -> list[tuple[int, int, list[str]]]:
        # align by the bare word, then report case/punctuation changes as one-word replacements
        sm = difflib.SequenceMatcher(None, [norm(t) for t in b], [norm(t) for t in other], autojunk=False)
        res = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                res.append((i1, i2, other[j1:j2]))
                continue
            for k in range(i2 - i1):
                if b[i1 + k].strip() != other[j1 + k].strip():
                    res.append((i1 + k, i1 + k + 1, [other[j1 + k]]))
        return res

    user_ops, fix_ops = ops(u), ops(f)

    def clash(a: tuple[int, int, list[str]], c: tuple[int, int, list[str]]) -> bool:
        (a1, a2, _), (c1, c2, _) = a, c
        if a1 == a2 and c1 == c2:
            return a1 == c1
        if a1 == a2:
            return c1 < a1 < c2
        if c1 == c2:
            return a1 < c1 < a2
        return a1 < c2 and c1 < a2

    trail = re.compile(r"([^\w\s]*)(\s*)$")
    accepted = []
    for op in fix_ops:
        if any(clash(op, uo) for uo in user_ops):
            continue
        i1, i2, rep = op
        if rep and i2 > i1 and any(uo[0] == i2 for uo in user_ops):
            # the user changed what follows: don't add punctuation that belonged to that context
            rm, bm = trail.search(rep[-1]), trail.search(b[i2 - 1])
            rep = [*rep[:-1], rep[-1][: rm.start()] + bm.group(1) + rm.group(2)]
        accepted.append((i1, i2, rep))
    out, pos = [], 0
    for i1, i2, rep in sorted(user_ops + accepted, key=lambda o: (o[0], o[1])):
        out += b[pos:i1] + rep
        pos = i2
    out += b[pos:]
    return re.sub(r" {2,}", " ", "".join(out))

SENTENCE_END = ".!?…"


def join_delta(typed_so_far: str, words: Sequence[str], continuation: bool = False) -> str:
    """Text to type for newly settled words (with the leading space if needed).

    `continuation` = the phrase was split mid-sentence, so the model wrongly
    capitalized its first word; lower it unless the text before ended a sentence.
    """
    words = [w for w in words if w]
    if not words:
        return ""
    if continuation and typed_so_far and typed_so_far.rstrip()[-1:] not in SENTENCE_END:
        w = words[0]
        if w[:1].isupper() and (len(w) == 1 or not w[1:2].isupper()):
            words = [w[:1].lower() + w[1:], *words[1:]]
    text = " ".join(words)
    if typed_so_far and not typed_so_far.endswith((" ", "\n")):
        text = " " + text
    return text


def join_chunks(chunks: Sequence[str]) -> str:
    text = " ".join(c.strip() for c in chunks if c and c.strip())
    return text[:1].upper() + text[1:]


@dataclass(frozen=True)
class Replacement:
    """Keystrokes that turn `old` (just typed, caret at its end) into `new`.

    left:   press Left this many times (skip the unchanged ending)
    select: then Shift+Left this many times (select the changed middle)
    insert: type this (replaces the selection)
    right:  press Right this many times (caret back to the end)
    """

    left: int
    select: int
    insert: str
    right: int

    @property
    def is_noop(self) -> bool:
        return self.select == 0 and not self.insert


def plan_replacement(old: str, new: str) -> Replacement:
    p = 0
    m = min(len(old), len(new))
    while p < m and old[p] == new[p]:
        p += 1
    s = 0
    while s < m - p and old[-1 - s] == new[-1 - s]:
        s += 1
    return Replacement(left=s, select=len(old) - p - s, insert=new[p : len(new) - s], right=s)


class SimEditor:
    """Tiny text-field model (caret + selection) used to verify replacements."""

    def __init__(self, text: str = "", caret: int | None = None) -> None:
        self.text = text
        self.caret = len(text) if caret is None else caret
        self.anchor: int | None = None

    def _sel(self) -> tuple[int, int] | None:
        if self.anchor is None or self.anchor == self.caret:
            return None
        return min(self.anchor, self.caret), max(self.anchor, self.caret)

    def left(self, shift: bool = False) -> None:
        if shift:
            if self.anchor is None:
                self.anchor = self.caret
            self.caret = max(0, self.caret - 1)
            return
        sel = self._sel()
        self.anchor = None
        self.caret = sel[0] if sel else max(0, self.caret - 1)

    def right(self, shift: bool = False) -> None:
        if shift:
            if self.anchor is None:
                self.anchor = self.caret
            self.caret = min(len(self.text), self.caret + 1)
            return
        sel = self._sel()
        self.anchor = None
        self.caret = sel[1] if sel else min(len(self.text), self.caret + 1)

    def type(self, s: str) -> None:
        sel = self._sel()
        a, b = sel if sel else (self.caret, self.caret)
        self.text = self.text[:a] + s + self.text[b:]
        self.caret = a + len(s)
        self.anchor = None

    def backspace(self) -> None:
        sel = self._sel()
        if sel:
            self.type("")
        elif self.caret > 0:
            self.text = self.text[: self.caret - 1] + self.text[self.caret :]
            self.caret -= 1

    def apply(self, r: Replacement) -> None:
        for _ in range(r.left):
            self.left()
        for _ in range(r.select):
            self.left(shift=True)
        if r.insert:
            self.type(r.insert)
        elif r.select:
            self.backspace()
        for _ in range(r.right):
            self.right()


def wer(ref: str, hyp: str) -> float:
    """Word error rate, ignoring case and punctuation."""
    r = [norm(w) for w in ref.split() if norm(w)]
    h = [norm(w) for w in hyp.split() if norm(w)]
    if not r:
        return float(bool(h))
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hw in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
    return prev[-1] / len(r)
