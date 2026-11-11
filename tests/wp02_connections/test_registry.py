from __future__ import annotations

from decimal import Decimal

import pytest

from cortex_operator.connections import (
    InvalidConnection,
    ManualRequired,
    ProviderContext,
    ProviderUnavailable,
    UnknownProvider,
)
from cortex_operator.connections.stores import InMemoryHealthState
from cortex_operator.contracts import connection as c
from cortex_operator.contracts.clock import FAKE_EPOCH, FakeClock
from cortex_operator.contracts.enums import Capability, ConnectionMode, TextRole
from cortex_operator.contracts.testing.contract_suite import assert_provider_contract
from cortex_operator.contracts.testing.mocks import MockProvider
from cortex_operator.contracts.testing.samples import sample_requests

from .conftest import T1, T2

TEXT = Capability.TEXT
S = c.OverrideScope


class Hosted(MockProvider):
    """A pretend hosted provider, defined in the test: adding one needs no core change."""

    behaviour = "ok"

    def __init__(self, ctx: ProviderContext, cap: Capability = TEXT):
        super().__init__(cap, ctx.clock, name=ctx.connection.provider)
        self.ctx = ctx

    def describe(self):
        return super().describe().model_copy(update={"mode": ConnectionMode.HOSTED, "sends_externally": True})

    def run(self, request):
        if self.behaviour == "unavailable":
            raise ProviderUnavailable("down")
        if self.behaviour == "boom":
            raise RuntimeError("provider blew up after reaching the vendor")
        return super().run(request)


def register(providers, name, behaviour="ok", cap=TEXT):
    cls = type("H", (Hosted,), {"behaviour": behaviour})
    providers.register(cap, name, ConnectionMode.HOSTED, lambda ctx: cls(ctx, cap))


def ids(registry, choice):
    rows = {r.id: r for r in registry.connections.list_all()}
    return [rows[i].provider for i in choice.ordered_connection_ids]


def test_empty_tenant_resolves_to_manual_for_every_capability(registry):
    for cap in Capability:
        choice = registry.resolve(T1, cap)
        assert ids(registry, choice) == ["manual"]


def test_default_then_fallbacks_in_priority_order_with_manual_last(registry, providers):
    for n in ("a", "b", "c"):
        register(providers, n)
    registry.add(T1, TEXT, "a")
    registry.add(T1, TEXT, "c", priority=5)
    registry.add(T1, TEXT, "b", priority=2)
    registry.add(T1, TEXT, "mock", priority=9)
    assert ids(registry, registry.resolve(T1, TEXT)) == ["a", "b", "c", "mock", "manual"]


def test_set_order_changes_the_default(registry, providers):
    register(providers, "a")
    register(providers, "b")
    a, b = registry.add(T1, TEXT, "a"), registry.add(T1, TEXT, "b")
    registry.set_order(T1, [b.id, a.id])
    assert ids(registry, registry.resolve(T1, TEXT)) == ["b", "a", "manual"]


def test_manual_as_default_ends_the_chain(registry, providers):
    register(providers, "a")
    registry.add(T1, TEXT, "a")
    registry.add(T1, TEXT, "mock", priority=-1)
    man = next(r for r in registry.connections.list_all() if r.provider == "manual")
    registry.connections.put(man.model_copy(update={"priority": -5}))
    assert ids(registry, registry.resolve(T1, TEXT)) == ["manual"]


def test_manual_cannot_be_removed_or_disabled(registry):
    registry.ensure_manual(T1)
    man = next(r for r in registry.connections.list_for_tenant(T1) if r.capability is TEXT)
    with pytest.raises(ManualRequired):
        registry.remove(T1, man.id)
    with pytest.raises(ManualRequired):
        registry.set_enabled(T1, man.id, False)
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "manual")  # a second tenant-level manual row


def test_disabled_connection_is_skipped(registry, providers):
    register(providers, "a")
    row = registry.add(T1, TEXT, "a")
    registry.set_enabled(T1, row.id, False)
    assert ids(registry, registry.resolve(T1, TEXT)) == ["manual"]


def test_scope_overrides_piece_format_brand_tenant(registry, providers):
    for n in ("tenant_p", "brand_p", "format_p", "piece_p"):
        register(providers, n)
    registry.add(T1, TEXT, "tenant_p")
    registry.add(T1, TEXT, "brand_p", scope=S.BRAND, scope_ref="b1")
    registry.add(T1, TEXT, "format_p", scope=S.FORMAT, scope_ref="carousel")
    registry.add(T1, TEXT, "piece_p", scope=S.PIECE, scope_ref="p1")
    full = registry.resolve(T1, TEXT, brand_id="b1", format="carousel", piece_id="p1")
    assert ids(registry, full) == ["piece_p", "format_p", "brand_p", "tenant_p", "manual"]
    assert (full.scope, full.scope_ref) == (S.PIECE, "p1")
    no_piece = registry.resolve(T1, TEXT, brand_id="b1", format="carousel", piece_id="other")
    assert ids(registry, no_piece)[0] == "format_p" and no_piece.scope is S.FORMAT
    brand = registry.resolve(T1, TEXT, brand_id="b1")
    assert ids(registry, brand) == ["brand_p", "tenant_p", "manual"] and brand.scope is S.BRAND
    assert ids(registry, registry.resolve(T1, TEXT, brand_id="b2")) == ["tenant_p", "manual"]
    assert registry.resolve(T1, TEXT).scope is S.TENANT


def test_piece_override_to_manual_ends_the_chain(registry, providers):
    register(providers, "a")
    registry.add(T1, TEXT, "a")
    registry.add(T1, TEXT, "manual", scope=S.PIECE, scope_ref="p1")
    assert ids(registry, registry.resolve(T1, TEXT, piece_id="p1")) == ["manual"]
    assert ids(registry, registry.resolve(T1, TEXT)) == ["a", "manual"]


def test_text_role_specific_first_then_roleless(registry, providers):
    for n in ("any", "grader"):
        register(providers, n)
    registry.add(T1, TEXT, "any")
    registry.add(T1, TEXT, "grader", role=TextRole.GRADE, priority=7)
    assert ids(registry, registry.resolve(T1, TEXT, role=TextRole.GRADE)) == ["grader", "any", "manual"]
    assert ids(registry, registry.resolve(T1, TEXT, role=TextRole.DRAFT)) == ["any", "manual"]
    assert ids(registry, registry.resolve(T1, TEXT)) == ["any", "manual"]


def test_tenants_are_isolated(registry, providers):
    register(providers, "a")
    registry.add(T1, TEXT, "a")
    assert ids(registry, registry.resolve(T2, TEXT)) == ["manual"]
    row = next(r for r in registry.connections.list_for_tenant(T1) if r.provider == "a")
    with pytest.raises(InvalidConnection):
        registry.remove(T2, row.id)


def test_validation(registry, providers):
    register(providers, "a")
    with pytest.raises(UnknownProvider):
        registry.add(T1, TEXT, "nope")
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "a", scope=S.BRAND)
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "a", scope_ref="b1")
    with pytest.raises(InvalidConnection):
        registry.add(T1, Capability.IMAGE, "mock", role=TextRole.DRAFT)
    registry.add(T1, TEXT, "a")
    with pytest.raises(InvalidConnection):
        registry.add(T1, TEXT, "a")


def test_row_mode_comes_from_the_registered_provider(registry):
    assert registry.add(T1, TEXT, "mock").mode is ConnectionMode.MOCK
    assert registry.add(T1, Capability.ADS, "mock").mode is ConnectionMode.MOCK


def test_any_capability_can_be_set_to_manual_or_mock_per_client(registry):
    for cap in Capability:
        registry.add(T1, cap, "mock")
        assert ids(registry, registry.resolve(T1, cap)) == ["mock", "manual"]
        assert ids(registry, registry.resolve(T2, cap)) == ["manual"]


def test_secret_goes_to_the_vault_and_the_row_holds_only_a_ref(registry, providers, vault):
    register(providers, "a")
    row = registry.add(T1, TEXT, "a", secret="sk-test-ROWSECRET")
    assert row.secret_ref and "ROWSECRET" not in row.model_dump_json() and "ROWSECRET" not in repr(row)
    assert vault.get(T1, row.secret_ref).reveal() == "sk-test-ROWSECRET"
    prov = registry.build(row)
    assert prov.ctx.secret().reveal() == "sk-test-ROWSECRET"
    registry.remove(T1, row.id)
    assert not vault.exists(T1, row.secret_ref)


def test_raw_secrets_in_settings_are_refused(registry, providers):
    register(providers, "a")
    for bad in ({"api_key": "x"}, {"nested": {"Authorization": "x"}}, {"list": [{"token": "x"}]}):
        with pytest.raises(InvalidConnection):
            registry.add(T1, TEXT, "a", settings=bad)
    registry.add(T1, TEXT, "a", settings={"endpoint": "https://api.invalid"})


def test_adding_a_provider_is_registration_only(registry, providers):
    clock = FakeClock(FAKE_EPOCH)
    register(providers, "newcomer")
    row = registry.add(T1, TEXT, "newcomer")
    prov = registry.build(row)
    assert_provider_contract(prov, clock)


# ---------------------------------------------------------------- running down the chain

def req():
    return sample_requests()[TEXT][0]


def test_run_uses_the_default(registry, providers):
    register(providers, "a")
    registry.add(T1, TEXT, "a")
    served = registry.run(T1, req())
    assert served.provider == "a" and served.skipped == ()


def test_run_falls_back_when_nothing_could_have_happened(registry, providers):
    register(providers, "down", "unavailable")
    register(providers, "ok")
    registry.add(T1, TEXT, "down")
    registry.add(T1, TEXT, "ok")
    served = registry.run(T1, req())
    assert served.provider == "ok" and [r for _, r in served.skipped] == ["unavailable"]


def test_run_falls_to_manual_last(registry, providers):
    register(providers, "down", "unavailable")
    registry.add(T1, TEXT, "down")
    served = registry.run(T1, req())
    assert served.provider == "manual" and served.result.provenance.made_by == "person"


def test_run_does_not_fall_back_after_an_unknown_failure(registry, providers):
    """A hosted provider that raised mid-call may already have acted; a retry elsewhere could double-send."""
    register(providers, "boom", "boom")
    registry.add(T1, TEXT, "boom")
    with pytest.raises(RuntimeError):
        registry.run(T1, req())


def test_run_falls_back_on_cost_cap_before_spending(registry, providers):
    class Pricey(MockProvider):
        def estimate(self, request):
            return c.Estimate(cost_eur=Decimal(5), seconds=1.0)

        def run(self, request):
            c.check_cap(self.estimate(request), request)
            return super().run(request)

    providers.register(TEXT, "pricey", ConnectionMode.HOSTED, lambda ctx: Pricey(TEXT, ctx.clock, "pricey"))
    registry.add(T1, TEXT, "pricey")
    capped = req().model_copy(update={"cost_cap_eur": Decimal(1)})
    assert registry.run(T1, capped).provider == "manual"


def test_run_skips_connections_seen_unhealthy_but_never_manual(clock, providers, vault):
    from cortex_operator.connections import ConnectionRegistry, InMemoryConnectionStore
    state = InMemoryHealthState()
    reg = ConnectionRegistry(clock=clock, providers=providers, connections=InMemoryConnectionStore(),
                             vault=vault, health=state)
    register(providers, "a")
    row = reg.add(T1, TEXT, "a")
    state.set_connected(row.id, False)
    served = reg.run(T1, req())
    assert served.provider == "manual" and served.skipped == ((row.id, "unhealthy"),)
    man = next(r for r in reg.connections.list_all() if r.provider == "manual")
    state.set_connected(man.id, False)
    assert reg.run(T1, req()).provider == "manual"


def test_text_role_comes_from_the_request(registry, providers):
    register(providers, "grader")
    register(providers, "any")
    registry.add(T1, TEXT, "any")
    registry.add(T1, TEXT, "grader", role=TextRole.GRADE, priority=5)
    graded = req().model_copy(update={"role": TextRole.GRADE})
    assert registry.run(T1, graded).provider == "grader"
    assert registry.run(T1, req()).provider == "any"
