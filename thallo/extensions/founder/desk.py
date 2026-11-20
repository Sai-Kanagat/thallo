"""Founder profile desk (WP-29, BUILD_PLAN module 30): posts for the founder's own profile, written
from the founder's own recorded answers and nothing else.

    answers = founder_answers(tx, founder.id)                 # done transcripts or typed answers
    outcome = draft_profile_post(registry=..., tenant=..., founder=..., answers=answers[:1], ...)
    with db.tenant(tenant.id) as repo:
        write_founder_post(repo, outcome)

Guards, all in code:
- Only the founder's own answers are source material: every answer must be `answered_by` this
  founder, its transcript done, its voice consent (when it was recorded) not withdrawn. Any other
  answer raises before a call. No answers: no draft and no call (copy production's NO_SOURCE).
- The piece goes through copy production (truth library, slop, repair loops) on channel
  `linkedin_personal`, then a code check `founder.own_words` measures how much of the post's
  vocabulary comes from the answers: below `FAIL_SHARE` it FAILs (the gate refuses approval and the
  variant goes back to DRAFTING for a person), below `FLAG_SHARE` it FLAGs for the approver.
- A `founder_post` row binds the variant to the founder and the exact answers (migration 0040,
  at least one answer, append-only), so every post can show where its words came from.
- The variant waits in CHECKING for the check stack and a named human at the gate. Nothing here
  publishes, messages, or touches LinkedIn: this module builds no send request, and DMs and
  connection requests are not a capability Thallo has.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from thallo.capture.records import CaptureAnswer, TranscriptState
from thallo.connections.registry import ConnectionRegistry
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Channel, CheckLayer, CheckVerdict, FunnelStage, Language, UserRole
from thallo.contracts.ids import new_id
from thallo.contracts.models import AppUser, Brief, CheckResult, Claim, Consent, Idea, Source, Tenant
from thallo.contracts.states import VariantState
from thallo.copy.produce import CopyOutcome, CopyStatus, produce_copy
from thallo.extensions.founder.records import FounderPost
from thallo.gate.canonical import LinkResolver

PROMPT_VERSION = "founder.post/1"
TASK_TYPE = "founder_post"
CHANNEL = Channel.LINKEDIN_PERSONAL
MAX_CHARS = 2000
OWN_WORDS_CHECK = "founder.own_words"
FAIL_SHARE = 0.35
FLAG_SHARE = 0.55
FOUNDER_ROLES = frozenset({UserRole.FOUNDER})

_SLOT = re.compile(r"\{\{[^}]*\}\}")
_STOP = frozenset("""
a an and are as at be but by can do for from has have he her his how i if in is it its me my no not of on or our
so than that the their them then there these they this to too us was we were what when which who will with you
your il lo la i gli le un uno una di da del della in con su per tra e o che non mi ti si ci noi voi loro sono
""".split())


class FounderError(ValueError):
    """Refused input; the message is safe to show the person."""


@dataclass(frozen=True)
class Answer:
    """One of the founder's answers and the source row that holds what they said (untrusted)."""

    answer: CaptureAnswer
    source: Source

    @property
    def text(self) -> str:
        return (self.source.transcript or "").strip()


def founder_answers(tx: Any, founder_id: str) -> list[Answer]:
    """The founder's usable answers, newest first: transcript done (or typed), text present, and a
    recording's voice consent not withdrawn."""
    consents = {c.id: c for c in tx.list_all(Consent)}
    out: list[Answer] = []
    for a in tx.list_all(CaptureAnswer, answered_by=founder_id):
        if a.transcript_state is not TranscriptState.DONE:
            continue
        if a.consent_id is not None:
            c = consents.get(a.consent_id)
            if c is None or c.withdrawn_at is not None:
                continue
        src = tx.get(Source, a.source_id)
        if src is None or not (src.transcript or "").strip():
            continue
        out.append(Answer(a, src))
    return sorted(out, key=lambda x: (x.answer.created_at, x.answer.id), reverse=True)


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def content_words(text: str) -> set[str]:
    words = re.findall(r"[^\W\d_]+", _fold(_SLOT.sub(" ", text)))
    return {w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w
            for w in words if len(w) > 2 and w not in _STOP}


def own_words_share(post: str, sources: Sequence[str]) -> float:
    """Share of the post's content words that also appear in the founder's answers."""
    have = content_words(post)
    if not have:
        return 0.0
    said = set().union(*(content_words(s) for s in sources)) if sources else set()
    return len(have & said) / len(have)


def own_words(post: str, sources: Sequence[str]) -> tuple[CheckVerdict, str | None]:
    share = own_words_share(post, sources)
    if share < FAIL_SHARE:
        return CheckVerdict.FAIL, (f"only {share:.0%} of the post's words come from the founder's answers; "
                                   "it may say things they did not")
    if share < FLAG_SHARE:
        return CheckVerdict.FLAG, f"{share:.0%} of the post's words come from the founder's answers; read it closely"
    return CheckVerdict.PASS, None


@dataclass(frozen=True)
class FounderOutcome:
    status: CopyStatus
    note: str
    copy: CopyOutcome
    idea: Idea | None = None
    brief: Brief | None = None
    post: FounderPost | None = None
    extra_checks: tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def variant(self) -> Any:
        return self.copy.variant

    def rows(self) -> list[Any]:
        made = self.copy.rows()
        head = [r for r in (self.idea, self.brief) if r is not None] if made else []
        tail = [self.post] if self.post is not None else []
        return [*head, *made, *self.extra_checks, *tail]


def draft_profile_post(*, registry: ConnectionRegistry, tenant: Tenant, founder: AppUser, answers: Sequence[Answer],
                       claims: Mapping[str, Claim], clock: Clock, resolver: LinkResolver, non_links: Collection[str],
                       allowed_claim_ids: Sequence[str] = (), voice_rules: Sequence[str] = (),
                       language: Language | None = None, rule_version: int = 1) -> FounderOutcome:
    """One personal-profile post from the given answers of this founder. `claims` is the truth
    library; only `allowed_claim_ids` may appear (none by default: the post is opinion and story)."""
    if founder.tenant_id != tenant.id or founder.role not in FOUNDER_ROLES:
        raise FounderError("a personal-profile post needs this business's founder")
    for a in answers:
        if (a.answer.tenant_id != tenant.id or a.answer.answered_by != founder.id
                or a.source.id != a.answer.source_id):
            raise FounderError("only the founder's own answers can be used for their profile")
        if a.answer.transcript_state is not TranscriptState.DONE:
            raise FounderError("an answer is not transcribed yet")
    sources = [a.text for a in answers if a.text]
    now = clock.now()
    lang = language or founder.language or tenant.default_language
    idea = Idea(id=new_id(clock), created_at=now, tenant_id=tenant.id, stage=FunnelStage.AWARENESS,
                title=f"Profile post from {len(sources)} answer(s) of {founder.display_name}"[:200],
                rationale="The founder's own recorded answers, for their personal profile.",
                source_ids=[a.source.id for a in answers], prompt_version=PROMPT_VERSION)
    brief = Brief(id=new_id(clock), created_at=now, tenant_id=tenant.id, idea_id=idea.id,
                  audience="People who follow the founder", job_to_be_done="Hear the founder's own view in their words",
                  message="Say only what the founder said in the source material, in the first person.",
                  channels=[CHANNEL], allowed_claim_ids=list(allowed_claim_ids), call_to_action="None",
                  language=lang, prompt_version=PROMPT_VERSION)
    copy = produce_copy(registry=registry, tenant=tenant, brief=brief, claims=claims, sources=sources,
                        channel=CHANNEL, rule_version=rule_version, clock=clock, length="short",
                        max_chars=MAX_CHARS, task_type=TASK_TYPE, voice_rules=voice_rules,
                        resolver=resolver, non_links=non_links)
    if copy.variant is None:
        return FounderOutcome(copy.status, copy.note, copy, idea, brief)
    variant = copy.variant
    sha = copy.check_results[0].content_sha256 if copy.check_results else None
    text = variant.rendered_text or variant.text
    verdict, reason = own_words(text, sources)
    attempt = max((r.attempt for r in copy.check_results), default=1)
    check = CheckResult(id=new_id(clock), created_at=clock.now(), tenant_id=tenant.id, variant_id=variant.id,
                        layer=CheckLayer.CODE, check_name=OWN_WORDS_CHECK, verdict=verdict, reason=reason,
                        attempt=attempt, content_sha256=sha)
    note = copy.note
    if verdict is CheckVerdict.FAIL and variant.state is VariantState.CHECKING:
        copy = replace(copy, variant=variant.model_copy(update={"state": VariantState.DRAFTING}),
                       status=CopyStatus.NEEDS_HUMAN)
        note = f"Sent to a person: {reason}."
    post = FounderPost(id=new_id(clock), created_at=clock.now(), tenant_id=tenant.id, founder_id=founder.id,
                       variant_id=variant.id, answer_ids=[a.answer.id for a in answers])
    return FounderOutcome(copy.status, note, copy, idea, brief, post, (check,))


def write_founder_post(repo: Any, outcome: FounderOutcome) -> None:
    """One transaction: idea, brief, the copy rows, the own-words check and the founder_post row.
    Cost rows are written even when no variant was made."""
    conn = getattr(repo, "conn", None)
    if conn is None or not callable(getattr(conn, "transaction", None)):
        raise ValueError("write_founder_post needs a repo with a transaction")
    rows = outcome.rows()
    if any(str(r.tenant_id).lower() != str(repo.tenant_id).lower() for r in rows):
        raise ValueError("outcome rows belong to another tenant than the repo's")
    with conn.transaction():
        for row in rows:
            repo.insert(row)
