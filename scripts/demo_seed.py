"""Seed the local demo database with one fictional bakery. Called by scripts/demo.sh only.

    DEMO_ADMIN_URL=... DEMO_APP_URL=... THALLO_BRAND_ROOT=/abs/dir python scripts/demo_seed.py

Everything here is fictional demo data for Forno Finto, the repository's fictional brand. Nothing
is sent, published or spent, and no outside service is called:

- Copy goes through the real copy path (`produce_copy`, the truth compiler, the slop catalogue,
  selection, `write_outcome`). The writer is a scripted text connection in MOCK mode, allowed for
  this tenant only, registered over an in-memory connection store, so it never reaches the
  database. It returns the fixed drafts below instead of the contract mock's hex digest, which
  the truth compiler would reject as an unsourced digit.
- Images come from the real template renderer (headless Chromium, the demo brand) under
  THALLO_BRAND_ROOT. A render the design lint rejects keeps no file and writes its FAIL rows.
- Check rows are written by the real writers (`truth.to_check_results`, `media.lint.to_check_results`)
  and bound to each variant's final hash, the one the gate computes from storage.
- The only connections stored are the manual floor rows (`ensure_manual`): no provider, no key.

It never reads .env. It ends by building every review card the way the API does and checking
the deck holds a clean piece, a piece with a flag to acknowledge and a piece blocked by a FAIL.
"""
from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from api.backend import PgBackend  # noqa: E402
from api.media_store import TenantMediaStore  # noqa: E402
from api.review import _card  # noqa: E402
from thallo.connections.pg import PostgresConnectionStore  # noqa: E402
from thallo.connections.registry import ConnectionRegistry, NoSettings, ProviderContext, ProviderRegistry  # noqa: E402
from thallo.connections.stores import InMemoryConnectionStore  # noqa: E402
from thallo.contracts import connection as c  # noqa: E402
from thallo.contracts.clock import SystemClock  # noqa: E402
from thallo.contracts.enums import (  # noqa: E402
    Capability,
    Channel,
    CheckVerdict,
    ClaimKind,
    ConnectionMode,
    FunnelStage,
    Language,
    MediaKind,
    SourceKind,
    TextRole,
    UserRole,
)
from thallo.contracts.ids import new_id  # noqa: E402
from thallo.contracts.models import (  # noqa: E402
    AppUser,
    Brief,
    Bundle,
    Claim,
    GrowthModel,
    Idea,
    Identity,
    Media,
    Operator,
    Pillar,
    PlanSlot,
    Source,
    Tenant,
    Variant,
    VariantMedia,
)
from thallo.contracts.states import BUNDLE, PLAN_SLOT, VARIANT, BundleState, ClaimState, PlanSlotState, VariantState  # noqa: E402
from thallo.contracts.testing.mocks import MockProvider  # noqa: E402
from thallo.copy import CopyStatus, produce_copy, write_outcome  # noqa: E402
from thallo.gate import content_hashes  # noqa: E402
from thallo.media.lint import Finding, LintReport, RenderRejected  # noqa: E402
from thallo.media.lint import to_check_results as lint_rows  # noqa: E402
from thallo.media.render import ChromiumRenderer, find_chromium  # noqa: E402
from thallo.providers.image.template_render import TemplateRender  # noqa: E402
from thallo.store import Database  # noqa: E402
from thallo.store.adapters.claims import PgClaimRepository  # noqa: E402
from thallo.truth import library, to_check_results  # noqa: E402

DEMO_EMAIL = "demo@thallo.test"
SCRIPTED = "demo_script"
SOURCE_TEXT = ("Owner, fictional demo interview: we bake the sourdough at four every morning, "
               "the focaccia is new this month, rye and spelt every day.")


class ScriptedText(MockProvider):
    """A MOCK text connection that answers with the next queued draft. No outside call, no cost."""

    queue: list[str] = []

    def __init__(self, clock) -> None:  # noqa: ANN001
        super().__init__(Capability.TEXT, clock, name=SCRIPTED)

    def run(self, request: c.Request) -> c.RunResult:
        c.check_request(self, request)
        assert isinstance(request, c.TextRequest)
        text = ScriptedText.queue.pop(0) if ScriptedText.queue else ""
        return c.RunResult(output=c.TextResult(text=text, input_tokens=100, output_tokens=30), cost_eur=Decimal(0),
                           provenance=c.Provenance(made_by="provider", provider=SCRIPTED, mode=ConnectionMode.MOCK,
                                                   model="mock-1", made_at=self.clock.now()))


@dataclass
class Piece:
    key: str
    drafts: tuple[str, ...]  # one per candidate, in order
    template: str
    slots: dict[str, str]
    alt: str
    channel: Channel = Channel.INSTAGRAM


PIECES = (
    Piece("sourdough", ("Our sourdough comes out of the oven before the shop opens. A loaf is {{C-0001}} this weekend.",
                        "Look no further: our sourdough is a game changer. A loaf is {{C-0001}}."),
          "single-01", {"headline": "Sourdough before the shop opens", "body": "Baked before sunrise, on the counter "
                        "when we open the door.", "cta": "See you at the counter"},
          "Cream card with the headline Sourdough before the shop opens and the Forno Finto logo"),
    Piece("rye", ("Rye, spelt and plain white, baked every morning. Ask at the counter which one is still warm.",),
          "single-02", {"headline": "Rye, spelt and plain white", "body": "Baked every morning. Ask which one is "
                        "still warm."},
          "Dark card with the headline Rye, spelt and plain white", Channel.FACEBOOK),
    Piece("focaccia", ("Be the first to taste our new focaccia. A slice is {{C-0002}}.",),
          "single-03", {"headline": "New focaccia on the counter", "body": "Olive oil, sea salt, rosemary.",
                        "cta": "Come and taste it"},
          "Terracotta card with the headline New focaccia on the counter"),
    Piece("olive", ("Our olive bread is back on Fridays. A loaf is {{C-0001}}.",),
          "single-01", {"headline": "Our olive bread is back on Fridays, warm from the oven and ready before the "
                        "doors open", "body": "Green and black olives, folded in by hand."},
          "Cream card about the olive bread"),
    Piece("guestbook", ("Ask at the counter which loaf came out of the oven last. We will tell you.",),
          "single-06", {"headline": "Which loaf is still warm?", "body": "Ask at the counter. We will tell you."},
          "Cream card with the question Which loaf is still warm"),
)


def ident(clock) -> str:  # noqa: ANN001
    return new_id(clock)


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def main() -> int:  # noqa: C901, PLR0915
    admin_url, app_url = os.environ["DEMO_ADMIN_URL"], os.environ["DEMO_APP_URL"]
    brand_root = Path(os.environ["THALLO_BRAND_ROOT"]).resolve(strict=True)
    chromium = find_chromium()
    if chromium is None:
        print("No headless Chromium found on this machine; set THALLO_CHROMIUM. The demo needs it to render images.")
        return 1
    clock = SystemClock()
    now = clock.now()
    db = Database(app_url, clock, admin_url=admin_url)
    media_store = TenantMediaStore(brand_root)

    op = db.create_operator(Operator(id=ident(clock), created_at=now, name="Demo operator"))
    tenant = db.create_tenant(Tenant(id=ident(clock), created_at=now, operator_id=op.id,
                                     name="Forno Finto (demo bakery)", slug="forno-finto-demo", currency="EUR",
                                     default_language=Language.EN, region_code="IT", timezone="Europe/Rome"))
    t = tenant.id
    identity = Identity(id=ident(clock), created_at=now, email=DEMO_EMAIL, display_name="Demo owner")
    with psycopg.connect(admin_url) as conn:  # identities sit above tenants: owner only
        conn.execute("INSERT INTO identity (id, created_at, email, display_name) VALUES (%s, %s, %s, %s)",
                     (identity.id, identity.created_at, identity.email, identity.display_name))

    with db.tenant(t) as repo:
        founder = repo.insert(AppUser(id=ident(clock), created_at=now, tenant_id=t, identity_id=identity.id,
                                      display_name="Demo owner", role=UserRole.FOUNDER))
        source = repo.insert(Source(id=ident(clock), created_at=now, tenant_id=t, kind=SourceKind.DOCUMENT,
                                    title="Price list (fictional demo)", captured_at=now))
        for code, value, note in (("C-0001", "4.50", "sourdough loaf"), ("C-0002", "2.80", "focaccia slice")):
            repo.insert(Claim(id=ident(clock), created_at=now, tenant_id=t, code=code, kind=ClaimKind.PRICE,
                              state=ClaimState.VERIFIED, value=value, unit="EUR", source_id=source.id,
                              evidence_sha256=hashlib.sha256(f"demo price list: {note}".encode()).hexdigest(),
                              verified_by=founder.id, verified_at=now, expires_at=now + timedelta(days=60)))
        claims = library(PgClaimRepository(repo), t)

    # The scripted writer: MOCK mode, allowed for this tenant only, stored nowhere.
    providers = ProviderRegistry.with_discovered()
    providers.register(Capability.TEXT, SCRIPTED, ConnectionMode.MOCK,
                       lambda ctx: ScriptedText(ctx.clock), settings_schema=NoSettings)
    writer = ConnectionRegistry(clock=clock, providers=providers, connections=InMemoryConnectionStore(),
                                mock_tenants=(t,), environ={})
    writer.add(t, Capability.TEXT, SCRIPTED, role=TextRole.DRAFT, model="mock-1")
    renderer = TemplateRender(clock, renderer=ChromiumRenderer(chromium), tenant_id=t, mock_tenants=(t,),
                              environ={"THALLO_BRAND_ROOT": str(brand_root)})

    variants: dict[str, Variant] = {}
    for p in PIECES:
        with db.tenant(t) as repo:
            idea = repo.insert(Idea(id=ident(clock), created_at=now, tenant_id=t, stage=FunnelStage.CONSIDERATION,
                                    title=p.key.title(), rationale="Fictional demo idea"))
            brief = repo.insert(Brief(id=ident(clock), created_at=now, tenant_id=t, idea_id=idea.id,
                                      audience="Neighbours walking past in the morning", job_to_be_done="Buy bread",
                                      message=p.slots["headline"], call_to_action="Come to the counter",
                                      channels=[p.channel], allowed_claim_ids=[x.id for x in claims.values()],
                                      language=Language.EN))
        ScriptedText.queue = list(p.drafts)
        backend = PgBackend(app_url, clock, media_store)
        with backend.tenant(t) as tx:
            out = produce_copy(registry=writer, tenant=tenant, brief=brief, claims=claims, sources=[SOURCE_TEXT],
                               channel=p.channel, rule_version=1, clock=clock, candidates=len(p.drafts),
                               resolver=lambda url, g=tx.gate: g.resolve_link(t, url),
                               non_links=tx.gate.non_link_allowlist(t))
            if out.status is not CopyStatus.READY_FOR_CHECKS:
                raise SystemExit(f"{p.key}: copy did not build: {out.note}")
            write_outcome(tx.repo, out)
        variant, built = out.variant, next(r.built for r in out.candidates if r.built is not None
                                          and r.built.compiled.text == out.variant.text)
        request = c.ImageRequest(tenant_id=t, purpose="template_render", template_ref=p.template,
                                 slots={**p.slots, "_lang": "en", "_channel": p.channel.value}, width=1080, height=1350)
        try:
            result = renderer.run(request)
            report = LintReport(findings=tuple(Finding(check=d["check"], verdict=CheckVerdict(d["verdict"]),
                                                       reason=d["reason"])
                                               for d in result.provenance.settings["lint"]))
            image: c.MediaResult | None = result.output  # type: ignore[assignment]
        except RenderRejected as exc:
            report, image = exc.report, None
        with backend.tenant(t) as tx:
            if image is not None:
                size = len(media_store.read(t, image.storage_uri))
                m = tx.insert(Media(id=ident(clock), created_at=now, tenant_id=t, kind=MediaKind.IMAGE,
                                    sha256=image.sha256, mime=image.mime, size_bytes=size,
                                    storage_uri=image.storage_uri, provenance=image.provenance, width=image.width,
                                    height=image.height, alt_text=p.alt))
                tx.insert(VariantMedia(id=ident(clock), created_at=now, tenant_id=t, variant_id=variant.id,
                                       media_id=m.id))
            sha = content_hashes(tx.gate, t, out.bundle.id)[variant.id]  # the final hash, media included
            rows = to_check_results(built.compiled, variant_id=variant.id, content_sha256=sha, clock=clock)
            rows += lint_rows(report, tenant_id=t, variant_id=variant.id, content_sha256=sha, clock=clock)
            for row in rows:
                tx.insert(row)
            VARIANT.check(VariantState.CHECKING, VariantState.REVIEW)
            BUNDLE.check(BundleState.OPEN, BundleState.IN_REVIEW)
            tx.update(Variant, variant.id, state=VariantState.REVIEW)
            tx.update(Bundle, out.bundle.id, state=BundleState.IN_REVIEW)
        variants[p.key] = variant
        print(f"  piece {p.key}: copy built, image {'rendered' if image else 'refused by the design lint'}")

    # The week plan: this week, a few slots.
    week = monday(now.date())
    with db.tenant(t) as repo:
        gm = repo.insert(GrowthModel(id=ident(clock), created_at=now, tenant_id=t, version=1))
        bread = repo.insert(Pillar(id=ident(clock), created_at=now, tenant_id=t, growth_model_id=gm.id,
                                   name="Daily bread", stage=FunnelStage.CONSIDERATION, target_share=Decimal("0.6")))
        repo.insert(Pillar(id=ident(clock), created_at=now, tenant_id=t, growth_model_id=gm.id, name="Behind the counter",
                           stage=FunnelStage.AWARENESS, target_share=Decimal("0.4")))
        plan = ((0, Channel.INSTAGRAM, FunnelStage.CONSIDERATION, PlanSlotState.MISSED, None),
                (1, Channel.INSTAGRAM, FunnelStage.CONSIDERATION, PlanSlotState.FILLED, "sourdough"),
                (2, Channel.FACEBOOK, FunnelStage.AWARENESS, PlanSlotState.OPEN, None),
                (3, Channel.INSTAGRAM, FunnelStage.AWARENESS, PlanSlotState.OPEN, None),
                (5, Channel.INSTAGRAM, FunnelStage.CONSIDERATION, PlanSlotState.OPEN, None))
        for day, channel, stage, state, key in plan:
            if state is not PlanSlotState.OPEN:
                PLAN_SLOT.check(PlanSlotState.OPEN, state)
            repo.insert(PlanSlot(id=ident(clock), created_at=now, tenant_id=t, week_start=week, channel=channel,
                                 pillar_id=bread.id, stage=stage, state=state,
                                 starts_at=datetime.combine(week + timedelta(days=day), time(8, 30), timezone.utc),
                                 variant_id=variants[key].id if key else None))

    # Connections: the manual floor only. No provider, no key.
    ConnectionRegistry(clock=clock, providers=ProviderRegistry.with_discovered(),
                       connections=PostgresConnectionStore(db), environ={}).ensure_manual(t)

    # Check the deck the way the API builds it.
    backend = PgBackend(app_url, clock, media_store)
    with backend.tenant(t) as tx:
        cards = [_card(tx, SimpleNamespace(tenant_id=t), b) for b in tx.list_all(Bundle, state=BundleState.IN_REVIEW)]
    clean = [x for x in cards if x["can_approve"] and not x["flags"]]
    flagged = [x for x in cards if x["can_approve"] and x["flags"]]
    failed = [x for x in cards if any(b["kind"] == "fail" for b in x["blocking"])]
    unchecked = [x for x in cards if any(b["kind"] != "fail" for b in x["blocking"])]
    print(f"  deck: {len(cards)} cards, {len(clean)} clean, {len(flagged)} with a flag, {len(failed)} blocked by a FAIL")
    if not (clean and flagged and failed) or unchecked:
        print("The demo deck is not as expected; see the counts above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
