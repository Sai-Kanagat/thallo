# Canonical form for the bundle root hash (version 1)

Code: `cortex_operator/gate/canonical.py`. BUILD_PLAN section 5, revision 4. The approval binds to the root defined here; any change to this document's rules changes every root, so it needs a new `CANONICAL_VERSION`, new domain strings, and a lead decision. `tests/wp04_gate/test_canonical.py::test_golden_root_is_stable` pins one root so an accidental change fails CI.

## What the root covers

For every variant in the bundle:

| Field | Form |
|---|---|
| `variant_id` | the row id |
| `channel`, `language` | enum values |
| `text` | canonical text of the source text, claim slots like `{{C-0012}}` included |
| `rendered_text` | canonical text of the rendered text, or `null` |
| `links` | every link in the outgoing text, in order, as `[url, destination]` |
| `media` | one entry per file in position order: `position`, `sha256` of the bytes, `kind`, `mime` (lowercase), canonical `alt_text`, `disclosure_label` |
| `claims` | one entry per linked claim, sorted by code then id, duplicates removed: `id`, `code`, `value` (NFC), `unit`, `as_of` (ISO date), `evidence_sha256`, `wording` (canonical text per language) |

Then the bundle: `tenant_id`, `bundle_id`, `rule_version`, and the sorted list of variant leaf hashes.

Not covered, on purpose: storage paths and URIs, row timestamps, states, `scheduled_for`, `task_type`, `template_ref`, claim `expires_at` (checked at publish time instead), and the canary mark (it must never show in the hash a reviewer sees).

## Text

Applied to every text field, in this order:

1. Unicode NFC. Not NFKC: compatibility characters (full-width digits, ligatures) stay distinct.
2. Line breaks: CRLF, CR, U+0085, U+2028 and U+2029 become LF.
3. Within each line, runs of spaces (U+0020) and tabs become one space; leading and trailing spaces and tabs are removed.
4. Runs of blank lines become one blank line. Leading and trailing blank lines are removed.

Everything else counts. Case, punctuation, no-break spaces, zero-width characters (U+200B, U+200C, U+200D) all change the hash: zero-width joiners shape Tamil, and invisible characters must never be a way to slip a change past an approval.

The publisher sends exactly the canonical outgoing text (`GateDecision.publish_text`), so what is hashed is what goes out.

## Links

A link is an `http` or `https` URL in the outgoing text (rendered text if present, else source text), ending at whitespace, `<`, `>` or `"`, with trailing `.,;:!?)]}'"` removed. Each is normalised: scheme and host lowercased, `:80` on http and `:443` on https dropped, an empty path becomes `/`. User info, path, query and fragment are kept byte for byte, with no percent decoding.

Each link is resolved to its final destination by a resolver the caller supplies (the tracked link table and known redirects; never the network) and the destination is normalised the same way. An unknown link resolves to `null`. So a tracked link whose destination is changed after approval voids the approval.

## Media

Each file is hashed as sha256 over its bytes, read fresh from storage at the moment of hashing (`MediaItem.from_bytes`). The path is never hashed, so a file moved to another bucket keeps its hash, and a file overwritten at the same path changes it. The media row's own `sha256` is compared separately and a mismatch is its own block reason. Media order carries meaning (carousels), so position order is kept; ties break on hash then media id.

## Ordering

Order carries no meaning for variants (leaf hashes are sorted) or claims (sorted by code, then id, deduplicated). Order carries meaning for media (position) and links (appearance).

## Encoding

- Each structure is JSON with sorted keys, separators `,` and `:` and no spaces, UTF-8 without `\u` escaping, no NaN or infinity. Floats are refused: every number is an int, every other value a string or `null`.
- Variant leaf: `sha256("thallo/variant/v1\n" + json(leaf))`, lowercase hex.
- Root: `sha256("thallo/bundle-root/v1\n" + json({v, tenant_id, bundle_id, rule_version, variants}))`, lowercase hex.
- The domain prefixes keep a leaf hash from ever being taken for a root and the reverse.

Binding the tenant and bundle id means an approval cannot be reused for a copy of the same content in another bundle or tenant, even before the explicit subject and tenant checks in `decision.py`.

## Audit log hashing

`cortex_operator/trust/audit.py` uses the same JSON rules with domain `thallo/audit/v1\n` over every entry field except `entry_hash`, with times as UTC ISO 8601 with microseconds. Each entry carries the previous entry's hash; the first has `prev_hash` null and seq 1.
