"""Structural layer (S1 to S5): complete, always BLOCK, and blind to `allowed_terms`."""
import pytest

from cortex_operator.contracts.enums import ClaimKind, Language
from cortex_operator.truth import CompileFailed, FillError, FindingKind, InMemoryClaimRepository, change_claim, library
from cortex_operator.truth.compiler import check_filled
from cortex_operator.truth.fill import Filled
from tests.wp03_truth.conftest import wording

EN, IT, TA = Language.EN, Language.IT, Language.TA
NBSP, NNBSP = chr(0xA0), chr(0x202F)


@pytest.fixture
def lib(make):
    return {c.code: c for c in (make("C-0001", ClaimKind.NUMBER, "10", "year"),
                                make("C-0002", ClaimKind.NUMBER, "5", "day"),
                                make("C-0003", ClaimKind.NAME, "Rossi Trasporti"),
                                make("C-0004", ClaimKind.NUMBER, "500", "km"))}


def kinds(result):
    return {f.kind for f in result.findings}


# ---------------------------------------------------------------- S1: a letter between two slots

S1 = ["{{C-0001}} {{C-0004}} trucks.", "{{C-0001}}" + NBSP + "{{C-0004}} trucks.",
      "{{C-0001}}" + NNBSP + "{{C-0004}} trucks.", "{{C-0001}}\u2013{{C-0002}} days.", "{{C-0001}}\u2014{{C-0002}} days.",
      "{{C-0001}}·{{C-0002}} rating.", "{{C-0001}}…{{C-0002}}", "{{C-0001}}․{{C-0002}}",
      "{{C-0001}}。{{C-0002}}", "{{C-0001}}．{{C-0002}}", "{{C-0001}}٫{{C-0002}}",
      "{{C-0001}}; {{C-0002}}", "({{C-0001}}){{C-0002}}", "{{C-0001}}){{C-0002}}", "{{C-0001}}\"{{C-0002}}",
      "[{{C-0001}}]{{C-0002}}", "{{C-0001}}\n{{C-0002}}", "{{C-0001}}, {{C-0002}}", "{{C-0001}}{{C-0002}}"]


@pytest.mark.parametrize("text", S1)
def test_s1_two_slots_need_a_letter_between_them(run, lib, text):
    assert FindingKind.SLOT_ADJACENT in kinds(run(text, claims=lib)), text


def test_s1_slots_with_a_word_between_pass(run, lib):
    assert run("{{C-0001}} of service and {{C-0004}} of road.", claims=lib).ok


# ---------------------------------------------------------------- S2: around a slot

S2 = [(EN, "{{C-0001}}-fold growth."), (EN, "{{C-0001}} times the output."), (EN, "{{C-0001}} grand."),
      (EN, "{{C-0001}} b in sales."), (EN, "{{C-0001}} bln in sales."), (EN, "{{C-0001}} lac customers."),
      (EN, "{{C-0001}}-k customers"), (EN, "{{C-0001}}-plus years."), (EN, "{{C-0001}}\u2013plus years."),
      (EN, "{{C-0001}} lakh"), (EN, "{{C-0001}} cr"), (IT, "{{C-0001}} volte il fatturato."),
      (EN, "#{{C-0001}} in Emilia."), (EN, "No. {{C-0001}} dealer."), (EN, "-{{C-0001}} on every truck."),
      (EN, "x {{C-0001}} the output"), (EN, "×{{C-0001}}"), (EN, "+{{C-0001}} more"),
      (EN, "±{{C-0001}}"), (EN, "№ {{C-0001}}"), (IT, "Numero {{C-0001}} in Emilia."),
      (IT, "Il n. {{C-0001}} in Emilia."), (EN, "Nr {{C-0001}} dealer"), (EN, "Number {{C-0001}} dealer")]


@pytest.mark.parametrize(("language", "text"), S2)
def test_s2_multiplier_after_or_sign_before_a_slot_blocks(run, lib, language, text):
    assert FindingKind.SLOT_ADJACENT in kinds(run(text, language, claims=lib)), text


def test_s2_plain_words_around_a_slot_pass(run, lib):
    assert run("Over {{C-0001}} on the road, up to {{C-0004}} of range.", claims=lib).ok


# ---------------------------------------------------------------- S3: enclosed and keycap digits

S3 = ["\U0001f51f years", "\U0001f4af satisfied", "\U0001f947 dealer", "\U0001f948 dealer",
      "\U0001f171\U0001f174\U0001f182\U0001f183", "\U0001f130 dealer", "1️⃣ dealer", "#️⃣",
      "① dealer", "ⓑⓔⓢⓣ dealer", "❶ dealer"]


@pytest.mark.parametrize("text", S3)
def test_s3_enclosed_and_keycap_digits_block_in_input(run, text):
    result = run(text)
    assert not result.ok and kinds(result) & {FindingKind.ENCLOSED_DIGIT, FindingKind.DIGIT}, text


def test_s3_enclosed_digit_in_approved_wording_blocks_the_output(make, built):
    claim = make("C-0001", ClaimKind.NUMBER, "10", languages={"en": "\U0001f51f showrooms", "it": "x", "ta": "x"})
    with pytest.raises(FillError, match="enclosed"):
        built("We run {{C-0001}}.", {"C-0001": claim})


def test_s3_post_fill_digit_check_stays():
    with pytest.raises(FillError, match="outside a slot"):
        check_filled(Filled(text="10 trucks, 5 vans", codes=("C-0001",), claim_ids=("x",), slot_spans=((0, 2),)))


def test_s3_flags_of_countries_are_not_digits(run):
    assert run("Made in Italy \U0001f1ee\U0001f1f9").ok


# ---------------------------------------------------------------- S4: a unitless NUMBER needs wording

def test_s4_unitless_number_without_wording_cannot_be_verified(make):
    from cortex_operator.truth import ClaimError
    with pytest.raises(ClaimError, match="wording"):
        make("C-0001", ClaimKind.NUMBER, "10")
    with pytest.raises(ClaimError, match="wording"):
        make("C-0001", ClaimKind.NUMBER, "10", languages={"en": "10 showrooms"})  # tenant also writes it, ta
    ok = make("C-0001", ClaimKind.NUMBER, "10", languages=wording("10"))
    assert ok.languages["it"] == "10 sedi"


def test_s4_compile_blocks_a_unitless_number_slot_without_wording_for_the_language(make, run):
    claim = make("C-0001", ClaimKind.NUMBER, "10", languages={"en": "10 showrooms"}, tenant_languages=(EN,))
    assert run("We run {{C-0001}}.", EN, claims={"C-0001": claim}).ok
    result = run("Abbiamo {{C-0001}}.", IT, claims={"C-0001": claim})
    assert FindingKind.UNBOUND_NUMBER in kinds(result)


# ---------------------------------------------------------------- S5: latest live version only

def test_s5_an_older_version_in_the_rows_given_is_refused(make, run, clock, tenant, human, verifier, repo):
    old = make("C-0001", ClaimKind.NUMBER, "10", "year")
    ch = change_claim(old, actor=human, verifier=verifier, clock=clock, repo=repo, value="12")
    repo.save(ch.previous)
    repo.save(ch.current)
    rows = [old, ch.current]  # a caller who kept the in-memory verified v1
    result = run("{{C-0001}} on the road.", claims=rows)
    assert FindingKind.STALE_CLAIM in kinds(result) or FindingKind.UNUSABLE_CLAIM in kinds(result)
    assert not result.ok
    assert not run("{{C-0001}} on the road.", claims=library(repo, tenant)).ok  # v2 is a candidate


def test_s5_the_review_bypass_is_closed(make, clock, human, verifier, repo):
    """Review p6: an EXPIRED v1 stayed EXPIRED after a change and could be re-verified."""
    from cortex_operator.contracts.states import ClaimState
    from cortex_operator.truth import ClaimError, expire_due, verify_claim
    from tests.wp03_truth.conftest import DAY
    v1 = make("C-0102", ClaimKind.PRICE, "5", "EUR")
    clock.advance(31 * DAY)
    [(ex, _)] = expire_due([v1], clock)
    repo.save(ex)
    ch = change_claim(ex, actor=human, verifier=verifier, clock=clock, repo=repo, value="6")
    assert ch.previous.state is ClaimState.VOIDED
    repo.save(ch.previous)
    repo.save(ch.current)
    with pytest.raises((ClaimError, ValueError)):
        verify_claim(ex, human, verifier=verifier, clock=clock, repo=repo)


# ---------------------------------------------------------------- the build never prints a joined number

@pytest.mark.parametrize("text", ["{{C-0001}} {{C-0004}} trucks.", "{{C-0001}}\u2013{{C-0002}} days.",
                                  "#{{C-0001}} in Emilia.", "-{{C-0001}} on every truck.", "{{C-0001}}-fold growth."])
def test_review_probes_no_longer_build(built, lib, text):
    with pytest.raises(CompileFailed):
        built(text, lib)


def test_structural_rules_ignore_allowed_terms(run, lib):
    allowed = frozenset({"7", "{{C-0001}} {{C-0004}}", "fold", "\U0001f51f"})
    assert not run("We sold 7 units.", claims=lib, allowed_terms=allowed).ok
    assert not run("{{C-0001}} {{C-0004}} trucks.", claims=lib, allowed_terms=allowed).ok
    assert not run("{{C-0001}}-fold growth.", claims=lib, allowed_terms=allowed).ok
    assert not run("\U0001f51f years", claims=lib, allowed_terms=allowed).ok


# ---------------------------------------------------------------- O5: tenant currency

def test_o5_price_in_another_currency_is_refused_unless_it_says_so(make, run):
    usd = make("C-0001", ClaimKind.PRICE, "500", "USD")
    eur = make("C-0002", ClaimKind.PRICE, "500", "EUR")
    said = make("C-0003", ClaimKind.PRICE, "500", "USD", languages={"en": "USD 500"})
    claims = {c.code: c for c in (usd, eur, said)}
    assert FindingKind.FOREIGN_CURRENCY in kinds(run("Only {{C-0001}} today.", claims=claims, tenant_currency="EUR"))
    assert run("Now {{C-0002}} today.", claims=claims, tenant_currency="EUR").ok
    assert run("Now {{C-0003}} today.", claims=claims, tenant_currency="EUR").ok
    assert run("Now {{C-0001}} today.", claims=claims).ok  # no tenant currency given


def test_rows_from_another_repo_are_unaffected(make, run):
    claim = make("C-0001", ClaimKind.NUMBER, "3", "t")
    assert run("{{C-0001}} of payload.", claims=InMemoryClaimRepository([claim]).latest_for_tenant(claim.tenant_id)).ok
