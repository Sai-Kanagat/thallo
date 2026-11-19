"""The guard on module 14: the share of leads with no source, reported weekly.

A lead has a source when any of its person's touches names a variant or an ad (link, code or form)
or carries a self-report answer. Plain counting over rows; no estimate, no blending.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from thallo.contracts.models import Lead, Touch


@dataclass(frozen=True)
class SourceShare:
    leads: int
    unsourced: int
    by_signal: dict[str, int]  # leads with each signal: "link_or_code", "self_report"

    @property
    def unsourced_share(self) -> float | None:
        return None if self.leads == 0 else self.unsourced / self.leads


def source_share(leads: Iterable[Lead], touches: Iterable[Touch], *, since: datetime, until: datetime) -> SourceShare:
    by_person: dict[str, list[Touch]] = {}
    by_id: dict[str, Touch] = {}
    for t in touches:
        by_id[t.id] = t
        if t.person_id:
            by_person.setdefault(t.person_id, []).append(t)
    total = unsourced = coded = reported = 0
    for lead in leads:
        if not (since <= lead.created_at < until):
            continue
        total += 1
        mine = list(by_person.get(lead.person_id, []))
        for tid in (lead.first_touch_id, lead.last_touch_id):
            if tid and tid in by_id:
                mine.append(by_id[tid])
        has_code = any(t.variant_id or t.ad_id for t in mine)
        has_report = any(t.self_report for t in mine)
        coded += has_code
        reported += has_report
        unsourced += not (has_code or has_report)
    return SourceShare(total, unsourced, {"link_or_code": coded, "self_report": reported})


__all__ = ["SourceShare", "source_share"]
