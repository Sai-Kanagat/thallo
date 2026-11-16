"""What a publishing provider raises from `run()`, so the publisher knows whether anything left.

The publisher treats every other exception from a send as `OutcomeUnknown`: when in doubt it
assumes the post may exist, records that, and reconciles instead of sending again.
"""
from __future__ import annotations

from thallo.connections.errors import ConnectionsError


class NotSent(ConnectionsError):  # noqa: N818 - says what happened, like ProviderUnavailable
    """Nothing reached the audience: refused before the request left, or refused by the service
    in a way that means no post exists (a 4xx on create, a rate limit). `retryable` says whether
    the same bytes may be tried again later with backoff."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class OutcomeUnknown(ConnectionsError):  # noqa: N818
    """The request may have created a post (a timeout or reset after the bytes left, a 5xx).
    Never resend: reconcile by readback."""


class LookupFailed(ConnectionsError):  # noqa: N818
    """A readback or reconcile lookup could not be answered. Nothing changes; try later."""


class AmbiguousLookup(LookupFailed):
    """The lookup found more than one candidate. Retrying will not settle it: a person must."""


__all__ = ["AmbiguousLookup", "LookupFailed", "NotSent", "OutcomeUnknown"]
