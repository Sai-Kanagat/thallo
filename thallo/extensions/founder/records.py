"""The founder desk's table (migration 0040)."""
from __future__ import annotations

from pydantic import Field

from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow


class FounderPost(TenantRow):
    """A personal-profile variant and the founder's own answers it was written from."""

    __table__ = "founder_post"
    founder_id: Id
    variant_id: Id
    answer_ids: list[Id] = Field(min_length=1)


__all__ = ["FounderPost"]
