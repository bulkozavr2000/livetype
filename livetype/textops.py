"""Pure text helpers: joining live words, planning the final in-place fix, merging with the
user's own edits, finding our text in a field's text, WER."""

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


# ------------------------------------------------------------------ finding our text in a field
# Rich editors do not always show exactly what was typed: a plain contenteditable stores a space
# typed at the end of a line as U+00A0 and turns it back into a space later, input rules make
# "..." into "…" and quotes into typographic ones, an empty block reads as U+FFFC, line breaks
# come as "\r\n" or "\r", Word may capitalize a word. Matching is done on a folded copy where
# every character remembers which characters of the original it came from.
_FOLD = {
    **dict.fromkeys((0x00A0, 0x2002, 0x2003, 0x2007, 0x2008, 0x2009, 0x200A, 0x202F, 0x3000, 0x09), " "),
    **dict.fromkeys((0x2018, 0x2019, 0x201A, 0x201B, 0x2032), "'"),
    **dict.fromkeys((0x201C, 0x201D, 0x201E, 0x201F, 0x00AB, 0x00BB, 0x2033), '"'),
    **dict.fromkeys((0x2010, 0x2011, 0x2012, 0x2013, 0x2212), "-"),
    0x2014: "--",  # the usual input rule makes "--" into an em dash
    0x2026: "...",
    **dict.fromkeys((0x2028, 0x2029, 0x0B, 0x0C, 0x85), "\n"),
    **dict.fromkeys((0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0xFFFC), ""),  # invisible
}
_WS = " \n"


@dataclass(frozen=True)
class Folded:
    s: str  # the folded text
    starts: list[int]  # for each folded character: where its source starts in the original
    ends: list[int]  # ... and where it ends
    size: int  # length of the original

    def span(self, i: int, j: int) -> tuple[int, int]:
        """The original's [start, end) behind the folded [i, j)."""
        if j > i:
            return self.starts[i], self.ends[j - 1]
        p = self.ends[i - 1] if i > 0 else 0
        return p, p


_SPECIAL = re.compile("[\r" + "".join(re.escape(chr(c)) for c in _FOLD) + "]")


def fold(text: str) -> Folded:
    """Lower case, one kind of space, quote, dash and line break, no invisible characters."""
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []

    def plain(a: int, b: int) -> None:  # ordinary characters: lower case, one to one
        low = text[a:b].lower()
        if len(low) == b - a:
            out.append(low)
            starts.extend(range(a, b))
            ends.extend(range(a + 1, b + 1))
            return
        for k in range(a, b):  # a few letters lower to two characters: keep those as they are
            c = text[k].lower()
            out.append(c if len(c) == 1 else text[k])
            starts.append(k)
            ends.append(k + 1)

    pos = 0
    for m in _SPECIAL.finditer(text):
        k = m.start()
        if k < pos:  # the "\n" of a "\r\n" that was already taken
            continue
        plain(pos, k)
        if text[k] == "\r":
            rep, pos = "\n", (k + 2 if text[k + 1:k + 2] == "\n" else k + 1)
        else:
            rep, pos = _FOLD[ord(text[k])], k + 1
        for c in rep:
            out.append(c)
            starts.append(k)
            ends.append(pos)
    plain(pos, len(text))
    return Folded("".join(out), starts, ends, len(text))


def _occurrences(hay: str, s: str) -> list[int]:
    res, k = [], hay.find(s)
    while k >= 0:
        res.append(k)
        k = hay.find(s, k + 1)
    return res


def _tail_end(hay: str, text: str, start: int = 0) -> int:
    """End of the last occurrence of the end of `text` in hay[start:], or -1."""
    if not text:
        return -1
    for n in (60, 30, 15):
        k = hay.rfind(text[-n:], start)
        if k >= 0:
            return k + len(text[-n:])
        if len(text) <= n:
            break
    return -1


def _between(hay: str, head: str, after: str, text: str, tail: int) -> tuple[int, int] | None:
    """Our text = whatever lies between the context captured when the dictation started."""
    variants = [(head, after)]
    loose = (head.rstrip(_WS), after.lstrip(_WS))  # without the whitespace next to the cursor
    if loose != (head, after):
        variants.append(loose)
    for h, a in variants:
        h, a = (h if h.strip() else ""), (a if a.strip() else "")
        start = 0
        if h:
            ends = [k + len(h) for k in _occurrences(hay, h)]
            if not ends:
                continue
            before = [e for e in ends if e <= tail]
            # the occurrence that leaves about our text's length up to our text's end
            start = min(before, key=lambda e: abs(tail - e - len(text))) if before else ends[0]
        if a:
            end = hay.find(a, start)
            if end < 0:
                continue
        else:  # dictating at the very end: stop at our tail, not at text typed after it
            t = _tail_end(hay, text, start)
            end = t if t >= 0 else len(hay)
        return start, end
    return None


def _by_ends(hay: str, text: str, tail: int) -> tuple[int, int] | None:
    """Our text by its own end and beginning (the context around it was changed)."""
    if tail < 0 or not text:
        return None
    first = text[: min(30, len(text))]
    starts = [k for k in _occurrences(hay, first) if k + len(first) <= tail]
    if starts:
        k = min(starts, key=lambda k: abs(tail - k - len(text)))
        if abs(tail - k - len(text)) <= len(text) // 2 + 20:
            return k, tail
    return max(0, tail - len(text)), tail


def _snap(hay: str, i: int, j: int, text: str, after_ws: bool) -> tuple[int, int]:
    """Drop whitespace at the edges that is not part of our text (it belongs to the context).
    If nothing but whitespace is left (our text was deleted), the place for new words is
    before it when the text after the cursor started with whitespace (an empty paragraph reads
    as "\\n": new words go before that "\\n"), otherwise after it."""
    lead = not text or text[0] not in _WS
    trail = not text or text[-1] not in _WS
    for side in ("trail", "lead") if after_ws else ("lead", "trail"):
        if side == "lead" and lead:
            while i < j and hay[i] in _WS:
                i += 1
        if side == "trail" and trail:
            while j > i and hay[j - 1] in _WS:
                j -= 1
    return i, j


def locate_region(field: str, head: str, after: str, text: str) -> tuple[int, int] | None:
    """Where our dictated `text` is in `field` (the field's whole text), as [start, end) indices
    into `field`, or None if it is gone.

    `head` / `after` = the field's text right before / after the cursor when the dictation
    started: our text is whatever lies between them now, edits by the user included. If that
    context was changed, our text is found by its own end and beginning.
    """
    f = fold(field)
    hay, ft, fa = f.s, fold(text).s, fold(after).s
    tail = _tail_end(hay, ft)
    for span in (_between(hay, fold(head).s, fa, ft, tail), _by_ends(hay, ft, tail)):
        if span is not None:
            i, j = _snap(hay, *span, ft, fa[:1] in _WS and fa != "")
            if j - i <= 3 * len(ft) + 200:
                return f.span(i, j)
    return None


def split_final(base: str, at: int, final: str) -> tuple[str, str]:
    """Split the corrected `final` text where `base` (the live text it corrects) is split at `at`."""
    words = list(re.finditer(r"\S+", final))
    old, new = base[:at].split(), base[at:].split()  # the new part may follow without a space
    n_old = len(old)
    sm = difflib.SequenceMatcher(None, [norm(w) for w in old + new], [norm(m.group()) for m in words],
                                 autojunk=False)
    j = len(words)
    for tag, i1, i2, j1, _ in sm.get_opcodes():
        if i2 > n_old:
            j = j1 + (n_old - i1) if tag == "equal" else j1
            break
    cut = words[j].start() if j < len(words) else len(final)
    if base[at:at + 1].isspace():  # the new part starts with a space in the field too
        while cut > 0 and final[cut - 1].isspace():
            cut -= 1
    return final[:cut], final[cut:]

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
