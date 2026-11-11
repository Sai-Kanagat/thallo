"""Who did something.

Every write and audit entry names an Actor. Human-only actions (BUILD_PLAN 2.8) go through
`require_human`, which needs a SessionVerifier: the object that proves this HumanActor came
from a live, interactive, authenticated session for the right tenant. A HumanActor value on
its own proves nothing, since any code can construct one. WP-24 (API) implements the
verifier; tests use a fake one.

Events and anything sent outside carry an ActorRef (kind and id only), never a session.
"""
from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from cortex_operator.contracts.enums import ActorKind, HumanOnlyAction


class HumanOnlyViolation(PermissionError):
    pass


class _ActorBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=1)


class HumanActor(_ActorBase):
    kind: Literal[ActorKind.HUMAN] = ActorKind.HUMAN
    tenant_id: str
    session_id: str = Field(min_length=1, description="Interactive session; never leaves the core")


class SystemActor(_ActorBase):
    kind: Literal[ActorKind.SYSTEM] = ActorKind.SYSTEM


class AgentActor(_ActorBase):
    kind: Literal[ActorKind.AGENT] = ActorKind.AGENT
    model_id: str | None = None


Actor = HumanActor | SystemActor | AgentActor


class ActorRef(BaseModel):
    """What an event, audit entry or outbound payload says about who acted. No session."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: ActorKind
    id: str = Field(min_length=1)

    @classmethod
    def of(cls, actor: Actor | ActorRef) -> ActorRef:
        return actor if isinstance(actor, ActorRef) else cls(kind=actor.kind, id=actor.id)


@runtime_checkable
class SessionVerifier(Protocol):
    def is_live_human_session(self, actor: HumanActor) -> bool:
        """True only if session_id is a live, interactive, authenticated session of user actor.id
        in tenant actor.tenant_id. API tokens and agent credentials never qualify."""
        ...


def require_human(actor: Actor, action: HumanOnlyAction, *, verifier: SessionVerifier,
                  tenant_id: str) -> HumanActor:
    """The runtime gate for every human-only action. Refuses machines, other tenants and
    unverified sessions."""
    if not isinstance(actor, HumanActor) or actor.kind is not ActorKind.HUMAN:
        raise HumanOnlyViolation(f"{action.value} is human-only; refused for {actor.kind.value} {actor.id}")
    if actor.tenant_id != tenant_id:
        raise HumanOnlyViolation(f"{action.value}: actor belongs to another tenant")
    if not verifier.is_live_human_session(actor):
        raise HumanOnlyViolation(f"{action.value}: session is not a verified live human session")
    return actor
