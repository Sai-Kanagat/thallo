"""Postiz Cloud. The origin is a constant in code: the key only ever goes to api.postiz.com.
Everything else is in `postiz_base.py`."""
from __future__ import annotations

from collections.abc import Mapping

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.publishing.postiz_base import PostizProvider, PostizSettings, Transport
from thallo.providers.text.base import Origin

NAME = "postiz_hosted"
ORIGIN: Origin = ("https", "api.postiz.com", 443)


class PostizHosted(PostizProvider):
    name = NAME
    api_path = "/public/v1"

    def origin(self) -> Origin:
        return ORIGIN


def create(ctx: ProviderContext, transport: Transport | None = None,
           environ: Mapping[str, str] | None = None) -> PostizHosted:
    return PostizHosted(ctx, transport=transport, environ=environ)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.PUBLISHING, NAME, ConnectionMode.HOSTED, create, settings_schema=PostizSettings)
