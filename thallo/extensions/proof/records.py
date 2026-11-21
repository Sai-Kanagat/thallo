"""The proof tables (migration 0039). See the migration for what each row means."""
from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from thallo.contracts.ids import Id
from thallo.contracts.models import TenantRow


class Use(StrEnum):
    """What a customer agreed to, on top of the consent row's closed scopes."""

    QUOTE = "quote"  # their exact words may be quoted (needs consent scope QUOTE)
    NAME = "name"  # the agreed name text may sit under the words; without it the card says "A customer"
    PHOTO = "photo"  # a photo of them may appear (needs consent scope LIKENESS)
    RESULT = "result"  # a verified result of their job may be told as their story (needs QUOTE)


class MaterialKind(StrEnum):
    QUOTE = "quote"
    PHOTO = "photo"
    RESULT = "result"


class PieceKind(StrEnum):
    QUOTE_CARD = "quote_card"
    CASE_STORY = "case_story"


class AskKind(StrEnum):
    REVIEW = "review"
    REFERRAL = "referral"


LIVE = "live"
WITHDRAWN = "withdrawn"


class ProofGrant(TenantRow):
    __table__ = "proof_grant"
    consent_id: Id
    person_id: Id
    deal_id: Id
    uses: list[Use]
    name_shown: str | None = None
    source_ids: list[Id] = Field(default_factory=list)  # recordings a person confirmed are this customer's
    recorded_by: Id


class ProofMaterial(TenantRow):
    __table__ = "proof_material"
    kind: MaterialKind
    grant_id: Id
    consent_id: Id
    person_id: Id
    deal_id: Id
    source_id: Id | None = None
    span_start: int | None = None
    span_end: int | None = None
    text: str | None = None  # the exact words, a slice of the transcript
    text_sha256: str | None = None
    media_id: Id | None = None
    claim_id: Id | None = None
    state: str = LIVE
    withdrawn_at: AwareDatetime | None = None


class ProofPiece(TenantRow):
    __table__ = "proof_piece"
    kind: PieceKind
    deal_id: Id
    person_id: Id
    consent_id: Id
    material_ids: list[Id]
    claim_ids: list[Id] = Field(default_factory=list)
    body: str  # result numbers appear only as {{C-0001}} slots
    card: dict = Field(default_factory=dict)
    state: str = LIVE
    variant_id: Id | None = None
    withdrawn_at: AwareDatetime | None = None


class ProofAsk(TenantRow):
    __table__ = "proof_ask"
    kind: AskKind
    lead_id: Id
    message_id: Id


class ProofFlag(TenantRow):
    __table__ = "proof_flag"
    piece_id: Id
    consent_id: Id
    variant_id: Id | None = None
    live: bool  # a publication exists: a person must take it down; the machine never unpublishes
    resolved_by: Id | None = None
    resolved_at: AwareDatetime | None = None


class BuyerNote(TenantRow):
    __table__ = "buyer_note"
    grant_id: Id
    consent_id: Id
    source_id: Id
    span_start: int
    span_end: int
    phrase: str
    state: str = LIVE


__all__ = ["LIVE", "WITHDRAWN", "AskKind", "BuyerNote", "MaterialKind", "PieceKind", "ProofAsk", "ProofFlag",
           "ProofGrant", "ProofMaterial", "ProofPiece", "Use"]
