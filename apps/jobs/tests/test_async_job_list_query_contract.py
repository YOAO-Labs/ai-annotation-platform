from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_jobs.async_job_list_query_contract import (
    ASYNC_JOB_LIST_FUNCTION_ROW_TYPES,
    ASYNC_JOB_LIST_PAGE_FUNCTION,
    ASYNC_JOB_LIST_QUERY_PROTOCOL,
    ASYNC_JOB_LIST_ROW_COLUMNS,
    ASYNC_JOB_LIST_ROW_TYPES,
    ASYNC_JOB_LIST_TOTAL_FUNCTION,
    AsyncJobListQuery,
    AsyncJobListRow,
    async_job_list_query_schema,
    async_job_list_query_sql,
)


def test_async_job_list_schema_sql_and_migration_resource_are_identical():
    root = Path(__file__).resolve().parents[3]
    schema_path = root / "apps/jobs/src/aap_jobs/async_job_list_query.schema.json"
    sql_path = root / "apps/jobs/src/aap_jobs/sql/0326_jobs_list_query.sql"
    migration_schema_path = (
        root / "apps/api/alembic/sql/0326_jobs_list_query.schema.json"
    )
    migration_sql_path = root / "apps/api/alembic/sql/0326_jobs_list_query.sql"
    migration_path = root / "apps/api/alembic/versions/0326_jobs_list_query.py"

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    sql = sql_path.read_text(encoding="utf-8")

    assert schema == async_job_list_query_schema()
    assert schema["$id"] == ASYNC_JOB_LIST_QUERY_PROTOCOL
    assert schema["additionalProperties"] is False
    assert schema["x-row-columns"] == list(ASYNC_JOB_LIST_ROW_COLUMNS)
    assert schema["x-row-types"] == ASYNC_JOB_LIST_ROW_TYPES
    row_schema = schema["$defs"]["AsyncJobListRow"]
    assert row_schema["additionalProperties"] is False
    assert row_schema["required"] == list(ASYNC_JOB_LIST_ROW_COLUMNS)
    assert set(row_schema["properties"]) == set(ASYNC_JOB_LIST_ROW_COLUMNS)
    assert schema["x-result"]["row_schema"] == "#/$defs/AsyncJobListRow"
    assert schema["x-identity-snapshot"]["preflight_reads_per_list_request"] == 1
    assert (
        "aap_identity_v1.read_actor_facts(uuid)"
        in schema["x-identity-snapshot"]["same_statement_membership_facts"]
    )
    assert "aap_identity_v1.read_actor_facts(uuid)" in schema["x-owner-capabilities"]
    assert (
        schema["x-owner-capabilities"]["aap_identity_v1.read_actor_facts(uuid)"][
            "grant_to"
        ]
        == "jobs_owner"
    )
    assert (
        schema["x-postgresql-functions"][ASYNC_JOB_LIST_PAGE_FUNCTION]["arguments"]
        == "uuid, text, text[], text[], uuid, text, integer, integer"
    )
    assert schema["x-postgresql-functions"][ASYNC_JOB_LIST_TOTAL_FUNCTION] == {
        "arguments": "uuid, text, text[], text[], uuid, text",
        "returns": "bigint",
    }
    for name in ASYNC_JOB_LIST_ROW_COLUMNS:
        assert (
            name
            in schema["x-postgresql-functions"][ASYNC_JOB_LIST_PAGE_FUNCTION]["returns"]
        )
    assert ASYNC_JOB_LIST_FUNCTION_ROW_TYPES["kind"] == "character varying"
    assert ASYNC_JOB_LIST_FUNCTION_ROW_TYPES["status"] == "character varying"
    assert "CREATE FUNCTION aap_jobs_v1._async_job_list_rows(" in sql
    assert "RETURNS SETOF public.async_jobs" in sql
    assert "CREATE FUNCTION aap_jobs_v1.async_job_list_page(" in sql
    assert "CREATE FUNCTION aap_jobs_v1.async_job_list_total(" in sql
    assert "SELECT job.*" in sql
    assert "aap_identity_v1.read_actor_facts(p_actor_id)" in sql
    assert "actor.is_active IS TRUE" in sql
    assert "actor.platform_role IN" in sql
    assert "actor.platform_role <> 'viewer'" in sql
    assert "FROM public.users" not in sql
    assert "ORDER BY job.created_at DESC, job.id DESC" in sql
    assert "COALESCE(job.project_id, job.retained_project_id)" in sql
    assert "job.kind IN ('audit_archive', 'dataset_import')" in sql
    assert "job.kind <> 'export'" in sql
    for key in (
        "prompt",
        "batch_display_id",
        "task_display_id",
        "model_key",
        "ml_backend_name",
        "error_type",
    ):
        assert f"job.payload ->> '{key}' ILIKE p_search_pattern" in sql
    assert "SET search_path = pg_catalog, pg_temp" in sql
    assert "SECURITY DEFINER" in sql
    assert "STABLE" in sql
    assert "FROM PUBLIC" in sql
    assert async_job_list_query_sql() == sql
    assert schema_path.read_bytes() == migration_schema_path.read_bytes()
    assert sql_path.read_bytes() == migration_sql_path.read_bytes()

    migration = migration_path.read_text(encoding="utf-8")
    assert 'revision = "0326"' in migration
    assert 'down_revision = "0325"' in migration


def test_async_job_list_query_keeps_empty_and_open_filter_semantics():
    query = AsyncJobListQuery.model_validate_json(
        json.dumps(
            {
                "actor_id": "3af0d5e6-fb54-45e6-ae26-c38dbf54f0cc",
                "platform_role": "employee",
                "statuses": [],
                "kinds": ["future_job_kind"],
                "project_id": None,
                "search_pattern": None,
                "limit": 50,
                "offset": 0,
            }
        )
    )
    assert query.statuses == []
    assert query.kinds == ["future_job_kind"]
    assert query.project_id is None

    with pytest.raises(ValidationError):
        AsyncJobListQuery.model_validate(
            {
                "actor_id": "3af0d5e6-fb54-45e6-ae26-c38dbf54f0cc",
                "platform_role": "employee",
                "statuses": ["unknown_status"],
                "kinds": [],
                "project_id": None,
                "search_pattern": None,
                "limit": 50,
                "offset": 0,
            }
        )


def test_async_job_list_row_contract_is_closed_but_keeps_native_json():
    row = AsyncJobListRow.model_validate_json(
        json.dumps(
            {
                "id": "3af0d5e6-fb54-45e6-ae26-c38dbf54f0cc",
                "kind": "future_job_kind",
                "project_id": None,
                "retained_project_id": None,
                "user_id": None,
                "status": "pending",
                "progress_pct": 0,
                "payload": ["legacy", None],
                "result": {"ok": True},
                "error_message": None,
                "celery_task_id": None,
                "started_at": None,
                "completed_at": None,
                "created_at": "2026-10-24T00:00:00Z",
                "updated_at": "2026-10-24T00:00:00Z",
            }
        )
    )
    assert row.kind == "future_job_kind"
    assert row.payload == ["legacy", None]

    with pytest.raises(ValidationError):
        AsyncJobListRow.model_validate({**row.model_dump(), "extra": True})
