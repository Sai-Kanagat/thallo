from __future__ import annotations

from datetime import timedelta

import pytest

from cortex_operator.contracts.actors import HumanActor, SystemActor
from cortex_operator.contracts.clock import FAKE_EPOCH, FakeClock
from cortex_operator.contracts.enums import ClaimKind
from cortex_operator.contracts.ids import new_id
from cortex_operator.truth import new_candidate, verify_claim

SYSTEM = SystemActor(id="extractor")


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
def make(clock, tenant, human, verifier):
    """make(code, kind, value, unit=None, verified=True, **kw) -> Claim"""

    def _make(code: str, kind: ClaimKind, value: str, unit: str | None = None, *, verified: bool = True, **kw):
        expires_at = kw.pop("verify_expires_at", None)
        claim, _ = new_candidate(tenant_id=tenant, code=code, kind=kind, value=value, unit=unit, clock=clock,
                                 actor=SYSTEM, evidence=f"source for {code}", **kw)
        if verified:
            claim, _ = verify_claim(claim, human, verifier=verifier, clock=clock, expires_at=expires_at)
        return claim

    return _make


DAY = timedelta(days=1)
