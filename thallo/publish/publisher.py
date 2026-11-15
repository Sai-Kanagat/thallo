"""The publisher (WP-08): sends only approved bytes, once, and proves what landed.

It is the only caller of `gate.check_publish`. One `publish()` is three steps:

1. Decide and claim, in one tenant transaction. Refuse if a publication for the variant already
   exists in any state but "retry, due". Call `check_publish` now. Any refusal is recorded (an
   event in `event_log`, and for a hash or approval change the variant goes back to CHECKING) and
   nothing is sent. Then read every media file the payload names through the gate's repository
   (the tenant-confined blob store), hash the bytes against `payload.media[i].sha256`, and refuse
   on any difference. Pick the connection (the registry's chain and its own skip rules), and claim:
   insert the `publication` (key `variant_id:approval_id`, the database allows one per variant)
   and its `publication_send` row in state SENDING, with the connection it will use. Commit.
2. Send, outside any transaction: the provider gets `decision.payload` as a `PublishRequest` and
   the verified bytes through `publish.context` (it hashes them again as it uploads).
3. Record, in a new transaction: SENT, MANUAL_PENDING, RETRY (definitely not sent, with backoff),
   FAILED (not sent, no more tries) or NEEDS_CHECK (may have landed).

A send whose outcome is unknown is never sent again by the machine. `reconcile()` asks the same
connection, by readback or the provider's own lookup; if it finds the post, the publication is
SENT; if it finds nothing or cannot tell, it stays NEEDS_CHECK and waits on a person (`settle`).
A crash after the send leaves SENDING; past `lease` that counts as NEEDS_CHECK.

The registry's `run()` is not used for sends: it cannot say which connection it reached when the
provider raises, and a second `run()` could fall through to another connection (the manual pack,
say) while a first post may exist. So the publisher pins one connection per publication, records
it before the call, and uses only that connection for readback and reconcile.

`readback()` compares canonical text (gate CANONICAL_FORM) with the text sent; a difference is
READ_BACK_MISMATCH with a diff, recorded on the publication and as an event. Changed media is a
warning, not a mismatch (platforms recompress). No text back is never READ_BACK_OK.
"""
from __future__ import annotations

import difflib
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from thallo.connections.errors import NoConnection, ProviderUnavailable
from thallo.connections.registry import ConnectionRegistry
from thallo.contracts import connection as c
from thallo.contracts.actors import HumanActor, SessionVerifier
from thallo.contracts.clock import Clock
from thallo.contracts.enums import Capability, Channel, ConnectionMode
from thallo.contracts.events import (
    BlockedPayload,
    EventName,
    PublicationMismatchPayload,
    PublicationPayload,
)
from thallo.contracts.ids import new_id
from thallo.contracts.models import Connection, Publication
from thallo.contracts.states import VariantState
from thallo.gate import check_publish
from thallo.gate.canonical import PublishPayload, canonical_text, outgoing_text, sha256_bytes
from thallo.gate.decision import BlockReason
from thallo.gate.repository import GateRepository
from thallo.gate.signing import GateSigner
from thallo.publish.context import SendContext, StagedMedia, sha256_hex, staged
from thallo.publish.errors import NotSent
from thallo.publish.preflight import channel_problems
from thallo.publish.records import PublicationSend, SendState
from thallo.publish.store import PublishStore

LEASE = timedelta(minutes=15)  # a SENDING claim older than this was left by a process that died
RETRY_BASE = timedelta(minutes=5)  # backoff: 5, 10, 20, 40 minutes
MAX_SEND_ATTEMPTS = 5
MAX_DIFF_CHARS = 4000

# A hash or approval change found at send time sends the variant back through the check stack
# (contracts: "never straight to review"). Pauses, repository errors and the like do not.
RECHECK = frozenset({
    BlockReason.CONTENT_CHANGED, BlockReason.APPROVAL_VOIDED, BlockReason.APPROVAL_EXPIRED,
    BlockReason.APPROVAL_STALE, BlockReason.MEDIA_BYTES_MISMATCH, BlockReason.PAYLOAD_MISMATCH,
})
MEDIA_CHANGED = "media_bytes_changed"  # the publisher's own reason: the bytes to upload differ


def idempotency_key(variant_id: str, approval_id: str) -> str:
    return f"{variant_id}:{approval_id}"


class Status(StrEnum):
    SENT = "sent"
    SCHEDULED = "scheduled"
    MANUAL_PENDING = "manual_pending"
    BLOCKED = "blocked"
    ALREADY_SENT = "already_sent"
    IN_FLIGHT = "in_flight"
    NEEDS_CHECK = "needs_check"
    RETRY_LATER = "retry_later"
    FAILED = "failed"
    READ_BACK_OK = "read_back_ok"
    READ_BACK_MISMATCH = "read_back_mismatch"
    READBACK_PENDING = "readback_pending"


class WaitingOn(StrEnum):
    """Who acts next (quality bar 7: every waiting state names who it waits on)."""

    NOBODY = "nobody"
    PERSON = "person"
    PUBLISHER = "publisher"  # a timed retry, a reconcile or a readback the publisher runs
    PROVIDER = "provider"


@dataclass(frozen=True)
class Outcome:
    status: Status
    variant_id: str
    waiting_on: WaitingOn
    next_step: str
    publication_id: str | None = None
    reasons: tuple[str, ...] = ()
    detail: str = ""
    warnings: tuple[str, ...] = ()
    external_id: str | None = None
    live_url: str | None = None
    attempt: int | None = None  # the send a person is shown; `settle` must quote it back
    requested_at: datetime | None = None


@dataclass(frozen=True)
class PublishSession:
    """One tenant transaction: the gate's repository and the publisher's store, same connection."""

    gate: GateRepository
    store: PublishStore


Sessions = Callable[[str], AbstractContextManager[PublishSession]]


@dataclass(frozen=True)
class _Prepared:
    tenant_id: str
    publication_id: str
    attempts: int
    request: c.PublishRequest
    context: SendContext
    provider: c.Provider
    connection: Connection


class StagingRefused(RuntimeError):
    pass


def stage_media(gate: GateRepository, tenant_id: str, variant_id: str,
                payload: PublishPayload) -> tuple[StagedMedia, ...]:
    """Read each file the payload names, from the variant's own media rows, through the gate's
    repository (which confines reads to the tenant's blob folder), and check its bytes hash to the
    approved value. The payload decides which files; no path comes from anywhere else."""
    rows = list(gate.get_variant_media(tenant_id, variant_id))
    out: list[StagedMedia] = []
    for pm in payload.media:
        match = [m for link, m in rows if link.position == pm.position and m.sha256 == pm.sha256
                 and m.tenant_id == tenant_id]
        if not match:
            raise StagingRefused(f"media at position {pm.position}: no stored file of this variant has the "
                                 "approved hash")
        media = match[0]
        raw = gate.read_media_bytes(tenant_id, media)
        data = bytes(raw) if isinstance(raw, bytes | bytearray | memoryview) else b"".join(raw)
        if sha256_bytes(data) != pm.sha256:
            raise StagingRefused(f"media at position {pm.position}: the stored bytes no longer hash to the "
                                 "approved value")
        if media.mime.lower() != pm.mime:
            raise StagingRefused(f"media at position {pm.position}: the stored type is not the approved type")
        out.append(StagedMedia(position=pm.position, sha256=pm.sha256, mime=pm.mime, alt_text=pm.alt_text, data=data))
    return tuple(out)


def _next_step(reasons: Sequence[str]) -> str:
    rs = set(reasons)
    if {BlockReason.SYSTEM_PAUSED.value, BlockReason.BRAND_PAUSED.value} & rs:
        return "a kill switch is on; the operator or the owner turns it off, then publish again"
    if {BlockReason.CHECK_FAILED.value, BlockReason.NOT_CHECKED.value} & rs:
        return "the check stack must pass on this exact content; fix the failing check, rerun, approve again"
    if {r.value for r in RECHECK} & rs or MEDIA_CHANGED in rs:
        return "the content or its approval changed; it goes back through the checks, then to review"
    if {BlockReason.CHECKS_UNREADABLE.value, BlockReason.REPO_ERROR.value, BlockReason.MEDIA_UNREADABLE.value} & rs:
        return "the store could not be read; nothing was sent, try again shortly"
    if BlockReason.GATE_KEY_MISSING.value in rs:
        return "the gate key is not configured on this process; the operator adds it"
    return "fix what the reason names, then publish again"


class Publisher:
    def __init__(self, *, registry: ConnectionRegistry, sessions: Sessions, clock: Clock,
                 signer: GateSigner | None = None, lease: timedelta = LEASE, retry_base: timedelta = RETRY_BASE,
                 max_attempts: int = MAX_SEND_ATTEMPTS) -> None:
        """`sessions(tenant_id)` opens one tenant transaction. `signer` is the gate key; without
        it `check_publish` reads THALLO_GATE_SIGNING_KEY."""
        self.registry = registry
        self.sessions = sessions
        self.clock = clock
        self.signer = signer
        self.lease = lease
        self.retry_base = retry_base
        self.max_attempts = max_attempts

    # ================================================================== publish

    def publish(self, *, tenant_id: str, bundle_id: str, variant_id: str, approval_id: str,
                channel_account_id: str) -> Outcome:
        try:
            with self.sessions(tenant_id) as s:
                prepared = self._prepare(s, tenant_id, bundle_id, variant_id, approval_id, channel_account_id)
        except Exception as exc:  # the transaction rolled back: nothing claimed, nothing sent
            return Outcome(Status.BLOCKED, variant_id, WaitingOn.PUBLISHER, _next_step([BlockReason.REPO_ERROR.value]),
                           reasons=(BlockReason.REPO_ERROR.value,), detail=type(exc).__name__)
        if isinstance(prepared, Outcome):
            return prepared
        return self._send(prepared)

    def _prepare(self, s: PublishSession, tenant_id: str, bundle_id: str, variant_id: str, approval_id: str,
                 channel_account_id: str) -> _Prepared | Outcome:
        now = self.clock.now()
        key = idempotency_key(variant_id, approval_id)
        existing = s.store.publication_for_variant(variant_id)
        send = s.store.send(existing.id) if existing is not None else None
        if existing is not None:
            held = self._standing(existing, send)
            if held is not None:
                return held
        decision = check_publish(s.gate, tenant_id=tenant_id, bundle_id=bundle_id, variant_id=variant_id,
                                 approval_id=approval_id, clock=self.clock, signer=self.signer)
        if not decision.allowed or decision.payload is None or decision.content_hash is None:
            reasons = [b.reason.value for b in decision.blocks] or [BlockReason.UNHASHABLE.value]
            detail = "; ".join(f"{b.reason.value}: {b.detail}" for b in decision.blocks)
            return self._refuse(s, variant_id, reasons, detail,
                                recheck=any(b.reason in RECHECK for b in decision.blocks))
        payload = decision.payload
        if problems := channel_problems(payload.channel, payload.text, len(payload.media)):
            return self._refuse(s, variant_id, ["preflight"], "; ".join(problems), recheck=False)
        account = s.store.channel_account(channel_account_id)
        if account is None or account.channel.value != payload.channel:
            return self._refuse(s, variant_id, ["channel_account"],
                                "no channel account of this tenant for the variant's channel", recheck=False)
        try:
            media = stage_media(s.gate, tenant_id, variant_id, payload)
        except StagingRefused as exc:
            return self._refuse(s, variant_id, [MEDIA_CHANGED], str(exc), recheck=True)
        except Exception as exc:  # unreadable storage blocks; it never passes
            return self._refuse(s, variant_id, [BlockReason.MEDIA_UNREADABLE.value], type(exc).__name__, recheck=False)

        text_sha = sha256_hex(payload.text.encode("utf-8"))
        request = c.PublishRequest(
            tenant_id=tenant_id, idempotency_key=key, channel=Channel(payload.channel),
            channel_account_id=channel_account_id, variant_id=variant_id, approval_id=approval_id,
            content_hash=decision.content_hash, text=payload.text, media_sha256=payload.media_sha256)
        try:
            row, provider = self.pick(tenant_id, request)
        except NoConnection as exc:
            return self._refuse(s, variant_id, ["no_connection"], str(exc), recheck=False)
        ctx = SendContext(tenant_id=tenant_id, idempotency_key=key, channel=payload.channel, text=payload.text,
                          text_sha256=text_sha, requested_at=now, media=media)

        if existing is None:
            pub = Publication(id=new_id(self.clock), created_at=now, tenant_id=tenant_id, variant_id=variant_id,
                              approval_id=approval_id, connection_id=row.id, idempotency_key=key,
                              content_sha256=payload.variant_hash)
            record = PublicationSend(id=pub.id, created_at=now, tenant_id=tenant_id, state=SendState.SENDING,
                                     attempts=1, requested_at=now, text_sha256=text_sha,
                                     media_sha256=list(payload.media_sha256), updated_at=now)
            if not s.store.claim(pub, record):
                return Outcome(Status.IN_FLIGHT, variant_id, WaitingOn.PUBLISHER,
                               "another process is publishing this variant; nothing more to do", detail="claim lost")
            publication_id, attempts = pub.id, 1
        else:
            assert send is not None  # `_standing` returns an outcome when the send row is missing
            attempts = send.attempts + 1
            if not s.store.swap(existing.id, {SendState.RETRY}, if_attempts=send.attempts,
                                if_requested_at=send.requested_at,
                                state=SendState.SENDING, attempts=attempts,
                                requested_at=now, text_sha256=text_sha, media_sha256=list(payload.media_sha256),
                                next_try_at=None, detail=None):
                return Outcome(Status.IN_FLIGHT, variant_id, WaitingOn.PUBLISHER,
                               "another process is retrying this variant; nothing more to do", existing.id)
            # Nothing was sent under the old key, so the retry may carry a newer approval.
            s.store.update_publication(existing.id, approval_id=approval_id, idempotency_key=key,
                                       content_sha256=payload.variant_hash, connection_id=row.id)
            publication_id = existing.id
        if not s.store.move_variant(variant_id, VariantState.SCHEDULED):
            raise RuntimeError("the variant cannot move to scheduled")  # rolls the claim back
        return _Prepared(tenant_id, publication_id, attempts, request, ctx, provider, row)

    def pick(self, tenant_id: str, request: c.Request) -> tuple[Connection, c.Provider]:
        """The first connection of the tenant's publishing chain that can serve the request: the
        registry's own pre-checks (last seen unhealthy, `ProviderUnavailable` or over the cap from
        `estimate()`), without running anything. The manual floor always serves."""
        choice = self.registry.resolve(tenant_id, Capability.PUBLISHING)
        for cid in choice.ordered_connection_ids:
            row = self.registry.connections.get(tenant_id, cid)
            if row is None:
                continue
            health = self.registry.health
            down = health is not None and health.last_connected(tenant_id, cid) is False
            if down and row.mode is not ConnectionMode.MANUAL:
                continue
            provider = self.registry.build(row)
            try:
                c.check_cap(provider.estimate(request), request)
            except (ProviderUnavailable, c.CostCapExceeded):
                continue
            return row, provider
        raise NoConnection("no publishing connection can serve this request")

    def status(self, tenant_id: str, variant_id: str) -> Outcome:
        """Where the variant's send stands, read only. Carries the attempt number and requested
        time a person must quote back to `settle`."""
        pub, send, _ = self._load(tenant_id, variant_id)
        if pub is None:
            return Outcome(Status.READBACK_PENDING, variant_id, WaitingOn.PUBLISHER, "nothing has been sent")
        out = self._standing(pub, send) or Outcome(Status.RETRY_LATER, variant_id, WaitingOn.PUBLISHER,
                                                   "not sent; the retry is due", pub.id)
        if send is None:
            return out
        return replace(out, attempt=send.attempts, requested_at=send.requested_at)

    def _standing(self, pub: Publication, send: PublicationSend | None) -> Outcome | None:
        """What an existing publication says, or None when it is a retry that is due."""
        vid, pid = pub.variant_id, pub.id
        if send is None:
            return Outcome(Status.NEEDS_CHECK, vid, WaitingOn.PERSON,
                           "a publication exists with no send record; a person checks the channel", pid)
        now = self.clock.now()
        match send.state:
            case SendState.SENT:
                return Outcome(Status.ALREADY_SENT, vid, WaitingOn.NOBODY, "already published; read back if not done",
                               pid, external_id=pub.external_id, live_url=pub.live_url)
            case SendState.MANUAL_PENDING:
                return Outcome(Status.MANUAL_PENDING, vid, WaitingOn.PERSON,
                               "post the prepared pack exactly as it is, then confirm the live link", pid)
            case SendState.SENDING if now - send.updated_at < self.lease:
                return Outcome(Status.IN_FLIGHT, vid, WaitingOn.PUBLISHER, "a send is in progress", pid)
            case SendState.SENDING | SendState.NEEDS_CHECK:
                return Outcome(Status.NEEDS_CHECK, vid, WaitingOn.PUBLISHER,
                               "the send may have landed; reconcile by readback, never resend", pid,
                               detail=send.detail or "")
            case SendState.FAILED:
                return Outcome(Status.FAILED, vid, WaitingOn.PERSON,
                               "the send failed and will not be retried; a person decides what next", pid,
                               detail=send.detail or "")
            case SendState.RETRY if send.next_try_at is not None and now < send.next_try_at:
                return Outcome(Status.RETRY_LATER, vid, WaitingOn.PUBLISHER,
                               f"not sent; the next try is due at {send.next_try_at.isoformat()}", pid,
                               detail=send.detail or "")
        return None

    def _refuse(self, s: PublishSession, variant_id: str, reasons: list[str], detail: str, *,
                recheck: bool) -> Outcome:
        """Record a refusal in the same transaction: an event, and for a hash or approval change,
        the variant back to CHECKING. Nothing is sent."""
        s.store.emit(EventName.PUBLICATION_BLOCKED,
                     BlockedPayload(variant_id=variant_id, reason=(", ".join(reasons) + ": " + detail)[:1000]))
        if recheck:
            s.store.move_variant(variant_id, VariantState.CHECKING)
        return Outcome(Status.BLOCKED, variant_id, WaitingOn.PERSON, _next_step(reasons),
                       reasons=tuple(reasons), detail=detail)

    # ------------------------------------------------------------------ send and record

    def _send(self, p: _Prepared) -> Outcome:
        try:
            with staged(p.context):
                result = p.provider.run(p.request)
        except NotSent as exc:
            return self._record_not_sent(p, exc)
        except Exception as exc:  # anything else may have reached the service: assume it did
            return self._record_unknown(p, f"the send raised {type(exc).__name__}; it may have landed")
        return self._record_result(p, result)

    def _record_result(self, p: _Prepared, result: c.RunResult) -> Outcome:
        out = result.output
        vid = p.request.variant_id
        if not isinstance(out, c.PublishResult):
            return self._record_unknown(p, "the provider returned something other than a publish result")
        now = self.clock.now()
        if out.status == "manual_pending":
            with self.sessions(p.tenant_id) as s:
                if not self._swap_mine(s, p, state=SendState.MANUAL_PENDING, detail="manual pack prepared"):
                    return self._conflict(s, p, "a manual pack was prepared")
            return Outcome(Status.MANUAL_PENDING, vid, WaitingOn.PERSON,
                           "post the prepared pack exactly as it is, then confirm the live link", p.publication_id)
        if not (out.external_id or out.live_url):
            return self._record_unknown(p, "the provider accepted the post but returned no id or link")
        published = out.status == "published"
        with self.sessions(p.tenant_id) as s:
            if not self._swap_mine(s, p, state=SendState.SENT, detail=out.status):
                return self._conflict(s, p, f"the provider accepted it (external id {out.external_id}, "
                                            f"live link {out.live_url})", out.external_id, out.live_url)
            s.store.update_publication(p.publication_id, external_id=out.external_id, live_url=out.live_url,
                                       sent_at=now)
            if published:
                s.store.move_variant(vid, VariantState.PUBLISHED)
            s.store.emit(EventName.PUBLICATION_SENT,
                         PublicationPayload(publication_id=p.publication_id, variant_id=vid))
        return Outcome(Status.SENT if published else Status.SCHEDULED, vid, WaitingOn.PUBLISHER,
                       "read back the live text to prove what landed", p.publication_id,
                       external_id=out.external_id, live_url=out.live_url)

    @staticmethod
    def _swap_mine(s: PublishSession, p: _Prepared, **fields) -> bool:
        """Compare and set on exactly the claim this send made: still SENDING, same attempt, same
        requested time. Anything else means another writer moved the row while the call ran."""
        return s.store.swap(p.publication_id, {SendState.SENDING}, if_attempts=p.attempts,
                            if_requested_at=p.context.requested_at, **fields)

    def _conflict(self, s: PublishSession, p: _Prepared, what: str, external_id: str | None = None,
                  live_url: str | None = None) -> Outcome:
        """The row moved while this send ran (a person settled it, a reconcile ran, a lease
        expired). Nothing of this send's result is written to the publication or the variant; the
        row goes to NEEDS_CHECK naming what this send got back, for a person to resolve."""
        vid = p.request.variant_id
        detail = (f"conflict: attempt {p.attempts} finished after the send row had moved; {what}")[:1000]
        # Only a row this attempt still owns (same attempt number and requested time) and that is
        # not already SENT may change. A newer attempt's row, or a SENT one, is left as it is and
        # the conflict is recorded as an event only.
        owned = s.store.swap(p.publication_id, set(SendState) - {SendState.SENT}, if_attempts=p.attempts,
                             if_requested_at=p.context.requested_at, state=SendState.NEEDS_CHECK, detail=detail)
        if not owned:
            detail = (detail + "; the row now belongs to another attempt or is sent, so it was left unchanged")[:1000]
        s.store.emit(EventName.PUBLICATION_BLOCKED, BlockedPayload(variant_id=vid, reason=detail))
        return Outcome(Status.NEEDS_CHECK, vid, WaitingOn.PERSON,
                       "two outcomes were recorded for one send; a person checks the channel and settles it",
                       p.publication_id, reasons=("conflict",), detail=detail, external_id=external_id,
                       live_url=live_url)

    def _record_not_sent(self, p: _Prepared, exc: NotSent) -> Outcome:
        vid = p.request.variant_id
        retry = exc.retryable and p.attempts < self.max_attempts
        next_try = self.clock.now() + self.retry_base * (2 ** (p.attempts - 1)) if retry else None
        detail = f"not sent: {exc}"[:1000]
        with self.sessions(p.tenant_id) as s:
            if not self._swap_mine(s, p, state=SendState.RETRY if retry else SendState.FAILED,
                                   next_try_at=next_try, detail=detail):
                return self._conflict(s, p, detail)
            s.store.move_variant(vid, VariantState.FAILED)
            s.store.emit(EventName.PUBLICATION_BLOCKED, BlockedPayload(variant_id=vid, reason=detail))
        if retry:
            assert next_try is not None
            return Outcome(Status.RETRY_LATER, vid, WaitingOn.PUBLISHER,
                           f"nothing was sent; the same bytes are tried again after {next_try.isoformat()}",
                           p.publication_id, detail=detail)
        return Outcome(Status.FAILED, vid, WaitingOn.PERSON,
                       "nothing was sent and the publisher will not retry; a person decides what next",
                       p.publication_id, detail=detail)

    def _record_unknown(self, p: _Prepared, detail: str) -> Outcome:
        vid = p.request.variant_id
        with self.sessions(p.tenant_id) as s:
            if not self._swap_mine(s, p, state=SendState.NEEDS_CHECK, detail=detail):
                return self._conflict(s, p, detail)
            s.store.emit(EventName.PUBLICATION_BLOCKED, BlockedPayload(variant_id=vid, reason=f"needs_check: {detail}"))
        return Outcome(Status.NEEDS_CHECK, vid, WaitingOn.PUBLISHER,
                       "the send may have landed; reconcile by readback, never resend", p.publication_id, detail=detail)

    # ================================================================== reconcile, settle

    def _connection_provider(self, tenant_id: str, pub: Publication) -> c.Provider:
        if pub.connection_id is None:
            raise NoConnection("the publication has no recorded connection")
        row = self.registry.connections.get(tenant_id, pub.connection_id)
        if row is None:
            raise NoConnection("the connection that sent this publication is gone")
        return self.registry.build(row)

    def _lookup(self, tenant_id: str, pub: Publication, send: PublicationSend, text: str, channel: str
                ) -> c.RunResult:
        provider = self._connection_provider(tenant_id, pub)
        ctx = SendContext(tenant_id=tenant_id, idempotency_key=pub.idempotency_key, channel=channel, text=text,
                          text_sha256=send.text_sha256, requested_at=send.requested_at)
        req = c.ReadbackRequest(tenant_id=tenant_id, idempotency_key=pub.idempotency_key, publication_id=pub.id,
                                external_id=pub.external_id, live_url=pub.live_url)
        with staged(ctx):
            return provider.run(req)

    def _load(self, tenant_id: str, variant_id: str):
        with self.sessions(tenant_id) as s:
            pub = s.store.publication_for_variant(variant_id)
            send = s.store.send(pub.id) if pub is not None else None
            variant = s.store.variant(variant_id)
        return pub, send, variant

    def reconcile(self, tenant_id: str, variant_id: str) -> Outcome:
        """Settle a send whose outcome is unknown, by asking the connection that sent it. Never sends."""
        pub, send, variant = self._load(tenant_id, variant_id)
        if pub is None or send is None or variant is None:
            return Outcome(Status.BLOCKED, variant_id, WaitingOn.PERSON, "there is no send to reconcile",
                           reasons=("no_publication",))
        standing = self._standing(pub, send)
        if standing is None or standing.status is not Status.NEEDS_CHECK:
            return standing or Outcome(Status.RETRY_LATER, variant_id, WaitingOn.PUBLISHER,
                                       "not sent; the retry is due", pub.id)
        if send.state is SendState.SENDING:  # the lease expired: the claimant died or hangs
            with self.sessions(tenant_id) as s:
                if not s.store.swap(pub.id, {SendState.SENDING}, if_attempts=send.attempts,
                                    if_requested_at=send.requested_at, state=SendState.NEEDS_CHECK,
                                    detail="the send's lease expired before its outcome was recorded"):
                    return Outcome(Status.IN_FLIGHT, variant_id, WaitingOn.PUBLISHER, "the send row moved; look again",
                                   pub.id)
        text = outgoing_text(variant)
        if sha256_hex(text.encode("utf-8")) != send.text_sha256:
            return self._hold(tenant_id, pub, send,
                              "the variant changed after the send; a person checks the channel by hand")
        try:
            result = self._lookup(tenant_id, pub, send, text, variant.channel.value)
        except Exception as exc:
            return Outcome(Status.NEEDS_CHECK, variant_id, WaitingOn.PROVIDER,
                           "the lookup could not be answered; try the reconcile again later", pub.id,
                           detail=type(exc).__name__)
        found = result.external_ids.get("external_id")
        if result.status != "done" or not found:
            return self._hold(tenant_id, pub, send,
                              "the reconcile found no post for this key; a person checks the channel")
        live_url = result.external_ids.get("live_url")
        with self.sessions(tenant_id) as s:
            if not s.store.swap(pub.id, {SendState.NEEDS_CHECK}, if_attempts=send.attempts,
                                if_requested_at=send.requested_at, state=SendState.SENT, detail="found by reconcile"):
                return Outcome(Status.IN_FLIGHT, variant_id, WaitingOn.PUBLISHER, "settled by another process", pub.id)
            s.store.update_publication(pub.id, external_id=found, live_url=live_url, sent_at=send.requested_at)
            s.store.move_variant(variant_id, VariantState.PUBLISHED)
            s.store.emit(EventName.PUBLICATION_SENT, PublicationPayload(publication_id=pub.id, variant_id=variant_id))
        return Outcome(Status.SENT, variant_id, WaitingOn.PUBLISHER, "found the post; read back the live text",
                       pub.id, external_id=found, live_url=live_url)

    def _hold(self, tenant_id: str, pub: Publication, send: PublicationSend, why: str) -> Outcome:
        with self.sessions(tenant_id) as s:
            s.store.swap(pub.id, {SendState.NEEDS_CHECK}, if_attempts=send.attempts, if_requested_at=send.requested_at,
                         state=SendState.NEEDS_CHECK, detail=why)
        return Outcome(Status.NEEDS_CHECK, pub.variant_id, WaitingOn.PERSON,
                       why + "; then record what is live (settle), the publisher never resends on its own", pub.id)

    def settle(self, tenant_id: str, variant_id: str, *, actor: HumanActor, verifier: SessionVerifier,
               attempt: int, requested_at: datetime, posted: bool, live_url: str | None = None,
               external_id: str | None = None) -> Outcome:
        """A person says what is live, for a manual pack or a send that needs a check. `attempt`
        and `requested_at` are the send the person was shown: if the row has moved since (another
        attempt, a reconcile), nothing is written. Only MANUAL_PENDING and NEEDS_CHECK can be
        settled; a send still in its lease cannot, and an expired one is first made NEEDS_CHECK
        by `reconcile`. `posted` with a link marks it SENT; `posted=False` says nothing went out,
        so the same bytes may be tried again (RETRY, due now)."""
        if actor.tenant_id != tenant_id or not verifier.is_live_human_session(actor):
            return Outcome(Status.BLOCKED, variant_id, WaitingOn.PERSON, "only the signed-in person can settle a send",
                           reasons=("not_a_person",))
        if posted and not (live_url or external_id):
            return Outcome(Status.BLOCKED, variant_id, WaitingOn.PERSON, "paste the live link to confirm",
                           reasons=("no_live_link",))
        with self.sessions(tenant_id) as s:
            pub = s.store.publication_for_variant(variant_id)
            if pub is None:
                return Outcome(Status.BLOCKED, variant_id, WaitingOn.PERSON, "there is no send to settle",
                               reasons=("no_publication",))
            open_states = {SendState.MANUAL_PENDING, SendState.NEEDS_CHECK}
            guard = {"if_attempts": attempt, "if_requested_at": requested_at}
            if posted:
                if not s.store.swap(pub.id, open_states, **guard, state=SendState.SENT,
                                    detail=f"confirmed by {actor.id}"):
                    return Outcome(Status.BLOCKED, variant_id, WaitingOn.NOBODY,
                                   "this send is not waiting on a person, or it changed since it was shown",
                                   pub.id, reasons=("not_open",))
                s.store.update_publication(pub.id, live_url=live_url, external_id=external_id, sent_at=self.clock.now())
                s.store.move_variant(variant_id, VariantState.PUBLISHED)
                s.store.emit(EventName.PUBLICATION_SENT,
                             PublicationPayload(publication_id=pub.id, variant_id=variant_id))
                return Outcome(Status.SENT, variant_id, WaitingOn.PUBLISHER, "read back the live text", pub.id,
                               live_url=live_url, external_id=external_id)
            if not s.store.swap(pub.id, open_states, **guard, state=SendState.RETRY, next_try_at=self.clock.now(),
                                detail=f"{actor.id} confirmed nothing went out"):
                return Outcome(Status.BLOCKED, variant_id, WaitingOn.NOBODY,
                               "this send is not waiting on a person, or it changed since it was shown",
                               pub.id, reasons=("not_open",))
            s.store.move_variant(variant_id, VariantState.FAILED)
        return Outcome(Status.RETRY_LATER, variant_id, WaitingOn.PUBLISHER,
                       "nothing went out; publish again to send the approved bytes", pub.id)

    # ================================================================== readback

    def readback(self, tenant_id: str, variant_id: str) -> Outcome:
        pub, send, variant = self._load(tenant_id, variant_id)
        if pub is None or send is None or variant is None or send.state is not SendState.SENT:
            return Outcome(Status.READBACK_PENDING, variant_id, WaitingOn.PUBLISHER, "nothing sent to read back yet",
                           pub.id if pub else None)
        text = outgoing_text(variant)
        if sha256_hex(text.encode("utf-8")) != send.text_sha256:
            return Outcome(Status.READBACK_PENDING, variant_id, WaitingOn.PERSON,
                           "the variant changed after the send, so the sent text is not known here; check by hand",
                           pub.id)
        try:
            result = self._lookup(tenant_id, pub, send, text, variant.channel.value)
        except Exception as exc:
            return Outcome(Status.READBACK_PENDING, variant_id, WaitingOn.PROVIDER, "the readback failed; try later",
                           pub.id, detail=type(exc).__name__)
        out = result.output
        assert isinstance(out, c.ReadbackResult)
        if result.status != "done":
            return Outcome(Status.READBACK_PENDING, variant_id, WaitingOn.PERSON,
                           "open the live post, paste its text and say whether the media changed", pub.id)
        return self._judge(tenant_id, pub, text, out.live_text, out.media_changed,
                           result.external_ids.get("live_url"))

    def record_readback(self, tenant_id: str, variant_id: str, *, actor: HumanActor, verifier: SessionVerifier,
                        live_text: str, media_changed: bool | None) -> Outcome:
        """The manual path: a person pastes what is live."""
        if actor.tenant_id != tenant_id or not verifier.is_live_human_session(actor):
            return Outcome(Status.BLOCKED, variant_id, WaitingOn.PERSON,
                           "only the signed-in person can record a readback",
                           reasons=("not_a_person",))
        pub, send, variant = self._load(tenant_id, variant_id)
        if pub is None or send is None or variant is None or send.state is not SendState.SENT:
            return Outcome(Status.READBACK_PENDING, variant_id, WaitingOn.PERSON, "confirm the post first",
                           pub.id if pub else None)
        text = outgoing_text(variant)
        if sha256_hex(text.encode("utf-8")) != send.text_sha256:
            return Outcome(Status.READBACK_PENDING, variant_id, WaitingOn.PERSON,
                           "the variant changed after the send; compare by hand", pub.id)
        return self._judge(tenant_id, pub, text, live_text, media_changed, None)

    def _judge(self, tenant_id: str, pub: Publication, sent: str, live: str | None, media_changed: bool | None,
               live_url: str | None) -> Outcome:
        vid = pub.variant_id
        if live is None:
            return Outcome(Status.READBACK_PENDING, vid, WaitingOn.PROVIDER,
                           "no live text came back; read back again later",
                           pub.id)
        ok = canonical_text(live) == canonical_text(sent)
        diff = None if ok else _diff(canonical_text(sent), canonical_text(live))
        warnings = ("the live media differ from the approved bytes (platforms recompress images); the text was "
                    "compared and the media change is reported, not treated as a mismatch",) if media_changed else ()
        note = diff if diff is not None else ("warning: " + warnings[0] if warnings else None)
        with self.sessions(tenant_id) as s:
            fields: dict = {"readback_at": self.clock.now(), "readback_ok": ok, "readback_diff": note}
            if live_url and not pub.live_url:
                fields["live_url"] = live_url
            s.store.update_publication(pub.id, **fields)
            s.store.move_variant(vid, VariantState.PUBLISHED)  # a scheduled post that is now live
            s.store.move_variant(vid, VariantState.READ_BACK_OK if ok else VariantState.READ_BACK_MISMATCH)
            if ok:
                s.store.emit(EventName.PUBLICATION_READ_BACK, PublicationPayload(publication_id=pub.id, variant_id=vid))
            else:
                assert diff is not None
                s.store.emit(EventName.PUBLICATION_MISMATCH,
                             PublicationMismatchPayload(publication_id=pub.id, variant_id=vid, diff=diff))
        if ok:
            return Outcome(Status.READ_BACK_OK, vid, WaitingOn.NOBODY, "done: the live text is the approved text",
                           pub.id, warnings=warnings, live_url=live_url or pub.live_url)
        return Outcome(Status.READ_BACK_MISMATCH, vid, WaitingOn.PERSON,
                       "the platform changed the text; a person reviews the diff and decides whether to edit or delete "
                       "the live post", pub.id, detail=diff or "", warnings=warnings)


def _diff(sent: str, live: str) -> str:
    lines = difflib.unified_diff(sent.splitlines(), live.splitlines(), "approved", "live", lineterm="")
    return "\n".join(lines)[:MAX_DIFF_CHARS] or "the texts differ"


__all__ = ["LEASE", "MAX_SEND_ATTEMPTS", "RETRY_BASE", "Outcome", "PublishSession", "Publisher", "Sessions",
           "StagingRefused", "Status", "WaitingOn", "idempotency_key", "stage_media"]
