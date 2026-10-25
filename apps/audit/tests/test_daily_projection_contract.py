from __future__ import annotations

from datetime import date, datetime
from importlib.resources import files
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_audit import (
    AUDIT_DAILY_FACTS_COLUMNS,
    AUDIT_DAILY_FACTS_RELATION,
    AUDIT_DAILY_FACTS_ROW_KEY,
    AUDIT_DAILY_REFRESH_FUNCTION,
    DAILY_PROJECTION_PROTOCOL,
    AuditDailyFact,
    audit_daily_projection_schema,
)


_ROOT = Path(__file__).resolve().parents[3]


def _fact(**overrides) -> dict:
    return {
        "day": "2026-10-24",
        "action": "task.submit",
        "target_type": "task",
        "actor_role": "reviewer",
        "status_family": 2,
        "event_count": "9223372036854775807",
        **overrides,
    }


def test_daily_projection_contract_matches_packaged_and_migration_resources():
    schema = audit_daily_projection_schema()
    assert schema["$id"] == DAILY_PROJECTION_PROTOCOL
    relation = schema["x-postgresql-relations"][AUDIT_DAILY_FACTS_RELATION]
    assert relation["columns"] == list(AUDIT_DAILY_FACTS_COLUMNS)
    assert relation["column_types"] == {
        "day": "date",
        "action": "character varying(64)",
        "target_type": "character varying",
        "actor_role": "character varying",
        "status_family": "smallint",
        "event_count": "bigint",
    }
    assert relation["row_key"] == list(AUDIT_DAILY_FACTS_ROW_KEY)
    assert relation["read_only"] is True
    assert relation["security_barrier"] is True
    materialization = (
        _ROOT / "apps/api/alembic/versions/0155_audit_bi_daily_mv.py"
    ).read_text()
    assert "(day, action, target_type, actor_role, status_family)" in materialization
    refresh = schema["x-postgresql-functions"][f"{AUDIT_DAILY_REFRESH_FUNCTION}()"]
    assert refresh == {
        "argument_types": [],
        "returns": "void",
        "owner": "audit",
        "required_owner_database_privileges": ["TEMPORARY"],
        "security_definer": True,
        "fixed_target": "public.mv_audit_bi_daily",
        "concurrent": True,
        "caller_commit_required": True,
    }

    package_schema = json.loads(
        files("aap_audit").joinpath("daily_projection.schema.json").read_text()
    )
    migration_schema = json.loads(
        (
            _ROOT / "apps/api/alembic/sql/0322_audit_daily_projection.schema.json"
        ).read_text()
    )
    assert package_schema == schema
    assert migration_schema == schema
    assert files("aap_audit.sql").joinpath("0322_audit_daily_projection.sql").is_file()
    sql = (
        _ROOT / "apps/audit/src/aap_audit/sql/0322_audit_daily_projection.sql"
    ).read_text()
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, pg_temp" in sql
    assert "REFRESH MATERIALIZED VIEW CONCURRENTLY public.mv_audit_bi_daily" in sql
    assert (
        "REVOKE ALL ON FUNCTION aap_audit_v1.refresh_audit_bi_mv() FROM PUBLIC" in sql
    )


def test_daily_fact_contract_keeps_date_and_bigint_lossless():
    row = AuditDailyFact.model_validate(
        _fact(day=date(2026, 10, 24), event_count=9223372036854775807)
    )
    assert row.day == "2026-10-24"
    assert row.event_count == "9223372036854775807"
    assert row.status_family == 2


def test_daily_fact_contract_rejects_noncanonical_or_out_of_range_values():
    for invalid_count in (-1, "-0", "01", "9223372036854775808", True):
        with pytest.raises(ValidationError):
            AuditDailyFact.model_validate(_fact(event_count=invalid_count))

    for invalid_day in (
        datetime(2026, 10, 24),
        "2026-2-4",
        "2026-02-30",
    ):
        with pytest.raises(ValidationError):
            AuditDailyFact.model_validate(_fact(day=invalid_day))
