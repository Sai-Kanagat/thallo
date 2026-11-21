"""Profile field changes (hours, categories, description, phone, website) as proposals.

A proposal is the current value, the proposed value, its hash and a reason. Values are validated
in code (hours as HH:MM per weekday, a description length cap). A proposal changes nothing: a
person of approver role approves the exact hash (`approve_proposal`, human-only), and then a
person applies it in the Business Profile dashboard from the manual pack and marks it done. No
code here writes to Google.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from thallo.contracts.actors import Actor, SessionVerifier, require_human
from thallo.contracts.clock import Clock
from thallo.contracts.enums import HumanOnlyAction
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser
from thallo.extensions.gbp.replies import Decision, ReplyRefused
from thallo.gate.approvals import approver_problem

FIELDS = ("regular_hours", "special_hours", "primary_category", "additional_categories", "description", "phone",
          "website")
DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PHONE = re.compile(r"^\+?[0-9 ()-]{6,24}$")
DESCRIPTION_MAX = 750  # Google's published limit for the business description


class ProposalInvalid(ValueError):
    pass


def value_hash(field: str, value: Any) -> str:
    blob = json.dumps({"field": field, "value": value}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _periods(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["hours must be a list of periods"]
    out: list[str] = []
    for p in value:
        if not isinstance(p, dict) or set(p) - {"day", "date", "open", "close", "closed"}:
            out.append("a period has unknown keys")
            continue
        if "day" in p and p["day"] not in DAYS:
            out.append(f"unknown day {p.get('day')!r}")
        if "date" in p and not (isinstance(p["date"], str) and DATE.match(p["date"])):
            out.append("a date must be YYYY-MM-DD")
        if p.get("closed") is True:
            continue
        o, cl = p.get("open"), p.get("close")
        if not (isinstance(o, str) and HHMM.match(o) and isinstance(cl, str) and HHMM.match(cl)):
            out.append("open and close must be HH:MM")
        elif o >= cl:
            out.append(f"closes at {cl} before it opens at {o}")
    return out


def problems(field: str, value: Any) -> list[str]:
    if field not in FIELDS:
        return [f"{field!r} is not a profile field this engine proposes"]
    if field == "regular_hours":
        return _periods(value) + ([] if all(isinstance(p, dict) and "day" in p for p in value or [])
                                  else ["regular hours need a day on every period"])
    if field == "special_hours":
        return _periods(value) + ([] if all(isinstance(p, dict) and "date" in p for p in value or [])
                                  else ["special hours need a date on every period"])
    if field == "additional_categories":
        ok = isinstance(value, list) and all(isinstance(v, str) and 0 < len(v) <= 100 for v in value)
        return [] if ok and len(value) <= 9 else ["additional categories are up to nine names"]
    if not isinstance(value, str) or not value.strip():
        return [f"{field} needs a value"]
    if field == "description" and len(value) > DESCRIPTION_MAX:
        return [f"the description is {len(value)} characters, over {DESCRIPTION_MAX}"]
    if field == "phone" and not PHONE.match(value):
        return ["the phone number has characters a phone number does not"]
    if field == "website" and not value.startswith("https://"):
        return ["the website must start with https://"]
    if field == "primary_category" and len(value) > 100:
        return ["the category name is too long"]
    return []


@dataclass(frozen=True)
class Proposal:
    id: str
    tenant_id: str
    field: str
    current_value: Any
    proposed_value: Any
    value_sha256: str
    reason: str
    proposed_by: str  # who suggested it: an agent id, a play, or a person


def propose(*, tenant_id: str, field: str, current: Any, proposed: Any, reason: str, proposed_by: str,
            clock: Clock) -> Proposal:
    if found := problems(field, proposed):
        raise ProposalInvalid("; ".join(found))
    if current == proposed:
        raise ProposalInvalid("the proposed value is the current value")
    if not reason.strip():
        raise ProposalInvalid("a proposal needs a reason a person can read")
    return Proposal(new_id(clock), tenant_id, field, current, proposed, value_hash(field, proposed), reason.strip(),
                    proposed_by)


def approve_proposal(proposal: Proposal, *, actor: Actor, approver: AppUser | None, verifier: SessionVerifier,
                     shown_sha256: str, clock: Clock, approve: bool = True, note: str | None = None) -> Decision:
    human = require_human(actor, HumanOnlyAction.APPROVE, verifier=verifier, tenant_id=proposal.tenant_id)
    if (problem := approver_problem(approver, human.id, proposal.tenant_id)) is not None:
        raise ReplyRefused(problem)
    if shown_sha256 != proposal.value_sha256 or value_hash(proposal.field, proposal.proposed_value) != shown_sha256:
        raise ReplyRefused("the value shown is not the proposal's value")
    return Decision(id=new_id(clock), tenant_id=proposal.tenant_id, subject_kind="profile_proposal",
                    subject_id=proposal.id, decision="approved" if approve else "rejected",
                    subject_sha256=proposal.value_sha256, actor_id=human.id, read_confirmed=True, note=note)
