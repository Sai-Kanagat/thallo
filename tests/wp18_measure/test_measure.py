"""WP-18 on the memory backend: ladder, sources side by side, shrinkage, the evidence rule, caps
and sign-off. All numbers are invented test data."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from tests.wp18_measure.scenarios import SHARES, strong_pillar
from tests.wp18_measure.world import WON_NO_AMOUNT, LeadSpec, Seeder
from thallo.contracts.actors import AgentActor
from thallo.contracts.enums import Channel, LeadRoute, MetricDay
from thallo.contracts.ids import new_id
from thallo.contracts.models import Brief, Forecast, ForecastScore, MetricSnapshot, Publication
from thallo.gate.signing import GateSigner
from thallo.measure import decide
from thallo.measure.decide import NO_DECISION, DecisionRefused, capped_shares, forecast_credit, propose_month
from thallo.measure.estimate import estimate, prob_beats
from thallo.measure.ladder import pieces, sources
from thallo.measure.records import DecisionKind, DecisionProposal, DecisionState


def seeder(mem, tx) -> Seeder:
    return Seeder(tx, mem.clock, mem.founder.id)


# ---------------------------------------------------------------- ladder


def test_won_revenue_comes_only_from_recorded_outcomes(mem):
    with mem.backend.tenant(mem.tenant.id) as tx:
        s = seeder(mem, tx)
        s.pillar_set(SHARES)
        v = s.piece(pillar="Craft")
        s.lead(v.id, LeadSpec(won=Decimal("250")))
        s.lead(v.id, LeadSpec(won=WON_NO_AMOUNT))
        s.lead(v.id, LeadSpec(route=LeadRoute.LOW_FIT))
        [row] = pieces(tx, mem.clock.now())
    first = row.rungs["first_touch"]
    assert first["won"] == 2 and first["booked"] == 2 and first["qualified"] == 2
    assert row.won_amount["first_touch"] == "250"  # never deal.value, never an estimate
    assert row.won_amount_missing["first_touch"] == 1
    assert row.as_dict()["self_report"] == "unavailable per piece"
    assert row.first_pass_approved and row.edits == 0


def test_first_and_last_touch_are_separate_views(mem):
    with mem.backend.tenant(mem.tenant.id) as tx:
        s = seeder(mem, tx)
        s.pillar_set(SHARES)
        a, b = s.piece(pillar="Craft"), s.piece(pillar="Offers")
        s.lead(a.id, LeadSpec(booked=True, last_touch_on=b.id))
        rows = {r.variant_id: r for r in pieces(tx, mem.clock.now())}
    assert rows[a.id].rungs["first_touch"]["booked"] == 1 and rows[a.id].rungs["last_touch"]["booked"] == 0
    assert rows[b.id].rungs["first_touch"]["booked"] == 0 and rows[b.id].rungs["last_touch"]["booked"] == 1


def test_young_pieces_wait_and_missing_metrics_are_not_zero(mem):
    with mem.backend.tenant(mem.tenant.id) as tx:
        s = seeder(mem, tx)
        s.pillar_set(SHARES)
        s.piece(pillar="Craft", days_ago=5)
        old = s.piece(pillar="Craft", days_ago=30, edited=True)
        pub = tx.list_all(Publication, variant_id=old.id)[0]
        tx.insert(MetricSnapshot(id=new_id(mem.clock), created_at=mem.clock.now(), tenant_id=mem.tenant.id,
                                 publication_id=pub.id, day=MetricDay.D14, metrics={"saves": 3, "shares": 1},
                                 missing=["comments"], pulled_at=mem.clock.now()))
        [row] = pieces(tx, mem.clock.now())
    assert row.variant_id == old.id
    assert row.rungs["first_touch"]["engaged"] is None
    assert not row.first_pass_approved and row.edits == 1


def test_three_sources_side_by_side_with_unsourced_share(mem):
    with mem.backend.tenant(mem.tenant.id) as tx:
        s = seeder(mem, tx)
        s.pillar_set(SHARES)
        v = s.piece(pillar="Craft", channel=Channel.FACEBOOK)
        s.lead(v.id, LeadSpec(self_report="friend"))
        s.lead(None, LeadSpec())
        s.lead(None, LeadSpec(self_report="something typed"))
        now = mem.clock.now()
        out = sources(tx, since=now - timedelta(days=30), until=now)
    by = {r["source"]: r for r in out["by_source"]}
    assert by["facebook"]["first_touch"] == 1 and by["facebook"]["last_touch"] == 1
    assert by["said:friend"]["self_report"] == 1 and by["said:unrecognised"]["self_report"] == 1
    assert by["none"] == {"source": "none", "first_touch": 2, "last_touch": 2, "self_report": 1}
    assert out["leads"] == 3 and out["unsourced"] == 1


# ---------------------------------------------------------------- shrinkage


def test_three_from_four_does_not_beat_ten_from_forty_on_shrinkage_alone():
    p = prob_beats((3, 4), (10, 40), 13 / 44)
    assert p is not None and p < 0.80
    e = estimate(3, 4, 13 / 44)
    assert e.small_sample and e.as_dict()["label"] == "small sample"
    assert e.rate < e.raw_rate and e.low < e.rate < e.high
    assert estimate(0, 0, 0.3).rate is None and prob_beats((1, 0), (1, 1), 0.3) is None
    assert prob_beats((3, 4), (10, 40), 13 / 44) == p  # fixed seed: same rows, same number


# ---------------------------------------------------------------- decisions


def test_too_little_data_says_no_decision_this_month_once(mem):
    with mem.backend.tenant(mem.tenant.id) as tx:
        s = seeder(mem, tx)
        strong_pillar(s, pieces_per_pillar=6)
        out = propose_month(tx, mem.clock)
        again = propose_month(tx, mem.clock)
    [row] = out
    assert row.kind is DecisionKind.NO_DECISION and row.summary == NO_DECISION == "no decision this month"
    assert row.state is DecisionState.NOTED
    assert [r.id for r in again] == [row.id]
    assert any("pieces on a side" in c["reason"] for c in row.evidence["checked"])


def check_pillar_change(rows: list[DecisionProposal]) -> DecisionProposal:
    [p] = rows
    assert p.kind is DecisionKind.CHANGE and p.dimension == "pillar" and p.state is DecisionState.PROPOSED
    shares = p.change["shares"]
    before = {k: Decimal(v["from"]) for k, v in shares.items()}
    after = {k: Decimal(v["to"]) for k, v in shares.items()}
    assert sum(after.values()) == sum(before.values())
    assert all(abs(after[k] - before[k]) <= Decimal("0.10") for k in after)
    assert all(v >= Decimal("0.10") for v in after.values())
    assert p.change["reserved_for_new_ideas"] == "0.20"
    ev = p.evidence
    assert ev["rung"] == "booked"
    for view in ("first_touch", "last_touch"):
        prob = ev[view]["p_this_beats_rest"]
        assert prob >= 0.8 or prob <= 0.2
        assert ev[view]["this"]["interval_90"][0] is not None
    assert ev["review"]["this"]["first_pass_rate"] == 1.0  # approval beside outcome
    return p


def test_clear_evidence_makes_one_capped_pillar_move(mem):
    with mem.backend.tenant(mem.tenant.id) as tx:
        strong_pillar(seeder(mem, tx))
        rows = propose_month(tx, mem.clock)
    check_pillar_change(rows)


def test_views_that_disagree_give_no_decision(mem):
    with mem.backend.tenant(mem.tenant.id) as tx:
        s = seeder(mem, tx)
        s.pillar_set(SHARES)
        team = [s.piece(pillar="Team", days_ago=20 + i) for i in range(10)]
        for i in range(10):
            v = s.piece(pillar="Craft", days_ago=20 + i)
            s.lead(v.id, LeadSpec(booked=True, last_touch_on=team[i].id))
            s.lead(v.id, LeadSpec(booked=True, last_touch_on=team[i].id))
        [row] = propose_month(tx, mem.clock)
    assert row.kind is DecisionKind.NO_DECISION
    craft = next(c for c in row.evidence["checked"] if c["subject"] == "Craft")
    assert craft["reason"] == "the views disagree"


def test_channel_gets_keep_and_stop():
    from datetime import UTC, datetime

    from thallo.measure.ladder import PieceRow

    def row(i, channel, booked):
        r = PieceRow(f"v{i}", channel, None, None, None, None, datetime(2026, 1, 1, tzinfo=UTC), True, 0)
        r.rungs = {v: {"won": 0, "booked": booked, "qualified": booked, "high_intent": 0, "engaged": None}
                   for v in ("first_touch", "last_touch")}
        return r
    rows = [row(i, "instagram", 2) for i in range(10)] + [row(i + 10, "facebook", 0) for i in range(10)]
    assert decide.compare(rows, "channel", "instagram", "instagram").verdict == "better"
    assert decide.compare(rows, "channel", "facebook", "facebook").verdict == "worse"


def test_capped_shares_respect_cap_floor_and_total():
    s = {"a": Decimal("0.45"), "b": Decimal("0.35"), "c": Decimal("0.20")}
    up = capped_shares(s, {}, "a", up=True)
    assert up["a"] == Decimal("0.55") and sum(up.values()) == 1
    down = capped_shares(s, {}, "c", up=False)
    assert down["c"] == Decimal("0.10") and sum(down.values()) == 1
    assert capped_shares({"a": Decimal("0.9"), "b": Decimal("0.1")}, {}, "a", up=True) is None
    floored = capped_shares({"a": Decimal("0.6"), "b": Decimal("0.4")}, {"b": Decimal("0.35")}, "a", up=True)
    assert floored["b"] == Decimal("0.35") and floored["a"] == Decimal("0.65")


# ---------------------------------------------------------------- sign-off


SIGNER = GateSigner(bytes(range(32)))


def kw(mem, tx, verifier, actor=None):
    return {"actor": actor or mem.human(), "verifier": verifier, "approver": mem.founder, "audit": tx.audit,
            "signer": SIGNER}


def test_a_named_person_signs_exactly_what_they_saw(mem, verifier):
    with mem.backend.tenant(mem.tenant.id) as tx:
        strong_pillar(seeder(mem, tx))
        [p] = propose_month(tx, mem.clock)
        with pytest.raises(DecisionRefused, match="changed"):
            decide.sign(tx, mem.clock, p.id, "f" * 64, **kw(mem, tx, verifier))
        with pytest.raises(PermissionError):
            decide.sign(tx, mem.clock, p.id, p.content_sha256, **kw(mem, tx, verifier, AgentActor(id="bot")))
        with pytest.raises(PermissionError):
            decide.sign(tx, mem.clock, p.id, p.content_sha256, **kw(mem, tx, verifier, mem.human("stale")))
        signed = decide.sign(tx, mem.clock, p.id, p.content_sha256, **kw(mem, tx, verifier))
        stored = tx.get(DecisionProposal, p.id)
        with pytest.raises(DecisionRefused, match="signed"):
            decide.decline(tx, mem.clock, p.id, p.content_sha256, **kw(mem, tx, verifier))
        [entry] = [e for e in tx.audit.store.entries(mem.tenant.id) if e.subject_id == p.id]
    assert signed.state is stored.state is DecisionState.SIGNED
    assert stored.signed_by == mem.founder.id and stored.signed_at == mem.clock.now()
    assert entry.action == decide.ACTION_SIGNED and entry.payload["content_hash"] == p.content_sha256
    fields = decide.decision_fields(p, decide.ACTION_SIGNED, mem.founder.id, mem.clock.now())
    assert SIGNER.verify(fields, entry.payload["mac"])


def test_a_second_actor_on_a_stale_read_is_refused(mem, verifier):
    with mem.backend.tenant(mem.tenant.id) as tx:
        strong_pillar(seeder(mem, tx))
        [p] = propose_month(tx, mem.clock)
        seen = decide._open(tx, p.id, p.content_sha256)  # both people loaded it as proposed
        decide.decline(tx, mem.clock, p.id, p.content_sha256, **kw(mem, tx, verifier))
        with pytest.raises(DecisionRefused, match="already acted"):
            decide._transition(tx, seen, state=DecisionState.SIGNED, signed_by=mem.founder.id,
                               signed_at=mem.clock.now())
        stored = tx.get(DecisionProposal, p.id)
    assert stored.state is DecisionState.DECLINED and stored.declined_by == mem.founder.id
    assert stored.signed_by is None


def test_tampered_evidence_cannot_be_signed(mem, verifier):
    with mem.backend.tenant(mem.tenant.id) as tx:
        strong_pillar(seeder(mem, tx))
        [p] = propose_month(tx, mem.clock)
        tx.update(DecisionProposal, p.id, change={"shares": {}})
        with pytest.raises(DecisionRefused, match="changed"):
            decide.sign(tx, mem.clock, p.id, p.content_sha256, **kw(mem, tx, verifier))


# ---------------------------------------------------------------- forecasts as the referee


def test_forecast_credit_needs_enough_scored_forecasts(mem):
    with mem.backend.tenant(mem.tenant.id) as tx:
        s = seeder(mem, tx)
        s.pillar_set(SHARES)
        for n in range(12):
            s.piece(pillar="Craft", prompt_version="planner.rules/1" if n < 9 else "planner.rules/2")
        briefs = sorted(tx.list_all(Brief), key=lambda b: b.id)
        now = mem.clock.now()
        for i, b in enumerate(briefs):
            f = tx.insert(Forecast(id=new_id(mem.clock), created_at=now, tenant_id=mem.tenant.id, brief_id=b.id,
                                   seq=i + 1, metric="enquiries", day=MetricDay.D14, low=0, high=2, baseline_low=0,
                                   baseline_high=1, sealed_at=now, seal_hash="c" * 64))
            tx.insert(ForecastScore(id=new_id(mem.clock), created_at=now, tenant_id=mem.tenant.id, forecast_id=f.id,
                                    actual=1, beat_baseline=True, scored_at=now))
        out = {r["prompt_version"]: r for r in forecast_credit(tx)}
    assert out["planner.rules/1"]["verdict"] == "credited" and out["planner.rules/1"]["scored"] == 9
    assert out["planner.rules/2"]["verdict"] == NO_DECISION and out["planner.rules/2"]["small_sample"]
