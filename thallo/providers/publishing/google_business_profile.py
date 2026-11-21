"""Google Business Profile posts (WP-26). Text posts only; everything else falls to the manual pack.

API, from developers.google.com/my-business/reference/rest/v4 (read 20 Nov 2026; the fixtures in
tests/wp26_gbp are written by hand from those pages, not captured from a live account):

    POST {base}/accounts/{a}/locations/{l}/localPosts   body LocalPost {languageCode, summary,
                                                        topicType: STANDARD}; reply the LocalPost
    GET  {base}/{name}                                  one LocalPost {name, summary, state:
                                                        LIVE|PROCESSING|REJECTED, createTime, searchUrl}
    GET  {base}/accounts/{a}/locations/{l}/localPosts?pageSize=100   reply {localPosts, nextPageToken}
    base https://mybusiness.googleapis.com/v4

What this provider does NOT do, by design:
- No review reply, no answer, no profile patch: it accepts only `PublishRequest` and
  `ReadbackRequest` (the publishing contract), and builds only the three calls above. Replies and
  profile changes are drafts a person pastes (thallo/extensions/gbp).
- No photos. LocalPost media take only a public `sourceUrl`, and the photo upload flow was not
  verified against the docs, so a request with media raises `ProviderUnavailable` from
  `estimate()` and the publisher's chain falls through to the manual pack.
- No channel but google_business_profile (same: `ProviderUnavailable`).

Auth as in thallo/providers/analytics/google_base.py: the vault holds {"client_id",
"client_secret", "refresh_token"} for an OAuth client with the business.manage scope. The stored
credential goes only to oauth2.googleapis.com, the short-lived access token only to
mybusiness.googleapis.com. Both origins are constants; settings hold pattern-checked ids only.

Failure mapping, so the publisher never double posts: anything before the create request leaves
(token exchange included) is NotSent(retryable); a 429 is NotSent(retryable); another 4xx is
NotSent(final); a timeout or reset after the bytes left, a 5xx or 3xx, or a 2xx without a post
name is OutcomeUnknown. Google has no idempotency key here, so reconcile matches the exact
canonical text created at or after the send.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from thallo.connections.errors import ConnectionsError, InvalidConnection, ProviderUnavailable
from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.connections.secret import Secret
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, Channel, ConnectionMode
from thallo.contracts.testing.mocks import MockProvider
from thallo.gate.canonical import canonical_text
from thallo.providers.publishing.postiz_base import PostizTransport, RawCall, RawReply, TransportError
from thallo.providers.text.base import Auth, check_header_secret, parse_origin, status_class
from thallo.publish.context import current
from thallo.publish.errors import AmbiguousLookup, LookupFailed, NotSent, OutcomeUnknown

NAME = "google_business_profile"
API_ORIGIN = ("https", "mybusiness.googleapis.com", 443)
API_BASE = "https://mybusiness.googleapis.com/v4"
TOKEN_ORIGIN = ("https", "oauth2.googleapis.com", 443)
TOKEN_URL = "https://oauth2.googleapis.com/token"
POST_MAX_CHARS = 1500
DEFAULT_TIMEOUT_S = 30.0
NAME_RE = r"^accounts/[0-9]{1,30}/locations/[0-9]{1,30}/localPosts/[A-Za-z0-9_-]{1,200}$"

Transport = Any  # a callable RawCall -> RawReply


class GbpSettings(BaseModel):
    """Tenant-writable: ids only, no URL, host or path."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: str = Field(pattern=r"^[0-9]{1,30}$")
    location_id: str = Field(pattern=r"^[0-9]{1,30}$")
    language_code: str = Field(default="it", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, gt=0, le=120)


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


class GbpProvider:
    capability: ClassVar[Capability] = Capability.PUBLISHING
    name: ClassVar[str] = NAME
    mode: ClassVar[ConnectionMode] = ConnectionMode.HOSTED

    def __init__(self, ctx: ProviderContext, *, transport: Transport | None = None) -> None:
        self.ctx = ctx
        self.clock = ctx.clock
        self.transport = transport or PostizTransport()
        try:
            self.settings: GbpSettings | None = GbpSettings.model_validate(ctx.connection.settings)
        except ValidationError:
            self.settings = None

    # ------------------------------------------------------------------ guards
    def problem(self) -> str | None:
        if self.settings is None:
            return "the connection settings need an account id and a location id"
        if self.ctx.connection.secret_ref is None or self.ctx.vault is None:
            return "no Google credential is stored for this connection"
        return None

    def _parent(self) -> str:
        assert self.settings is not None
        return f"accounts/{self.settings.account_id}/locations/{self.settings.location_id}"

    def _timeout(self) -> float:
        return self.settings.timeout_s if self.settings else DEFAULT_TIMEOUT_S

    def _call(self, call: RawCall) -> RawReply:
        """The one way out: only the two constant origins, and the credential only to the token one."""
        try:
            origin = parse_origin(call.url)
        except ValueError:
            origin = None
        if origin not in (API_ORIGIN, TOKEN_ORIGIN) or (origin == TOKEN_ORIGIN and call.url != TOKEN_URL):
            raise TransportError("refused a call to an endpoint the provider does not allow", reached=False)
        if origin == TOKEN_ORIGIN and call.auth is not None:
            raise TransportError("the token exchange carries no header secret", reached=False)
        return self.transport(call)

    def _bearer(self) -> Auth:
        """Exchange the stored refresh credential for an access token. Every failure is NotSent:
        nothing has gone to the API yet."""
        secret = self.ctx.secret()
        if secret is None or not secret:
            raise NotSent("the vault returned no Google credential for this connection", retryable=False)
        try:
            cred = json.loads(secret.reveal())
        except ValueError:
            raise NotSent("the stored Google credential is not JSON; store it again", retryable=False) from None
        keys = ("client_id", "client_secret", "refresh_token")
        if not isinstance(cred, dict) or not all(isinstance(cred.get(k), str) and cred[k] for k in keys):
            raise NotSent("the stored Google credential lacks client_id, client_secret or refresh_token",
                          retryable=False)
        form = urlencode({"grant_type": "refresh_token", **{k: cred[k] for k in keys}}).encode()
        try:
            reply = self._call(RawCall("POST", TOKEN_URL, form, "application/x-www-form-urlencoded",
                                       timeout=self._timeout()))
        except TransportError as exc:
            raise NotSent(f"the Google token exchange failed: {exc}", retryable=True) from None
        if not 200 <= reply.status < 300:
            raise NotSent(f"the Google token exchange answered HTTP {status_class(reply.status)}",
                          retryable=reply.status == 429 or reply.status >= 500)
        token = reply.body.get("access_token") if isinstance(reply.body, dict) else None
        if not isinstance(token, str) or not token:
            raise NotSent("the Google token exchange returned no access token", retryable=True)
        access = Secret(token)
        try:
            check_header_secret(access)
        except ConnectionsError:
            raise NotSent("the Google access token cannot be sent as a header", retryable=False) from None
        return Auth("authorization", access, "Bearer ")

    def _check_tenant(self, request: c.Request) -> None:
        if request.tenant_id != self.ctx.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")

    # ------------------------------------------------------------------ the five calls
    def describe(self) -> c.ProviderInfo:
        return c.ProviderInfo(capability=self.capability, name=self.name, mode=self.mode,
                              limits={"configured": self.problem() is None, "idempotency_key": False,
                                      "text_max_chars": POST_MAX_CHARS, "media": False,
                                      "channels": [Channel.GOOGLE_BUSINESS_PROFILE.value]},
                              licence_note="Google Business Profile API terms; posts appear on the public listing",
                              sends_externally=True)

    def health(self) -> c.Health:
        now = self.clock.now()
        if (issue := self.problem()) is not None:
            return c.Health(connected=False, checked_at=now, detail=issue)
        try:
            auth = self._bearer()
            reply = self._call(RawCall("GET", f"{API_BASE}/{self._parent()}/localPosts?pageSize=1", auth=auth,
                                       timeout=15.0))
        except (ConnectionsError, NotSent) as exc:
            return c.Health(connected=False, checked_at=now, detail=type(exc).__name__)
        ok = 200 <= reply.status < 300
        return c.Health(connected=ok, checked_at=now,
                        detail=None if ok else f"Google answered HTTP {status_class(reply.status)}")

    def estimate(self, request: c.Request) -> c.Estimate:
        c.check_request(self, request)
        self._check_tenant(request)
        if (issue := self.problem()) is not None:
            raise ProviderUnavailable(f"the {self.name} connection is not configured: {issue}")
        if isinstance(request, c.PublishRequest):
            if request.channel is not Channel.GOOGLE_BUSINESS_PROFILE:
                raise ProviderUnavailable(f"{self.name} posts only to google_business_profile")
            if request.media_sha256:
                raise ProviderUnavailable("photos go through the manual pack; this provider posts text only")
            if not request.text.strip():
                raise ProviderUnavailable("a text post needs text")
            if len(request.text) > POST_MAX_CHARS:
                raise ProviderUnavailable(f"the post is over {POST_MAX_CHARS} characters")
        return c.Estimate(cost_eur=Decimal(0), seconds=2.0)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)
        self._check_tenant(request)
        c.check_cap(self.estimate(request), request)
        if isinstance(request, c.PublishRequest):
            output, ids = self._publish(request)
        else:
            assert isinstance(request, c.ReadbackRequest)
            output, ids = self._readback(request)
        return c.RunResult(output=output, cost_eur=Decimal(0), external_ids=ids,
                           provenance=c.Provenance(made_by="provider", provider=self.name, mode=self.mode,
                                                   made_at=self.clock.now()))

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name=f"mock-{self.name}")

    # ------------------------------------------------------------------ publish
    def _publish(self, req: c.PublishRequest) -> tuple[c.PublishResult, dict[str, str]]:
        ctx = current()
        if ctx is None:
            raise NotSent("no staged send: only the publisher may send to the Business Profile", retryable=False)
        ctx.verify(req)
        assert self.settings is not None
        if req.scheduled_for is not None and req.scheduled_for > self.clock.now():
            raise NotSent("the Business Profile API posts now; the publisher sends at the slot time", retryable=True)
        auth = self._bearer()
        body = json.dumps({"languageCode": self.settings.language_code, "summary": req.text,
                           "topicType": "STANDARD"}, ensure_ascii=False).encode("utf-8")
        try:
            reply = self._call(RawCall("POST", f"{API_BASE}/{self._parent()}/localPosts", body, "application/json",
                                       auth, self._timeout()))
        except TransportError as exc:
            if exc.reached:
                raise OutcomeUnknown(f"create post: {exc}") from None
            raise NotSent(f"create post: {exc}", retryable=True) from None
        if reply.status == 429:
            raise NotSent("Google rate limit (HTTP 429): no post was created", retryable=True)
        if 400 <= reply.status < 500:
            raise NotSent(f"Google refused the post (HTTP {status_class(reply.status)})", retryable=False)
        if not 200 <= reply.status < 300:
            raise OutcomeUnknown(f"Google answered HTTP {status_class(reply.status)}; the post may exist")
        b = reply.body if isinstance(reply.body, dict) else {}
        name = b.get("name")
        if not isinstance(name, str) or not name.startswith(self._parent() + "/localPosts/"):
            raise OutcomeUnknown("Google accepted the post but returned no post name")
        ids = {"post_id": name}
        url = b.get("searchUrl")
        if isinstance(url, str) and url.startswith("https://"):
            ids["live_url"] = url
        return c.PublishResult(status="published", external_id=name, live_url=ids.get("live_url")), ids

    # ------------------------------------------------------------------ readback and reconcile
    def _list(self, auth: Auth) -> list[dict[str, Any]]:
        try:
            reply = self._call(RawCall("GET", f"{API_BASE}/{self._parent()}/localPosts?pageSize=100", auth=auth,
                                       timeout=self._timeout()))
        except TransportError as exc:
            raise LookupFailed(f"listing posts failed: {exc}") from None
        if not 200 <= reply.status < 300:
            raise LookupFailed(f"listing posts answered HTTP {status_class(reply.status)}")
        posts = reply.body.get("localPosts", []) if isinstance(reply.body, dict) else None
        if not isinstance(posts, list):
            raise LookupFailed("listing posts answered an unexpected shape")
        return [p for p in posts if isinstance(p, dict)]

    def _readback(self, req: c.ReadbackRequest) -> tuple[c.ReadbackResult, dict[str, str]]:
        ctx = current()
        if ctx is None:
            raise LookupFailed("no staged lookup: the publisher supplies the text and the date")
        if req.tenant_id != ctx.tenant_id or req.idempotency_key != ctx.idempotency_key:
            raise LookupFailed("the lookup is not the one the publisher staged")
        try:
            auth = self._bearer()
        except NotSent as exc:
            raise LookupFailed(str(exc)) from None
        if req.external_id is not None:
            if not re.fullmatch(NAME_RE, req.external_id) or not req.external_id.startswith(self._parent() + "/"):
                raise LookupFailed("the external id is not a post of this location")
            try:
                reply = self._call(RawCall("GET", f"{API_BASE}/{req.external_id}", auth=auth, timeout=self._timeout()))
            except TransportError as exc:
                raise LookupFailed(f"reading the post failed: {exc}") from None
            if reply.status == 404:
                return c.ReadbackResult(live_text=None, media_changed=None), {}
            if not 200 <= reply.status < 300 or not isinstance(reply.body, dict):
                raise LookupFailed(f"reading the post answered HTTP {status_class(reply.status)}")
            match = [reply.body]
        else:
            want = canonical_text(ctx.text)
            since = ctx.requested_at.astimezone(UTC).replace(microsecond=0)
            match = [p for p in self._list(auth)
                     if isinstance(p.get("summary"), str) and canonical_text(p["summary"]) == want
                     and str(p.get("name")) not in ctx.taken_external_ids
                     and (_when(p.get("createTime")) or since.replace(year=1970)) >= since]
        if len(match) > 1:
            raise AmbiguousLookup("more than one post matches; a person must check")
        if not match:
            return c.ReadbackResult(live_text=None, media_changed=None), {}
        p = match[0]
        state = p.get("state") if isinstance(p.get("state"), str) else "UNKNOWN"
        ids = {"external_id": str(p.get("name")), "state": state}
        if isinstance(p.get("searchUrl"), str) and p["searchUrl"].startswith("https://"):
            ids["live_url"] = p["searchUrl"]
        live = p.get("summary") if state == "LIVE" and isinstance(p.get("summary"), str) else None
        return c.ReadbackResult(live_text=live, media_changed=None), ids


def create(ctx: ProviderContext, transport: Transport | None = None) -> GbpProvider:
    return GbpProvider(ctx, transport=transport)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.PUBLISHING, NAME, ConnectionMode.HOSTED, create, settings_schema=GbpSettings)


__all__ = ["API_BASE", "NAME", "GbpProvider", "GbpSettings", "create", "register"]
