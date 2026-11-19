"""Slugs and human codes. Plain code, no model.

- A slug is the opaque tail of a public redirect URL: 10 characters of lowercase base32
  (50 bits), so it never contains `/`, `?` or `#` and survives `normalise_url` unchanged.
- A code is what a person types or says: a WhatsApp reference, an offer code at the counter,
  a code read out on a call. Six characters from an alphabet with no look-alikes (no 0/O, 1/I/L,
  2/Z, 5/S, 8/B, 6/G), upper case. Codes are matched only against the tenant's own known codes,
  so a random word is never mistaken for one.
"""
from __future__ import annotations

import re
import secrets
import unicodedata

SLUG_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
SLUG_LENGTH = 10
SLUG_RE = re.compile(rf"^[{SLUG_ALPHABET}]{{{SLUG_LENGTH}}}$")

CODE_ALPHABET = "ACDEFHJKMNPRTUVWXY3479"
CODE_LENGTH = 6
CODE_RE = re.compile(rf"^[{CODE_ALPHABET}]{{{CODE_LENGTH}}}$")
_CODE_TOKEN = re.compile(r"[A-Za-z0-9]+")
MAX_CANDIDATES = 20  # a long message cannot make us run many lookups


def new_slug() -> str:
    return "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_LENGTH))


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def is_slug(value: object) -> bool:
    return isinstance(value, str) and bool(SLUG_RE.match(value))


def normalise_code(raw: object) -> str | None:
    """A typed or spoken code in canonical form, or None. Case, spaces and dashes are forgiven."""
    if not isinstance(raw, str) or len(raw) > 40:
        return None
    text = unicodedata.normalize("NFKC", raw).upper()
    text = "".join(ch for ch in text if ch.isalnum())
    return text if CODE_RE.match(text) else None


def code_candidates(text: object) -> list[str]:
    """Tokens in a message that could be a code, in order, deduplicated, at most MAX_CANDIDATES.
    The caller keeps only those that are the tenant's real codes. The message itself is never
    stored."""
    if not isinstance(text, str):
        return []
    text = unicodedata.normalize("NFKC", text[:4000])
    out: list[str] = []
    for tok in _CODE_TOKEN.findall(text):
        code = normalise_code(tok)
        if code and code not in out:
            out.append(code)
            if len(out) >= MAX_CANDIDATES:
                break
    return out


__all__ = ["CODE_ALPHABET", "CODE_RE", "SLUG_RE", "code_candidates", "is_slug", "new_code", "new_slug",
           "normalise_code"]
