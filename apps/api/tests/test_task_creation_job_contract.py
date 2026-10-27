from __future__ import annotations

import json
import re
from pathlib import Path


_SQL_PATH = (
    Path(__file__).parents[1] / "alembic" / "sql" / "0193_task_creation_jobs.sql"
)
_SCHEMA_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "sql"
    / "0193_task_creation_jobs.schema.json"
)
_MIGRATION_PATH = (
    Path(__file__).parents[1] / "alembic" / "versions" / "0193_task_creation_jobs.py"
)
_SEPARATOR = "-- aap-statement-break"


def _function(sql: str, name: str) -> str:
    for statement in sql.split(_SEPARATOR):
        if re.search(rf"CREATE FUNCTION aap_jobs_v1\.{name}\s*\(", statement):
            return statement
    raise AssertionError(f"missing aap_jobs_v1.{name}")


def test_jobs_migration_and_frozen_artifact_are_down_revision_0192():
    migration = _MIGRATION_PATH.read_text(encoding="utf-8")

    assert '_ARTIFACT_NAME = "0193_task_creation_jobs.sql"' in migration

    sql = _SQL_PATH.read_text(encoding="utf-8")
    statements = [item.strip() for item in sql.split(_SEPARATOR) if item.strip()]
    functions = [item for item in statements if item.startswith("CREATE FUNCTION")]
    assert len(functions) == 10
    assert all(item.endswith("$function$;") for item in functions)
    assert all(
        statement.count(";") == 1
        for statement in statements
        if not statement.startswith("CREATE FUNCTION")
    )

    for statement in functions:
        match = re.match(r"CREATE FUNCTION\s+(aap_jobs_v1\.\w+)", statement)
        assert match is not None
        assert f"REVOKE ALL ON FUNCTION {match.group(1)}(" in sql


def test_frozen_schema_rejects_extra_request_and_payload_fields():
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(schema["x-postgresql-functions"]) == {
        "aap_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid)",
        "aap_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid)",
        "aap_jobs_v1.claim_task_creation_job(uuid, text)",
        "aap_jobs_v1.lock_task_creation_job(uuid, uuid, uuid, uuid, uuid, uuid)",
        "aap_jobs_v1.update_task_creation_progress(uuid, uuid, bigint, bigint, bigint)",
        "aap_jobs_v1.finish_task_creation_job(uuid, uuid)",
        "aap_jobs_v1.fail_task_creation_job(uuid, uuid, text)",
        "aap_jobs_v1.cancel_task_creation_job(uuid, uuid)",
    }

    for name in (
        "createRequest",
        "discoveryRequest",
        "claimRequest",
        "lockRequest",
        "progressRequest",
        "finishRequest",
        "failRequest",
        "cancelRequest",
        "createTasksPayload",
    ):
        assert schema["$defs"][name]["additionalProperties"] is False
    assert schema["$defs"]["createTasksPayload"]["properties"]["schema"]["const"] == (
        "aap-create-tasks/v1"
    )
    assert schema["$defs"]["createTasksPayload"]["properties"]["mode"]["const"] == (
        "link"
    )
    assert schema["$defs"]["createTasksPayload"]["allOf"]
    assert schema["$defs"]["progressResult"] == {"type": "boolean"}


def test_create_and_cancel_keep_authority_before_asset_or_job_locks():
    sql = _SQL_PATH.read_text(encoding="utf-8")
    create = _function(sql, "create_task_creation_job")
    assert (
        create.index("aap_workspace_v1.lock_project_manager")
        < create.index("aap_assets_v1.lock_current_association")
        < create.index("INSERT INTO public.async_jobs")
    )

    cancel = _function(sql, "cancel_task_creation_job")
    first_job_read = cancel.index("FROM public.async_jobs AS job")
    current_authority = cancel.index("aap_workspace_v1.lock_project_manager")
    job_lock = cancel.index("FOR UPDATE")
    assert first_job_read < current_authority < job_lock
    assert "aap_assets_v1.lock_current_association" not in cancel


def test_finish_reads_labeling_before_taking_a_jobs_row_lock():
    finish = _function(
        _SQL_PATH.read_text(encoding="utf-8"), "finish_task_creation_job"
    )
    assert (
        finish.index("FROM public.async_jobs AS job")
        < finish.index("aap_labeling_v1.read_dataset_task_creation")
        < finish.index("UPDATE public.async_jobs AS job")
    )
    assert "FOR UPDATE" not in finish


def test_job_scope_validation_uses_canonical_text_and_only_allows_cancel_metadata():
    validator = _function(
        _SQL_PATH.read_text(encoding="utf-8"), "_task_creation_job_payload_valid"
    )
    assert "p_payload - ARRAY['cancel_requested', 'cancel_requested_at']" in validator
    assert "::uuid" not in validator
    assert "'operation_id', p_job_id::text" in validator

    sql = _SQL_PATH.read_text(encoding="utf-8")
    assert "v_job.user_id IS NULL OR v_job.user_id::text" in sql
    assert "v_job.payload ->> 'actor_id'" in sql


def test_progress_is_monotonic_and_failure_keeps_partial_result():
    sql = _SQL_PATH.read_text(encoding="utf-8")
    progress = _function(sql, "update_task_creation_progress")
    assert "GREATEST(job.progress_pct, v_progress)" in progress
    assert "v_progress >= v_job.progress_pct + 5" in progress
    assert "'created_tasks', p_created" in progress
    assert "'total', p_total" in progress

    fail = _function(sql, "fail_task_creation_job")
    assert "error_message = 'task_creation:' || p_code" in fail
    assert "SET status = 'failed'" in fail
    assert "result =" not in fail
    assert "IN ('completed', 'failed', 'cancelled')" in fail
    assert "p_code IN ('job_not_active', 'job_cancelled', 'creation_cancelled')" in fail
    claim = _function(sql, "claim_task_creation_job")
    assert "v_job.payload -> 'cancel_requested' = 'true'::jsonb" in claim
    assert "SET status = 'cancelled'" in claim
