"""Consent for an identifiable person in media (WP-04). Used by both approval and publish.

A media row with a consent_id needs that consent to be present, already given, not withdrawn, and to
cover the use (contracts `ConsentScope`, a closed set, so no free text can parse as a yes): LIKENESS
for images, PDFs and video, VOICE for audio, and VOICE as well for any media with `has_voice_track`.
A media row points at one consent row with one scope, so a video with a voice track (LIKENESS and
VOICE) cannot be covered today and fails closed. That needs a contract change (see the WP-04 report).
"""
from __future__ import annotations

from datetime import datetime

from cortex_operator.contracts.enums import ConsentScope, MediaKind
from cortex_operator.gate.canonical import MediaItem

MISSING = "consent_missing"
WITHDRAWN = "consent_withdrawn"
SCOPE = "consent_scope"

_BASE: dict[MediaKind, frozenset[ConsentScope]] = {
    MediaKind.IMAGE: frozenset({ConsentScope.LIKENESS}),
    MediaKind.PDF: frozenset({ConsentScope.LIKENESS}),
    MediaKind.VIDEO: frozenset({ConsentScope.LIKENESS}),
    MediaKind.AUDIO: frozenset({ConsentScope.VOICE}),
}


def required_scopes(item: MediaItem) -> frozenset[ConsentScope]:
    need = set(_BASE.get(item.media.kind, frozenset({ConsentScope.LIKENESS})))
    if item.media.has_voice_track:
        need.add(ConsentScope.VOICE)
    return frozenset(need)


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
    covered = {c.scope} if isinstance(c.scope, ConsentScope) else set()
    if missing := required_scopes(item) - covered:
        return SCOPE, f"media {media.id}: consent does not cover {sorted(s.value for s in missing)}"
    return None


__all__ = ["MISSING", "SCOPE", "WITHDRAWN", "consent_problem", "required_scopes"]
