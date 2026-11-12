"""Consent for an identifiable person in media (WP-04). Used by both approval and publish.

A media row with a consent_id needs that consent to be present, already given, not withdrawn, and
to cover the use: likeness for images, PDFs and video, voice for audio. A video is assumed to carry a
voice track (the media row has no field saying otherwise), so it needs likeness and voice. A consent
scope may name several uses, separated by `+`, `,`, `;`, `|`, `/` or spaces, such as "likeness+voice".
"""
from __future__ import annotations

import re
from datetime import datetime

from cortex_operator.contracts.enums import MediaKind
from cortex_operator.gate.canonical import MediaItem

MISSING = "consent_missing"
WITHDRAWN = "consent_withdrawn"
SCOPE = "consent_scope"

REQUIRED_SCOPES: dict[MediaKind, frozenset[str]] = {
    MediaKind.IMAGE: frozenset({"likeness"}),
    MediaKind.PDF: frozenset({"likeness"}),
    MediaKind.VIDEO: frozenset({"likeness", "voice"}),
    MediaKind.AUDIO: frozenset({"voice"}),
}
_SPLIT = re.compile(r"[\s+,;|/]+")


def scopes(scope: str) -> frozenset[str]:
    return frozenset(s for s in _SPLIT.split(scope.strip().lower()) if s)


def consent_problem(item: MediaItem, now: datetime) -> tuple[str, str] | None:
    """(reason, detail) when the media item may not go out, or None."""
    media = item.media
    if media.consent_id is None:
        return None
    c = item.consent
    if c is None or c.id != media.consent_id:
        return MISSING, f"media {media.id}: consent record missing"
    if c.given_at > now:
        return MISSING, f"media {media.id}: consent not given yet"
    if c.withdrawn_at is not None and c.withdrawn_at <= now:
        return WITHDRAWN, f"media {media.id}: consent withdrawn"
    need = REQUIRED_SCOPES.get(media.kind, frozenset({"likeness"}))
    if missing := need - scopes(c.scope):
        return SCOPE, f"media {media.id}: consent scope {c.scope!r} does not cover {sorted(missing)}"
    return None


__all__ = ["MISSING", "REQUIRED_SCOPES", "SCOPE", "WITHDRAWN", "consent_problem", "scopes"]
