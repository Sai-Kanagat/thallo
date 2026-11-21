"""Subscribers and the issue's one send path (WP-29, BUILD_PLAN module 31).

`subscribe` records an opt-in for one exact email address through the lead desk's consent
recorder (a person records it, with evidence) and a subscriber row with a random unsubscribe token.

`send_issue` is the only place a newsletter `MessageSendRequest` is built:
1. A named human with a verified live session (`require_human(APPROVE)`).
2. A `GateDecision` from `gate.check_publish` that allows this issue's variant on channel
   newsletter. The body is exactly `decision.payload.text`, the approved text, plus a footer built
   here in code: the region policy's footer and the subscriber's own unsubscribe link.
3. For every subscriber, at that moment: not unsubscribed, and the consent decision (region policy,
   contact consent for this exact address on email, no withdrawal) allows it. Anyone else is
   skipped with the reason. The list is never taken from a cached export.
4. A `newsletter_delivery` row (unique per issue and subscriber) is written before the provider
   call, so a second tap sends nobody twice. Nothing is retried by the machine.
The provider sits behind the registry: manual (a mailto draft, the default) or a live email
connection that the operator switched on.

`unsubscribe` takes the token, marks the subscriber and withdraws the consent it relied on.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from thallo.contracts import connection as c
from thallo.contracts.actors import Actor, SessionVerifier, require_human
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Channel, ConsentBasis, HumanOnlyAction, MessageChannel
from thallo.contracts.ids import new_id
from thallo.contracts.models import Consent, Person, RegionPolicy, Tenant
from thallo.extensions.newsletter.records import NewsletterDelivery, NewsletterIssue, NewsletterSubscriber
from thallo.gate.decision import GateDecision
from thallo.leads import consent as consent_rules
from thallo.leads.desk import Desk, norm_email
from thallo.leads.records import ConsentRecipient, SendState

BASE_URL = re.compile(r"^https://[a-z0-9.-]+(/[A-Za-z0-9._~/-]*)?$")


class SendRefused(PermissionError):
    pass


def subscribe(tx: Any, *, tenant_id: str, clock: Clock, person_id: str, email: str, evidence: str,
              actor: Actor, verifier: SessionVerifier) -> NewsletterSubscriber:
    to = norm_email(email)
    if to is None:
        raise ValueError("not a usable email address")
    consent = Desk(tx, tenant_id, clock).record_consent(person_id, channel=MessageChannel.EMAIL, recipient=to,
                                                       basis=ConsentBasis.OPT_IN, evidence=evidence, actor=actor,
                                                       verifier=verifier)
    token = secrets.token_hex(32)
    old = tx.list_all(NewsletterSubscriber, recipient=to)
    if old:
        tx.update(NewsletterSubscriber, old[0].id, person_id=person_id, consent_id=consent.id, token=token,
                  unsubscribed_at=None)
        return tx.get(NewsletterSubscriber, old[0].id)
    return tx.insert(NewsletterSubscriber(id=new_id(clock), created_at=clock.now(), tenant_id=tenant_id,
                                          person_id=person_id, consent_id=consent.id, recipient=to, token=token))


def unsubscribe(tx: Any, *, token: str, clock: Clock) -> bool:
    if not re.fullmatch(r"[0-9a-f]{64}", token or ""):
        return False
    rows = tx.list_all(NewsletterSubscriber, token=token)
    if not rows:
        return False
    sub = rows[0]
    now = clock.now()
    if sub.unsubscribed_at is None:
        tx.update(NewsletterSubscriber, sub.id, unsubscribed_at=now)
    consent = tx.get(Consent, sub.consent_id)
    if consent is not None and consent.withdrawn_at is None:
        tx.update(Consent, consent.id, withdrawn_at=now)
    return True


def footer(policy_footer: str | None, unsubscribe_base: str, token: str) -> str:
    lines = [policy_footer] if policy_footer else []
    lines.append(f"Unsubscribe: {unsubscribe_base.rstrip('/')}/{token}")
    return "\n".join(lines)


@dataclass
class SendReport:
    issue_id: str
    sent: list[str] = field(default_factory=list)
    opened: list[dict[str, Any]] = field(default_factory=list)  # manual drafts: the person sends them
    failed: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (subscriber id, reason)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def send_issue(tx: Any, *, tenant_id: str, clock: Clock, issue_id: str, decision: GateDecision, actor: Actor,
               verifier: SessionVerifier, sender: Any, unsubscribe_base: str) -> SendReport:
    human = require_human(actor, HumanOnlyAction.APPROVE, verifier=verifier, tenant_id=tenant_id)
    issue = tx.get(NewsletterIssue, issue_id)
    if issue is None:
        raise SendRefused("no such issue")
    if not isinstance(decision, GateDecision) or not decision.allowed or decision.payload is None:
        raise SendRefused("the gate did not allow this issue")
    p = decision.payload
    if (decision.tenant_id != tenant_id or decision.variant_id != issue.variant_id
            or p.variant_id != issue.variant_id or p.channel != Channel.NEWSLETTER.value):
        raise SendRefused("the approved payload is not this newsletter issue")
    if not BASE_URL.match(unsubscribe_base or ""):
        raise SendRefused("an https unsubscribe address is required")
    tenant = tx.get(Tenant, tenant_id)
    now = clock.now()
    policy = consent_rules.current_policy(tx.list_all(RegionPolicy), tenant.region_code, now) if tenant else None
    covered: dict[str, set[tuple[MessageChannel, str]]] = {}
    for r in tx.list_all(ConsentRecipient):
        covered.setdefault(r.consent_id, set()).add((r.channel, r.recipient))
    done = {d.subscriber_id for d in tx.list_all(NewsletterDelivery, issue_id=issue.id)}
    report = SendReport(issue.id)
    for sub in sorted(tx.list_all(NewsletterSubscriber), key=lambda s: (s.created_at, s.id)):
        if sub.unsubscribed_at is not None:
            report.skipped.append((sub.id, "unsubscribed"))
            continue
        if sub.id in done:
            report.skipped.append((sub.id, "already_tapped"))
            continue
        person = tx.get(Person, sub.person_id)
        d = consent_rules.decide(person_id=sub.person_id, channel=MessageChannel.EMAIL, recipient=sub.recipient,
                                 consents=tx.list_all(Consent, person_id=sub.person_id), covered=covered,
                                 policy=policy, now=now) \
            if person else consent_rules.ConsentDecision(False, "no_person")
        if not d.allowed or d.consent_id is None:
            report.skipped.append((sub.id, d.reason or consent_rules.NO_CONSENT))
            continue
        body = p.text.rstrip() + "\n\n" + footer(d.footer, unsubscribe_base, sub.token)
        rec = tx.insert(NewsletterDelivery(id=new_id(clock), created_at=now, tenant_id=tenant_id, issue_id=issue.id,
                                           subscriber_id=sub.id, recipient=sub.recipient, consent_id=d.consent_id,
                                           body_sha256=_sha(body), approved_by=human.id, approved_at=now,
                                           state=SendState.APPROVED, updated_at=now))
        req = c.MessageSendRequest(tenant_id=tenant_id, idempotency_key=f"newsletter:{rec.id}", message_id=rec.id,
                                   channel=MessageChannel.EMAIL, to=sub.recipient, body=body,
                                   consent_id=d.consent_id, human=human)
        try:
            served = sender.run(tenant_id, req)
        except Exception as exc:  # noqa: BLE001 - recorded, never retried by the machine
            tx.update(NewsletterDelivery, rec.id, state=SendState.FAILED, updated_at=clock.now(),
                      detail=f"not confirmed ({type(exc).__name__}); check before sending again")
            report.failed.append(sub.id)
            continue
        out = served.result.output
        if isinstance(out, c.MessageSendResult) and out.status == "sent" and served.result.status == "done":
            tx.update(NewsletterDelivery, rec.id, state=SendState.SENT, external_id=out.external_id,
                      updated_at=clock.now())
            report.sent.append(sub.id)
        else:
            link = out.click_to_chat_url if isinstance(out, c.MessageSendResult) else None
            tx.update(NewsletterDelivery, rec.id, state=SendState.OPENED, updated_at=clock.now())
            report.opened.append({"subscriber_id": sub.id, "link": link})
    return report


def confirm_sent(tx: Any, *, tenant_id: str, delivery_id: str, actor: Actor, verifier: SessionVerifier,
                 clock: Clock) -> None:
    """The person sent a manual draft from their own mailbox and says so."""
    require_human(actor, HumanOnlyAction.APPROVE, verifier=verifier, tenant_id=tenant_id)
    rec = tx.get(NewsletterDelivery, delivery_id)
    if rec is None or rec.state is not SendState.OPENED:
        raise SendRefused("open the draft first")
    tx.update(NewsletterDelivery, rec.id, state=SendState.SENT, updated_at=clock.now())
