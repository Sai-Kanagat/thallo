"""Copy production (WP-06): three candidates, the truth compile with repair loops, the slop
catalogue, selection with a stored reason, and the per-piece cost cap. See `produce.py`."""
from thallo.copy.persist import write_outcome
from thallo.copy.produce import (
    CANDIDATES,
    COST_CAP_USD,
    MAX_REPAIR_LOOPS,
    CandidateRun,
    CopyOutcome,
    CopyStatus,
    Grader,
    cost_cap_eur,
    produce_copy,
)
from thallo.copy.slop import SlopHit, SlopKind, find_slop

__all__ = [
    "CANDIDATES", "COST_CAP_USD", "MAX_REPAIR_LOOPS", "CandidateRun", "CopyOutcome", "CopyStatus", "Grader",
    "SlopHit", "SlopKind", "cost_cap_eur", "find_slop", "produce_copy", "write_outcome",
]
