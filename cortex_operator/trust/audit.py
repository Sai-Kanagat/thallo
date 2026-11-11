"""The audit log: append-only and hash-chained per tenant (WP-04).

Each entry hashes its own fields plus the previous entry's hash, so `verify_chain` detects an
edited, deleted, inserted or reordered entry. Removing entries from the end leaves a valid
shorter chain; that is caught only against an anchor (a head seq and hash recorded elsewhere,
for example in the weekly report), which `verify_chain` accepts as `anchor`.

Entries carry an ActorRef (kind and id). A session id never enters the log: an Actor is
reduced to its ActorRef on the way in, and payload keys naming a session are refused.

Storage is a small Protocol. WP-01 implements it over Postgres, with the unique
(tenant_id, seq) constraint making a concurrent append fail instead of forking the chain.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from cortex_operator.contracts.actors import Actor, ActorRef
from cortex_operator.contracts.clock import Clock
from cortex_operator.contracts.ids import new_id
from cortex_operator.contracts.models import AuditEntry

AUDIT_DOMAIN = b"thallo/audit/v1\n"
AUDIT_VERSION = 1


class AuditError(ValueError):
    pass


class AuditConflict(AuditError):
    """Another writer appended the same seq first. Re-read the head and try again."""


class AuditStore(Protocol):
    def head(self, tenant_id: str) -> AuditEntry | None:
        """The entry with the highest seq for this tenant, or None."""
        ...

    def insert(self, entry: AuditEntry) -> None:
        """Insert only. Must raise AuditConflict if (tenant_id, seq) exists. No update, no delete."""
        ...

    def entries(self, tenant_id: str) -> Sequence[AuditEntry]:
        """Every entry for this tenant, ordered by seq."""
        ...


class InMemoryAuditStore:
    """For tests and the demo. Mirrors the database's insert-only rule."""

    def __init__(self) -> None:
        self._rows: dict[str, list[AuditEntry]] = {}

    def head(self, tenant_id: str) -> AuditEntry | None:
        rows = self._rows.get(tenant_id)
        return rows[-1] if rows else None

    def insert(self, entry: AuditEntry) -> None:
        rows = self._rows.setdefault(entry.tenant_id, [])
        if any(r.seq == entry.seq for r in rows):
            raise AuditConflict(f"seq {entry.seq} already exists for this tenant")
        rows.append(entry)
        rows.sort(key=lambda r: r.seq)

    def entries(self, tenant_id: str) -> Sequence[AuditEntry]:
        return tuple(self._rows.get(tenant_id, ()))


# ---------------------------------------------------------------- hashing

def _check_payload(value: Any, path: str = "payload") -> None:
    if isinstance(value, float):
        raise AuditError(f"{path}: floats are not allowed; store a string or an int")
    if isinstance(value, Mapping):
        for k, v in value.items():
            if not isinstance(k, str):
                raise AuditError(f"{path}: keys must be strings")
            _check_text(k, f"{path} key")
            _check_payload(v, f"{path}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            _check_payload(v, f"{path}[{i}]")
    elif isinstance(value, str):
        _check_text(value, path)
    elif not (value is None or isinstance(value, int | bool)):
        raise AuditError(f"{path}: {type(value).__name__} is not a JSON value")


def _check_text(value: str | None, path: str) -> None:
    """A session never enters the log, not even in free text. Refuses any mention, case-insensitive."""
    if value is not None and "session" in value.lower():
        raise AuditError(f"{path}: a session never enters the audit log")


def _ts(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def compute_entry_hash(entry: AuditEntry) -> str:
    """The hash of every field except entry_hash itself."""
    body = {
        "v": AUDIT_VERSION,
        "id": entry.id,
        "tenant_id": entry.tenant_id,
        "seq": entry.seq,
        "actor_kind": entry.actor_kind.value,
        "actor_id": entry.actor_id,
        "action": entry.action,
        "subject_kind": entry.subject_kind,
        "subject_id": entry.subject_id,
        "payload": entry.payload,
        "prev_hash": entry.prev_hash,
        "occurred_at": _ts(entry.occurred_at),
        "created_at": _ts(entry.created_at),
    }
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(AUDIT_DOMAIN + blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- verification

@dataclass(frozen=True)
class ChainProblem:
    seq: int | None
    problem: str


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    problems: tuple[ChainProblem, ...]
    head_seq: int
    head_hash: str | None


def verify_chain(entries: Sequence[AuditEntry], *, tenant_id: str,
                 anchor: tuple[int, str] | None = None) -> ChainReport:
    """Check a tenant's chain in the order given (which should be seq order).

    Detects: an edited entry (hash mismatch), a deleted or inserted entry (seq gap or broken
    link), a reordered pair (broken link and seq out of order), another tenant's entry, and,
    with an anchor, a truncated tail or a rewritten history up to the anchor."""
    problems: list[ChainProblem] = []
    prev: AuditEntry | None = None
    for i, e in enumerate(entries):
        expected_seq = i + 1
        if e.tenant_id != tenant_id:
            problems.append(ChainProblem(e.seq, "entry belongs to another tenant"))
        if e.seq != expected_seq:
            problems.append(ChainProblem(e.seq, f"expected seq {expected_seq}: an entry was deleted, "
                                                "inserted or moved"))
        if compute_entry_hash(e) != e.entry_hash:
            problems.append(ChainProblem(e.seq, "entry hash does not match its contents: edited"))
        expected_prev = prev.entry_hash if prev is not None else None
        if e.prev_hash != expected_prev:
            problems.append(ChainProblem(e.seq, "prev_hash does not match the entry before it"))
        prev = e
    head_seq = prev.seq if prev is not None else 0
    head_hash = prev.entry_hash if prev is not None else None
    if anchor is not None:
        a_seq, a_hash = anchor
        match = next((e for e in entries if e.seq == a_seq), None)
        if match is None:
            problems.append(ChainProblem(a_seq, "anchored entry is missing: the log was truncated"))
        elif match.entry_hash != a_hash:
            problems.append(ChainProblem(a_seq, "anchored entry hash differs: history was rewritten"))
    return ChainReport(ok=not problems, problems=tuple(problems), head_seq=head_seq, head_hash=head_hash)


# ---------------------------------------------------------------- the log

@dataclass(frozen=True)
class AuditDraft:
    actor: Actor | ActorRef
    action: str
    subject_kind: str | None = None
    subject_id: str | None = None
    payload: Mapping[str, Any] | None = None


class AuditLog:
    def __init__(self, store: AuditStore, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    def prepare(self, tenant_id: str, drafts: Sequence[AuditDraft]) -> tuple[AuditEntry, ...]:
        """Build chained entries after the current head WITHOUT writing them. The caller inserts
        them in the same transaction as the row change they describe (see gate/AUDIT.md). If another
        writer got there first, the unique (tenant_id, seq) makes that transaction fail as a whole."""
        head = self.store.head(tenant_id)
        seq = head.seq if head is not None else 0
        prev = head.entry_hash if head is not None else None
        now = self.clock.now()
        out: list[AuditEntry] = []
        for d in drafts:
            if not d.action:
                raise AuditError("an audit entry needs an action")
            for text, where in ((d.action, "action"), (d.subject_kind, "subject_kind"),
                                (d.subject_id, "subject_id")):
                _check_text(text, where)
            ref = ActorRef.of(d.actor)  # drops any session
            _check_text(ref.id, "actor_id")
            body = dict(d.payload or {})
            _check_payload(body)
            seq += 1
            draft = AuditEntry(
                id=new_id(self.clock), created_at=now, tenant_id=tenant_id, seq=seq,
                actor_kind=ref.kind, actor_id=ref.id, action=d.action, subject_kind=d.subject_kind,
                subject_id=d.subject_id, payload=body, prev_hash=prev, entry_hash="0" * 64, occurred_at=now,
            )
            entry = draft.model_copy(update={"entry_hash": compute_entry_hash(draft)})
            prev = entry.entry_hash
            out.append(entry)
        return tuple(out)

    def append(self, tenant_id: str, actor: Actor | ActorRef, action: str, *,
               subject_kind: str | None = None, subject_id: str | None = None,
               payload: Mapping[str, Any] | None = None) -> AuditEntry:
        """Prepare and insert one entry on its own. Use `prepare` when the entry describes a row
        change: the two must commit together."""
        (entry,) = self.prepare(tenant_id, [AuditDraft(actor, action, subject_kind, subject_id, payload)])
        self.store.insert(entry)
        return entry

    def verify(self, tenant_id: str, anchor: tuple[int, str] | None = None) -> ChainReport:
        return verify_chain(self.store.entries(tenant_id), tenant_id=tenant_id, anchor=anchor)

    def head(self, tenant_id: str) -> tuple[int, str] | None:
        """The anchor to record outside the log: (seq, entry_hash) of the newest entry."""
        h = self.store.head(tenant_id)
        return (h.seq, h.entry_hash) if h is not None else None


__all__ = [
    "AuditConflict", "AuditDraft", "AuditError", "AuditLog", "AuditStore", "ChainProblem", "ChainReport",
    "InMemoryAuditStore", "compute_entry_hash", "verify_chain",
]
