"""What the truth library needs from storage. WP-01's store implements these Protocols.

Claims are append-only by version (contracts 2.0.0): rows are unique on (tenant_id, code,
version). Lookups by code take the latest version; `library` builds the code-keyed mapping that
`compile_text` and `build` take, from latest versions only, so an old version can never fill a slot.

The library itself never touches a database. `InMemoryClaimRepository` is for tests, the demo
and any package that wants to exercise the truth library before wiring the store.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

from thallo.contracts.models import Claim
from thallo.contracts.states import ClaimState


@runtime_checkable
class ClaimUsage(Protocol):
    """Who used a claim row. Used to list what a change to the claim voids."""

    def variants_using_claim(self, tenant_id: str, claim_id: str) -> Sequence[str]:
        """Ids of variants with a variant_claim row for this claim row (this exact version)."""
        ...

    def live_approvals_for_variants(self, tenant_id: str, variant_ids: Sequence[str]) -> Sequence[str]:
        """Ids of approvals not yet voided whose subject covers any of these variants."""
        ...


@runtime_checkable
class ClaimRepository(ClaimUsage, Protocol):
    def latest(self, tenant_id: str, code: str) -> Claim | None:
        """The highest version of this code, whatever its state."""
        ...

    def history(self, tenant_id: str, code: str) -> Sequence[Claim]:
        """Every version of this code, oldest first."""
        ...

    def latest_for_tenant(self, tenant_id: str) -> Sequence[Claim]:
        """The latest version of every code of this tenant."""
        ...

    def save(self, claim: Claim) -> None:
        """Insert a new version, or update the state of an existing one. Never rewrites the
        content of a stored version, except a candidate's (never usable in copy)."""
        ...


def library(repo: ClaimRepository, tenant_id: str) -> dict[str, Claim]:
    """The code-keyed mapping `compile_text` and `build` take: latest versions only."""
    return {c.code: c for c in repo.latest_for_tenant(tenant_id)}


_CONTENT = ("kind", "value", "unit", "as_of", "source_id", "source_span", "evidence_sha256", "languages",
            "supersedes_id")


class InMemoryClaimRepository:
    def __init__(self, claims: Iterable[Claim] = ()) -> None:
        self._rows: dict[tuple[str, str, int], Claim] = {}
        self._uses: dict[tuple[str, str], set[str]] = {}  # (tenant, claim_id) -> variant ids
        self._approvals: dict[tuple[str, str], set[str]] = {}  # (tenant, approval_id) -> variant ids
        self._voided: set[tuple[str, str]] = set()
        for claim in claims:
            self.save(claim)

    # ClaimRepository
    def latest(self, tenant_id: str, code: str) -> Claim | None:
        versions = self.history(tenant_id, code)
        return versions[-1] if versions else None

    def history(self, tenant_id: str, code: str) -> Sequence[Claim]:
        return [c for (t, k, _), c in sorted(self._rows.items()) if t == tenant_id and k == code]

    def latest_for_tenant(self, tenant_id: str) -> Sequence[Claim]:
        codes = sorted({k for t, k, _ in self._rows if t == tenant_id})
        return [c for c in (self.latest(tenant_id, k) for k in codes) if c is not None]

    def save(self, claim: Claim) -> None:
        key = (claim.tenant_id, claim.code, claim.version)
        stored = self._rows.get(key)
        if stored is not None:
            if stored.id != claim.id:
                raise ValueError(f"{claim.code} v{claim.version} already exists")
            content_changed = any(getattr(stored, f) != getattr(claim, f) for f in _CONTENT)
            if content_changed and stored.state is not ClaimState.CANDIDATE:
                raise ValueError(f"{claim.code} v{claim.version}: stored versions are append-only")
        self._rows[key] = claim

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
