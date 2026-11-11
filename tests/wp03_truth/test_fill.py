import pytest

from cortex_operator.contracts.enums import ClaimKind, Language
from cortex_operator.truth import FillError, fill
from cortex_operator.truth.fill import format_number
from tests.wp03_truth.conftest import DAY

EN, IT, TA = Language.EN, Language.IT, Language.TA


@pytest.mark.parametrize(("value", "language", "out"), [
    ("3.5", EN, "3.5"), ("3.5", IT, "3,5"), ("3.5", TA, "3.5"),
    ("1234567.50", EN, "1,234,567.50"), ("1234567.50", IT, "1.234.567,50"), ("1234567.50", TA, "12,34,567.50"),
    ("999", TA, "999"), ("100000", TA, "1,00,000"), ("-1200", IT, "-1.200"),
])
def test_number_formatting_keeps_every_digit(value, language, out):
    assert format_number(value, language) == out


def one(make, clock, kind, value, unit=None, language=EN, default=EN, **kw):
    claim = make("C-0001", kind, value, unit, **kw)
    return fill("{{C-0001}}", language=language, claims={"C-0001": claim}, clock=clock,
                default_language=default).text


@pytest.mark.parametrize(("value", "unit", "language", "out"), [
    ("3.5", "t", EN, "3.5 tonnes"), ("3.5", "t", IT, "3,5 tonnellate"), ("3.5", "t", TA, "3.5 டன்"),
    ("1", "t", EN, "1 tonne"), ("1", "t", IT, "1 tonnellata"),
    ("48", "hour", IT, "48 ore"), ("30", "day", TA, "30 நாட்கள்"), ("1", "year", TA, "1 ஆண்டு"),
    ("12.5", "%", IT, "12,5%"), ("120", "km", IT, "120 km"), ("40", None, IT, "40"),
])
def test_numbers_with_units(make, clock, value, unit, language, out):
    assert one(make, clock, ClaimKind.NUMBER, value, unit, language) == out


@pytest.mark.parametrize(("value", "unit", "language", "out"), [
    ("1234.50", "EUR", EN, "€1,234.50"), ("1234.50", "EUR", IT, "1.234,50 €"),
    ("245000", "INR", TA, "₹2,45,000"), ("99", "CHF", EN, "CHF 99"), ("99", "CHF", IT, "99 CHF"),
])
def test_prices(make, clock, value, unit, language, out):
    assert one(make, clock, ClaimKind.PRICE, value, unit, language) == out


@pytest.mark.parametrize(("value", "language", "out"), [
    ("2026-01-05", EN, "5 January 2026"), ("2026-01-05", IT, "5 gennaio 2026"), ("2026-01-05", TA, "5 ஜனவரி 2026"),
    ("2026-03", IT, "marzo 2026"), ("1998", TA, "1998"),
])
def test_dates(make, clock, value, language, out):
    assert one(make, clock, ClaimKind.DATE, value, language=language) == out


def test_approved_wording_wins(make, clock):
    out = one(make, clock, ClaimKind.NUMBER, "3.5", "t", TA, languages={"ta": "மூன்றரை டன்"})
    assert out == "மூன்றரை டன்"


def test_names_are_not_translated(make, clock):
    assert one(make, clock, ClaimKind.NAME, "Rossi Trasporti", language=TA) == "Rossi Trasporti"


def test_quote_needs_approved_wording_outside_the_default_language(make, clock):
    assert one(make, clock, ClaimKind.QUOTE, "Never late.", language=EN, default=EN) == "Never late."
    with pytest.raises(FillError, match="no approved it wording"):
        one(make, clock, ClaimKind.CAPABILITY, "Delivers on Sundays", language=IT, default=EN)


def test_unknown_unit_outside_the_default_language_refuses(make, clock):
    assert one(make, clock, ClaimKind.NUMBER, "3", "trucks", EN) == "3 trucks"
    with pytest.raises(FillError, match="unit 'trucks'"):
        one(make, clock, ClaimKind.NUMBER, "3", "trucks", IT)


def test_fill_refuses_whole_text_on_any_bad_slot(make, clock):
    good = make("C-0001", ClaimKind.NUMBER, "3.5", "t")
    pending = make("C-0002", ClaimKind.NUMBER, "40", verified=False)
    claims = {c.code: c for c in (good, pending)}
    with pytest.raises(FillError) as err:
        fill("{{C-0001}} and {{C-0002}} and {{C-0003}}", language=EN, claims=claims, clock=clock,
             default_language=EN)
    assert len(err.value.problems) == 2


def test_fill_refuses_expired_price(make, clock):
    price = make("C-0001", ClaimKind.PRICE, "9.99", "EUR")
    clock.advance(30 * DAY)
    with pytest.raises(FillError, match="expired"):
        fill("{{C-0001}}", language=EN, claims={"C-0001": price}, clock=clock, default_language=EN)


def test_fill_reports_codes_and_ids_in_order(make, clock):
    a = make("C-0001", ClaimKind.NUMBER, "1", "t")
    b = make("C-0002", ClaimKind.NAME, "Acme")
    out = fill("{{C-0002}} moves {{C-0001}}; {{C-0002}} again.", language=EN, claims={"C-0001": a, "C-0002": b},
               clock=clock, default_language=EN)
    assert out.text == "Acme moves 1 tonne; Acme again."
    assert out.codes == ("C-0002", "C-0001") and out.claim_ids == (b.id, a.id)
