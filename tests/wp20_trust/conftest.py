"""WP-20 fixtures. Reuses WP-09's two-tenant world, so every test that takes `world` runs in
memory and on Postgres as the per-run app role."""
from __future__ import annotations

from thallo.contracts.actors import AgentActor, HumanActor
from thallo.trust import service as svc
from tests.wp09_api.conftest import pg_urls, world  # noqa: F401

LIVE = "live-1"


class FakeVerifier:
    def __init__(self, live: set[str] | None = None) -> None:
        self.live = live if live is not None else {LIVE}

    def is_live_human_session(self, actor: HumanActor) -> bool:
        return actor.session_id in self.live


VERIFIER = FakeVerifier()
AGENT = AgentActor(id="writer-agent", model_id="m")


def human(shop) -> HumanActor:  # noqa: ANN001
    return HumanActor(id=shop.founder.id, tenant_id=shop.tenant.id, session_id=LIVE)


def at_level(tx, clock, task_type, level):  # noqa: ANN001, ANN201
    """Test setup only: put a task type at a level as if it had earned it."""
    st, _ = svc.state(tx, clock, task_type)
    svc.change_level(tx, clock, st, level, "test setup")
    return tx.get(type(st), st.id)


def items(tx, clock, task_type, outcomes, prefix="i"):  # noqa: ANN001, ANN201
    out = []
    for n, o in enumerate(outcomes):
        clock.advance(seconds=1)
        out.append(svc.record_item(tx, clock, task_type=task_type, subject_kind="bundle",
                                   subject_id=f"{prefix}-{n}", outcome=o))
    return out
