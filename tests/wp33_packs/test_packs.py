"""WP-33 without a database: export rules, privacy, signature, expiry, import as candidates."""
from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest

from tests.wp33_packs.conftest import DAY, LANGS, FakeVerifier, human, library, now_clock, signer, trusting
from thallo.contracts.enums import ClaimKind, Language, SourceKind
from thallo.contracts.ids import new_id
from thallo.contracts.states import ClaimState
from thallo.contracts.testing.samples import T, _id
from thallo.extensions.packs import (
    PackError,
    export_pack,
    import_pack,
    open_pack,
    to_bytes,
    verify_pack_claim,
)
from thallo.extensions.packs.pack import canonical, sign_body, text_problems
from thallo.truth import InMemoryClaimRepository, build
from thallo.truth.compiler import CompileFailed
from thallo.truth.fill import LibraryError

MINUTE = timedelta(minutes=1)
B = "0190f3a0-0000-7000-8000-0000000000bb"


def make(codes=("C-0001", "C-0002", "C-0004"), *, days=100, s=None, **kw):
    clock = now_clock()
    claims, sources = library(**kw)
    s = s or signer()
    pack = export_pack(claims=claims, sources=sources, codes=list(codes), pack_id="maker-model-x", title="Model X",
                       publisher="Maker", expires_at=clock.now() + days * DAY, languages=LANGS, clock=clock,
                       signer=s)
    return pack, s, clock, claims


def do_import(data, s, clock, tenant=B, **kw):
    return import_pack(data, trusted=trusting(s), tenant_id=tenant, existing_codes=["C-0001", "C-0007"],
                       tenant_languages=LANGS, clock=clock, actor=human(tenant), **kw)


def test_export_carries_no_ids_or_private_fields():
    pack, *_ = make()
    text = to_bytes(pack).decode()
    claims, sources = library()
    for secret in [T, *[c.id for c in claims.values()], *sources, _id(71), "private note", "ab" * 32]:
        assert secret not in text
    item = pack.body["items"][0]
    assert set(item) == {"key", "kind", "value", "unit", "as_of", "languages", "source"}
    assert set(item["source"]) == {"title", "uri", "captured_on"}


@pytest.mark.parametrize("codes,msg", [(["C-0003"], "price"), (["C-0009"], "unknown"), ([], "pick")])
def test_export_refuses_what_may_not_travel(codes, msg):
    with pytest.raises(PackError, match=msg):
        make(codes)


def test_export_refuses_private_sources_and_unverified():
    for kw in ({"source_kind": SourceKind.VOICE_ANSWER}, {"source_kind": SourceKind.SELLER_NOTE},
               {"consent_id": _id(300)}):
        with pytest.raises(PackError, match="source"):
            make(["C-0004"], **kw)
    clock = now_clock()
    claims, sources = library()
    claims["C-0004"] = claims["C-0004"].model_copy(update={"state": ClaimState.CANDIDATE})
    with pytest.raises(PackError, match="verified"):
        export_pack(claims=claims, sources=sources, codes=["C-0004"], pack_id="abc", title="t", publisher="p",
                    expires_at=clock.now() + DAY, languages=LANGS, clock=clock, signer=signer())


def test_pack_expiry_bounded_by_claims_and_a_year():
    with pytest.raises(PackError, match="before the pack"):
        make(["C-0001"], days=300)
    with pytest.raises(PackError, match="365"):
        make(["C-0004"], days=400)


def test_personal_data_in_text_refused():
    assert text_problems("call +39 051 123 4567") and text_problems("ask anna@shop.it")
    assert text_problems("see www.x.it") and text_problems("follow @dealer")
    assert not text_problems("Runs on solar power")
    clock = now_clock()
    claims, sources = library()
    claims["C-0004"] = claims["C-0004"].model_copy(update={"value": "Ask Mario at mario@dealer.it"})
    with pytest.raises(PackError, match="personal"):
        export_pack(claims=claims, sources=sources, codes=["C-0004"], pack_id="abc", title="t", publisher="p",
                    expires_at=clock.now() + DAY, languages=LANGS, clock=clock, signer=signer())


def test_import_makes_fresh_candidates_with_pack_expiry():
    pack, s, clock, _ = make()
    out = do_import(to_bytes(pack), s, clock)
    assert [c.code for c in out.claims] == ["C-0008", "C-0009", "C-0010"]
    assert all(c.state is ClaimState.CANDIDATE and c.tenant_id == B and c.verified_by is None for c in out.claims)
    assert {c.expires_at for c in out.claims} == {out.expires_at} == {clock.now() + 100 * DAY}
    assert all(c.source_id in {src.id for src in out.sources} and c.evidence_sha256 for c in out.claims)
    assert len(out.links) == 3 and len(out.events) == 3


def test_imported_claim_fills_no_slot_until_this_tenant_verifies():
    pack, s, clock, _ = make()
    out = do_import(to_bytes(pack), s, clock)
    repo = InMemoryClaimRepository(out.claims)
    lib = {c.code: c for c in out.claims}
    with pytest.raises((CompileFailed, LibraryError)):
        build("Warranty of {{C-0008}}.", tenant_id=B, language=Language.EN, claims=lib, clock=clock,
              default_language=Language.EN)
    claim, link = out.claims[0], out.links[0]
    verified, _ = verify_pack_claim(claim, link, human(B), verifier=FakeVerifier(), clock=clock, repo=repo,
                                    tenant_languages=LANGS)
    assert verified.expires_at == link.pack_expires_at
    lib[claim.code] = verified
    built = build("Warranty of {{C-0008}}.", tenant_id=B, language=Language.EN, claims=lib, clock=clock,
                  default_language=Language.EN)
    assert "5" in built.filled.text
    with pytest.raises(PackError, match="another claim"):
        verify_pack_claim(out.claims[1], link, human(B), verifier=FakeVerifier(), clock=clock, repo=repo)


def test_tampering_wrong_key_and_key_swap_refused():
    pack, s, clock, _ = make()
    raw = json.loads(to_bytes(pack))
    flipped = json.loads(json.dumps(raw))
    flipped["body"]["items"][0]["value"] = "50"
    with pytest.raises(PackError, match="signature"):
        do_import(flipped, s, clock)
    with pytest.raises(PackError, match="does not trust"):
        do_import(raw, signer(), clock)
    attacker = signer()
    swapped = sign_body(open_pack(raw, trusting(s))[0].model_copy(update={"title": "Forged"}), attacker)
    with pytest.raises(PackError, match="does not trust"):
        do_import(swapped.model_dump(mode="json"), s, clock)
    raw["key_id"] = s.key_id
    raw["signature"] = base64.b64encode(b"x" * 64).decode()
    with pytest.raises(PackError, match="signature"):
        do_import(raw, s, clock)
    with pytest.raises(PackError, match="not a claim pack"):
        do_import(b"{nope", s, clock)


def test_expired_pack_blocks_import_and_verification():
    pack, s, clock, _ = make(days=10)
    out = do_import(to_bytes(pack), s, clock)
    clock.advance(10 * DAY)
    with pytest.raises(PackError, match="expired"):
        do_import(to_bytes(pack), s, clock)
    repo = InMemoryClaimRepository(out.claims)
    for claim, link in zip(out.claims, out.links, strict=True):
        with pytest.raises(PackError, match="expired"):
            verify_pack_claim(claim, link, human(B), verifier=FakeVerifier(), clock=clock, repo=repo)


def test_reimport_refused_and_importer_reruns_rules():
    pack, s, clock, _ = make()
    out = do_import(to_bytes(pack), s, clock)
    with pytest.raises(PackError, match="already"):
        do_import(to_bytes(pack), s, clock, imported_digests={out.digest})
    body = open_pack(json.loads(to_bytes(pack)), trusting(s))[0]
    bad = body.model_copy(update={"items": [body.items[0].model_copy(update={"kind": ClaimKind.PRICE,
                                                                             "unit": "EUR"})]})
    with pytest.raises(PackError, match="price"):
        do_import(sign_body(bad, s).model_dump(mode="json"), s, clock)


def test_import_refuses_future_dated_overlong_and_contact_data():
    pack, s, clock, _ = make()
    body = open_pack(json.loads(to_bytes(pack)), trusting(s))[0]
    future = body.model_copy(update={"issued_at": clock.now() + DAY, "expires_at": clock.now() + 50 * DAY})
    with pytest.raises(PackError, match="future"):
        do_import(sign_body(future, s).model_dump(mode="json"), s, clock)
    edge = body.model_copy(update={"issued_at": clock.now() + 5 * MINUTE,
                                   "expires_at": clock.now() + 5 * MINUTE + 365 * DAY})
    with pytest.raises(PackError, match="365"):
        do_import(sign_body(edge, s).model_dump(mode="json"), s, clock)
    for field, text in (("title", "Call 051 123 4567"), ("publisher", "anna@maker.it")):
        bad = body.model_copy(update={field: text})
        with pytest.raises(PackError, match="personal"):
            do_import(sign_body(bad, s).model_dump(mode="json"), s, clock)


def test_non_canonical_or_extra_fields_refused():
    pack, s, clock, _ = make()
    raw = json.loads(to_bytes(pack))
    raw["body"]["items"][0]["tenant_id"] = new_id(clock)
    resigned = {**raw, "signature": base64.b64encode(s.sign(b"thallo/claim-pack/v1\n" + canonical(raw["body"])))
                .decode()}
    with pytest.raises(PackError, match="malformed"):
        do_import(resigned, s, clock)
