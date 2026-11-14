"""Render Postgres DDL from the row models, so the schema and the types cannot drift.

    python -m thallo.contracts.ddl > db/migrations/0001_core.sql

tests/contracts/test_ddl.py fails if the committed migration differs from this output.
Row-level security policies are not here: WP-01 adds them in its own migration.
"""
from __future__ import annotations

import types
import typing
from datetime import date, datetime
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any, Union, get_args, get_origin

import annotated_types
from pydantic.fields import FieldInfo

from thallo.contracts.models import TABLES, Row

TENANT_TABLES = frozenset(t.__table__ for t in TABLES if "tenant_id" in t.model_fields)

# Header keeps the old package name on purpose: 0001_core.sql is sha-locked, so emitted bytes must not change.
HEADER = """-- 0001_core.sql: the frozen core schema (WP-00).
-- GENERATED from cortex_operator/contracts/models.py. Do not edit by hand.
-- Regenerate: python -m cortex_operator.contracts.ddl > db/migrations/0001_core.sql
-- Row-level security is added by WP-01 in a later migration.
-- Every foreign key between tenant tables is composite (tenant_id, x_id), so no row can
-- point at another tenant's row. scripts/migrate.py wraps each file in one transaction.
"""


def _unwrap(annotation: Any) -> tuple[Any, bool]:
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _is_uuid(field: FieldInfo, base: Any) -> bool:
    if base is not str:
        return False
    for meta in field.metadata:
        if getattr(meta, "pattern", "") and "-7[0-9a-f]{3}-" in meta.pattern:
            return True
    return False


def _sql_type(field: FieldInfo, base: Any) -> tuple[str, str | None]:
    """Return (type, check expression template with {col})."""
    if get_origin(base) is typing.Annotated:
        inner, *metas = get_args(base)
        if inner is str and any("-7[0-9a-f]{3}-" in (getattr(m, "pattern", "") or "") for m in metas):
            return "uuid", None
        return _sql_type(field, inner)
    if isinstance(base, type) and issubclass(base, IntEnum):
        vals = ", ".join(str(int(v)) for v in base)
        return "smallint", f"{{col}} IN ({vals})"
    if isinstance(base, type) and issubclass(base, StrEnum):
        vals = ", ".join(f"'{v.value}'" for v in base)
        return "text", f"{{col}} IN ({vals})"
    if _is_uuid(field, base):
        return "uuid", None
    if base is bool:
        return "boolean", None
    if base is int:
        return "bigint", None
    if base is float:
        return "double precision", None
    if base is Decimal:
        return "numeric(14,4)", None
    if base is str:
        return "text", None
    if base is date:
        return "date", None
    if base is datetime or getattr(base, "__name__", "") == "AwareDatetime" or (
        get_origin(base) is typing.Annotated and get_args(base)[0] is datetime
    ):
        return "timestamptz", None
    if get_origin(base) in (dict, list, tuple) or base in (dict, list, tuple):
        return "jsonb", None
    raise TypeError(f"no SQL type for {base!r}")


def _default_sql(field: FieldInfo, sql_type: str) -> str | None:
    if field.default_factory is not None:
        made = field.default_factory()  # type: ignore[call-arg]
        if isinstance(made, dict):
            return "'{}'::jsonb"
        if isinstance(made, list | tuple):
            return "'[]'::jsonb"
        return None
    d = field.default
    if d is None or d is ... or type(d).__name__ == "PydanticUndefinedType":
        return None
    if isinstance(d, bool):
        return "true" if d else "false"
    if isinstance(d, IntEnum):
        return str(int(d))
    if isinstance(d, StrEnum):
        return f"'{d.value}'"
    if isinstance(d, int | Decimal | float):
        return str(d)
    if isinstance(d, str):
        return "'" + d.replace("'", "''") + "'"
    return None


def _extra(field: FieldInfo) -> dict[str, Any]:
    extra = field.json_schema_extra
    return extra if isinstance(extra, dict) else {}


def table_ddl(model: type[Row]) -> str:
    name = model.__table__
    tenant_scoped = "tenant_id" in model.model_fields
    cols: list[str] = []
    fks: list[str] = []
    after: list[str] = []
    for col, field in model.model_fields.items():
        if "since" in _extra(field):
            continue  # added by a later migration; 0001 is frozen
        base, optional = _unwrap(field.annotation)
        sql_type, check = _sql_type(field, base)
        parts = [f"  {col} {sql_type}"]
        if col == "id":
            parts.append("PRIMARY KEY")
        elif not optional:
            parts.append("NOT NULL")
        default = _default_sql(field, sql_type)
        if default is not None:
            parts.append(f"DEFAULT {default}")
        extra = _extra(field)
        if "fk" in extra:
            target = extra["fk"]
            if col == "tenant_id" or target not in TENANT_TABLES or not tenant_scoped:
                parts.append(f"REFERENCES {target}(id)")
            else:
                # Composite key: a row can only point at a row of its own tenant.
                fks.append(f"  FOREIGN KEY (tenant_id, {col}) REFERENCES {target} (tenant_id, id)")
        if extra.get("unique"):
            parts.append("UNIQUE")
        checks = [check.format(col=col)] if check else []
        for meta in field.metadata:
            if isinstance(meta, annotated_types.Ge):
                checks.append(f"{col} >= {meta.ge}")
            if isinstance(meta, annotated_types.Le):
                checks.append(f"{col} <= {meta.le}")
        if checks:
            parts.append(f"CHECK ({' AND '.join(checks)})")
        cols.append(" ".join(parts))
        if extra.get("index") or ("fk" in extra and col != "tenant_id"):
            after.append(f"CREATE INDEX {name}_{col}_idx ON {name} ({col});")
    if tenant_scoped:
        cols.append("  UNIQUE (tenant_id, id)")
    for combo in model.__unique__:
        cols.append(f"  UNIQUE ({', '.join(combo)})")
    cols += fks
    if "tenant_id" in model.model_fields:
        after.insert(0, f"CREATE INDEX {name}_tenant_idx ON {name} (tenant_id);")
    body = ",\n".join(cols)
    return f"CREATE TABLE {name} (\n{body}\n);\n" + "".join(a + "\n" for a in after)


def render() -> str:
    out = [HEADER]
    out += [table_ddl(t) for t in TABLES]
    return "\n".join(out)


if __name__ == "__main__":
    print(render(), end="")
