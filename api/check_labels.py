# ruff: noqa: E501  (a table of sentences reads better one entry per line)
"""Plain words for every check name, in one place: a short label and one line a business owner
understands. The review card shows these; the raw check name stays available, small, beside them.

A name not in the table gets a generic label, so a new check never shows up blank; the test in
tests/wp09_api/test_review_labels.py fails when a compile kind or a design lint check has no entry.
"""
from __future__ import annotations

LABELS: dict[str, tuple[str, str]] = {
    # the copy compiler: numbers and facts
    "truth.compile": ("Facts and numbers", "Every number and fact in the text has to come from your verified facts."),
    "truth.compile.digit": ("A number typed by hand", "Numbers must come from a verified fact, not be typed into the text."),
    "truth.compile.symbol": ("A symbol that implies a number", "Signs like % or a currency sign need a verified fact behind them."),
    "truth.compile.enclosed_digit": ("A styled number", "Circled or styled digits count as numbers and need a verified fact."),
    "truth.compile.foreign_script": ("Characters from another alphabet", "Letters from another script can hide words the checks cannot read."),
    "truth.compile.hidden_character": ("A hidden character", "Invisible characters can change what the text says without showing it."),
    "truth.compile.text_too_long": ("Too long for the channel", "The text is longer than this channel allows."),
    "truth.compile.malformed_slot": ("A broken fact slot", "A fact placeholder is written wrongly, so no fact can fill it."),
    "truth.compile.slot_adjacent": ("Two facts run together", "Two facts sit side by side with nothing between them."),
    "truth.compile.unknown_claim": ("A fact that does not exist", "The text points at a fact that is not in your library."),
    "truth.compile.unusable_claim": ("A fact not yet verified", "The fact is unverified or expired, so it cannot be used yet."),
    "truth.compile.stale_claim": ("An old version of a fact", "A newer version of this fact exists; the text uses the old one."),
    "truth.compile.unbound_number": ("A number with no approved wording", "This number has no approved wording in this language yet."),
    "truth.compile.foreign_currency": ("A price in another currency", "Prices have to be in your own currency."),
    "truth.compile.claim_not_allowed": ("A fact outside the brief", "The brief for this piece does not allow this fact."),
    "truth.compile.number_word": ("A number written as a word", "Numbers spelled out in words still need a verified fact."),
    "truth.compile.ambiguous_number_word": ("A word that may be a number", "A word like \"one\" or \"a dozen\" may read as a number. Check it is not a claim."),
    "truth.compile.universal_claim": ("An always or never claim", "Words like always, every or never promise something absolute."),
    "truth.compile.superlative": ("A best or most claim", "Words like best or fastest need proof behind them."),
    "truth.compile.comparative": ("A comparison", "Words like better or cheaper compare you to someone else and need proof."),
    "truth.compile.named_customer": ("A customer named", "A customer is named; make sure they agreed to be mentioned."),
    "truth.compile.spaced_letters": ("Letters spaced apart", "Spaced-out letters can hide words the checks cannot read."),
    "truth.compile.overlong_word": ("A word too long to read", "One word is too long for the checks to read, so it blocks."),
    "truth.compile.classifier": ("A risky phrase", "A content check found a phrase a person should look at."),
    "truth.fill": ("Facts could not be filled in", "The writer's draft could not be filled with your verified facts."),
    "truth.library": ("A problem in your fact library", "Your fact library has an error; fix the library first."),
    "copy.grader": ("The draft could not be scored", "The step that scores drafts stopped with an error."),
    # the design lint on images
    "design.canvas_size": ("Wrong image size", "The image is not the size this channel asks for."),
    "design.contrast": ("Text hard to read", "Text and background are too close in colour to read easily."),
    "design.font_loaded": ("Brand font missing", "The image fell back to another font instead of your brand font."),
    "design.logo_clear_space": ("Logo too crowded", "Something sits inside the space the logo needs around it."),
    "design.logo_size": ("Logo too small", "The logo is smaller than your brand allows."),
    "design.min_font_size": ("Text too small", "Some text is too small to read on a phone."),
    "design.overflow": ("Text spills over", "Text runs outside its box and gets cut off."),
    "design.request": ("Image request not valid", "The image was asked for with settings the template cannot draw."),
    "design.safe_zone": ("Too close to the edge", "Something sits where the app may crop or cover it."),
    "design.script": ("Template script error", "The image template failed while drawing."),
    "design.slots": ("Missing image text", "A text field the template needs was empty."),
    "design.words_per_slide": ("Too many words", "A slide carries more words than people read in a scroll."),
    # the gate itself
    "media.readable": ("A file could not be read", "An image or video in this piece could not be opened."),
    "gate.canonical_form": ("The piece could not be prepared", "The piece could not be put in the exact form approval signs."),
}

COVERAGE_LABELS: dict[str, tuple[str, str]] = {
    "text": ("Text not checked yet", "The checks on this text have not run yet."),
    "media": ("Image not checked yet", "The checks on this image have not run yet."),
}

GENERIC = ("A check", "A check asked a person to look at this.")


def label(check_name: str) -> dict[str, str]:
    """`label` and `explain` for a check name, never empty."""
    if check_name in LABELS:
        name, explain = LABELS[check_name]
    elif check_name.startswith("coverage."):
        name, explain = COVERAGE_LABELS.get(check_name.removeprefix("coverage."),
                                            ("Not checked yet", "A check has not run on this content yet."))
    else:
        name, explain = GENERIC
    return {"label": name, "explain": explain}
