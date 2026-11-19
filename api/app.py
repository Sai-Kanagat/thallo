"""The FastAPI application.

    from api.app import create_app, services_from_env
    app = create_app(services_from_env())

Every response carries strict security headers. JSON is never cached. The API serves no HTML and
no script, so its CSP is `default-src 'none'`; the web app (`web/`) sets its own nonce-based CSP.

`services_from_env` reads only operator variables (see `.env.example`). It refuses to start
without the app and owner database URLs, and uses the console code sender only when
THALLO_DEV_CONSOLE_LOGIN=1; otherwise no code is ever delivered and nobody can sign in.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping

from fastapi import FastAPI, Request, Response

from api import (  # one router per line: parallel packages append without conflicts
    auth,
    connections,
    engage,
    errors,
    growth,
    leads,
    plan,
    review,
    tracking,
    trust,
    voice,
)
from api.context import Services

log = logging.getLogger("thallo.api")

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


def create_app(svc: Services) -> FastAPI:
    app = FastAPI(title="Thallo API", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = svc
    errors.install(app)

    @app.middleware("http")
    async def _headers(request: Request, call_next) -> Response:  # noqa: ANN001
        response = await call_next(request)
        for k, v in _HEADERS.items():
            response.headers.setdefault(k, v)
        response.headers.setdefault("Content-Security-Policy", API_CSP)
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    for r in (
        auth.router,
        review.router,
        plan.router,
        connections.router,
        growth.router,
        leads.router,
        tracking.router,
        voice.router,
        trust.router,
        engage.router,
    ):
        app.include_router(r)
    return app


_LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$")


_HEX_KEY = re.compile(r"^(?:[0-9a-fA-F]{2}){32,}$")


class StartupRefused(RuntimeError):
    pass


def check_startup(env: Mapping[str, str]) -> tuple[list[str], bool]:
    """Operator settings that must hold before the API serves anyone. Returns (web origins,
    whether the dev console sender may run) or raises StartupRefused with the reason.

    - THALLO_WEB_ORIGIN must be set unless THALLO_ENVIRONMENT=development; the Origin check is
      never silently skipped in production.
    - THALLO_PROXY_STAMP_KEY must be set unless THALLO_ENVIRONMENT=development: without it no
      client address stamp is trusted and every browser would share the web server's bucket.
    - THALLO_DEV_CONSOLE_LOGIN=1 prints login codes to stderr, so it needs a second marker
      (THALLO_ENVIRONMENT=development) and every web origin on localhost; a loud warning is logged."""
    dev = env.get("THALLO_ENVIRONMENT", "") == "development"
    origins = [o.strip() for o in env.get("THALLO_WEB_ORIGIN", "").split(",") if o.strip()]
    if not origins and not dev:
        raise StartupRefused("THALLO_WEB_ORIGIN is empty: set the web app's origin (or THALLO_ENVIRONMENT=development)")
    key = env.get("THALLO_PROXY_STAMP_KEY", "")
    if not dev and not _HEX_KEY.match(key):
        raise StartupRefused("THALLO_PROXY_STAMP_KEY must be hex of 32 or more bytes (scripts/gen_keys.py)")
    console = env.get("THALLO_DEV_CONSOLE_LOGIN") == "1"
    if console:
        if not dev:
            raise StartupRefused("THALLO_DEV_CONSOLE_LOGIN=1 needs THALLO_ENVIRONMENT=development")
        if not origins or not all(_LOCAL_ORIGIN.match(o) for o in origins):
            raise StartupRefused("THALLO_DEV_CONSOLE_LOGIN=1 needs THALLO_WEB_ORIGIN on localhost only")
        log.warning("DEVELOPMENT LOGIN: sign-in codes are printed to this console. Never run this on a real server.")
    return origins, console


def services_from_env(environ: Mapping[str, str] | None = None) -> Services:  # pragma: no cover - wiring
    from api.backend import PgBackend
    from api.directory import OwnerDirectory
    from api.media_store import TenantMediaStore
    from api.sessions import ConsoleCodeSender, DeviceSigner, NoCodeSender
    from thallo.connections.registry import ProviderRegistry
    from thallo.connections.vault import Vault
    from thallo.contracts.clock import SystemClock
    from thallo.gate.signing import GateKeyMissing, GateSigner

    env = os.environ if environ is None else environ
    origins, console = check_startup(env)
    app_url, owner_url = env.get("DATABASE_URL", ""), env.get("DATABASE_ADMIN_URL", "")
    if not app_url or not owner_url:
        raise RuntimeError("DATABASE_URL and DATABASE_ADMIN_URL must be set for the API")
    clock = SystemClock()
    media = TenantMediaStore(env.get("THALLO_BRAND_ROOT") or None)
    backend = PgBackend(app_url, clock, media,
                        redirect_prefixes=[p for p in env.get("THALLO_REDIRECT_PREFIXES", "").split(",") if p.strip()])
    try:
        signer: GateSigner | None = GateSigner.from_env(env)
    except GateKeyMissing:
        signer = None  # approvals refuse with a clear reason
    _conns, secrets, _health = backend.connection_stores()
    vault = Vault.from_env(secrets, clock, env) if env.get("THALLO_VAULT_KEY") else None
    sender = ConsoleCodeSender() if console else NoCodeSender()
    device_key = env.get("THALLO_DEVICE_KEY", "")
    peers = frozenset(h.strip() for h in env.get("THALLO_WEB_PROXY_HOSTS", "").split(",") if h.strip())
    raw_stamp_key = env.get("THALLO_PROXY_STAMP_KEY", "")
    stamp_key = bytes.fromhex(raw_stamp_key) if _HEX_KEY.match(raw_stamp_key) else None
    redirector = None
    if env.get("THALLO_REDIRECT_DATABASE_URL"):
        from thallo.tracking.redirector import Redirector
        from thallo.tracking.wiring import PgLinkDirectory, backend_recorder
        redirector = Redirector(PgLinkDirectory(env["THALLO_REDIRECT_DATABASE_URL"]),
                                backend_recorder(backend, clock), clock)
    return Services(clock=clock, backend=backend, directory=OwnerDirectory(owner_url), sender=sender, media=media,
                    signer=signer, providers=ProviderRegistry.with_discovered(), vault=vault,
                    allowed_origins=origins, environ=connections.default_environ(),
                    proxy_peers=peers, stamp_key=stamp_key,
                    devices=DeviceSigner(bytes.fromhex(device_key) if device_key else None), redirector=redirector)


__all__ = ["API_CSP", "StartupRefused", "check_startup", "create_app", "services_from_env"]
