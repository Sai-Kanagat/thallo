"""What the truth library needs from storage. WP-01's store implements these Protocols.

The library itself never touches a database. `InMemoryClaimRepository` is for tests, the demo
and any package that wants to exercise the truth library before WP-01 lands.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

from cortex_operator.contracts.models import Claim


@runtime_checkable
class ClaimUsage(Protocol):
    """Who used a claim. Used to list what a change to the claim voids."""

    def variants_using_claim(self, tenant_id: str, claim_id: str) -> Sequence[str]:
        """Ids of variants with a variant_claim row for this claim."""
        ...

    def live_approvals_for_variants(self, tenant_id: str, variant_ids: Sequence[str]) -> Sequence[str]:
        """Ids of approvals not yet voided whose subject covers any of these variants (the bundle
        each variant belongs to, today)."""
        ...


@runtime_checkable
class ClaimRepository(ClaimUsage, Protocol):
    def get_by_code(self, tenant_id: str, code: str) -> Claim | None: ...

    def claims_for_tenant(self, tenant_id: str) -> Sequence[Claim]: ...

    def save(self, claim: Claim) -> None: ...


class InMemoryClaimRepository:
    def __init__(self, claims: Iterable[Claim] = ()) -> None:
        self._claims: dict[tuple[str, str], Claim] = {}
        self._uses: dict[tuple[str, str], set[str]] = {}  # (tenant, claim_id) -> variant ids
        self._approvals: dict[tuple[str, str], set[str]] = {}  # (tenant, approval_id) -> variant ids
        self._voided: set[tuple[str, str]] = set()
        for claim in claims:
            self.save(claim)

    # ClaimRepository
    def get_by_code(self, tenant_id: str, code: str) -> Claim | None:
        return self._claims.get((tenant_id, code))

    def claims_for_tenant(self, tenant_id: str) -> Sequence[Claim]:
        return [c for (t, _), c in sorted(self._claims.items()) if t == tenant_id]

    def save(self, claim: Claim) -> None:
        self._claims[(claim.tenant_id, claim.code)] = claim

    # ClaimUsage
    def variants_using_claim(self, tenant_id: str, claim_id: str) -> Sequence[str]:
        return sorted(self._uses.get((tenant_id, claim_id), set()))

    def live_approvals_for_variants(self, tenant_id: str, variant_ids: Sequence[str]) -> Sequence[str]:
        wanted = set(variant_ids)
        return sorted(a for (t, a), vs in self._approvals.items()
                      if t == tenant_id and (t, a) not in self._voided and vs & wanted)

    # test helpers
    def link(self, tenant_id: str, variant_id: str, claim_id: str) -> None:
        self._uses.setdefault((tenant_id, claim_id), set()).add(variant_id)

    def approve(self, tenant_id: str, approval_id: str, variant_ids: Iterable[str]) -> None:
        self._approvals[(tenant_id, approval_id)] = set(variant_ids)

    def void(self, tenant_id: str, approval_id: str) -> None:
        self._voided.add((tenant_id, approval_id))
