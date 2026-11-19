"""Shared code for the analytics providers. No `register` here.

Every provider in this folder answers an `AnalyticsRequest` with rows of the one shape

    {"date": "YYYY-MM-DD", "metric": <our metric name>, "value": <finite number>, "untrusted": {...}}

`metric` is always one of the provider's allow-listed names, never a string from the source.
Anything the source wrote (search queries, page paths, labels) goes only under `untrusted`, as
data for later code; it is never a key, a metric name or error text. A metric asked for that the
provider does not know, or that the source did not return, is in `missing`: missing is missing,
never zero.

Raw pulls are kept: every call's decoded reply is handed to the staged intake (thallo/signals/
context.py) under its sha256, and `raw_ref` is `sha256:<hex>`. The intake writes it to
`signal_pull`.

Where calls go. Hosts are constants in each provider file. Settings hold only strictly validated
ids (a property number, a site id) and a timeout; never a URL, host or path. Every call goes
through `PostizTransport` (https only, public addresses only, pinned socket, no redirects; the
secret is put in the header only after the connection to the checked address is open), behind
an exact origin match. Errors carry a status class or an error type, never a body.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ValidationError

from thallo.connections.errors import ConnectionsError, InvalidConnection, ProviderUnavailable
from thallo.connections.registry import ProviderContext
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.testing.mocks import MockProvider
from thallo.providers.publishing.postiz_base import PostizTransport, RawCall, RawReply, TransportError
from thallo.providers.text.base import Auth, Origin, check_header_secret, parse_origin, status_class
from thallo.signals import context as staging

DEFAULT_TIMEOUT_S = 30.0
Transport = Any  # a callable RawCall -> RawReply


class PullFailed(ConnectionsError):
    """The source did not give a usable answer. The message is safe to store and show."""


def raw_ref(body: Any) -> str:
    """Keep the raw reply in the staged intake and return its reference."""
    blob = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    sha = hashlib.sha256(blob).hexdigest()
    staging.keep_raw(sha, body)
    return f"sha256:{sha}"


def number(value: Any) -> float | None:
    """A finite number from a source value, or None (which means missing, never zero)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            out = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return out if math.isfinite(out) else None


def in_window(day: str, req: c.AnalyticsRequest) -> bool:
    try:
        d = date.fromisoformat(day)
    except (TypeError, ValueError):
        return False
    return req.start <= d <= req.end


def finish(req: c.AnalyticsRequest, rows: list[dict[str, Any]], body: Any) -> c.AnalyticsResult:
    """Rows for asked metrics only; every asked metric with no row is missing."""
    rows = [r for r in rows if r["metric"] in req.metrics and in_window(r["date"], req)]
    seen = {r["metric"] for r in rows}
    missing = tuple(m for m in req.metrics if m not in seen)
    return c.AnalyticsResult(rows=tuple(rows), missing=missing, raw_ref=raw_ref(body))


class AnalyticsHttpProvider:
    """Five calls for an HTTP analytics source. Subclasses set the constants and `_pull`."""

    capability: ClassVar[Capability] = Capability.ANALYTICS
    name: ClassVar[str]
    mode: ClassVar[ConnectionMode] = ConnectionMode.HOSTED
    origins: ClassVar[tuple[Origin, ...]]
    settings_model: ClassVar[type[BaseModel]]
    metrics_known: ClassVar[tuple[str, ...]]
    licence_note: ClassVar[str] = ""

    def __init__(self, ctx: ProviderContext, *, transport: Transport | None = None) -> None:
        self.ctx = ctx
        self.clock = ctx.clock
        self.transport = transport or PostizTransport()
        try:
            self.settings: Any = self.settings_model.model_validate(ctx.connection.settings)
        except ValidationError:
            self.settings = None

    # ------------------------------------------------------------------ guards
    def problem(self) -> str | None:
        if self.settings is None:
            return f"the connection settings do not match the {self.name} schema"
        if self.ctx.connection.secret_ref is None or self.ctx.vault is None:
            return "no credential is stored for this connection"
        return None

    def _timeout(self) -> float:
        return getattr(self.settings, "timeout_s", DEFAULT_TIMEOUT_S) if self.settings else DEFAULT_TIMEOUT_S

    def _call(self, call: RawCall) -> RawReply:
        """The one way out. A call to any origin but this provider's own is refused here."""
        try:
            ok = parse_origin(call.url) in self.origins
        except ValueError:
            ok = False
        if not ok:
            raise TransportError("refused a call to an endpoint this provider does not use", reached=False)
        return self.transport(call)

    def _json(self, call: RawCall, what: str) -> Any:
        try:
            reply = self._call(call)
        except TransportError as exc:
            raise PullFailed(f"{what}: {exc}") from None
        if not 200 <= reply.status < 300:
            raise PullFailed(f"{what} answered HTTP {status_class(reply.status)}")
        return reply.body

    def _secret(self):
        secret = self.ctx.secret()
        if secret is None or not secret:
            raise PullFailed("the vault returned no credential for this connection")
        return secret

    def _header_auth(self, header: str, scheme: str) -> Auth:
        secret = self._secret()
        try:
            check_header_secret(secret)
        except ConnectionsError:
            raise PullFailed("the stored credential cannot be sent as a header; store it again") from None
        return Auth(header, secret, scheme)

    def _check_tenant(self, request: c.Request) -> None:
        if request.tenant_id != self.ctx.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")

    # ------------------------------------------------------------------ the five calls
    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=self.name, mode=self.mode,
                              limits={"configured": self.problem() is None, "metrics": list(self.metrics_known)},
                              licence_note=self.licence_note, sends_externally=False)

    def health(self) -> c.Health:
        issue = self.problem()
        return c.Health(connected=issue is None, checked_at=self.clock.now(), detail=issue)

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        self._check_tenant(request)
        if (issue := self.problem()) is not None:
            raise ProviderUnavailable(f"the {self.name} connection is not configured: {issue}")
        return c.Estimate(cost_eur=Decimal(0), seconds=3.0)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)
        self._check_tenant(request)
        c.check_cap(self.estimate(request), request)
        assert isinstance(request, c.AnalyticsRequest)
        output = self._pull(request)
        return c.RunResult(output=output, cost_eur=Decimal(0),
                           provenance=c.Provenance(made_by="provider", provider=self.name, mode=self.mode,
                                                   made_at=self.clock.now()))

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name=f"mock-{self.name}")

    def _pull(self, req: c.AnalyticsRequest) -> c.AnalyticsResult:
        raise NotImplementedError


__all__ = ["AnalyticsHttpProvider", "PullFailed", "RawCall", "RawReply", "TransportError", "finish", "in_window",
           "number", "raw_ref"]
