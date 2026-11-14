import time

import pytest

from tests.wp03_truth.conftest import DAY
from thallo.contracts.enums import CheckLayer, CheckVerdict, ClaimKind, Language
from thallo.contracts.events import ClaimChangedPayload, EventName
from thallo.contracts.ids import new_id
from thallo.contracts.states import ClaimState
from thallo.truth import (
    ClaimError,
    FindingKind,
    Severity,
    build,
    change_claim,
    library,
    to_check_results,
    verify_claim,
)
from thallo.truth.compiler import MAX_TEXT_CHARS

EN, IT, TA = Language.EN, Language.IT, Language.TA


@pytest.fixture
def lib(make):
    return {c.code: c for c in (make("C-0001", ClaimKind.NUMBER, "10", "year"),
                                make("C-0002", ClaimKind.NAME, "Acme"))}


def kinds(result, severity):
    return [f.kind for f in result.findings if f.severity is severity]


# ---------------------------------------------------------------- review-flag tier

@pytest.mark.parametrize(("language", "text"), [
    (IT, "Sei pronto?"), (IT, "Prima di tutto, chiamaci."), (IT, "Secondo me, è la scelta giusta."),
    (EN, "The one you need."), (EN, "One of a kind."),
])
def test_ambiguous_words_that_count_nothing_produce_nothing(run, language, text):
    """Review ruling L2: no finding at all, so approvers are not trained to tap through."""
    assert run(text, language).findings == (), text


@pytest.mark.parametrize(("language", "text"), [(IT, "Prima in Emilia."), (EN, "First in Chennai."),
                                                (IT, "La prima e unica.")])
def test_rank_words_that_count_nothing_flag(run, language, text):
    result = run(text, language)
    assert result.ok and result.flags, text


@pytest.mark.parametrize(("language", "text"), [
    (IT, "Sei clienti."), (IT, "Sei nuovi clienti."), (IT, "Sei anni di garanzia."), (IT, "Sedi: una in città."),
    (IT, "uno {{C-0001}}"), (IT, "Prima 5 auto."), (IT, "Sei km."), (IT, "Una sola nuova sede."),
    (IT, "Un anno di garanzia."), (IT, "Sedi: una a Bologna."),
    (EN, "We sold one truck."), (EN, "One more happy customer."), (EN, "one {{C-0001}}"), (EN, "One km away."),
    (EN, "one-year warranty"),
])
def test_ambiguous_words_near_a_count_block(run, lib, language, text):
    assert not run(text, language, claims=lib).ok, text


@pytest.mark.parametrize(("language", "text"), [
    (EN, "It never breaks down."), (EN, "Every price is clear."), (IT, "Sempre aperti, mai un guasto."),
    (IT, "Tutti i prezzi sono chiari."), (TA, "எப்போதும் நல்ல விலை."),
])
def test_universal_claims_flag(run, language, text):
    result = run(text, language)
    assert result.ok and FindingKind.UNIVERSAL_CLAIM in kinds(result, Severity.FLAG), result.findings


def test_build_returns_flags_with_spans(built, lib, clock, tenant):
    out = build("{{C-0002}} never breaks down. Il top.", tenant_id=tenant, language=IT, claims=lib, clock=clock,
                default_language=EN)
    assert out.filled.text == "Acme never breaks down. Il top."
    flagged = {(f.kind, out.compiled.text[f.start:f.end]) for f in out.flags}
    assert flagged == {(FindingKind.UNIVERSAL_CLAIM, "never"), (FindingKind.SUPERLATIVE, "top")}


def test_check_results_carry_fail_flag_and_pass(run, clock, tenant):
    variant = new_id(clock)
    first, second = "a1" * 32, "b2" * 32
    rows = to_check_results(run("Il top. The best.", IT), variant_id=variant, content_sha256=first, clock=clock)
    assert {(r.verdict, r.layer) for r in rows} == {(CheckVerdict.FLAG, CheckLayer.CODE),
                                                    (CheckVerdict.FAIL, CheckLayer.CODE)}
    assert all(r.tenant_id == tenant and r.variant_id == variant and r.reason.startswith("[") for r in rows)
    assert all(r.content_sha256 == first for r in rows)
    [ok] = to_check_results(run("Calm weeks."), variant_id=variant, content_sha256=second, clock=clock)
    assert ok.verdict is CheckVerdict.PASS and ok.content_sha256 == second
    with pytest.raises(ValueError):  # a row must name real content
        to_check_results(run("Calm weeks."), variant_id=variant, content_sha256="unbound-before-0021", clock=clock)


def test_legacy_gate_ignores_flags():
    from thallo.contracts.clock import FAKE_EPOCH
    from thallo.truth import ClaimLedger
    assert ClaimLedger().compile("Always calm weeks.", FAKE_EPOCH) == ([], [])


# ---------------------------------------------------------------- multipliers on folded text

@pytest.mark.parametrize("text", ["{{C-0001}} ｋ customers", "{{C-0001}} × the output", "{{C-0001}}×",
                                  "{{C-0001}} ✕ more", "{{C-0001}} ｘ faster", "{{C-0001}} MLN units"])
def test_multipliers_and_symbols_after_a_slot_block(run, lib, text):
    assert FindingKind.SLOT_ADJACENT in [f.kind for f in run(text, claims=lib).findings], text


# ---------------------------------------------------------------- ReDoS and length cap

@pytest.mark.parametrize(("language", "text"), [
    (IT, "cento" * 3000 + "x"), (IT, "trent" * 3000 + "z"), (IT, "a" * 19000 + "issimx"),
    (EN, "b e " * 4000 + "!"), (IT, "più " * 4000), (EN, "x" * 19990), (TA, "பது" * 5000),
])
def test_adversarial_input_finishes_fast(run, lib, language, text):
    run("warm up", language)
    start = time.perf_counter()
    run(text, language, claims=lib)
    assert time.perf_counter() - start < 0.1 or len(text) > 10000 and time.perf_counter() - start < 1.0


def test_italian_number_parse_is_linear():
    from thallo.truth.lexicon import italian_number_word
    start = time.perf_counter()
    for _ in range(200):
        italian_number_word("cento" * 12 + "q")
    assert time.perf_counter() - start < 0.1


def test_text_over_the_cap_is_refused(run):
    result = run("a" * (MAX_TEXT_CHARS + 1))
    assert [f.kind for f in result.findings] == [FindingKind.TEXT_TOO_LONG] and not result.ok


# ---------------------------------------------------------------- versions (contracts 2.0.0)

def test_change_writes_a_new_version_and_voids_the_old(make, clock, tenant, human, verifier, run, repo):
    claim = make("C-0001", ClaimKind.PRICE, "9.99", "EUR")
    repo.link(tenant, "v1", claim.id)
    repo.approve(tenant, "a1", ["v1"])
    ch = change_claim(claim, actor=human, verifier=verifier, clock=clock, repo=repo, value="10.49")
    assert ch.previous.state is ClaimState.VOIDED and ch.previous.value == "9.99"
    assert (ch.current.version, ch.current.supersedes_id, ch.current.state) == (2, claim.id, ClaimState.CANDIDATE)
    assert [e.name for e in ch.events] == [EventName.CLAIM_CHANGED, EventName.CLAIM_CANDIDATE_ADDED]
    payload = ClaimChangedPayload.model_validate(ch.events[0].payload)
    assert payload.voided_approval_ids == ["a1"] and payload.voided_variant_ids == ["v1"]
    repo.save(ch.previous)
    repo.save(ch.current)
    assert [c.version for c in repo.history(tenant, "C-0001")] == [1, 2]
    assert repo.latest(tenant, "C-0001").id == ch.current.id
    assert not run("{{C-0001}}", claims=library(repo, tenant)).ok  # candidate until a human verifies
    v2, _ = verify_claim(ch.current, human, verifier=verifier, clock=clock, repo=repo)
    repo.save(v2)
    assert run("{{C-0001}}", claims=library(repo, tenant)).ok
    with pytest.raises(ValueError, match="append-only"):
        repo.save(ch.previous.model_copy(update={"value": "1"}))


def test_change_of_expired_candidate_voided_and_rejected(make, clock, human, verifier, repo):
    exp = make("C-0001", ClaimKind.NUMBER, "3", "t", verify_expires_at=clock.now() + DAY)
    clock.advance(DAY)
    from thallo.truth import expire_due
    [(expired, _)] = expire_due([exp], clock)
    ch = change_claim(expired, actor=human, verifier=verifier, clock=clock, repo=repo, value="4")
    assert ch.previous.state is ClaimState.VOIDED and ch.current.version == 2  # O2: expired rows are voided too
    cand = make("C-0002", ClaimKind.NUMBER, "3", "t", verified=False)
    edit = change_claim(cand, actor=human, verifier=verifier, clock=clock, repo=repo, value="4")
    assert edit.previous is None and edit.current.version == 1 and edit.current.value == "4"
    voided = change_claim(make("C-0003", ClaimKind.NUMBER, "3", "t"), actor=human, verifier=verifier, clock=clock,
                          repo=repo,
                          value="4").previous
    with pytest.raises(ClaimError, match="latest version"):
        change_claim(voided, actor=human, verifier=verifier, clock=clock, repo=repo, value="5")


def test_price_prints_in_its_own_currency(built, make):
    claim = make("C-0001", ClaimKind.PRICE, "245000", "INR")
    assert built("{{C-0001}}", {"C-0001": claim}, EN) == "₹2,45,000"  # Indian grouping for INR


# ---------------------------------------------------------------- expiry_reason (contracts 2.1.0)

def test_expiry_past_ninety_days_needs_a_reason(make, clock, human, verifier, repo):
    claim = make("C-0001", ClaimKind.PRICE, "9.99", "EUR", verified=False)
    far = clock.now() + 180 * DAY
    for reason in (None, "", "   "):
        with pytest.raises(ClaimError, match="expiry_reason"):
            verify_claim(claim, human, verifier=verifier, clock=clock, repo=repo, expires_at=far, expiry_reason=reason)
    ok, _ = verify_claim(claim, human, verifier=verifier, clock=clock, repo=repo, expires_at=far,
                         expiry_reason="Annual list price, signed contract")
    assert ok.expires_at == far and ok.expiry_reason == "Annual list price, signed contract"
    short, _ = verify_claim(claim, human, verifier=verifier, clock=clock, repo=repo, expiry_reason="ignored")
    assert short.expiry_reason is None
