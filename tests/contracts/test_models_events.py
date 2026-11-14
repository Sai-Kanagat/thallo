"""Row models, events and the generated schema."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from thallo.contracts.actors import AgentActor, SystemActor
from thallo.contracts.clock import FAKE_EPOCH
from thallo.contracts.ddl import render
from thallo.contracts.events import PAYLOADS, ClaimPayload, Event, EventName, LeadPayload
from thallo.contracts.models import ER_ENTITIES, TABLES, Lead, ModelMade, Tenant, TenantRow

ROOT = Path(__file__).resolve().parents[2]
T = "0190f3a0-0000-7000-8000-000000000001"
ID = "0190f3a0-0000-7000-8000-000000000002"


def test_every_er_entity_has_a_table():
    names = {t.__table__ for t in TABLES}
    assert set(ER_ENTITIES.values()) <= names
    assert len(names) == len(TABLES)


def test_every_row_is_tenant_scoped_except_the_layer_above():
    above = {"tenant", "operator", "identity"}
    for t in TABLES:
        if t.__table__ in above:
            assert "tenant_id" not in t.model_fields
        else:
            assert issubclass(t, TenantRow), t.__table__


def test_no_ip_address_and_no_raw_secret_columns():
    for t in TABLES:
        for col in t.model_fields:
            assert "ip" not in col.split("_"), f"{t.__table__}.{col}"
            assert col in ("secret_ref",) or not any(w in col for w in ("secret", "token", "password", "api_key")) \
                or col == "token_expires_at", f"{t.__table__}.{col}"


@pytest.mark.parametrize("table", ["idea", "brief", "forecast", "asset", "variant", "check_result",
                                   "rule_proposal", "message", "claim", "media", "growth_model"])
def test_model_written_rows_carry_model_and_prompt(table):
    model = next(t for t in TABLES if t.__table__ == table)
    assert issubclass(model, ModelMade)


def test_rows_refuse_naive_time_and_unknown_fields():
    with pytest.raises(ValidationError):
        Tenant(id=T, created_at=datetime(2026, 1, 1), name="x", slug="x", default_language="en",  # noqa: DTZ001
               region_code="IT", currency="EUR", operator_id=ID)
    with pytest.raises(ValidationError):
        Tenant(id=T, created_at=FAKE_EPOCH, name="x", slug="x", default_language="en", region_code="IT",
               currency="EUR", operator_id=ID, ip="1")
    with pytest.raises(ValidationError):  # currency must be ISO 4217
        Tenant(id=T, created_at=FAKE_EPOCH, name="x", slug="x", default_language="en", region_code="IT",
               currency="eur", operator_id=ID)


def test_lead_fit_is_bounded():
    with pytest.raises(ValidationError):
        Lead(id=ID, tenant_id=T, created_at=FAKE_EPOCH, person_id=ID, fit=101)


def test_committed_migration_matches_models():
    committed = (ROOT / "db" / "migrations" / "0001_core.sql").read_text()
    assert committed == render(), "run: python -m thallo.contracts.ddl > db/migrations/0001_core.sql"


def test_every_event_has_a_payload_type():
    assert set(PAYLOADS) == set(EventName)


def test_event_validates_payload_against_its_name():
    ok = Event.build(id=ID, tenant_id=T, occurred_at=FAKE_EPOCH, actor=SystemActor(id="w"),
                     name=EventName.CLAIM_VERIFIED, payload=ClaimPayload(claim_id="c", code="C-0001"))
    assert Event.model_validate_json(ok.model_dump_json()) == ok
    with pytest.raises(TypeError):
        Event.build(id=ID, tenant_id=T, occurred_at=FAKE_EPOCH, actor=SystemActor(id="w"),
                    name=EventName.CLAIM_VERIFIED, payload=LeadPayload(lead_id="l"))
    with pytest.raises(ValidationError):
        Event(id=ID, tenant_id=T, occurred_at=datetime.now(UTC), actor=AgentActor(id="a"),  # noqa: DTZ005
              name=EventName.LEAD_CREATED, payload={"claim_id": "c", "code": "C-1"})
