"""Hosted image generation over the OpenAI-compatible image API. Off by default.

Unavailable unless the operator set THALLO_ALLOW_GENERATED_MEDIA=1. Every file it makes is
GENERATED (`ingest_provenance`), needs the region policy's disclosure label at ingest and is
FLAGged by the check stack. Only backgrounds and illustrations; without a consent-backed request
the prompt forbids people, products, premises and text.

Operator configuration, in the environment only:
    THALLO_IMAGE_GEN_ORIGINS        comma-separated https origins allowed (e.g. https://api.openai.com)
    THALLO_IMAGE_GEN_ORIGINS_BASE   the base URL including /v1, on one of those origins
    THALLO_BRAND_ROOT               where the tenant's output folder is
The secret comes from the vault; the model from the connection row.
"""
from __future__ import annotations

from collections.abc import Mapping

from thallo.connections.registry import ProviderContext, ProviderRegistry
from thallo.contracts.enums import Capability, ConnectionMode
from thallo.providers.image.http_media import ConsentLookup, ImageGenerator, MediaSettings, MediaTransportFn

NAME = "generated_hosted"


class HostedImageGenerator(ImageGenerator):
    name = NAME
    mode = ConnectionMode.HOSTED
    origins_env = "THALLO_IMAGE_GEN_ORIGINS"


def create(ctx: ProviderContext, transport: MediaTransportFn | None = None,
           environ: Mapping[str, str] | None = None, consents: ConsentLookup | None = None) -> HostedImageGenerator:
    return HostedImageGenerator(ctx, transport=transport, environ=environ, consents=consents)


def register(registry: ProviderRegistry) -> None:
    registry.register(Capability.IMAGE, NAME, ConnectionMode.HOSTED, create, settings_schema=MediaSettings)
