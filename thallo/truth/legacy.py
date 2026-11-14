"""Truth ledger of the original engine, kept until WP-06 ports the engine to `truth.compiler`.

The original rule was "a factual sentence must carry a [[C-0001]] reference", which made the
sentence splitter a security boundary: any wrong merge let an unmarked sentence ride on its
neighbour's marker. `compile` now delegates to the token-level `truth.compiler.scan`. Every digit,
number word, superlative, comparative or protected name outside a marker fails wherever it sits,
and a reference supports nothing but itself. The segmenter only names the sentence in the error
message. The return shape, (claim ids, errors), is unchanged.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from thallo.contracts import sha256
from thallo.contracts.enums import Language
from thallo.truth.compiler import Severity, scan
from thallo.truth.segment import segment as _segment  # display only; tests swap it

REF = re.compile(r"\[\[(C-[0-9]+)\]\]")


@dataclass
class Claim:
    id: str
    text: str
    source: str
    evidence_hash: str
    expires_at: datetime | None = None
    verified: bool = False


class ClaimLedger:
    def __init__(self) -> None:
        self.claims: dict[str, Claim] = {}

    def add(self, text: str, source: str, evidence: str,
            expires_at: datetime | None = None) -> Claim:
        claim = Claim(f"C-{len(self.claims) + 1:04d}", text, source, sha256(evidence), expires_at)
        self.claims[claim.id] = claim
        return claim

    def verify(self, claim_id: str) -> None:
        self.claims[claim_id].verified = True

    def problem(self, claim_id: str, now: datetime) -> str | None:
        claim = self.claims.get(claim_id)
        if claim is None:
            return f"{claim_id} does not exist"
        if not claim.verified:
            return f"{claim_id} is not verified"
        if claim.expires_at and now >= claim.expires_at:
            return f"{claim_id} has expired"
        return None

    def compile(self, text: str, now: datetime,
                protected_names: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
        """Returns claim IDs used and errors. Any error blocks the draft."""
        text = unicodedata.normalize("NFC", text)
        ids = REF.findall(text)
        errors = [p for p in (self.problem(i, now) for i in ids) if p]
        findings = [f for f in scan(text, language=Language.EN, marker_spans=[m.span() for m in REF.finditer(text)],
                                    protected_names=protected_names) if f.severity is Severity.BLOCK]
        if findings:
            sentences = _segment(text, Language.EN)
            grouped: dict[str, list[str]] = {}
            for f in findings:
                where = next((s.text for s in sentences if s.start <= f.start < s.end), f.match)
                grouped.setdefault(where, []).append(f"{f.kind.value} {f.match!r}")
            errors += [f"unsupported: {where} ({', '.join(why)})" for where, why in grouped.items()]
        return sorted(set(ids)), errors

    def fingerprint(self, claim_ids: list[str] | tuple[str, ...]) -> list[str]:
        return [self.claims[i].evidence_hash for i in claim_ids if i in self.claims]


def render(text: str) -> str:
    """Public text, with claim references removed."""
    return re.sub(r" ?\[\[C-[0-9]+\]\]", "", text)
