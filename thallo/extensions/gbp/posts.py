"""GBP posts and photo updates as ordinary pieces, left for the check stack, the gate and the publisher.

`build_post` takes post text with claim slots (from copy production or a person), runs it through
`truth.build`, and returns idea, brief, asset, bundle and variant rows on channel
`google_business_profile` (task type `gbp_post`, the name the plays use), plus claim links and the
compile check rows bound to the gate's check hash. `build_photo_update` does the same for one or
more of the client's own photos (task type `gbp_photo`) with an optional caption.

Photos must be the client's own: provenance `client_photo` or `person_file`. A generated or
template image is refused, because a Business Profile photo stands for the real premises.

Nothing here approves, schedules or publishes. The variant lands in CHECKING; WP-07 runs the rest
of the stack, a named human approves the bundle, and `Publisher.publish` sends it through
`check_publish` to the tenant's publishing chain (the GBP provider, Postiz, or the manual pack).
"""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from thallo.contracts.clock import Clock
from thallo.contracts.enums import Channel, FunnelStage, Language, MediaKind, MediaProvenance
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    Asset,
    Brief,
    Bundle,
    CheckResult,
    Claim,
    Idea,
    Media,
    Tenant,
    Variant,
    VariantClaim,
    VariantMedia,
)
from thallo.contracts.states import VariantState
from thallo.gate.canonical import LinkResolver, MediaItem, VariantContent
from thallo.gate.checks import check_hash
from thallo.truth import build, to_check_results

PROMPT_VERSION = "gbp.post/1"
POST_TASK = "gbp_post"
PHOTO_TASK = "gbp_photo"
POST_MAX_CHARS = 1500  # Google's published cap on a post body; also checked by the provider
CLIENT_OWN = frozenset({MediaProvenance.CLIENT_PHOTO, MediaProvenance.PERSON_FILE})
MAX_PHOTOS = 10


class PostRefused(ValueError):
    pass


@dataclass
class PieceOutcome:
    idea: Idea
    brief: Brief
    asset: Asset
    bundle: Bundle
    variant: Variant
    variant_claims: tuple[VariantClaim, ...] = ()
    variant_media: tuple[VariantMedia, ...] = ()
    check_results: tuple[CheckResult, ...] = ()
    flags: tuple[str, ...] = field(default_factory=tuple)

    def rows(self) -> list[Any]:
        return [self.idea, self.brief, self.asset, self.bundle, self.variant, *self.variant_claims,
                *self.variant_media, *self.check_results]


def _piece(*, tenant: Tenant, text: str, title: str, why: str, task: str, claims: Mapping[str, Claim],
           photos: Sequence[Media], clock: Clock, resolver: LinkResolver, non_links: Collection[str],
           language: Language | None) -> PieceOutcome:
    lang = language or tenant.default_language
    now = clock.now()
    built = None
    if text.strip():
        built = build(text, tenant_id=tenant.id, language=lang, claims=claims, clock=clock,
                      default_language=tenant.default_language, tenant_currency=tenant.currency)
        if len(built.filled.text) > POST_MAX_CHARS:
            raise PostRefused(f"the post is {len(built.filled.text)} characters, over {POST_MAX_CHARS}")
    by_id = {cl.id: cl for cl in claims.values()}
    used = [by_id[i] for i in dict.fromkeys(built.compiled.claim_ids if built else ()) if i in by_id]
    idea = Idea(id=new_id(clock), created_at=now, tenant_id=tenant.id, stage=FunnelStage.AWARENESS,
                title=title[:200], rationale=why, prompt_version=PROMPT_VERSION)
    brief = Brief(id=new_id(clock), created_at=now, tenant_id=tenant.id, idea_id=idea.id,
                  audience="People who find the business on Google",
                  job_to_be_done="See the business is open and active",
                  message=title[:500], channels=[Channel.GOOGLE_BUSINESS_PROFILE],
                  allowed_claim_ids=[x.id for x in used], call_to_action="Visit or call", language=lang,
                  prompt_version=PROMPT_VERSION)
    source = built.compiled.text if built else ""
    asset = Asset(id=new_id(clock), created_at=now, tenant_id=tenant.id, brief_id=brief.id, master_text=source,
                  prompt_version=PROMPT_VERSION)
    bundle = Bundle(id=new_id(clock), created_at=now, tenant_id=tenant.id, asset_id=asset.id, rule_version=1)
    variant = Variant(id=new_id(clock), created_at=now, tenant_id=tenant.id, bundle_id=bundle.id,
                      channel=Channel.GOOGLE_BUSINESS_PROFILE, language=lang, state=VariantState.CHECKING,
                      text=source, rendered_text=built.filled.text if built else "", task_type=task,
                      prompt_version=PROMPT_VERSION)
    links = tuple(VariantClaim(id=new_id(clock), created_at=now, tenant_id=tenant.id, variant_id=variant.id,
                               claim_id=x.id) for x in used)
    vm = tuple(VariantMedia(id=new_id(clock), created_at=now, tenant_id=tenant.id, variant_id=variant.id,
                            media_id=m.id, position=i) for i, m in enumerate(photos))
    rows: tuple[CheckResult, ...] = ()
    if built is not None:
        items = tuple(MediaItem(media=m, position=i, bytes_sha256=m.sha256) for i, m in enumerate(photos))
        sha = check_hash(VariantContent(variant=variant, claims=tuple(used), media=items), resolver=resolver,
                         non_links=frozenset(non_links))
        rows = tuple(to_check_results(built.compiled, variant_id=variant.id, content_sha256=sha, clock=clock))
    flags = tuple(f.message for f in built.flags) if built else ()
    return PieceOutcome(idea, brief, asset, bundle, variant, links, vm, rows, flags)


def build_post(*, tenant: Tenant, text: str, title: str, claims: Mapping[str, Claim], clock: Clock,
               resolver: LinkResolver, non_links: Collection[str], language: Language | None = None,
               why: str = "Keep the Business Profile fresh with a current post.") -> PieceOutcome:
    if not text.strip():
        raise PostRefused("a post needs text")
    return _piece(tenant=tenant, text=text, title=title, why=why, task=POST_TASK, claims=claims, photos=(),
                  clock=clock, resolver=resolver, non_links=non_links, language=language)


def build_photo_update(*, tenant: Tenant, photos: Sequence[Media], claims: Mapping[str, Claim], clock: Clock,
                       resolver: LinkResolver, non_links: Collection[str], caption: str = "",
                       language: Language | None = None, title: str = "New photos of the business",
                       why: str = "Fresh photos from the client keep the listing current.") -> PieceOutcome:
    if not photos or len(photos) > MAX_PHOTOS:
        raise PostRefused(f"a photo update needs between 1 and {MAX_PHOTOS} photos")
    for m in photos:
        if m.tenant_id != tenant.id:
            raise PostRefused("a photo belongs to another tenant")
        if m.kind is not MediaKind.IMAGE:
            raise PostRefused("a photo update takes images only")
        if m.provenance not in CLIENT_OWN:
            raise PostRefused(f"photo {m.id} is {m.provenance.value}; the Business Profile takes the "
                              "client's own photos")
    return _piece(tenant=tenant, text=caption, title=title, why=why, task=PHOTO_TASK, claims=claims, photos=photos,
                  clock=clock, resolver=resolver, non_links=non_links, language=language)
