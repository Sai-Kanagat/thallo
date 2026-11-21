"""Import a signed claim pack into one tenant, and verify an imported claim.

Import never trusts the file beyond its signature: it checks the signature against keys the
workspace already trusts, refuses an expired pack, re-runs every content rule, and writes each
fact as a fresh claim CANDIDATE with a new code of this tenant. Nothing is copied from the pack
but the fact itself: no code, state, verifier or id. A candidate fills no slot, so each
importing client must verify each claim before it can be used (`verify_pack_claim`, human only).

Every imported claim takes the pack's expiry, so pack claims expire together. `verify_pack_claim`
always verifies to exactly that date and refuses once the pack has expired; migration 0041 adds a
database trigger so no other verify path can push a pack claim past it either.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from thallo.contracts.actors import Actor, SessionVerifier
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Language, SourceKind
from thallo.contracts.events import Event
from thallo.contracts.ids import new_id
from thallo.contracts.models import Claim, Source
from thallo.extensions.packs.pack import (
    MAX_LIFETIME,
    PackError,
    TrustedKeys,
    canonical,
    check_item,
    check_window,
    open_pack,
    text_problems,
)
from thallo.truth.claims import new_candidate, next_code, verify_claim
from thallo.truth.repository import ClaimRepository

CLOCK_SKEW = timedelta(minutes=10)


@dataclass(frozen=True)
class PackLink:
    """One claim_pack_import row."""

    id: str
    created_at: datetime
    tenant_id: str
    pack_id: str
    pack_digest: str
    key_id: str
    publisher: str
    item_key: str
    claim_id: str
    pack_expires_at: datetime


@dataclass(frozen=True)
class PackImport:
    pack_id: str
    title: str
    publisher: str
    digest: str
    expires_at: datetime
    sources: tuple[Source, ...]
    claims: tuple[Claim, ...]
    links: tuple[PackLink, ...]
    events: tuple[Event, ...]


def import_pack(data: bytes | str | Mapping[str, Any], *, trusted: TrustedKeys, tenant_id: str,
                existing_codes: Sequence[str], tenant_languages: Collection[Language], clock: Clock, actor: Actor,
                imported_digests: Collection[str] = ()) -> PackImport:
    """Rows to write in one transaction (see `persist.write_import`)."""
    body, pack_digest, key_id = open_pack(data, trusted)
    now = clock.now()
    if pack_digest in set(imported_digests):
        raise PackError("this pack is already imported")
    if now >= body.expires_at:
        raise PackError(f"the pack expired at {body.expires_at.isoformat()}")
    check_window(body.issued_at, body.expires_at)
    if body.issued_at > now + CLOCK_SKEW:
        raise PackError("the pack is dated in the future")
    if body.expires_at > now + MAX_LIFETIME:
        raise PackError(f"a pack may live at most {MAX_LIFETIME.days} days from today")
    for label, text in (("title", body.title), ("publisher", body.publisher)):
        if text_problems(text):
            raise PackError(f"pack {label} contains personal or contact data")
    for item in body.items:
        check_item(item, tenant_languages)
        if text_problems(f"{body.publisher}: {item.source.title}"):
            raise PackError(f"{item.key}: source title contains personal or contact data")
    codes = list(existing_codes)
    sources, claims, links, events = [], [], [], []
    for item in body.items:
        src = Source(id=new_id(clock), created_at=now, tenant_id=tenant_id, kind=SourceKind.DOCUMENT,
                     title=f"{body.publisher}: {item.source.title}"[:300], uri=item.source.uri,
                     captured_at=datetime.combine(item.source.captured_on, time(0), tzinfo=UTC))
        code = next_code(codes)
        codes.append(code)
        claim, event = new_candidate(
            tenant_id=tenant_id, code=code, kind=item.kind, value=item.value, unit=item.unit, as_of=item.as_of,
            clock=clock, actor=actor, source_id=src.id, evidence=canonical(item.model_dump(mode="json")),
            languages=item.languages, expires_at=body.expires_at)
        sources.append(src)
        claims.append(claim)
        events.append(event)
        links.append(PackLink(id=new_id(clock), created_at=now, tenant_id=tenant_id, pack_id=body.pack_id,
                              pack_digest=pack_digest, key_id=key_id, publisher=body.publisher, item_key=item.key,
                              claim_id=claim.id, pack_expires_at=body.expires_at))
    return PackImport(pack_id=body.pack_id, title=body.title, publisher=body.publisher, digest=pack_digest,
                      expires_at=body.expires_at, sources=tuple(sources), claims=tuple(claims), links=tuple(links),
                      events=tuple(events))


def verify_pack_claim(claim: Claim, link: PackLink, actor: Actor, *, verifier: SessionVerifier, clock: Clock,
                      repo: ClaimRepository, tenant_languages: Collection[Language] | None = None
                      ) -> tuple[Claim, Event]:
    """Human-only (through `truth.verify_claim`). Verifies to exactly the pack's expiry."""
    if link.tenant_id != claim.tenant_id or link.claim_id != claim.id:
        raise PackError("the pack link is for another claim")
    if clock.now() >= link.pack_expires_at:
        raise PackError(f"the pack expired at {link.pack_expires_at.isoformat()}; import a newer pack")
    return verify_claim(claim, actor, verifier=verifier, clock=clock, repo=repo, expires_at=link.pack_expires_at,
                        tenant_languages=tenant_languages)
