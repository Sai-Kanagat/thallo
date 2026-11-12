"""Second round of automated findings: broad link detection, consent scope, the grant entry
inside an intact chain, and one resolver snapshot for hash and payload. Written before the fix."""
from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from cortex_operator.contracts.actors import HumanActor
from cortex_operator.contracts.enums import MediaKind
from cortex_operator.gate import ApprovalRefused, BlockReason, check_publish, decide, load_bundle
from cortex_operator.gate.canonical import find_links, find_unresolved_links
from cortex_operator.gate.decision import ApprovalEvidence
from cortex_operator.gate.signing import ENV_KEY, GateSigner
from cortex_operator.trust.audit import compute_entry_hash


def vid0(w):
    return next(iter(w.variants))


def publish(w, approval_id, variant_id=None):
    return check_publish(w, tenant_id=w.tenant.id, bundle_id=w.bundle.id, variant_id=variant_id or vid0(w),
                         approval_id=approval_id, clock=w.clock, signer=w.signer)


def blocked(d, reason):
    assert not d.allowed and reason in d.reasons, d.blocks


# ---------------------------------------------------------------- 1. links: match broadly, fail closed

@pytest.mark.parametrize("text", [
    "Vai su b" + chr(0xFC) + "cher.example/x",  # IDN label
    "evil" + chr(0xFF0E) + "com",  # full-width full stop
    "evil" + chr(0x3002) + "com",  # ideographic full stop
    "evil" + chr(0xFF61) + "com",  # half-width ideographic full stop
    "xn--bcher-kva.example",  # punycode
    "evil.123",  # numeric TLD
    "10.0.0.1/admin",  # IPv4
    "10.0.0.1",
    "//evil.example/x",  # protocol-relative
    "x-evil.example",  # hyphen before
    ".evil.example",  # dot before
    "a:evil.example",  # colon before
    "/evil.example",  # slash before
    "evil" + chr(0x200B) + ".example",  # zero-width space inside
    "ftp://evil.example",  # other scheme
    "see:https://evil.example",  # scheme inside a token
])
def test_domain_like_tokens_are_found(text):
    assert find_links(text), text


@pytest.mark.parametrize("text", [
    "Costa 1.200 euro",  # Italian thousands separator
    "Sconto del 12,5% oggi",
    "Da 1.200,50 al mese",
    "Write to info@demo.example please",  # an email address is not a link
    "Hello world. New line.",
])
def test_plain_text_is_not_a_link(text):
    assert find_links(text) == []


@pytest.mark.parametrize("text", [
    "See report.pdf", "Built on Node.js", "Read README.md", "Il Dr.Rossi risponde", "Va bene ciao.Grazie",
    "e.g. this", "Demo S.p.A.", "a.m. or p.m.", "main.py and data.csv", "foto.JPEG",
])
def test_file_names_abbreviations_and_prose_are_not_links(text):
    assert find_links(text) == [], text


@pytest.mark.parametrize("text", [
    "evil.zip", "evil.mov",  # real TLDs, not just file extensions
    "Visit evil.Com", "www.evil", "demo.shop", "Shop.Example/x", "https://x.y",
])
def test_plausible_hosts_still_count(text):
    assert find_links(text), text


def test_find_unresolved_links_for_early_copy_checks():
    text = "Go to demo.shop or https://known.example/a, see report.pdf"
    known = {"https://known.example/a": "https://known.example/a"}
    assert find_unresolved_links(text, known.get) == ["https://demo.shop/"]
    assert find_unresolved_links(text, known.get, non_links=frozenset({"DEMO.shop"})) == []


def test_an_unknown_host_blocks_until_the_tenant_vouches_for_it(world):
    world.edit_variant(vid0(world), rendered_text="Ordina su demo.shop oggi")
    with pytest.raises(ApprovalRefused):
        world.approve()
    world.non_links |= {"DEMO.shop"}  # case-insensitive, after NFKC
    a = world.approve()
    assert publish(world, a.id).allowed
    world.non_links.clear()
    blocked(publish(world, a.id), BlockReason.UNRESOLVED_LINK)


def test_unresolved_idn_link_blocks_publish(world):
    url = "https://b" + chr(0xFC) + "cher.example/x"
    world.edit_variant(vid0(world), rendered_text="Vai su b" + chr(0xFC) + "cher.example/x")
    world.links[url] = url
    a = world.approve()
    del world.links[url]
    blocked(publish(world, a.id), BlockReason.UNRESOLVED_LINK)


def test_full_width_dot_link_must_resolve(world):
    world.edit_variant(vid0(world), rendered_text="Offerta su evil" + chr(0xFF0E) + "example")
    with pytest.raises(ApprovalRefused):
        world.approve()


# ---------------------------------------------------------------- 2. consent scope matches use

def _with_consent(world, *, kind=MediaKind.IMAGE, scope="likeness", **consent_update):
    mid = next(iter(world.media))
    cid = world.add_consent()
    world.consents[cid] = world.consents[cid].model_copy(update={"scope": scope, **consent_update})
    world.media[mid] = world.media[mid].model_copy(update={"consent_id": cid, "kind": kind})
    return cid


@pytest.mark.parametrize("kind,scope", [
    (MediaKind.IMAGE, "voice"),
    (MediaKind.IMAGE, "contact"),
    (MediaKind.AUDIO, "likeness"),
    (MediaKind.VIDEO, "likeness"),  # no voice field on media yet: a video is assumed to carry a voice
    (MediaKind.VIDEO, "voice"),
])
def test_consent_scope_must_match_the_use(world, kind, scope):
    _with_consent(world, kind=kind, scope=scope)
    with pytest.raises(ApprovalRefused):
        world.approve()


@pytest.mark.parametrize("kind,scope", [
    (MediaKind.IMAGE, "likeness"),
    (MediaKind.AUDIO, "voice"),
    (MediaKind.VIDEO, "likeness+voice"),
    (MediaKind.VIDEO, "voice, likeness"),
])
def test_matching_consent_scope_publishes(world, kind, scope):
    _with_consent(world, kind=kind, scope=scope)
    a = world.approve()
    assert publish(world, a.id).allowed


def test_consent_scope_narrowed_after_approval_blocks(world):
    cid = _with_consent(world)
    a = world.approve()
    world.consents[cid] = world.consents[cid].model_copy(update={"scope": "contact"})
    blocked(publish(world, a.id), BlockReason.CONSENT_SCOPE)


def test_consent_given_in_the_future_is_not_consent(world):
    _with_consent(world, given_at=world.clock.now() + timedelta(days=1))
    with pytest.raises(ApprovalRefused):
        world.approve()


def test_consent_given_in_the_future_blocks_publish(world):
    cid = _with_consent(world)
    a = world.approve()
    world.consents[cid] = world.consents[cid].model_copy(update={"given_at": world.clock.now() + timedelta(days=1)})
    blocked(publish(world, a.id), BlockReason.CONSENT_MISSING)


# ---------------------------------------------------------------- 3. the grant entry, inside an intact chain

@pytest.mark.parametrize("field,delta", [("approved_at", timedelta(seconds=-1)), ("expires_at", timedelta(hours=-1))])
def test_grant_payload_times_must_match_the_row(world, field, delta):
    a = world.approve()
    world.approvals[a.id] = a.model_copy(update={field: getattr(a, field) + delta})
    blocked(publish(world, a.id), BlockReason.NO_GRANT_RECORD)


def test_grant_payload_canary_must_match_the_row(world):
    world.edit_variant(vid0(world), is_canary=True)
    a = world.approve()
    world.approvals[a.id] = a.model_copy(update={"canary": False})
    d = publish(world, a.id)
    blocked(d, BlockReason.CANARY)
    assert BlockReason.NO_GRANT_RECORD in d.reasons


def _forge(world):
    """An agent writes an approval row and a grant entry with a valid own hash."""
    a = world.approve()
    grant = next(e for e in world.audit_store.entries(world.tenant.id) if e.action == "approval.granted")
    forged_row = a.model_copy(update={"id": world.bundle.id[:-1] + "f"})
    world.approvals[forged_row.id] = forged_row
    draft = grant.model_copy(update={"id": world.bundle.id[:-1] + "e", "subject_id": forged_row.id})
    return forged_row, draft


def test_a_grant_entry_outside_the_chain_does_not_vouch(world):
    forged_row, draft = _forge(world)
    head = world.audit_store.head(world.tenant.id)
    entry = draft.model_copy(update={"seq": head.seq + 1, "prev_hash": "0" * 64})
    entry = entry.model_copy(update={"entry_hash": compute_entry_hash(entry)})
    world.audit_store.insert(entry)
    blocked(publish(world, forged_row.id), BlockReason.NO_GRANT_RECORD)


def test_a_grant_entry_spliced_into_the_middle_does_not_vouch(world):
    forged_row, draft = _forge(world)
    world.audit.append(world.tenant.id, world.human(), "note.later")
    rows = world.audit_store._rows[world.tenant.id]
    first = rows[0]
    entry = draft.model_copy(update={"seq": 2, "prev_hash": first.entry_hash})
    entry = entry.model_copy(update={"entry_hash": compute_entry_hash(entry)})
    rows[1:] = [entry] + [r.model_copy(update={"seq": r.seq + 1}) for r in rows[1:]]
    blocked(publish(world, forged_row.id), BlockReason.NO_GRANT_RECORD)


def test_a_chain_that_contradicts_the_anchor_does_not_vouch(world):
    a = world.approve()
    world.anchor = (1, "f" * 64)  # recorded elsewhere, and the log no longer matches it
    blocked(publish(world, a.id), BlockReason.NO_GRANT_RECORD)


def test_a_truncated_chain_does_not_vouch(world):
    a = world.approve()
    world.audit.append(world.tenant.id, world.human(), "note.later")
    world.anchor = world.audit.head(world.tenant.id)
    world.audit_store._rows[world.tenant.id].pop()
    blocked(publish(world, a.id), BlockReason.NO_GRANT_RECORD)


def test_a_matching_anchor_still_publishes(world):
    a = world.approve()
    world.audit.append(world.tenant.id, world.human(), "note.later")
    world.anchor = world.audit.head(world.tenant.id)
    assert publish(world, a.id).allowed


# ---------------------------------------------------------------- 4. one resolver snapshot

def test_resolver_is_called_once_per_link_and_payload_matches_the_hash(world):
    a = world.approve()
    calls: dict[str, int] = {}
    flip = iter(["https://demo-bikes.example/offer?utm=ig", "https://evil.example/"] * 10)

    def resolver(url):
        calls[url] = calls.get(url, 0) + 1
        return next(flip)

    content = load_bundle(world, world.tenant.id, world.bundle)
    ev = ApprovalEvidence(approval=a, approver=world.users[world.user_id],
                          audit_entries=tuple(world.get_audit_entries(world.tenant.id, "approval", a.id)),
                          chain=tuple(world.get_audit_chain(world.tenant.id, 1)), anchor=None)
    d = decide(content, ev, tenant=world.tenant, variant_id=vid0(world), now=world.clock.now(),
               system_paused=False, resolver=resolver, non_links=frozenset(), signer=world.signer)
    assert calls and all(n == 1 for n in calls.values()), calls
    assert d.allowed, d.blocks
    assert d.payload.variant_hash == d.bundle_hash.variants[vid0(world)]
    assert d.payload.links[0][1] == "https://demo-bikes.example/offer?utm=ig"


def test_identical_text_hashes_the_same_with_bytes_from_any_source(world):
    """Sanity: the media hash is of bytes, so it matches hashlib over the stored blob."""
    a = world.approve()
    d = publish(world, a.id)
    assert d.payload.media[0].sha256 == hashlib.sha256(world.blobs["s3://bucket/hero.png"]).hexdigest()


def test_human_actor_type_is_unchanged():
    assert HumanActor.model_fields["session_id"].is_required()


# ---------------------------------------------------------------- 5. a keyed MAC on every grant

def test_forged_row_and_grant_appended_at_the_head_do_not_publish(world):
    """The chain hash is public, so an agent can append a well-linked forged grant. Only the MAC,
    made with a key the app role never holds, tells the two apart."""
    forged_row, draft = _forge(world)
    head = world.audit_store.head(world.tenant.id)
    entry = draft.model_copy(update={"seq": head.seq + 1, "prev_hash": head.entry_hash})
    entry = entry.model_copy(update={"entry_hash": compute_entry_hash(entry)})
    world.audit_store.insert(entry)
    assert world.audit.verify(world.tenant.id).ok  # the chain itself cannot tell
    blocked(publish(world, forged_row.id), BlockReason.NO_GRANT_RECORD)


def test_a_grant_signed_with_another_key_does_not_publish(world):
    a = world.approve()
    world.signer = GateSigner(b"k" * 32)
    blocked(publish(world, a.id), BlockReason.NO_GRANT_RECORD)


def test_missing_key_at_publish_blocks(world, monkeypatch):
    a = world.approve()
    monkeypatch.delenv(ENV_KEY, raising=False)
    d = check_publish(world, tenant_id=world.tenant.id, bundle_id=world.bundle.id, variant_id=vid0(world),
                      approval_id=a.id, clock=world.clock)  # no signer passed, none in the environment
    blocked(d, BlockReason.GATE_KEY_MISSING)


def test_key_is_read_from_the_environment(world, monkeypatch):
    a = world.approve()
    monkeypatch.setenv(ENV_KEY, world.signer.key.hex())
    d = check_publish(world, tenant_id=world.tenant.id, bundle_id=world.bundle.id, variant_id=vid0(world),
                      approval_id=a.id, clock=world.clock)
    assert d.allowed, d.blocks


@pytest.mark.parametrize("raw", ["", "short", "zz" * 40])
def test_weak_or_malformed_keys_are_refused(monkeypatch, raw):
    monkeypatch.setenv(ENV_KEY, raw)
    with pytest.raises(ValueError):
        GateSigner.from_env()


def test_key_never_shows_in_repr(world):
    assert world.signer.key.hex() not in repr(world.signer)


def test_payload_that_differs_from_the_hashed_leaf_blocks(world, monkeypatch):
    import dataclasses

    from cortex_operator.gate import canonical

    a = world.approve()
    real = canonical.Snapshot.payload
    monkeypatch.setattr(canonical.Snapshot, "payload",
                        lambda self, vid: dataclasses.replace(real(self, vid), variant_hash="0" * 64))
    blocked(publish(world, a.id), BlockReason.PAYLOAD_MISMATCH)
