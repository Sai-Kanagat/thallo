"""Fit, heat and route: plain code, no model. Two numbers, never blended.

Fit: the play's base plus every rule whose conditions all hold, clamped to 0..100. Some rule values
are not answers but facts derived from per-tenant lists (service area, stock, supported use cases,
free email domains). With an empty list the derived fact is "unknown" and its rule never fires.

Heat: a decayed sum of the lead's actions, half-life 14 days (k = ln 2 / 14 = 0.0495 per day).
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from thallo.contracts.enums import LeadRoute
from thallo.leads.play import LeadPlay

HALF_LIFE_DAYS = 14.0
DECAY_PER_DAY = math.log(2) / HALF_LIFE_DAYS  # 0.0495
UNKNOWN = "unknown"
NOT_MATCHED = "not_matched"

# Common consumer mail domains. A tenant can extend the list; this is a default, not a judgement.
FREE_EMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
                                "outlook.com", "live.com", "libero.it", "icloud.com", "me.com", "aol.com",
                                "proton.me", "protonmail.com", "gmx.com", "virgilio.it", "tiscali.it"})


@dataclass(frozen=True)
class TenantLists:
    """Per-tenant data the derived facts need. Supplied by the business; empty means unknown."""

    service_area: frozenset[str] = frozenset()
    stock: frozenset[str] = frozenset()
    supported_use_cases: frozenset[str] = frozenset()
    free_email_domains: frozenset[str] = field(default=FREE_EMAIL_DOMAINS)


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def derived_facts(answers: dict[str, Any], email: str | None, lists: TenantLists) -> dict[str, str]:
    facts: dict[str, str] = {}
    if lists.service_area and answers.get("town"):
        facts["town"] = "in_service_area" if _norm(answers["town"]) in {_norm(t) for t in lists.service_area} \
            else NOT_MATCHED
    elif "town" in answers:
        facts["town"] = UNKNOWN
    if lists.stock and answers.get("model"):
        facts["model"] = "in_stock" if _norm(answers["model"]) in {_norm(m) for m in lists.stock} else NOT_MATCHED
    elif "model" in answers:
        facts["model"] = UNKNOWN
    if lists.supported_use_cases and answers.get("use_case"):
        text = _norm(answers["use_case"])
        hit = any(_norm(u) and _norm(u) in text for u in lists.supported_use_cases)
        facts["use_case"] = "matches_supported_list" if hit else NOT_MATCHED
    elif "use_case" in answers:
        facts["use_case"] = UNKNOWN
    if email and "@" in email:
        domain = email.rsplit("@", 1)[1].strip().casefold()
        facts["email_domain"] = "free" if domain in lists.free_email_domains else "company"
    return facts


@dataclass(frozen=True)
class FitResult:
    fit: int
    lines: tuple[tuple[str, int], ...]  # readable table: (rule, points)


def fit(play: LeadPlay, answers: dict[str, Any], *, email: str | None = None,
        lists: TenantLists = TenantLists()) -> FitResult:
    facts = {k: str(v) for k, v in answers.items() if isinstance(v, str | int | float | bool)}
    facts.update(derived_facts(answers, email, lists))
    total = play.fit_base
    lines: list[tuple[str, int]] = [("base", play.fit_base)]
    for rule in play.fit_rules:
        if all(facts.get(k) == v for k, v in rule.when.items()):
            total += rule.add
            lines.append((" and ".join(f"{k} is {v}" for k, v in rule.when.items()), rule.add))
    return FitResult(max(0, min(100, total)), tuple(lines))


@dataclass(frozen=True)
class Action:
    kind: str
    occurred_at: datetime


def heat(play: LeadPlay, actions: Iterable[Action], now: datetime) -> float:
    total = 0.0
    for a in actions:
        weight = play.heat_actions.get(a.kind)
        if weight is None:
            continue
        days = max(0.0, (now - a.occurred_at).total_seconds() / 86400)
        total += weight * math.exp(-DECAY_PER_DAY * days)
    return round(total, 4)


def route(play: LeadPlay, fit_value: int, heat_value: float) -> LeadRoute:
    hot, warm = play.routes["hot"], play.routes["warm"]
    if fit_value >= (hot.fit_min or 0) and heat_value >= (hot.heat_min or 0):
        return LeadRoute.HOT
    if fit_value >= (warm.fit_min or 0) and heat_value >= (warm.heat_min or 0):
        return LeadRoute.WARM
    return LeadRoute.LOW_FIT


__all__ = ["DECAY_PER_DAY", "FREE_EMAIL_DOMAINS", "HALF_LIFE_DAYS", "Action", "FitResult", "TenantLists",
           "derived_facts", "fit", "heat", "route"]
