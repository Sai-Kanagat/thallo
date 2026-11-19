"""Slugs, codes and destination checks: plain code, no database."""
from __future__ import annotations

import pytest

from thallo.gate.canonical import find_links, normalise_url
from thallo.tracking.codes import code_candidates, is_slug, new_code, new_slug, normalise_code
from thallo.tracking.destinations import BadDestination, check_destination, with_ref
from thallo.tracking.links import BadPrefix, check_prefix, public_url, whatsapp_text


def test_slug_shape_survives_the_gate():
    for _ in range(200):
        slug = new_slug()
        assert is_slug(slug)
        url = public_url("https://go.example/r/", slug)
        assert normalise_url(url) == url
        assert find_links(f"Book here {url} today") == [url]


def test_codes_have_no_lookalikes_and_are_forgiving():
    for _ in range(200):
        code = new_code()
        assert not set(code) & set("0O1IL2Z5S8B6G")
        assert normalise_code(code.lower()) == code
        assert normalise_code(f" {code[:3]}-{code[3:]} ") == code
    assert normalise_code("HELLO") is None
    assert normalise_code(None) is None
    assert normalise_code("x" * 100) is None


def test_code_candidates_are_capped_and_never_keep_the_text():
    assert code_candidates("Ciao! (ref k7m3pa) grazie") == ["K7M3PA"]
    assert len(code_candidates(" ".join(new_code() for _ in range(200)))) == 20
    assert code_candidates(None) == []


@pytest.mark.parametrize("url", [
    "https://shop.example/landing",
    "https://shop.example/landing?utm_source=ig&utm_campaign=spring",
    "https://wa.me/393331234567?text=Ciao%20(ref%20K7M3PA)",
    "https://shop.example:8443/x#top",
])
def test_good_destinations(url):
    assert check_destination(url) == normalise_url(url)


@pytest.mark.parametrize("url,reason", [
    ("http://shop.example/", "https"),
    ("javascript:alert(1)", "https"),
    ("//evil.example/", "https"),
    ("https://user:pw@shop.example/", "user info"),
    ("https://shop.example@evil.example/", "user info"),
    ("https://127.0.0.1/", "plain domain"),
    ("https://localhost/", "plain domain"),
    ("https://shop.example/ landing", "spaces"),
    ("https://shop.example/\x00", "control"),
    ("https://shöp.example/", "non-ASCII"),
    ("https://shop.example/?email=a%40b.it", "personal"),
    ("https://shop.example/?e=a%40b.it", "personal"),
    ("https://shop.example/?phone=1", "personal"),
    ("https://shop.example/?n=3331234567", "personal"),
    ("https://shop.example/u/3331234567", "long number"),
    ("https://shop.example/u/a%40b.it", "@"),
    ("https://shop.example\\@evil.example/", "spaces"),
    ("https://shop.example/" + "a" * 2001, "too long"),
])
def test_bad_destinations(url, reason):
    with pytest.raises(BadDestination, match=reason):
        check_destination(url)


def test_with_ref_keeps_query_and_fragment():
    assert with_ref("https://s.example/", "abcdefghij") == "https://s.example/?thallo_ref=abcdefghij"
    assert with_ref("https://s.example/p?a=1#f", "abcdefghij") == "https://s.example/p?a=1&thallo_ref=abcdefghij#f"


def test_prefix_must_be_a_plain_https_directory():
    assert check_prefix("https://GO.example/r/") == "https://go.example/r/"
    for bad in ("http://go.example/r/", "https://go.example/r", "https://go.example/r/?x=", "https://u@go.example/r/"):
        with pytest.raises(BadPrefix):
            check_prefix(bad)


def test_whatsapp_text():
    assert whatsapp_text("Ciao!", "K7M3PA") == "Ciao! (ref K7M3PA)"
    assert whatsapp_text("", "K7M3PA") == "ref K7M3PA"
