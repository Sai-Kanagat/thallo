"""Automated findings on the review fix: one-shot claim iterators, overlong words, names vs exceptions."""
import pytest

from thallo.contracts.enums import ClaimKind, Language
from thallo.truth import AllowedTermError, FindingKind, build

EN, IT, TA = Language.EN, Language.IT, Language.TA


# 1. build reads `claims` once, so compile and fill see the same rows

def test_build_accepts_a_one_shot_iterator(make, clock, tenant):
    claim = make("C-0001", ClaimKind.NUMBER, "3", "t")
    out = build("Carries {{C-0001}}.", tenant_id=tenant, language=EN, claims=iter([claim]), clock=clock,
                default_language=EN)
    assert out.filled.text == "Carries 3 tonnes."


def test_build_fills_from_the_rows_it_compiled(make, clock, tenant, human, verifier, repo):
    from thallo.truth import CompileFailed, change_claim
    old = make("C-0001", ClaimKind.NUMBER, "3", "t")
    ch = change_claim(old, actor=human, verifier=verifier, clock=clock, repo=repo, value="4")
    rows = iter([old, ch.current])  # the newer row is a candidate: compile must refuse, fill never runs
    with pytest.raises(CompileFailed):
        build("Carries {{C-0001}}.", tenant_id=tenant, language=EN, claims=rows, clock=clock, default_language=EN)


# 2. a word too long to parse blocks instead of passing unchecked

@pytest.mark.parametrize(("language", "word"), [
    (EN, "twenty" * 12 + "five"), (IT, "trenta" * 12 + "cinque"), (TA, "அ" * 70), (EN, "a" * 65),
])
def test_overlong_word_blocks(run, language, word):
    result = run(f"We have {word} here.", language)
    assert FindingKind.OVERLONG_WORD in {f.kind for f in result.blocks}, word


def test_ordinary_long_words_pass(run):
    assert run("Precipitevolissimevolmente is long but under the cap.", EN).ok


# 3. fixed phrases and allowed terms never silence a protected name

@pytest.fixture
def names(make):
    return {c.code: c for c in (make("C-0003", ClaimKind.NAME, "Rossi Trasporti"),
                                make("C-0004", ClaimKind.NAME, "Top Up Srl"))}


def test_fixed_phrase_does_not_silence_a_name(run, names):
    result = run("Top up with Top Up Srl.", claims=names)
    assert FindingKind.NAMED_CUSTOMER in {f.kind for f in result.blocks}


def test_allowed_term_overlapping_a_protected_name_is_refused(run, names):
    for term in ("Rossi", "Rossi Trasporti", "rossi trasporti srl", "Trasporti Rossi"):
        with pytest.raises(AllowedTermError, match="protected name"):
            run("Ask Rossi.", claims=names, allowed_terms=frozenset({term}))
    assert run("Fiat Seicento.", IT, claims=names, allowed_terms=frozenset({"Seicento"})).ok
