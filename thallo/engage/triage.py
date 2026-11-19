"""Deterministic triage. Plain rules, no model: abuse and crisis never depend on a model.

Precedence: crisis, abuse, lead, complaint, spam, praise. A crisis or abuse match wins over
everything, so a threatening comment that also asks a price is flagged, not treated as a lead.
Matching runs on a folded copy (lower case, accents removed). The text is untrusted data.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from thallo.engage.records import Category

RULES: tuple[tuple[Category, str, tuple[str, ...]], ...] = (
    (Category.CRISIS, "safety or legal risk", (
        r"\bsuicid", r"\bkill (my|him|her|your)self\b", r"\bfarmi del male\b", r"\bfarla finita\b",
        r"\bpoisoned\b", r"\bavvelenat", r"\bintossicaz", r"\bfood poisoning\b", r"\binjur", r"\bferit[oai]\b",
        r"\bincidente\b", r"\baccident\b", r"\bfire\b", r"\bincendio\b", r"\blawyer\b", r"\bavvocat",
        r"\bdenunc", r"\bsue\b", r"\blawsuit\b", r"\bpolice\b", r"\bpolizia\b", r"\bcarabinieri\b",
        r"\bdata breach\b", r"\bboycott", r"\bboicott")),
    (Category.ABUSE, "abusive or threatening", (
        r"\bidiot", r"\bstupid", r"\bmoron", r"\bscum\b", r"\bfuck", r"\bshit\b", r"\bbitch",
        r"\bcretin", r"\bimbecill", r"\bstronz", r"\bcazz", r"\bvaffanculo\b", r"\bfanculo\b", r"\bmerda\b",
        r"\btruffator", r"\bladr[io]\b", r"\bi will (find|hurt|get) you\b", r"\bti (trovo|ammazzo)\b",
        r"\bthreat")),
    (Category.LEAD, "buying intent", (
        r"\bhow much\b", r"\bprice\b", r"\bpricing\b", r"\bquote\b", r"\bavailable\b", r"\bin stock\b",
        r"\bappointment\b", r"\btest ride\b", r"\btest drive\b", r"\bbuy\b",
        r"\bquanto costa\b", r"\bquanto viene\b", r"\bprezz", r"\bpreventiv", r"\bdisponibil",
        r"\bappuntament", r"\bprenot", r"\bcomprare\b", r"\bacquist", r"\bordinare\b",
        r"\bcontattatemi\b", r"\bcontact me\b", r"\bcall me\b", r"\bchiamatemi\b")),
    (Category.COMPLAINT, "unhappy customer", (
        r"\bterrible\b", r"\bawful\b", r"\bworst\b", r"\brefund\b", r"\bbroken\b", r"\bnever again\b",
        r"\bdisappoint", r"\bscam\b", r"\bpessim", r"\bvergogn", r"\brimborso\b", r"\brott[oa]\b",
        r"\bdelus[oa]\b", r"\bmai piu\b", r"\bschifo\b", r"\btruffa\b", r"\bnon funziona\b",
        r"\bnessuno risponde\b", r"\bno one answers\b")),
    (Category.SPAM, "looks like spam", (
        r"\bfollow (me|back)\b", r"\bcheck my (page|profile)\b", r"\bdm me\b", r"\bcrypto\b", r"\bbitcoin\b",
        r"\bpromo code\b", r"\bguadagn\w* da casa\b", r"\bearn \$", r"https?://")),
    (Category.PRAISE, "kind words", (
        r"\bthank", r"\bgreat\b", r"\blove\b", r"\bamazing\b", r"\bgrazie\b", r"\bbravi\b", r"\bbellissim",
        r"\bfantastic", r"\bottim", r"\bcomplimenti\b")),
)
_COMPILED = tuple((cat, why, tuple(re.compile(p) for p in pats)) for cat, why, pats in RULES)
_ORDER = [cat for cat, _, _ in RULES]
_PHONE = re.compile(r"(?:\+|00)?\d[\d .-]{7,16}\d")
_EMAIL = re.compile(r"[^@\s]{1,64}@[a-z0-9.-]{1,190}\.[a-z]{2,24}")


def fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", t.casefold())


@dataclass(frozen=True)
class Triage:
    category: Category
    reasons: tuple[str, ...]


def triage(text: str) -> Triage:
    t = fold(text)
    hits = [(cat, why) for cat, why, pats in _COMPILED if any(p.search(t) for p in pats)]
    if (_PHONE.search(t) or _EMAIL.search(t)) and not any(c in (Category.CRISIS, Category.ABUSE) for c, _ in hits):
        hits.append((Category.LEAD, "left a phone number or address"))
    if not hits:
        return Triage(Category.QUESTION, ("asks something",)) if "?" in t else Triage(Category.OTHER, ())
    hits.sort(key=lambda h: _ORDER.index(h[0]))
    top = hits[0][0]
    if top is Category.PRAISE and "?" in t:
        top = Category.QUESTION
    return Triage(top, tuple(dict.fromkeys(why for _, why in hits)))


__all__ = ["RULES", "Triage", "fold", "triage"]
