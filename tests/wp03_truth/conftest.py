from __future__ import annotations

from datetime import timedelta

import pytest

from cortex_operator.contracts.actors import HumanActor, SystemActor
from cortex_operator.contracts.clock import FAKE_EPOCH, FakeClock
from cortex_operator.contracts.enums import ClaimKind, Language
from cortex_operator.contracts.ids import new_id
from cortex_operator.truth import InMemoryClaimRepository, build, compile_text, new_candidate, verify_claim

SYSTEM = SystemActor(id="extractor")
DAY = timedelta(days=1)
ALL_LANGUAGES = (Language.EN, Language.IT, Language.TA)


class FakeVerifier:
    def __init__(self, live: set[str]) -> None:
        self.live = live

    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id in self.live


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def tenant(clock: FakeClock) -> str:
    return new_id(clock)


@pytest.fixture
def human(clock: FakeClock, tenant: str) -> HumanActor:
    return HumanActor(id=new_id(clock), tenant_id=tenant, session_id="live")


@pytest.fixture
def verifier() -> FakeVerifier:
    return FakeVerifier({"live"})


@pytest.fixture
def repo() -> InMemoryClaimRepository:
    return InMemoryClaimRepository()


def wording(value: str) -> dict[str, str]:
    """Approved wording in every language for a unitless NUMBER claim (S4)."""
    return {"en": f"{value} showrooms", "it": f"{value} sedi", "ta": f"{value} கடைகள்"}


@pytest.fixture
def make(clock, tenant, human, verifier, repo):
    """make(code, kind, value, unit=None, verified=True, tenant_id=..., **kw) -> Claim. Saves to `repo`."""

    def _make(code: str, kind: ClaimKind, value: str, unit: str | None = None, *, verified: bool = True,
              tenant_id: str | None = None, **kw):
        expires_at = kw.pop("verify_expires_at", None)
        reason = kw.pop("expiry_reason", None)
        tenant_languages = kw.pop("tenant_languages", ALL_LANGUAGES)
        owner = tenant_id or tenant
        claim, _ = new_candidate(tenant_id=owner, code=code, kind=kind, value=value, unit=unit, clock=clock,
                                 actor=SYSTEM, evidence=f"source for {code}", **kw)
        store = repo if owner == tenant else InMemoryClaimRepository()
        if verified:
            actor = human if owner == tenant else HumanActor(id=human.id, tenant_id=owner, session_id="live")
            claim, _ = verify_claim(claim, actor, verifier=verifier, clock=clock, repo=store, expires_at=expires_at,
                                    expiry_reason=reason, tenant_languages=tenant_languages)
        store.save(claim)
        return claim

    return _make


@pytest.fixture
def run(clock, tenant):
    """run(text, language=EN, claims=None, **kw) -> CompileResult"""

    def _run(text: str, language: Language = Language.EN, claims=None, **kw):
        return compile_text(text, tenant_id=tenant, language=language, claims=claims or {}, clock=clock, **kw)

    return _run


@pytest.fixture
def built(clock, tenant):
    """built(text, claims, language=EN, default=EN) -> output text"""

    def _built(text: str, claims, language: Language = Language.EN, default: Language = Language.EN, **kw):
        return build(text, tenant_id=tenant, language=language, claims=claims, clock=clock,
                     default_language=default, **kw).filled.text

    return _built
