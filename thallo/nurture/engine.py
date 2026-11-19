"""Nurture sequences (module 16): keeps warm leads moving with wording the owner approved once.

Rules, all plain code:

- A nurture template is approved once by hash (`ApprovalSubject.TEMPLATE`, the lead desk's rule and
  helpers). The approval fixes the wording; it never sends. Every instance is drafted through the
  lead desk's draft choke point (`Desk._new_draft`, which runs the consent decision for the exact
  recipient and channel) and waits for a person's tap on the lead desk's one send path
  (`Desk.approve_and_send`). This module never builds a send request.
- `run_due` drafts at most one message per enrolment step (a unique row makes a second run a
  no-op). The next step is due only `wait_hours` after the previous one was sent.
- Quiet hours (tenant local time) and caps per person (at most `max_per_week` in any 7 days, at
  least `min_gap_hours` apart, counted over every message to that person) are checked when a step
  is drafted and again at the tap (`send_guard`).
- A sequence stops on a reply (any newer touch on the lead), on consent withdrawal or loss of a
  basis, and when the lead leaves NURTURING (booked, lost, contacted). Stopping discards its open drafts.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from thallo.contracts.actors import Actor, SessionVerifier, require_human
from thallo.contracts.enums import (
    ApprovalMethod,
    ApprovalSubject,
    HumanOnlyAction,
    Language,
    LeadRoute,
    MessageChannel,
    MessageKind,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import Approval, AppUser, Lead, Message, MessageTemplate, Tenant
from thallo.contracts.states import LeadState, MessageState
from thallo.leads import consent as consent_rules
from thallo.leads.desk import Desk, DeskError, _move
from thallo.leads.templates import APPROVER_ROLES, live_approval, promise_problems, render, template_hash
from thallo.nurture.records import EnrolmentState, NurtureEnrolment, NurtureSequence, NurtureStepDraft

TEMPLATE_TTL = timedelta(days=180)
WEEK = timedelta(days=7)
ENROLLABLE = frozenset({LeadState.ROUTED, LeadState.NO_SHOW, LeadState.CONTACTED})
MAX_STEPS = 8


def _tz(desk: Desk) -> ZoneInfo:
    t = desk.tx.get(Tenant, desk.tenant_id)
    try:
        return ZoneInfo(t.timezone if t else "UTC")
    except Exception:  # noqa: BLE001 - an unknown zone falls back to UTC
        return ZoneInfo("UTC")


def quiet_until(seq: NurtureSequence, now: datetime, tz: ZoneInfo) -> datetime | None:
    """None when `now` is outside quiet hours, else the moment they end."""
    local = now.astimezone(tz)
    s, e = seq.quiet_start_hour, seq.quiet_end_hour
    if s == e:
        return None
    h = local.hour
    quiet = (s <= h < e) if s < e else (h >= s or h < e)
    if not quiet:
        return None
    end = local.replace(hour=e, minute=0, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end.astimezone(now.tzinfo)


def cap_until(seq: NurtureSequence, person_msgs: list[Message], now: datetime,
              exclude: str | None = None) -> datetime | None:
    """None when another message to this person is within the caps now, else when it will be.
    Counts sent messages and open drafts the person may still receive (nurture only for drafts)."""
    times = sorted(m.sent_at for m in person_msgs if m.id != exclude and m.sent_at is not None
                   and m.state in (MessageState.SENT, MessageState.DELIVERED))
    pending = sum(1 for m in person_msgs if m.id != exclude and m.state is MessageState.DRAFTED
                  and m.kind is MessageKind.NURTURE)
    waits: list[datetime] = []
    if times and now < times[-1] + timedelta(hours=seq.min_gap_hours):
        waits.append(times[-1] + timedelta(hours=seq.min_gap_hours))
    window = [t for t in times if t > now - WEEK]
    if len(window) + pending >= seq.max_per_week:
        if pending and len(window) < seq.max_per_week:
            waits.append(now + timedelta(hours=seq.min_gap_hours))  # wait for the open draft first
        else:
            waits.append(window[len(window) + pending - seq.max_per_week] + WEEK)
    return max(waits) if waits else None


class Nurture:
    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.tx = desk.tx
        self.clock = desk.clock
        self.tenant_id = desk.tenant_id

    # ------------------------------------------------------------------ templates
    def new_template(self, *, name: str, channel: MessageChannel, language: Language, body: str,
                     slot_sources: dict[str, str]) -> MessageTemplate:
        versions = [t.version for t in self.tx.list_all(MessageTemplate) if t.name == name]
        t = MessageTemplate(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id, name=name,
                            version=max(versions, default=0) + 1, kind=MessageKind.NURTURE, channel=channel,
                            language=language, body=body.strip(), slot_sources=slot_sources, content_sha256="0" * 64)
        t = t.model_copy(update={"content_sha256": template_hash(t)})
        if problems := promise_problems(t):
            raise DeskError("template_refused", "; ".join(problems))
        self.tx.insert(t)
        return t

    def approve_template(self, template_id: str, *, actor: Actor, verifier: SessionVerifier) -> Approval:
        human = require_human(actor, HumanOnlyAction.SIGN_TEMPLATE, verifier=verifier, tenant_id=self.tenant_id)
        user = self.tx.get(AppUser, human.id)
        if user is None or user.role not in APPROVER_ROLES:
            raise DeskError("forbidden", "only a founder, approver or operator approves a template")
        t = self.tx.get(MessageTemplate, template_id)
        if t is None or t.kind is not MessageKind.NURTURE:
            raise DeskError("not_found", "no such nurture template")
        if problems := promise_problems(t):
            raise DeskError("template_refused", "; ".join(problems))
        now = self.clock.now()
        a = Approval(id=new_id(self.clock), created_at=now, tenant_id=self.tenant_id,
                     subject_kind=ApprovalSubject.TEMPLATE, subject_id=t.id, content_hash=template_hash(t),
                     approver_id=user.id, method=ApprovalMethod.TEMPLATE_INSTANCE, approved_at=now,
                     expires_at=now + TEMPLATE_TTL)
        self.tx.insert(a)
        self.tx.update(MessageTemplate, t.id, active=True)
        return a

    def live_template(self, template_id: str) -> tuple[MessageTemplate, Approval] | None:
        t = self.tx.get(MessageTemplate, template_id)
        if t is None or t.kind is not MessageKind.NURTURE or not t.active:
            return None
        users = {u.id: u for u in self.tx.list_all(AppUser)}
        a = live_approval(t, self.tx.list_all(Approval, subject_id=t.id), users, self.clock.now())
        return (t, a) if a else None

    # ------------------------------------------------------------------ sequences
    def new_sequence(self, *, name: str, channel: MessageChannel, steps: list[tuple[str, int]],
                     quiet_start_hour: int = 20, quiet_end_hour: int = 9, max_per_week: int = 2,
                     min_gap_hours: int = 48) -> NurtureSequence:
        if not 1 <= len(steps) <= MAX_STEPS:
            raise DeskError("bad_sequence", f"a sequence has 1 to {MAX_STEPS} steps")
        out = []
        for template_id, wait in steps:
            t = self.tx.get(MessageTemplate, template_id)
            if t is None or t.kind is not MessageKind.NURTURE or t.channel is not channel:
                raise DeskError("bad_sequence", "each step needs a nurture template on the sequence's channel")
            if not 0 <= int(wait) <= 24 * 60:
                raise DeskError("bad_sequence", "a wait is between 0 and 1440 hours")
            out.append({"template_id": template_id, "wait_hours": int(wait)})
        seq = NurtureSequence(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id,
                              name=name.strip()[:80] or "Sequence", channel=channel, steps=out,
                              quiet_start_hour=quiet_start_hour, quiet_end_hour=quiet_end_hour,
                              max_per_week=max_per_week, min_gap_hours=min_gap_hours)
        self.tx.insert(seq)
        return seq

    # ------------------------------------------------------------------ enrolment
    def active_enrolment(self, lead_id: str) -> NurtureEnrolment | None:
        return next((e for e in self.tx.list_all(NurtureEnrolment, lead_id=lead_id)
                     if e.state is EnrolmentState.ACTIVE), None)

    def enrol(self, lead_id: str, sequence_id: str) -> NurtureEnrolment:
        lead = self.desk.lead(lead_id)
        seq = self.tx.get(NurtureSequence, sequence_id)
        if seq is None or not seq.active:
            raise DeskError("not_found", "no such sequence")
        if lead.route is not LeadRoute.WARM or lead.state not in ENROLLABLE:
            raise DeskError("not_warm", "only a warm, routed lead joins a nurture sequence")
        if self.active_enrolment(lead.id):
            raise DeskError("already_enrolled", "this lead is already in a sequence")
        person = self.desk.person(lead)
        if not self.desk.consent(person, seq.channel).allowed:
            raise DeskError("no_consent", "no consent basis for this channel and recipient")
        _move(self.tx, lead, LeadState.NURTURING)
        now = self.clock.now()
        e = NurtureEnrolment(id=new_id(self.clock), created_at=now, tenant_id=self.tenant_id, lead_id=lead.id,
                             person_id=person.id, sequence_id=seq.id,
                             due_at=now + timedelta(hours=seq.steps[0]["wait_hours"]),
                             last_touch_id=lead.last_touch_id, updated_at=now)
        self.tx.insert(e)
        return e

    def stop(self, e: NurtureEnrolment, reason: str, *, done: bool = False) -> None:
        self.tx.update(NurtureEnrolment, e.id, state=EnrolmentState.DONE if done else EnrolmentState.STOPPED,
                       stop_reason=reason, updated_at=self.clock.now())
        for sd in self.tx.list_all(NurtureStepDraft, enrolment_id=e.id):
            m = self.tx.get(Message, sd.message_id)
            if m is not None and m.state is MessageState.DRAFTED:
                self.tx.update(Message, m.id, state=MessageState.DISCARDED)

    def stop_for_lead(self, lead_id: str, reason: str) -> None:
        if (e := self.active_enrolment(lead_id)) is not None:
            self.stop(e, reason)

    def stop_reason(self, e: NurtureEnrolment, seq: NurtureSequence) -> str | None:
        lead = self.tx.get(Lead, e.lead_id)
        if lead is None or lead.state is not LeadState.NURTURING:
            return "left_nurture"
        if lead.last_touch_id != e.last_touch_id:
            return "replied"
        decision = self.desk.consent(self.desk.person(lead), seq.channel)
        if not decision.allowed:
            return "withdrawn" if decision.reason == consent_rules.WITHDRAWN else "no_consent"
        return None

    # ------------------------------------------------------------------ the runner
    def run_due(self) -> dict[str, int]:
        """Draft the steps that are due. Sends nothing. Safe to run twice."""
        counts = {"drafted": 0, "deferred": 0, "stopped": 0, "done": 0, "waiting": 0}
        now = self.clock.now()
        tz = _tz(self.desk)
        for e in self.tx.list_all(NurtureEnrolment):
            if e.state is not EnrolmentState.ACTIVE:
                continue
            seq = self.tx.get(NurtureSequence, e.sequence_id)
            if seq is None:
                continue
            if reason := self.stop_reason(e, seq):
                self.stop(e, reason)
                counts["stopped"] += 1
                continue
            drafts = {sd.step: sd for sd in self.tx.list_all(NurtureStepDraft, enrolment_id=e.id)}
            if e.step > 0:
                prev = self.tx.get(Message, drafts[e.step - 1].message_id) if e.step - 1 in drafts else None
                if prev is not None and prev.state is MessageState.DRAFTED:
                    counts["waiting"] += 1  # a person has not tapped the last one yet
                    continue
                if e.step >= len(seq.steps):
                    self.stop(e, "sequence_complete", done=True)
                    counts["done"] += 1
                    continue
                base = prev.sent_at if prev is not None and prev.sent_at else e.updated_at
                due = base + timedelta(hours=seq.steps[e.step]["wait_hours"])
                if due != e.due_at:
                    self.tx.update(NurtureEnrolment, e.id, due_at=due)
                    e = e.model_copy(update={"due_at": due})
            if now < e.due_at or e.step in drafts:
                continue
            later = quiet_until(seq, now, tz) or cap_until(seq, self.tx.list_all(Message, person_id=e.person_id), now)
            if later is not None:
                self.tx.update(NurtureEnrolment, e.id, due_at=later, updated_at=now)
                counts["deferred"] += 1
                continue
            found = self.live_template(seq.steps[e.step]["template_id"])
            if found is None:
                counts["waiting"] += 1  # the wording is not approved (or expired): nothing drafted
                continue
            t, approval = found
            lead = self.desk.lead(e.lead_id)
            body = render(t, display_name=self.desk.person(lead).display_name, answers=lead.answers)
            msg = self.desk._new_draft(lead, MessageKind.NURTURE, body, template=t, template_approval=approval)
            if msg is None:
                self.stop(e, "no_consent")
                counts["stopped"] += 1
                continue
            self.tx.insert(NurtureStepDraft(id=new_id(self.clock), created_at=now, tenant_id=self.tenant_id,
                                            enrolment_id=e.id, step=e.step, message_id=msg.id))
            self.tx.update(NurtureEnrolment, e.id, step=e.step + 1, updated_at=now)
            counts["drafted"] += 1
        return counts

    # ------------------------------------------------------------------ the tap
    def send_guard(self, desk: Desk, msg: Message) -> None:
        """Run by the lead desk at the tap, after its consent re-check. Raises to refuse."""
        sd = next((s for s in self.tx.list_all(NurtureStepDraft) if s.message_id == msg.id), None)
        e = self.tx.get(NurtureEnrolment, sd.enrolment_id) if sd else None
        if e is None or e.state is not EnrolmentState.ACTIVE:
            raise DeskError("sequence_stopped", "this nurture sequence has stopped")
        seq = self.tx.get(NurtureSequence, e.sequence_id)
        if seq is None:
            raise DeskError("sequence_stopped", "this nurture sequence is gone")
        if reason := self.stop_reason(e, seq):
            self.stop(e, reason)
            raise DeskError("sequence_stopped", f"the sequence stopped ({reason})")
        if msg.template_id is None or (found := self.live_template(msg.template_id)) is None \
                or found[1].id != msg.template_approval_id:
            raise DeskError("template_not_live", "the nurture template is no longer approved")
        now = self.clock.now()
        if quiet_until(seq, now, _tz(desk)) is not None:
            raise DeskError("quiet_hours", "it is quiet hours for this business; send it later")
        if cap_until(seq, self.tx.list_all(Message, person_id=msg.person_id), now, exclude=msg.id) is not None:
            raise DeskError("frequency_cap", "this person has had enough messages for now")

    def guards(self) -> dict[MessageKind, Any]:
        return {MessageKind.NURTURE: self.send_guard}


def nurture_desk(tx: Any, tenant_id: str, clock: Any, **kw: Any) -> tuple[Desk, Nurture]:
    """A lead desk whose send path also carries nurture messages, guarded."""
    desk = Desk(tx, tenant_id, clock, **kw)
    n = Nurture(desk)
    desk.send_guards = n.guards()
    return desk, n


__all__ = ["Nurture", "cap_until", "nurture_desk", "quiet_until"]
