"""The stack end to end on memory: order, coverage as the gate decides it, media rules, cost exactly
once, the cross-family rule, the repair limit, the vision layer and dropped graders."""
from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from thallo.checks import MAX_REPAIR_LOOPS, Settings, StackStatus, VisionReply, run_stack, with_text, write_stack
from thallo.checks import models as m
from thallo.contracts.enums import CheckLayer, CheckVerdict, MediaProvenance
from thallo.contracts.ids import new_id
from thallo.contracts.models import CheckResult
from thallo.contracts.testing.samples import T
from thallo.gate.checks import PHOTO_ATTESTATION, PHOTO_CHECK, PROVENANCE_CHECK, check_hash

from .conftest import (
    NO_LINKS,
    SHARP_PHOTO,
    SOURCE,
    TEMPLATE_PNG,
    ClaudeScript,
    GeminiScript,
    RecordedVision,
    Vendors,
    gate_review,
    make_registry,
)

DIGIT = "Our sourdough costs 12 euros this weekend."


def names(rows, layer=None):
    return {r.check_name for r in rows if layer is None or r.layer is layer}


def test_order_is_code_then_graders_then_second_reader(make_piece, make_ctx, vendors):
    out = run_stack(make_piece(media=((TEMPLATE_PNG, MediaProvenance.TEMPLATE_RENDER),)),
                    make_ctx(vision=RecordedVision()))
    assert out.status is StackStatus.READY_FOR_REVIEW
    rows = out.check_results
    layers = [r.layer for r in sorted(rows, key=lambda r: r.id)]
    assert layers == sorted(layers, key=[CheckLayer.CODE, CheckLayer.GRADER, CheckLayer.SECOND_READER].index)
    assert [r.id for r in rows] == sorted(r.id for r in rows)
    assert {"truth.compile", "gate.links", "copy.slop", "copy.sameness", "copy.length", "copy.pii",
            "copy.injection", "design.contrast"} <= names(rows, CheckLayer.CODE)
    assert {"grader.voice", "grader.specific", "grader.vision"} <= names(rows, CheckLayer.GRADER)
    assert names(rows, CheckLayer.SECOND_READER) >= {"second_reader", "second_reader.independence"}
    # Every row is bound to the hash approval computes for the finished piece.
    sha = check_hash(out.piece.content, resolver=NO_LINKS, non_links=())
    assert {r.content_sha256 for r in rows} == {sha}
    # Untrusted material travels as data, never in the instructions.
    assert SOURCE not in vendors.grader.system(0) and SOURCE in vendors.grader.user(0)
    review = gate_review(out.piece, rows)
    assert not review.unchecked and not review.failing


def test_graders_and_reader_never_fail_and_never_cover(make_piece, make_ctx):
    out = run_stack(make_piece(), make_ctx())
    for r in out.check_results:
        if r.layer is not CheckLayer.CODE:
            assert r.verdict is not CheckVerdict.FAIL
    code_only = [r for r in out.check_results if r.layer is not CheckLayer.CODE]
    assert gate_review(out.piece, code_only).unchecked  # grader rows never satisfy coverage


def test_only_truth_rows_or_only_design_rows_is_refused(make_piece, make_ctx):
    out = run_stack(make_piece(media=((TEMPLATE_PNG, MediaProvenance.TEMPLATE_RENDER),)), make_ctx())
    rows = out.check_results
    truth_only = [r for r in rows if r.check_name.startswith("truth.compile")]
    design_only = [r for r in rows if r.check_name.startswith("design.")]
    assert [k.value for k in gate_review(out.piece, truth_only).missing[out.piece.content.variant.id]][0] \
        .startswith("media.template_render.")
    assert [k.value for k in gate_review(out.piece, design_only).missing[out.piece.content.variant.id]] == ["text"]
    assert not gate_review(out.piece, truth_only + design_only).unchecked


def test_client_photo_needs_its_photo_check_and_an_acknowledged_attestation(make_piece, make_ctx):
    out = run_stack(make_piece(media=((SHARP_PHOTO, MediaProvenance.CLIENT_PHOTO),)), make_ctx())
    rows = out.check_results
    sha = out.piece.content.media[0].bytes_sha256
    att = [r for r in rows if r.check_name == PHOTO_ATTESTATION]
    assert len(att) == 1 and att[0].verdict is CheckVerdict.FLAG and att[0].media_sha256 == sha
    review = gate_review(out.piece, rows)
    assert not review.unchecked and att[0].id in review.flags  # the approver must acknowledge it
    assert gate_review(out.piece, [r for r in rows if r.check_name != PHOTO_ATTESTATION]).unchecked
    assert gate_review(out.piece, [r for r in rows if not r.check_name.startswith(PHOTO_CHECK + ".")
                                   and r.check_name != PHOTO_CHECK]).unchecked
    # A design lint row for the photo's bytes is not a photo check.
    lint_instead = [r.model_copy(update={"check_name": "design.contrast"}) if r.check_name.startswith(PHOTO_CHECK)
                    and r.check_name != PHOTO_ATTESTATION else r for r in rows]
    assert gate_review(out.piece, lint_instead).unchecked


def test_photo_that_cannot_be_measured_fails(make_piece, make_ctx):
    out = run_stack(make_piece(media=((b"\xff\xd8\xff jpeg", MediaProvenance.CLIENT_PHOTO),), mime="image/jpeg"),
                    make_ctx())
    assert out.status is StackStatus.NEEDS_HUMAN and "media.photo" in out.note
    assert [r.verdict for r in out.check_results if r.check_name == PHOTO_CHECK] == [CheckVerdict.FAIL]


def test_unknown_source_never_satisfies_coverage(make_piece, make_ctx):
    out = run_stack(make_piece(media=((TEMPLATE_PNG, MediaProvenance.CLIENT_GRAPHIC),)), make_ctx())
    assert out.status is StackStatus.NEEDS_HUMAN and "media.source" in out.note
    review = gate_review(out.piece, out.check_results)
    assert review.unchecked and review.failing


@pytest.mark.parametrize("source", [MediaProvenance.GENERATED, MediaProvenance.GENERATED_BACKGROUND])
def test_generated_imagery_fails_unless_enabled_then_always_flags(make_piece, make_ctx, source):
    piece = make_piece(media=((TEMPLATE_PNG, source),))
    off = run_stack(piece, make_ctx())
    assert [r.verdict for r in off.check_results if r.check_name == PROVENANCE_CHECK] == [CheckVerdict.FAIL]
    assert gate_review(piece, off.check_results).failing
    on = run_stack(piece, make_ctx(settings=Settings(allow_generated_media=True), vision=RecordedVision()))
    [row] = [r for r in on.check_results if r.check_name == PROVENANCE_CHECK]
    assert row.verdict is CheckVerdict.FLAG
    review = gate_review(piece, on.check_results)
    assert not review.unchecked and not review.failing and row.id in review.flags


def _billed(out):
    return [e for e in out.cost_entries]


def test_every_billed_call_writes_exactly_one_cost_row(make_piece, make_ctx, vendors):
    out = run_stack(make_piece(), make_ctx())
    calls = len(vendors.grader.calls) + len(vendors.reader.calls)
    assert calls == 3 and len(_billed(out)) == 3
    assert len({e.run_ref for e in out.cost_entries}) == 3
    assert out.spent_eur == sum(e.cost_eur for e in out.cost_entries) > 0


def test_a_result_over_the_cap_after_the_call_is_ledgered_once(make_piece, make_ctx, clock, vault):
    vendors = Vendors(grader=ClaudeScript(output_tokens=900_000))
    ctx = make_ctx(registry=make_registry(clock, vault, vendors), settings=Settings(second_reader=False))
    out = run_stack(make_piece(), ctx)
    assert len(vendors.grader.calls) >= 1
    assert len(out.cost_entries) == len(vendors.grader.calls)  # billed, refused after, still one row each
    flags = [r for r in out.check_results if r.check_name.endswith(m.NOT_RUN)]
    assert flags and all(r.verdict is CheckVerdict.FLAG for r in flags)


def test_a_billed_provider_error_is_ledgered_and_flags(make_piece, make_ctx, clock, vault):
    vendors = Vendors(grader=ClaudeScript(stop="max_tokens"))
    ctx = make_ctx(registry=make_registry(clock, vault, vendors), settings=Settings(second_reader=False))
    out = run_stack(make_piece(), ctx)
    assert len(vendors.grader.calls) == 2 and len(out.cost_entries) == 2
    assert names(out.check_results, CheckLayer.GRADER) == {"grader.voice.not_run", "grader.specific.not_run"}


def test_the_cap_is_respected_before_any_call(make_piece, make_ctx, vendors):
    out = run_stack(make_piece(), make_ctx(settings=Settings(cost_cap_eur=Decimal("0.000001"))))
    assert vendors.grader.calls == [] and vendors.reader.calls == [] and out.cost_entries == ()
    assert all(r.verdict is CheckVerdict.FLAG for r in out.check_results if r.layer is not CheckLayer.CODE)


def test_grader_concerns_and_unreadable_replies_flag(make_piece, make_ctx, clock, vault):
    concern = json.dumps({"flags": [{"issue": "generic", "quote": "comes out of the oven"}]})
    vendors = Vendors(grader=ClaudeScript(concern, "not json at all"))
    out = run_stack(make_piece(), make_ctx(registry=make_registry(clock, vault, vendors)))
    got = {(r.check_name, r.verdict) for r in out.check_results if r.layer is CheckLayer.GRADER}
    assert got == {("grader.voice", CheckVerdict.FLAG), ("grader.specific.not_run", CheckVerdict.FLAG)}
    assert out.status is StackStatus.READY_FOR_REVIEW


def test_second_reader_must_be_another_family(make_piece, make_ctx, clock, vault):
    vendors = Vendors()
    reg = make_registry(clock, vault, vendors, reader_provider="claude", reader_model="claude-opus-5-5")
    out = run_stack(make_piece(), make_ctx(registry=reg))
    [row] = [r for r in out.check_results if r.layer is CheckLayer.SECOND_READER]
    assert row.check_name == m.INDEPENDENCE and row.verdict is CheckVerdict.FLAG
    assert len(vendors.grader.calls) == 2  # the graders only: no same-family read was bought


def test_a_same_family_answer_is_not_used_but_is_paid_for(make_piece, make_ctx, clock, vault):
    unsupported = json.dumps({"unsupported": [{"sentence": "x"}]})
    vendors = Vendors(reader=GeminiScript(unsupported, model="claude-sonnet-5-5"))
    out = run_stack(make_piece(), make_ctx(registry=make_registry(clock, vault, vendors)))
    rows = [r for r in out.check_results if r.layer is CheckLayer.SECOND_READER]
    assert [(r.check_name, r.verdict) for r in rows] == [(m.INDEPENDENCE, CheckVerdict.FLAG)]
    assert len(out.cost_entries) == 3


def test_second_reader_flags_unsupported_sentences(make_piece, make_ctx, clock, vault):
    unsupported = json.dumps({"unsupported": [{"sentence": "before the shop opens", "should_be_backed_by": "hours"}]})
    vendors = Vendors(reader=GeminiScript(unsupported))
    out = run_stack(make_piece(), make_ctx(registry=make_registry(clock, vault, vendors)))
    got = [(r.check_name, r.verdict) for r in out.check_results if r.layer is CheckLayer.SECOND_READER]
    assert got == [(m.INDEPENDENCE, CheckVerdict.PASS), (m.SECOND_READER, CheckVerdict.FLAG)]


class Repairer:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.seen = []

    def repair(self, piece, failing):
        self.seen.append(sorted({r.check_name for r in failing}))
        if not self.texts:
            return None
        t = self.texts.pop(0)
        return with_text(piece, text=t, rendered_text=t)


def test_repair_stops_after_three_loops_and_names_the_failing_check(make_piece, make_ctx, vendors):
    rep = Repairer(*(f"Our sourdough costs {n} euros now." for n in (13, 14, 15, 16)))
    out = run_stack(make_piece(DIGIT, rendered=DIGIT), make_ctx(), repair=rep)
    assert out.status is StackStatus.NEEDS_HUMAN and len(out.runs) == MAX_REPAIR_LOOPS + 1
    assert "truth.compile" in out.note and "after 3 repair loop" in out.note
    attempts = sorted(r.attempt for r in out.check_results if r.check_name == "truth.compile")
    assert attempts == [1, 2, 3, 4]
    assert vendors.grader.calls == []  # nothing spent on a piece going back for repair


def test_repair_that_fixes_the_piece_clears_it(make_piece, make_ctx, price, clock):
    from thallo.contracts.enums import Language
    from thallo.truth import build
    clean = "A loaf is {{C-0001}} this weekend, baked before dawn."
    filled = build(clean, tenant_id=T, language=Language.EN, claims={"C-0001": price}, clock=clock,
                   default_language=Language.EN).filled.text

    class Fix:
        def repair(self, piece, failing):
            p = with_text(piece, text=clean, rendered_text=filled)
            return replace(p, content=replace(p.content, claims=(price,)))

    out = run_stack(make_piece(DIGIT, rendered=DIGIT), make_ctx(), repair=Fix())
    assert out.status is StackStatus.READY_FOR_REVIEW and len(out.runs) == 2
    review = gate_review(out.piece, out.check_results)
    assert not review.failing and not review.unchecked


def test_attempts_rise_above_rows_already_written(make_piece, make_ctx, clock):
    piece = make_piece()
    sha = check_hash(piece.content, resolver=NO_LINKS, non_links=())
    old = [CheckResult(id=new_id(clock), created_at=clock.now(), tenant_id=T, variant_id=piece.content.variant.id,
                       layer=CheckLayer.CODE, check_name="truth.compile", verdict=CheckVerdict.FAIL, attempt=4,
                       content_sha256=sha)]
    out = run_stack(piece, make_ctx(existing=old))
    [row] = [r for r in out.check_results if r.check_name == "truth.compile"]
    assert row.attempt == 5 and row.id > old[0].id
    assert not gate_review(piece, [*old, *out.check_results]).failing  # the newer, higher PASS clears it


def test_vision_grader_flags_from_the_catalogue_and_bills_once(make_piece, make_ctx):
    piece = make_piece(media=((TEMPLATE_PNG, MediaProvenance.TEMPLATE_RENDER),))
    sha = piece.content.media[0].bytes_sha256
    vision = RecordedVision({sha: VisionReply(flags=(("default_gradient", "two-tone blue"),), provider="recorded",
                                              model="vision-fixture", cost_eur=Decimal("0.003"), units=800)})
    out = run_stack(piece, make_ctx(vision=vision))
    [row] = [r for r in out.check_results if r.check_name == m.VISION]
    assert row.verdict is CheckVerdict.FLAG and row.media_sha256 == sha and "default_gradient" in row.reason
    assert "malformed_hands" in vision.calls[0][1]  # it was shown the catalogue
    assert sum(1 for e in out.cost_entries if e.provider == "recorded") == 1


def test_a_failing_vision_grader_flags_and_keeps_its_billed_cost(make_piece, make_ctx):
    class Billed(RuntimeError):
        cost_eur = Decimal("0.004")
        provider = "recorded"

    out = run_stack(make_piece(media=((TEMPLATE_PNG, MediaProvenance.TEMPLATE_RENDER),)),
                    make_ctx(vision=RecordedVision(raises=Billed("boom"))))
    assert [r.verdict for r in out.check_results if r.check_name == m.VISION_OFF] == [CheckVerdict.FLAG]
    assert sum(1 for e in out.cost_entries if e.provider == "recorded") == 1


def test_no_vision_grader_is_a_flag_not_a_pass(make_piece, make_ctx):
    out = run_stack(make_piece(media=((TEMPLATE_PNG, MediaProvenance.TEMPLATE_RENDER),)), make_ctx())
    assert [r.verdict for r in out.check_results if r.check_name == m.VISION_OFF] == [CheckVerdict.FLAG]


def test_a_dropped_grader_writes_no_flags_and_is_reported(make_piece, make_ctx, vendors):
    out = run_stack(make_piece(), make_ctx(settings=Settings(dropped_graders=frozenset({"grader.voice"}))))
    assert "grader.voice" not in names(out.check_results)
    assert len(vendors.grader.calls) == 1 and out.dropped and "grader.voice" in out.dropped[0]


def test_grader_names_cannot_take_the_reserved_suffix():
    with pytest.raises(ValueError):
        m.TextGrader("grader.voice.not_run", "x")


def test_model_families():
    assert m.model_family("claude-sonnet-5-5") == "anthropic" and m.model_family("gemini-3.1-pro") == "google"
    assert m.model_family("gpt-5") == "openai" and m.model_family("o3-mini") == "openai"
    assert m.model_family("ollama/llama3") == "meta" and m.model_family("orca") is None
    assert m.model_family(None) is None


class FakeConn:
    def __init__(self):
        self.opened = 0

    def transaction(self):
        conn = self

        class Tx:
            def __enter__(self):
                conn.opened += 1

            def __exit__(self, *a):
                return False
        return Tx()


class FakeRepo:
    def __init__(self, tenant_id):
        self.tenant_id, self.conn, self.rows = tenant_id, FakeConn(), []

    def insert(self, row):
        self.rows.append(row)


def test_write_stack_is_one_transaction_and_refuses_another_tenant(make_piece, make_ctx):
    out = run_stack(make_piece(), make_ctx())
    repo = FakeRepo(T)
    write_stack(repo, out)
    assert repo.conn.opened == 1 and len(repo.rows) == len(out.check_results) + len(out.cost_entries)
    with pytest.raises(ValueError):
        write_stack(FakeRepo("00000000-0000-7000-8000-000000000000"), out)
