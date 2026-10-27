from __future__ import annotations

import hashlib
import inspect
import ast
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_module_data_boundary import (
    READ_RELATION_SPECS,
    _assert_read_relation_structure_matches,
    _normalize_read_relation_observations,
    _installed_function_body_sha256,
)
import verify_module_data_boundary as verifier


def test_frozen_sql_install_uses_upgrade_dependency_order():
    artifacts = verifier.SQL_ARTIFACTS
    revisions = [int(name.split("_", 1)[0]) for name in artifacts]
    assert revisions == sorted(revisions)
    assert not any(name.endswith(".previous.sql") for name in artifacts)
    assert artifacts.index("0338_labeling_annotation_revision_support.sql") < (
        artifacts.index("0338_labeling_annotation_writes.sql")
    )


def test_scale_seed_capabilities_are_registered_as_fixed_labeling_functions():
    signatures = (
        "aap_labeling_v1.purge_scale_seed_labeling_rows(text,uuid[])",
        "aap_labeling_v1.create_scale_seed_batches(text,uuid,uuid,integer,uuid)",
        "aap_labeling_v1.create_scale_seed_tasks(text,uuid,uuid)",
        "aap_labeling_v1.create_scale_seed_annotations(text,uuid,uuid,integer)",
    )
    artifact = "0364_labeling_scale_seed_operations"
    assert artifact + ".sql" in verifier.SQL_ARTIFACTS
    assert artifact + ".schema.json" in verifier.SCHEMA_ARTIFACTS
    assert all(
        verifier.FUNCTION_BODY_ARTIFACTS.get(signature) == artifact + ".sql"
        for signature in signatures
    )
    assert verifier.READ_RELATION_SPECS[
        "aap_assets_v1.scale_seed_dataset_item_facts"
    ] == {
        "sql": artifact + ".sql",
        "owner": "assets_code",
        "columns": [
            ("id", "uuid"),
            ("dataset_id", "uuid"),
            ("file_name", "character varying(500)"),
        ],
    }


@pytest.mark.parametrize("suffix", ["sql", "schema.json"])
def test_pool_reference_resources_match_migration_artifacts(suffix):
    name = f"0356_inference_pool_reference.{suffix}"
    frozen = verifier.ROOT / "apps/api/alembic/sql" / name
    packaged = verifier.ROOT / "apps/inference/src/aap_inference/sql" / name
    assert packaged.read_bytes() == frozen.read_bytes()


@pytest.mark.parametrize(
    "test_path",
    [
        "tests/test_notifications_module_assembly_live.py",
        "tests/test_jobs_module_assembly_live.py",
    ],
)
def test_module_cold_case_selects_api_bootstrap_credentials(test_path):
    selected = {test_path}
    tree = ast.parse(inspect.getsource(verifier.main))
    condition = next(
        node.test
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.BinOp)
        and isinstance(node.test.op, ast.BitAnd)
        and isinstance(node.test.right, ast.Set)
        and any(
            isinstance(element, ast.Constant)
            and element.value == "tests/test_notifications_socket_runtime_live.py"
            for element in node.test.right.elts
        )
    )
    assert (
        eval(
            compile(ast.Expression(condition), "<bootstrap selector>", "eval"),
            {"selected_test_paths": selected},
        )
        == selected
    )


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
    monkeypatch.setattr(verifier, "_psql", lambda *_args, **_kwargs: b"SELECT 2".hex())
    arguments = dict(
        database="owned_test",
        owner_password="fixture-only",
        signature="aap_test.second()",
        sql_name="queries.sql",
    )
    assert _installed_function_body_sha256("id", "name", "identity", **arguments) == (
        hashlib.sha256(b"SELECT 2").hexdigest()
    )
    monkeypatch.setattr(verifier, "_psql", lambda *_args, **_kwargs: b"SELECT 1".hex())
    with pytest.raises(RuntimeError, match="differs from frozen SQL"):
        _installed_function_body_sha256("id", "name", "identity", **arguments)


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
