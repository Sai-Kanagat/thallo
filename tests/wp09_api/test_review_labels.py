"""Read-only fields the review screens show in plain words: check labels, the one-line reason,
the headline and the deck status. None of them changes what approval hashes or accepts."""
from __future__ import annotations

from api.check_labels import GENERIC, LABELS, label
from thallo.contracts.models import PlanSlot
from thallo.copy.produce import FILL_CHECK, GRADER_CHECK, LIBRARY_CHECK
from thallo.gate.checks import DESIGN_LINT_CHECKS, TRUTH_CHECK
from thallo.truth.findings import FindingKind


def test_every_known_check_has_a_plain_label():
    names = {TRUTH_CHECK, FILL_CHECK, LIBRARY_CHECK, GRADER_CHECK, *DESIGN_LINT_CHECKS,
             *(f"{TRUTH_CHECK}.{k.value}" for k in FindingKind)}
    missing = sorted(n for n in names if n not in LABELS)
    assert missing == []
    for name, (short, explain) in LABELS.items():
        assert short and explain and "_" not in short, name
        assert not ({chr(0x2014), chr(0x2013)} & set(short + explain))


def test_unknown_and_coverage_names_never_come_back_blank():
    assert label("something.new") == {"label": GENERIC[0], "explain": GENERIC[1]}
    assert label("coverage.media")["label"] == "Image not checked yet"
    assert label("coverage.audio")["label"] == "Not checked yet"


def test_card_flags_carry_label_and_raw_name(world):
    c, _ = world.login(world.a.founder_email)
    card = c.get(f"/api/review/bundles/{world.a.bundle_id}").json()
    (flag,) = card["flags"]
    assert flag["check_name"].startswith("truth.compile.")
    assert flag["label"] == LABELS[flag["check_name"]][0]
    assert flag["explain"] == LABELS[flag["check_name"]][1]
    assert card["why"] == "Range is the top question"  # the technical detail stays, for "How this was chosen"


def test_reason_falls_back_from_slot_to_fact(world):
    c, _ = world.login(world.a.founder_email)
    card = c.get(f"/api/review/bundles/{world.a.bundle_id}").json()
    assert card["slot"] is None
    assert card["reason"] == "Built on a fact you verified: 120 km."
    assert card["headline"] and "120" in card["headline"]
    with world.svc.backend.tenant(world.a.tenant.id) as tx:
        tx.update(PlanSlot, world.a.slot_ids[1], variant_id=world.a.variant_id)
        slot = tx.get(PlanSlot, world.a.slot_ids[1])
    card = c.get(f"/api/review/bundles/{world.a.bundle_id}").json()
    day = slot.starts_at.strftime("%A")
    assert card["reason"] == f"Fills {day}'s Range slot."
    assert card["slot"]["pillar"] == "Range" and card["slot"]["day"] == slot.starts_at.date().isoformat()


def test_deck_lists_headline_thumbnail_and_status(world):
    c, _ = world.login(world.a.founder_email)
    (row,) = c.get("/api/review/bundles").json()["bundles"]
    assert row["status"] == "flag" and row["flag_count"] == 1 and row["blocking_count"] == 0
    assert row["thumbnail_url"] == f"/api/media/{world.a.media_id}"
    assert row["headline"] and "120" in row["headline"]


def test_listing_the_deck_does_not_count_as_showing_a_card(world):
    """Approval needs the card itself opened first; the deck list must not stand in for that."""
    from tests.wp09_api.test_review import _approve

    c, csrf = world.login(world.a.founder_email)
    assert c.get("/api/review/bundles").status_code == 200
    r = _approve(c, csrf, world, world.a.flag_ids)
    assert r.status_code == 409 and "open the card" in r.json()["message"]
