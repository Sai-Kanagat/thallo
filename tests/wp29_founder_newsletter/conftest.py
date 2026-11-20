"""Shared fixtures for WP-29: a tenant, a founder, answers, and a scripted text registry (no network)."""
from __future__ import annotations

import os

import pytest

from tests.wp06_text.test_copy import Script, make_registry
from thallo.capture.records import AnswerMode, CaptureAnswer, TranscriptState
from thallo.connections import InMemorySecretStore, KeyRing, Vault
from thallo.contracts.clock import FAKE_EPOCH, FakeClock
from thallo.contracts.enums import Language, SourceKind, UserRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser, Source, Tenant
from thallo.contracts.testing.samples import T, _id
from thallo.extensions.founder import Answer

SAID = ("We started the workshop because we were tired of bikes coming back with the same fault. "
        "I still check every frame myself before it leaves, and I tell every customer why the chain matters.")
POST = ("We started the workshop because we were tired of bikes coming back with the same fault. "
        "I still check every frame myself before it leaves.")
GHOST = ("Artificial intelligence will transform global logistics markets and venture investors "
         "should prepare portfolios accordingly.")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FAKE_EPOCH)


@pytest.fixture
def vault(clock) -> Vault:
    return Vault(KeyRing(os.urandom(32)), InMemorySecretStore(), clock)


@pytest.fixture
def tenant(clock) -> Tenant:
    return Tenant(id=T, created_at=clock.now(), operator_id=_id(1), name="Workshop", slug="workshop",
                  default_language=Language.EN, region_code="IT", currency="EUR")


@pytest.fixture
def founder(clock) -> AppUser:
    return AppUser(id=_id(40), created_at=clock.now(), tenant_id=T, identity_id=_id(41), display_name="Founder",
                   role=UserRole.FOUNDER)


def answer_of(clock, user_id: str, text: str = SAID, state=TranscriptState.DONE) -> Answer:
    now = clock.now()
    src = Source(id=new_id(clock), created_at=now, tenant_id=T, kind=SourceKind.TYPED, title="Why did you start?",
                 transcript=text, untrusted=True, captured_at=now)
    ans = CaptureAnswer(id=new_id(clock), created_at=now, tenant_id=T, mode=AnswerMode.TYPED, source_id=src.id,
                        answered_by=user_id, transcript_state=state, provider="typed", updated_at=now)
    return Answer(ans, src)


@pytest.fixture
def registry_for(clock, vault):
    def _make(*texts: str):
        script = Script(*texts)
        return make_registry(clock, vault, script, prices=("2", "10")), script
    return _make
