"""Proof, referral and customer voice (module 29).

Works on one tenant transaction (`get`, `list_all`, `insert`, `update`), like the lead desk.

Rules this module holds, whatever the caller does:
- No consent, nothing drafted. A person records a proof consent for a won customer (the person on
  the deal's lead), with evidence, under CONSENT_CALL. It writes a `consent` row naming that
  customer (scope QUOTE, plus LIKENESS for a photo) and a `proof_grant` with the uses agreed
  (quote, name, photo, result). The recorder's own voice tick from capture never counts.
- Every quote card and case story is built only from `proof_material`, and every material and
  piece re-checks the consent at the moment it is made. The database refuses material or a
  piece without a consent id.
- A quote is the customer's exact words: a slice of a transcript the person confirmed is theirs,
  stored with its hash and re-checked against the transcript when a piece is drafted. Words with
  a digit are refused: a number goes through the truth library as a result claim instead.
- A result is a verified claim, emitted as its {{C-0001}} slot, never a paraphrased number.
- No model is called here. Pieces are deterministic templates; the gate and a human approve
  any variant they are placed in.
- Review and referral asks are lead desk drafts on the won lead (`Desk._new_draft`, which runs
  the contact consent for the exact recipient) and are sent only by `Desk.approve_and_send` after
  a person's tap. This module never builds a send request.
- Withdrawal marks the material, pieces and notes withdrawn so no future piece can use them, and
  writes a flag for every piece already placed in a variant: `live` when it was published, so a
  person takes it down. The gate refuses any variant carrying such a piece at approval and at
  publish, for any content hash (see `gate.py`). Nothing is unpublished by the machine.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from thallo.contracts.actors import Actor, SessionVerifier, require_human
from thallo.contracts.clock import Clock
from thallo.contracts.enums import (
    ConsentBasis,
    ConsentScope,
    HumanOnlyAction,
    Language,
    MediaKind,
    MessageKind,
    OutcomeKind,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import (
    Claim,
    Consent,
    Deal,
    Lead,
    Media,
    Message,
    Outcome,
    Person,
    Publication,
    Source,
    Variant,
)
from thallo.contracts.states import LeadState
from thallo.extensions.proof.records import (
    LIVE,
    WITHDRAWN,
    AskKind,
    BuyerNote,
    MaterialKind,
    PieceKind,
    ProofAsk,
    ProofFlag,
    ProofGrant,
    ProofMaterial,
    ProofPiece,
    Use,
)
from thallo.truth.claims import problem as claim_problem

MAX_QUOTE = 300
MIN_QUOTE = 8
MAX_NOTES = 20
ANONYMOUS = {Language.EN: "A customer", Language.IT: "Un cliente"}
_DIGIT = re.compile(r"\d")
_LINK = re.compile(r"^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~%/?&=+-]*)?$")
# A use needs these consent scopes on the customer's consent row.
_SCOPES: dict[Use, frozenset[ConsentScope]] = {
    Use.QUOTE: frozenset({ConsentScope.QUOTE}),
    Use.NAME: frozenset({ConsentScope.QUOTE}),
    Use.RESULT: frozenset({ConsentScope.QUOTE}),
    Use.PHOTO: frozenset({ConsentScope.LIKENESS}),
}


class ProofError(Exception):
    """A refusal a person can read. `code` is stable for the API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str | None, cap: int) -> str | None:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t[:cap] if t else None


def consent_live(consent: Consent | None, person_id: str, now: datetime) -> bool:
    return (consent is not None and consent.person_id == person_id and consent.given_at <= now
            and (consent.withdrawn_at is None or consent.withdrawn_at > now))


@dataclass
class Proof:
    tx: Any
    tenant_id: str
    clock: Clock

    # ------------------------------------------------------------------ consent (the guard)
    def won_deal(self, deal_id: str) -> tuple[Deal, Lead, Person]:
        deal = self.tx.get(Deal, deal_id)
        if deal is None:
            raise ProofError("not_found", "no such deal")
        won = any(o.kind is OutcomeKind.WON for o in self.tx.list_all(Outcome, deal_id=deal.id))
        lead = self.tx.get(Lead, deal.lead_id)
        if not won or lead is None or lead.state is not LeadState.WON:
            raise ProofError("not_won", "proof comes only from a won deal")
        person = self.tx.get(Person, lead.person_id)
        if person is None:
            raise ProofError("not_found", "the deal has no customer")
        return deal, lead, person

    def record_consent(self, deal_id: str, *, uses: Iterable[Use], evidence: str, actor: Actor,
                       verifier: SessionVerifier, name_shown: str | None = None,
                       source_ids: Iterable[str] = ()) -> ProofGrant:
        """A person records what the customer agreed to, with its evidence (who said yes, how, when)."""
        human = require_human(actor, HumanOnlyAction.CONSENT_CALL, verifier=verifier, tenant_id=self.tenant_id)
        deal, _lead, person = self.won_deal(deal_id)
        agreed = sorted({Use(u) for u in uses})
        ev = _clean(evidence, 500)
        if not agreed or ev is None:
            raise ProofError("bad_consent", "a proof consent needs what was agreed and its evidence")
        name = _clean(name_shown, 80)
        if Use.NAME in agreed and name is None:
            raise ProofError("bad_consent", "say the exact name the customer agreed to show")
        if Use.NAME not in agreed:
            name = None
        sources = list(dict.fromkeys(source_ids))
        for sid in sources:
            src = self.tx.get(Source, sid)
            if src is None or not src.transcript:
                raise ProofError("bad_source", "each recording must exist and have a transcript")
        if Use.QUOTE in agreed and not sources:
            raise ProofError("bad_consent", "a quote needs the recording the customer's words come from")
        scopes = sorted(set().union(*(_SCOPES[u] for u in agreed)), key=lambda s: s.value)
        primary = ConsentScope.QUOTE if ConsentScope.QUOTE in scopes else scopes[0]
        now = self.clock.now()
        consent = self.tx.insert(Consent(
            id=new_id(self.clock), created_at=now, tenant_id=self.tenant_id, person_id=person.id,
            basis=ConsentBasis.OPT_IN, scope=primary, extra_scopes=[s for s in scopes if s is not primary],
            evidence=f"Proof consent recorded by app user {human.id} at {now.isoformat()} for "
                     f"{', '.join(u.value for u in agreed)}. {ev}", given_at=now))
        return self.tx.insert(ProofGrant(id=new_id(self.clock), created_at=now, tenant_id=self.tenant_id,
                                         consent_id=consent.id, person_id=person.id, deal_id=deal.id, uses=agreed,
                                         name_shown=name, source_ids=sources, recorded_by=human.id))

    def withdraw(self, consent_id: str, *, actor: Actor, verifier: SessionVerifier) -> dict[str, int]:
        """The customer said stop. Recorded by a person; then the sweep runs."""
        require_human(actor, HumanOnlyAction.CONSENT_CALL, verifier=verifier, tenant_id=self.tenant_id)
        c = self.tx.get(Consent, consent_id)
        if c is None:
            raise ProofError("not_found", "no such consent")
        if c.withdrawn_at is None:
            self.tx.update(Consent, c.id, withdrawn_at=self.clock.now())
        return self.sweep()

    def _grant(self, grant_id: str, need: Use) -> tuple[ProofGrant, Consent]:
        """The grant and its consent, live now and covering `need`, or a refusal."""
        g = self.tx.get(ProofGrant, grant_id)
        if g is None:
            raise ProofError("no_consent", "no proof consent recorded")
        c = self.tx.get(Consent, g.consent_id)
        if not consent_live(c, g.person_id, self.clock.now()):
            raise ProofError("no_consent", "the customer's consent is missing or withdrawn")
        if need not in g.uses or not _SCOPES[need] <= c.scopes:
            raise ProofError("consent_scope", f"the customer did not agree to {need.value}")
        return g, c

    # ------------------------------------------------------------------ material
    def _material(self, g: ProofGrant, kind: MaterialKind, **fields: Any) -> ProofMaterial:
        return self.tx.insert(ProofMaterial(id=new_id(self.clock), created_at=self.clock.now(),
                                            tenant_id=self.tenant_id, kind=kind, grant_id=g.id,
                                            consent_id=g.consent_id, person_id=g.person_id, deal_id=g.deal_id,
                                            **fields))

    def add_quote(self, grant_id: str, *, source_id: str, start: int, end: int) -> ProofMaterial:
        """The customer's exact words: transcript[start:end], unchanged."""
        g, _ = self._grant(grant_id, Use.QUOTE)
        if source_id not in g.source_ids:
            raise ProofError("bad_source", "this recording is not one the customer's consent names")
        src = self.tx.get(Source, source_id)
        text = (src.transcript or "") if src else ""
        if not (0 <= start < end <= len(text)):
            raise ProofError("bad_span", "the quote is not inside the transcript")
        words = text[start:end]
        if words != words.strip() or not (MIN_QUOTE <= len(words) <= MAX_QUOTE):
            raise ProofError("bad_span", f"a quote is {MIN_QUOTE} to {MAX_QUOTE} characters, no edge spaces")
        if _DIGIT.search(words):
            raise ProofError("number_in_quote", "a number in a quote must be a verified result claim instead")
        return self._material(g, MaterialKind.QUOTE, source_id=source_id, span_start=start, span_end=end,
                              text=words, text_sha256=_sha(words))

    def add_photo(self, grant_id: str, *, media_id: str) -> ProofMaterial:
        g, _ = self._grant(grant_id, Use.PHOTO)
        m = self.tx.get(Media, media_id)
        if m is None or m.kind is not MediaKind.IMAGE:
            raise ProofError("bad_media", "a proof photo is an image in the media library")
        return self._material(g, MaterialKind.PHOTO, media_id=media_id)

    def add_result(self, grant_id: str, *, claim_id: str) -> ProofMaterial:
        g, _ = self._grant(grant_id, Use.RESULT)
        self._claim_ok(claim_id)
        return self._material(g, MaterialKind.RESULT, claim_id=claim_id)

    def _claim_ok(self, claim_id: str) -> Claim:
        cl = self.tx.get(Claim, claim_id)
        if (why := claim_problem(cl, self.clock.now())) is not None:
            raise ProofError("claim_not_verified", why)
        assert cl is not None
        if any(o.code == cl.code and o.version > cl.version for o in self.tx.list_all(Claim)):
            raise ProofError("claim_not_verified", f"claim {cl.code} has a newer version")
        return cl

    def _usable(self, material_id: str) -> ProofMaterial:
        """Material that may go into a piece now: live, consent live and covering its kind, words intact."""
        m = self.tx.get(ProofMaterial, material_id)
        if m is None or m.state != LIVE:
            raise ProofError("material_withdrawn", "this material is withdrawn or gone")
        need = {MaterialKind.QUOTE: Use.QUOTE, MaterialKind.PHOTO: Use.PHOTO, MaterialKind.RESULT: Use.RESULT}[m.kind]
        g, _ = self._grant(m.grant_id, need)
        if m.consent_id != g.consent_id:
            raise ProofError("no_consent", "material and consent disagree")
        if m.kind is MaterialKind.QUOTE:
            src = self.tx.get(Source, m.source_id) if m.source_id else None
            now_text = (src.transcript or "")[m.span_start:m.span_end] if src else ""
            if not m.text or now_text != m.text or _sha(m.text) != m.text_sha256:
                raise ProofError("quote_changed", "the transcript no longer holds these exact words")
        if m.kind is MaterialKind.RESULT:
            self._claim_ok(m.claim_id or "")
        return m

    # ------------------------------------------------------------------ pieces
    def _attribution(self, g: ProofGrant, language: Language) -> str:
        if Use.NAME in g.uses and g.name_shown:
            return g.name_shown
        return ANONYMOUS.get(language, ANONYMOUS[Language.EN])

    def _piece(self, kind: PieceKind, mats: list[ProofMaterial], body: str, card: dict,
               claim_ids: list[str]) -> ProofPiece:
        if len({m.consent_id for m in mats}) != 1:
            raise ProofError("mixed_consent", "one piece tells one customer's story under one consent")
        m0 = mats[0]
        return self.tx.insert(ProofPiece(id=new_id(self.clock), created_at=self.clock.now(),
                                         tenant_id=self.tenant_id, kind=kind, deal_id=m0.deal_id,
                                         person_id=m0.person_id, consent_id=m0.consent_id,
                                         material_ids=[m.id for m in mats], claim_ids=claim_ids, body=body,
                                         card=card))

    def quote_card(self, quote_id: str, *, photo_id: str | None = None,
                   language: Language = Language.EN) -> ProofPiece:
        q = self._usable(quote_id)
        if q.kind is not MaterialKind.QUOTE:
            raise ProofError("bad_material", "a quote card starts from a quote")
        mats = [q]
        photo = None
        if photo_id is not None:
            photo = self._usable(photo_id)
            if photo.kind is not MaterialKind.PHOTO:
                raise ProofError("bad_material", "the image must be photo material")
            mats.append(photo)
        g = self.tx.get(ProofGrant, q.grant_id)
        c = self.tx.get(Consent, q.consent_id)
        who = self._attribution(g, language)
        card = {"quote": q.text, "attribution": who, "photo_media_id": photo.media_id if photo else None,
                "consent_id": q.consent_id, "consent_scope": sorted(s.value for s in c.scopes),
                "uses": sorted(u.value for u in g.uses), "source_id": q.source_id,
                "span": [q.span_start, q.span_end], "quote_sha256": q.text_sha256}
        return self._piece(PieceKind.QUOTE_CARD, mats, f"“{q.text}”\n{who}", card, [])

    def case_story(self, material_ids: list[str], *, language: Language = Language.EN) -> ProofPiece:
        """Quotes in the customer's exact words and results as claim slots, nothing else."""
        mats = [self._usable(i) for i in dict.fromkeys(material_ids)]
        quotes = [m for m in mats if m.kind is MaterialKind.QUOTE]
        results = [m for m in mats if m.kind is MaterialKind.RESULT]
        if not quotes or not results:
            raise ProofError("thin_story", "a case story needs the customer's words and a verified result")
        g = self.tx.get(ProofGrant, mats[0].grant_id)
        who = self._attribution(g, language)
        claims = [self._claim_ok(m.claim_id or "") for m in results]
        heads = {Language.IT: ("Cosa dice", "Il risultato"), Language.EN: ("In their words", "The result")}
        say, res = heads.get(language, heads[Language.EN])
        lines = [f"{say}: {who}", *(f"“{m.text}”" for m in quotes), f"{res}:",
                 *(f"{{{{{cl.code}}}}}" for cl in claims)]
        card = {"attribution": who, "consent_id": mats[0].consent_id, "uses": sorted(u.value for u in g.uses),
                "quotes": [{"text": m.text, "source_id": m.source_id, "span": [m.span_start, m.span_end]}
                           for m in quotes],
                "photo_media_ids": [m.media_id for m in mats if m.kind is MaterialKind.PHOTO]}
        return self._piece(PieceKind.CASE_STORY, mats, "\n".join(lines), card, [cl.id for cl in claims])

    def place(self, piece_id: str, variant_id: str) -> ProofPiece:
        """Record that a piece went into a variant. Re-checks every material's consent now."""
        p = self.tx.get(ProofPiece, piece_id)
        if p is None or p.state != LIVE:
            raise ProofError("piece_withdrawn", "this piece is withdrawn or gone")
        if p.variant_id is not None and p.variant_id != variant_id:
            raise ProofError("already_placed", "a piece goes into one variant; draft a new one")
        for mid in p.material_ids:
            self._usable(mid)
        if self.tx.get(Variant, variant_id) is None:
            raise ProofError("not_found", "no such variant")
        self.tx.update(ProofPiece, p.id, variant_id=variant_id)
        return p.model_copy(update={"variant_id": variant_id})

    # ------------------------------------------------------------------ withdrawal
    def sweep(self) -> dict[str, int]:
        """Idempotent: anything resting on a withdrawn consent is withdrawn, placed pieces flagged.
        Keyed on `consent.withdrawn_at`, so it also catches withdrawals recorded elsewhere."""
        now = self.clock.now()
        gone = {c.id for c in self.tx.list_all(Consent) if c.withdrawn_at is not None and c.withdrawn_at <= now}
        counts = {"material": 0, "pieces": 0, "notes": 0, "flags": 0, "live_flags": 0}
        if not gone:
            return counts
        for m in self.tx.list_all(ProofMaterial):
            if m.consent_id in gone and m.state == LIVE:
                self.tx.update(ProofMaterial, m.id, state=WITHDRAWN, withdrawn_at=now)
                counts["material"] += 1
        for n in self.tx.list_all(BuyerNote):
            if n.consent_id in gone and n.state == LIVE:
                self.tx.update(BuyerNote, n.id, state=WITHDRAWN)
                counts["notes"] += 1
        flags = {f.piece_id: f for f in self.tx.list_all(ProofFlag)}
        published = {p.variant_id for p in self.tx.list_all(Publication)}
        for p in self.tx.list_all(ProofPiece):
            if p.consent_id not in gone:
                continue
            if p.state == LIVE:
                self.tx.update(ProofPiece, p.id, state=WITHDRAWN, withdrawn_at=now)
                counts["pieces"] += 1
            if p.variant_id is None:
                continue
            live = p.variant_id in published
            old = flags.get(p.id)
            if old is None:
                self.tx.insert(ProofFlag(id=new_id(self.clock), created_at=now, tenant_id=self.tenant_id,
                                         piece_id=p.id, consent_id=p.consent_id, variant_id=p.variant_id,
                                         live=live))
                counts["flags"] += 1
                counts["live_flags"] += int(live)
            elif live and not old.live:  # published after the flag was written: a person must take it down
                self.tx.update(ProofFlag, old.id, live=True, resolved_by=None, resolved_at=None)
                counts["live_flags"] += 1
        return counts

    def open_flags(self) -> list[ProofFlag]:
        return [f for f in self.tx.list_all(ProofFlag) if f.resolved_at is None]

    def resolve_flag(self, flag_id: str, *, actor: Actor, verifier: SessionVerifier) -> None:
        """A person says the piece is taken down (or was never out)."""
        human = require_human(actor, HumanOnlyAction.CONSENT_CALL, verifier=verifier, tenant_id=self.tenant_id)
        f = self.tx.get(ProofFlag, flag_id)
        if f is None or f.resolved_at is not None:
            raise ProofError("not_found", "no open flag")
        self.tx.update(ProofFlag, f.id, resolved_by=human.id, resolved_at=self.clock.now())

    # ------------------------------------------------------------------ asks, through the lead desk
    def draft_ask(self, desk: Any, lead_id: str, kind: AskKind, *, review_link: str | None = None) -> Message:
        """A review or referral ask to the won customer, as a lead desk draft. The desk checks the
        contact consent for the exact recipient; a person reads, may edit, and taps send there."""
        k = AskKind(kind)
        lead = desk.lead(lead_id)
        if lead.state is not LeadState.WON:
            raise ProofError("not_won", "ask for a review or referral only after a won deal")
        if any(a.kind is k for a in self.tx.list_all(ProofAsk, lead_id=lead.id)):
            raise ProofError("already_asked", f"this customer already has a {k.value} ask")
        link = None
        if k is AskKind.REVIEW:
            link = review_link if review_link and len(review_link) <= 300 and _LINK.match(review_link) else None
            if link is None:
                raise ProofError("bad_link", "a review ask needs the business's https review link")
        person = desk.person(lead)
        body = ask_text(k, person.language or Language.EN, person.display_name, link)
        msg = desk._new_draft(lead, MessageKind.REPLY_DRAFT, body, prompt_version=f"proof-{k.value}-v1")
        if msg is None:
            raise ProofError("no_consent", "no contact consent for this customer's number or address")
        self.tx.insert(ProofAsk(id=new_id(self.clock), created_at=self.clock.now(), tenant_id=self.tenant_id,
                                kind=k, lead_id=lead.id, message_id=msg.id))
        return msg


_ASK = {
    (AskKind.REVIEW, Language.EN): "Hi{name}, thank you for choosing us. If you are happy with how it went, "
                                   "would you leave us a short review? {link}",
    (AskKind.REVIEW, Language.IT): "Ciao{name}, grazie per averci scelto. Se sei contento di come è andata, "
                                   "ci lasceresti una breve recensione? {link}",
    (AskKind.REFERRAL, Language.EN): "Hi{name}, thank you again. If you know someone who could use the same help, "
                                     "feel free to pass this message on. No pressure at all.",
    (AskKind.REFERRAL, Language.IT): "Ciao{name}, grazie ancora. Se conosci qualcuno a cui potremmo essere utili, "
                                     "puoi girargli questo messaggio. Nessun impegno.",
}


def ask_text(kind: AskKind, language: Language, display_name: str | None, link: str | None) -> str:
    lang = language if (kind, language) in _ASK else Language.EN
    first = re.sub(r"[^\w'-]", "", (display_name or "").split(" ")[0])[:40]
    return _ASK[(kind, lang)].format(name=f" {first}" if first else "", link=link or "").strip()


__all__ = ["ANONYMOUS", "Proof", "ProofError", "ask_text", "consent_live"]
