"""Every outbound action passes here. Nothing else may call a publisher."""
from __future__ import annotations

from datetime import datetime

from cortex_operator.contracts import BrandSettings, Decision, Item, Passport, sha256
from cortex_operator.truth import ClaimLedger


def content_hash(item: Item, rule_version: int, claims: ClaimLedger,
                 claim_ids: list[str] | tuple[str, ...]) -> str:
    return sha256([item.text, item.media, item.channel, rule_version,
                   list(claim_ids), claims.fingerprint(claim_ids)])


class Gate:
    def __init__(self, claims: ClaimLedger) -> None:
        self.claims = claims
        self.killed = False

    def evaluate(self, item: Item, passport: Passport | None, brand: BrandSettings,
                 now: datetime, spend_eur: float = 0.0) -> Decision:
        if self.killed:
            return Decision(False, "global kill switch")
        if brand.paused:
            return Decision(False, "brand paused")
        if item.channel not in brand.channels:
            return Decision(False, "channel not allowed")
        if passport is None:
            return Decision(False, "no approval")
        expected = content_hash(item, passport.rule_version, self.claims, passport.claim_ids)
        if expected != passport.content_hash:
            return Decision(False, "content or evidence changed since approval")
        for claim_id in passport.claim_ids:
            if problem := self.claims.problem(claim_id, now):
                return Decision(False, problem)
        if brand.spent_this_week_eur + spend_eur > brand.weekly_spend_cap_eur and spend_eur > 0:
            return Decision(False, "spend cap")
        return Decision(True, "ok")
