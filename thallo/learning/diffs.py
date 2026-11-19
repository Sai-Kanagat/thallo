"""Edit diffs, classified (BUILD_PLAN 2.7, "Edits become typed rules"). Pure code.

A diff between a draft and the human's final is one of: fact fix, typo, cut, word swap, structure
change, tone shift. The order of the tests matters:

1. FACT_FIX first: any change to a claim slot (`{{C-0012}}`) or to a digit is a fact fix, however
   small. Facts route to the truth library, never to voice rules.
2. TYPO is conservative, because `thallo.checks.agreement` reads a typo edit as "the reviewer found
   it fine": at most two words changed, each a near spelling of the other, or punctuation and
   spacing only.
3. STRUCTURE_CHANGE: the sentences or paragraphs were reordered, split or merged.
4. CUT: words only removed.
5. WORD_SWAP: short replacements (up to three words each side), nothing longer added.
6. TONE_SHIFT: anything else, a rewrite that kept the structure.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from thallo.contracts.enums import EditKind

SLOT = re.compile(r"\{\{C-\d{4,}\}\}")
DIGITS = re.compile(r"\d+")
TOKEN = re.compile(r"\{\{C-\d{4,}\}\}|[\w']+|[^\w\s]")
SENTENCE = re.compile(r"[^.!?\n]+[.!?]*")
SWAP_MAX = 3


@dataclass(frozen=True)
class EditDiff:
    kind: EditKind
    removed: tuple[str, ...]  # spans only in the draft (deletes, and the old side of replaces)
    added: tuple[str, ...]  # spans only in the final
    swaps: tuple[tuple[str, str], ...]  # (old, new) for short replacements
    ratio: float  # 0 is identical, 1 is a full rewrite


def tokens(text: str) -> list[str]:
    return TOKEN.findall(text.lower())


def _words(span: list[str]) -> str:
    return " ".join(t for t in span if re.match(r"[\w'{]", t))


def _sentences(text: str) -> list[str]:
    return [" ".join(tokens(s)) for s in SENTENCE.findall(text) if tokens(s)]


def _paragraphs(text: str) -> int:
    return len([p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()])


def _near(a: str, b: str) -> bool:
    return abs(len(a) - len(b)) <= 2 and difflib.SequenceMatcher(a=a, b=b, autojunk=False).ratio() >= 0.75


def classify(draft: str, final: str) -> EditDiff:
    a, b = tokens(draft), tokens(final)
    ops = [op for op in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes() if op[0] != "equal"]
    removed = tuple(w for _, i1, i2, _, _ in ops if (w := _words(a[i1:i2])))
    added = tuple(w for tag, _, _, j1, j2 in ops if tag in ("insert", "replace") and (w := _words(b[j1:j2])))
    swaps = tuple((_words(a[i1:i2]), _words(b[j1:j2])) for tag, i1, i2, j1, j2 in ops
                  if tag == "replace" and _words(a[i1:i2]) and _words(b[j1:j2]))
    ratio = 1 - difflib.SequenceMatcher(a=draft, b=final, autojunk=False).ratio()

    def out(kind: EditKind) -> EditDiff:
        return EditDiff(kind, removed, added, swaps, ratio)

    if sorted(SLOT.findall(draft)) != sorted(SLOT.findall(final)) or \
            sorted(DIGITS.findall(SLOT.sub("", draft))) != sorted(DIGITS.findall(SLOT.sub("", final))):
        return out(EditKind.FACT_FIX)
    word_ops = [op for op in ops if _words(a[op[1]:op[2]]) or _words(b[op[3]:op[4]])]
    if not word_ops:
        return out(EditKind.TYPO)  # punctuation or spacing only
    if len(word_ops) <= 2 and all(
            tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1 and _near(a[i1], b[j1])
            for tag, i1, i2, j1, j2 in word_ops):
        return out(EditKind.TYPO)
    sa, sb = _sentences(draft), _sentences(final)
    if _paragraphs(draft) != _paragraphs(final) or len(sa) != len(sb) or \
            (sorted(sa) == sorted(sb) and sa != sb):
        if not (len(sa) > len(sb) and all(s in sa for s in sb)):  # dropping whole sentences is a cut
            return out(EditKind.STRUCTURE_CHANGE)
    if all(tag == "delete" for tag, *_ in word_ops):
        return out(EditKind.CUT)
    if all(tag == "replace" and i2 - i1 <= SWAP_MAX and j2 - j1 <= SWAP_MAX
           for tag, i1, i2, j1, j2 in word_ops if tag != "delete"):
        return out(EditKind.WORD_SWAP)
    return out(EditKind.TONE_SHIFT)


__all__ = ["EditDiff", "classify", "tokens"]
