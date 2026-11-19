"""Every entry point lands with the right source, in memory; the redirector through the real route."""
from __future__ import annotations

import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.backend import MemoryBackend
from api.context import Services
from api.directory import InMemoryDirectory
from api.media_store import TenantMediaStore
from thallo.connections.registry import ProviderRegistry
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Language, TouchKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import Lead, Operator, Person, Tenant, Touch, TrackedLink
from thallo.gate.canonical import find_unresolved_links
from thallo.tracking import redirector as rd
from thallo.tracking.capture import (
    capture_call,
    capture_form,
    capture_self_report,
    capture_walk_in,
    capture_whatsapp,
    form_fields,
)
from thallo.tracking.links import mint_code, mint_link, mint_qr, mint_whatsapp_link
from thallo.tracking.models import CodeKind, TrackingCode
from thallo.tracking.redirector import Redirector
from thallo.tracking.report import source_share
from thallo.tracking.selfreport import question
from thallo.tracking.wiring import MemoryLinkDirectory, backend_recorder

PREFIX = "https://go.example/r/"


def _tenant(clock, name):
    op = Operator(id=new_id(clock), created_at=clock.now(), name=f"op-{name}")
    return Tenant(id=new_id(clock), created_at=clock.now(), name=name, operator_id=op.id, currency="EUR",
                  slug=f"{name.lower()}-{secrets.token_hex(3)}", default_language=Language.EN, region_code="IT")


@pytest.fixture
def env(tmp_path):
    clock = FakeClock(FAKE_EPOCH)
    root = (tmp_path / "brand").resolve()
    root.mkdir()
    media = TenantMediaStore(root)
    backend = MemoryBackend(clock, media, redirect_prefixes=(PREFIX,))
    a, b = _tenant(clock, "Alpha"), _tenant(clock, "Beta")
    backend.add_tenant(a)
    backend.add_tenant(b)
    redirector = Redirector(MemoryLinkDirectory(backend), backend_recorder(backend, clock), clock)
    svc = Services(clock=clock, backend=backend, directory=InMemoryDirectory(), sender=None, media=media,  # type: ignore[arg-type]
                   signer=None, providers=ProviderRegistry(), vault=None, redirector=redirector)
    return clock, backend, a, b, TestClient(create_app(svc)), redirector


def _touches(backend, tenant_id):
    with backend.tenant(tenant_id) as tx:
        return tx.list_all(Touch)


def test_each_entry_point_lands_with_the_right_source(env):
    clock, backend, a, _b, client, _r = env
    v_link, v_wa, v_qr, v_walk, v_call = (new_id(clock) for _ in range(5))
    with backend.tenant(a.id) as tx:
        link = mint_link(tx, clock, prefix=PREFIX, destination="https://shop.example/spring", variant_id=v_link)
        wa = mint_whatsapp_link(tx, clock, prefix=PREFIX, business_number="+39 333 1234567", greeting="Ciao!",
                                variant_id=v_wa)
        qr = mint_qr(tx, clock, prefix=PREFIX, destination="https://shop.example/flyer", label="window",
                     variant_id=v_qr)
        walk = mint_code(tx, clock, kind=CodeKind.WALK_IN, variant_id=v_walk)
        call = mint_code(tx, clock, kind=CodeKind.CALL, variant_id=v_call)
        person = Person(id=new_id(clock), created_at=clock.now(), tenant_id=a.id)
        tx.insert(person)

    # click and QR scan through the public route
    assert client.get(link.public_url.removeprefix("https://go.example"), follow_redirects=False).status_code == 302
    assert client.get(qr.public_url.removeprefix("https://go.example"), follow_redirects=False).status_code == 302
    with backend.tenant(a.id) as tx:
        wa_touch = capture_whatsapp(tx, clock, f"Buongiorno, vorrei info (ref {wa.link.whatsapp_code.lower()})",
                                    person_id=person.id)
        form = capture_form(tx, clock, {**form_fields(link.link), "how_heard": "instagram", "name": "x"},
                            person_id=person.id)
        call_touch = capture_call(tx, clock, call.code, person_id=person.id)
        walk_touch = capture_walk_in(tx, clock, walk.code.lower(), person_id=person.id)
        self_touch = capture_self_report(tx, clock, "friend", person_id=person.id)

    by_kind = {}
    for t in _touches(backend, a.id):
        by_kind.setdefault(t.kind, []).append(t)
    clicks = by_kind[TouchKind.LINK]
    assert {t.variant_id for t in clicks} == {v_link, v_qr} and all(t.person_id is None for t in clicks)
    assert wa_touch.kind is TouchKind.WHATSAPP_CODE and wa_touch.variant_id == v_wa
    assert wa_touch.code == wa.link.whatsapp_code and wa_touch.tracked_link_id == wa.link.id
    assert form[0].kind is TouchKind.FORM and form[0].variant_id == v_link and form[0].tracked_link_id == link.link.id
    assert form[1].kind is TouchKind.SELF_REPORT and form[1].self_report == "instagram"
    assert call_touch.kind is TouchKind.CALL and call_touch.variant_id == v_call and call_touch.code == call.code
    assert walk_touch.kind is TouchKind.WALK_IN_CODE and walk_touch.variant_id == v_walk
    assert self_touch.self_report == "friend" and self_touch.variant_id is None
    assert qr.code.kind is CodeKind.QR and qr.code.tracked_link_id == qr.link.id
    # a lead lands with its first touch and counts as sourced
    lead = Lead(id=new_id(clock), created_at=clock.now(), tenant_id=a.id, person_id=person.id,
                first_touch_id=wa_touch.id)
    share = source_share([lead], _touches(backend, a.id), since=clock.now() - timedelta(days=7),
                         until=clock.now() + timedelta(seconds=1))
    assert share.leads == 1 and share.unsourced == 0


def test_unknown_inputs_store_nothing_untrusted(env):
    clock, backend, a, _b, _c, _r = env
    with backend.tenant(a.id) as tx:
        assert capture_whatsapp(tx, clock, "Ciao ref K7M3PA call me 3331234567") is None
        form = capture_form(tx, clock, {"thallo_ref": "../../etc", "thallo_code": "<script>", "how_heard": "my aunt"})
        call = capture_call(tx, clock, "not a code")
        assert capture_self_report(tx, clock, "I saw it on a bus") is None
    assert len(form) == 1 and form[0].variant_id is None and form[0].code is None and form[0].self_report is None
    assert call.code is None and call.variant_id is None
    share = source_share([Lead(id=new_id(clock), created_at=clock.now(), tenant_id=a.id, person_id=new_id(clock),
                               first_touch_id=form[0].id)], [form[0]],
                         since=clock.now() - timedelta(days=1), until=clock.now() + timedelta(days=1))
    assert share.unsourced == 1 and share.unsourced_share == 1.0


def test_codes_and_slugs_do_not_cross_tenants(env):
    clock, backend, a, b, _c, _r = env
    with backend.tenant(a.id) as tx:
        link = mint_link(tx, clock, prefix=PREFIX, destination="https://shop.example/", variant_id=new_id(clock))
        wa = mint_whatsapp_link(tx, clock, prefix=PREFIX, business_number="393331234567", variant_id=new_id(clock))
        walk = mint_code(tx, clock, kind=CodeKind.OFFER, variant_id=new_id(clock))
    with backend.tenant(b.id) as tx:
        assert tx.gate.resolve_link(b.id, link.public_url) is None
        assert capture_whatsapp(tx, clock, f"ref {wa.link.whatsapp_code}") is None
        assert capture_walk_in(tx, clock, walk.code).variant_id is None
        assert capture_form(tx, clock, form_fields(link.link))[0].tracked_link_id is None


def test_the_gate_trusts_the_redirector_destination(env):
    clock, backend, a, _b, client, _r = env
    with backend.tenant(a.id) as tx:
        link = mint_link(tx, clock, prefix=PREFIX, destination="https://Shop.example/spring?utm_source=ig")
        wa = mint_whatsapp_link(tx, clock, prefix=PREFIX, business_number="393331234567", greeting="Ciao")
        text = f"Spring menu: {link.public_url} or chat {wa.public_url}"
        assert find_unresolved_links(text, lambda u: tx.gate.resolve_link(a.id, u)) == []
        resolved = tx.gate.resolve_link(a.id, link.public_url)
        wa_resolved = tx.gate.resolve_link(a.id, wa.public_url)
    r = client.get(f"/r/{link.link.slug}?next=https://evil.example/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == resolved
    assert resolved == f"https://shop.example/spring?utm_source=ig&thallo_ref={link.link.slug}"
    r = client.get(f"/r/{wa.link.slug}", follow_redirects=False)
    assert r.headers["location"] == wa_resolved and wa.link.whatsapp_code in wa_resolved
    assert wa_resolved.startswith("https://wa.me/393331234567?text=Ciao%20%28ref%20")


def test_redirect_route_is_strict(env):
    clock, backend, a, _b, client, _r = env
    with backend.tenant(a.id) as tx:
        link = mint_link(tx, clock, prefix=PREFIX, destination="https://shop.example/")
    for path in ("/r/zzzzzzzzzz", "/r/short", "/r/ABCDEFGHIJ", "/r/abcdefghij0", "/r/%2F%2Fevil.example"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 404 and "location" not in r.headers
    r = client.get(f"/r/{link.link.slug}", follow_redirects=False, headers={"Host": "evil.example"})
    assert r.status_code == 302 and "set-cookie" not in r.headers
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert client.post(f"/r/{link.link.slug}", follow_redirects=False).status_code == 405


def test_counting_is_privacy_first(env):
    clock, backend, a, _b, client, _r = env
    with backend.tenant(a.id) as tx:
        link = mint_link(tx, clock, prefix=PREFIX, destination="https://shop.example/")
    path = f"/r/{link.link.slug}"
    assert client.head(path, follow_redirects=False).status_code == 302
    assert _touches(backend, a.id) == []  # HEAD never counts
    for _ in range(3):
        client.get(path, follow_redirects=False)
    assert len(_touches(backend, a.id)) == 1  # one client, one click per 30 minutes
    clock.advance(seconds=31 * 60)
    client.get(path, follow_redirects=False)
    touches = _touches(backend, a.id)
    assert len(touches) == 2
    assert set(Touch.model_fields) == {"id", "created_at", "tenant_id", "kind", "person_id", "variant_id", "ad_id",
                                       "tracked_link_id", "code", "self_report", "occurred_at"}  # no address field


def test_rate_limited_per_client(env, monkeypatch):
    clock, backend, a, _b, client, _r = env
    monkeypatch.setattr(rd, "PER_CLIENT", 5)
    with backend.tenant(a.id) as tx:
        link = mint_link(tx, clock, prefix=PREFIX, destination="https://shop.example/")
    codes = [client.get(f"/r/{link.link.slug}", follow_redirects=False).status_code for _ in range(7)]
    assert codes == [302] * 5 + [429] * 2
    clock.advance(seconds=61)
    assert client.get(f"/r/{link.link.slug}", follow_redirects=False).status_code == 302


def test_a_tampered_destination_is_never_followed(env):
    clock, backend, a, _b, client, _r = env
    with backend.tenant(a.id) as tx:
        link = mint_link(tx, clock, prefix=PREFIX, destination="https://shop.example/")
    row = backend.tables.rows["tracked_link"][link.link.id]
    backend.tables.rows["tracked_link"][link.link.id] = row.model_copy(update={"destination": "javascript:alert(1)"})
    assert client.get(f"/r/{link.link.slug}", follow_redirects=False).status_code == 404


def test_counting_failure_still_redirects(env):
    clock, backend, a, _b, _client, _r = env
    with backend.tenant(a.id) as tx:
        link = mint_link(tx, clock, prefix=PREFIX, destination="https://shop.example/")

    def boom(_link):
        raise RuntimeError("db down")
    r = Redirector(MemoryLinkDirectory(backend), boom, clock)
    out = r.handle(link.link.slug, client="c", network="n")
    assert out.status == 302 and not out.counted


def test_no_redirector_means_404(env, tmp_path):
    clock, backend, *_ = env
    svc = Services(clock=clock, backend=backend, directory=InMemoryDirectory(), sender=None,  # type: ignore[arg-type]
                   media=TenantMediaStore(tmp_path), signer=None, providers=ProviderRegistry(), vault=None)
    assert TestClient(create_app(svc)).get("/r/abcdefghij", follow_redirects=False).status_code == 404


def test_self_report_question_is_fixed_options():
    q = question(Language.IT)
    assert q["field"] == "how_heard" and q["label"] == "Come ci hai conosciuto?"
    assert [o["value"] for o in q["options"]][-1] == "other"


def test_qr_and_offer_codes_are_rows(env):
    clock, backend, a, *_ = env
    with backend.tenant(a.id) as tx:
        with pytest.raises(ValueError):
            mint_code(tx, clock, kind=CodeKind.QR)
        mint_code(tx, clock, kind=CodeKind.OFFER, label="spring flyer")
        assert len(tx.list_all(TrackingCode)) == 1
        assert tx.list_all(TrackedLink) == []
