"""The one table this package adds (migration 0024). Contracts stay with the lead; this row model
lives here and is not part of `thallo.contracts`."""
from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow, fk


class CodeKind(StrEnum):
    WALK_IN = "walk_in"  # said or shown at the counter
    CALL = "call"  # read out on the phone
    OFFER = "offer"  # printed on a flyer or in a post, used anywhere
    QR = "qr"  # a printed QR code; always backed by its own tracked link


class TrackingCode(TenantRow):
    __table__ = "tracking_code"
    __unique__ = (("tenant_id", "code"),)
    code: str = Field(pattern=r"^[A-Z0-9]{4,12}$")
    kind: CodeKind
    variant_id: Id | None = fk("variant", default=None)
    ad_id: Id | None = fk("ad", default=None)
    tracked_link_id: Id | None = fk("tracked_link", default=None)
    label: str | None = Field(default=None, max_length=120)
    active: bool = True


__all__ = ["CodeKind", "TrackingCode"]
