"""May this person be messaged on this channel? Checked before a draft is made for sending, and
again at the moment a person taps send. Fails closed.

The region policy row (filled from the client's adviser) says which consent bases allow contact on
which channel, and the footer to add. The shape this module reads, under `rules["contact"]`:

    {"whatsapp": {"bases": ["opt_in", "enquiry"], "footer": "Reply STOP to opt out"},
     "email":    {"bases": ["opt_in"], "footer": "..."}}

Anything else (no policy row, no `contact` key, a channel missing, a malformed entry) means no basis:
the lead is shown to the seller for a personal reply only, and nothing is drafted for sending.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from thallo.contracts.enums import ConsentBasis, ConsentScope, MessageChannel
from thallo.contracts.models import Consent, RegionPolicy

NO_POLICY = "no_region_policy"
NO_RULE = "policy_has_no_contact_rule_for_channel"
NO_CONSENT = "no_contact_consent"
WITHDRAWN = "consent_withdrawn"
BASIS = "basis_not_allowed"


@dataclass(frozen=True)
class ConsentDecision:
    allowed: bool
    reason: str | None = None
    consent_id: str | None = None
    footer: str | None = None
    policy_version: int | None = None


def current_policy(policies: Iterable[RegionPolicy], region_code: str, now: datetime) -> RegionPolicy | None:
    live = [p for p in policies if p.region_code == region_code and p.effective_from <= now]
    return max(live, key=lambda p: p.version, default=None)


def _channel_rule(policy: RegionPolicy, channel: MessageChannel) -> tuple[frozenset[str], str | None] | None:
    contact = policy.rules.get("contact") if isinstance(policy.rules, dict) else None
    entry = contact.get(channel.value) if isinstance(contact, dict) else None
    if not isinstance(entry, dict):
        return None
    bases = entry.get("bases")
    footer = entry.get("footer")
    if not isinstance(bases, list) or not all(isinstance(b, str) for b in bases):
        return None
    if footer is not None and not isinstance(footer, str):
        return None
    allowed = frozenset(b for b in bases if b in {x.value for x in ConsentBasis} and b != ConsentBasis.NONE.value)
    return allowed, footer


def decide(*, person_id: str, channel: MessageChannel, recipient: str, consents: Iterable[Consent],
           covered: Mapping[str, Collection[tuple[MessageChannel, str]]], policy: RegionPolicy | None,
           now: datetime) -> ConsentDecision:
    """`covered` maps a consent id to the exact (channel, recipient) pairs it covers. A consent
    that does not name this recipient on this channel does not count, so a number or address that
    was never consented can never be messaged on another contact point's consent."""
    if policy is None:
        return ConsentDecision(False, NO_POLICY)
    rule = _channel_rule(policy, channel)
    if rule is None or not rule[0]:
        return ConsentDecision(False, NO_RULE, policy_version=policy.version)
    bases, footer = rule
    relevant = [c for c in consents if c.person_id == person_id and ConsentScope.CONTACT in c.scopes
                and (c.channel is None or c.channel is channel)]
    # A withdrawal on ANY of the person's contact records for this channel (with or without a
    # recipient row) wins over every yes given before it.
    latest_withdrawal = max((c.withdrawn_at for c in relevant if c.withdrawn_at and c.withdrawn_at <= now),
                            default=None)
    # The recipient binding only picks the positive grant.
    mine = [c for c in relevant if c.given_at <= now and (channel, recipient) in covered.get(c.id, ())]
    if not mine:
        return ConsentDecision(False, WITHDRAWN if latest_withdrawal else NO_CONSENT, policy_version=policy.version)
    live = [c for c in mine if c.withdrawn_at is None or c.withdrawn_at > now]
    if not live:
        return ConsentDecision(False, WITHDRAWN, policy_version=policy.version)
    usable = [c for c in live if c.basis.value in bases
              and (latest_withdrawal is None or c.given_at > latest_withdrawal)]
    if not usable:
        return ConsentDecision(False, WITHDRAWN if latest_withdrawal else BASIS, policy_version=policy.version)
    best = max(usable, key=lambda c: (c.given_at, c.id))
    return ConsentDecision(True, None, best.id, footer, policy.version)


__all__ = ["BASIS", "NO_CONSENT", "NO_POLICY", "NO_RULE", "WITHDRAWN", "ConsentDecision", "current_policy", "decide"]
