"""The check stack (WP-07). See `stack.py` for the order and the rules.

- `stack`: `run_stack`, `write_stack`, `FinishedPiece`, `Context`, `Settings`, the repair loop
- `text`: code checks on the outgoing text (slop, sameness, length, PII and injection screen)
- `photo`: technical checks on client photos and the attestation FLAG
- `models`: text graders, the vision grader protocol, the cross-family second reader, the ledger
- `agreement`: the weekly grader agreement job
- `catalogues`: the design slop catalogue
"""
from thallo.checks.agreement import AgreementReport, GraderScore, weekly_agreement
from thallo.checks.models import DEFAULT_GRADERS, Ledger, TextGrader, VisionGrader, VisionReply, model_family
from thallo.checks.stack import (
    MAX_REPAIR_LOOPS,
    Context,
    FinishedPiece,
    Repairer,
    Settings,
    StackOutcome,
    StackStatus,
    run_stack,
    with_text,
    write_stack,
)

__all__ = [
    "DEFAULT_GRADERS", "MAX_REPAIR_LOOPS", "AgreementReport", "Context", "FinishedPiece", "GraderScore", "Ledger",
    "Repairer", "Settings", "StackOutcome", "StackStatus", "TextGrader", "VisionGrader", "VisionReply",
    "model_family", "run_stack", "weekly_agreement", "with_text", "write_stack",
]
