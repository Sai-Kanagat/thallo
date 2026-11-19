"""Image generation on this machine: a loopback server speaking the OpenAI-compatible image API
(LocalAI, or a diffusion server behind a compatible shim). Off by default.

Unavailable unless the operator set THALLO_ALLOW_GENERATED_MEDIA=1. No secret and nothing leaves
the machine, but the files are still GENERATED: disclosure label at ingest, FLAG at the checks.

Operator configuration, in the environment only:
    THALLO_IMAGE_LOCAL_URL   the loopback base URL including /v1 (http://127.0.0.1:PORT/v1)
    THALLO_BRAND_ROOT        where the tenant's output folder is
"""
from __future__ import annotations

from collections.abc import Mapping

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.connections.secret import Secret
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.image.http_media import (
    ConsentLookup,
    ImageGenerator,
    MediaProviderError,
    MediaSettings,
    MediaTransportFn,
)
from thallo.providers.text.base import AddressPolicy, Auth

NAME = "generated_local"


class LocalImageGenerator(ImageGenerator):
    name = NAME
    mode = ConnectionMode.LOCAL
    needs_secret = False
    sends_externally = False
    address_policy = AddressPolicy.LOOPBACK
    local_url_env = "THALLO_IMAGE_LOCAL_URL"
    licence_note = "the local model's own licence; nothing leaves this machine; carries a disclosure label"

    def auth(self, secret: Secret) -> Auth:  # pragma: no cover - needs_secret is False
        raise MediaProviderError("the local endpoint takes no secret")


def create(ctx: ProviderContext, transport: MediaTransportFn | None = None,
           environ: Mapping[str, str] | None = None, consents: ConsentLookup | None = None) -> LocalImageGenerator:
    return LocalImageGenerator(ctx, transport=transport, environ=environ, consents=consents)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.IMAGE, NAME, ConnectionMode.LOCAL, create, settings_schema=MediaSettings)
