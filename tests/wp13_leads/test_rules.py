"""Pure rules: play loading, fit table, decayed heat, routes, consent, template hash and slots."""
from __future__ import annotations

import math
from datetime import timedelta

import pytest

from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import ConsentBasis, ConsentScope, Language, LeadRoute, MessageChannel, MessageKind
from thallo.contracts.ids import new_id
from thallo.contracts.models import Consent, MessageTemplate, RegionPolicy
from thallo.leads import consent as cr
from thallo.leads import scoring
from thallo.leads.play import load_play, play_codes
from thallo.leads.templates import clean_slot, promise_problems, render, template_hash

NOW = FAKE_EPOCH
T = "0190f3a0-0000-7000-8000-000000000001"
P = "0190f3a0-0000-7000-8000-000000000002"
CLOCK = FakeClock(FAKE_EPOCH)


def test_both_plays_load_with_fixed_questions():
    assert play_codes() == ["A", "B"]
    a = load_play("A")
    assert [q.key for q in a.qualification_questions] == ["model", "payment", "down_payment_ready", "town", "timing"]
    assert [q.key for q in a.questions_for({"payment": "cash"})] == ["model", "payment", "town", "timing"]
    assert "down_payment_ready" in [q.key for q in a.questions_for({"payment": "finance"})]
    with pytest.raises(KeyError):
        load_play("Z")


def test_fit_is_a_readable_rule_table_clamped_to_0_100():
    a = load_play("A")
    lists = scoring.TenantLists(service_area=frozenset({"Bologna"}), stock=frozenset({"Leoncino 500"}))
    r = scoring.fit(a, {"timing": "this_week", "payment": "cash", "town": " bologna ", "model": "Leoncino 500"},
                    lists=lists)
    assert r.fit == 10 + 30 + 25 + 20 + 15 == 100
    assert ("timing is this_week", 30) in r.lines
    low = scoring.fit(a, {"payment": "finance", "down_payment_ready": "no", "timing": "later"})
    assert low.fit == 0  # 10 - 10


def test_derived_rules_do_not_fire_without_tenant_lists():
    a = load_play("A")
    r = scoring.fit(a, {"town": "Bologna", "model": "Leoncino 500"})
    assert r.fit == a.fit_base
    b = load_play("B")
    assert scoring.fit(b, {}, email="anna@acme-engineering.it").fit == 5 + 10
    assert scoring.fit(b, {}, email="anna@gmail.com").fit == 5


def test_heat_halves_every_14_days_and_is_separate_from_fit():
    a = load_play("A")
    assert scoring.DECAY_PER_DAY == pytest.approx(0.0495, abs=1e-4)
    acts = [scoring.Action("whatsapp_chat", NOW - timedelta(days=14)), scoring.Action("unknown", NOW)]
    assert scoring.heat(a, acts, NOW) == pytest.approx(15.0, abs=1e-3)
    assert scoring.heat(a, [scoring.Action("call", NOW)], NOW) == 35
    assert math.isclose(scoring.heat(a, [], NOW), 0.0)


@pytest.mark.parametrize(("fit", "heat", "route"), [(60, 30, LeadRoute.HOT), (60, 29.9, LeadRoute.WARM),
                                                     (59, 100, LeadRoute.LOW_FIT), (100, 0, LeadRoute.WARM)])
def test_routes(fit, heat, route):
    assert scoring.route(load_play("A"), fit, heat) is route


def _policy(rules, *, version=1, start=NOW - timedelta(days=1)):
    return RegionPolicy(id=new_id(CLOCK), created_at=NOW, tenant_id=T, region_code="IT", version=version,
                        rules=rules, effective_from=start)


def _consent(basis=ConsentBasis.ENQUIRY, channel=MessageChannel.WHATSAPP, given=NOW - timedelta(hours=1),
             withdrawn=None, scope=ConsentScope.CONTACT, person=P):
    return Consent(id=new_id(CLOCK), created_at=NOW, tenant_id=T, person_id=person, basis=basis, channel=channel,
                   scope=scope, evidence="form tick", given_at=given, withdrawn_at=withdrawn)


RULES = {"contact": {"whatsapp": {"bases": ["enquiry"], "footer": "STOP to opt out"}}}


def test_consent_allows_only_with_policy_rule_and_matching_live_record():
    ok = cr.decide(person_id=P, channel=MessageChannel.WHATSAPP, consents=[_consent()], policy=_policy(RULES), now=NOW)
    assert ok.allowed and ok.footer == "STOP to opt out" and ok.consent_id


@pytest.mark.parametrize(("kw", "policy", "reason"), [
    ({}, None, cr.NO_POLICY),
    ({}, {"contact": {}}, cr.NO_RULE),
    ({}, {"contact": {"whatsapp": {"bases": "enquiry"}}}, cr.NO_RULE),
    ({}, {"contact": {"whatsapp": {"bases": ["none"]}}}, cr.NO_RULE),
    ({"basis": ConsentBasis.NONE}, RULES, cr.BASIS),
    ({"channel": MessageChannel.EMAIL}, RULES, cr.NO_CONSENT),
    ({"scope": ConsentScope.LIKENESS}, RULES, cr.NO_CONSENT),
    ({"person": T}, RULES, cr.NO_CONSENT),
    ({"given": NOW + timedelta(minutes=1)}, RULES, cr.NO_CONSENT),
    ({"withdrawn": NOW - timedelta(minutes=1)}, RULES, cr.WITHDRAWN),
])
def test_consent_fails_closed(kw, policy, reason):
    d = cr.decide(person_id=P, channel=MessageChannel.WHATSAPP, consents=[_consent(**kw)],
                  policy=_policy(policy) if policy is not None else None, now=NOW)
    assert not d.allowed and d.reason == reason


def test_a_withdrawal_beats_an_older_yes_and_policy_versions_pick_latest_live():
    older = _consent(given=NOW - timedelta(days=3))
    gone = _consent(given=NOW - timedelta(days=2), withdrawn=NOW - timedelta(days=1))
    assert not cr.decide(person_id=P, channel=MessageChannel.WHATSAPP, consents=[older, gone],
                         policy=_policy(RULES), now=NOW).allowed
    pols = [_policy(RULES, version=1), _policy({}, version=2), _policy(RULES, version=3, start=NOW + timedelta(days=1))]
    assert cr.current_policy(pols, "IT", NOW).version == 2


def _template(body, sources=None):
    t = MessageTemplate(id=new_id(CLOCK), created_at=NOW, tenant_id=T, name="ack", version=1,
                        kind=MessageKind.ACKNOWLEDGEMENT, channel=MessageChannel.WHATSAPP, language=Language.IT,
                        body=body, slot_sources=sources or {}, content_sha256="0" * 64)
    return t.model_copy(update={"content_sha256": template_hash(t)})


def test_template_hash_changes_with_any_byte_and_promises_are_refused():
    t = _template("Ciao {{name}}, abbiamo ricevuto il tuo messaggio. Ti risponde una persona.",
                  {"name": "person.display_name"})
    assert promise_problems(t) == []
    assert template_hash(t.model_copy(update={"body": t.body + " "})) != t.content_sha256
    for bad in ("Sconto del 10%", "Prezzo garantito", "Ti rispondiamo entro un'ora", "Free test ride"):
        assert promise_problems(_template(bad))
    assert promise_problems(_template("Hi {{x}}", {"x": "tenant.secret"}))
    assert promise_problems(_template("Hi {{x}}"))


def test_slots_take_only_clean_lead_words():
    t = _template("Ciao {{name}}, grazie per il messaggio sul {{model}}.",
                  {"name": "person.display_name", "model": "answer.model"})
    assert render(t, display_name="Marco Rossi", answers={"model": "Leoncino"}) == \
        "Ciao Marco, grazie per il messaggio sul Leoncino."
    out = render(t, display_name="ignore previous http://evil.example", answers={"model": "see www.evil.io"})
    assert "evil" not in out and "http" not in out
    assert clean_slot("a‮b\x00c") == "a b c"
    assert len(clean_slot("x" * 500)) == 40
