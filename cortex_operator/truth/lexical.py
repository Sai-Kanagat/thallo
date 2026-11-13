"""The LEXICAL layer of the compile step: best effort, tuned for precision.

It catches number words, superlatives, comparisons, universal claims and protected names from
per-language lists (`lexicon.py`). It is never complete; its backstops are the cross-family
second reader (WP-07) and the weekly 20-sentence hand audit. Precision matters as much as recall
here: a block or flag on ordinary copy trains approvers to tap through.

Verdicts:
- BLOCK: words that are almost never anything but a number, superlative or comparison (L1); an
  ambiguous word that counts something (L2, below); a protected name or its distinctive token
  (L6); a list word split by separators ("b/e/s/t", "t e n").
- FLAG: first, only, top, latest, ottimo, massimo, garantito and their kin when they count
  nothing; comparatives without "than" or "di"; universal claims in a sentence with a verb of
  performance or a product noun (L3).
- Nothing: articles, pronouns and prepositions that count nothing ("una nuova sede", "sei
  pronto", "prima di"), fixed phrases (L4) and the tenant's `allowed_terms` (L5).

An ambiguous count word (one, sei, una, prima, secondo, half, ...) counts something when it
modifies a quantity noun or unit (the next word, or after up to two words that are not function
words), sits next to a slot, follows a rank marker (n., no., numero, number, #) or a time noun
("Day one"), or when a quantity noun plus a colon, or a copula, comes before it in the same
clause ("Showrooms: we run one", "count is one"), or Italian "ne" does ("Ne abbiamo aperte sei").
The articles un, una, uno and Tamil ஒரு count only before a measure noun (anno, mese, km) or
"sola", "unica".

Everything runs on one tokenisation of the folded text, single pass, linear in its length.
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence

from cortex_operator.contracts.enums import Language
from cortex_operator.truth.findings import FindingKind, Severity
from cortex_operator.truth.lexicon import (
    ARTICLE_COUNT_WORDS,
    COPULAS,
    FUNCTION_WORDS,
    LEFT,
    MEASURE_NOUNS,
    QUANTITY_NOUNS,
    RANK_MARKERS,
    RIGHT,
    ROMAN_NEIGHBOURS,
    ROMAN_WORDS,
    SOLE,
    TA_MEASURE_STEMS,
    TA_QUANTITY_STEMS,
    TIME_NOUNS,
    WORD,
    Category,
    block_patterns,
    block_word,
    exception_pattern,
    fold,
    fold_str,
    italian_collective,
    merged,
    roman_numeral,
)
from cortex_operator.truth.units import ISO_4217, SYMBOL_UNITS, UNITS

SLOT_CHAR = "\x00"
TOKEN = re.compile(rf"{WORD.pattern}|{SLOT_CHAR}+")
CLAUSE_BREAKS = frozenset(".!?;" + "".join(map(chr, (0x2026, 0x0964, 0x3002, 0x0A, 0x0D, 0x0B, 0x0C, 0x85,
                                                      0x2028, 0x2029))))
HYPHENS = frozenset("-" + "".join(map(chr, range(0x2010, 0x2016))) + chr(0x2212))
APOSTROPHES = frozenset("'" + chr(0x2019))
_UNIT_WORDS = frozenset({fold_str(u) for u in SYMBOL_UNITS} | {fold_str(c) for c in ISO_4217}
                        | {fold_str(w) for forms in UNITS.values() for pair in forms.values() for w in pair})
COUNTED = QUANTITY_NOUNS | _UNIT_WORDS
MEASURED = MEASURE_NOUNS | _UNIT_WORDS
IT_ELIDED_TENS = frozenset({"vent", "trent", "quarant", "cinquant", "sessant", "settant", "ottant", "novant"})
RECORD = frozenset({"record"})
MAX_JOIN_TOKENS = 8
MAX_JOIN_CHARS = 32

# Words of a protected name too common to protect on their own ("Rossi Trasporti": Rossi alone is
# protected, Trasporti alone is not).
COMMON_NAME_WORDS = frozenset(fold_str(w) for w in (
    "auto autos motors motor group gruppo trasporti transport transports logistics logistica service services "
    "servizi servizio italia italy italiana italiano company compagnia industries industria holding "
    "international internazionale ltd limited corp sons figli fratelli brothers center centre centro store "
    "shop garage cars veicoli trucks truck camion furgoni commerciale commerciali dealer rent noleggio global "
    "solutions soluzioni sistemi systems tech digital consulting studio energia energy costruzioni edilizia "
    "impianti agency agenzia trading import export food foods farm nord north south east west nuova nuovo new "
    "city citta store spa srl snc sas").split())


def _is_letter(ch: str) -> bool:
    return ch != SLOT_CHAR and WORD.match(ch) is not None


class _Text:
    """One tokenisation of the folded text, with the per-token facts every rule needs."""

    def __init__(self, text: str, markers: Sequence[tuple[int, int]]) -> None:
        chars = list(text)
        for a, b in markers:
            chars[a:b] = SLOT_CHAR * (b - a)
        self.text = text
        self.folded, self.origin = fold("".join(chars))
        spans = [m.span() for m in TOKEN.finditer(self.folded)]
        self.starts = [a for a, _ in spans]
        self.ends = [b for _, b in spans]
        self.words = [self.folded[a:b] for a, b in spans]
        self.slot = [w[0] == SLOT_CHAR for w in self.words]
        self.n = len(spans)
        # gap[k]: the folded characters between token k - 1 and token k
        self.gap = [self.folded[(self.ends[k - 1] if k else 0):self.starts[k]] for k in range(self.n)]
        clause = 0
        self.clause = []
        for k in range(self.n):
            if k and any(c in CLAUSE_BREAKS for c in self.gap[k]):
                clause += 1
            self.clause.append(clause)

    def original(self, a: int, b: int) -> tuple[int, int]:
        """Original span of the folded span [a, b)."""
        return self.origin[a], self.origin[b - 1] + 1

    def tokens_in(self, a: int, b: int) -> range:
        return range(bisect_right(self.ends, a), bisect_left(self.starts, b))

    def joined(self, k: int, *, allow_newline: bool = True) -> bool:
        """Token k follows token k - 1 directly: only spaces, hyphens or apostrophes between."""
        if k <= 0 or k >= self.n:
            return False
        for c in self.gap[k]:
            if c in HYPHENS or c in APOSTROPHES:
                continue
            if c.isspace() and (allow_newline or c not in CLAUSE_BREAKS):
                continue
            return False
        return True


def _is_quantity(word: str, article: bool) -> bool:
    if article:
        return word in MEASURED or word in SOLE or word.startswith(TA_MEASURE_STEMS)
    return word in COUNTED or word.startswith(TA_QUANTITY_STEMS)


class _Scan:
    def __init__(self, text: str, markers: Sequence[tuple[int, int]], language: Language,
                 protected_names: Iterable[str], allowed_terms: Iterable[str]) -> None:
        self.t = _Text(text, markers)
        self.language = language
        self.names = list(protected_names)
        self.out: list[tuple[FindingKind, Severity, int, int, str]] = []
        t = self.t
        self.quiet = [False] * t.n  # fixed phrases and allowed terms: no word finding
        self.allowed = [False] * t.n  # allowed terms: no lexical finding at all
        self.blocked = [False] * t.n
        for m in exception_pattern().finditer(t.folded):
            self._mark(self.quiet, *m.span())
        allowed = _allowed_pattern(allowed_terms)
        if allowed is not None:
            for m in allowed.finditer(t.folded):
                self._mark(self.quiet, *m.span())
                self._mark(self.allowed, *m.span())
        self._clause_state()

    def _mark(self, flags: list[bool], a: int, b: int) -> None:
        for k in self.t.tokens_in(a, b):
            flags[k] = True

    def _clause_state(self) -> None:
        """For each token, what came before it in its clause: a quantity noun and then a colon,
        any quantity noun, Italian "ne"."""
        t = self.t
        self.qn_colon = [False] * t.n
        self.qn_seen = [False] * t.n
        self.ne_seen = [False] * t.n
        qn = colon = ne = False
        for k in range(t.n):
            if k and t.clause[k] != t.clause[k - 1]:
                qn = colon = ne = False
            if qn and ":" in t.gap[k]:
                colon = True
            self.qn_colon[k], self.qn_seen[k], self.ne_seen[k] = colon, qn, ne
            w = t.words[k]
            if not t.slot[k]:
                qn = qn or _is_quantity(w, False)
                ne = ne or (w == "ne" and self.language is Language.IT)

    # ------------------------------------------------------------ helpers

    def add(self, kind: FindingKind, severity: Severity, a: int, b: int, message: str, *,
            phrase: bool = False) -> bool:
        """Add a finding on the folded span [a, b) unless a quiet token is inside it. A block
        pattern (`phrase`) is only silenced by an allowed term, not by a fixed phrase, so "no one
        sells more" still blocks although "no one" alone is a fixed phrase."""
        toks = self.t.tokens_in(a, b)
        if any((self.allowed if phrase else self.quiet)[k] for k in toks):
            return False
        if severity is Severity.BLOCK:
            for k in toks:
                self.blocked[k] = True
        oa, ob = self.t.original(a, b)
        self.out.append((kind, severity, oa, ob, message))
        return True

    def add_token(self, kind: FindingKind, severity: Severity, k: int, message: str) -> bool:
        return self.add(kind, severity, self.t.starts[k], self.t.ends[k], message)

    def counts(self, k: int) -> bool:
        """L2: the ambiguous word at token k counts something."""
        t = self.t
        word = t.words[k]
        article = word in ARTICLE_COUNT_WORDS
        soft = set(" ,-")
        if k + 1 < t.n and t.slot[k + 1] and set(t.gap[k + 1]) <= soft:
            return True
        if k and t.slot[k - 1] and set(t.gap[k]) <= soft:
            return True
        if "#" in t.gap[k]:
            return True
        if k and t.words[k - 1] in RANK_MARKERS and set(t.gap[k]) <= set(" .") and not t.slot[k - 1]:
            return True
        j = k
        for _ in range(3):
            j += 1
            if j >= t.n or t.slot[j] or not t.joined(j):
                break
            if _is_quantity(t.words[j], article):
                return True
            if t.words[j] in FUNCTION_WORDS:
                break
        if self.qn_colon[k]:
            return True
        if not article and k and t.joined(k) and t.words[k - 1] in COPULAS and self.qn_seen[k - 1]:
            return True
        if self.ne_seen[k] and word not in ("prima", "secondo", "primo"):
            return True
        return bool(not article and k and t.joined(k) and t.words[k - 1] in TIME_NOUNS)

    def _next_word(self, k: int) -> str:
        t = self.t
        return t.words[k + 1] if k + 1 < t.n and not t.slot[k + 1] and t.joined(k + 1) else ""

    def _prev_word(self, k: int) -> str:
        t = self.t
        return t.words[k - 1] if k and not t.slot[k - 1] and t.joined(k) else ""

    # ------------------------------------------------------------ passes

    def run(self) -> list[tuple[FindingKind, Severity, int, int, str]]:
        self.patterns()
        comparatives, universals = self.words()
        self.second_tier(comparatives, universals)
        self.split_words()
        self.protected()
        return self.out

    def patterns(self) -> None:
        for category, pattern in block_patterns(self.language):
            kind = FindingKind(category.value)
            for m in pattern.finditer(self.t.folded):
                if m.end() > m.start():
                    self.add(kind, Severity.BLOCK, *m.span(), f"{kind.value} outside a slot: {m.group(0)!r}",
                             phrase=True)

    def words(self) -> tuple[list[int], list[int]]:
        t, lang = self.t, self.language
        count_words = merged("count", lang)
        flag_words = merged("flag", lang)
        comparative_words = merged("comparative", lang)
        universal_words = merged("universal", lang)
        comparatives: list[int] = []
        universals: list[int] = []
        for k in range(t.n):
            if t.slot[k] or self.quiet[k] or self.blocked[k]:
                continue
            w = t.words[k]
            original = t.text[slice(*t.original(t.starts[k], t.ends[k]))]
            category = block_word(w, lang)
            if category is None and lang is Language.IT:
                if w == "meta" and chr(0xE0) in original.lower():
                    category = Category.NUMBER_WORD  # meta with the accent is "half"
                elif w in IT_ELIDED_TENS and t.gap[k + 1:k + 2] and t.gap[k + 1][:1] in APOSTROPHES:
                    category = Category.NUMBER_WORD  # vent'anni
                elif italian_collective(w) and self._next_word(k) in ("di", "d"):
                    category = Category.NUMBER_WORD  # una trentina di
            if category is not None:
                kind = FindingKind(category.value)
                self.add_token(kind, Severity.BLOCK, k, f"{kind.value} outside a slot: {w!r}")
                continue
            if roman_numeral(w) and self._roman(k, w, original):
                self.add_token(FindingKind.NUMBER_WORD, Severity.BLOCK, k, f"Roman numeral outside a slot: {w!r}")
                continue
            if w in RECORD and (_is_quantity(self._next_word(k), False) or _is_quantity(self._prev_word(k), False)):
                self.add_token(FindingKind.SUPERLATIVE, Severity.BLOCK, k, "record claim outside a slot")
                continue
            if w in count_words:
                if self.counts(k):
                    self.add_token(FindingKind.NUMBER_WORD, Severity.BLOCK, k, f"counts something: {w!r}")
                elif self._flags(k, w, flag_words):
                    self.add_token(FindingKind.AMBIGUOUS_NUMBER_WORD, Severity.FLAG, k,
                                   f"may claim a rank: {w!r}; check it")
                continue
            if w in flag_words:
                self.add_token(FindingKind.SUPERLATIVE, Severity.FLAG, k, f"may be a superlative: {w!r}; check it")
            elif w in comparative_words or (lang is Language.IT and w in ("piu", "meno") and self._compares(k)):
                comparatives.append(k)
            elif w in universal_words:
                universals.append(k)
        return comparatives, universals

    def _roman(self, k: int, word: str, original: str) -> bool:
        if len(word) >= 3:
            return word not in ROMAN_WORDS
        if len(word) == 2 and original.isupper():
            return self._next_word(k) in ROMAN_NEIGHBOURS or self._prev_word(k) in ROMAN_NEIGHBOURS
        return False

    def _flags(self, k: int, word: str, flag_words: frozenset[str]) -> bool:
        t = self.t
        if word == "first" and t.gap[k + 1:k + 2] and t.gap[k + 1].startswith(","):
            return False  # "First, book a test drive": sequence, not rank
        if word == "prima":
            return self._prev_word(k) in ("la", "le", "una") or self._next_word(k) in ("in", "tra", "fra", "nel",
                                                                                         "nella", "a", "ad")
        return word in flag_words

    def _compares(self, k: int) -> bool:
        nxt = self._next_word(k)
        return bool(nxt) and nxt not in FUNCTION_WORDS and self._prev_word(k) not in ("di", "al", "in")

    def second_tier(self, comparatives: list[int], universals: list[int]) -> None:
        t = self.t
        blocked_comparison = {t.clause[k] for k in range(t.n) if self.blocked[k] and t.words[k] in
                              ("than", "di", "che", "del", "della", "dei", "rispetto", "confronto", "versus", "vs")}
        for k in comparatives:
            if t.clause[k] not in blocked_comparison and not self.blocked[k]:
                self.add_token(FindingKind.COMPARATIVE, Severity.FLAG, k,
                               f"comparison without a stated reference: {t.words[k]!r}; check it")
        performance = merged("performance", self.language)
        busy = {t.clause[k] for k in range(t.n) if not t.slot[k] and t.words[k] in performance}
        for k in universals:
            if t.clause[k] in busy and not self.blocked[k]:
                self.add_token(FindingKind.UNIVERSAL_CLAIM, Severity.FLAG, k, f"universal claim: {t.words[k]!r}")

    def split_words(self) -> None:
        """L6: rejoin a list word split by separators ("b/e/s/t", "be_st") or spread over single
        letters ("t e n", "b est")."""
        t, lang = self.t, self.language

        def strong(word: str) -> bool:
            return block_word(word, lang) is not None

        def try_join(first: int, last: int) -> bool:
            toks = range(first, last + 1)
            if any(self.blocked[k] or self.quiet[k] for k in toks):
                return False
            joined = "".join(t.words[k] for k in toks)
            if len(joined) <= MAX_JOIN_CHARS and strong(joined):
                return self.add(FindingKind.SPACED_LETTERS, Severity.BLOCK, t.starts[first], t.ends[last],
                                f"split word: {joined!r}")
            return False

        # (a) a chunk with no whitespace: separators only between the letters
        k = 0
        while k < t.n:
            j = k
            while j + 1 < t.n and not t.slot[j + 1] and t.gap[j + 1] and not any(
                    c.isspace() or c == SLOT_CHAR for c in t.gap[j + 1]) and j + 1 - k < MAX_JOIN_TOKENS:
                j += 1
            if j > k and not t.slot[k]:
                try_join(k, j)
            k = j + 1
        # (b) runs where every neighbour pair includes a single letter
        k = 0
        while k < t.n:
            j = k
            while (j + 1 < t.n and not t.slot[j] and not t.slot[j + 1]
                   and (len(t.words[j]) == 1 or len(t.words[j + 1]) == 1)
                   and not any(c in CLAUSE_BREAKS or _is_letter(c) for c in t.gap[j + 1])):
                j += 1
            for a in range(k, j):
                if self.blocked[a] or self.quiet[a]:
                    continue
                joined = t.words[a]
                for b in range(a + 1, min(j, a + MAX_JOIN_TOKENS) + 1):
                    if self.blocked[b] or self.quiet[b]:
                        break
                    joined += t.words[b]
                    if len(joined) > MAX_JOIN_CHARS:
                        break
                    if strong(joined) and self.add(FindingKind.SPACED_LETTERS, Severity.BLOCK, t.starts[a],
                                                   t.ends[b], f"split word: {joined!r}"):
                        break
            k = j + 1

    def protected(self) -> None:
        """L6: protected names, whole or by their distinctive tokens, on a letters-only view so
        separators, hyphenated line breaks and spacing cannot split them."""
        if not self.names:
            return
        t = self.t
        folded = t.folded
        idx = [i for i, c in enumerate(folded) if _is_letter(c)]
        compact = "".join(folded[i] for i in idx)
        size = len(folded)

        def boundary_before(j: int) -> bool:
            i = idx[j]
            return i == 0 or not _is_letter(folded[i - 1])

        def boundary_after(e: int) -> bool:  # e: compact index just past the match
            i = idx[e - 1] + 1
            return i >= size or not _is_letter(folded[i])

        patterns: list[str] = []
        for name in self.names:
            parts = [p for p in re.split(r"[\W_\d]+", fold_str(name)) if p]
            whole = "".join(parts)
            if len(whole) >= 2:
                patterns.append(whole)
            patterns += [p for p in parts if len(p) >= 4 and p not in COMMON_NAME_WORDS and p != whole]
        seen: set[tuple[int, int]] = set()
        for pattern in sorted(set(patterns), key=len, reverse=True):
            j = compact.find(pattern)
            while j >= 0:
                e = j + len(pattern)
                end_ok = boundary_after(e) or (e < len(compact) and compact[e] == "s" and boundary_after(e + 1))
                if boundary_before(j) and end_ok:
                    a, b = idx[j], idx[e - 1] + 1
                    if not any(x < b and a < y for x, y in seen) and self.add(
                            FindingKind.NAMED_CUSTOMER, Severity.BLOCK, a, b, f"name outside a slot: {pattern!r}"):
                        seen.add((a, b))
                j = compact.find(pattern, j + 1)


def _allowed_pattern(terms: Iterable[str]) -> re.Pattern[str] | None:
    parts = []
    for term in terms:
        words = [re.escape(w) for w in re.split(r"[\s\-]+", fold_str(term).strip()) if w]
        if words:
            parts.append(LEFT + r"[\s\-]+".join(words) + RIGHT)
    if not parts:
        return None
    parts.sort(key=len, reverse=True)
    return re.compile("|".join(f"(?:{p})" for p in parts))


def scan_lexical(text: str, markers: Sequence[tuple[int, int]], language: Language,
                 protected_names: Iterable[str] = (), allowed_terms: Iterable[str] = ()
                 ) -> list[tuple[FindingKind, Severity, int, int, str]]:
    """Lexical findings as (kind, severity, start, end, message) on the original text."""
    return _Scan(text, markers, language, protected_names, allowed_terms).run()
