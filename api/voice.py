"""Source capture routes (WP-10): the week's questions, recorded or typed answers, the long
interview, transcription, claim candidates to check, and the shot list.

Uploads are the raw request body (Content-Type: the file's type), read in chunks and cut off at
the size cap, so no multipart parser is needed. Every write needs the session's CSRF token; the
tenant always comes from the session. Consent is a query flag the server checks itself: without
`consent=1` nothing is stored. Transcripts are untrusted tenant text and are returned as plain
data for the page to render as text.

  GET  /api/voice?week=YYYY-MM-DD
  PUT  /api/voice/questions/{id}                  {text}            founder, operator
  POST /api/voice/questions/{id}/recording?consent=1&language=en   raw audio
  POST /api/voice/questions/{id}/typed            {text, language}
  POST /api/voice/interview/recording?consent=1&language=en         raw audio
  POST /api/voice/answers/{id}/transcribe
  POST /api/voice/answers/{id}/transcript         {text}
  POST /api/voice/shots                           {text, people_in_shot, week}   founder, operator
  POST /api/voice/shots/{id}/file?consent=1       raw image or video
  POST /api/voice/shots/{id}/drop                                   founder, operator
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from api.context import PLANNERS, member, services
from api.errors import ApiError, bad_request, not_found
from thallo.capture import service as cap
from thallo.capture.records import CaptureAnswer, CaptureQuestion, ShotRequest
from thallo.connections.registry import ConnectionRegistry
from thallo.contracts.enums import Capability, Language, UserRole
from thallo.contracts.models import Claim, Source

router = APIRouter(prefix="/api/voice")
CAPTURERS = frozenset(UserRole)  # anyone in the business can answer or send a shot
CHUNK = 64 * 1024


class TextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=cap.MAX_TYPED_CHARS * 5)
    language: Language | None = None


class ShotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=500)
    people_in_shot: bool = False
    week: date | None = None


def _language(raw: str | None) -> Language | None:
    if raw in (None, ""):
        return None
    try:
        return Language(raw)
    except ValueError:
        raise bad_request("language must be en, it or ta") from None


async def _body(request: Request, limit: int) -> bytes:
    size = request.headers.get("content-length")
    if size is not None and size.isdigit() and int(size) > limit:
        raise ApiError(413, "too_large", f"the file is over {limit // (1024 * 1024)} MB")
    out = bytearray()
    async for chunk in request.stream():
        out += chunk
        if len(out) > limit:
            raise ApiError(413, "too_large", f"the file is over {limit // (1024 * 1024)} MB")
    return bytes(out)


def _run(fn, *a, **kw):  # noqa: ANN001, ANN002, ANN003, ANN202
    try:
        return fn(*a, **kw)
    except cap.NotFound:
        raise not_found() from None
    except cap.CaptureError as exc:
        raise bad_request(str(exc)) from None


def _claim_view(c: Claim) -> dict:
    return {"id": c.id, "code": c.code, "kind": c.kind.value, "value": c.value, "unit": c.unit,
            "state": c.state.value, "span": c.source_span}


def _answer_view(a: CaptureAnswer, sources: dict[str, Source], claims: list[Claim]) -> dict:
    s = sources.get(a.source_id)
    return {"id": a.id, "question_id": a.question_id, "mode": a.mode.value, "state": a.transcript_state.value,
            "provider": a.provider, "detail": a.detail, "created_at": a.created_at.isoformat(),
            "transcript": s.transcript if s else None, "language": s.language.value if s and s.language else None,
            "candidates": [_claim_view(c) for c in claims if c.source_id == a.source_id]}


@router.get("")
def week(request: Request, week: str | None = None) -> dict:
    ctx = member(request, CAPTURERS)
    svc = services(request)
    try:
        day = date.fromisoformat(week) if week else svc.clock.now().date()
    except ValueError:
        raise bad_request("week must be a date like 2026-01-05") from None
    with svc.backend.tenant(ctx.tenant_id) as tx:
        questions = cap.week_questions(tx, ctx.tenant_id, day, svc.clock)
        shots = cap.week_shots(tx, ctx.tenant_id, day, svc.clock)
        qids = {q.id for q in questions}
        start = questions[0].week_start
        end = start + timedelta(days=7)
        answers = sorted((a for a in tx.list_all(CaptureAnswer)
                          if a.question_id in qids or (a.question_id is None and start <= a.created_at.date() < end)),
                         key=lambda a: (a.created_at, a.id))
        sources = {s.id: s for s in (tx.get(Source, a.source_id) for a in answers) if s is not None}
        claims = [c for c in tx.list_all(Claim) if c.source_id in sources]
    views = [_answer_view(a, sources, claims) for a in answers]
    return {"week_start": questions[0].week_start.isoformat(),
            "questions": [{"id": q.id, "position": q.position, "text": q.text,
                           "answers": [v for v in views if v["question_id"] == q.id]} for q in questions],
            "interviews": [v for v in views if v["mode"] == "interview"],
            "shots": [{"id": s.id, "text": s.text, "people_in_shot": s.people_in_shot, "state": s.state.value,
                       "media_id": s.media_id} for s in shots],
            "consent": {"voice": cap.VOICE_CONSENT_TEXT, "people": cap.PEOPLE_CONSENT_TEXT},
            "limits": {"answer_mb": cap.MAX_ANSWER_BYTES // 2**20, "interview_mb": cap.MAX_INTERVIEW_BYTES // 2**20},
            "can_edit": ctx.user.role in PLANNERS}


@router.put("/questions/{question_id}")
def rewrite(question_id: str, body: TextBody, request: Request) -> dict:
    ctx = member(request, PLANNERS, unsafe=True)
    with services(request).backend.tenant(ctx.tenant_id) as tx:
        q: CaptureQuestion = _run(cap.rewrite_question, tx, question_id, body.text)
    return {"id": q.id, "text": q.text}


async def _record(request: Request, question_id: str | None, consent: str | None, language: str | None) -> dict:
    ctx = member(request, CAPTURERS, unsafe=True)
    if consent != "1":
        raise bad_request("tick the consent box before recording; nothing was stored")
    lang = _language(language)
    data = await _body(request, cap.MAX_ANSWER_BYTES if question_id else cap.MAX_INTERVIEW_BYTES)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        saved = _run(cap.save_recording, tx, svc.media, tenant_id=ctx.tenant_id, actor=ctx.human(), data=data,
                     mime=request.headers.get("content-type", ""), consent_ticked=True, clock=svc.clock,
                     question_id=question_id, language=lang)
    return {"answer_id": saved.answer.id, "state": saved.answer.transcript_state.value, "reused": saved.reused}


@router.post("/questions/{question_id}/recording")
async def record_answer(question_id: str, request: Request, consent: str | None = None,
                        language: str | None = None) -> dict:
    return await _record(request, question_id, consent, language)


@router.post("/interview/recording")
async def record_interview(request: Request, consent: str | None = None, language: str | None = None) -> dict:
    return await _record(request, None, consent, language)


@router.post("/questions/{question_id}/typed")
def typed(question_id: str, body: TextBody, request: Request) -> dict:
    ctx = member(request, CAPTURERS, unsafe=True)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        saved, claims = _run(cap.save_typed, tx, tenant_id=ctx.tenant_id, actor=ctx.human(), question_id=question_id,
                             text=body.text, clock=svc.clock, language=body.language)
    return {"answer_id": saved.answer.id, "state": "done", "candidates": [_claim_view(c) for c in claims]}


def _registry(request: Request) -> ConnectionRegistry:
    svc = services(request)
    conns, _secrets, health = svc.backend.connection_stores()
    return ConnectionRegistry(clock=svc.clock, providers=svc.providers, connections=conns, vault=svc.vault,
                              health=health, mock_tenants=svc.mock_tenants, environ=svc.environ)


@router.post("/answers/{answer_id}/transcribe")
def transcribe(answer_id: str, request: Request) -> dict:
    ctx = member(request, CAPTURERS, unsafe=True)
    svc = services(request)
    registry = _registry(request)
    registry.ensure_manual(ctx.tenant_id, Capability.TRANSCRIPTION)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        answer, claims = _run(cap.transcribe, tx, registry, tenant_id=ctx.tenant_id, answer_id=answer_id,
                              clock=svc.clock)
    return {"answer_id": answer.id, "state": answer.transcript_state.value,
            "candidates": [_claim_view(c) for c in claims]}


@router.post("/answers/{answer_id}/transcript")
def transcript(answer_id: str, body: TextBody, request: Request) -> dict:
    ctx = member(request, CAPTURERS, unsafe=True)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        answer, claims = _run(cap.type_transcript, tx, answer_id=answer_id, text=body.text, clock=svc.clock)
    return {"answer_id": answer.id, "state": answer.transcript_state.value,
            "candidates": [_claim_view(c) for c in claims]}


@router.post("/shots")
def add_shot(body: ShotBody, request: Request) -> dict:
    ctx = member(request, PLANNERS, unsafe=True)
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        shot: ShotRequest = _run(cap.add_shot, tx, ctx.tenant_id, body.week or svc.clock.now().date(), body.text,
                                 body.people_in_shot, svc.clock)
    return {"id": shot.id, "state": shot.state.value}


@router.post("/shots/{shot_id}/file")
async def shot_file(shot_id: str, request: Request, consent: str | None = None) -> dict:
    ctx = member(request, CAPTURERS, unsafe=True)
    data = await _body(request, max(cap.MAX_IMAGE_BYTES, cap.MAX_VIDEO_BYTES))
    svc = services(request)
    with svc.backend.tenant(ctx.tenant_id) as tx:
        shot = _run(cap.fulfil_shot, tx, svc.media, tenant_id=ctx.tenant_id, actor=ctx.human(), shot_id=shot_id,
                    data=data, mime=request.headers.get("content-type", ""), people_consent_ticked=consent == "1",
                    clock=svc.clock)
    return {"id": shot.id, "state": shot.state.value, "media_id": shot.media_id}


@router.post("/shots/{shot_id}/drop")
def drop_shot(shot_id: str, request: Request) -> dict:
    ctx = member(request, PLANNERS, unsafe=True)
    with services(request).backend.tenant(ctx.tenant_id) as tx:
        shot = _run(cap.drop_shot, tx, shot_id)
    return {"id": shot.id, "state": shot.state.value}


__all__ = ["router"]
