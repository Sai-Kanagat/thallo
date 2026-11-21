"""Withdrawn proof never passes the gate, whatever the variant's content hash.

Approval and check_publish both read a variant's check rows through the gate repository's
`get_check_results(tenant, variant, sha)`. The store adapters append `blocking_rows(...)` to that
read: when the variant carries a proof piece that is withdrawn, whose consent is not live now, or
that has an unresolved withdrawal flag, the gate sees one FAIL code row bound to exactly the hash
it asked about. An edit gives a new hash and the next read returns the FAIL for that hash too,
so no edit can drop the block. The row is never stored, so nothing can supersede it: it carries
the highest attempt and is rebuilt on every read.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

from thallo.contracts.enums import CheckLayer, CheckVerdict
from thallo.contracts.models import CheckResult, Consent
from thallo.extensions.proof.records import LIVE, ProofFlag, ProofPiece

CHECK_NAME = "proof.consent"
TOP_ATTEMPT = 2**31 - 1


def variant_problem(list_all: Callable[..., list], get: Callable[..., Any], variant_id: str,
                    now: datetime) -> str | None:
    pieces = list_all(ProofPiece, variant_id=variant_id)
    if not pieces:
        return None
    for p in pieces:
        c = get(Consent, p.consent_id)
        live = (c is not None and c.person_id == p.person_id and c.given_at <= now
                and (c.withdrawn_at is None or c.withdrawn_at > now))
        if p.state != LIVE or not live:
            return f"proof piece {p.id} rests on a consent that is withdrawn or not live"
    if any(f.resolved_at is None for f in list_all(ProofFlag, variant_id=variant_id)):
        return "a proof withdrawal flag on this variant is not resolved"
    return None


def blocking_rows(list_all: Callable[..., list], get: Callable[..., Any], *, tenant_id: str, variant_id: str,
                  content_sha256: str, now: datetime) -> list[CheckResult]:
    """Zero or one synthetic FAIL row for the hash the gate asked about. Fails closed: if the proof
    tables cannot be read, the variant is refused."""
    try:
        why = variant_problem(list_all, get, variant_id, now)
    except Exception as exc:  # noqa: BLE001 - an unreadable proof table refuses, never passes
        why = f"proof consent could not be read ({type(exc).__name__})"
    if why is None:
        return []
    h = hashlib.sha256(f"{tenant_id}:{variant_id}:{content_sha256}".encode()).hexdigest()
    rid = f"ffffffff-ffff-7{h[:3]}-8{h[3:6]}-{h[6:18]}"  # uuid7 shape, sorts after every real id
    return [CheckResult(id=rid, created_at=now, tenant_id=tenant_id, variant_id=variant_id, layer=CheckLayer.CODE,
                        check_name=CHECK_NAME, verdict=CheckVerdict.FAIL, reason=why, attempt=TOP_ATTEMPT,
                        content_sha256=content_sha256)]


__all__ = ["CHECK_NAME", "blocking_rows", "variant_problem"]
