"""What a tracked link may point at. Checked when the link is minted and again at every redirect.

A destination is accepted only if it is:
- https, with a plain host name (letters, digits, hyphens, at least one dot), an optional port,
  no user info, no IP literal, nothing that is not printable ASCII, at most 2000 characters;
- free of personal data: no query key that names a person (email, phone, name, ...), no value
  holding an `@` or a run of seven or more digits. The only digits allowed are the business's own
  WhatsApp number in the path of a wa.me link, which the WhatsApp builder writes.

Parsing is a regex, never the network. `thallo_ref=<slug>` may be added to a landing page
destination when the link is minted, so the page can put the slug into a hidden form field; it is
part of the stored destination, so the gate hashes and the reviewer sees exactly where it goes.
"""
from __future__ import annotations

import re

from thallo.gate.canonical import normalise_url

MAX_LENGTH = 2000
REF_PARAM = "thallo_ref"
WHATSAPP_HOSTS = frozenset({"wa.me", "api.whatsapp.com"})

_URL = re.compile(r"^https://([A-Za-z0-9.-]+)(:\d{1,5})?(/[^?#]*)?(\?[^#]*)?(#.*)?$")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PERSONAL_KEYS = frozenset({
    "email", "e-mail", "mail", "phone", "tel", "telephone", "mobile", "cell", "name", "firstname", "first_name",
    "lastname", "last_name", "surname", "fullname", "full_name", "address", "addr", "dob", "birthday", "user",
    "username", "uid", "userid", "user_id", "customer", "customer_id", "contact", "nome", "cognome", "telefono",
    "cellulare", "indirizzo"})
_LONG_DIGITS = re.compile(r"\d{7,}")
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_WA_PATH = re.compile(r"^/\d{6,15}$")


class BadDestination(ValueError):
    pass


def _unquote(value: str) -> str:
    return _ESCAPE.sub(lambda m: bytes.fromhex(m.group(1)).decode("latin-1"), value.replace("+", " "))


def _host_ok(host: str) -> bool:
    labels = host.split(".")
    if len(labels) < 2 or len(host) > 253:
        return False
    if all(label.isdigit() for label in labels):
        return False  # an IPv4 literal
    return all(_LABEL.match(label) for label in labels) and not labels[-1].isdigit()


def check_destination(url: object) -> str:
    """The normalised destination, or BadDestination with the reason."""
    if not isinstance(url, str) or not url:
        raise BadDestination("destination is empty")
    if len(url) > MAX_LENGTH:
        raise BadDestination("destination is too long")
    if any(not (0x21 <= ord(ch) <= 0x7E) for ch in url) or "\\" in url:
        raise BadDestination("destination has spaces, control or non-ASCII characters")
    url = normalise_url(url)
    m = _URL.match(url)
    if not m:
        raise BadDestination("destination must be an https URL with a plain host and no user info")
    host, _port, path, query, _frag = m.groups()
    if not _host_ok(host):
        raise BadDestination("destination host is not a plain domain name")
    path = path or "/"
    if "@" in _unquote(path):
        raise BadDestination("destination path holds an @; personal data never goes in a link")
    if _LONG_DIGITS.search(path) and not (host in WHATSAPP_HOSTS and _WA_PATH.match(path)):
        raise BadDestination("destination path holds a long number; personal data never goes in a link")
    for pair in (query or "?")[1:].split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        if key.lower() in _PERSONAL_KEYS:
            raise BadDestination(f"query key {key!r} would carry personal data")
        plain = _unquote(value)
        if "@" in plain or _LONG_DIGITS.search(plain):
            raise BadDestination(f"query value for {key!r} looks like personal data")
    return url


def with_ref(destination: str, slug: str) -> str:
    """The destination with `thallo_ref=<slug>` added before any fragment."""
    base, hash_, frag = destination.partition("#")
    sep = "" if base.endswith(("?", "&")) else ("&" if "?" in base else "?")
    return f"{base}{sep}{REF_PARAM}={slug}{hash_}{frag}"


__all__ = ["BadDestination", "REF_PARAM", "check_destination", "with_ref"]
