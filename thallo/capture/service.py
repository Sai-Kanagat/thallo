"""Source capture: the week's questions, recorded or typed answers, the long interview, the shot list.

Every function takes one tenant transaction (`tx`: get, list_all, insert, update; the API's
`backend.tenant(...)` and the store's repo both fit) and never commits on its own, so a caller's
block is one unit.

Rules this module holds, whatever the caller does:
- No consent tick, no recording. The tick writes a `consent` row (scope VOICE, basis opt_in) with
  evidence naming who ticked which wording, when and from which session; the media row and the
  answer point at it. The database refuses a recording without one too (migration 0026).
- What people say is tenant data and untrusted: the `source` row is written with untrusted=True,
  audio sits under the tenant's folder, and nothing here puts a transcript into a prompt. This
  module makes no model call.
- Numbers in a transcript become claim candidates (`candidates.py`), never verified claims.
- A recording is stored under its sha256; sending the same bytes again returns the answer that
  already holds them instead of a second media row.
- Transcription is its own step (`transcribe`), so a long interview never holds the upload open.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from thallo.capture.candidates import extract_candidates
from thallo.capture.questions import monday, questions_for, shots_for
from thallo.capture.records import (
    AnswerMode,
    CaptureAnswer,
    CaptureQuestion,
    ShotRequest,
    ShotState,
    TranscriptState,
)
from thallo.connections.errors import ConnectionsError
from thallo.contracts import connection as c
from thallo.contracts.actors import HumanActor
from thallo.contracts.clock import Clock
from thallo.contracts.enums import ConsentBasis, ConsentScope, Language, MediaKind, MediaProvenance, SourceKind
from thallo.contracts.ids import is_id, new_id
from thallo.contracts.models import Claim, Consent, Media, Play, Source

VOICE_CONSENT_VERSION = "voice-v1"
VOICE_CONSENT_TEXT = ("I agree that this business may keep this recording of my voice and use what I say, "
                      "and my voice, in its marketing. I can withdraw this later.")
PEOPLE_CONSENT_VERSION = "people-v1"
PEOPLE_CONSENT_TEXT = ("Everyone who can be seen or heard in this file agreed to appear in this business's "
                       "marketing, and I can show who agreed if asked.")

MAX_ANSWER_BYTES = 15 * 1024 * 1024  # a few minutes of compressed speech
MAX_INTERVIEW_BYTES = 60 * 1024 * 1024  # under the media store's 64 MB read limit
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 60 * 1024 * 1024
MAX_TYPED_CHARS = 20_000

AUDIO_MIMES = frozenset({"audio/webm", "audio/ogg", "audio/mp4", "audio/mpeg", "audio/wav", "audio/x-wav",
                         "audio/aac", "audio/x-m4a"})
IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/heic"})
VIDEO_MIMES = frozenset({"video/mp4", "video/webm", "video/quicktime"})


class CaptureError(ValueError):
    """Refused input; the message is safe to show the person."""


class NotFound(LookupError):
    pass


class Rows(Protocol):
    def get(self, model: type, row_id: str) -> Any: ...
    def list_all(self, model: type, **where: Any) -> list: ...
    def insert(self, row: Any) -> Any: ...
    def update(self, model: type, row_id: str, **fields: Any) -> bool: ...


class Blobs(Protocol):
    def write(self, tenant_id: str, name: str, data: bytes) -> str: ...


class Runner(Protocol):
    """`ConnectionRegistry.run`: the chain for a capability, ending at the manual floor."""

    def run(self, tenant_id: str, request: c.Request) -> Any: ...


def blob_name(sha256: str) -> str:
    """Where a capture file sits in the tenant folder. The transcription providers read audio
    back from the same name (`providers/transcription/audio.py`)."""
    return f"capture-{sha256}"


# ---------------------------------------------------------------- files

def base_mime(mime: str) -> str:
    return (mime or "").split(";", 1)[0].strip().lower()


def _sniff(data: bytes) -> str | None:
    """The family the first bytes say a file is, so a page cannot store a script as "audio"."""
    head = data[:16]
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if head.startswith(b"OggS"):
        return "ogg"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mpeg"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


_FAMILIES = {
    "audio/webm": {"webm"}, "audio/ogg": {"ogg"}, "audio/mp4": {"mp4"}, "audio/x-m4a": {"mp4"},
    "audio/aac": {"mp4", "mpeg"}, "audio/mpeg": {"mpeg"}, "audio/wav": {"wav"}, "audio/x-wav": {"wav"},
    "image/jpeg": {"jpeg"}, "image/png": {"png"}, "image/webp": {"webp"}, "image/heic": {"mp4"},
    "video/mp4": {"mp4"}, "video/webm": {"webm"}, "video/quicktime": {"mp4"},
}


def check_file(data: bytes, mime: str, allowed: frozenset[str], limit: int) -> str:
    m = base_mime(mime)
    if m not in allowed:
        raise CaptureError(f"this kind of file ({m or 'unknown'}) is not accepted here")
    if not data:
        raise CaptureError("the file is empty")
    if len(data) > limit:
        raise CaptureError(f"the file is over {limit // (1024 * 1024)} MB")
    if _sniff(data) not in _FAMILIES[m]:
        raise CaptureError("the file's contents do not match its type")
    return m


# ---------------------------------------------------------------- the week

def _play_code(tx: Rows) -> str | None:
    plays = sorted(tx.list_all(Play), key=lambda p: (p.created_at, p.id))
    return plays[0].code if plays else None


def week_questions(tx: Rows, tenant_id: str, week: date, clock: Clock) -> list[CaptureQuestion]:
    """The week's three questions, written from the play's defaults the first time they are asked for."""
    start = monday(week)
    have = {q.position: q for q in tx.list_all(CaptureQuestion, week_start=start)}
    if len(have) < 3:
        code = _play_code(tx)
        for pos, text in enumerate(questions_for(code), start=1):
            if pos not in have:
                have[pos] = tx.insert(CaptureQuestion(id=new_id(clock), created_at=clock.now(), tenant_id=tenant_id,
                                                      week_start=start, position=pos, play_code=code, text=text))
    return [have[p] for p in sorted(have)]


def week_shots(tx: Rows, tenant_id: str, week: date, clock: Clock) -> list[ShotRequest]:
    start = monday(week)
    have = sorted(tx.list_all(ShotRequest, week_start=start), key=lambda s: s.position)
    if have:
        return have
    out = []
    for pos, shot in enumerate(shots_for(_play_code(tx)), start=1):
        out.append(tx.insert(ShotRequest(id=new_id(clock), created_at=clock.now(), tenant_id=tenant_id,
                                         week_start=start, position=pos, text=shot.text,
                                         people_in_shot=shot.people_in_shot)))
    return out


def add_shot(tx: Rows, tenant_id: str, week: date, text: str, people_in_shot: bool, clock: Clock) -> ShotRequest:
    text = (text or "").strip()
    if not 1 <= len(text) <= 500:
        raise CaptureError("a shot request is 1 to 500 characters")
    start = monday(week)
    taken = {s.position for s in week_shots(tx, tenant_id, start, clock)}
    pos = next((p for p in range(1, 11) if p not in taken), None)
    if pos is None:
        raise CaptureError("a week holds at most 10 shot requests")
    return tx.insert(ShotRequest(id=new_id(clock), created_at=clock.now(), tenant_id=tenant_id, week_start=start,
                                 position=pos, text=text, people_in_shot=people_in_shot))


def rewrite_question(tx: Rows, question_id: str, text: str) -> CaptureQuestion:
    q = _get(tx, CaptureQuestion, question_id)
    text = (text or "").strip()
    if not 1 <= len(text) <= 500:
        raise CaptureError("a question is 1 to 500 characters")
    if tx.list_all(CaptureAnswer, question_id=q.id):
        raise CaptureError("this question already has an answer; it can no longer change")
    tx.update(CaptureQuestion, q.id, text=text)
    return q.model_copy(update={"text": text})


def _get(tx: Rows, model: type, row_id: str) -> Any:
    if not isinstance(row_id, str) or not is_id(row_id):
        raise NotFound(model.__table__)
    row = tx.get(model, row_id)
    if row is None:
        raise NotFound(model.__table__)
    return row


# ---------------------------------------------------------------- consent

def _consent(tx: Rows, tenant_id: str, actor: HumanActor, scope: ConsentScope, extra: list[ConsentScope],
             version: str, wording: str, clock: Clock) -> Consent:
    now = clock.now()
    session_ref = hashlib.sha256(actor.session_id.encode()).hexdigest()[:16]  # never the raw session id
    evidence = (f"Ticked in the Thallo voice page by app user {actor.id} in session ref {session_ref} "
                f"at {now.isoformat()}. Wording {version}: {wording}")
    return tx.insert(Consent(id=new_id(clock), created_at=now, tenant_id=tenant_id,
                             subject_label=f"app_user:{actor.id}", basis=ConsentBasis.OPT_IN, scope=scope,
                             extra_scopes=extra, evidence=evidence, given_at=now))


# ---------------------------------------------------------------- answers

@dataclass(frozen=True)
class Saved:
    answer: CaptureAnswer
    source: Source
    reused: bool = False


def _store_media(tx: Rows, blobs: Blobs, tenant_id: str, data: bytes, mime: str, kind: MediaKind,
                 provenance: MediaProvenance, consent_id: str | None, voice: bool, clock: Clock) -> tuple[Media, bool]:
    sha = hashlib.sha256(data).hexdigest()
    existing = tx.list_all(Media, sha256=sha)
    if existing:
        return existing[0], True
    uri = blobs.write(tenant_id, blob_name(sha), data)
    media = Media(id=new_id(clock), created_at=clock.now(), tenant_id=tenant_id, kind=kind, sha256=sha, mime=mime,
                  size_bytes=len(data), storage_uri=uri, provenance=provenance, consent_id=consent_id,
                  has_voice_track=voice)
    return tx.insert(media), False


def save_recording(tx: Rows, blobs: Blobs, *, tenant_id: str, actor: HumanActor, data: bytes, mime: str,
                   consent_ticked: bool, clock: Clock, question_id: str | None = None,
                   language: Language | None = None) -> Saved:
    """A recorded answer to one of the week's questions, or (question_id None) the long interview."""
    if consent_ticked is not True:
        raise CaptureError("tick the consent box before recording; nothing was stored")
    interview = question_id is None
    question = None if interview else _get(tx, CaptureQuestion, question_id)
    m = check_file(data, mime, AUDIO_MIMES, MAX_INTERVIEW_BYTES if interview else MAX_ANSWER_BYTES)
    sha = hashlib.sha256(data).hexdigest()
    for media in tx.list_all(Media, sha256=sha):
        for ans in tx.list_all(CaptureAnswer, media_id=media.id):
            return Saved(ans, tx.get(Source, ans.source_id), reused=True)
    consent = _consent(tx, tenant_id, actor, ConsentScope.VOICE, [ConsentScope.QUOTE], VOICE_CONSENT_VERSION,
                       VOICE_CONSENT_TEXT, clock)
    media, _ = _store_media(tx, blobs, tenant_id, data, m, MediaKind.AUDIO, MediaProvenance.PERSON_RECORDING,
                            consent.id, True, clock)
    now = clock.now()
    title = "Long interview" if question is None else question.text
    source = tx.insert(Source(id=new_id(clock), created_at=now, tenant_id=tenant_id,
                              kind=SourceKind.INTERVIEW if interview else SourceKind.VOICE_ANSWER,
                              language=language, title=title[:500], media_id=media.id, consent_id=consent.id,
                              untrusted=True, captured_at=now))
    answer = tx.insert(CaptureAnswer(id=new_id(clock), created_at=now, tenant_id=tenant_id,
                                     question_id=None if question is None else question.id,
                                     mode=AnswerMode.INTERVIEW if interview else AnswerMode.QUESTION,
                                     source_id=source.id, media_id=media.id, consent_id=consent.id,
                                     answered_by=actor.id, transcript_state=TranscriptState.PENDING, updated_at=now))
    return Saved(answer, source)


def save_typed(tx: Rows, *, tenant_id: str, actor: HumanActor, question_id: str, text: str, clock: Clock,
               language: Language | None = None) -> tuple[Saved, list[Claim]]:
    """A typed answer. No recording, so no voice consent; the text is still untrusted."""
    question = _get(tx, CaptureQuestion, question_id)
    text = _clean_text(text)
    now = clock.now()
    source = tx.insert(Source(id=new_id(clock), created_at=now, tenant_id=tenant_id, kind=SourceKind.TYPED,
                              language=language, title=question.text[:500], transcript=text, untrusted=True,
                              captured_at=now))
    answer = tx.insert(CaptureAnswer(id=new_id(clock), created_at=now, tenant_id=tenant_id, question_id=question.id,
                                     mode=AnswerMode.TYPED, source_id=source.id, answered_by=actor.id,
                                     transcript_state=TranscriptState.DONE, provider="typed", updated_at=now))
    claims = _candidates(tx, answer, source, clock)
    return Saved(answer.model_copy(update={"candidates_extracted": True}), source), claims


def _clean_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise CaptureError("the answer is empty")
    if len(text) > MAX_TYPED_CHARS:
        raise CaptureError(f"the answer is over {MAX_TYPED_CHARS} characters")
    if any(ord(ch) < 0x20 and ch not in "\n\t" for ch in text):
        raise CaptureError("the answer holds control characters")
    return text


def _candidates(tx: Rows, answer: CaptureAnswer, source: Source, clock: Clock) -> list[Claim]:
    made, _events = extract_candidates(tx, source, clock=clock)
    tx.update(CaptureAnswer, answer.id, candidates_extracted=True, updated_at=clock.now())
    return made


def transcribe(tx: Rows, runner: Runner, *, tenant_id: str, answer_id: str, clock: Clock,
               cost_cap_eur: Any = None) -> tuple[CaptureAnswer, list[Claim]]:
    """Run the transcription chain for a recorded answer. A served transcript is written to the
    source and its numbers become candidates; the manual floor (empty text, a person's task)
    leaves the answer waiting for someone to type it."""
    answer = _get(tx, CaptureAnswer, answer_id)
    if answer.media_id is None:
        raise CaptureError("a typed answer has nothing to transcribe")
    if answer.transcript_state is TranscriptState.DONE:
        return answer, []
    media = tx.get(Media, answer.media_id)
    source = tx.get(Source, answer.source_id)
    request = c.TranscriptionRequest(tenant_id=tenant_id, media_sha256=media.sha256, language_hint=source.language,
                                     idempotency_key=f"transcribe:{answer.id}", cost_cap_eur=cost_cap_eur)
    now = clock.now()
    try:
        served = runner.run(tenant_id, request)
    except (ConnectionsError, c.CostCapExceeded) as exc:
        tx.update(CaptureAnswer, answer.id, transcript_state=TranscriptState.FAILED, detail=type(exc).__name__,
                  updated_at=now)
        return answer.model_copy(update={"transcript_state": TranscriptState.FAILED}), []
    result = served.result
    out = result.output
    text = out.text.strip() if isinstance(out, c.TranscriptionResult) else ""
    if result.status == "pending_person" or not text:
        tx.update(CaptureAnswer, answer.id, transcript_state=TranscriptState.NEEDS_TYPING, provider=served.provider,
                  detail="type what is said in the recording", updated_at=now)
        return answer.model_copy(update={"transcript_state": TranscriptState.NEEDS_TYPING}), []
    language = out.language or source.language
    tx.update(Source, source.id, transcript=text[:MAX_TYPED_CHARS * 5], language=language)
    tx.update(CaptureAnswer, answer.id, transcript_state=TranscriptState.DONE, provider=served.provider, detail=None,
              updated_at=now)
    source = source.model_copy(update={"transcript": text[:MAX_TYPED_CHARS * 5], "language": language})
    claims = _candidates(tx, answer, source, clock)
    return answer.model_copy(update={"transcript_state": TranscriptState.DONE, "candidates_extracted": True}), claims


def type_transcript(tx: Rows, *, answer_id: str, text: str, clock: Clock) -> tuple[CaptureAnswer, list[Claim]]:
    """A person types (or corrects) what a recording says. Corrections add candidates for new spans."""
    answer = _get(tx, CaptureAnswer, answer_id)
    text = _clean_text(text) if answer.mode is not AnswerMode.INTERVIEW else _clean_long(text)
    source = tx.get(Source, answer.source_id)
    tx.update(Source, source.id, transcript=text)
    tx.update(CaptureAnswer, answer.id, transcript_state=TranscriptState.DONE,
              provider=answer.provider if answer.transcript_state is TranscriptState.DONE else "typed",
              detail=None, updated_at=clock.now())
    claims = _candidates(tx, answer, source.model_copy(update={"transcript": text}), clock)
    return tx.get(CaptureAnswer, answer.id), claims


def _clean_long(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise CaptureError("the transcript is empty")
    if len(text) > MAX_TYPED_CHARS * 5:
        raise CaptureError("the transcript is too long")
    if any(ord(ch) < 0x20 and ch not in "\n\t" for ch in text):
        raise CaptureError("the transcript holds control characters")
    return text


# ---------------------------------------------------------------- shots

def fulfil_shot(tx: Rows, blobs: Blobs, *, tenant_id: str, actor: HumanActor, shot_id: str, data: bytes, mime: str,
                people_consent_ticked: bool, clock: Clock) -> ShotRequest:
    shot = _get(tx, ShotRequest, shot_id)
    if shot.state is not ShotState.OPEN:
        raise CaptureError(f"this shot request is {shot.state.value}")
    is_video = base_mime(mime) in VIDEO_MIMES
    allowed, limit = (VIDEO_MIMES, MAX_VIDEO_BYTES) if is_video else (IMAGE_MIMES, MAX_IMAGE_BYTES)
    m = check_file(data, mime, allowed, limit)
    consent = None
    if shot.people_in_shot:
        if people_consent_ticked is not True:
            raise CaptureError("this shot has people in it: tick that everyone agreed; nothing was stored")
        extra = [ConsentScope.VOICE] if is_video else []
        consent = _consent(tx, tenant_id, actor, ConsentScope.LIKENESS, extra, PEOPLE_CONSENT_VERSION,
                           PEOPLE_CONSENT_TEXT, clock)
    media, reused = _store_media(tx, blobs, tenant_id, data, m, MediaKind.VIDEO if is_video else MediaKind.IMAGE,
                                 MediaProvenance.CLIENT_CLIP if is_video else MediaProvenance.CLIENT_PHOTO,
                                 None if consent is None else consent.id, is_video and shot.people_in_shot, clock)
    if reused and shot.people_in_shot and media.consent_id is None:
        raise CaptureError("this file is already stored without a consent record; ask the operator")
    now = clock.now()
    fields = {"state": ShotState.FULFILLED, "media_id": media.id, "consent_id": None if consent is None else consent.id,
              "fulfilled_by": actor.id, "fulfilled_at": now}
    tx.update(ShotRequest, shot.id, **fields)
    return shot.model_copy(update=fields)


def drop_shot(tx: Rows, shot_id: str) -> ShotRequest:
    shot = _get(tx, ShotRequest, shot_id)
    if shot.state is not ShotState.OPEN:
        raise CaptureError(f"this shot request is {shot.state.value}")
    tx.update(ShotRequest, shot.id, state=ShotState.DROPPED)
    return shot.model_copy(update={"state": ShotState.DROPPED})


def backlog(tx: Rows) -> list[CaptureAnswer]:
    """Answers with a transcript that no piece has used yet: the guard against a week with no answers.
    (Use is tracked on briefs' source_ids by later packages; today every done answer counts.)"""
    return [a for a in tx.list_all(CaptureAnswer) if a.transcript_state is TranscriptState.DONE]


__all__ = [
    "AUDIO_MIMES", "IMAGE_MIMES", "PEOPLE_CONSENT_TEXT", "VIDEO_MIMES", "VOICE_CONSENT_TEXT", "CaptureError",
    "NotFound", "Saved", "add_shot", "backlog", "blob_name", "check_file", "drop_shot", "fulfil_shot",
    "rewrite_question", "save_recording", "save_typed", "transcribe", "type_transcript", "week_questions",
    "week_shots",
]
