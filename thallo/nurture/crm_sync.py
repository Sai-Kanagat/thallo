"""One-way CRM sync (module 17) and the CSV floor.

CRM data is personal data. The projection is a fixed allowlist, computed per person:

- the email address only if the lead desk's consent decision allows that exact address now, the
  phone number only if it allows that exact number now (WhatsApp or SMS);
- the display name only when at least one of those is allowed;
- the pipeline stage and the won amount (business facts about the deal).

No free-text answers, no self-reports, no touches. A person with no contact allowed is not synced;
if they were synced before, an erase record clears the contact fields in the CRM. Nothing read
from a CRM comes back (one-way), and none of this is ever put into a model prompt.

A sync is started by a person (`require_human(APPROVE)`) and runs through the connection registry:
HubSpot only with the operator's `THALLO_CRM_LIVE_SYNC=1`, else the manual connection (export CSV).
`crm_sync_record` keeps the last pushed hash per person, so only changes are sent.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from decimal import Decimal
from typing import Any

from thallo.contracts import connection as c
from thallo.contracts.actors import Actor, SessionVerifier, require_human
from thallo.contracts.enums import HumanOnlyAction, MessageChannel
from thallo.contracts.ids import new_id
from thallo.contracts.models import Deal, Lead, Outcome, Person
from thallo.leads.desk import Desk
from thallo.nurture.pipeline import stage_of
from thallo.nurture.records import CrmSyncRecord

FIELDS = ("thallo_id", "name", "email", "phone", "stage", "won_amount", "erase")
MAX_BATCH = 100


def _hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def project(desk: Desk) -> dict[str, dict[str, Any] | None]:
    """person id to its record, or None when nothing about the person may leave."""
    out: dict[str, dict[str, Any] | None] = {}
    leads = desk.tx.list_all(Lead)
    deals = desk.tx.list_all(Deal)
    outcomes = {o.deal_id: o for o in desk.tx.list_all(Outcome)}
    for person in desk.tx.list_all(Person):
        email = desk.recipient(person, MessageChannel.EMAIL)
        phone = desk.recipient(person, MessageChannel.WHATSAPP)
        email = email if email and desk.consent(person, MessageChannel.EMAIL).allowed else None
        phone_ok = phone and (desk.consent(person, MessageChannel.WHATSAPP).allowed
                              or desk.consent(person, MessageChannel.SMS).allowed)
        phone = phone if phone_ok else None
        if not email and not phone:
            out[person.id] = None
            continue
        mine = sorted((x for x in leads if x.person_id == person.id), key=lambda x: x.created_at)
        won = Decimal(0)
        for d in deals:
            o = outcomes.get(d.id)
            if o is not None and o.kind.value == "won" and any(x.id == d.lead_id for x in mine):
                won += o.amount or Decimal(0)
        out[person.id] = {"thallo_id": person.id, "name": person.display_name or "", "email": email or "",
                          "phone": phone or "", "stage": stage_of(mine[-1].state) if mine else "",
                          "won_amount": str(won), "erase": False}
    return out


def pending(desk: Desk, provider: str) -> list[dict[str, Any]]:
    """Changed records only, with the CRM id when one is known."""
    ledger = {r.person_id: r for r in desk.tx.list_all(CrmSyncRecord) if r.provider == provider}
    batch: list[dict[str, Any]] = []
    for pid, projected in sorted(project(desk).items()):
        rec = projected
        prev = ledger.get(pid)
        if rec is None:
            if prev is None or prev.erased:
                continue
            rec = {"thallo_id": pid, "name": "", "email": "", "phone": "", "stage": "", "won_amount": "0",
                   "erase": True}
        if prev is not None and prev.content_sha256 == _hash(rec):
            continue
        batch.append({**rec, "external_id": prev.external_id if prev else None})
    return batch[:MAX_BATCH]


def sync(desk: Desk, registry: Any, *, actor: Actor, verifier: SessionVerifier) -> dict[str, Any]:
    human = require_human(actor, HumanOnlyAction.APPROVE, verifier=verifier, tenant_id=desk.tenant_id)
    choice = registry.resolve(desk.tenant_id, c.CrmSyncRequest.capability)
    first = registry._get(desk.tenant_id, choice.ordered_connection_ids[0]) if choice.ordered_connection_ids else None
    provider_hint = first.provider if first is not None else "manual"
    batch = pending(desk, provider_hint)
    if not batch:
        return {"status": "up_to_date", "synced": 0, "provider": provider_hint}
    key = "crm:" + hashlib.sha256("".join(_hash(r) for r in batch).encode()).hexdigest()[:32]
    served = registry.run(desk.tenant_id, c.CrmSyncRequest(tenant_id=desk.tenant_id, idempotency_key=key,
                                                           records=tuple(batch)))
    out = served.result.output
    now = desk.clock.now()
    synced = 0
    if served.result.status == "done" and isinstance(out, c.CrmSyncResult):
        ledger = {r.person_id: r for r in desk.tx.list_all(CrmSyncRecord) if r.provider == served.provider}
        for rec in batch:
            pid = rec["thallo_id"]
            ext = out.external_ids.get(pid)
            if ext is None:
                continue  # not confirmed: tried again next time
            clean = {k: v for k, v in rec.items() if k != "external_id"}
            fields = dict(content_sha256=_hash(clean), external_id=ext, erased=bool(rec["erase"]), synced_at=now)
            if pid in ledger:
                desk.tx.update(CrmSyncRecord, ledger[pid].id, **fields)
            else:
                desk.tx.insert(CrmSyncRecord(id=new_id(desk.clock), created_at=now, tenant_id=desk.tenant_id,
                                             provider=served.provider, person_id=pid, **fields))
            synced += 1
        return {"status": "synced", "synced": synced, "provider": served.provider, "by": human.id}
    return {"status": "export_csv", "synced": 0, "provider": served.provider, "task": served.result.task,
            "records": len(batch)}


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t") else text


def to_csv(desk: Desk) -> str:
    """The manual floor: every person with a contact basis, the same allowlist, formula-safe."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(FIELDS[:-1])
    for _pid, rec in sorted(project(desk).items()):
        if rec is not None:
            w.writerow([_cell(rec[f]) for f in FIELDS[:-1]])
    return buf.getvalue()


__all__ = ["FIELDS", "pending", "project", "sync", "to_csv"]
