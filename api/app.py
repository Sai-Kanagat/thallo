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

import os
from collections.abc import Mapping

from fastapi import FastAPI, Request, Response

from api import auth, connections, errors, plan, review
from api.context import Services

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

    for r in (auth.router, review.router, plan.router, connections.router):
        app.include_router(r)
    return app


def services_from_env(environ: Mapping[str, str] | None = None) -> Services:  # pragma: no cover - wiring
    from api.backend import PgBackend
    from api.directory import OwnerDirectory
    from api.media_store import TenantMediaStore
    from api.sessions import ConsoleCodeSender, NoCodeSender
    from thallo.connections.registry import ProviderRegistry
    from thallo.connections.vault import Vault
    from thallo.contracts.clock import SystemClock
    from thallo.gate.signing import GateKeyMissing, GateSigner

    env = os.environ if environ is None else environ
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
    sender = ConsoleCodeSender() if env.get("THALLO_DEV_CONSOLE_LOGIN") == "1" else NoCodeSender()
    origins = [o.strip() for o in env.get("THALLO_WEB_ORIGIN", "").split(",") if o.strip()]
    return Services(clock=clock, backend=backend, directory=OwnerDirectory(owner_url), sender=sender, media=media,
                    signer=signer, providers=ProviderRegistry.with_discovered(), vault=vault,
                    allowed_origins=origins, environ=connections.default_environ())


__all__ = ["API_CSP", "create_app", "services_from_env"]
