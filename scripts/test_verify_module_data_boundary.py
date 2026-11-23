from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_module_data_boundary import (
    FROZEN_OWNER_NAMESPACES,
    READ_RELATION_SPECS,
    _assert_read_relation_structure_matches,
    _sql_artifact_path,
    _normalize_read_relation_observations,
    _installed_function_body_sha256,
)
import verify_module_data_boundary as verifier


def _qualified_namespace(body: bytes) -> bytes:
    """Model the in-place namespace cutover applied to the installed body."""
    for current, frozen in FROZEN_OWNER_NAMESPACES.items():
        body = body.replace(f"{frozen}.".encode(), f"{current}.".encode())
    return body


def test_installed_body_uses_named_function_in_multi_function_resource(
    tmp_path, monkeypatch
):
    sql_dir = tmp_path / "apps/api/alembic/sql"
    sql_dir.mkdir(parents=True)
    (sql_dir / "queries.sql").write_text(
        "CREATE FUNCTION aap_test.first() RETURNS int LANGUAGE sql "
        "AS $function$SELECT 1$function$;\n"
        "CREATE FUNCTION aap_test.second() RETURNS int LANGUAGE sql "
        "AS $function$SELECT 2$function$;\n"
    )
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    signature = "aap_test.second()"
    assert _installed_function_body_sha256(signature, "queries.sql", "SELECT 2") == (
        hashlib.sha256(b"SELECT 2").hexdigest()
    )
    with pytest.raises(RuntimeError, match="differs from frozen SQL"):
        _installed_function_body_sha256(signature, "queries.sql", "SELECT 1")
    with pytest.raises(RuntimeError, match="body is missing"):
        _installed_function_body_sha256(signature, "queries.sql", None)


def test_insights_saved_view_body_references_current_assets_owner():
    name = "0487_insights_dataset_views.sql"
    signature = "yoao_insights_v1.prepare_personal_dataset_view(uuid,uuid)"
    source = _sql_artifact_path(name).read_text(encoding="utf-8")
    body = source.split("CREATE FUNCTION " + signature.split("(")[0], 1)[1].split(
        "$function$", 2
    )[1]
    assert "yoao_assets_v1.lock_dataset_reference(p_dataset)" in body
    assert verifier.FUNCTION_BODY_ARTIFACTS[signature] == name
    assert (
        _installed_function_body_sha256(signature, name, body)
        == hashlib.sha256(body.encode()).hexdigest()
    )


def test_installed_mask_owner_requires_acceptance_history_dispatch(monkeypatch):
    path = verifier.ROOT / "apps/api/alembic/versions/0430_acceptance_history.py"
    spec = importlib.util.spec_from_file_location("acceptance_history_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    captured = []
    monkeypatch.setattr(migration, "_apply", captured.append)
    migration._mask_command(history=True)
    installed = _qualified_namespace(captured[0].split("$function$", 2)[1].encode())
    arguments = dict(
        signature="yoao_labeling_v1.mask_edit_command(jsonb)",
        sql_name="0348_labeling_mask_edits.sql",
    )
    assert _installed_function_body_sha256(actual=installed.decode(), **arguments) == (
        hashlib.sha256(installed).hexdigest()
    )
    captured.clear()
    migration._mask_command(history=False)
    old_body = _qualified_namespace(captured[0].split("$function$", 2)[1].encode())
    with pytest.raises(RuntimeError, match="differs from frozen SQL"):
        _installed_function_body_sha256(actual=old_body.decode(), **arguments)


def test_view_evidence_hashes_catalog_deparser_and_tracks_owner_transfer():
    deparser_definitions = {
        relation_name: f"SELECT source.{index} FROM public.source_{index}".encode()
        for index, relation_name in enumerate(READ_RELATION_SPECS, start=1)
    }

    def observations(*, owners, changed_type: str | None = None):
        result = []
        for index, (relation_name, spec) in enumerate(
            READ_RELATION_SPECS.items(), start=1
        ):
            columns = [
                {"name": name, "type": type_name} for name, type_name in spec["columns"]
            ]
            if changed_type is not None and index == 1:
                columns[0]["type"] = changed_type
            result.append(
                {
                    "relation": relation_name,
                    "deparser_hex": deparser_definitions[relation_name].hex(),
                    "relkind": "v",
                    "owner": owners[relation_name],
                    "reloptions": ["security_barrier=true"],
                    "columns": columns,
                }
            )
        return json.dumps(result)

    migration_owners = {name: "owner" for name in READ_RELATION_SPECS}
    code_owners = {name: spec["owner"] for name, spec in READ_RELATION_SPECS.items()}
    before = _normalize_read_relation_observations(
        observations(owners=migration_owners), expected_owner="owner"
    )
    after = _normalize_read_relation_observations(
        observations(owners=code_owners), expected_owner=code_owners
    )

    _assert_read_relation_structure_matches(
        before, after, phase="module owner transfer"
    )
    for relation_name, evidence in before.items():
        assert (
            evidence["pg_get_viewdef_sha256"]
            == hashlib.sha256(deparser_definitions[relation_name]).hexdigest()
        )
        assert evidence["definition_source"] == "pg_catalog.pg_get_viewdef(oid, true)"
        assert evidence["metadata"]["owner"] == "owner"
        assert "deparser_hex" not in evidence

    with pytest.raises(RuntimeError, match="fixed contract"):
        _normalize_read_relation_observations(
            observations(owners=code_owners, changed_type="text"),
            expected_owner=code_owners,
        )
