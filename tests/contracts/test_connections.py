"""The connection contract, its safety rules and the mock for every capability."""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from cortex_operator.contracts import connection as c
from cortex_operator.contracts.actors import HumanActor
from cortex_operator.contracts.clock import FAKE_EPOCH, FakeClock
from cortex_operator.contracts.enums import Capability, Channel, Language, MessageChannel
from cortex_operator.contracts.events import ClaimPayload, Event, EventName
from cortex_operator.contracts.testing.contract_suite import ContractFailure, assert_provider_contract
from cortex_operator.contracts.testing.mocks import MockProvider, mock_providers

T = "0190f3a0-0000-7000-8000-000000000001"
A = "0190f3a0-0000-7000-8000-000000000031"
B = "0190f3a0-0000-7000-8000-000000000032"
SHA = "a" * 64


def test_every_capability_has_request_types():
    assert set(c.REQUEST_TYPES) == set(Capability)
    for cap, types in c.REQUEST_TYPES.items():
        assert all(t.capability is cap for t in types)


@pytest.mark.parametrize("cap", list(Capability))
def test_mock_provider_passes_contract(cap):
    clk = FakeClock(FAKE_EPOCH)
    assert_provider_contract(mock_providers(clk)[cap], clk)


def test_contract_suite_catches_a_provider_that_ignores_cost_caps():
    class Careless(MockProvider):
        def run(self, request):
            c.check_request(self, request)
            return super().run(request.model_copy(update={"cost_cap_eur": None}))

    clk = FakeClock(FAKE_EPOCH)
    with pytest.raises(ContractFailure):
        assert_provider_contract(Careless(Capability.TEXT, clk), clk)


def _publish(**kw):
    base = dict(tenant_id=T, idempotency_key="variant:approval", channel=Channel.INSTAGRAM,
                channel_account_id=A, variant_id=A, approval_id=B, content_hash=SHA, text="x")
    return c.PublishRequest(**(base | kw))


def test_publish_needs_real_approval_hash_and_idempotency_key():
    _publish()
    for bad in ({"approval_id": ""}, {"content_hash": ""}, {"content_hash": "h"}, {"idempotency_key": "k"},
                {"variant_id": "v1"}):
        with pytest.raises(ValidationError):
            _publish(**bad)


def test_message_send_needs_exactly_one_authority():
    sai = HumanActor(id="u1", tenant_id=T, session_id="s1")
    base = dict(tenant_id=T, message_id=A, channel=MessageChannel.WHATSAPP, to="x", body="hi")
    with pytest.raises(ValidationError):
        c.MessageSendRequest(**base)
    with pytest.raises(ValidationError):  # a template instance needs consent
        c.MessageSendRequest(**base, template_approval_id=B)
    with pytest.raises(ValidationError):  # not both
        c.MessageSendRequest(**base, template_approval_id=B, consent_id=B, human=sai)
    with pytest.raises(ValidationError):  # sender from another tenant
        c.MessageSendRequest(**base, human=sai.model_copy(update={"tenant_id": B}))
    c.MessageSendRequest(**base, human=sai)
    c.MessageSendRequest(**base, template_approval_id=B, consent_id=B)


def _apply(**kw):
    base = dict(tenant_id=T, action=c.AdsAction.APPLY_SIGNED, platform="meta", spend_proposal_id=A,
                approval_id=B, approved_content_hash=SHA, to_daily_eur=Decimal("20"), campaign_external_id="c1")
    return c.AdsRequest(**(base | kw))


def test_spend_change_carries_the_exact_signed_amount():
    _apply()
    for missing in ("spend_proposal_id", "approval_id", "approved_content_hash", "to_daily_eur",
                    "campaign_external_id"):
        with pytest.raises(ValidationError):
            _apply(**{missing: None})


def test_no_side_door_for_spend():
    with pytest.raises(ValidationError):
        c.AdsRequest(tenant_id=T, action=c.AdsAction.CREATE_PAUSED, platform="meta",
                     payload={"status": "ACTIVE", "daily_budget": 500})
    with pytest.raises(ValidationError):
        c.AdsRequest(tenant_id=T, action=c.AdsAction.PAUSE, platform="meta", to_daily_eur=Decimal(5))


def test_model_construct_cannot_skip_validation_at_the_provider():
    clk = FakeClock(FAKE_EPOCH)
    forged = c.AdsRequest.model_construct(tenant_id=T, action=c.AdsAction.APPLY_SIGNED, platform="meta",
                                          payload={}, cost_cap_eur=None, idempotency_key=None,
                                          campaign_external_id=None, ad_id=None, spend_proposal_id=None,
                                          approval_id=None, approved_content_hash=None, to_daily_eur=None)
    with pytest.raises(ValidationError):
        MockProvider(Capability.ADS, clk).run(forged)


def test_hooks_never_see_sessions_or_other_tenants():
    sai = HumanActor(id="u1", tenant_id=T, session_id="SESSION-SECRET")
    ev = Event.build(id=A, tenant_id=T, occurred_at=FAKE_EPOCH, actor=sai, name=EventName.CLAIM_VERIFIED,
                     payload=ClaimPayload(claim_id="c", code="C-0001"))
    hook = c.HookRequest(tenant_id=T, event=ev)
    assert "SESSION-SECRET" not in hook.model_dump_json()
    with pytest.raises(ValidationError):
        c.HookRequest(tenant_id=B, event=ev)


def test_generated_people_and_real_voices_need_consent():
    with pytest.raises(ValidationError):
        c.ImageRequest(tenant_id=T, purpose="background", width=1, height=1, allow_generated_people=True)
    with pytest.raises(ValidationError):
        c.VideoRequest(tenant_id=T, action="generate", width=1, height=1, allow_generated_people=True)
    with pytest.raises(ValidationError):
        c.VoiceoverRequest(tenant_id=T, text="x", language=Language.TA, voice="v", is_real_person_voice=True)


def test_requests_are_immutable():
    req = c.TextRequest(tenant_id=T, role="draft", instructions="x")
    with pytest.raises(ValidationError):
        req.instructions = "y"


def test_research_results_are_always_untrusted():
    with pytest.raises(ValidationError):
        c.ResearchResult(items=(), untrusted=False)
