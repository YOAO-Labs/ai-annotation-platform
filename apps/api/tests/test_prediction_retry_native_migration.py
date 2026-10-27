"""Static contract for the native durable failed-prediction retry authority."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql"
MIGRATION = ROOT / "apps/api/alembic/versions/0233_prediction_retries.py"


def _migration():
    spec = importlib.util.spec_from_file_location(
        "prediction_retries_migration", MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _all_statements():
    module = _migration()
    return {
        filename: module._statements(filename) for filename in module._UPGRADE_ARTIFACTS
    }


def _function_signature(statement: str) -> str | None:
    match = re.search(
        r"(?is)^CREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+(.+?)\s*\(",
        statement,
    )
    if not match:
        return None
    start = match.start(1)
    opening = statement.find("(", start)
    depth = 0
    for index in range(opening, len(statement)):
        if statement[index] == "(":
            depth += 1
        elif statement[index] == ")":
            depth -= 1
            if depth == 0:
                return " ".join(statement[start : index + 1].split())
    return None


def test_revision_is_additive_after_tracker_and_artifacts_exist():
    module = _migration()

    assert module._UPGRADE_ARTIFACTS == (
        "0233_prediction_retry_state.sql",
        "0233_prediction_retry_result_scope.sql",
        "0233_prediction_retry_finalization.sql",
    )
    assert all((SQL / filename).is_file() for filename in module._UPGRADE_ARTIFACTS)


def test_native_retry_tables_are_managed_and_do_not_own_live_rows():
    from app.db.native_schema import NATIVE_MANAGED_TABLES

    state = (SQL / "0233_prediction_retry_state.sql").read_text(encoding="utf-8")
    assert {
        "prediction_retry_attempts",
        "prediction_retry_deliveries",
    } <= NATIVE_MANAGED_TABLES
    assert "job_id uuid PRIMARY KEY" in state
    assert "WHERE state IN ('accepted','applying','failing')" in state
    assert "REFERENCES public.async_jobs" not in state
    assert "REFERENCES public.failed_predictions" not in state
    assert "REFERENCES public.tasks" not in state
    assert "REFERENCES public.projects" not in state
    assert "REFERENCES public.ml_backend" not in state


def test_six_public_operations_and_owner_only_helpers_are_installed():
    statements = _all_statements()
    all_sql = "\n".join(
        statement for group in statements.values() for statement in group
    )
    signatures = {
        signature.split("(", 1)[0].strip().lower()
        for group in statements.values()
        for statement in group
        if (signature := _function_signature(statement))
    }
    expected = {
        "aap_labeling_v1.admit_prediction_retry",
        "aap_labeling_v1.read_prediction_retry",
        "aap_labeling_v1.finish_prediction_retry_write",
        "aap_labeling_v1.finish_prediction_retry_failure",
        "aap_jobs_v1.claim_prediction_retry_deliveries",
        "aap_jobs_v1.finish_prediction_retry_delivery",
    }
    assert expected <= signatures
    assert "CREATE FUNCTION aap_jobs_v1._create_prediction_retry_job" in all_sql
    assert "CREATE FUNCTION aap_jobs_v1._finish_prediction_retry_job" in all_sql
    assert "aap_workspace_v1.lock_project_manager(v_actor,v_project)" in all_sql
    assert "aap_labeling_v1._prediction_retry_attempt_scope_valid" in all_sql
    assert "aap_labeling_v1._prediction_retry_terminal_matches" in all_sql
    assert "prediction_retry_request_digest" in all_sql
    assert "aap-prediction-retries/v1" in all_sql


def test_final_result_and_failure_are_fenced_and_atomic():
    statements = _all_statements()
    all_sql = "\n".join(
        statement for group in statements.values() for statement in group
    )
    assert "_batch_result_prediction_matches(v_prepared,v_prepared.body)" in all_sql
    assert (
        "DELETE FROM public.prediction_metas AS meta WHERE meta.failed_prediction_id"
        in all_sql
    )
    assert (
        "DELETE FROM public.failed_predictions AS failed WHERE failed.id=v_attempt.failed_prediction_id"
        in all_sql
    )
    assert "_finish_prediction_retry_job" in all_sql
    assert "state='succeeded'" in all_sql
    assert "state='failed'" in all_sql
    assert "terminal_digest" in all_sql
    assert "v_bundle.state IN ('applied','failed','abandoned')" in all_sql
    assert "v_bundle.receipt_count>0" in all_sql
    assert "retry_count=v_retry_count,last_retry_at" in all_sql
    assert "failed.extra ->> 'retry_job_id' IS DISTINCT FROM v_job_id::text" in all_sql
    assert "char_length(p_payload ->> 'reason')>200" in all_sql
    assert "'content_deleting','content_epoch_changed'" in all_sql


def test_generic_terminal_paths_cannot_bypass_retry_proof():
    statements = _all_statements()
    all_sql = "\n".join(
        statement for group in statements.values() for statement in group
    )
    assert "trg_batch_result_prediction_retry_execution_guard" in all_sql
    assert "trg_batch_result_prediction_retry_member_guard" in all_sql
    assert "trg_batch_result_prediction_retry_bundle_guard" in all_sql
    assert "trg_async_jobs_prediction_retry_terminal" in all_sql
    assert "trg_async_jobs_prediction_retry_delete_guard" in all_sql
    assert "_prediction_retry_terminal_matches(NEW.job_id,v_state,NULL)" in all_sql
    assert "_abandon_prediction_retry_from_job" in all_sql
    assert "_retire_prediction_retry_results" in all_sql


def test_labeling_uses_jobs_owned_finish_scope_and_collision_checks():
    state = (SQL / "0233_prediction_retry_state.sql").read_text(encoding="utf-8")
    result_scope = (SQL / "0233_prediction_retry_result_scope.sql").read_text(
        encoding="utf-8"
    )
    finalization = (SQL / "0233_prediction_retry_finalization.sql").read_text(
        encoding="utf-8"
    )
    admit = state.split("CREATE FUNCTION aap_labeling_v1.admit_prediction_retry", 1)[
        1
    ].split("REVOKE ALL ON FUNCTION aap_labeling_v1.admit_prediction_retry", 1)[0]
    write = finalization.split(
        "CREATE FUNCTION aap_labeling_v1.finish_prediction_retry_write", 1
    )[1].split(
        "REVOKE ALL ON FUNCTION aap_labeling_v1.finish_prediction_retry_write", 1
    )[0]
    failure = finalization.split(
        "CREATE FUNCTION aap_labeling_v1.finish_prediction_retry_failure", 1
    )[1].split(
        "REVOKE ALL ON FUNCTION aap_labeling_v1.finish_prediction_retry_failure", 1
    )[0]
    assert "FROM public.async_jobs" not in admit
    assert "FROM public.batch_result_executions" not in admit
    assert "task.id, task.project_id, task.version, task.status" in admit
    assert "lock_prediction_retry_finish_scope(jsonb)" in result_scope
    assert "aap_jobs_v1.lock_prediction_retry_finish_scope" in write
    assert "aap_jobs_v1.lock_prediction_retry_finish_scope" in failure
    assert "FROM public.batch_result_members" not in write
    assert "FROM public.batch_result_members" not in failure


def test_unbound_legacy_markers_are_reclaimed_without_retry_accounting():
    state = (SQL / "0233_prediction_retry_state.sql").read_text(encoding="utf-8")
    assert "UPDATE public.failed_predictions AS failed" in state
    assert "pg_catalog.jsonb_typeof(failed.extra)='object'" in state
    assert "failed.extra -> 'retry_pending'='true'::jsonb" in state
    assert "NOT (failed.extra ? 'retry_job_id')" in state
    reclaim = state.split("-- Reclaim only the legacy early-ack marker", 1)[1].split(
        "CREATE FUNCTION aap_labeling_v1._prediction_retry_reply", 1
    )[0]
    assert "retry_count=" not in reclaim
    assert "last_retry_at=" not in reclaim


def test_permanent_failure_can_retire_a_captured_uncommitted_result():
    finalization = (SQL / "0233_prediction_retry_finalization.sql").read_text(
        encoding="utf-8"
    )
    failure = finalization.split(
        "CREATE FUNCTION aap_labeling_v1.finish_prediction_retry_failure", 1
    )[1]
    assert "v_bundle.raw_complete OR v_bundle.prepared_complete" not in failure
    assert "v_bundle.state IN ('applied','failed','abandoned')" in failure
    assert "v_bundle.receipt_count>0" in failure
    assert "p_payload -> 'abandon'" in failure
    assert "'status','cancelled'" in failure
    assert "IF v_gpu IS NOT NULL AND v_gpu<>'null'::jsonb THEN" in failure
    assert "'gpu_arbiter_error',v_gpu" in failure


def test_admission_preserves_only_bounded_legacy_output_mode_string():
    state = (SQL / "0233_prediction_retry_state.sql").read_text(encoding="utf-8")
    admit = state.split("CREATE FUNCTION aap_labeling_v1.admit_prediction_retry", 1)[
        1
    ].split("REVOKE ALL ON FUNCTION aap_labeling_v1.admit_prediction_retry", 1)[0]
    assert "v_source_extra #- '{request_context,output}'" in admit
    assert (
        "pg_catalog.jsonb_typeof(v_source_extra #> '{request_context,output}') IS DISTINCT FROM 'string'"
        in admit
    )
    assert (
        "pg_catalog.octet_length(v_source_extra #>> '{request_context,output}')>8192"
        in admit
    )


def test_native_request_defaults_match_closed_schema_defaults():
    state = (SQL / "0233_prediction_retry_state.sql").read_text(encoding="utf-8")
    finalization = (SQL / "0233_prediction_retry_finalization.sql").read_text(
        encoding="utf-8"
    )
    admit = state.split("CREATE FUNCTION aap_labeling_v1.admit_prediction_retry", 1)[
        1
    ].split("REVOKE ALL ON FUNCTION aap_labeling_v1.admit_prediction_retry", 1)[0]
    claim = state.split(
        "CREATE FUNCTION aap_jobs_v1.claim_prediction_retry_deliveries", 1
    )[1].split(
        "REVOKE ALL ON FUNCTION aap_jobs_v1.claim_prediction_retry_deliveries", 1
    )[0]
    failure = finalization.split(
        "CREATE FUNCTION aap_labeling_v1.finish_prediction_retry_failure", 1
    )[1].split(
        "REVOKE ALL ON FUNCTION aap_labeling_v1.finish_prediction_retry_failure", 1
    )[0]
    assert "NOT (p_payload ? 'legacy_delivery')" in admit
    assert "'legacy_delivery',false" in admit
    assert (
        "jsonb_typeof(p_payload -> 'legacy_delivery') IS DISTINCT FROM 'boolean'"
        in admit
    )
    assert "NOT (p_payload ? 'job_id')" in claim
    assert "NOT (p_payload ? 'limit')" in claim
    assert "'limit',100" in claim
    assert "NOT (p_payload ? 'lease_seconds')" in claim
    assert "'lease_seconds',30" in claim
    assert "NOT (p_payload ? 'abandon')" in failure
    assert "'abandon',false" in failure
    assert "jsonb_typeof(p_payload -> 'abandon') IS DISTINCT FROM 'boolean'" in failure


def test_all_new_native_functions_revoke_public_and_downgrade_restores_0231():
    module = _migration()
    for filename in module._UPGRADE_ARTIFACTS:
        statements = module._statements(filename)
        functions = [
            statement
            for statement in statements
            if statement.startswith("CREATE FUNCTION")
            or statement.startswith("CREATE OR REPLACE FUNCTION")
        ]
        assert functions
        assert all("SET timezone TO 'UTC'" in statement for statement in functions)
        revoked = {
            re.sub(r"\s+", "", match.group(1)).lower()
            for statement in statements
            if (
                match := re.search(
                    r"(?is)^REVOKE\s+ALL\s+ON\s+FUNCTION\s+(.+?)\s+FROM\s+PUBLIC\s*;$",
                    statement,
                )
            )
        }
        assert len(revoked) >= len(functions)
    restored = module._replacement_functions()
    assert len(restored) == len(module._RESTORED_FUNCTIONS)
    assert all(
        statement.startswith("CREATE OR REPLACE FUNCTION") for statement in restored
    )


def test_downgrade_keeps_retry_history_guarded():
    module = _migration()
    assert module._DOWNGRADE_GUARDS == {
        "attempts": "SELECT count(*) FROM public.prediction_retry_attempts",
        "deliveries": "SELECT count(*) FROM public.prediction_retry_deliveries",
    }
