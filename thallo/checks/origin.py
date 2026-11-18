"""Where media bytes came from, decided by the source that produced them, never by a caller's label
(WP-07 security review, ruling 2).

Ingest code calls `ingest_provenance(served)` with the registry's `Served` for the run that made the
file and writes that value to `media.provenance` (fixed after insert by migration 0023): the
template renderer gives TEMPLATE_RENDER, a manual connection (a person uploading the client's file)
gives CLIENT_PHOTO, any other image or video provider gives GENERATED. The check stack compares a
media row's label with the source it can still see (`origin_of`) and FAILs a mismatch.
"""
from __future__ import annotations

from thallo.connections.registry import ConnectionRegistry, Served
from thallo.contracts.enums import Capability, ConnectionMode, MediaProvenance
from thallo.contracts.models import Media

RENDERER = "template_render"
_MAKERS = frozenset({Capability.IMAGE, Capability.VIDEO})


def ingest_provenance(served: Served) -> MediaProvenance:
    if served.provider == RENDERER:
        return MediaProvenance.TEMPLATE_RENDER
    if served.mode is ConnectionMode.MANUAL:
        return MediaProvenance.CLIENT_PHOTO
    return MediaProvenance.GENERATED


def origin_of(media: Media, registry: ConnectionRegistry | None) -> MediaProvenance | None:
    """The source a media row's connection says made it; None when no connection is recorded."""
    if media.connection_id is None or registry is None:
        return None
    conn = registry.connections.get(media.tenant_id, media.connection_id)
    if conn is None:
        return MediaProvenance.GENERATED  # a recorded maker we cannot see: assume the strictest
    if conn.provider == RENDERER:
        return MediaProvenance.TEMPLATE_RENDER
    if conn.mode is ConnectionMode.MANUAL:
        return MediaProvenance.CLIENT_PHOTO
    if conn.capability in _MAKERS:
        return MediaProvenance.GENERATED
    return None


def same_source(label: MediaProvenance, origin: MediaProvenance) -> bool:
    generated = {MediaProvenance.GENERATED, MediaProvenance.GENERATED_BACKGROUND}
    return label == origin or (label in generated and origin in generated)


__all__ = ["RENDERER", "ingest_provenance", "origin_of", "same_source"]
