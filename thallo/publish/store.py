"""What the publisher reads and writes, inside one tenant transaction.

`PublishStore` works over any row session with typed `insert`, `get`, `list` and `update`
(`thallo.store.db.TenantRepo`, or an in-memory one in tests). `PgPublishStore` replaces the two
writes that must be atomic under concurrency with single SQL statements:

- `claim`: `INSERT ... ON CONFLICT DO NOTHING` on `publication`, whose unique `(tenant_id,
  variant_id)` and `(tenant_id, idempotency_key)` make the database the arbiter. The loser gets
  False, its transaction is not aborted, and it makes no provider call.
- `swap` and `swap_slot`: compare and set on the state column (`UPDATE ... WHERE state = ANY`).

Every state write goes through the contract's state machines (`VARIANT`, `PLAN_SLOT`).
Events go to `event_log` (the outbox) in the same transaction as the change they describe.
"""
from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Any, Protocol

from thallo.contracts.actors import SystemActor
from thallo.contracts.clock import Clock
from thallo.contracts.events import Event, EventName, Payload
from thallo.contracts.ids import new_id
from thallo.contracts.models import ChannelAccount, EventRecord, PlanSlot, Publication, Row, Variant
from thallo.contracts.states import PLAN_SLOT, VARIANT, PlanSlotState, VariantState
from thallo.publish.records import PublicationSend, SendState

ACTOR_ID = "publisher"


class RowSession(Protocol):
    def insert(self, row: Row) -> Row: ...

    def get(self, model: type[Any], row_id: str) -> Any: ...

    def list(self, model: type[Any], **where: Any) -> list[Any]: ...

    def update(self, model: type[Any], row_id: str, **fields: Any) -> bool: ...


class PublishStore:
    def __init__(self, rows: RowSession, tenant_id: str, clock: Clock) -> None:
        self.rows = rows
        self.tenant_id = tenant_id
        self.clock = clock

    # ------------------------------------------------------------------ variants
    def variant(self, variant_id: str) -> Variant | None:
        v = self.rows.get(Variant, variant_id)
        return v if v is not None and v.tenant_id == self.tenant_id else None

    def move_variant(self, variant_id: str, target: VariantState) -> bool:
        """Move through the VARIANT machine. True if it moved or was already there; False if the
        move is not legal from where the variant is (nothing is written)."""
        v = self.variant(variant_id)
        if v is None:
            return False
        if v.state is target:
            return True
        if not VARIANT.allowed(v.state, target):
            return False
        return self.rows.update(Variant, variant_id, state=target)

    def bench_variants(self, channel: Any) -> list[Variant]:
        return [v for v in self.rows.list(Variant, state=VariantState.BENCHED, channel=channel) if not v.is_canary]

    def channel_account(self, account_id: str) -> ChannelAccount | None:
        a = self.rows.get(ChannelAccount, account_id)
        return a if a is not None and a.tenant_id == self.tenant_id else None

    # ------------------------------------------------------------------ publications
    def publication_for_variant(self, variant_id: str) -> Publication | None:
        found = self.rows.list(Publication, variant_id=variant_id)
        return found[0] if found else None

    def send(self, publication_id: str) -> PublicationSend | None:
        return self.rows.get(PublicationSend, publication_id)

    def claim(self, publication: Publication, send: PublicationSend) -> bool:
        """Insert both rows, or neither if the variant or the key already has a publication."""
        if self.rows.list(Publication, variant_id=publication.variant_id) or \
                self.rows.list(Publication, idempotency_key=publication.idempotency_key):
            return False
        self.rows.insert(publication)
        self.rows.insert(send)
        return True

    def swap(self, publication_id: str, expect: Collection[SendState], *, if_attempts: int,
             if_requested_at: datetime | None = None, **fields: Any) -> bool:
        """Set fields on the send row only if its state is one of `expect` and, when given, its
        attempt number and requested time are still these (the attempt the caller acted on)."""
        current = self.send(publication_id)
        if current is None or current.state not in expect:
            return False
        if if_attempts is not None and current.attempts != if_attempts:
            return False
        if if_requested_at is not None and current.requested_at != if_requested_at:
            return False
        return self.rows.update(PublicationSend, publication_id, updated_at=self.clock.now(), **fields)

    def update_publication(self, publication_id: str, **fields: Any) -> None:
        if not self.rows.update(Publication, publication_id, **fields):
            raise LookupError("publication not found")

    # ------------------------------------------------------------------ plan slots
    def slots(self, **where: Any) -> list[PlanSlot]:
        return self.rows.list(PlanSlot, **where)

    def slot(self, slot_id: str) -> PlanSlot | None:
        return self.rows.get(PlanSlot, slot_id)

    def swap_slot(self, slot_id: str, expect: PlanSlotState, target: PlanSlotState, **fields: Any) -> bool:
        PLAN_SLOT.check(expect, target)
        s = self.slot(slot_id)
        if s is None or s.state is not expect:
            return False
        return self.rows.update(PlanSlot, slot_id, state=target, **fields)

    # ------------------------------------------------------------------ events
    def emit(self, name: EventName, payload: Payload) -> None:
        now = self.clock.now()
        event = Event.build(id=new_id(self.clock), tenant_id=self.tenant_id, occurred_at=now,
                            actor=SystemActor(id=ACTOR_ID), payload=payload, name=name)
        self.rows.insert(EventRecord(id=event.id, created_at=now, tenant_id=self.tenant_id, name=event.name.value,
                                     version=event.version, actor_kind=event.actor.kind, actor_id=event.actor.id,
                                     payload=event.payload, occurred_at=now))


class PgPublishStore(PublishStore):
    """The same store over `TenantRepo`, with the atomic writes in SQL."""

    def claim(self, publication: Publication, send: PublicationSend) -> bool:
        p = publication
        row = self.rows.execute(  # type: ignore[attr-defined]
            "INSERT INTO publication (id, created_at, tenant_id, variant_id, approval_id, connection_id, "
            "idempotency_key, content_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (p.id, p.created_at, p.tenant_id, p.variant_id, p.approval_id, p.connection_id, p.idempotency_key,
             p.content_sha256)).fetchone()
        if row is None:
            return False
        self.rows.insert(send)
        return True

    def swap(self, publication_id: str, expect: Collection[SendState], *, if_attempts: int,
             if_requested_at: datetime | None = None, **fields: Any) -> bool:
        from thallo.store.db import _value  # the repo's own value encoding

        fields["updated_at"] = self.clock.now()
        names = list(fields)
        sets = ", ".join(f"{n} = %s" for n in names)  # names are this module's own field names
        for n in names:
            if n not in PublicationSend.model_fields:
                raise ValueError(f"unknown field {n}")
        where = "id = %s AND state = ANY(%s)"
        params: list[Any] = [publication_id, [s.value for s in expect]]
        if if_attempts is not None:
            where += " AND attempts = %s"
            params.append(if_attempts)
        if if_requested_at is not None:
            where += " AND requested_at = %s"
            params.append(if_requested_at)
        cur = self.rows.execute(  # type: ignore[attr-defined]
            f"UPDATE publication_send SET {sets} WHERE {where}", [_value(fields[n]) for n in names] + params)
        return cur.rowcount == 1

    def swap_slot(self, slot_id: str, expect: PlanSlotState, target: PlanSlotState, **fields: Any) -> bool:
        from thallo.store.db import _value

        PLAN_SLOT.check(expect, target)
        for n in fields:
            if n not in PlanSlot.model_fields:
                raise ValueError(f"unknown field {n}")
        sets = ", ".join(["state = %s", *(f"{n} = %s" for n in fields)])
        cur = self.rows.execute(  # type: ignore[attr-defined]
            f"UPDATE plan_slot SET {sets} WHERE id = %s AND state = %s",
            [target.value, *(_value(v) for v in fields.values()), slot_id, expect.value])
        return cur.rowcount == 1


__all__ = ["ACTOR_ID", "PgPublishStore", "PublishStore", "RowSession"]
