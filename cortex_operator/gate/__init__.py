"""The gate. Every outbound action passes here. Nothing else may call a publisher.

`legacy` keeps the original engine's Gate and content_hash working until WP-08 ports the
publisher. The bundle gate (WP-04) lives in `canonical`, `approvals` and `decision`.
"""
from cortex_operator.gate.legacy import Gate, content_hash

__all__ = ["Gate", "content_hash"]
