import pytest

from tests.wp03_truth.conftest import DAY, wording
from thallo.contracts.enums import ClaimKind, Language
from thallo.truth import ClaimError, FillError, render_claim
from thallo.truth.fill import format_number

EN, IT, TA = Language.EN, Language.IT, Language.TA


@pytest.mark.parametrize(("value", "language", "out"), [
    ("3.5", EN, "3.5"), ("3.5", IT, "3,5"), ("3.5", TA, "3.5"),
    ("1234567.50", EN, "1,234,567.50"), ("1234567.50", IT, "1.234.567,50"), ("1234567.50", TA, "1,234,567.50"),
    ("999", TA, "999"), ("100000", TA, "100,000"), ("-1200", IT, "-1.200"),
])
def test_number_formatting_keeps_every_digit(value, language, out):
    assert format_number(value, language) == out


@pytest.fixture
def one(make, built):
    def _one(kind, value, unit=None, language=EN, default=EN, **kw):
        claim = make("C-0001", kind, value, unit, **kw)
        return built("{{C-0001}}", {"C-0001": claim}, language, default)
    return _one


@pytest.mark.parametrize(("value", "unit", "language", "out"), [
    ("3.5", "t", EN, "3.5 tonnes"), ("3.5", "t", IT, "3,5 tonnellate"), ("3.5", "t", TA, "3.5 டன்"),
    ("1", "t", EN, "1 tonne"), ("1", "t", IT, "1 tonnellata"),
    ("48", "hour", IT, "48 ore"), ("30", "day", TA, "30 நாட்கள்"), ("1", "year", TA, "1 ஆண்டு"),
    ("12.5", "%", IT, "12,5%"), ("120", "km", IT, "120 km"),
])
def test_numbers_with_units(one, value, unit, language, out):
    assert one(ClaimKind.NUMBER, value, unit, language) == out


@pytest.mark.parametrize(("value", "unit", "language", "out"), [
    ("1234.50", "EUR", EN, "€1,234.50"), ("1234.50", "EUR", IT, "1.234,50 €"),
    ("245000", "INR", TA, "₹2,45,000"), ("99", "CHF", EN, "CHF 99"), ("99", "CHF", IT, "99 CHF"),
])
def test_prices(one, value, unit, language, out):
    assert one(ClaimKind.PRICE, value, unit, language) == out


@pytest.mark.parametrize(("value", "language", "out"), [
    ("2026-01-05", EN, "5 January 2026"), ("2026-01-05", IT, "5 gennaio 2026"), ("2026-01-05", TA, "5 ஜனவரி 2026"),
    ("2026-03", IT, "marzo 2026"), ("1998", TA, "1998"),
])
def test_dates(one, value, language, out):
    assert one(ClaimKind.DATE, value, language=language) == out


def test_indian_grouping_is_for_inr():
    assert format_number("100000", EN, indian=True) == "1,00,000"


def test_unitless_number_prints_its_approved_wording(one):
    assert one(ClaimKind.NUMBER, "40", None, IT, languages=wording("40")) == "40 sedi"


def test_approved_wording_wins(one):
    assert one(ClaimKind.NUMBER, "3.5", "t", TA, languages={"ta": "மூன்றரை டன்"}) == "மூன்றரை டன்"


def test_names_are_not_translated(one):
    assert one(ClaimKind.NAME, "Rossi Trasporti", language=TA) == "Rossi Trasporti"


def test_quote_capability_and_offer_need_approved_wording_outside_the_default_language(one, make, built):
    assert one(ClaimKind.QUOTE, "Never late.", language=EN, default=EN) == "Never late."
    with pytest.raises(FillError, match="no approved it wording"):
        one(ClaimKind.CAPABILITY, "Delivers on Sundays", language=IT, default=EN)
    offer = make("C-0002", ClaimKind.OFFER, "Free first service", languages={"it": "Primo tagliando gratis"})
    assert built("{{C-0002}}", {"C-0002": offer}, IT) == "Primo tagliando gratis"


@pytest.mark.parametrize(("kind", "unit"), [(ClaimKind.NUMBER, "trucks"), (ClaimKind.NUMBER, "tonne, the best"),
                                            (ClaimKind.PRICE, "EUR best"), (ClaimKind.PRICE, "eur"),
                                            (ClaimKind.PRICE, "XYZ"), (ClaimKind.NAME, "kg")])
def test_units_outside_the_tables_are_refused_at_creation(make, kind, unit):
    with pytest.raises(ClaimError):
        make("C-0001", kind, "1" if kind is not ClaimKind.NAME else "Acme", unit)


def test_build_refuses_whole_text_on_any_bad_slot(make, built):
    good = make("C-0001", ClaimKind.NUMBER, "3.5", "t")
    pending = make("C-0002", ClaimKind.NUMBER, "40", verified=False)
    claims = {c.code: c for c in (good, pending)}
    with pytest.raises(ValueError, match="C-0002"):
        built("{{C-0001}} and {{C-0002}}", claims)


def test_build_refuses_expired_price(make, built, clock):
    price = make("C-0001", ClaimKind.PRICE, "9.99", "EUR")
    clock.advance(30 * DAY)
    with pytest.raises(ValueError, match="expired"):
        built("{{C-0001}}", {"C-0001": price})


def test_build_reports_codes_ids_and_slot_spans(make, clock, tenant):
    from thallo.truth import build
    a = make("C-0001", ClaimKind.NUMBER, "1", "t")
    b = make("C-0002", ClaimKind.NAME, "Acme")
    out = build("{{C-0002}} moves {{C-0001}}, then {{C-0002}} again.", tenant_id=tenant, language=EN,
                claims={"C-0001": a, "C-0002": b}, clock=clock, default_language=EN).filled
    assert out.text == "Acme moves 1 tonne, then Acme again."
    assert out.codes == ("C-0002", "C-0001") and out.claim_ids == (b.id, a.id)
    assert [out.text[x:y] for x, y in out.slot_spans] == ["Acme", "1 tonne", "Acme"]


def test_render_claim_is_pure(make):
    assert render_claim(make("C-0001", ClaimKind.NUMBER, "2", "day"), IT, default_language=EN) == "2 giorni"
