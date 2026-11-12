# Canonical form for the bundle root hash (version 1)

Code: `cortex_operator/gate/canonical.py`. BUILD_PLAN section 5, revision 4. The approval binds to the root defined here; any change to these rules changes every root, so once approvals are stored it needs a new `CANONICAL_VERSION`, new domain strings, and a lead decision. `tests/wp04_gate/test_canonical.py::test_golden_root_is_stable` pins one root so an accidental change fails CI. (Version 1 was extended with `consent_id`, `tracked_links` and the claim `version` and `supersedes_id` before any approval was stored. A new claim version is a new row with a new id, so it always changes the root and voids the approval.)

## What the root covers

For every variant in the bundle:

| Field | Form |
|---|---|
| `variant_id` | the row id |
| `channel`, `language` | enum values |
| `text` | canonical text of the source text, claim slots like `{{C-0012}}` included |
| `rendered_text` | canonical text of the rendered text, or `null` |
| `links` | every link in the outgoing text, in order of appearance, as `[url, destination]` |
| `tracked_links` | every tracked link row whose `variant_id` is this variant, as `[slug, destination, whatsapp_code]`, sorted, duplicates removed |
| `media` | one entry per file in position order: `position`, `sha256` of the bytes, `kind`, `mime` (lowercase), canonical `alt_text`, `disclosure_label`, `consent_id` |
| `claims` | one entry per linked claim, sorted by code then id, duplicates removed: `id`, `code`, `version`, `supersedes_id`, `value` (NFC), `unit`, `as_of` (ISO date), `evidence_sha256`, `wording` (canonical text per language) |

Then the bundle: `tenant_id`, `bundle_id`, `rule_version`, and the sorted list of variant leaf hashes.

Not covered, on purpose: storage paths and URIs, row timestamps, states, `scheduled_for`, `task_type`, `template_ref`, claim `expires_at` and claim verifier fields (checked at publish time instead), the consent row itself (its withdrawal is checked at publish time; its id is hashed), and the canary mark (it must never show in the hash a reviewer sees).

## Text

Applied to every text field, in this order:

1. Unicode NFC. Not NFKC: compatibility characters (full-width digits, ligatures) stay distinct.
2. Line breaks: CRLF, CR, U+0085, U+2028 and U+2029 become LF.
3. Within each line, runs of spaces (U+0020) and tabs become one space; leading and trailing spaces and tabs are removed.
4. Runs of blank lines become one blank line. Leading and trailing blank lines are removed.

Everything else counts. Case, punctuation, no-break spaces, zero-width characters (U+200B, U+200C, U+200D) all change the hash: zero-width joiners shape Tamil, and invisible characters must never be a way to slip a change past an approval.

## Links

Detection works on a copy of the text after NFKC, with the ideographic and half-width full stops (U+3002, U+FF61) turned into dots and invisible characters (soft hyphen, zero-width space, joiners, word joiner, BOM) removed. The text is split on whitespace, `<`, `>` and `"`, and leading and trailing punctuation (brackets, quotes, `*`, `_`, and at the end also `.,;:!?%` and currency signs) is stripped from each token. A token is a link, matched broadly so that the check fails closed, when it:

- contains a scheme (`https://`, `ftp://`, any `x://`), read from the scheme on;
- starts with `//`, read as https;
- contains an IPv4 address, or starts with `www.`;
- contains a dot-separated run of labels (letters of any script, digits, `-`, `_`), read as https from the start of that run.

These are not links:

- numbers, such as `1.200`, `12,5` or `1.200,50` (three dots or more is IPv4 and counts);
- email addresses;
- a run whose final label is a single character (`e.g.`, `a.m.`, `S.p.A.`);
- a run whose final label is a common file extension that is not a real TLD: `pdf`, `md`, `js`, `ts`, `py`, `png`, `jpg`, `jpeg`, `gif`, `docx`, `xlsx`, `pptx`, `csv`, `txt`, in any case (`zip`, `mov` and other real TLDs still count);
- prose with no space after a full stop: exactly two alphabetic words, the second capitalised (`ciao.Grazie`, `Dr.Rossi`), unless the second word is a common TLD (`com`, `net`, `org`, `info`, `biz`, `io`, `co`, `it`, `eu`, `uk`, `de`, `fr`, `es`, `app`, `dev`, `shop`, `me`).

Each link is normalised: scheme and host lowercased, `:80` on http and `:443` on https dropped, an empty path becomes `/`. User info, path, query and fragment are kept byte for byte, with no percent decoding.

Each link is resolved by `GateRepository.resolve_link` (the tracked link table, known redirects, or the URL itself for a direct link the tenant vouches for; never the network) and the destination is normalised the same way. A resolver is required: approval (`approve_bundle_from_repo`) and publish (`check_publish`) both load through `load_bundle` and resolve through the same repository, so they cannot disagree.

Each link is resolved once per bundle check: the resolver is memoised (`MemoResolver`), every variant leaf is built once, and both the root hash and the publish payload are derived from those same leaves. Publish blocks with `payload_mismatch` unless the payload's `variant_hash` equals the hashed leaf.

A link that does not resolve is hashed as `null`, and it blocks, unless the token or its URL is on the tenant's non-link allowlist (`GateRepository.non_link_allowlist`, compared case-insensitively after the same NFKC preparation): approval is refused (the reviewer cannot see where it goes) and publish is blocked with `unresolved_link`. The allowlist is not hashed, so removing an entry after approval makes publish block. `find_unresolved_links(text, resolver, non_links)` lets the copy checks (WP-07) find these early.

## Media

Each file is hashed as sha256 over its bytes, read fresh from storage at the moment of hashing (`MediaItem.from_bytes`). The path is never hashed, so a file moved to another bucket keeps its hash, and a file overwritten at the same path changes it. The media row's own `sha256` is compared separately and a mismatch is its own block reason. Media order carries meaning (carousels), so position order is kept; ties break on hash then media id.

A media row with a `consent_id` needs consent (an identifiable person), checked by `gate/consent.py` at approval and at publish. The consent row must exist and have `given_at` at or before now (`consent_missing`), must not be withdrawn (`consent_withdrawn`), and its scope must cover the use (`consent_scope`): likeness for images and PDFs, voice for audio, and likeness plus voice for video, since the media row has no field saying a video is silent. A scope may list several uses, such as `likeness+voice`.

## Ordering

Order carries no meaning for variants (leaf hashes are sorted), claims (sorted by code, then id, deduplicated) or tracked links (sorted). Order carries meaning for media (position) and links (appearance).

## Encoding

- Each structure is JSON with sorted keys, separators `,` and `:` and no spaces, UTF-8 without `\u` escaping, no NaN or infinity. Floats are refused: every number is an int, every other value a string or `null`.
- Variant leaf: `sha256("thallo/variant/v1\n" + json(leaf))`, lowercase hex.
- Root: `sha256("thallo/bundle-root/v1\n" + json({v, tenant_id, bundle_id, rule_version, variants}))`, lowercase hex.
- The domain prefixes keep a leaf hash from ever being taken for a root and the reverse.

Binding the tenant and bundle id means an approval cannot be reused for a copy of the same content in another bundle or tenant, even before the explicit subject and tenant checks in `decision.py`.

## Publishing (rule for WP-08)

An allowed `GateDecision` carries `payload`, a frozen `PublishPayload` built from the same leaf that was hashed: the canonical outgoing text, every media item in position order with its sha256, mime, alt text, disclosure label and consent id, the resolved links, the tracked links, and the variant leaf hash. A blocked decision carries no payload.

The check and the send happen at different moments, so the publisher must close that gap itself:

1. Call `check_publish` immediately before sending. Do not reuse an older decision.
2. Send exactly `payload.text`, and exactly the media in `payload.media`, in that order, with that alt text and those disclosure labels.
3. Hash the exact bytes it uploads (each media file as uploaded, and the text as sent, UTF-8) and compare them to the payload: each upload's sha256 must equal `payload.media[i].sha256`, and the sent text must equal `payload.text`. On any difference, block, send nothing more, and raise an alert.
4. Put `decision.content_hash` in `PublishRequest.content_hash` and the media hashes from `payload.media_sha256` in `PublishRequest.media_sha256`.

The publisher must never re-read media from storage between the check and the upload without hashing it again against the payload.

## Audit log hashing

`cortex_operator/trust/audit.py` uses the same JSON rules with domain `thallo/audit/v1\n` over every entry field except `entry_hash`, with times as UTC ISO 8601 with microseconds. Each entry carries the previous entry's hash; the first has `prev_hash` null and seq 1. How entries are written and anchored is in `AUDIT.md`.
