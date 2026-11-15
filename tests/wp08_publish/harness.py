"""The publisher wired to the wave 1 backends (memory and Postgres as the per-run app role), with
a fake publishing provider that records every call and never leaves the process.

The seeded shop (tests/integration/test_wave1_flow.py `seed` and `approve`) is an approved
Instagram variant with text, a claim, a tracked link and one image, checked by the real truth
compiler and the real design-lint writer, bound to the hash the gate computes. Nothing here
loosens coverage: the media path runs on rows the real writers produced.
"""
from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tests.integration.test_wave1_flow import Shop, _id
from thallo.connections import InMemoryConnectionStore, ProviderRegistry
from thallo.connections.registry import ConnectionRegistry, ProviderContext
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, Channel, ConnectionMode
from thallo.contracts.models import ChannelAccount, Connection
from thallo.contracts.testing.mocks import MockProvider
from thallo.gate.signing import GateSigner
from thallo.publish import PgPublishStore, Publisher, PublishSession, PublishStore, current

FAKE = "fake_poster"


@dataclass
class FakePoster:
    """A publishing provider double. `behaviour` decides what `run(PublishRequest)` does:
    "ok", "not_sent_retry", "not_sent_final", "timeout", "no_id". `posts` is what the service
    holds, by idempotency key; `live` is the text a readback returns per external id."""

    clock: Any
    behaviour: str = "ok"
    calls: list[c.PublishRequest] = field(default_factory=list)
    uploads: list[str] = field(default_factory=list)  # sha256 of the bytes the provider got
    readbacks: int = 0
    posts: dict[str, str] = field(default_factory=dict)
    live: dict[str, str | None] = field(default_factory=dict)
    media_changed: bool | None = False
    on_send: Callable[[], None] | None = None  # runs inside the send, before the outcome
    capability = Capability.PUBLISHING
    name = FAKE

    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=FAKE, mode=ConnectionMode.HOSTED, sends_externally=True)

    def health(self) -> c.Health:
        return c.Health(connected=True, checked_at=self.clock.now())

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        return c.Estimate(cost_eur=Decimal(0), seconds=0.0)

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name="mock-fake")

    def _result(self, output: c.Result, ids: dict[str, str] | None = None) -> c.RunResult:
        return c.RunResult(output=output, cost_eur=Decimal(0), external_ids=ids or {},
                           provenance=c.Provenance(made_by="provider", provider=FAKE, mode=ConnectionMode.HOSTED,
                                                   made_at=self.clock.now()))

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)
        if isinstance(request, c.ReadbackRequest):
            self.readbacks += 1
            ext = request.external_id or self.posts.get(request.idempotency_key or "")
            if ext is None:
                return self._result(c.ReadbackResult(live_text=None))
            return self._result(c.ReadbackResult(live_text=self.live.get(ext), media_changed=self.media_changed),
                                {"external_id": ext, "live_url": f"https://social.invalid/p/{ext}"})
        assert isinstance(request, c.PublishRequest)
        self.calls.append(request)
        ctx = current()
        assert ctx is not None, "the publisher stages every send"
        ctx.verify(request)
        self.uploads += [hashlib.sha256(ctx.media_for(i, s)).hexdigest() for i, s in enumerate(request.media_sha256)]
        if self.on_send is not None:
            self.on_send()
        from thallo.publish import NotSent

        if self.behaviour == "not_sent_retry":
            raise NotSent("rate limited", retryable=True)
        if self.behaviour == "not_sent_final":
            raise NotSent("refused", retryable=False)
        ext = f"post-{len(self.calls)}"
        self.posts[request.idempotency_key] = ext
        self.live.setdefault(ext, request.text)
        if self.behaviour == "timeout":
            raise TimeoutError("the service did not answer")
        if self.behaviour == "no_id":
            return self._result(c.PublishResult(status="published"))
        return self._result(c.PublishResult(status="published", external_id=ext,
                                            live_url=f"https://social.invalid/p/{ext}"))


@dataclass
class Rig:
    backend: Any
    shop: Shop
    signer: GateSigner
    registry: ConnectionRegistry
    poster: FakePoster
    publisher: Publisher
    account: ChannelAccount
    gate_wrap: Callable[[Any], Any] | None = None
    store_class: type[PublishStore] | None = None

    @property
    def t(self) -> str:
        return self.shop.tenant.id

    def publish(self, **kw: Any):
        self.backend.clock.advance(seconds=1)
        return self.publisher.publish(tenant_id=self.t, bundle_id=kw.get("bundle_id", self.shop.bundle.id),
                                      variant_id=kw.get("variant_id", self.shop.variant.id),
                                      approval_id=kw.get("approval_id", self.shop.approval.id),
                                      channel_account_id=kw.get("channel_account_id", self.account.id))

    def session(self):
        return self.sessions(self.t)

    @contextmanager
    def sessions(self, tenant_id: str) -> Iterator[PublishSession]:
        with self.backend.session(tenant_id) as s:
            if self.backend.name == "postgres":
                store = (self.store_class or PgPublishStore)(s.repo, tenant_id, self.backend.clock)
            else:
                store = (self.store_class or PublishStore)(s, tenant_id, self.backend.clock)
            gate = self.gate_wrap(s.gate) if self.gate_wrap else s.gate
            yield PublishSession(gate=gate, store=store)


def mirror_connections(rig: Rig) -> None:
    """On Postgres, publication.connection_id is a foreign key: put the registry's rows in the table."""
    if rig.backend.name != "postgres":
        return
    with rig.backend.session(rig.t) as s:
        have = {r.id for r in s.list(Connection)}
        for row in rig.registry.connections.list_for_tenant(rig.t):
            if row.id not in have:
                s.insert(row)


def build_rig(backend, shop: Shop, signer: GateSigner, *, fake: bool = True, behaviour: str = "ok") -> Rig:
    clock = backend.clock
    poster = FakePoster(clock, behaviour=behaviour)
    providers = ProviderRegistry.with_discovered()
    providers.register(Capability.PUBLISHING, FAKE, ConnectionMode.HOSTED, lambda ctx: poster)
    registry = ConnectionRegistry(clock=clock, providers=providers, connections=InMemoryConnectionStore(),
                                  environ={})
    t = shop.tenant.id
    if fake:
        registry.add(t, Capability.PUBLISHING, FAKE)
    else:
        registry.ensure_manual(t, Capability.PUBLISHING)
    with backend.session(t) as s:
        account = s.insert(ChannelAccount(id=_id(backend), created_at=clock.now(), tenant_id=t,
                                          channel=Channel.INSTAGRAM, handle=f"@shop{secrets.token_hex(2)}"))
    rig = Rig(backend, shop, signer, registry, poster, publisher=None, account=account)  # type: ignore[arg-type]
    rig.publisher = Publisher(registry=registry, sessions=rig.sessions, clock=clock, signer=signer)
    mirror_connections(rig)
    return rig


def unused_ctx(clock, row: Connection) -> ProviderContext:
    return ProviderContext(row.tenant_id, row, clock)
