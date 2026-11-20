"""Rows for migration 0037. `tx` is a store `TenantRepo` (one tenant transaction, row-level security).

All four tables are append-only for the app role: a new draft or a new decision is a new row."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from psycopg.types.json import Jsonb

from thallo.extensions.gbp.inbox import InboxItem
from thallo.extensions.gbp.posts import PieceOutcome
from thallo.extensions.gbp.profile import Proposal
from thallo.extensions.gbp.replies import Decision, ReplyDraft


def _own(tx: Any, tenant_id: str) -> None:
    if str(tenant_id).lower() != str(tx.tenant_id).lower():
        raise ValueError("the row belongs to another tenant than the repo's")


def write_items(tx: Any, items: Sequence[InboxItem]) -> dict[str, str]:
    """Insert items not seen before. Returns external key ("kind:external_id") -> row id for all of them."""
    rows = tx.fetch_all("SELECT kind, external_id, id FROM gbp_inbox_item")
    known = {f"{k}:{e}": str(i) for k, e, i in rows}
    for it in items:
        key = f"{it.kind}:{it.external_id}"
        if key in known:
            continue
        rid = tx.new_id()
        tx.execute("INSERT INTO gbp_inbox_item (id, created_at, tenant_id, kind, external_id, rating, author, body,"
                   " written_at, source) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                   (rid, tx.now(), tx.tenant_id, it.kind, it.external_id, it.rating, it.author, it.body,
                    it.written_at, it.source))
        known[key] = rid
    return known


def write_draft(tx: Any, d: ReplyDraft) -> None:
    _own(tx, d.tenant_id)
    tx.execute("INSERT INTO gbp_reply_draft (id, created_at, tenant_id, item_id, text, text_sha256, claim_ids,"
               " made_by, provider, model_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
               (d.id, tx.now(), tx.tenant_id, d.item_id, d.text, d.text_sha256, Jsonb(list(d.claim_ids)), d.made_by,
                d.provider, d.model_id))


def write_proposal(tx: Any, p: Proposal) -> None:
    _own(tx, p.tenant_id)
    tx.execute("INSERT INTO gbp_profile_proposal (id, created_at, tenant_id, field, current_value, proposed_value,"
               " value_sha256, reason, proposed_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
               (p.id, tx.now(), tx.tenant_id, p.field, Jsonb(p.current_value), Jsonb(p.proposed_value),
                p.value_sha256, p.reason, p.proposed_by))


def write_decision(tx: Any, d: Decision) -> None:
    _own(tx, d.tenant_id)
    tx.execute("INSERT INTO gbp_decision (id, created_at, tenant_id, subject_kind, subject_id, decision,"
               " subject_sha256, actor_id, read_confirmed, note) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
               (d.id, tx.now(), tx.tenant_id, d.subject_kind, d.subject_id, d.decision, d.subject_sha256, d.actor_id,
                d.read_confirmed, d.note))


def decisions(tx: Any) -> list[Decision]:
    rows = tx.fetch_all("SELECT id, subject_kind, subject_id, decision, subject_sha256, actor_id, read_confirmed, note"
                        " FROM gbp_decision ORDER BY created_at, id")
    return [Decision(str(i), str(tx.tenant_id), k, str(s), d, h, str(a), bool(r), n) for i, k, s, d, h, a, r, n in rows]


def drafts(tx: Any) -> list[ReplyDraft]:
    rows = tx.fetch_all("SELECT id, item_id, text, text_sha256, claim_ids, made_by, provider, model_id"
                        " FROM gbp_reply_draft ORDER BY created_at, id")
    return [ReplyDraft(str(i), str(tx.tenant_id), str(it), t, h, tuple(c or ()), m, p, mo)
            for i, it, t, h, c, m, p, mo in rows]


def write_piece(tx: Any, piece: PieceOutcome) -> None:
    conn = getattr(tx, "conn", None)
    if conn is None or not callable(getattr(conn, "transaction", None)):
        raise ValueError("write_piece needs a repo with a transaction")
    rows = piece.rows()
    for r in rows:
        _own(tx, r.tenant_id)
    with conn.transaction():
        for r in rows:
            tx.insert(r)
