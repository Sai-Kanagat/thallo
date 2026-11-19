"""Load a golden set file from `evals/golden/` into tenant-scoped cases (fictional demo data only)."""
from __future__ import annotations

import json
from pathlib import Path

from thallo.contracts.clock import Clock
from thallo.contracts.enums import Channel, Language
from thallo.contracts.ids import new_id
from thallo.learning.records import CaseKind, RegressionCase

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "evals" / "golden"


def load_golden(path: Path, *, tenant_id: str, clock: Clock) -> list[RegressionCase]:
    data = json.loads(path.read_text())
    return [RegressionCase(id=new_id(clock), tenant_id=tenant_id, created_at=clock.now(), kind=CaseKind(c["kind"]),
                           channel=Channel(c["channel"]) if c.get("channel") else None,
                           language=Language(c["language"]) if c.get("language") else None,
                           brief=c.get("brief", {}), draft=c.get("draft"), final=c["final"])
            for c in data["cases"]]


__all__ = ["GOLDEN_DIR", "load_golden"]
