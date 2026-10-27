"""Static contract for the native read-only Mask GC preview authority."""

from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SQL = ROOT / "apps/api/alembic/sql"
MIGRATION = ROOT / "apps/api/alembic/versions/0234_mask_gc_preview.py"


def _migration():
    spec = importlib.util.spec_from_file_location(
        "mask_gc_preview_migration", MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_revision_is_additive_after_0233_and_installs_one_artifact():
    module = _migration()
    artifact = SQL / "0234_mask_gc_preview.sql"

    assert module._ARTIFACT == artifact.name
    assert artifact.is_file()


def test_operation_is_owner_only_and_adds_no_native_storage():
    module = _migration()
    statements = module._statements()
    sql = "\n".join(statements)
    signatures = [
        _function_signature(statement)
        for statement in statements
        if _function_signature(statement) is not None
    ]

    assert signatures == ["aap_labeling_v1.inspect_mask_gc_page(p_payload jsonb)"]
    assert statements[-1] == (
        "REVOKE ALL ON FUNCTION "
        "aap_labeling_v1.inspect_mask_gc_page(jsonb) FROM PUBLIC;"
    )
    assert "GRANT " not in sql.upper()
    assert "CREATE TABLE" not in sql.upper()
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, pg_temp" in sql
    assert "SET timezone TO 'UTC'" in sql
    assert "DROP FUNCTION IF EXISTS " in inspect.getsource(module.downgrade)


def test_request_and_page_validation_are_closed_and_bounded():
    sql = (SQL / "0234_mask_gc_preview.sql").read_text(encoding="utf-8")

    assert "octet_length(p_payload::text) > 1048576" in sql
    assert "(p_payload - ARRAY[" in sql
    assert "'after_key', 'page', 'include_reference_count'" in sql
    assert "NOT (p_payload ? 'page')" in sql
    assert "p_payload ? 'after_key'" in sql
    assert "p_payload ? 'include_reference_count'" in sql
    assert "v_input_after_key text := NULL" in sql
    assert "v_include_reference_count boolean := false" in sql
    assert "v_referenced bigint := NULL" in sql
    assert "IS DISTINCT FROM 'boolean'" in sql
    assert "(v_page - ARRAY['items', 'after_key', 'complete'])" in sql
    assert "jsonb_array_length(v_items)" in sql
    assert "v_page_count > 1000" in sql
    assert "jsonb_object_length" not in sql
    assert "WITH ORDINALITY AS entry(value, ordinal)" in sql
    assert "_mask_gc_key_valid(v_object_key)" in sql
    assert 'COLLATE "C"' in sql
    assert "COLLATE \"C\" !~ '^[!#-~]+$'" in sql
    assert "^(0|[1-9][0-9]*)$" in sql
    assert "9223372036854775807" in sql
    assert "T[0-9]{2}:[0-9]{2}:[0-9]{2}" in sql
    assert "v_page_after_key IS NULL" in sql


def test_preview_reuses_live_roots_and_staging_grace_without_mutation():
    sql = (SQL / "0234_mask_gc_preview.sql").read_text(encoding="utf-8")
    lowered = sql.lower()

    assert "clock_timestamp() - interval '24 hours'" in lowered
    assert "epoch.last_write_at" in lowered
    assert "intent.state <> 'completed'" in lowered
    assert "upload.created_at >= v_cutoff" in lowered
    assert "_mask_gc_has_live_root(v_object_key, true)" in sql
    assert "count(distinct root.object_key)" in lowered
    assert "_mask_gc_root_entry_live(" in lowered
    assert "'scanned', v_scanned" in lowered
    assert "'eligible', v_eligible" in lowered
    assert "'retained', v_retained" in lowered
    assert "v_referenced" in lowered

    assert not re.search(
        r"\b(insert|update|delete|truncate|lock)\b|pg_advisory",
        lowered,
    )
