"""Static contract for the native ordinary batch-result authority."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql"
MIGRATION = ROOT / "apps/api/alembic/versions/0231_preannotation_results.py"
ARTIFACT = "0231_preannotation_results.sql"
ROOTS_ARTIFACT = "0231_preannotation_result_mask_roots.sql"


def _migration():
    spec = importlib.util.spec_from_file_location(
        "preannotation_results_migration", MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, count):
        self.count = count

    def scalar_one(self):
        return self.count


class _Bind:
    def __init__(self, counts):
        self.counts = counts
        self.queries = []

    def execute(self, statement):
        query = str(statement)
        self.queries.append(query)
        return _Result(self.counts.get(query, 0))


def _record_operations(monkeypatch, module):
    emitted = []
    monkeypatch.setattr(
        module.op, "execute", lambda statement: emitted.append(statement)
    )
    monkeypatch.setattr(
        module.op,
        "drop_table",
        lambda *args, **kwargs: emitted.append(("drop_table", args, kwargs)),
    )
    return emitted


def test_revision_is_additive_and_has_two_native_artifacts():
    module = _migration()

    assert module._UPGRADE_ARTIFACTS == (ARTIFACT, ROOTS_ARTIFACT)
    assert (SQL / ARTIFACT).is_file()
    assert (SQL / ROOTS_ARTIFACT).is_file()


def test_all_native_functions_revoke_public_and_protocol_operations_are_complete():
    module = _migration()
    statements = module._statements(ARTIFACT)
    signatures = set(module._new_function_signatures())
    assert len(signatures) >= 36
    assert all(
        re.search(r"(?is)^REVOKE ALL ON FUNCTION .* FROM PUBLIC;$", statement)
        for statement in statements
        if statement.startswith("REVOKE ALL ON FUNCTION")
    )
    expected_jobs = {
        "aap_jobs_v1.claim_batch_result_execution(jsonb)",
        "aap_jobs_v1.heartbeat_batch_result_execution(jsonb)",
        "aap_jobs_v1.release_batch_result_execution(jsonb)",
        "aap_jobs_v1.stage_batch_result_members(jsonb)",
        "aap_jobs_v1.start_batch_result_execution(jsonb)",
        "aap_jobs_v1.read_batch_result_execution(jsonb)",
        "aap_jobs_v1.mark_batch_result_overwrite(jsonb)",
        "aap_jobs_v1.skip_batch_result_member(jsonb)",
        "aap_jobs_v1.finish_batch_result_execution(jsonb)",
        "aap_jobs_v1.start_batch_result_member(jsonb)",
        "aap_jobs_v1.lock_batch_result_execution(jsonb)",
        "aap_jobs_v1.advance_batch_result_member(jsonb)",
        "aap_jobs_v1.record_batch_result_member(jsonb)",
        "aap_jobs_v1.batch_result_execution_referenced(jsonb)",
    }
    expected_labeling = {
        "aap_labeling_v1.begin_batch_result(jsonb)",
        "aap_labeling_v1.put_batch_result_part(jsonb)",
        "aap_labeling_v1.seal_batch_result(jsonb)",
        "aap_labeling_v1.read_batch_result(jsonb)",
        "aap_labeling_v1.lock_batch_result_write(jsonb)",
        "aap_labeling_v1.finish_batch_result_write(jsonb)",
        "aap_labeling_v1.finish_batch_result_failure(jsonb)",
        "aap_labeling_v1.batch_result_commit_proof(jsonb)",
        "aap_labeling_v1.batch_result_referenced(jsonb)",
        "aap_labeling_v1.retire_batch_results(jsonb)",
    }
    assert expected_jobs | expected_labeling <= signatures
    assert any(
        module._signature_key(signature)
        == module._signature_key(module._MASK_GC_ROOT_LIVENESS)
        for signature in signatures
    )
    assert "CREATE TRIGGER trg_async_jobs_batch_result_terminal" in "\n".join(
        statements
    )
    joined = "\n".join(statements)
    assert "CREATE TRIGGER trg_async_jobs_batch_result_delete_guard" in joined
    assert "CREATE TRIGGER trg_batch_result_payload_mask_roots" in joined
    assert (
        "p_payload -> 'execution_id' <> 'null'::jsonb" in joined
        and "pg_catalog.jsonb_typeof(p_payload -> 'execution_id') IS DISTINCT FROM 'string'"
        in joined
    )
    assert "IF v_bundle.raw_complete" in joined
    assert "byte_size bigint NOT NULL CHECK (byte_size >= 0)" in joined
    assert "part_count bigint NOT NULL CHECK (part_count >= 1)" in joined
    assert "REFERENCES public.async_jobs" not in joined
    assert "2147483647" not in joined
    assert "BETWEEN 1 AND 4" not in joined
    assert "SET timezone TO 'UTC'" in joined
    assert "pg_catalog.octet_length(content) <= 524288" in joined
    assert "pg_catalog.string_agg(content, ''::bytea ORDER BY part_index)" in joined
    assert "IF v_count = 0 AND NOT v_complete" in joined
    assert "COALESCE(\n            v_extra -> 'pipeline_stages'" in joined
    assert "bundle.state IN ('capturing','captured','ready')" in joined
    assert "IF v_status = 'completed' THEN" in joined
    assert "v_error !~" not in joined
    assert "v_manifest_item <> 'null'::jsonb AND (" in joined
    function_statements = [
        statement for statement in statements if statement.startswith("CREATE FUNCTION")
    ]
    assert function_statements
    assert all(
        "SET timezone TO 'UTC'" in statement for statement in function_statements
    )


def test_native_tables_are_excluded_from_orm_autogeneration():
    from app.db.native_schema import NATIVE_MANAGED_TABLES

    assert {
        "batch_result_executions",
        "batch_result_members",
        "batch_result_bundles",
        "batch_result_payloads",
        "batch_result_parts",
    } <= NATIVE_MANAGED_TABLES


def test_downgrade_preserves_any_persisted_execution_or_receipt(monkeypatch):
    module = _migration()
    for name, query in module._DOWNGRADE_GUARDS.items():
        bind = _Bind({query: 1})
        emitted = _record_operations(monkeypatch, module)
        monkeypatch.setattr(module.op, "get_bind", lambda bind=bind: bind)

        with pytest.raises(RuntimeError, match=name):
            module.downgrade()

        assert query in bind.queries
        assert emitted == []


def test_empty_downgrade_removes_triggers_before_functions_and_tables(monkeypatch):
    module = _migration()
    bind = _Bind({})
    emitted = _record_operations(monkeypatch, module)
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)
    monkeypatch.setattr(
        module, "_new_function_signatures", lambda: ["aap_jobs_v1._helper()"]
    )

    module.downgrade()

    statements = [str(item) for item in emitted if not isinstance(item, tuple)]
    assert statements[:3] == [
        "DROP TRIGGER IF EXISTS trg_batch_result_payload_mask_roots ON public.batch_result_payloads RESTRICT",
        "DROP TRIGGER IF EXISTS trg_async_jobs_batch_result_terminal ON public.async_jobs RESTRICT",
        "DROP TRIGGER IF EXISTS trg_async_jobs_batch_result_delete_guard ON public.async_jobs RESTRICT",
    ]
    assert statements[3] == (
        "DELETE FROM public.mask_gc_roots WHERE root_kind = 'batch_result_payloads'"
    )
    restored = next(
        index
        for index, statement in enumerate(statements)
        if "CREATE OR REPLACE FUNCTION aap_labeling_v1._mask_gc_root_entry_live"
        in statement
    )
    assert restored == 4
    assert "video_tracker_input_scopes" in statements[restored]
    assert "batch_result_payloads" not in statements[restored]
    assert statements[-1] == "DROP FUNCTION IF EXISTS aap_jobs_v1._helper() RESTRICT"
    dropped = [item[1][0] for item in emitted if isinstance(item, tuple)]
    assert dropped == [
        "batch_result_parts",
        "batch_result_payloads",
        "batch_result_bundles",
        "batch_result_members",
        "batch_result_executions",
    ]


def test_downgrade_restores_the_immutable_0229_helper_before_dropping_tables():
    module = _migration()
    restored = module._previous_mask_gc_root_liveness_statements()
    previous = [
        statement
        for statement in module._statements("0229_mask_gc.sql")
        if statement.startswith(
            "CREATE FUNCTION aap_labeling_v1._mask_gc_root_entry_live"
        )
    ][0]
    assert restored[0] == previous.replace(
        "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
    )
    assert module._signature_key(module._function_signature(restored[1])) == (
        module._signature_key(module._MASK_GC_ROOT_LIVENESS)
    )
