"""Monthly decisions: keep, change, stop, or "no decision this month" (BUILD_PLAN 2.6).

For each dimension (pillar, channel, format) every value is compared with the rest of the brand:

- the rung is the highest one on the ladder where both sides together have at least
  RUNG_MIN_EVENTS outcomes under both views (first touch and last touch). Reach and likes never
  count; engaged is the lowest rung;
- the estimate is the shrunk rate per piece (`estimate.py`), and the engine reports P(value beats
  the rest) under first touch and under last touch, side by side;
- minimum evidence: a proposal needs P >= 0.80 under BOTH views (or <= 0.20 for worse) and at
  least 8 pieces on each side. The views disagreeing, or anything short of that, is no decision.

Proposals:
- pillar better: change, its share up; pillar worse: change, its share down. One pillar move per
  month, the one with the strongest evidence, by at most 10 points, never below
  max(10%, the pillar's own floor), and the other pillars absorb the difference so the total
  stays the same. Shares are of the regular slots; the planner keeps 20% of slots for new ideas.
- channel or format better: keep; worse: stop, listing the recent pieces of that kind.

Each proposal carries its evidence and a content hash. A named person signs or declines it; the
engine never applies it. Applying a signed pillar change is the growth model's job.

The thresholds marked "default" are starting values, not benchmarks.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from thallo.contracts.actors import Actor, ActorRef, HumanActor, SessionVerifier, require_human
from thallo.contracts.clock import Clock
from thallo.contracts.enums import HumanOnlyAction
from thallo.contracts.ids import is_id, new_id
from thallo.contracts.models import AppUser, Brief, Forecast, ForecastScore, Idea, Pillar
from thallo.gate.approvals import approver_problem
from thallo.gate.signing import GateSigner
from thallo.measure.estimate import SMALL_SAMPLE, estimate, prob_above_half, prob_beats
from thallo.measure.ladder import MATURE_DAYS, RUNGS, VIEWS, PieceRow, pieces
from thallo.measure.records import DecisionKind, DecisionProposal, DecisionState
from thallo.trust.audit import AuditLog

NO_DECISION = "no decision this month"
MIN_PROBABILITY = 0.80
MIN_PIECES = SMALL_SAMPLE  # 8 per side, from the spec
RUNG_MIN_EVENTS = 10  # default
LOOKBACK_DAYS = 180  # default
MAX_MOVE = Decimal("0.10")
MIN_SHARE = Decimal("0.10")
RESERVED_NEW = Decimal("0.20")
STEP = Decimal("0.0001")
DIMENSIONS: dict[str, Callable[[PieceRow], str | None]] = {
    "pillar": lambda p: p.pillar_id,
    "channel": lambda p: p.channel,
    "format": lambda p: p.format,
}


class DecisionRefused(ValueError):
    pass


def period_of(now: datetime) -> str:
    return f"{now.year:04d}-{now.month:02d}"


# ---------------------------------------------------------------- comparison


@dataclass(frozen=True)
class Comparison:
    dimension: str
    subject: str
    label: str
    rung: str | None
    verdict: str  # better | worse | none
    reason: str
    evidence: dict[str, Any]
    strength: float  # distance of the weaker view from 0.5, to rank pillar moves


def _side(rows: Sequence[PieceRow], rung: str, view: str) -> tuple[int, int]:
    known = [r.count(rung, view) for r in rows]
    vals = [v for v in known if v is not None]
    return sum(vals), len(vals)


def _rung(a: Sequence[PieceRow], b: Sequence[PieceRow]) -> str | None:
    for rung in RUNGS:
        if all(_side(a, rung, v)[0] + _side(b, rung, v)[0] >= RUNG_MIN_EVENTS for v in VIEWS):
            return rung
    return None


def _review(rows: Sequence[PieceRow]) -> dict[str, Any]:
    n = len(rows)
    ok = sum(r.first_pass_approved for r in rows)
    return {"pieces": n, "first_pass_approved": ok, "first_pass_rate": round(ok / n, 4) if n else None}


def compare(rows: Sequence[PieceRow], dimension: str, subject: str, label: str) -> Comparison:
    key = DIMENSIONS[dimension]
    a = [r for r in rows if key(r) == subject]
    b = [r for r in rows if key(r) not in (subject, None)]
    rung = _rung(a, b)
    ev: dict[str, Any] = {"dimension": dimension, "subject": label, "rung": rung,
                          "window": f"pieces live {MATURE_DAYS} to {LOOKBACK_DAYS} days",
                          "minimum": {"probability": MIN_PROBABILITY, "pieces_per_side": MIN_PIECES},
                          "review": {"this": _review(a), "rest": _review(b)}}
    if rung is None:
        return Comparison(dimension, subject, label, None, "none",
                          f"fewer than {RUNG_MIN_EVENTS} outcomes on every rung", ev, 0.0)
    probs: dict[str, float | None] = {}
    for view in VIEWS:
        sa, sb = _side(a, rung, view), _side(b, rung, view)
        brand = (sa[0] + sb[0]) / max(1, sa[1] + sb[1])
        probs[view] = prob_beats(sa, sb, brand)
        ev[view] = {"this": estimate(*sa, brand).as_dict(), "rest": estimate(*sb, brand).as_dict(),
                    "brand_rate": round(brand, 4), "p_this_beats_rest": probs[view]}
    pieces_a, pieces_b = _side(a, rung, VIEWS[0])[1], _side(b, rung, VIEWS[0])[1]
    ps = [p for p in probs.values() if p is not None]
    if min(pieces_a, pieces_b) < MIN_PIECES or len(ps) < len(VIEWS):
        return Comparison(dimension, subject, label, rung, "none",
                          f"fewer than {MIN_PIECES} pieces on a side", ev, 0.0)
    if all(p >= MIN_PROBABILITY for p in ps):
        return Comparison(dimension, subject, label, rung, "better", "both views agree", ev, min(ps) - 0.5)
    if all(p <= 1 - MIN_PROBABILITY for p in ps):
        return Comparison(dimension, subject, label, rung, "worse", "both views agree", ev, 0.5 - max(ps))
    agree = "the views disagree" if any(p >= MIN_PROBABILITY or p <= 1 - MIN_PROBABILITY for p in ps) \
        else f"probability below {MIN_PROBABILITY:.0%}"
    return Comparison(dimension, subject, label, rung, "none", agree, ev, 0.0)


# ---------------------------------------------------------------- capped share moves


def capped_shares(shares: dict[str, Decimal], floors: dict[str, Decimal], target: str,
                  up: bool) -> dict[str, Decimal] | None:
    """New shares after moving `target` by at most MAX_MOVE, floors kept, total kept.
    None when nothing can move."""
    floor = {k: max(MIN_SHARE, floors.get(k, MIN_SHARE)) for k in shares}
    others = [k for k in sorted(shares) if k != target]
    if not others:
        return None
    if up:
        room = {k: max(Decimal(0), shares[k] - floor[k]) for k in others}
        move = min(MAX_MOVE, sum(room.values(), Decimal(0)))
        if move <= 0:
            return None
        total_room = sum(room.values(), Decimal(0))
        take = {k: (move * room[k] / total_room).quantize(STEP, ROUND_DOWN) for k in others}
        roomiest = max(others, key=lambda k: (room[k] - take[k], k))
        take[roomiest] += min(move - sum(take.values(), Decimal(0)), room[roomiest] - take[roomiest])
        out = {k: shares[k] - take[k] for k in others}
        out[target] = shares[target] + sum(take.values(), Decimal(0))
    else:
        move = min(MAX_MOVE, shares[target] - floor[target]).quantize(STEP, ROUND_DOWN)
        if move <= 0:
            return None
        give = {k: (move / len(others)).quantize(STEP, ROUND_DOWN) for k in others}
        give[others[0]] += move - sum(give.values(), Decimal(0))
        out = {k: shares[k] + give[k] for k in others}
        out[target] = shares[target] - move
    return out


# ---------------------------------------------------------------- proposals


def content_hash(p: DecisionProposal) -> str:
    body = {"period": p.period, "dimension": p.dimension, "subject": p.subject, "kind": p.kind.value,
            "summary": p.summary, "evidence": p.evidence, "change": p.change}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _row(tx: Any, clock: Clock, period: str, dimension: str, subject: str, kind: DecisionKind, summary: str,
         evidence: dict, change: dict) -> DecisionProposal:
    state = DecisionState.NOTED if kind is DecisionKind.NO_DECISION else DecisionState.PROPOSED
    p = DecisionProposal(id=new_id(clock), tenant_id=tx.tenant_id, created_at=clock.now(), period=period,
                         dimension=dimension, subject=subject, kind=kind, state=state, summary=summary,
                         evidence=evidence, change=change, content_sha256="0" * 64)
    return p.model_copy(update={"content_sha256": content_hash(p)})


def _pillar_change(c: Comparison, pillars: list[Pillar], recent: int) -> tuple[str, dict] | None:
    shares = {p.id: p.target_share for p in pillars}
    new = capped_shares(shares, {p.id: p.min_share for p in pillars}, c.subject, c.verdict == "better")
    if new is None:
        return None
    moved = abs(new[c.subject] - shares[c.subject])
    names = {p.id: p.name for p in pillars}
    change = {"shares": {names[k]: {"pillar_id": k, "from": str(shares[k]), "to": str(new[k])} for k in sorted(new)},
              "reserved_for_new_ideas": str(RESERVED_NEW),
              "slots_moved_per_month": int((moved * recent).to_integral_value()),
              "slots_basis": f"{recent} pieces live in the last 30 days"}
    word = "up" if c.verdict == "better" else "down"
    return f"Move {c.label} {word} from {shares[c.subject]:.0%} to {new[c.subject]:.0%} of regular slots", change


def propose_month(tx: Any, clock: Clock) -> list[DecisionProposal]:
    """This month's proposals, written once. A second run in the same month returns what exists."""
    now = clock.now()
    period = period_of(now)
    existing = [p for p in tx.list_all(DecisionProposal) if p.period == period]
    if existing:
        return sorted(existing, key=lambda p: (p.dimension, p.subject))
    rows = [r for r in pieces(tx, now) if r.sent_at >= now - timedelta(days=LOOKBACK_DAYS)]
    recent = sum(1 for r in rows if r.sent_at >= now - timedelta(days=MATURE_DAYS + 30))
    pillars = list(tx.list_all(Pillar))
    names = {p.id: p.name for p in pillars}
    comparisons = []
    for dim, key in DIMENSIONS.items():
        for subject in sorted({k for r in rows if (k := key(r)) is not None}):
            if dim == "pillar" and subject not in names:
                continue
            comparisons.append(compare(rows, dim, subject, names.get(subject, subject) if dim == "pillar" else subject))
    out: list[DecisionProposal] = []
    moves = sorted((c for c in comparisons if c.dimension == "pillar" and c.verdict != "none"),
                   key=lambda c: (-c.strength, c.subject))
    for c in moves:
        model = next(q.growth_model_id for q in pillars if q.id == c.subject)
        siblings = [p for p in pillars if p.growth_model_id == model]
        made = _pillar_change(c, siblings, recent)
        if made:
            out.append(_row(tx, clock, period, "pillar", c.subject, DecisionKind.CHANGE, made[0], c.evidence, made[1]))
            break  # one pillar move a month keeps every share within the 10 point cap
    for c in comparisons:
        if c.dimension == "pillar" or c.verdict == "none":
            continue
        key = DIMENSIONS[c.dimension]
        mine = [r.variant_id for r in rows if key(r) == c.subject]
        if c.verdict == "better":
            out.append(_row(tx, clock, period, c.dimension, c.subject, DecisionKind.KEEP,
                            f"Keep {c.dimension} {c.label}: it beats the rest on {c.rung}", c.evidence, {}))
        else:
            out.append(_row(tx, clock, period, c.dimension, c.subject, DecisionKind.STOP,
                            f"Stop {c.dimension} {c.label}: it trails the rest on {c.rung}", c.evidence,
                            {"pieces_that_would_be_dropped": mine[-20:],
                             "note": "future slots of this kind; the listed pieces are the recent ones"}))
    if not out:
        reasons = [{"dimension": c.dimension, "subject": c.label, "rung": c.rung, "reason": c.reason,
                    **{v: c.evidence[v]["p_this_beats_rest"] for v in VIEWS if v in c.evidence}}
                   for c in comparisons]
        out.append(_row(tx, clock, period, "*", "*", DecisionKind.NO_DECISION, NO_DECISION,
                        {"pieces": len(rows), "checked": reasons,
                         "minimum": {"probability": MIN_PROBABILITY, "pieces_per_side": MIN_PIECES}}, {}))
    for p in out:
        tx.insert(p)
    return sorted(out, key=lambda p: (p.dimension, p.subject))


# ---------------------------------------------------------------- sign-off


ACTION_SIGNED = "decision.signed"
ACTION_DECLINED = "decision.declined"
AUDIT_SUBJECT = "decision_proposal"


def decision_fields(p: DecisionProposal, action: str, person_id: str, at: datetime) -> dict[str, Any]:
    """What the decision MAC covers: which proposal, of what content, who, when, and the verb."""
    return {"action": action, "proposal_id": p.id, "tenant_id": p.tenant_id, "period": p.period,
            "dimension": p.dimension, "subject": p.subject, "kind": p.kind.value,
            "content_hash": p.content_sha256, "person_id": person_id, "at": at.isoformat()}


def _person(actor: Actor, verifier: SessionVerifier, approver: AppUser | None, tenant_id: str) -> HumanActor:
    person = require_human(actor, HumanOnlyAction.APPROVE, verifier=verifier, tenant_id=tenant_id)
    if not is_id(person.id):
        raise DecisionRefused("approver id is not a valid id")
    if problem := approver_problem(approver, person.id, tenant_id):
        raise DecisionRefused(problem)
    return person


def _open(tx: Any, proposal_id: str, content_sha256: str) -> DecisionProposal:
    p = tx.get(DecisionProposal, proposal_id)
    if p is None:
        raise DecisionRefused("no such proposal")
    if p.state is not DecisionState.PROPOSED:
        raise DecisionRefused(f"proposal is {p.state.value}, not proposed")
    if content_hash(p) != p.content_sha256 or content_sha256 != p.content_sha256:
        raise DecisionRefused("the proposal changed since it was shown")
    return p


def _transition(tx: Any, p: DecisionProposal, **fields: Any) -> None:
    """Move a proposed row in one conditional statement: only while it is still proposed with the
    hash that was shown. Two people acting at once: one wins, the other is refused."""
    repo = getattr(tx, "repo", tx)
    if hasattr(repo, "execute"):  # Postgres: one UPDATE ... WHERE state and hash still match
        from psycopg import sql

        sets = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in fields)
        cur = repo.execute(sql.SQL("UPDATE decision_proposal SET {} WHERE id = %s AND state = 'proposed' "
                                   "AND content_sha256 = %s").format(sets),
                           [v.value if isinstance(v, DecisionState) else v for v in fields.values()]
                           + [p.id, p.content_sha256])
        changed = cur.rowcount == 1
    else:  # memory: one in-process check and set, no other writer between the two
        cur_row = tx.get(DecisionProposal, p.id)
        changed = (cur_row is not None and cur_row.state is DecisionState.PROPOSED
                   and cur_row.content_sha256 == p.content_sha256 and tx.update(DecisionProposal, p.id, **fields))
    if not changed:
        raise DecisionRefused("the proposal was already acted on or changed")


def _act(tx: Any, clock: Clock, proposal_id: str, content_sha256: str, *, actor: Actor, verifier: SessionVerifier,
         approver: AppUser | None, audit: AuditLog, signer: GateSigner, signing: bool) -> DecisionProposal:
    if not isinstance(signer, GateSigner):
        raise DecisionRefused("no gate signer configured")
    p = _open(tx, proposal_id, content_sha256)
    person = _person(actor, verifier, approver, p.tenant_id)
    now = clock.now()
    state, action = (DecisionState.SIGNED, ACTION_SIGNED) if signing else (DecisionState.DECLINED, ACTION_DECLINED)
    who = {"signed_by": person.id, "signed_at": now} if signing else {"declined_by": person.id, "declined_at": now}
    _transition(tx, p, state=state, **who)
    audit.append(p.tenant_id, ActorRef.of(person), action, subject_kind=AUDIT_SUBJECT, subject_id=p.id,
                 payload={"content_hash": p.content_sha256, "period": p.period, "kind": p.kind.value,
                          "at": now.isoformat(), "mac": signer.sign(decision_fields(p, action, person.id, now))})
    return p.model_copy(update={"state": state, **who})


def sign(tx: Any, clock: Clock, proposal_id: str, content_sha256: str, *, actor: Actor, verifier: SessionVerifier,
         approver: AppUser | None, audit: AuditLog, signer: GateSigner) -> DecisionProposal:
    """A named person signs exactly the proposal they saw. Recording only: nothing is applied."""
    return _act(tx, clock, proposal_id, content_sha256, actor=actor, verifier=verifier, approver=approver,
                audit=audit, signer=signer, signing=True)


def decline(tx: Any, clock: Clock, proposal_id: str, content_sha256: str, *, actor: Actor,
            verifier: SessionVerifier, approver: AppUser | None, audit: AuditLog,
            signer: GateSigner) -> DecisionProposal:
    return _act(tx, clock, proposal_id, content_sha256, actor=actor, verifier=verifier, approver=approver,
                audit=audit, signer=signer, signing=False)


# ---------------------------------------------------------------- forecasts as the referee


def forecast_credit(tx: Any) -> list[dict[str, Any]]:
    """Per prompt version: credit only if its sealed forecasts beat the naive baseline, with
    P(beat rate > 50%) >= 0.80 over at least 8 scored forecasts. Otherwise no decision."""
    briefs = {b.id: b.idea_id for b in tx.list_all(Brief)}
    ideas = {i.id: i.prompt_version for i in tx.list_all(Idea)}
    version = {f.id: ideas.get(briefs.get(f.brief_id, ""), None) for f in tx.list_all(Forecast)}
    tally: dict[str, list[bool]] = {}
    for s in tx.list_all(ForecastScore):
        if s.beat_baseline is not None:
            tally.setdefault(version.get(s.forecast_id) or "unknown", []).append(s.beat_baseline)
    out = []
    for v, beats in sorted(tally.items()):
        n, k = len(beats), sum(beats)
        p = prob_above_half(k, n)
        credited = n >= MIN_PIECES and p is not None and p >= MIN_PROBABILITY
        out.append({"prompt_version": v, "scored": n, "beat_baseline": k, "p_beats_baseline": p,
                    "small_sample": n < MIN_PIECES, "verdict": "credited" if credited else NO_DECISION})
    return out


__all__ = ["NO_DECISION", "Comparison", "DecisionRefused", "capped_shares", "compare", "content_hash", "decline",
           "forecast_credit", "period_of", "propose_month", "sign"]
