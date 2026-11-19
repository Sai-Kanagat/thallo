"""Autonomy state per tenant and task type, over one tenant transaction.

`tx` is the API backend's tenant transaction (memory or Postgres): get, list_all, insert, update,
and `audit` (the hash-chained log). Nothing here commits: a demotion, its incident, its event and
its audit entry land in the caller's transaction together, so they happen the same moment as the
thing that caused them, or not at all.

Rules held here, whatever the caller does:
- The system never raises a level. `raise_level` needs a verified live human session
  (RAISE_TRUST_LEVEL) AND measured evidence that meets the bound; one step at a time.
- Demotion is automatic and immediate (BUILD_PLAN 2.8): a hard-rule hit that reached approval or
  any escaped defect goes back to L1 with an incident; two defects in the last 20 items, a missed
  canary, a model or prompt version change, or any incident goes down one level; a region policy
  change sends the named task types to L1. Two breaker trips in a rolling 7 days pause the task type.
- Every level change resets the window: evidence is counted again from the new level.

Postgres: the autonomy_state row is locked (SELECT ... FOR UPDATE) before counting, so two
reviews of the same task type cannot both read the old counters.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from thallo.contracts.actors import Actor, SessionVerifier, SystemActor, require_human
from thallo.contracts.clock import Clock
from thallo.contracts.enums import AutonomyLevel, HumanOnlyAction, IncidentSeverity
from thallo.contracts.events import AutonomyPayload, Event, EventName, Payload
from thallo.contracts.ids import new_id
from thallo.contracts.models import AutonomyState, EventRecord, Incident
from thallo.trust import ladder
from thallo.trust.records import COUNTS_AS_DEFECT, CanaryState, ItemOutcome, TrustCanary, TrustItem, TrustTask

SYSTEM = SystemActor(id="trust")
MAX_TASK_TYPE = 80


class TrustError(ValueError):
    pass


class Refused(TrustError):
    def __init__(self, reasons: tuple[str, ...] | list[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__(" ".join(self.reasons))


@dataclass(frozen=True)
class LevelChange:
    task_type: str
    from_level: AutonomyLevel
    to_level: AutonomyLevel
    reason: str


@dataclass(frozen=True)
class ItemResult:
    item: TrustItem
    changes: tuple[LevelChange, ...]
    incident: Incident | None


# ---------------------------------------------------------------- plumbing

def _check_task_type(task_type: str) -> str:
    t = (task_type or "").strip()
    if not t or len(t) > MAX_TASK_TYPE:
        raise TrustError(f"a task type is 1 to {MAX_TASK_TYPE} characters")
    return t


def _repo(tx: Any) -> Any:
    return getattr(tx, "repo", None)


def _lock(tx: Any, state: AutonomyState) -> None:
    repo = _repo(tx)
    if repo is not None:
        repo.execute("SELECT 1 FROM autonomy_state WHERE id = %s FOR UPDATE", (state.id,))


def emit(tx: Any, clock: Clock, actor: Actor, name: EventName, payload: Payload) -> None:
    now = clock.now()
    event = Event.build(id=new_id(clock), tenant_id=tx.tenant_id, occurred_at=now, actor=actor,
                        payload=payload, name=name)
    tx.insert(EventRecord(id=event.id, created_at=now, tenant_id=tx.tenant_id, name=event.name.value,
                          version=event.version, actor_kind=event.actor.kind, actor_id=event.actor.id,
                          payload=event.payload, occurred_at=now))


def audit(tx: Any, actor: Actor, action: str, subject_kind: str, subject_id: str, payload: dict) -> None:
    log = getattr(tx, "audit", None)
    if log is None:
        raise TrustError("this transaction has no audit log; trust changes are always audited")
    log.append(tx.tenant_id, actor, action, subject_kind=subject_kind, subject_id=subject_id, payload=payload)


def state(tx: Any, clock: Clock, task_type: str) -> tuple[AutonomyState, TrustTask]:
    """The task type's state, created at L0 on first sight."""
    task_type = _check_task_type(task_type)
    now = clock.now()
    rows = tx.list_all(AutonomyState, task_type=task_type)
    st = rows[0] if rows else tx.insert(AutonomyState(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id,
                                                     task_type=task_type, changed_at=now))
    metas = tx.list_all(TrustTask, task_type=task_type)
    meta = metas[0] if metas else tx.insert(TrustTask(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id,
                                                      task_type=task_type, updated_at=now))
    return st, meta


def _reload(tx: Any, st: AutonomyState, meta: TrustTask) -> tuple[AutonomyState, TrustTask]:
    return tx.get(AutonomyState, st.id), tx.get(TrustTask, meta.id)


def recent_items(tx: Any, task_type: str, since: datetime, limit: int) -> list[TrustItem]:
    """The newest `limit` items of this task type since `since`, newest first."""
    repo = _repo(tx)
    if repo is not None:
        return repo.query(TrustItem, "SELECT * FROM trust_item WHERE task_type = %s AND occurred_at > %s "
                                     "ORDER BY occurred_at DESC, id DESC LIMIT %s", (task_type, since, limit))
    rows = [r for r in tx.list_all(TrustItem, task_type=task_type) if r.occurred_at > since]
    rows.sort(key=lambda r: (r.occurred_at, r.id), reverse=True)
    return rows[:limit]


def canaries_since(tx: Any, task_type: str, since: datetime) -> list[TrustCanary]:
    repo = _repo(tx)
    if repo is not None:
        return repo.query(TrustCanary, "SELECT * FROM trust_canary WHERE task_type = %s AND planted_at > %s "
                                       "ORDER BY planted_at, id", (task_type, since))
    return [c for c in tx.list_all(TrustCanary, task_type=task_type) if c.planted_at > since]


# ---------------------------------------------------------------- level changes

def change_level(tx: Any, clock: Clock, st: AutonomyState, to: AutonomyLevel, reason: str,
                 actor: Actor = SYSTEM) -> LevelChange | None:
    """Set the level and restart the window. No change, no write."""
    if to == st.level:
        return None
    now = clock.now()
    tx.update(AutonomyState, st.id, level=to, window_items=0, defects=0, escaped=0, canaries_shown=0,
              canaries_caught=0, changed_at=now)
    emit(tx, clock, actor, EventName.AUTONOMY_CHANGED,
         AutonomyPayload(task_type=st.task_type, from_level=st.level, to_level=to, reason=reason))
    audit(tx, actor, "trust.level_changed", "autonomy_state", st.id,
          {"task_type": st.task_type, "from": int(st.level), "to": int(to), "reason": reason})
    return LevelChange(st.task_type, st.level, to, reason)


def _demote(tx: Any, clock: Clock, st: AutonomyState, to: AutonomyLevel, reason: str) -> tuple[LevelChange, ...]:
    if to >= st.level:
        return ()
    ch = change_level(tx, clock, st, to, reason)
    return (ch,) if ch else ()


def open_incident(tx: Any, clock: Clock, *, severity: IncidentSeverity, title: str, task_type: str | None = None,
                  subject_kind: str | None = None, subject_id: str | None = None, note: str | None = None,
                  actor: Actor = SYSTEM) -> tuple[Incident, tuple[LevelChange, ...]]:
    """Any incident drops autonomy: high and hard-rule back to L1, low down one level."""
    now = clock.now()
    inc = tx.insert(Incident(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, severity=severity,
                             title=title[:300], subject_kind=subject_kind, subject_id=subject_id, note=note,
                             opened_at=now))
    audit(tx, actor, "trust.incident_opened", "incident", inc.id,
          {"severity": severity.value, "task_type": task_type, "title": title[:300]})
    changes: tuple[LevelChange, ...] = ()
    if task_type is not None:
        st, _ = state(tx, clock, task_type)
        _lock(tx, st)
        st = tx.get(AutonomyState, st.id)
        to = ladder.down_one(st.level) if severity is IncidentSeverity.LOW else ladder.to_l1(st.level)
        changes = _demote(tx, clock, st, to, f"incident: {title[:120]}")
    return inc, changes


def note_version(tx: Any, clock: Clock, task_type: str, version_key: str) -> tuple[LevelChange, ...]:
    """The model id and prompt version that make this task type's items. A change drops one level
    and must be re-earned with 30 clean items before any raise. The first key sets the baseline."""
    if not version_key or len(version_key) > 200:
        raise TrustError("a version key is 1 to 200 characters")
    st, meta = state(tx, clock, task_type)
    _lock(tx, st)
    st, meta = _reload(tx, st, meta)
    if meta.version_key == version_key:
        return ()
    now = clock.now()
    first = meta.version_key is None
    tx.update(TrustTask, meta.id, version_key=version_key, updated_at=now,
              **({} if first else {"version_changed_at": now, "clean_since_version": 0}))
    if first:
        return ()
    audit(tx, SYSTEM, "trust.version_changed", "autonomy_state", st.id,
          {"task_type": st.task_type, "from": meta.version_key, "to": version_key})
    return _demote(tx, clock, st, ladder.down_one(st.level), "model or prompt version changed")


def region_policy_changed(tx: Any, clock: Clock, task_types: list[str], policy_ref: str) -> tuple[LevelChange, ...]:
    out: list[LevelChange] = []
    for t in task_types:
        st, _ = state(tx, clock, t)
        _lock(tx, st)
        st = tx.get(AutonomyState, st.id)
        out += _demote(tx, clock, st, ladder.to_l1(st.level), f"region policy changed: {policy_ref[:80]}")
    return tuple(out)


def pause(tx: Any, clock: Clock, task_type: str, reason: str, actor: Actor = SYSTEM) -> bool:
    st, meta = state(tx, clock, task_type)
    if st.paused:
        return False
    tx.update(AutonomyState, st.id, paused=True)
    tx.update(TrustTask, meta.id, pause_reason=reason[:300], updated_at=clock.now())
    audit(tx, actor, "trust.paused", "autonomy_state", st.id, {"task_type": st.task_type, "reason": reason[:300]})
    return True


def unpause(tx: Any, clock: Clock, task_type: str, *, actor: Actor, verifier: SessionVerifier) -> None:
    human = require_human(actor, HumanOnlyAction.RAISE_TRUST_LEVEL, verifier=verifier, tenant_id=tx.tenant_id)
    st, meta = state(tx, clock, task_type)
    tx.update(AutonomyState, st.id, paused=False)
    tx.update(TrustTask, meta.id, pause_reason=None, updated_at=clock.now())
    audit(tx, human, "trust.unpaused", "autonomy_state", st.id, {"task_type": st.task_type})


# ---------------------------------------------------------------- items

def record_item(tx: Any, clock: Clock, *, task_type: str, subject_kind: str, subject_id: str,
                outcome: ItemOutcome, version_key: str | None = None, note: str | None = None) -> ItemResult:
    """One reviewed item (never a canary: those go through `canary.resolve`). An item already
    recorded can only be raised to escaped or hard-rule later (a defect found after approval)."""
    changes: list[LevelChange] = []
    if version_key is not None:
        changes += note_version(tx, clock, task_type, version_key)
    st, meta = state(tx, clock, task_type)
    _lock(tx, st)
    st, meta = _reload(tx, st, meta)
    now = clock.now()
    planted = tx.list_all(TrustCanary, variant_id=subject_id) if subject_kind == "variant" else []
    if planted:
        raise TrustError("this item is a canary; resolve it with canary.resolve")
    existing = tx.list_all(TrustItem, task_type=st.task_type, subject_id=subject_id)
    if existing:
        prior = existing[0]
        if outcome not in (ItemOutcome.ESCAPED, ItemOutcome.HARD_RULE) or prior.outcome == outcome:
            raise TrustError("this item is already recorded")
        tx.update(TrustItem, prior.id, outcome=outcome, note=note)
        item = tx.get(TrustItem, prior.id)
        in_window = prior.occurred_at > st.changed_at
        was_defect = prior.outcome in COUNTS_AS_DEFECT
        fields: dict[str, Any] = {}
        if in_window:
            fields = {"defects": st.defects + (0 if was_defect else 1), "escaped": st.escaped + 1}
    else:
        item = tx.insert(TrustItem(id=new_id(clock), created_at=now, tenant_id=tx.tenant_id, task_type=st.task_type,
                                   subject_kind=subject_kind, subject_id=subject_id, outcome=outcome,
                                   version_key=version_key or meta.version_key, note=note, occurred_at=now))
        fields = {"window_items": st.window_items + 1,
                  "defects": st.defects + (1 if outcome in COUNTS_AS_DEFECT else 0),
                  "escaped": st.escaped + (1 if outcome in (ItemOutcome.ESCAPED, ItemOutcome.HARD_RULE) else 0)}
    if fields:
        tx.update(AutonomyState, st.id, **fields)
    clean = outcome in (ItemOutcome.CLEAN, ItemOutcome.TYPO)
    meta_fields: dict[str, Any] = {"updated_at": now,
                                   "clean_since_version": meta.clean_since_version + 1 if clean else 0}
    calibrated = fields.get("window_items", 0) >= ladder.CALIBRATION_BATCH
    if st.level == ladder.L0 and not meta.calibration_done and calibrated:
        meta_fields["calibration_done"] = True
    tx.update(TrustTask, meta.id, **meta_fields)
    audit(tx, SYSTEM, "trust.item_recorded", subject_kind[:40], subject_id,
          {"task_type": st.task_type, "outcome": outcome.value})
    st = tx.get(AutonomyState, st.id)

    incident = None
    if outcome in (ItemOutcome.ESCAPED, ItemOutcome.HARD_RULE):
        what = "a hard-rule hit reached approval" if outcome is ItemOutcome.HARD_RULE else "a defect escaped review"
        sev = IncidentSeverity.HARD_RULE if outcome is ItemOutcome.HARD_RULE else IncidentSeverity.HIGH
        incident, ch = open_incident(tx, clock, severity=sev, title=f"{st.task_type}: {what}", task_type=st.task_type,
                                     subject_kind=subject_kind, subject_id=subject_id, note=note)
        changes += ch
    elif outcome is ItemOutcome.DEFECT:
        recent = recent_items(tx, st.task_type, st.changed_at, ladder.RECENT_DEFECT_WINDOW)
        if sum(1 for r in recent if r.outcome in COUNTS_AS_DEFECT) >= ladder.RECENT_DEFECT_LIMIT:
            changes += _demote(tx, clock, st, ladder.down_one(st.level),
                               f"{ladder.RECENT_DEFECT_LIMIT} defects in the last {ladder.RECENT_DEFECT_WINDOW} items")
    return ItemResult(item, tuple(changes), incident)


# ---------------------------------------------------------------- evidence and raising

def evidence(tx: Any, clock: Clock, task_type: str) -> ladder.Evidence:
    st, meta = state(tx, clock, task_type)
    target = ladder.RULES.get(AutonomyLevel(min(st.level + 1, 4)))
    window = target.items if target else ladder.RULES[ladder.L4].items
    items = recent_items(tx, st.task_type, st.changed_at, window)
    canaries = [c for c in canaries_since(tx, st.task_type, st.changed_at) if c.state is not CanaryState.PLANTED]
    return ladder.Evidence(
        level=st.level, items=len(items),
        defects=sum(1 for r in items if r.outcome in COUNTS_AS_DEFECT),
        escaped=sum(1 for r in items if r.outcome is ItemOutcome.ESCAPED),
        hard_rule_hits=sum(1 for r in items if r.outcome is ItemOutcome.HARD_RULE),
        canaries_shown=len(canaries), canaries_caught=sum(1 for c in canaries if c.state is CanaryState.CAUGHT),
        calibration_done=meta.calibration_done, voice_rules_signed=meta.voice_rules_signed_by is not None,
        template_signed=meta.template_signed_by is not None,
        version_reearn_needed=meta.version_changed_at is not None, clean_since_version=meta.clean_since_version,
        paused=st.paused)


def raise_level(tx: Any, clock: Clock, task_type: str, *, actor: Actor, verifier: SessionVerifier) -> LevelChange:
    """A named human raises one level, only on evidence that meets the bound."""
    human = require_human(actor, HumanOnlyAction.RAISE_TRUST_LEVEL, verifier=verifier, tenant_id=tx.tenant_id)
    st, meta = state(tx, clock, task_type)
    _lock(tx, st)
    elig = ladder.eligibility(evidence(tx, clock, task_type))
    if not elig.ok or elig.target is None:
        raise Refused(elig.reasons)
    st = tx.get(AutonomyState, st.id)
    reason = "raised by a person on measured evidence"
    if elig.bound_pct is not None:
        reason = f"raised by a person; defect bound {elig.bound_pct}% at 95%"
    ch = change_level(tx, clock, st, elig.target, reason, actor=human)
    if meta.version_changed_at is not None:
        tx.update(TrustTask, meta.id, version_changed_at=None, updated_at=clock.now())
    if ch is None:
        raise TrustError("the level did not change")
    return ch


def sign_voice_rules(tx: Any, clock: Clock, task_type: str, *, actor: Actor, verifier: SessionVerifier) -> None:
    human = require_human(actor, HumanOnlyAction.SIGN_TEMPLATE, verifier=verifier, tenant_id=tx.tenant_id)
    _, meta = state(tx, clock, task_type)
    tx.update(TrustTask, meta.id, voice_rules_signed_by=human.id, updated_at=clock.now())
    audit(tx, human, "trust.voice_rules_signed", "trust_task", meta.id, {"task_type": meta.task_type})


def sign_template(tx: Any, clock: Clock, task_type: str, template_sha256: str, *, actor: Actor,
                  verifier: SessionVerifier) -> None:
    human = require_human(actor, HumanOnlyAction.SIGN_TEMPLATE, verifier=verifier, tenant_id=tx.tenant_id)
    if len(template_sha256) != 64 or any(c not in "0123456789abcdef" for c in template_sha256):
        raise TrustError("a template is signed by its sha256 (64 lowercase hex)")
    _, meta = state(tx, clock, task_type)
    tx.update(TrustTask, meta.id, template_sha256=template_sha256, template_signed_by=human.id,
              updated_at=clock.now())
    audit(tx, human, "trust.template_signed", "trust_task", meta.id,
          {"task_type": meta.task_type, "template_sha256": template_sha256})


__all__ = [
    "SYSTEM", "ItemResult", "LevelChange", "Refused", "TrustError", "audit", "canaries_since", "change_level",
    "emit", "evidence", "note_version", "open_incident", "pause", "raise_level", "recent_items", "record_item",
    "region_policy_changed", "sign_template", "sign_voice_rules", "state", "unpause",
]
