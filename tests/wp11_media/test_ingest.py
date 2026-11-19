from __future__ import annotations

import hashlib
import io
from decimal import Decimal

import pytest

from thallo.checks.origin import ingest_provenance, origin_of, same_source
from thallo.connections.registry import Served
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode, ConsentScope, MediaKind, MediaProvenance
from thallo.contracts.models import Media
from thallo.media.ingest import (
    IngestError,
    MediaFile,
    check_consent,
    clean_image,
    ingest_result,
    ingest_upload,
)

from .conftest import T2, T, consent, jpeg_with_gps, png, policy


def test_upload_strips_metadata_and_applies_orientation():
    clean, mime, w, h = clean_image(jpeg_with_gps(40, 20, orientation=6), "image/jpeg")
    from PIL import Image

    img = Image.open(io.BytesIO(clean))
    assert mime == "image/jpeg" and (w, h) == (20, 40) and img.size == (20, 40)
    assert not img.getexif() and b"PhoneMaker" not in clean and "icc_profile" not in img.info


@pytest.mark.parametrize(("data", "mime"), [(png(), "image/jpeg"), (b"<svg/>", "image/svg+xml"), (b"", "image/png"),
                                            (b"GIF89a....", "image/gif")])
def test_upload_refuses_wrong_or_unlisted_types(data, mime):
    with pytest.raises(IngestError):
        clean_image(data, mime)


def test_upload_goes_through_manual_and_records_everything(rows, blobs, registry, clock):
    got = ingest_upload(rows, blobs, registry, tenant_id=T, data=png(), mime="image/png", clock=clock,
                        alt_text="the shop counter")
    m, f = got.media, got.file
    assert m.provenance is MediaProvenance.CLIENT_PHOTO and m.kind is MediaKind.IMAGE
    assert m.storage_uri.startswith(f"local:{T}/media-") and hashlib.sha256(blobs.read(T, m.storage_uri)).hexdigest() \
        == m.sha256
    assert f.mode is ConnectionMode.MANUAL and f.cost_eur == 0 and not f.flagged and f.media_id == m.id
    assert same_source(m.provenance, origin_of(m, registry))  # the check stack's own comparison agrees
    again = ingest_upload(rows, blobs, registry, tenant_id=T, data=png(), mime="image/png", clock=clock)
    assert again.reused and again.media.id == m.id and len(rows.list_all(MediaFile)) == 1


def test_upload_with_people_needs_a_live_likeness_consent(rows, blobs, registry, clock):
    with pytest.raises(IngestError, match="consent"):
        ingest_upload(rows, blobs, registry, tenant_id=T, data=png(), mime="image/png", clock=clock,
                      people_in_shot=True)
    ok = consent(clock)
    rows.insert(ok)
    got = ingest_upload(rows, blobs, registry, tenant_id=T, data=png(), mime="image/png", clock=clock,
                        people_in_shot=True, consent_id=ok.id)
    assert got.media.consent_id == ok.id


@pytest.mark.parametrize(("kw", "why"), [({"given": 2}, "not given yet"), ({"withdrawn": -0}, "withdrawn"),
                                         ({"tenant": T2}, "no likeness"), ({"scope": ConsentScope.VOICE}, "cover")])
def test_check_consent_matches_the_gate_rules(rows, clock, kw, why):
    row = consent(clock, **kw)
    rows.insert(row)
    with pytest.raises(IngestError, match=why):
        check_consent(rows, T, row.id, ConsentScope.LIKENESS, clock)


def test_piece_override_lands_on_the_media_file(rows, blobs, registry, clock):
    got = ingest_upload(rows, blobs, registry, tenant_id=T, data=png(color=(1, 2, 3)), mime="image/png", clock=clock,
                        piece_id="piece-7")
    assert got.file.choice_scope is c.OverrideScope.PIECE and got.file.choice_ref == "piece-7"


def _generated(blobs, data, provider="generated_hosted", mode=ConnectionMode.HOSTED):
    uri = blobs.write(T, "gen.png", data)
    out = c.MediaResult(kind=MediaKind.IMAGE, sha256=hashlib.sha256(data).hexdigest(), mime="image/png",
                        storage_uri=uri, provenance=MediaProvenance.GENERATED_BACKGROUND, width=64, height=64,
                        disclosure_required=True)
    from thallo.contracts.clock import FAKE_EPOCH

    res = c.RunResult(output=out, cost_eur=Decimal("0.04"), provenance=c.Provenance(
        made_by="provider", provider=provider, mode=mode, model="img-1",
        settings={"licence": "x"} if provider == "stock" else {}, made_at=FAKE_EPOCH))
    return Served("0190f3a0-0000-7000-8000-0000000000e1", provider, mode, res)


def test_generated_result_needs_the_region_label_then_is_flagged(rows, blobs, clock):
    req = c.ImageRequest(tenant_id=T, purpose="background", prompt="soft paper texture", width=64, height=64)
    served = _generated(blobs, png(color=(9, 9, 9)))
    with pytest.raises(IngestError, match="disclosure"):
        ingest_result(rows, blobs, served, req, clock=clock)
    rows.insert(policy(clock))
    got = ingest_result(rows, blobs, served, req, clock=clock)
    assert got.media.provenance is ingest_provenance(served) is MediaProvenance.GENERATED
    assert got.media.disclosure_label == "Made with AI" and got.file.flagged
    assert got.file.cost_eur == Decimal("0.04") and got.media.generation_prompt == "soft paper texture"


def test_stock_licence_is_recorded(rows, blobs, clock):
    rows.insert(policy(clock))
    req = c.ImageRequest(tenant_id=T, purpose="illustration", prompt="olive tree", width=64, height=64)
    got = ingest_result(rows, blobs, _generated(blobs, png(color=(5, 5, 5)), "stock"), req, clock=clock)
    assert got.file.licence == "x"


def test_result_bytes_must_match_the_reported_hash(rows, blobs, clock):
    served = _generated(blobs, png())
    blobs.write(T, "gen.png", png(color=(0, 0, 1)))  # swapped after the run
    req = c.ImageRequest(tenant_id=T, purpose="background", prompt="p", width=64, height=64)
    with pytest.raises(IngestError, match="hash"):
        ingest_result(rows, blobs, served, req, clock=clock)


def test_generated_people_need_consent_at_ingest(rows, blobs, clock):
    rows.insert(policy(clock))
    withdrawn = consent(clock, withdrawn=-1)
    rows.insert(withdrawn)
    req = c.ImageRequest(tenant_id=T, purpose="background", prompt="p", width=64, height=64,
                         allow_generated_people=True, consent_id=withdrawn.id)
    with pytest.raises(IngestError, match="withdrawn"):
        ingest_result(rows, blobs, _generated(blobs, png(color=(7, 7, 7))), req, clock=clock)
    assert rows.list_all(Media) == []


def test_pending_manual_result_is_not_ingested(rows, blobs, registry, clock):
    req = c.ImageRequest(tenant_id=T, purpose="upload", width=10, height=10, idempotency_key="k-1")
    served = registry.run(T, req)
    assert served.result.status == "pending_person" and served.provider == "manual"
    with pytest.raises(IngestError, match="person"):
        ingest_result(rows, blobs, served, req, clock=clock)
    assert Capability.IMAGE
