"""Tokens, templates, channels and rotation. No browser."""
from __future__ import annotations

import json
import re
import shutil

import pytest

from thallo.brand.templates import (
    MIN_TEMPLATES_PER_FAMILY,
    ROTATION_WINDOW,
    choose_template,
    rotation_ok,
)
from thallo.brand.tokens import DEMO_BRAND_DIR, TokenError, load_brand, script_for
from thallo.contracts.enums import Channel, Language

FAMILIES = {"single", "carousel", "quote", "proof", "offer", "video_frame"}


def _copy_demo(tmp_path):
    d = tmp_path / "b"
    shutil.copytree(DEMO_BRAND_DIR, d)
    return d


def _edit(path, fn):
    data = json.loads(path.read_text())
    fn(data)
    path.write_text(json.dumps(data))


def test_demo_brand_is_fictional_and_has_both_scripts(brand):
    assert brand.tokens.fictional
    assert set(brand.scripts) == {"latin", "tamil"}
    assert brand.for_language(Language.EN).script == "latin"
    assert brand.for_language(Language.IT).script == "latin"
    assert brand.for_language(Language.TA).script == "tamil"
    assert script_for(Language.TA) == "tamil"


def test_demo_file_refused_unless_fictional(tmp_path):
    d = _copy_demo(tmp_path)
    _edit(d / "demo.brand.json", lambda b: b.update(fictional=False))
    with pytest.raises(TokenError, match="fictional"):
        load_brand(d)


def test_real_brand_file_wins_over_demo_file(tmp_path):
    d = _copy_demo(tmp_path)
    data = json.loads((d / "demo.brand.json").read_text())
    data.update(brand="other", fictional=False)
    (d / "brand.json").write_text(json.dumps(data))
    assert load_brand(d).tokens.brand == "other"


@pytest.mark.parametrize("mutate", [
    lambda b: b["colors"].update(ink="red"),
    lambda b: b["colors"].pop("on_scrim"),
    lambda b: b["type_scale"].pop("small"),
    lambda b: b["logo"].update(file="../../etc/hosts"),
    lambda b: b["logo"].update(width=10),
    lambda b: b.update(photo_scrim="nope"),
])
def test_bad_brand_tokens_refused(tmp_path, mutate):
    d = _copy_demo(tmp_path)
    _edit(d / "demo.brand.json", mutate)
    with pytest.raises(TokenError):
        load_brand(d)


def test_script_tokens_cannot_serve_the_wrong_language(tmp_path):
    d = _copy_demo(tmp_path)
    _edit(d / "script" / "tamil.json", lambda s: s.update(languages=["ta", "en"]))
    with pytest.raises(TokenError, match="cannot serve"):
        load_brand(d)


def test_font_names_cannot_break_out_of_css(tmp_path):
    d = _copy_demo(tmp_path)
    _edit(d / "script" / "latin.json", lambda s: s["fonts"]["body"].update(local=['x"); } body { color: red']))
    with pytest.raises(TokenError):
        load_brand(d)


def test_six_families_with_eight_templates_each(book):
    assert set(book.families) == FAMILIES
    for name, fam in book.families.items():
        assert len(fam.layouts) >= MIN_TEMPLATES_PER_FAMILY, name
        assert len(book.refs(name)) == len(fam.layouts)
    assert len(book.refs()) == 48
    assert book.get("quote-01").family_name == "quote"
    for bad in ("quote-00", "quote-09", "nope-01", "quote1"):
        with pytest.raises(TokenError):
            book.get(bad)


def test_specimen_copy_has_no_digits(book):
    """Numbers come only from verified claim slots; template previews must not type any."""
    for fam in book.families.values():
        for copy in fam.specimen.values():
            for text in copy.values():
                assert not re.search(r"\d", text), text


def test_photo_layouts_require_a_photo(book):
    t = book.get("single-04")
    assert t.layout.photo == "full" and t.slot_spec("photo").required
    assert not book.get("single-01").slot_spec("photo").required


def test_channel_rules(book):
    ch = book.channels
    assert ch.rule(Channel.INSTAGRAM, 1080, 1920)[1] != ch.rule(Channel.INSTAGRAM, 1080, 1350)[1]
    assert ch.rule(Channel.INSTAGRAM, 1080, 1350)[2] is True
    assert ch.rule(Channel.INSTAGRAM, 1200, 627)[2] is False
    strict = ch.rule(None, 1080, 1350)[0]
    assert strict == max(r.min_font_px for r in ch.channels.values())
    assert (1080, 1350) in ch.all_sizes() and (1200, 627) in ch.all_sizes()


def test_rotation_never_repeats_inside_five_posts(book):
    history: list[str] = []
    for i in range(60):
        history.append(choose_template(book, "quote", history, seed=f"post-{i}"))
    assert rotation_ok(history)
    assert len(set(history)) > ROTATION_WINDOW


def test_rotation_is_deterministic_and_detects_repeats(book):
    a = choose_template(book, "offer", ["offer-01"], seed="x")
    assert a == choose_template(book, "offer", ["offer-01"], seed="x")
    assert a != "offer-01"
    assert not rotation_ok(["q-1", "q-2", "q-3", "q-1"])
    assert rotation_ok(["q-1", "q-2", "q-3", "q-4", "q-5", "q-1"])


def test_rotation_window_larger_than_family_refused(book):
    with pytest.raises(TokenError, match="too few"):
        choose_template(book, "quote", book.refs("quote"), seed="x", window=9)
