"""The planner's own tables (migration 0033). Core tables (`idea`, `brief`, `forecast`,
`forecast_score`, `plan_slot`) are used as they are; these rows hold what core has no column for.

- `planner_week`: one per tenant and week. The plan's two reasoning lines, what it chose not to
  do, and the stage check against the play.
- `planner_slot`: one per planned `plan_slot`. The play, the target metric and day, the format,
  the idea and brief behind the slot, and the sealed forecast (or why there is none).
- `planner_mirror`: one per mirror run. What went out to which calendar connection.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import AwareDatetime, Field

from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow


class PlannerWeek(TenantRow):
    __table__ = "planner_week"
    __unique__ = (("tenant_id", "week_start"),)
    week_start: date
    play_code: str
    growth_model_version: int
    reasoning: list[str] = Field(min_length=2, max_length=2)
    not_doing: list[str] = Field(default_factory=list)
    stage_check: dict[str, Any] = Field(default_factory=dict)
    planned_slots: int = Field(ge=0)
    experiment_slots: int = Field(ge=0)


class PlannerSlot(TenantRow):
    __table__ = "planner_slot"
    __unique__ = (("tenant_id", "slot_id"),)
    week_id: Id
    slot_id: Id
    idea_id: Id
    brief_id: Id
    forecast_id: Id | None = None
    no_forecast_reason: str | None = None
    play_code: str
    target_metric: str
    metric_day: int
    format: str
    reason: str


class PlannerMirror(TenantRow):
    __table__ = "planner_mirror"
    week_start: date
    provider: str
    mode: str
    status: str  # ok | failed
    mirrored: int = Field(ge=0)
    detail: str | None = None
    mirrored_at: AwareDatetime


__all__ = ["PlannerMirror", "PlannerSlot", "PlannerWeek"]
