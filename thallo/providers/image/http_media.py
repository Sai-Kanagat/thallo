"""Shared machinery for the HTTP media providers: generated images and video, and stock photos.

No `register()` here: discovery skips this module. What it holds:

- `BytesTransport`: the transcription transport's rules (SSRF policy and pinned address from
  `providers/text/base.py`, no redirects, the secret only in the header while the call is made,
  errors that carry a type name and never a body or the secret), but it keeps the raw reply bytes
  too, because an image or a clip comes back as bytes.
- `HttpMediaProvider`: the five calls around one vendor shape. Configuration is operator only:
  the endpoint origins (hosted) or the loopback URL (local) come from the environment, the
  secret from the vault, the output folder is `<THALLO_BRAND_ROOT>/<tenant>/renders`. Tenant
  settings carry only prices and a timeout.
- The generation switch: a provider with `generates = True` is unavailable unless the operator
  set THALLO_ALLOW_GENERATED_MEDIA=1. Its files are GENERATED (`ingest_provenance`), carry a
  disclosure label and are FLAGged downstream.
- Generated people: unless the request allows people (which needs a consent record, checked
  against the consent table at ingest), the prompt sent carries a fixed instruction that the
  picture shows no people, faces, products, logos, premises or text. A generated image is never
  a picture of the product.

Request and reply shapes follow the vendors' public docs as of Nov 2026; the test fixtures are
written by hand from them, not captured.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from thallo.connections.errors import ConnectionsError, InvalidConnection, ProviderUnavailable
from thallo.connections.registry import ProviderContext
from thallo.connections.secret import Secret
from thallo.contracts import connection as c
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.contracts.testing.mocks import MockProvider
from thallo.providers.image.template_render import (
    BrandMisconfigured,
    RenderOutputError,
    private_dir,
    read_sha256,
    tenant_paths,
)
from thallo.providers.text.base import (
    MODEL_ID,
    AddressPolicy,
    Auth,
    ConnectionFactory,
    Origin,
    Resolver,
    address_allowed,
    check_header_secret,
    check_local,
    parse_origin,
    parse_origin_list,
    pinned_connection,
    status_class,
    system_resolver,
    to_eur,
)

SWITCH_ENV = "THALLO_ALLOW_GENERATED_MEDIA"
MAX_MEDIA_BYTES = 60 * 1024 * 1024
NO_PEOPLE = ("Show no people, faces, hands, products, packaging, logos, shop fronts or premises, "
             "and no words, letters or numbers.")


class MediaProviderError(ConnectionsError):
    """The vendor failed or answered in an unexpected shape. Never carries a body or a secret."""


# ====================================================================== transport

@dataclass(frozen=True)
class MediaCall:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    auth: Auth | None = None
    timeout: float = 120.0


@dataclass(frozen=True)
class MediaReply:
    status: int
    body: Any  # parsed JSON, or None
    raw: bytes = b""
    content_type: str = ""


MediaTransportFn = Callable[[MediaCall], MediaReply]


class BytesTransport:
    def __init__(self, policy: AddressPolicy, *, resolver: Resolver = system_resolver,
                 connect: ConnectionFactory = pinned_connection) -> None:
        self.policy = policy
        self.resolver = resolver
        self.connect = connect

    def __call__(self, call: MediaCall) -> MediaReply:
        if call.auth is not None:
            check_header_secret(call.auth.secret)
        parts = urlsplit(call.url)
        allowed = ("https",) if self.policy is AddressPolicy.PUBLIC else ("http", "https")
        if parts.scheme not in allowed or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise MediaProviderError("the endpoint URL is not allowed")
        host = parts.hostname
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            raise MediaProviderError("the endpoint URL is not allowed") from None
        ips = list(self.resolver(host, port))
        if not ips or not all(address_allowed(ip, self.policy) for ip in ips):
            raise MediaProviderError(f"the endpoint resolves to an address outside the {self.policy.value} policy")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        headers = {"accept": "application/json, image/*, video/*", **call.headers}
        conn = self.connect(parts.scheme, host, port, ips[0], call.timeout)
        failed: str | None = None
        status, raw, ctype = 0, b"", ""
        try:
            if call.auth is not None:
                headers[call.auth.header] = call.auth.scheme + call.auth.secret.reveal()
            conn.request(call.method, path, body=call.body, headers=headers)
            resp = conn.getresponse()
            status, ctype = resp.status, (resp.getheader("content-type") or "").lower()
            raw = resp.read(MAX_MEDIA_BYTES + 1)
        except (OSError, http.client.HTTPException, ValueError, TypeError) as exc:
            failed = type(exc).__name__
        finally:
            if call.auth is not None:
                headers.pop(call.auth.header, None)
            conn.close()
        if failed is not None:
            raise MediaProviderError(f"could not reach the endpoint ({failed})")
        if 300 <= status < 400:
            raise MediaProviderError("the endpoint answered with a redirect (HTTP 3xx); redirects are refused")
        if len(raw) > MAX_MEDIA_BYTES:
            raise MediaProviderError("the endpoint's answer is too large")
        body = None
        if "json" in ctype or raw[:1] in (b"{", b"["):
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
        return MediaReply(status, body, raw, ctype)


# ====================================================================== settings

class MediaSettings(BaseModel):
    """Tenant-writable: a price and a timeout, never an endpoint, a path or a secret."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    usd_per_unit: Decimal = Field(default=Decimal(0), ge=0)  # per image, per second of video, per photo
    timeout_s: float = Field(default=120.0, gt=0, le=1800)


def generation_allowed(environ: Mapping[str, str]) -> bool:
    return environ.get(SWITCH_ENV) == "1"


def guarded_prompt(prompt: str, allow_people: bool) -> str:
    text = " ".join(prompt.split())
    return text if allow_people else f"{text}\n\n{NO_PEOPLE}"


def image_kind(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def write_output(environ: Mapping[str, str], tenant_id: str, data: bytes, ext: str) -> tuple[Path, str]:
    """Content-addressed write into the tenant's private renders folder, read back and re-hashed."""
    try:
        out = tenant_paths(tenant_id, environ).output
    except BrandMisconfigured as exc:
        raise ProviderUnavailable(f"no tenant folder for the output: {exc}") from None
    folder = private_dir(out)
    sha = hashlib.sha256(data).hexdigest()
    final = folder / f"{sha}.{ext}"
    if final.is_symlink():
        raise RenderOutputError("refusing to write over a symlink")
    tmp = folder / f".media-{sha}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, final)
    if read_sha256(final) != sha:
        final.unlink(missing_ok=True)
        raise RenderOutputError("the written file does not hash to the bytes received; removed")
    return final, sha


# ====================================================================== the base provider

class HttpMediaProvider:
    capability: ClassVar[Capability]
    name: ClassVar[str]
    mode: ClassVar[ConnectionMode] = ConnectionMode.HOSTED
    generates: ClassVar[bool] = True
    needs_secret: ClassVar[bool] = True
    needs_model: ClassVar[bool] = True
    sends_externally: ClassVar[bool] = True
    licence_note: ClassVar[str | None] = None
    address_policy: ClassVar[AddressPolicy] = AddressPolicy.PUBLIC
    origins_env: ClassVar[str] = ""  # hosted: comma-separated origins the operator allows
    local_url_env: ClassVar[str] = ""  # local: the loopback base URL
    Settings: ClassVar[type[MediaSettings]] = MediaSettings

    def __init__(self, ctx: ProviderContext, *, transport: MediaTransportFn | None = None,
                 environ: Mapping[str, str] | None = None) -> None:
        self.ctx = ctx
        self.clock = ctx.clock
        self.model = ctx.connection.model
        self.environ: Mapping[str, str] = dict(os.environ if environ is None else environ)
        self.transport: MediaTransportFn = transport or BytesTransport(self.address_policy)
        try:
            self.settings: MediaSettings | None = self.Settings.model_validate(ctx.connection.settings or {})
        except ValidationError:
            self.settings = None

    # ------------------------------------------------------------------ vendor specifics

    def serves(self, request: c.Request) -> str | None:
        """None when this provider can serve the request, else why not."""
        raise NotImplementedError

    def units(self, request: c.Request) -> Decimal:
        return Decimal(1)

    def produce(self, request: c.Request, auth: Auth | None) -> tuple[bytes, str, str, dict[str, Any], Decimal]:
        """(bytes, mime, ext, provenance settings, units billed)."""
        raise NotImplementedError

    def health_path(self) -> str:
        return "/models"

    def auth(self, secret: Secret) -> Auth:
        return Auth("authorization", secret, "Bearer ")

    # ------------------------------------------------------------------ configuration

    def base_url(self) -> str | None:
        if self.local_url_env:
            try:
                return check_local(self.environ.get(self.local_url_env, ""))
            except ValueError:
                return None
        raw = self.environ.get(f"{self.origins_env}_BASE", "")
        return raw.rstrip("/") or None

    def allowed_origins(self) -> frozenset[Origin]:
        if self.local_url_env:
            base = self.base_url()
            return frozenset({parse_origin(base, schemes=self.schemes())}) if base else frozenset()
        return parse_origin_list(self.environ.get(self.origins_env, ""))

    def schemes(self) -> tuple[str, ...]:
        return ("https",) if self.address_policy is AddressPolicy.PUBLIC else ("http", "https")

    def problem(self) -> str | None:
        if self.generates and not generation_allowed(self.environ):
            return "generated media is switched off by the operator"
        if self.needs_model and not self.model:
            return "no model is set on this connection"
        if self.model and not MODEL_ID.match(self.model):
            return "the model id on this connection is not a valid id"
        if self.settings is None:
            return "the connection settings do not match this provider's schema"
        if self.needs_secret and (self.ctx.connection.secret_ref is None or self.ctx.vault is None):
            return "no secret is stored for this connection"
        try:
            tenant_paths(self.ctx.tenant_id, self.environ)
        except BrandMisconfigured as exc:
            return f"no tenant folder for the output: {exc}"
        base = self.base_url()
        if base is None:
            return "the operator has not set the endpoint for this provider"
        try:
            allowed = self.allowed_origins()
            here = parse_origin(base, schemes=self.schemes())
        except ValueError:
            return "the operator's endpoint configuration is not a list of canonical origins"
        return None if here in allowed else "the endpoint is not one the operator allowed"

    def _send(self, call: MediaCall, *, extra_origins: frozenset[Origin] = frozenset()) -> MediaReply:
        try:
            ok = parse_origin(call.url, schemes=self.schemes()) in (self.allowed_origins() | extra_origins)
        except ValueError:
            ok = False
        if not ok:
            raise MediaProviderError("refused a call to an endpoint the operator did not allow")
        return self.transport(call)

    def _auth(self) -> Auth | None:
        if not self.needs_secret:
            return None
        secret = self.ctx.secret()
        if secret is None or not secret:
            raise MediaProviderError("the vault returned no secret for this connection")
        check_header_secret(secret)
        return self.auth(secret)

    def _check(self, request: c.Request) -> None:
        c.check_request(self, request)  # type: ignore[arg-type]
        if request.tenant_id != self.ctx.tenant_id:
            raise InvalidConnection("this connection belongs to another tenant")
        if (issue := self.problem()) is not None:
            raise ProviderUnavailable(f"the {self.name} connection is not configured: {issue}")
        if (why := self.serves(request)) is not None:
            raise ProviderUnavailable(why)

    def _cost(self, units: Decimal) -> Decimal:
        assert self.settings is not None
        return to_eur(units * self.settings.usd_per_unit)

    # ------------------------------------------------------------------ the five calls

    def describe(self) -> c.ProviderInfo:
        s = self.settings
        return c.ProviderInfo(capability=self.capability, name=self.name, mode=self.mode,
                              models=(self.model,) if self.model else (),
                              unit_cost_eur={} if s is None else {"per_unit": to_eur(s.usd_per_unit)},
                              limits={"configured": self.problem() is None, "generates": self.generates},
                              licence_note=self.licence_note, sends_externally=self.sends_externally)

    def health(self) -> c.Health:
        now = self.clock.now()
        if (issue := self.problem()) is not None:
            return c.Health(connected=False, checked_at=now, detail=issue)
        base = self.base_url()
        assert base is not None
        try:
            reply = self._send(MediaCall("GET", f"{base}{self.health_path()}", {}, None, self._auth(), 10.0))
        except ConnectionsError as exc:
            return c.Health(connected=False, checked_at=now, detail=type(exc).__name__)
        ok = 200 <= reply.status < 300
        return c.Health(connected=ok, checked_at=now,
                        detail=None if ok else f"health check answered HTTP {status_class(reply.status)}")

    def estimate(self, request: c.Request) -> c.Estimate:
        self._check(request)
        return c.Estimate(cost_eur=self._cost(self.units(request)), seconds=30.0)

    def run(self, request: c.Request) -> c.RunResult:
        self._check(request)
        c.check_cap(self.estimate(request), request)
        data, mime, ext, settings, units = self.produce(request, self._auth())
        path, sha = write_output(self.environ, request.tenant_id, data, ext)
        media = self.media_result(request, sha, mime, path)
        cost = self._cost(units)
        return c.RunResult(output=media, cost_eur=cost, seconds=30.0,
                           provenance=c.Provenance(made_by="provider", provider=self.name, mode=self.mode,
                                                   model=self.model, settings=settings, made_at=self.clock.now()),
                           external_ids=_external(settings))

    def media_result(self, request: c.Request, sha: str, mime: str, path: Path) -> c.MediaResult:
        raise NotImplementedError

    def mock(self) -> MockProvider:
        return MockProvider(self.capability, self.clock, name=f"mock-{self.name}")


def _external(settings: Mapping[str, Any]) -> dict[str, str]:
    ext = settings.get("external_id")
    return {"external_id": ext} if isinstance(ext, str) else {}


def check_reply(reply: MediaReply, provider: str) -> None:
    if not 200 <= reply.status < 300:
        raise MediaProviderError(f"{provider} answered HTTP {status_class(reply.status)}")


# ====================================================================== OpenAI-compatible image generation

class ImageGenerator(HttpMediaProvider):
    """POST {base}/images/generations {"model", "prompt", "size": "WxH", "n": 1}
    reply {"data": [{"b64_json": "..."}]}. The shape OpenAI's image API uses and the common local
    servers (LocalAI and others) copy. Only backgrounds and illustrations: never an upload, never
    a template, never the product."""

    capability = Capability.IMAGE
    licence_note = "generated by the vendor's model under its terms; carries a disclosure label"

    def serves(self, request: c.Request) -> str | None:
        assert isinstance(request, c.ImageRequest)
        if request.purpose not in ("background", "illustration"):
            return "generated images are only backgrounds or illustrations"
        if not request.prompt or not request.prompt.strip():
            return "a generated image needs a prompt"
        return None

    def produce(self, request: c.Request, auth: Auth | None) -> tuple[bytes, str, str, dict[str, Any], Decimal]:
        assert isinstance(request, c.ImageRequest) and self.settings is not None and request.prompt
        import base64

        base = self.base_url()
        prompt = guarded_prompt(request.prompt, request.allow_generated_people)
        body = json.dumps({"model": self.model, "prompt": prompt, "size": f"{request.width}x{request.height}",
                           "n": 1}).encode()
        reply = self._send(MediaCall("POST", f"{base}/images/generations", {"content-type": "application/json"},
                                     body, auth, self.settings.timeout_s))
        check_reply(reply, self.name)
        b = reply.body
        item = b.get("data", [None])[0] if isinstance(b, dict) and isinstance(b.get("data"), list) and b["data"] \
            else None
        if not isinstance(item, dict) or not isinstance(item.get("b64_json"), str):
            raise MediaProviderError(f"{self.name} returned an unexpected response shape (data[0].b64_json)")
        try:
            data = base64.b64decode(item["b64_json"], validate=True)
        except ValueError:
            raise MediaProviderError(f"{self.name} returned an image that is not base64") from None
        kind = image_kind(data)
        if kind is None:
            raise MediaProviderError(f"{self.name} returned bytes that are not an image")
        settings = {"purpose": request.purpose, "prompt_sent": prompt, "people_allowed": request.allow_generated_people,
                    "size": f"{request.width}x{request.height}"}
        return data, kind[0], kind[1], settings, Decimal(1)

    def media_result(self, request: c.Request, sha: str, mime: str, path: Path) -> c.MediaResult:
        from thallo.contracts.enums import MediaKind, MediaProvenance

        assert isinstance(request, c.ImageRequest)
        prov = MediaProvenance.GENERATED_BACKGROUND if request.purpose == "background" else MediaProvenance.GENERATED
        return c.MediaResult(kind=MediaKind.IMAGE, sha256=sha, mime=mime, storage_uri=path.as_uri(),
                             provenance=prov, width=request.width, height=request.height, disclosure_required=True)


__all__ = ["NO_PEOPLE", "SWITCH_ENV", "BytesTransport", "HttpMediaProvider", "ImageGenerator", "MediaCall",
           "MediaProviderError", "MediaReply", "MediaSettings", "MediaTransportFn", "check_reply", "generation_allowed",
           "guarded_prompt", "image_kind", "write_output"]
