"""Shared code for the two Postiz providers (hosted and self-hosted). No `register` here.

Public API, from docs.postiz.com (read 15 Nov 2026; the test fixtures are written by hand from
those pages, not captured from a live server):

    auth     header `Authorization: <api key>` (no "Bearer")
    POST     {base}/upload          multipart/form-data, field `file`; reply {id, path, ...}
    POST     {base}/posts           {type: now|schedule|draft, date, shortLink, tags, posts: [
                                      {integration: {id}, value: [{content, image: [{id, path}]}],
                                       settings: {__type}}]}; reply [{postId, integration}]
    GET      {base}/posts?startDate=&endDate=   reply {posts: [{id, content, publishDate,
                                      releaseURL, state: QUEUE|PUBLISHED|ERROR|DRAFT, integration: {id}}]}
    hosted base       https://api.postiz.com/public/v1
    self-hosted base  https://<your domain>/api/public/v1

Postiz has no idempotency key. So a send whose outcome is unknown is never resent: the
publisher reconciles through `run(ReadbackRequest)` without an external id, which lists the posts
around the requested date and matches this connection's integration and the exact canonical text.

Where the key may go. Connection settings are tenant-writable, so they hold only the channel to
integration map and a timeout: no URL, no host, no path. The hosted origin is a constant in code.
The self-hosted origin comes only from the operator's environment, THALLO_POSTIZ_SELF_HOSTED_ORIGIN
(one origin, `https://host[:port]`, no path, parsed strictly by `parse_origin`; an exact
(scheme, host, port) match is required for every call). Every call goes through `PostizTransport`:
https only, the host resolved once, every address must be public, the socket pinned to the
checked address, redirects refused, and the key revealed into the header only after the
connection to that address is open. A provider that is not configured raises
`ProviderUnavailable` from `estimate()` and `run()` before anything is built, so the registry's
chain falls through to the manual pack.

Bytes. The provider takes media only from the publisher's `SendContext` (publish/context.py) and
hashes the exact object it puts in each upload body against the request's media hashes. The text
in the post body is the request text, which the context checked against the approved text.

What each failure means, so the publisher never double posts:
  before the create-post request leaves (no secret, a refused address, a failed connect, an
  upload refused or lost: an orphan upload is not a post)     NotSent(retryable)
  create-post answered 429                                    NotSent(retryable=True)
  create-post answered another 4xx                            NotSent(retryable=False)
  create-post timed out, was reset, answered 5xx or 3xx, or
  answered 2xx without a post id                              OutcomeUnknown
"""
from __future__ import annotations

import http.client
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from thallo.connections.errors import ConnectionsError, InvalidConnection, ProviderUnavailable
from thallo.connections.registry import ProviderContext
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, Channel, ConnectionMode
from thallo.contracts.testing.mocks import MockProvider
from thallo.gate.canonical import canonical_text
from thallo.providers.text.base import (
    MAX_BODY_BYTES,
    AddressPolicy,
    Auth,
    ConnectionFactory,
    Origin,
    Resolver,
    address_allowed,
    check_header_secret,
    parse_origin,
    pinned_connection,
    secret_header_safe,
    status_class,
    system_resolver,
)
from thallo.publish.context import current
from thallo.publish.errors import LookupFailed, NotSent, OutcomeUnknown

DEFAULT_TIMEOUT_S = 30.0
LOOKUP_WINDOW = timedelta(days=1)
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "video/mp4": "mp4",
        "application/pdf": "pdf"}


# ====================================================================== settings

class PostizIntegration(BaseModel):
    """One Postiz channel: its integration id and its provider identifier (`settings.__type`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    provider: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")


class PostizSettings(BaseModel):
    """Tenant-writable: no URL, host, path or binary may appear here."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    integrations: dict[Channel, PostizIntegration] = Field(default_factory=dict)
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, gt=0, le=120)


# ====================================================================== transport

class TransportError(ConnectionsError):
    """`reached` False: nothing left this machine for this call. True: the request may have been
    received. Messages carry an error type or a status class, never a body or an address."""

    def __init__(self, message: str, *, reached: bool) -> None:
        super().__init__(message)
        self.reached = reached


@dataclass(frozen=True)
class RawCall:
    method: str
    url: str
    body: bytes | None = field(default=None, repr=False)
    content_type: str | None = None
    auth: Auth | None = None
    timeout: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class RawReply:
    status: int
    body: Any


class PostizTransport:
    """stdlib http.client with the text providers' guards (thallo/providers/text/base.py), for a
    raw body: https only, one resolution, public addresses only, the socket pinned to the checked
    address (TLS still checks the host name), no redirects. The connection is opened before the
    secret is revealed, so a refused or unreachable address never sees it."""

    def __init__(self, *, resolver: Resolver = system_resolver, connect: ConnectionFactory = pinned_connection) -> None:
        self.resolver = resolver
        self.connect = connect

    def __call__(self, call: RawCall) -> RawReply:
        if call.auth is not None and not secret_header_safe(call.auth.secret):
            raise TransportError("the stored secret cannot be sent as a header", reached=False)
        parts = urlsplit(call.url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise TransportError("the endpoint URL is not allowed", reached=False)
        host = parts.hostname
        try:
            port = parts.port or 443
        except ValueError:
            raise TransportError("the endpoint URL is not allowed", reached=False) from None
        try:
            ips = list(self.resolver(host, port))
        except ConnectionsError:
            raise TransportError("the endpoint's host name did not resolve", reached=False) from None
        if not ips or not all(address_allowed(ip, AddressPolicy.PUBLIC) for ip in ips):
            raise TransportError("the endpoint resolves to an address that is not public", reached=False)
        conn = self.connect("https", host, port, ips[0], call.timeout)
        try:
            conn.connect()
        except (OSError, http.client.HTTPException, ValueError) as exc:
            conn.close()
            raise TransportError(f"could not connect ({type(exc).__name__})", reached=False) from None
        headers = {"accept": "application/json"}
        if call.content_type:
            headers["content-type"] = call.content_type
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        failed: str | None = None
        status, raw = 0, b""
        try:
            if call.auth is not None:
                headers[call.auth.header] = call.auth.scheme + call.auth.secret.reveal()
            conn.request(call.method, path, body=call.body, headers=headers)
            resp = conn.getresponse()
            status, raw = resp.status, resp.read(MAX_BODY_BYTES + 1)
        except (OSError, http.client.HTTPException, ValueError, TypeError) as exc:
            failed = type(exc).__name__  # the original may quote header bytes: dropped
        finally:
            if call.auth is not None:
                headers.pop(call.auth.header, None)
            conn.close()
        if failed is not None:
            raise TransportError(f"the exchange failed ({failed})", reached=True)
        if len(raw) > MAX_BODY_BYTES:
            raise TransportError("the answer is too large", reached=True)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        return RawReply(status, body)


Transport = Any  # a callable RawCall -> RawReply


def multipart(field_name: str, filename: str, mime: str, data: bytes) -> tuple[bytes, str]:
    """One file part. `data` goes into the body as is; the caller hashed this same object."""
    boundary = "thallo-" + secrets.token_hex(16)
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; filename=\"{filename}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return head + data + tail, f"multipart/form-data; boundary={boundary}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ====================================================================== the provider

class PostizProvider:
    capability: ClassVar[Capability] = Capability.PUBLISHING
    name: ClassVar[str]
    mode: ClassVar[ConnectionMode] = ConnectionMode.HOSTED
    api_path: ClassVar[str]

    def __init__(self, ctx: ProviderContext, *, transport: Transport | None = None,
                 environ: Mapping[str, str] | None = None) -> None:
        self.ctx = ctx
        self.clock = ctx.clock
        self.environ = environ
        self.transport = transport or PostizTransport()
        try:
            self.settings: PostizSettings | None = PostizSettings.model_validate(ctx.connection.settings)
        except ValidationError:
            self.settings = None

    # ------------------------------------------------------------------ where calls may go
    def origin(self) -> Origin:
        """The one origin this provider may call; raises ValueError when there is none."""
        raise NotImplementedError

    def base(self) -> str:
        scheme, host, port = self.origin()
        shown = f"[{host}]" if ":" in host else host
        return f"{scheme}://{shown}" + ("" if port == 443 else f":{port}") + self.api_path

    def problem(self) -> str | None:
        if self.settings is None:
            return "the connection settings do not match the Postiz schema"
        if not self.settings.integrations:
            return "no Postiz integration is mapped to a channel on this connection"
        if self.ctx.connection.secret_ref is None or self.ctx.vault is None:
            return "no API key is stored for this connection"
        try:
            self.origin()
        except ValueError as exc:
            return str(exc)
        return None

    def _call(self, call: RawCall) -> RawReply:
        """The one way out. A call to any origin but this provider's one is refused here."""
        try:
            ok = parse_origin(call.url) == self.origin()
        except ValueError:
            ok = False
        if not ok:
            raise TransportError("refused a call to an endpoint the operator did not allow", reached=False)
        return self.transport(call)

    def _auth(self) -> Auth:
        secret = self.ctx.secret()
        if secret is None or not secret:
            raise NotSent("the vault returned no API key for this connection", retryable=False)
        try:
            check_header_secret(secret)
        except ConnectionsError:
            raise NotSent("the stored API key cannot be sent as a header; store it again", retryable=False) from None
        return Auth("authorization", secret, "")

    def _check_tenant(self, request: c.Request) -> None:
        if request.tenant_id != self.ctx.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")

    def _timeout(self) -> float:
        return self.settings.timeout_s if self.settings else DEFAULT_TIMEOUT_S

    # ------------------------------------------------------------------ the five calls
    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=self.name, mode=self.mode,
                              limits={"configured": self.problem() is None, "idempotency_key": False},
                              licence_note="Postiz terms; posts go to the connected social accounts",
                              sends_externally=True)

    def health(self) -> c.Health:
        now = self.clock.now()
        if (issue := self.problem()) is not None:
            return c.Health(connected=False, checked_at=now, detail=issue)
        try:
            reply = self._call(RawCall("GET", f"{self.base()}/integrations", auth=self._auth(), timeout=15.0))
        except ConnectionsError as exc:
            return c.Health(connected=False, checked_at=now, detail=type(exc).__name__)
        ok = 200 <= reply.status < 300
        return c.Health(connected=ok, checked_at=now,
                        detail=None if ok else f"Postiz answered HTTP {status_class(reply.status)}")

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        self._check_tenant(request)
        if (issue := self.problem()) is not None:
            raise ProviderUnavailable(f"the {self.name} connection is not configured: {issue}")
        assert self.settings is not None
        if isinstance(request, c.PublishRequest) and request.channel not in self.settings.integrations:
            raise ProviderUnavailable(f"no Postiz integration for {request.channel.value} on this connection")
        return c.Estimate(cost_eur=Decimal(0), seconds=2.0 + 2.0 * len(getattr(request, "media_sha256", ())))

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)
        self._check_tenant(request)
        c.check_cap(self.estimate(request), request)  # ProviderUnavailable when not configured
        if isinstance(request, c.PublishRequest):
            output, ids = self._publish(request)
        else:
            assert isinstance(request, c.ReadbackRequest)
            output, ids = self._readback(request)
        return c.RunResult(output=output, cost_eur=Decimal(0),
                           provenance=c.Provenance(made_by="provider", provider=self.name, mode=self.mode,
                                                   made_at=self.clock.now()),
                           external_ids=ids)

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name=f"mock-{self.name}")

    # ------------------------------------------------------------------ publish
    def _publish(self, req: c.PublishRequest) -> tuple[c.PublishResult, dict[str, str]]:
        ctx = current()
        if ctx is None:
            raise NotSent("no staged send: only the publisher may send through Postiz", retryable=False)
        ctx.verify(req)
        assert self.settings is not None
        integ = self.settings.integrations[req.channel]
        auth = self._auth()
        images: list[dict[str, str]] = []
        for i, sha in enumerate(req.media_sha256):
            data = ctx.media_for(i, sha)  # hashed again: this exact object goes into the body
            mime = ctx.media[i].mime
            body, ctype = multipart("file", f"{sha[:16]}.{_EXT.get(mime, 'bin')}", mime, data)
            try:
                reply = self._call(RawCall("POST", f"{self.base()}/upload", body, ctype, auth, self._timeout()))
            except TransportError as exc:
                raise NotSent(f"media upload failed: {exc}", retryable=True) from None
            if not 200 <= reply.status < 300:
                raise NotSent(f"media upload answered HTTP {status_class(reply.status)}",
                              retryable=reply.status == 429 or reply.status >= 500)
            b = reply.body
            if not (isinstance(b, dict) and isinstance(b.get("id"), str) and isinstance(b.get("path"), str)):
                raise NotSent("media upload answered without an id and a path", retryable=True)
            images.append({"id": b["id"], "path": b["path"]})
        now = self.clock.now()
        later = req.scheduled_for is not None and req.scheduled_for > now
        when = req.scheduled_for if later and req.scheduled_for is not None else ctx.requested_at
        post = {
            "type": "schedule" if later else "now", "date": _iso(when), "shortLink": False, "tags": [],
            "posts": [{"integration": {"id": integ.id}, "value": [{"content": req.text, "image": images}],
                       "settings": {"__type": integ.provider}}],
        }
        body = json.dumps(post, ensure_ascii=False).encode("utf-8")
        try:
            reply = self._call(RawCall("POST", f"{self.base()}/posts", body, "application/json", auth, self._timeout()))
        except TransportError as exc:
            if exc.reached:
                raise OutcomeUnknown(f"create post: {exc}") from None
            raise NotSent(f"create post: {exc}", retryable=True) from None
        if reply.status == 429:
            raise NotSent("Postiz rate limit (HTTP 429): no post was created", retryable=True)
        if 400 <= reply.status < 500:
            raise NotSent(f"Postiz refused the post (HTTP {status_class(reply.status)})", retryable=False)
        if not 200 <= reply.status < 300:
            raise OutcomeUnknown(f"Postiz answered HTTP {status_class(reply.status)}; the post may exist")
        b = reply.body
        first = b[0] if isinstance(b, list) and b else b
        post_id = first.get("postId") if isinstance(first, dict) else None
        if not isinstance(post_id, str) or not post_id:
            raise OutcomeUnknown("Postiz accepted the post but returned no post id")
        status = "scheduled" if later else "published"
        return c.PublishResult(status=status, external_id=post_id), {"post_id": post_id}

    # ------------------------------------------------------------------ readback and reconcile
    def _readback(self, req: c.ReadbackRequest) -> tuple[c.ReadbackResult, dict[str, str]]:
        """With an external id: that post. Without one (reconcile): the one post of this
        connection's integration for the channel whose canonical text is the staged text, in a
        window around the requested date. Two matches are refused (`LookupFailed`), never guessed."""
        ctx = current()
        if ctx is None:
            raise LookupFailed("no staged lookup: the publisher supplies the text and the date")
        if req.tenant_id != ctx.tenant_id or req.idempotency_key != ctx.idempotency_key:
            raise LookupFailed("the lookup is not the one the publisher staged")
        assert self.settings is not None
        integ = self.settings.integrations.get(Channel(ctx.channel))
        if integ is None:
            raise LookupFailed("no Postiz integration for the channel on this connection")
        query = urlencode({"startDate": _iso(ctx.requested_at - LOOKUP_WINDOW),
                           "endDate": _iso(ctx.requested_at + LOOKUP_WINDOW)})
        try:
            reply = self._call(RawCall("GET", f"{self.base()}/posts?{query}", auth=self._auth(),
                                       timeout=self._timeout()))
        except (TransportError, NotSent) as exc:
            raise LookupFailed(f"listing posts failed: {exc}") from None
        if not 200 <= reply.status < 300:
            raise LookupFailed(f"listing posts answered HTTP {status_class(reply.status)}")
        posts = reply.body.get("posts") if isinstance(reply.body, dict) else None
        if not isinstance(posts, list):
            raise LookupFailed("listing posts answered an unexpected shape")
        posts = [p for p in posts if isinstance(p, dict)]
        if req.external_id is not None:
            match = [p for p in posts if p.get("id") == req.external_id]
        else:
            want = canonical_text(ctx.text)
            match = [p for p in posts if isinstance(p.get("integration"), dict)
                     and p["integration"].get("id") == integ.id
                     and isinstance(p.get("content"), str) and canonical_text(p["content"]) == want]
        if len(match) > 1:
            raise LookupFailed("more than one post matches; a person must check")
        if not match:
            return c.ReadbackResult(live_text=None, media_changed=None), {}
        p = match[0]
        state = p.get("state") if isinstance(p.get("state"), str) else "UNKNOWN"
        ids = {"external_id": str(p.get("id")), "state": state}
        if isinstance(p.get("releaseURL"), str) and p["releaseURL"]:
            ids["live_url"] = p["releaseURL"]
        # Postiz's own copy of the text; live only once Postiz says PUBLISHED. Media cannot be
        # compared through this API, so media_changed is unknown (None), never False.
        live = p.get("content") if state == "PUBLISHED" and isinstance(p.get("content"), str) else None
        return c.ReadbackResult(live_text=live, media_changed=None), ids


__all__ = ["PostizIntegration", "PostizProvider", "PostizSettings", "PostizTransport", "RawCall", "RawReply",
           "TransportError", "multipart"]
