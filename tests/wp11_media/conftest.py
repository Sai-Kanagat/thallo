"""WP-11 fixtures: an in-memory row store, a tenant folder, a registry with the manual floor."""
from __future__ import annotations

import io
from datetime import timedelta

import pytest

from api.media_store import TenantMediaStore
from thallo.connections import ConnectionRegistry, InMemoryConnectionStore, ProviderRegistry
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import ConsentBasis, ConsentScope
from thallo.contracts.models import Consent, RegionPolicy

T = "0190f3a0-0000-7000-8000-000000000001"
T2 = "0190f3a0-0000-7000-8000-000000000002"


class MemRows:
    def __init__(self) -> None:
        self.rows: dict[type, dict[str, object]] = {}

    def get(self, model, row_id):  # noqa: ANN001, ANN201
        return self.rows.get(model, {}).get(row_id)

    def list_all(self, model, **where):  # noqa: ANN001, ANN201
        return [r for r in self.rows.get(model, {}).values()
                if all(getattr(r, k) == v for k, v in where.items())]

    def insert(self, row):  # noqa: ANN001, ANN201
        self.rows.setdefault(type(row), {})[row.id] = row
        return row


@pytest.fixture
def clock():
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def root(tmp_path):
    r = (tmp_path / "brands").resolve()
    r.mkdir(mode=0o700)
    (r / T).mkdir(mode=0o700)
    return r


@pytest.fixture
def env(root):
    return {"THALLO_BRAND_ROOT": str(root)}


@pytest.fixture
def blobs(root):
    return TenantMediaStore(root)


@pytest.fixture
def rows():
    return MemRows()


@pytest.fixture
def registry(clock):
    return ConnectionRegistry(clock=clock, providers=ProviderRegistry.with_discovered(),
                              connections=InMemoryConnectionStore(), mock_tenants={T})


def consent(clock, *, cid="0190f3a0-0000-7000-8000-0000000000c1", tenant=T, scope=ConsentScope.LIKENESS,
            given=-1, withdrawn=None):
    now = clock.now()
    return Consent(id=cid, created_at=now, tenant_id=tenant, subject_label="staff", basis=ConsentBasis(
        list(ConsentBasis)[0]), scope=scope, evidence="signed form", given_at=now + timedelta(days=given),
        withdrawn_at=None if withdrawn is None else now + timedelta(days=withdrawn))


def policy(clock, label="Made with AI"):
    return RegionPolicy(id="0190f3a0-0000-7000-8000-0000000000d1", created_at=clock.now(), tenant_id=T,
                        region_code="IT", version=1, disclosure_label=label, effective_from=clock.now())


def jpeg_with_gps(w=40, h=20, orientation=6) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (w, h), (200, 30, 30))
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "PhoneMaker"
    exif[0x8825] = {1: "N", 2: (45.0, 0.0, 0.0)}
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


def png(w=64, h=64, color=(10, 120, 200)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()
