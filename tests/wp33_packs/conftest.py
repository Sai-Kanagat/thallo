"""Shared helpers for WP-33. No keys, no network: signing keys are generated per test."""
from __future__ import annotations

from datetime import timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.wp01_store.conftest import admin, clock, database, urls  # noqa: F401
from thallo.contracts.actors import HumanActor
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import ClaimKind, Language, SourceKind
from thallo.contracts.models import Claim, Source
from thallo.contracts.states import ClaimState
from thallo.contracts.testing.samples import T, _id
from thallo.extensions.packs import Ed25519Signer, TrustedKeys

LANGS = (Language.EN, Language.IT)
DAY = timedelta(days=1)


class FakeVerifier:
    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id == "live"


def signer() -> Ed25519Signer:
    return Ed25519Signer(Ed25519PrivateKey.generate())


def trusting(*signers: Ed25519Signer) -> TrustedKeys:
    return TrustedKeys([s.private.public_key() for s in signers])


def library(tenant_id: str = T, *, base: int = 1, source_kind: SourceKind = SourceKind.DOCUMENT,
            consent_id: str | None = None, clock_at=FAKE_EPOCH):
    src = Source(id=_id(base + 50), created_at=clock_at, tenant_id=tenant_id, kind=source_kind,
                 title="Model X spec sheet", uri="https://maker.example/specs/model-x.pdf", captured_at=clock_at,
                 consent_id=consent_id)
    common = dict(created_at=clock_at, tenant_id=tenant_id, state=ClaimState.VERIFIED, source_id=src.id,
                  verified_by=_id(base + 70), verified_at=clock_at - DAY, source_span={"start": 0, "end": 3,
                                                                                      "text": "private note"},
                  evidence_sha256="ab" * 32)
    claims = {
        "C-0001": Claim(id=_id(base), code="C-0001", kind=ClaimKind.NUMBER, value="5", unit="year",
                        expires_at=clock_at + 200 * DAY, **common),
        "C-0002": Claim(id=_id(base + 1), code="C-0002", kind=ClaimKind.NUMBER, value="12",
                        languages={"en": "warranty points", "it": "punti di garanzia"}, **common),
        "C-0003": Claim(id=_id(base + 2), code="C-0003", kind=ClaimKind.PRICE, value="45", unit="EUR",
                        expires_at=clock_at + 20 * DAY, **common),
        "C-0004": Claim(id=_id(base + 3), code="C-0004", kind=ClaimKind.CAPABILITY, value="Runs on solar power",
                        **common),
    }
    return claims, {src.id: src}


def human(tenant_id: str, user_id: str | None = None) -> HumanActor:
    return HumanActor(id=user_id or _id(90), tenant_id=tenant_id, session_id="live")


def now_clock() -> FakeClock:
    return FakeClock(FAKE_EPOCH)
