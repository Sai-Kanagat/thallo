"""Capture through the API, in memory and on Postgres as the app role: consent, typed and
recorded answers, transcription down the chain, candidates, shots, roles and tenant isolation."""
from __future__ import annotations

import hashlib

from api.context import CSRF_HEADER
from thallo.capture.records import CaptureAnswer, CaptureQuestion, ShotRequest
from thallo.connections.registry import ConnectionRegistry
from thallo.contracts.enums import Capability, ConsentScope
from thallo.contracts.models import Claim, Consent, Media, Source
from thallo.contracts.states import ClaimState

from .conftest import PNG, WEBM


def _week(c):
    r = c.get("/api/voice?week=2026-01-07")
    assert r.status_code == 200, r.text
    return r.json()


def test_three_questions_and_shots_per_week(world):
    c, _ = world.login(world.a.staff_email)
    body = _week(c)
    assert body["week_start"] == "2026-01-05" and len(body["questions"]) == 3
    assert body["shots"] and body["can_edit"] is False
    assert _week(c)["questions"] == body["questions"]  # written once


def test_no_consent_no_recording(world):
    c, csrf = world.login(world.a.staff_email)
    q = _week(c)["questions"][0]["id"]
    r = c.post(f"/api/voice/questions/{q}/recording", content=WEBM,
               headers={CSRF_HEADER: csrf, "content-type": "audio/webm"})
    assert r.status_code == 400 and "consent" in r.json()["message"]
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        assert tx.list_all(CaptureAnswer) == [] and tx.list_all(Media, sha256=hashlib.sha256(WEBM).hexdigest()) == []


def test_record_transcribe_and_candidates(world):
    t = world.a.tenant.id
    reg = ConnectionRegistry(clock=world.svc.clock, providers=world.svc.providers,
                             connections=world.svc.backend.connection_stores()[0], vault=world.svc.vault,
                             mock_tenants=world.svc.mock_tenants)
    reg.add(t, Capability.TRANSCRIPTION, "mock")
    c, csrf = world.login(world.a.staff_email)
    q = _week(c)["questions"][0]["id"]
    h = {CSRF_HEADER: csrf, "content-type": "audio/webm;codecs=opus"}
    r = c.post(f"/api/voice/questions/{q}/recording?consent=1&language=en", content=WEBM, headers=h)
    assert r.status_code == 200, r.text
    aid = r.json()["answer_id"]
    again = c.post(f"/api/voice/questions/{q}/recording?consent=1", content=WEBM, headers=h).json()
    assert again["answer_id"] == aid and again["reused"] is True
    r = c.post(f"/api/voice/answers/{aid}/transcribe", headers={CSRF_HEADER: csrf})
    assert r.status_code == 200 and r.json()["state"] == "done", r.text
    r = c.post(f"/api/voice/answers/{aid}/transcript", json={"text": "We sold 12 cars at 3.5% this week."},
               headers={CSRF_HEADER: csrf})
    assert [x["value"] for x in r.json()["candidates"]] == ["12", "3.5"]
    with world.svc.backend.tenant(t) as tx:
        ans = tx.get(CaptureAnswer, aid)
        src = tx.get(Source, ans.source_id)
        consent = tx.get(Consent, ans.consent_id)
        media = tx.get(Media, ans.media_id)
        claims = [x for x in tx.list_all(Claim) if x.source_id == src.id]
    assert src.untrusted and consent.scope is ConsentScope.VOICE and media.consent_id == consent.id
    assert media.has_voice_track and "session ref" in consent.evidence
    assert claims and all(x.state is ClaimState.CANDIDATE for x in claims)
    view = _week(c)["questions"][0]["answers"][0]
    assert view["transcript"].startswith("We sold") and len(view["candidates"]) == 2


def test_manual_floor_asks_a_person_to_type(world):
    c, csrf = world.login(world.a.founder_email)
    r = c.post("/api/voice/interview/recording?consent=1", content=WEBM,
               headers={CSRF_HEADER: csrf, "content-type": "audio/webm"})
    assert r.status_code == 200, r.text
    aid = r.json()["answer_id"]
    assert c.post(f"/api/voice/answers/{aid}/transcribe", headers={CSRF_HEADER: csrf}).json()["state"] == \
        "needs_typing"


def test_typed_answer_and_bad_files(world):
    c, csrf = world.login(world.a.staff_email)
    q = _week(c)["questions"][1]["id"]
    r = c.post(f"/api/voice/questions/{q}/typed", json={"text": "Venduto 1.200 pezzi.", "language": "it"},
               headers={CSRF_HEADER: csrf})
    assert r.status_code == 200 and r.json()["candidates"][0]["value"] == "1200"
    for ctype, data in (("text/html", b"<script>"), ("audio/webm", b"<script>alert(1)</script>")):
        r = c.post(f"/api/voice/questions/{q}/recording?consent=1", content=data,
                   headers={CSRF_HEADER: csrf, "content-type": ctype})
        assert r.status_code == 400
    r = c.post(f"/api/voice/questions/{q}/recording?consent=1", content=WEBM, headers={"content-type": "audio/webm"})
    assert r.status_code == 403  # no CSRF token


def test_shots_and_roles(world):
    staff, scsrf = world.login(world.a.staff_email)
    shots = _week(staff)["shots"]
    assert staff.post("/api/voice/shots", json={"text": "x"}, headers={CSRF_HEADER: scsrf}).status_code == 403
    people = next((s for s in shots if s["people_in_shot"]), None)
    plain = next(s for s in shots if not s["people_in_shot"])
    r = staff.post(f"/api/voice/shots/{plain['id']}/file", content=PNG,
                   headers={CSRF_HEADER: scsrf, "content-type": "image/png"})
    assert r.status_code == 200 and r.json()["state"] == "fulfilled"
    founder, fcsrf = world.login(world.a.founder_email)
    r = founder.post("/api/voice/shots", json={"text": "The team at work", "people_in_shot": True,
                                                 "week": "2026-01-05"}, headers={CSRF_HEADER: fcsrf})
    assert r.status_code == 200
    target = people["id"] if people else r.json()["id"]
    r = staff.post(f"/api/voice/shots/{target}/file", content=PNG + b"1",
                   headers={CSRF_HEADER: scsrf, "content-type": "image/png"})
    assert r.status_code == 400 and "agreed" in r.json()["message"]
    r = staff.post(f"/api/voice/shots/{target}/file?consent=1", content=PNG + b"1",
                   headers={CSRF_HEADER: scsrf, "content-type": "image/png"})
    assert r.status_code == 200
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        shot = tx.get(ShotRequest, target)
        assert tx.get(Consent, shot.consent_id).scope is ConsentScope.LIKENESS


def test_questions_change_only_before_an_answer(world):
    founder, csrf = world.login(world.a.founder_email)
    q = _week(founder)["questions"][2]["id"]
    assert founder.put(f"/api/voice/questions/{q}", json={"text": "New wording?"},
                       headers={CSRF_HEADER: csrf}).status_code == 200
    founder.post(f"/api/voice/questions/{q}/typed", json={"text": "An answer."}, headers={CSRF_HEADER: csrf})
    assert founder.put(f"/api/voice/questions/{q}", json={"text": "Again?"},
                       headers={CSRF_HEADER: csrf}).status_code == 400


def test_tenant_isolation(world):
    a, acsrf = world.login(world.a.staff_email)
    qa = _week(a)["questions"][0]["id"]
    r = a.post(f"/api/voice/questions/{qa}/recording?consent=1", content=WEBM,
               headers={CSRF_HEADER: acsrf, "content-type": "audio/webm"})
    aid = r.json()["answer_id"]
    b, bcsrf = world.login(world.b.staff_email)
    week_b = _week(b)
    assert qa not in {q["id"] for q in week_b["questions"]}
    assert all(not q["answers"] for q in week_b["questions"])
    assert b.post(f"/api/voice/answers/{aid}/transcript", json={"text": "hijack"},
                  headers={CSRF_HEADER: bcsrf}).status_code == 404
    assert b.post(f"/api/voice/questions/{qa}/typed", json={"text": "x"},
                  headers={CSRF_HEADER: bcsrf}).status_code == 404
    with world.svc.backend.tenant(world.b.tenant.id) as tx:
        assert tx.get(CaptureAnswer, aid) is None and tx.get(CaptureQuestion, qa) is None
