"""A Postiz the operator runs. Its origin comes ONLY from the operator's environment:

    THALLO_POSTIZ_SELF_HOSTED_ORIGIN   exactly one origin, https://host[:port], no path

never from connection settings (tenant-writable; the schema has no URL field at all). The value is
parsed strictly (`parse_origin`: lower case, no credentials, no path, query or fragment, https
only), and every call must match its (scheme, host, port) exactly. The host must also resolve to
public addresses only (`PostizTransport`), so a self-hosted Postiz on a private network is not
reachable from here; that is deliberate. Unset or malformed means the provider is unavailable and
the chain falls through to the manual pack. Everything else is in `postiz_base.py`.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.publishing.postiz_base import PostizProvider, PostizSettings, Transport
from thallo.providers.text.base import Origin, parse_origin

NAME = "postiz_self_hosted"
ORIGIN_ENV = "THALLO_POSTIZ_SELF_HOSTED_ORIGIN"


def operator_origin(value: str) -> Origin:
    """The one origin in the operator's setting; raises ValueError for anything else."""
    value = value.strip()
    if not value:
        raise ValueError(f"{ORIGIN_ENV} is not set; the operator names the self-hosted Postiz origin")
    if "," in value:
        raise ValueError(f"{ORIGIN_ENV} must name exactly one origin")
    if urlsplit(value).path not in ("", "/") or "?" in value or "#" in value:
        raise ValueError(f"{ORIGIN_ENV} is an origin: no path, query or fragment")
    try:
        return parse_origin(value.rstrip("/"))
    except ValueError:
        raise ValueError(f"{ORIGIN_ENV} is not a canonical https origin") from None


class PostizSelfHosted(PostizProvider):
    name = NAME
    api_path = "/api/public/v1"

    def origin(self) -> Origin:
        env = os.environ if self.environ is None else self.environ
        return operator_origin(env.get(ORIGIN_ENV, ""))


def create(ctx: ProviderContext, transport: Transport | None = None,
           environ: Mapping[str, str] | None = None) -> PostizSelfHosted:
    return PostizSelfHosted(ctx, transport=transport, environ=environ)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.PUBLISHING, NAME, ConnectionMode.HOSTED, create, settings_schema=PostizSettings)
