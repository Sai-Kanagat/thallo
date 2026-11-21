"""Shared scenarios, run on both backends. Invented numbers, for tests only."""
from __future__ import annotations

from decimal import Decimal

from tests.wp18_measure.world import LeadSpec, Seeder

SHARES = {"Craft": "0.45", "Offers": "0.35", "Team": "0.20"}


def strong_pillar(s: Seeder, pieces_per_pillar: int = 10) -> None:
    """Craft pieces each bring two booked leads; Offers one booking in total; Team none."""
    s.pillar_set(SHARES)
    for i in range(pieces_per_pillar):
        v = s.piece(pillar="Craft", days_ago=20 + i)
        s.lead(v.id, LeadSpec(booked=True))
        s.lead(v.id, LeadSpec(booked=True, won=Decimal("400") if i == 0 else None))
        o = s.piece(pillar="Offers", days_ago=20 + i)
        if i == 0:
            s.lead(o.id, LeadSpec(booked=True))
        s.piece(pillar="Team", days_ago=20 + i)
