"""O4: the review's 20,000-character worst cases finish in under 150 ms each."""
import time

import pytest

from cortex_operator.contracts.enums import ClaimKind, Language
from cortex_operator.truth.compiler import MAX_TEXT_CHARS

EN, IT, TA = Language.EN, Language.IT, Language.TA

CASES = [
    (EN, "Our trucks are reliable and sturdy. " * 600), (EN, "{{C-0001}} one " * 1400), (EN, "one " * 5000),
    (IT, "sei a " * 3400), (EN, "{{C-0001}} " * 1800), (EN, "{{C-0001}} a " * 1500), (TA, "பதினைந்து " * 2000),
    (EN, "Rossi Trasporti " * 1250), (EN, "a" * 20000), (EN, "mmmm " * 4000), (EN, "1 " * 10000),
    (EN, "a b " * 5000), (IT, "{{C-0001}} " * 900 + "sei a " * 1700), (EN, "{{C-0001}} " * 900 + "one. " * 2000),
    (IT, "sei. " * 4000), (EN, "one.\n" * 4000), (EN, "{{C-0001}}" * 1000 + "abcdefghij" * 1000),
    (EN, "{{C-0001}}" * 1000 + " one." * 2000), (IT, "Anni: sei " * 2000), (EN, "b e s t " * 2500),
]


@pytest.fixture
def lib(make):
    return {c.code: c for c in (make("C-0001", ClaimKind.NUMBER, "10", "year"),
                                make("C-0003", ClaimKind.NAME, "Rossi Trasporti"))}


@pytest.mark.parametrize(("language", "text"), CASES)
def test_worst_cases_finish_in_150_ms(run, lib, language, text):
    text = text[:MAX_TEXT_CHARS]
    run("warm up", language, claims=lib)
    best = min(_timed(run, text, language, lib) for _ in range(3))
    assert best < 0.150, best


def _timed(run, text, language, lib):
    start = time.perf_counter()
    run(text, language, claims=lib)
    return time.perf_counter() - start
