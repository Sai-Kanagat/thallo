"""Buyer-language notes: the customer's own phrases, for briefs and the buyer panel (module 35).

Deterministic, no model. Phrases come only from recordings a proof consent names, while that
consent is live. A phrase keeps its exact span; any phrase with a digit is dropped (numbers are
claims, not vocabulary), and so is any phrase holding a word of the customer's name or the
agreed name text. What `brief_notes` returns is untrusted data: callers put it in
`TextRequest.untrusted_data`, never in instructions.
"""
from __future__ import annotations

import re
from typing import Any

from thallo.contracts.ids import new_id
from thallo.contracts.models import Consent, Person, Source
from thallo.extensions.proof.records import LIVE, BuyerNote, ProofGrant, Use
from thallo.extensions.proof.service import MAX_NOTES, Proof, ProofError, consent_live

_SENTENCE = re.compile(r"[^.!?\n]+")
MIN_WORDS, MAX_WORDS = 4, 25


def _name_words(*names: str | None) -> set[str]:
    return {w.lower() for n in names if n for w in re.findall(r"\w+", n) if len(w) > 1}


def phrases(text: str, banned: set[str]) -> list[tuple[int, int, str]]:
    out = []
    for m in _SENTENCE.finditer(text):
        raw = m.group(0)
        lead = len(raw) - len(raw.lstrip())
        words = raw.strip()
        if not words or re.search(r"\d", words) or len(words) > 300:
            continue
        tokens = re.findall(r"\w+", words)
        if not (MIN_WORDS <= len(tokens) <= MAX_WORDS) or {t.lower() for t in tokens} & banned:
            continue
        start = m.start() + lead
        out.append((start, start + len(words), words))
    return out


def extract_notes(proof: Proof, grant_id: str) -> list[BuyerNote]:
    """Write notes from every recording the grant names. Running it again adds nothing new."""
    g = proof.tx.get(ProofGrant, grant_id)
    if g is None:
        raise ProofError("no_consent", "no proof consent recorded")
    proof._grant(g.id, Use.QUOTE)
    person = proof.tx.get(Person, g.person_id)
    banned = _name_words(person.display_name if person else None, g.name_shown)
    have = {(n.source_id, n.span_start, n.span_end) for n in proof.tx.list_all(BuyerNote)}
    made = []
    for sid in g.source_ids:
        src = proof.tx.get(Source, sid)
        for start, end, words in phrases((src.transcript or "") if src else "", banned):
            if (sid, start, end) in have:
                continue
            made.append(proof.tx.insert(BuyerNote(id=new_id(proof.clock), created_at=proof.clock.now(),
                                                  tenant_id=proof.tenant_id, grant_id=g.id,
                                                  consent_id=g.consent_id, source_id=sid, span_start=start,
                                                  span_end=end, phrase=words)))
    return made


def brief_notes(tx: Any, now: Any, limit: int = MAX_NOTES) -> list[str]:
    """Phrases a brief may carry as untrusted data: live notes whose consent is live now."""
    grants = {g.id: g for g in tx.list_all(ProofGrant)}
    consents = {c.id: c for c in tx.list_all(Consent)}
    seen: dict[str, None] = {}
    for n in sorted(tx.list_all(BuyerNote), key=lambda n: (n.created_at, n.id), reverse=True):
        g = grants.get(n.grant_id)
        if n.state != LIVE or g is None or not consent_live(consents.get(n.consent_id), g.person_id, now):
            continue
        seen.setdefault(n.phrase, None)
        if len(seen) >= limit:
            break
    return list(seen)


__all__ = ["brief_notes", "extract_notes", "phrases"]
