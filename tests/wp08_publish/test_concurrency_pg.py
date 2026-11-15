"""Two publishers racing on one variant, on Postgres as the per-run app role: one publication, one
provider call. The database decides (unique variant and key on `publication`, ON CONFLICT DO
NOTHING), not the code."""
# ruff: noqa: F811
from __future__ import annotations

import threading

import psycopg
import pytest

from tests.integration.conftest import pg_urls  # noqa: F401
from tests.integration.test_wave1_flow import PgBackend, _id, approve, seed, signer  # noqa: F401
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.models import Publication
from thallo.publish import PgPublishStore, PublicationSend, SendState, Status
from thallo.store.adapters import LocalBlobStore

from .harness import build_rig


@pytest.fixture
def pg(pg_urls, tmp_path):
    return PgBackend(pg_urls, FakeClock(FAKE_EPOCH), LocalBlobStore(tmp_path / "blobs"))


class RacingStore(PgPublishStore):
    """Both publishers reach the claim before either writes it."""

    barrier: threading.Barrier

    def claim(self, publication, send):
        self.barrier.wait(timeout=20)
        return super().claim(publication, send)


def test_concurrent_publish_of_one_variant_makes_one_publication_and_one_call(pg, signer):
    shop = seed(pg, "Race")
    approve(pg, shop, signer)
    rig = build_rig(pg, shop, signer)
    RacingStore.barrier = threading.Barrier(2)
    rig.store_class = RacingStore
    outcomes = []

    def go():
        outcomes.append(rig.publisher.publish(tenant_id=rig.t, bundle_id=shop.bundle.id, variant_id=shop.variant.id,
                                              approval_id=shop.approval.id, channel_account_id=rig.account.id))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=60)
    assert sorted(o.status.value for o in outcomes) == sorted([Status.SENT.value, Status.IN_FLIGHT.value])
    assert len(rig.poster.calls) == 1
    with pg.session(rig.t) as s:
        assert len(s.list(Publication)) == 1
        (pub,) = s.list(Publication)
        assert s.get(PublicationSend, pub.id).state is SendState.SENT


def test_the_database_refuses_a_second_publication_for_a_variant(pg, signer):
    shop = seed(pg, "Unique")
    approve(pg, shop, signer)
    rig = build_rig(pg, shop, signer)
    assert rig.publish().status is Status.SENT
    with pg.session(rig.t) as s:
        (pub,) = s.list(Publication)
        dup = pub.model_copy(update={"id": _id(pg), "idempotency_key": "other:key"})
        with pytest.raises(psycopg.errors.UniqueViolation):
            s.repo.insert(dup)


def test_the_app_role_cannot_delete_a_send_record(pg, signer):
    shop = seed(pg, "Keep")
    approve(pg, shop, signer)
    rig = build_rig(pg, shop, signer)
    rig.publish()
    with pytest.raises(psycopg.errors.InsufficientPrivilege), pg.session(rig.t) as s:
        s.repo.execute("DELETE FROM publication_send")


def test_send_records_are_tenant_isolated(pg, signer):
    a = seed(pg, "IsoA")
    approve(pg, a, signer)
    b = seed(pg, "IsoB")
    rig = build_rig(pg, a, signer)
    rig.publish()
    with pg.session(b.tenant.id) as s:
        assert s.repo.execute("SELECT count(*) FROM publication_send").fetchone()[0] == 0
