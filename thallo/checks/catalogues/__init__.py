"""Catalogues the check stack reads: the design slop list (markdown, maintained by people)."""
from __future__ import annotations

import re
from functools import cache
from pathlib import Path

DESIGN_SLOP_PATH = Path(__file__).with_name("design_slop.md")
_ENTRY = re.compile(r"^- ([a-z_]+): (.+)$")


@cache
def design_slop() -> dict[str, str]:
    """Entry id to description, in file order."""
    out: dict[str, str] = {}
    for line in DESIGN_SLOP_PATH.read_text(encoding="utf-8").splitlines():
        m = _ENTRY.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def design_slop_text() -> str:
    """The catalogue as a grader reads it: one `id: description` per line."""
    return "\n".join(f"{k}: {v}" for k, v in design_slop().items())


__all__ = ["DESIGN_SLOP_PATH", "design_slop", "design_slop_text"]
