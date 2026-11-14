"""Edges of the adapters that the flow does not reach: blob paths, claim versions, a change whose
row is hidden by row-level security."""
from __future__ import annotations

import pytest

from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.ids import new_id
from thallo.contracts.models import Bundle
from thallo.contracts.states import CLAIM, IllegalTransition
from thallo.gate.approvals import GateChange
from thallo.store.adapters import BlobError, ChangeNotApplied, ClaimWriteRefused, LocalBlobStore

from .test_wave1_flow import backend, seed, signer  # noqa: F401  (fixtures)


def test_blob_store_keeps_tenants_apart_and_stays_under_its_root(tmp_path):
    clock = FakeClock(FAKE_EPOCH)
    a, b = new_id(clock), new_id(clock, b"\x01" * 10)
    blobs = LocalBlobStore(tmp_path)
    uri = blobs.write(a, "hero.png", b"bytes")
    assert blobs.read(a, uri) == b"bytes"
    with pytest.raises(BlobError, match="another tenant"):
        blobs.read(b, uri)
    for name in ("../x.png", ".hidden", "a/b.png", "", "x" * 300):
        with pytest.raises(BlobError):
            blobs.write(a, name, b"x")
    with pytest.raises(BlobError):
        blobs.read(a, f"local:{a}/../{b}/hero.png")
    with pytest.raises(BlobError, match="not found"):
        blobs.read(a, f"local:{a}/missing.png")
    with pytest.raises(BlobError):
        blobs.read(a, "s3://bucket/hero.png")


def test_claim_versions_must_chain_and_terminal_states_stay_terminal(backend):  # noqa: F811
    shop = seed(backend, "Eta")
    t = shop.tenant.id
    with backend.session(t) as s:
        v1 = s.claims.latest(t, "C-0001")
        orphan = v1.model_copy(update={"id": new_id(backend.clock), "version": 2, "supersedes_id": None})
        with pytest.raises(ClaimWriteRefused, match="supersede"):
            s.claims.save(orphan)
        duplicate = v1.model_copy(update={"id": new_id(backend.clock)})
        with pytest.raises(ClaimWriteRefused, match="already exists"):
            s.claims.save(duplicate)
        voided = v1.model_copy(update={"state": CLAIM.check(v1.state, type(v1.state).VOIDED)})
        s.claims.save(voided)
        with pytest.raises(IllegalTransition):
            s.claims.save(voided.model_copy(update={"state": type(v1.state).VERIFIED}))


def test_a_change_to_a_row_this_tenant_cannot_see_writes_nothing(backend):  # noqa: F811
    shop = seed(backend, "Theta")
    other = seed(backend, "Iota")
    t = shop.tenant.id
    foreign = other.bundle  # another tenant's bundle, relabelled as ours
    change = GateChange(tenant_id=t, bundle=Bundle.model_validate({**foreign.model_dump(), "tenant_id": t}),
                        variants=(), voided_approvals=(), audit_entries=())
    with pytest.raises(ChangeNotApplied), backend.session(t) as s:
        s.write(change)
    with backend.session(other.tenant.id) as s:
        assert s.get(Bundle, foreign.id) == foreign
