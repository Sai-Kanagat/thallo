"""The lead desk service (BUILD_PLAN 2.6 lead journey, module 15).

Works on a tenant transaction with `get`, `list_all`, `insert`, `update` (the API's TenantTx, or
the store's repo through it). Row-level security scopes every call to one tenant.

Two choke points carry the rules:

- `_new_draft` is the only place a message for sending is created. It runs the consent decision
  first (region policy, contact consent, channel) and refuses when there is no basis. A lead with
  no basis gets no draft: the seller sees the lead and replies personally.
- `approve_and_send` is the only place a `MessageSendRequest` is built. It needs a verified live
  human session (`require_human(FIRST_REPLY)`), re-checks consent at that moment, binds the exact
  channel, recipient and body hash the person saw in a `lead_message_send` row (a second tap fails),
  and hands the request to the messaging connection with the human attached. Manual and draft
  providers return a link that opens the seller's own app; the message counts as sent only when a
  provider says "sent" or the person confirms it.

Nothing here sends on a timer, a template or a route. The acknowledgement is drafted from the
hash-approved template and still waits for a person's tap.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from thallo.contracts import connection as c
from thallo.contracts.actors import Actor, HumanActor, SessionVerifier, require_human
from thallo.contracts.clock import Clock
from thallo.contracts.enums import (
    ActorKind,
    ApprovalMethod,
    ApprovalSubject,
    ConsentBasis,
    ConsentScope,
    HumanOnlyAction,
    Language,
    LeadRoute,
    LossReason,
    MessageChannel,
    MessageKind,
    OutcomeKind,
    TouchKind,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    Account,
    Approval,
    AppUser,
    Booking,
    Consent,
    Deal,
    Lead,
    Message,
    MessageTemplate,
    Outcome,
    Person,
    RegionPolicy,
    Tenant,
    Touch,
)
from thallo.contracts.states import LEAD, BookingState, LeadState, MessageState
from thallo.leads import consent as consent_rules
from thallo.leads import scoring
from thallo.leads.drafts import Drafter, PlainDrafter
from thallo.leads.play import LeadPlay, load_play
from thallo.leads.records import (
    ConsentRecipient,
    ContactCandidate,
    LeadAction,
    LeadDeskSetting,
    LeadMessageSend,
    SendState,
)
from thallo.leads.templates import (
    APPROVER_ROLES,
    clean_slot,
    live_approval,
    promise_problems,
    render,
    template_hash,
)
from thallo.leads.touches import HEAT_ACTION, TouchEvent

MAX_TEXT_ANSWER = 200
TEMPLATE_TTL = timedelta(days=180)
CLOSED = frozenset({LeadState.WON, LeadState.LOST})
SELLER_KINDS = frozenset({MessageKind.ACKNOWLEDGEMENT, MessageKind.REPLY_DRAFT})


class DeskError(Exception):
    """A refusal a person can read. `code` is stable for the API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ====================================================================== small helpers

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def norm_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]  # international prefix written as 00
    return ("+" + digits) if 6 <= len(digits) <= 15 else None


def norm_email(raw: str | None) -> str | None:
    if not raw:
        return None
    e = raw.strip().casefold()
    return e if re.fullmatch(r"[^@\s<>\"']{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}", e) else None


def _clean_text(value: Any, cap: int = MAX_TEXT_ANSWER) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]", " ", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:cap] or None


def _move(tx: Any, lead: Lead, to: LeadState) -> Lead:
    if lead.state is to:
        return lead
    if not LEAD.allowed(lead.state, to):
        raise DeskError("illegal_state", f"a {lead.state.value} lead cannot become {to.value}")
    tx.update(Lead, lead.id, state=to)
    return lead.model_copy(update={"state": to})


# ====================================================================== the desk

@dataclass
class Desk:
    tx: Any
    tenant_id: str
    clock: Clock
    drafter: Drafter = None  # type: ignore[assignment]
    play_loader: Callable[[str], LeadPlay] = load_play
    # Extra message kinds this desk may send, each with a guard run at the tap after the consent
    # re-check (WP-14 nurture: caps, quiet hours, stop rules). Empty by default: fail closed.
    send_guards: dict[MessageKind, Callable[[Any, Message], None]] | None = None

    def __post_init__(self) -> None:
        if self.drafter is None:
            self.drafter = PlainDrafter()

    # ------------------------------------------------------------------ setup
    def setting(self) -> LeadDeskSetting:
        rows = self.tx.list_all(LeadDeskSetting)
        if not rows:
            raise DeskError("not_set_up", "the lead desk has no play set for this business yet")
        return rows[0]

    def configure(self, play_code: str, *, service_area: list[str] = (), stock: list[str] = (),  # type: ignore[assignment]
                  supported_use_cases: list[str] = (), response_minutes: int = 60,  # type: ignore[assignment]
                  acknowledgement_template_id: str | None = None) -> LeadDeskSetting:
        self.play_loader(play_code)  # unknown play raises
        fields = dict(play_code=play_code, service_area=[t for t in (_clean_text(s, 80) for s in service_area) if t],
                      stock=[t for t in (_clean_text(s, 80) for s in stock) if t],
                      supported_use_cases=[t for t in (_clean_text(s, 80) for s in supported_use_cases) if t],
                      response_minutes=response_minutes, acknowledgement_template_id=acknowledgement_template_id)
        existing = self.tx.list_all(LeadDeskSetting)
        if existing:
            self.tx.update(LeadDeskSetting, existing[0].id, **fields)
            return existing[0].model_copy(update=fields)
        row = LeadDeskSetting(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id, **fields)
        self.tx.insert(row)
        return row

    # ------------------------------------------------------------------ the acknowledgement template
    def new_template(self, *, name: str, channel: MessageChannel, language: Language, body: str,
                     slot_sources: dict[str, str]) -> MessageTemplate:
        versions = [t.version for t in self.tx.list_all(MessageTemplate) if t.name == name]
        t = MessageTemplate(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id, name=name,
                            version=max(versions, default=0) + 1, kind=MessageKind.ACKNOWLEDGEMENT, channel=channel,
                            language=language, body=body.strip(), slot_sources=slot_sources, content_sha256="0" * 64)
        t = t.model_copy(update={"content_sha256": template_hash(t)})
        if problems := promise_problems(t):
            raise DeskError("template_refused", "; ".join(problems))
        self.tx.insert(t)
        return t

    def approve_template(self, template_id: str, *, actor: Actor, verifier: SessionVerifier,
                         ttl: timedelta = TEMPLATE_TTL) -> Approval:
        """A founder, approver or operator approves the exact wording once, by hash. It is used for
        acknowledgements; each one still waits for a person to tap send."""
        human = require_human(actor, HumanOnlyAction.SIGN_TEMPLATE, verifier=verifier, tenant_id=self.tenant_id)
        user = self.tx.get(AppUser, human.id)
        if user is None or user.role not in APPROVER_ROLES:
            raise DeskError("forbidden", "only a founder, approver or operator approves a template")
        if not timedelta(0) < ttl <= TEMPLATE_TTL:
            raise DeskError("bad_ttl", "a template approval lasts at most 180 days")
        t = self.tx.get(MessageTemplate, template_id)
        if t is None:
            raise DeskError("not_found", "no such template")
        if problems := promise_problems(t):
            raise DeskError("template_refused", "; ".join(problems))
        now = self.clock.now()
        for old in self.tx.list_all(MessageTemplate):
            if old.kind is MessageKind.ACKNOWLEDGEMENT and old.active and old.id != t.id and old.channel is t.channel:
                self.tx.update(MessageTemplate, old.id, active=False)
        a = Approval(id=new_id(self.clock), created_at=now, tenant_id=self.tenant_id,
                     subject_kind=ApprovalSubject.TEMPLATE, subject_id=t.id, content_hash=template_hash(t),
                     approver_id=user.id, method=ApprovalMethod.TEMPLATE_INSTANCE, approved_at=now,
                     expires_at=now + ttl)
        self.tx.insert(a)
        self.tx.update(MessageTemplate, t.id, active=True)
        s = self.setting()
        self.tx.update(LeadDeskSetting, s.id, acknowledgement_template_id=t.id)
        return a

    def play(self) -> LeadPlay:
        return self.play_loader(self.setting().play_code)

    def lists(self) -> scoring.TenantLists:
        s = self.setting()
        return scoring.TenantLists(frozenset(s.service_area), frozenset(s.stock), frozenset(s.supported_use_cases))

    # ------------------------------------------------------------------ intake
    def _match(self, ev: TouchEvent) -> tuple[Person | None, list[tuple[MessageChannel, str]]]:
        """The existing person this touch names, and any contact point it adds. A touch is not
        verified, so a new number or address is never merged onto the person; it is returned as
        a candidate for a person to confirm."""
        phone, email = norm_phone(ev.phone), norm_email(ev.email)
        for p in self.tx.list_all(Person):
            if (phone and norm_phone(p.phone) == phone) or (email and norm_email(p.email) == email):
                extra: list[tuple[MessageChannel, str]] = []
                if phone and norm_phone(p.phone) != phone:
                    extra.append((MessageChannel.WHATSAPP, phone))
                if email and norm_email(p.email) != email:
                    extra.append((MessageChannel.EMAIL, email))
                return p, extra
        return None, []

    def _person(self, ev: TouchEvent) -> Person:
        found, _extra = self._match(ev)
        if found is not None:
            return found
        phone, email = norm_phone(ev.phone), norm_email(ev.email)
        account_id = None
        if email:
            domain = email.rsplit("@", 1)[1]
            if domain not in scoring.FREE_EMAIL_DOMAINS:
                acct = next((a for a in self.tx.list_all(Account) if a.domain == domain), None)
                if acct is None:
                    acct = Account(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id,
                                   name=domain, domain=domain)
                    self.tx.insert(acct)
                account_id = acct.id
        p = Person(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id,
                   account_id=account_id, display_name=_clean_text(ev.display_name, 80), phone=phone, email=email)
        self.tx.insert(p)
        return p

    def intake(self, ev: TouchEvent) -> Lead:
        """A first (or repeat) touch: person matched or created, touch stored, lead opened or
        updated, heat recomputed, clock started. Drafts the acknowledgement when allowed."""
        play = self.play()
        _found, candidates = self._match(ev)
        person = self._person(ev)
        touch = Touch(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id, kind=ev.kind,
                      person_id=person.id, variant_id=ev.variant_id, ad_id=ev.ad_id,
                      tracked_link_id=ev.tracked_link_id, code=_clean_text(ev.code, 40),
                      self_report=_clean_text(ev.self_report), occurred_at=ev.occurred_at)
        self.tx.insert(touch)
        lead = next((x for x in self.tx.list_all(Lead, person_id=person.id) if x.state not in CLOSED), None)
        if lead is None:
            lead = Lead(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id,
                        person_id=person.id, first_touch_id=touch.id, last_touch_id=touch.id,
                        clock_started_at=self.clock.now())
            self.tx.insert(lead)
            new = True
        else:
            self.tx.update(Lead, lead.id, last_touch_id=touch.id)
            new = False
        for channel, value in candidates:
            self.tx.insert(ContactCandidate(id=new_id(self.clock), created_at=self.clock.now(),
                                            tenant_id=self.tenant_id, lead_id=lead.id, channel=channel, value=value,
                                            touch_id=touch.id))
        kinds = [HEAT_ACTION[ev.kind]] if ev.kind in HEAT_ACTION else []
        kinds += [k for k in ev.extra_actions if re.fullmatch(r"[a-z][a-z0-9_]{0,39}", k)]
        for kind in kinds:
            self.tx.insert(LeadAction(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id,
                                      lead_id=lead.id, kind=kind, touch_id=touch.id, occurred_at=ev.occurred_at))
        lead = self.rescore(lead.id)
        known = {q.key for q in play.qualification_questions}
        form = {k: v for k, v in ev.answers.items() if k in known}
        if form:
            lead = self.record_answers(lead.id, form, draft_ack=False)
        if new and ev.kind is not TouchKind.SELF_REPORT:
            self.draft_acknowledgement(lead.id)
        return self.lead(lead.id)

    # ------------------------------------------------------------------ reading
    def lead(self, lead_id: str) -> Lead:
        lead = self.tx.get(Lead, lead_id)
        if lead is None:
            raise DeskError("not_found", "no such lead")
        return lead

    def person(self, lead: Lead) -> Person:
        p = self.tx.get(Person, lead.person_id)
        if p is None:
            raise DeskError("not_found", "the lead's person is missing")
        return p

    def channel_for(self, person: Person) -> MessageChannel | None:
        if norm_phone(person.phone):
            return MessageChannel.WHATSAPP
        if norm_email(person.email):
            return MessageChannel.EMAIL
        return None

    def recipient(self, person: Person, channel: MessageChannel) -> str | None:
        return norm_email(person.email) if channel is MessageChannel.EMAIL else norm_phone(person.phone)

    def record_consent(self, person_id: str, *, channel: MessageChannel, recipient: str, basis: ConsentBasis,
                       evidence: str, actor: Actor, verifier: SessionVerifier) -> Consent:
        """A person records a contact consent for one exact number or address, with its evidence."""
        require_human(actor, HumanOnlyAction.CONSENT_CALL, verifier=verifier, tenant_id=self.tenant_id)
        to = norm_email(recipient) if channel is MessageChannel.EMAIL else norm_phone(recipient)
        ev = _clean_text(evidence, 300)
        if to is None or ev is None or self.tx.get(Person, person_id) is None:
            raise DeskError("bad_consent", "consent needs the person, a usable recipient and its evidence")
        now = self.clock.now()
        row = Consent(id=new_id(self.clock), created_at=now, tenant_id=self.tenant_id, person_id=person_id,
                      basis=basis, channel=channel, scope=ConsentScope.CONTACT, evidence=ev, given_at=now)
        self.tx.insert(row)
        self.tx.insert(ConsentRecipient(id=new_id(self.clock), created_at=now, tenant_id=self.tenant_id,
                                        consent_id=row.id, channel=channel, recipient=to))
        return row

    def consent(self, person: Person, channel: MessageChannel) -> consent_rules.ConsentDecision:
        to = self.recipient(person, channel)
        if to is None:
            return consent_rules.ConsentDecision(False, consent_rules.NO_CONSENT)
        covered: dict[str, set[tuple[MessageChannel, str]]] = {}
        for r in self.tx.list_all(ConsentRecipient):
            covered.setdefault(r.consent_id, set()).add((r.channel, r.recipient))
        tenant = self.tx.get(Tenant, self.tenant_id)
        now = self.clock.now()
        policy = consent_rules.current_policy(self.tx.list_all(RegionPolicy), tenant.region_code, now) \
            if tenant is not None else None
        return consent_rules.decide(person_id=person.id, channel=channel, recipient=to,
                                    consents=self.tx.list_all(Consent, person_id=person.id), covered=covered,
                                    policy=policy, now=now)

    def clock_status(self, lead: Lead) -> dict[str, Any]:
        s = self.setting()
        if lead.clock_started_at is None:
            return {"running": False, "due_at": None, "overdue": False, "minutes_left": None}
        due = lead.clock_started_at + timedelta(minutes=s.response_minutes)
        if lead.first_response_at is not None or lead.state in CLOSED:
            return {"running": False, "due_at": due.isoformat(), "overdue": False, "minutes_left": None,
                    "answered_in_minutes": round((lead.first_response_at - lead.clock_started_at).total_seconds() / 60)
                    if lead.first_response_at else None}
        left = (due - self.clock.now()).total_seconds() / 60
        return {"running": True, "due_at": due.isoformat(), "overdue": left < 0, "minutes_left": round(left)}

    # ------------------------------------------------------------------ qualification and scoring
    def record_answers(self, lead_id: str, answers: dict[str, Any], *, draft_ack: bool = True) -> Lead:
        play = self.play()
        lead = self.lead(lead_id)
        if lead.state in CLOSED:
            raise DeskError("closed", "this lead is closed")
        merged = dict(lead.answers)
        for key, raw in answers.items():
            q = play.question(key)
            if q is None:
                raise DeskError("bad_answer", f"{key!r} is not a question of this play")
            if q.type == "choice":
                if raw not in q.options:
                    raise DeskError("bad_answer", f"{key!r} must be one of {', '.join(q.options)}")
                merged[key] = raw
            else:
                text = _clean_text(raw)
                if text is None:
                    raise DeskError("bad_answer", f"{key!r} needs a short text answer")
                merged[key] = text
        # Drop an answer whose only_if no longer holds (finance changed to cash).
        merged = {k: v for k, v in merged.items() if (q := play.question(k)) is None or q.applies(merged)}
        self.tx.update(Lead, lead.id, answers=merged)
        lead = lead.model_copy(update={"answers": merged})
        if lead.state in (LeadState.NEW, LeadState.ACKNOWLEDGED):
            lead = _move(self.tx, lead, LeadState.QUALIFYING)
        lead = self.rescore(lead.id)
        if lead.state is LeadState.QUALIFYING and all(q.key in merged for q in play.questions_for(merged)):
            lead = self.route(lead.id)
        return lead

    def rescore(self, lead_id: str) -> Lead:
        play = self.play()
        lead = self.lead(lead_id)
        person = self.person(lead)
        now = self.clock.now()
        f = scoring.fit(play, lead.answers, email=person.email, lists=self.lists())
        actions = [scoring.Action(a.kind, a.occurred_at) for a in self.tx.list_all(LeadAction, lead_id=lead.id)]
        h = scoring.heat(play, actions, now)
        self.tx.update(Lead, lead.id, fit=f.fit, heat=h, heat_updated_at=now)
        return lead.model_copy(update={"fit": f.fit, "heat": h, "heat_updated_at": now})

    def route(self, lead_id: str) -> Lead:
        play = self.play()
        lead = self.rescore(lead_id)
        r = scoring.route(play, lead.fit, lead.heat)
        self.tx.update(Lead, lead.id, route=r)
        lead = lead.model_copy(update={"route": r})
        if r is LeadRoute.LOW_FIT:
            return _move(self.tx, lead, LeadState.PARKED)  # no outreach, kept for reporting
        lead = _move(self.tx, lead, LeadState.ROUTED)
        if r is LeadRoute.HOT:
            self.draft_reply(lead.id)
        # WARM: the approved nurture sequence is WP-14's; the lead waits in ROUTED for it.
        return lead

    # ------------------------------------------------------------------ drafting (choke point 1)
    def _new_draft(self, lead: Lead, kind: MessageKind, body: str, *, template: MessageTemplate | None = None,
                   template_approval: Approval | None = None, model_id: str | None = None,
                   prompt_version: str | None = None) -> Message | None:
        person = self.person(lead)
        channel = template.channel if template else self.channel_for(person)
        if channel is None or self.recipient(person, channel) is None:
            return None
        decision = self.consent(person, channel)
        if not decision.allowed:
            return None  # personal reply only: nothing drafted for sending
        if decision.footer and decision.footer not in body:
            body = f"{body}\n\n{decision.footer}"
        msg = Message(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id,
                      person_id=person.id, lead_id=lead.id, kind=kind, channel=channel, body=body,
                      template_id=template.id if template else None,
                      template_approval_id=template_approval.id if template_approval else None,
                      model_id=model_id, prompt_version=prompt_version)
        self.tx.insert(msg)
        return msg

    def _open_drafts(self, lead_id: str, kind: MessageKind) -> list[Message]:
        return [m for m in self.tx.list_all(Message, lead_id=lead_id)
                if m.kind is kind and m.state is MessageState.DRAFTED]

    def acknowledgement_template(self) -> tuple[MessageTemplate, Approval] | None:
        s = self.setting()
        if not s.acknowledgement_template_id:
            return None
        t = self.tx.get(MessageTemplate, s.acknowledgement_template_id)
        if t is None or not t.active or t.kind is not MessageKind.ACKNOWLEDGEMENT:
            return None
        users = {u.id: u for u in self.tx.list_all(AppUser)}
        a = live_approval(t, self.tx.list_all(Approval, subject_id=t.id), users, self.clock.now())
        return (t, a) if a else None

    def draft_acknowledgement(self, lead_id: str) -> Message | None:
        lead = self.lead(lead_id)
        if self._open_drafts(lead.id, MessageKind.ACKNOWLEDGEMENT) or any(
                m.kind is MessageKind.ACKNOWLEDGEMENT for m in self.tx.list_all(Message, lead_id=lead.id)):
            return None
        found = self.acknowledgement_template()
        if found is None:
            return None
        t, approval = found
        person = self.person(lead)
        body = render(t, display_name=person.display_name, answers=lead.answers)
        return self._new_draft(lead, MessageKind.ACKNOWLEDGEMENT, body, template=t, template_approval=approval)

    def draft_reply(self, lead_id: str) -> Message | None:
        lead = self.lead(lead_id)
        if lead.state in CLOSED or lead.route is LeadRoute.LOW_FIT:
            return None
        if self._open_drafts(lead.id, MessageKind.REPLY_DRAFT):
            return None
        person = self.person(lead)
        channel = self.channel_for(person)
        if channel is None or not self.consent(person, channel).allowed:
            return None  # checked before any model call: no basis, no draft, no spend
        d = self.drafter.draft(tenant_id=self.tenant_id, language=person.language or Language.EN,
                               display_name=person.display_name, answers=lead.answers,
                               idempotency_key=self.reply_key(lead.id))
        return self._new_draft(lead, MessageKind.REPLY_DRAFT, d.text, model_id=d.model_id,
                               prompt_version=d.prompt_version)

    def reply_key(self, lead_id: str) -> str:
        """Stable per lead and reply round: the number of this lead's earlier reply drafts."""
        rounds = sum(1 for m in self.tx.list_all(Message, lead_id=lead_id) if m.kind is MessageKind.REPLY_DRAFT)
        return f"lead-reply:{lead_id}:{rounds}"

    def edit_draft(self, message_id: str, body: str) -> Message:
        msg = self._draft(message_id)
        if msg.template_id is not None or msg.kind not in (MessageKind.REPLY_DRAFT,):
            raise DeskError("template_fixed", "this is approved wording; discard it and reply instead")
        text = (body or "").strip()
        if not text or len(text) > 2000:
            raise DeskError("bad_body", "a message needs between 1 and 2000 characters")
        self.tx.update(Message, msg.id, body=text)
        return msg.model_copy(update={"body": text})

    def discard(self, message_id: str) -> None:
        msg = self._draft(message_id)
        self.tx.update(Message, msg.id, state=MessageState.DISCARDED)

    def _draft(self, message_id: str) -> Message:
        msg = self.tx.get(Message, message_id)
        if msg is None or msg.lead_id is None:
            raise DeskError("not_found", "no such message")
        if msg.state is not MessageState.DRAFTED:
            raise DeskError("not_draft", f"this message is {msg.state.value}")
        return msg

    # ------------------------------------------------------------------ sending (choke point 2)
    def approve_and_send(self, message_id: str, *, actor: Actor, verifier: SessionVerifier, sender: Any,
                         body_sha256: str) -> dict[str, Any]:
        """One person, one tap, one message. `body_sha256` is the hash of the body the person saw;
        any difference refuses. `sender` is the ConnectionRegistry (anything with run(tenant, request))."""
        human: HumanActor = require_human(actor, HumanOnlyAction.FIRST_REPLY, verifier=verifier,
                                          tenant_id=self.tenant_id)
        msg = self._draft(message_id)
        guard = (self.send_guards or {}).get(msg.kind)
        if msg.kind not in SELLER_KINDS and guard is None:
            raise DeskError("not_sendable", "only acknowledgements and reply drafts are sent from the lead desk")
        if self.tx.get(LeadMessageSend, msg.id) is not None:
            raise DeskError("already_tapped", "this message was already approved once")
        if _sha(msg.body) != body_sha256:
            raise DeskError("changed", "the message changed since you saw it; read it again")
        lead = self.lead(msg.lead_id or "")
        person = self.person(lead)
        to = self.recipient(person, msg.channel)
        if to is None:
            raise DeskError("no_recipient", "the lead has no usable number or address for this channel")
        decision = self.consent(person, msg.channel)
        if not decision.allowed:
            raise DeskError("no_consent", f"consent does not allow this message now ({decision.reason})")
        if msg.kind is MessageKind.ACKNOWLEDGEMENT:
            found = self.acknowledgement_template()
            if found is None or found[0].id != msg.template_id:
                raise DeskError("template_not_live", "the acknowledgement template is no longer approved")
        if msg.template_id is not None or msg.kind is not MessageKind.REPLY_DRAFT:
            self._check_template_bound(msg, lead, person, decision.footer)
        if guard is not None:
            guard(self, msg)  # raises DeskError to refuse
        now = self.clock.now()
        record = LeadMessageSend(id=msg.id, created_at=now, tenant_id=self.tenant_id, channel=msg.channel,
                                 recipient=to, body_sha256=body_sha256, consent_id=decision.consent_id,
                                 approved_by=human.id, approved_at=now, state=SendState.APPROVED, updated_at=now)
        self.tx.insert(record)  # a second tap on the same message fails here
        request = c.MessageSendRequest(tenant_id=self.tenant_id, idempotency_key=f"message:{msg.id}",
                                       message_id=msg.id, channel=msg.channel, to=to, body=msg.body,
                                       consent_id=decision.consent_id, human=human)
        try:
            served = sender.run(self.tenant_id, request)
        except Exception as exc:  # noqa: BLE001 - the outcome is recorded, never retried by the machine
            self.tx.update(LeadMessageSend, msg.id, state=SendState.FAILED, updated_at=self.clock.now(),
                           detail=f"not confirmed ({type(exc).__name__}); check your app before sending again")
            return {"status": "failed", "message_id": msg.id, "link": None,
                    "detail": "The send could not be confirmed. Check your app before sending it again."}
        result = served.result
        out = result.output
        if isinstance(out, c.MessageSendResult) and out.status == "sent" and result.status == "done":
            self.tx.update(LeadMessageSend, msg.id, state=SendState.SENT, external_id=out.external_id,
                           updated_at=self.clock.now())
            self._mark_sent(msg, lead, human)
            return {"status": "sent", "message_id": msg.id, "link": None}
        link = out.click_to_chat_url if isinstance(out, c.MessageSendResult) else None
        self.tx.update(LeadMessageSend, msg.id, state=SendState.OPENED, updated_at=self.clock.now(),
                       external_id=out.external_id if isinstance(out, c.MessageSendResult) else None)
        return {"status": "open_in_app", "message_id": msg.id, "link": safe_link(link)}

    def _check_template_bound(self, msg: Message, lead: Lead, person: Person, footer: str | None) -> None:
        """Any template-bound message: its approval is live for the template's current hash, and its
        body is the template's approved wording with only cleaned slot values filled in (plus the
        consent footer). Answers may change after drafting; wording may not."""
        t = self.tx.get(MessageTemplate, msg.template_id) if msg.template_id else None
        if t is None or not t.active or t.kind is not msg.kind or t.channel is not msg.channel:
            raise DeskError("template_not_live", "this message has no live approved template")
        users = {u.id: u for u in self.tx.list_all(AppUser)}
        a = live_approval(t, self.tx.list_all(Approval, subject_id=t.id), users, self.clock.now())
        if a is None or a.id != msg.template_approval_id:
            raise DeskError("template_not_live", "the template approval is no longer live")
        if not _matches_template(t.body, msg.body, footer):
            raise DeskError("changed", "the message no longer matches its approved template")

    def confirm_sent(self, message_id: str, *, actor: Actor, verifier: SessionVerifier) -> None:
        """The person sent it from their own app and says so."""
        human = require_human(actor, HumanOnlyAction.FIRST_REPLY, verifier=verifier, tenant_id=self.tenant_id)
        msg = self._draft(message_id)
        rec = self.tx.get(LeadMessageSend, msg.id)
        if rec is None or rec.state is not SendState.OPENED:
            raise DeskError("not_opened", "open the message in your app first")
        self.tx.update(LeadMessageSend, msg.id, state=SendState.SENT, updated_at=self.clock.now())
        self._mark_sent(msg, self.lead(msg.lead_id or ""), human)

    def _mark_sent(self, msg: Message, lead: Lead, human: HumanActor) -> None:
        now = self.clock.now()
        self.tx.update(Message, msg.id, state=MessageState.SENT, sent_by_kind=ActorKind.HUMAN,
                       sent_by_user_id=human.id, sent_at=now)
        if lead.first_response_at is None:
            self.tx.update(Lead, lead.id, first_response_at=now)
        if msg.kind is MessageKind.ACKNOWLEDGEMENT:
            if lead.state is LeadState.NEW:
                _move(self.tx, lead, LeadState.ACKNOWLEDGED)
        elif msg.kind is MessageKind.REPLY_DRAFT and LEAD.allowed(lead.state, LeadState.CONTACTED):
            _move(self.tx, lead, LeadState.CONTACTED)

    # ------------------------------------------------------------------ booking and outcome buttons
    def book(self, lead_id: str, *, starts_at: datetime, kind: str, actor: Actor, verifier: SessionVerifier) -> Booking:
        require_human(actor, HumanOnlyAction.FIRST_REPLY, verifier=verifier, tenant_id=self.tenant_id)
        lead = self.lead(lead_id)
        k = _clean_text(kind, 40)
        if not k:
            raise DeskError("bad_booking", "say what is booked: a visit, a demo, a callback")
        lead = _move(self.tx, lead, LeadState.BOOKED)
        b = Booking(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id, lead_id=lead.id,
                    kind=k, starts_at=starts_at)
        self.tx.insert(b)
        return b

    def outcome(self, lead_id: str, kind: str, *, actor: Actor, verifier: SessionVerifier,
                amount: Decimal | None = None, reason: LossReason | None = None) -> Lead:
        """One tap: held, no_show, won (with amount) or lost (with a reason from the fixed list)."""
        human = require_human(actor, HumanOnlyAction.FIRST_REPLY, verifier=verifier, tenant_id=self.tenant_id)
        lead = self.lead(lead_id)
        bookings = sorted((b for b in self.tx.list_all(Booking, lead_id=lead.id) if b.state is BookingState.BOOKED),
                          key=lambda b: b.starts_at)
        if kind in ("held", "no_show"):
            if not bookings:
                raise DeskError("no_booking", "there is no open booking to mark")
            target = LeadState.HELD if kind == "held" else LeadState.NO_SHOW
            lead = _move(self.tx, lead, target)
            self.tx.update(Booking, bookings[-1].id, state=BookingState(kind))
            return lead
        if kind not in ("won", "lost"):
            raise DeskError("bad_outcome", "an outcome is held, no_show, won or lost")
        if kind == "won" and (amount is None or amount < 0):
            raise DeskError("bad_outcome", "a won deal needs its amount")
        if kind == "lost" and reason is None:
            raise DeskError("bad_outcome", "a lost deal needs a reason from the list")
        lead = _move(self.tx, lead, LeadState.WON if kind == "won" else LeadState.LOST)
        held = sorted(self.tx.list_all(Booking, lead_id=lead.id), key=lambda b: b.starts_at)
        booking_id = held[-1].id if held else None
        if booking_id and any(d.booking_id == booking_id for d in self.tx.list_all(Deal, lead_id=lead.id)):
            booking_id = None
        deal = Deal(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id,
                    booking_id=booking_id, lead_id=lead.id, value=amount if kind == "won" else None)
        self.tx.insert(deal)
        self.tx.insert(Outcome(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id,
                               deal_id=deal.id, kind=OutcomeKind(kind), amount=amount if kind == "won" else None,
                               loss_reason=reason if kind == "lost" else None, recorded_by=human.id,
                               occurred_at=self.clock.now()))
        for m in self.tx.list_all(Message, lead_id=lead.id):
            if m.state is MessageState.DRAFTED:
                self.tx.update(Message, m.id, state=MessageState.DISCARDED)
        return lead


def _matches_template(template_body: str, body: str, footer: str | None) -> bool:
    """True when `body` is the template wording with each slot replaced by a cleaned slot value
    (one line, no links, at most MAX_SLOT_CHARS) and, optionally, the consent footer."""
    from thallo.leads.templates import _LINKISH, MAX_SLOT_CHARS, SLOT

    parts = SLOT.split(template_body.strip())
    out = []
    for i, p in enumerate(parts):
        if i % 2:
            out.append(f"([^\\n]{{0,{MAX_SLOT_CHARS}}})")
        else:
            out.append(r"\s*".join(re.escape(w) for w in re.split(r"\s+", p)) if p.strip() else r"\s*")
    tail = f"(?:\\n\\n{re.escape(footer)})?" if footer else ""
    m = re.fullmatch(r"\s*" + "".join(out) + r"\s*" + tail, body)
    return m is not None and not any(_LINKISH.search(g or "") for g in m.groups())


_SAFE_LINK = re.compile(r"^(https://wa\.me/\d{6,15}\?text=|mailto:|sms:|https://mail\.google\.com/)")


def safe_link(link: str | None) -> str | None:
    """Only links that open the seller's own app pass to the screen."""
    if link and _SAFE_LINK.match(link) and not re.search(r"[\s\"'<>]", link):
        return link
    return None


def body_hash(body: str) -> str:
    return _sha(body)


__all__ = ["Desk", "DeskError", "body_hash", "clean_slot", "norm_email", "norm_phone", "safe_link"]
