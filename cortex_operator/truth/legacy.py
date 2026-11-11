"""Truth ledger of the original engine: factual sentences must reference a live, verified claim.

Kept so the engine and its tests run unchanged until WP-06 ports them to `truth.compiler`.
Sentence splitting now uses `truth.segment`, which fixes the BUILD_PLAN 0.1 bug: "3.5 tonnes
[[C-0001]]." used to split at the decimal point and flag "3." as unsupported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from cortex_operator.contracts import sha256
from cortex_operator.truth.segment import segment

REF = re.compile(r"\[\[(C-\d+)\]\]")
CHECKABLE = re.compile(r"\d|%|\b(first|only|best|fastest|largest|guaranteed|proven)\b", re.I)


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

    def compile(self, text: str, now: datetime) -> tuple[list[str], list[str]]:
        """Returns claim IDs used and errors. Any error blocks the draft."""
        ids = REF.findall(text)
        errors = [p for p in (self.problem(i, now) for i in ids) if p]
        for sentence in (s.text for s in segment(text)):
            if CHECKABLE.search(REF.sub("", sentence)) and not REF.search(sentence):
                errors.append(f"unsupported: {sentence.strip()}")
        return sorted(set(ids)), errors

    def fingerprint(self, claim_ids: list[str] | tuple[str, ...]) -> list[str]:
        return [self.claims[i].evidence_hash for i in claim_ids if i in self.claims]


def render(text: str) -> str:
    """Public text, with claim references removed."""
    return re.sub(r" ?\[\[C-\d+\]\]", "", text)
