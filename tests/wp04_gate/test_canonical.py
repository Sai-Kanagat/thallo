"""The canonical form: what counts as the same content, and what does not."""
from __future__ import annotations

import hashlib

import pytest

from cortex_operator.contracts.enums import Channel, Language
from cortex_operator.gate.canonical import (
    BundleContent,
    CanonicalFormError,
    MediaItem,
    VariantContent,
    bundle_hash,
    canonical_json,
    canonical_text,
    claim_slots,
    find_links,
    normalise_url,
    sha256_bytes,
)
from tests.wp04_gate.conftest import make_world


def root(content, resolver=None):
    return bundle_hash(content, resolver).root


# ---------------------------------------------------------------- text

@pytest.mark.parametrize("a,b", [
    ("Hello world", "Hello   world"),
    ("Hello world", "Hello\tworld"),
    ("Hello world", "  Hello world  "),
    ("a\nb", "a\r\nb"),
    ("a\nb", "a\rb"),
    ("a\nb", "a" + chr(0x2028) + "b"),
    ("a\n\nb", "a\n\n\n\nb"),
    ("a\n\nb", "a\n \t \nb"),
    ("a", "\n\na\n\n"),
    ("caf" + chr(0xE9), "cafe" + chr(0x301)),  # NFC: precomposed and combining forms are the same
])
def test_text_differences_that_do_not_count(a, b):
    assert canonical_text(a) == canonical_text(b)


@pytest.mark.parametrize("a,b", [
    ("Hello world", "hello world"),
    ("Hello world", "Hello world."),
    ("a b", "a" + chr(0xA0) + "b"),  # a no-break space is kept
    ("ab", "a" + chr(0x200D) + "b"),  # zero-width joiner is kept (Tamil shaping depends on it)
    ("ab", "a" + chr(0x200B) + "b"),  # zero-width space is kept: hidden text never hides from the hash
    ("a\nb", "a b"),
    ("a\nb", "a\n\nb"),
    ("120", "12O"),
])
def test_text_differences_that_count(a, b):
    assert canonical_text(a) != canonical_text(b)


def test_canonical_text_is_idempotent():
    s = "  Ciao\t\tmondo \r\n\r\n\r\n https://x.test/a  "
    assert canonical_text(canonical_text(s)) == canonical_text(s)


def test_claim_slots():
    assert claim_slots("Up to {{C-0012}} and {{ C-0013 }} and {{C-0012}}") == ["C-0012", "C-0013"]


# ---------------------------------------------------------------- links

@pytest.mark.parametrize("raw,norm", [
    ("HTTPS://Example.COM", "https://example.com/"),
    ("https://example.com:443/a", "https://example.com/a"),
    ("http://example.com:80/a?b=C", "http://example.com/a?b=C"),
    ("https://example.com:8443/A", "https://example.com:8443/A"),
    ("https://example.com?q=1", "https://example.com/?q=1"),
    ("https://User@Example.com/p", "https://User@example.com/p"),
])
def test_url_normalisation(raw, norm):
    assert normalise_url(raw) == norm


def test_find_links_strips_sentence_punctuation():
    assert find_links("See https://a.test/x. Or (https://b.test/y), ok") == ["https://a.test/x", "https://b.test/y"]


# ---------------------------------------------------------------- media

def test_media_hash_is_of_bytes_not_path():
    assert sha256_bytes(b"abc") == hashlib.sha256(b"abc").hexdigest() == sha256_bytes([b"a", b"bc"])


def _with_media(content, vid, items):
    return BundleContent(bundle=content.bundle, variants=tuple(
        VariantContent(variant=vc.variant, media=items, claims=vc.claims) if vc.variant.id == vid else vc
        for vc in content.variants))


def test_moving_a_file_to_another_path_does_not_change_the_root():
    w = make_world()
    before = root(w.content())
    for mid, m in list(w.media.items()):
        new_uri = m.storage_uri.replace("s3://bucket/", "gs://other/")
        w.blobs[new_uri] = w.blobs[m.storage_uri]
        w.media[mid] = m.model_copy(update={"storage_uri": new_uri})
    assert root(w.content()) == before


def test_media_bytes_order_and_alt_text_change_the_root():
    w = make_world()
    c = w.content()
    vid = next(iter(w.variants))
    items = c.variant(vid).media
    base = root(c)
    swapped = tuple(MediaItem(media=m.media, position=1 - m.position, bytes_sha256=m.bytes_sha256) for m in items)
    assert root(_with_media(c, vid, swapped)) != base  # carousel order carries meaning
    changed = (MediaItem.from_bytes(items[0].media, 0, b"other"), items[1])
    assert root(_with_media(c, vid, changed)) != base
    alt = (MediaItem(media=items[0].media.model_copy(update={"alt_text": "A blue bike"}), position=0,
                     bytes_sha256=items[0].bytes_sha256), items[1])
    assert root(_with_media(c, vid, alt)) != base


# ---------------------------------------------------------------- the root

def test_variant_and_claim_order_carry_no_meaning():
    w = make_world()
    c = w.content()
    reordered = BundleContent(bundle=c.bundle, variants=tuple(reversed(c.variants)))
    assert root(reordered) == root(c)
    vc = c.variants[0]
    doubled = VariantContent(variant=vc.variant, media=vc.media, claims=vc.claims + vc.claims)
    assert root(BundleContent(bundle=c.bundle, variants=(doubled,) + c.variants[1:])) == root(c)


@pytest.mark.parametrize("field,value", [
    ("channel", Channel.FACEBOOK),
    ("language", Language.TA),
    ("text", "Fino a {{C-0012}} con due cariche."),
    ("rendered_text", "Fino a 120 km con una carica."),
])
def test_variant_fields_change_the_root(field, value):
    w = make_world()
    before = root(w.content())
    w.edit_variant(next(iter(w.variants)), **{field: value})
    assert root(w.content()) != before


@pytest.mark.parametrize("field,value", [
    ("evidence_sha256", "c" * 64),
    ("value", "121"),
    ("unit", "mi"),
    ("languages", {"it": "121 km di autonomia"}),
])
def test_claim_fields_change_the_root(field, value):
    w = make_world()
    before = root(w.content())
    w.edit_claim(next(iter(w.claims)), **{field: value})
    assert root(w.content()) != before


def test_fields_outside_the_canonical_form_do_not_change_the_root():
    """Scheduling, state and the canary mark are not content. The canary mark especially must not
    leak through the hash the reviewer sees."""
    w = make_world()
    before = root(w.content())
    vid = next(iter(w.variants))
    w.edit_variant(vid, is_canary=True, canary_flaw="x", task_type="other")
    w.edit_claim(next(iter(w.claims)), expires_at=None)
    assert root(w.content()) == before


def test_rule_version_tenant_and_bundle_bind_the_root():
    w = make_world()
    c = w.content()
    base = root(c)
    for update in ({"rule_version": 4}, {"id": w.user_id}, {"tenant_id": w.user_id}):
        assert root(BundleContent(bundle=c.bundle.model_copy(update=update), variants=c.variants)) != base


def test_link_resolution_binds_the_destination():
    w = make_world()
    c = w.content()
    a = root(c, {"https://thallo.test/r/abc": "https://demo.example/offer"}.get)
    b = root(c, {"https://thallo.test/r/abc": "https://DEMO.example:443/offer"}.get)
    other = root(c, {"https://thallo.test/r/abc": "https://evil.example/"}.get)
    assert a == b != other
    assert root(c, None) != a


def test_empty_or_duplicated_bundle_has_no_root():
    w = make_world()
    c = w.content()
    with pytest.raises(CanonicalFormError):
        bundle_hash(BundleContent(bundle=c.bundle, variants=()))
    with pytest.raises(CanonicalFormError):
        bundle_hash(BundleContent(bundle=c.bundle, variants=(c.variants[0], c.variants[0])))


def test_canonical_json_refuses_floats_and_is_stable():
    with pytest.raises(CanonicalFormError):
        canonical_json({"a": 1.5})
    assert canonical_json({"b": [1, "è"], "a": None}) == '{"a":null,"b":[1,"è"]}'.encode()


def test_golden_root_is_stable():
    """Pins the canonical form. If this changes, the form changed: bump CANONICAL_VERSION and
    CANONICAL_FORM.md, because every stored approval hash depends on it."""
    w = make_world()
    w.bundle = w.bundle.model_copy(update={"id": "019b8d62-7a80-7000-8000-000000000001",
                                           "tenant_id": "019b8d62-7a80-7000-8000-000000000002"})
    ids = iter(f"019b8d62-7a80-7000-8000-0000000001{i:02d}" for i in range(50))
    remap = {}
    for vid in list(w.variants):
        new = next(ids)
        remap[vid] = new
        w.variants[new] = w.variants.pop(vid).model_copy(update={"id": new, "bundle_id": w.bundle.id,
                                                                  "tenant_id": w.bundle.tenant_id})
    w.variant_media = {remap[k]: [vm.model_copy(update={"tenant_id": w.bundle.tenant_id}) for vm in v]
                       for k, v in w.variant_media.items()}
    w.variant_claims = {remap[k]: v for k, v in w.variant_claims.items()}
    for mid in list(w.media):
        w.media[mid] = w.media[mid].model_copy(update={"tenant_id": w.bundle.tenant_id})
    w.tenant = w.tenant.model_copy(update={"id": w.bundle.tenant_id})
    cid = next(iter(w.claims))
    w.edit_claim(cid, id="019b8d62-7a80-7000-8000-000000000200", tenant_id=w.bundle.tenant_id)
    w.claims = {c.id: c for c in w.claims.values()}
    w.variant_claims = {k: ["019b8d62-7a80-7000-8000-000000000200"] for k in w.variant_claims}
    h = bundle_hash(w.content(), w.links.get)
    assert h.root == GOLDEN_ROOT, h.root


GOLDEN_ROOT = "025d834c04b445f0c5b8af685c7722d651b9cac395a36d7a12dfc2bf06ab34ab"
