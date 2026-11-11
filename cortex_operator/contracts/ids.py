"""Identifiers.

Every row id is a UUIDv7 rendered as a lowercase string: sortable by creation
time and safe to generate on any machine. Human-readable codes (for example
claim codes like C-0012) are a separate `code` field, never the id.
"""
from __future__ import annotations

import os
import re
from typing import Annotated, NewType

from pydantic import StringConstraints

from cortex_operator.contracts.clock import Clock

_UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_UUID = re.compile(_UUID_RE)

Id = Annotated[str, StringConstraints(pattern=_UUID_RE)]
TenantId = NewType("TenantId", str)

CLAIM_CODE_RE = r"^C-\d{4,}$"
ClaimCode = Annotated[str, StringConstraints(pattern=CLAIM_CODE_RE)]


def new_id(clock: Clock, rand: bytes | None = None) -> str:
    """UUIDv7 from the clock's time. Pass `rand` (10 bytes) for a deterministic id."""
    millis = int(clock.now().timestamp() * 1000)
    tail = rand if rand is not None else os.urandom(10)
    if len(tail) != 10:
        raise ValueError("rand must be exactly 10 bytes")
    rand_a = int.from_bytes(tail[:2], "big") & 0x0FFF
    rand_b = int.from_bytes(tail[2:], "big") & ((1 << 62) - 1)
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def is_id(value: str) -> bool:
    return bool(_UUID.match(value))
