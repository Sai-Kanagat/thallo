"""Contract and robustness tests: the mock provider, the vision grader protocol, unverified bytes,
errors that are not provider errors, a raising repair, text changed after filling."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from thallo.checks import Settings, StackStatus, VisionGrader, VisionReply, run_stack
from thallo.checks import models as m
from thallo.connections import ConnectionRegistry, InMemoryConnectionStore, ProviderRegistry
from thallo.contracts.enums import Capability, CheckLayer, CheckVerdict, Language, MediaProvenance, TextRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import CostEntry
from thallo.contracts.testing.samples import T
from thallo.gate.checks import PHOTO_CHECK
from thallo.truth import build

from .conftest import SHARP_PHOTO, TEMPLATE_PNG, ClaudeScript, RecordedVision, Vendors, make_registry

DIGIT = "Our sourdough costs 12 euros this weekend."


def test_graders_on_the_mock_provider_never_read_as_no_concern(make_piece, make_ctx, clock, vault):
    reg = ConnectionRegistry(clock=clock, providers=ProviderRegistry.with_discovered(),
                             connections=InMemoryConnectionStore(), vault=vault, mock_tenants=[T], environ={})
    reg.add(T, Capability.TEXT, "mock", role=TextRole.GRADE)
    reg.add(T, Capability.TEXT, "mock", role=TextRole.SECOND_READ)
    out = run_stack(make_piece(), make_ctx(registry=reg))
    judged = [r for r in out.check_results if r.layer is not CheckLayer.CODE]
    assert judged and all(r.verdict is CheckVerdict.FLAG for r in judged)  # unreadable or unproven, never PASS


def test_vision_grader_contract():
    grader: VisionGrader = RecordedVision()
    reply = grader.look(image=TEMPLATE_PNG, mime="image/png", catalogue="x", idempotency_key="k",
                        cost_cap_eur=Decimal("1"))
    assert isinstance(reply, VisionReply) and reply.cost_eur is not None


def test_vision_unknown_ids_and_errors_flag(make_piece, make_ctx):
    piece = make_piece(media=((TEMPLATE_PNG, MediaProvenance.TEMPLATE_RENDER),))
    sha = piece.content.media[0].bytes_sha256
    out = run_stack(piece, make_ctx(vision=RecordedVision({sha: VisionReply(flags=(("made_up", "odd"),))})))
    [row] = [r for r in out.check_results if r.check_name == m.VISION]
    assert row.verdict is CheckVerdict.FLAG and row.reason.startswith("other:")
    out = run_stack(piece, make_ctx(vision=RecordedVision({sha: VisionReply(error="timeout")})))
    assert [r.verdict for r in out.check_results if r.check_name == m.VISION_OFF] == [CheckVerdict.FLAG]


def test_bytes_that_do_not_match_their_hash_are_not_read(make_piece, make_ctx):
    piece = make_piece(media=((SHARP_PHOTO, MediaProvenance.CLIENT_PHOTO),))
    sha = piece.content.media[0].bytes_sha256
    out = run_stack(replace(piece, media_bytes={sha: TEMPLATE_PNG}), make_ctx())
    [row] = [r for r in out.check_results if r.check_name == PHOTO_CHECK]
    assert row.verdict is CheckVerdict.FAIL and "not read" in row.reason


class Breaks:
    """A Claude-shaped first reply, then an error that is not a provider error."""

    def __init__(self):
        self.inner, self.calls = ClaudeScript(), 0

    def __call__(self, call):
        if call.method != "GET":
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("transport bug")
        return self.inner(call)


def test_an_unexpected_error_keeps_earlier_costs_and_flags(make_piece, make_ctx, clock, vault):
    vendors = Vendors()
    vendors.grader = Breaks()  # type: ignore[assignment]
    out = run_stack(make_piece(), make_ctx(registry=make_registry(clock, vault, vendors),
                                           settings=Settings(second_reader=False)))
    assert len(out.cost_entries) == 1
    assert ("grader.specific.not_run", CheckVerdict.FLAG) in {(r.check_name, r.verdict) for r in out.check_results}


def test_a_raising_repair_goes_to_a_person_and_keeps_its_costs(make_piece, make_ctx, clock):
    class Raising:
        def __init__(self):
            self.pending = []

        def repair(self, piece, failing):
            self.pending.append(CostEntry(id=new_id(clock), created_at=clock.now(), tenant_id=T,
                                          capability=Capability.TEXT, provider="claude", cost_eur=Decimal("0.02"),
                                          run_ref="repair", occurred_at=clock.now()))
            raise RuntimeError("boom")

        def drain_costs(self):
            out, self.pending = self.pending, []
            return out

    out = run_stack(make_piece(DIGIT, rendered=DIGIT), make_ctx(), repair=Raising())
    assert out.status is StackStatus.NEEDS_HUMAN and "repair raised" in out.note
    assert [e.run_ref for e in out.cost_entries] == ["repair"]


def test_text_changed_after_filling_is_a_flag_not_a_fail(make_piece, make_ctx, price, clock):
    source = "A loaf is {{C-0001}} this weekend."
    filled = build(source, tenant_id=T, language=Language.EN, claims={"C-0001": price}, clock=clock,
                   default_language=Language.EN).filled.text
    out = run_stack(make_piece(source, rendered=filled + " Order by Friday."), make_ctx())
    assert [r.verdict for r in out.check_results if r.check_name == "truth.fill"] == [CheckVerdict.FLAG]
