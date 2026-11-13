# Truth library (WP-03)

Typed claims with proof and expiry, slot filling per language, and the compile step that keeps
numbers, superlatives, comparisons and names out of model-typed copy.

## What the compile step guarantees, and what it does not

As `docs/contracts.md` states it:

GUARANTEED (structural, complete): no digit in any script prints outside a verified slot; slots
cannot combine or be scaled into a number no claim states; a slot fills only from the tenant's
latest live claim version.

BEST EFFORT (lexical, never complete): number words, superlatives, comparatives and names are
caught from per-language lists tuned for precision. The backstops for that layer are the
cross-family second reader (WP-07) and the weekly 20-sentence hand audit. No document or pitch
may claim more than this.

## The two layers in code

STRUCTURAL, `compiler.py`, always BLOCK, blind to `allowed_terms`:
- digits of any script, currency and percent signs, keycap and enclosed digits (input and
  output), invisible characters, letters of another script, malformed slots
- S1: a letter between any two slots
- S2: no multiplier or scale word after a slot (dashes skipped), no sign or rank marker before it
- S3: after filling, every digit sits inside one rendered slot
- S4: a unitless NUMBER needs approved wording for every tenant language (to verify) and for the
  copy's language (to compile)
- S5: latest live version only. Pass `library(repo, tenant_id)` to `compile_text` and `build`;
  verify and change refuse a row that is not the latest version

LEXICAL, `lexical.py` with lists in `lexicon.py`, best effort, tuned for precision:
- BLOCK only high-precision patterns (number words and compounds, superlatives, "than" and "di"
  comparisons), ambiguous words that count something, protected names and their distinctive
  tokens, list words split by separators, and any word too long to parse
- FLAG a few words (first, only, top, latest, ottimo, massimo, garantito), comparatives without
  "than" or "di", and universal claims next to a verb of performance or a product noun
- nothing for articles, pronouns, prepositions, fixed phrases and the tenant's `allowed_terms`
  (never for protected names)

FAIL blocks the build. FLAG passes it and the approver must acknowledge each flag.

## Regression corpora

`tests/wp03_truth/test_corpora.py`: ordinary Italian and English copy must give zero blocks and
at most three flags; every bypass from the review must block.
