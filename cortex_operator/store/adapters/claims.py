"""Truth's `ClaimRepository` and `ClaimUsage` over Postgres (WP-01 follow-up to WP-03).

Claims are append-only by version: rows are unique on (tenant_id, code, version). `save` inserts
a new version, or moves an existing version through the claim state machine and records who
verified it. It never rewrites what a stored version says (value, unit, evidence, wording),
except a candidate's, which was never usable in copy.
"""
from __future__ import annotations

from collections.abc import Sequence

from cortex_operator.contracts.models import Claim
from cortex_operator.contracts.states import CLAIM, ClaimState
from cortex_operator.store.adapters.audit import own
from cortex_operator.store.db import TenantRepo

CONTENT = ("kind", "value", "unit", "as_of", "source_id", "source_span", "evidence_sha256", "languages",
           "supersedes_id")
LIFECYCLE = ("state", "verified_by", "verified_at", "expires_at", "expiry_reason")


class ClaimWriteRefused(ValueError):
    pass


class PgClaimRepository:
    def __init__(self, repo: TenantRepo) -> None:
        self.repo = repo

    # ClaimRepository
    def latest(self, tenant_id: str, code: str) -> Claim | None:
        own(self.repo, tenant_id)
        rows = self.repo.query(Claim, "SELECT * FROM claim WHERE code = %s ORDER BY version DESC LIMIT 1", (code,))
        return rows[0] if rows else None

    def history(self, tenant_id: str, code: str) -> Sequence[Claim]:
        own(self.repo, tenant_id)
        return self.repo.query(Claim, "SELECT * FROM claim WHERE code = %s ORDER BY version", (code,))

    def latest_for_tenant(self, tenant_id: str) -> Sequence[Claim]:
        own(self.repo, tenant_id)
        return self.repo.query(Claim, "SELECT DISTINCT ON (code) * FROM claim ORDER BY code, version DESC")

    def get(self, tenant_id: str, claim_id: str) -> Claim | None:
        own(self.repo, tenant_id)
        return self.repo.get(Claim, claim_id)

    def save(self, claim: Claim) -> None:
        own(self.repo, claim.tenant_id)
        rows = self.repo.query(Claim, "SELECT * FROM claim WHERE code = %s AND version = %s FOR UPDATE",
                               (claim.code, claim.version))
        if not rows:
            if claim.version > 1:
                before = self.repo.query(Claim, "SELECT * FROM claim WHERE code = %s AND version = %s",
                                         (claim.code, claim.version - 1))
                if not before or claim.supersedes_id != before[0].id:
                    raise ClaimWriteRefused(f"{claim.code} v{claim.version} must supersede v{claim.version - 1}")
            self.repo.insert(claim)
            return
        stored = rows[0]
        if stored.id != claim.id:
            raise ClaimWriteRefused(f"{claim.code} v{claim.version} already exists")
        content = [f for f in CONTENT if getattr(stored, f) != getattr(claim, f)]
        if content and stored.state is not ClaimState.CANDIDATE:
            raise ClaimWriteRefused(f"{claim.code} v{claim.version}: stored versions are append-only "
                                    f"({', '.join(content)} changed)")
        if claim.state is not stored.state:
            CLAIM.check(stored.state, claim.state)  # VOIDED and REJECTED are terminal
        fields = {f: getattr(claim, f) for f in (*CONTENT, *LIFECYCLE) if getattr(stored, f) != getattr(claim, f)}
        if fields:
            self.repo.update(Claim, claim.id, **fields)

    # ClaimUsage
    def variants_using_claim(self, tenant_id: str, claim_id: str) -> Sequence[str]:
        own(self.repo, tenant_id)
        rows = self.repo.execute("SELECT variant_id FROM variant_claim WHERE claim_id = %s ORDER BY variant_id",
                                 (claim_id,)).fetchall()
        return [str(r[0]) for r in rows]

    def live_approvals_for_variants(self, tenant_id: str, variant_ids: Sequence[str]) -> Sequence[str]:
        own(self.repo, tenant_id)
        if not variant_ids:
            return []
        rows = self.repo.execute(
            "SELECT DISTINCT a.id FROM approval a "
            "JOIN variant v ON v.tenant_id = a.tenant_id AND v.bundle_id = a.subject_id "
            "WHERE a.subject_kind = 'bundle' AND a.voided_at IS NULL AND v.id = ANY(%s::uuid[]) ORDER BY a.id",
            (list(variant_ids),)).fetchall()
        return [str(r[0]) for r in rows]


__all__ = ["ClaimWriteRefused", "PgClaimRepository"]
