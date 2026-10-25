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


@pytest.mark.parametrize(
    "role,functions,artifact",
    [
        (
            verifier.IDENTITY_CLIENT_ROLES["INFERENCE_POOL_REFERENCE"],
            ("aap_inference_v1.ai_mask_pool_reference_row(uuid)",),
            "0356_inference_pool_reference",
        ),
        (
            verifier.JOBS_LIST_QUERY_ROLE,
            verifier.JOBS_LIST_QUERY_FUNCTIONS,
            "0326_jobs_list_query",
        ),
        (
            verifier.JOBS_LIFECYCLE_ROLE,
            verifier.JOBS_LIFECYCLE_FUNCTIONS,
            "0327_jobs_lifecycle",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_EDITING"],
            ("aap_labeling_v1.annotation_bulk_command(jsonb)",),
            "0339_labeling_annotation_bulk",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_EDITING"],
            ("aap_labeling_v1.video_bbox_editing_command(jsonb)",),
            "0340_labeling_video_bbox_editing",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_BATCH_CONTROL"],
            ("aap_labeling_v1.batch_control_command(jsonb)",),
            "0341_labeling_batch_control",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_PREDICTION"],
            ("aap_labeling_v1.prediction_record_command(jsonb)",),
            "0342_labeling_prediction_records",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_SCENE_TRACK_COMMAND"],
            ("aap_labeling_v1.scene_track_command(jsonb)",),
            "0343_labeling_scene_track_commands",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_CROSS_FRAME"],
            ("aap_labeling_v1.cross_frame_write_command(jsonb)",),
            "0344_labeling_cross_frame_writes",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_PREDICTION"],
            ("aap_labeling_v1.annotation_prediction_command(jsonb)",),
            "0345_labeling_annotation_prediction",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_ANNOTATION_MAINTENANCE"],
            ("aap_labeling_v1.annotation_maintenance_command(jsonb)",),
            "0346_labeling_annotation_maintenance",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_DISCUSSION"],
            (
                "aap_labeling_v1.discussion_command(jsonb)",
                "aap_labeling_v1.task_event_command(jsonb)",
            ),
            "0347_labeling_discussion",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_MASK_EDIT"],
            (
                "aap_labeling_v1.mask_edit_command(jsonb)",
                "aap_labeling_v1.mask_revision_command(jsonb)",
                "aap_labeling_v1.ai_mask_accept_command(jsonb)",
            ),
            "0348_labeling_mask_edits",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_VIDEO_CONTROL"],
            ("aap_labeling_v1.video_control_command(jsonb)",),
            "0350_labeling_video_control",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_ANNOTATION_TRANSFER"],
            ("aap_labeling_v1.annotation_transfer_command(jsonb)",),
            "0351_labeling_annotation_transfer",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_CORE_READ"],
            (
                "aap_labeling_v1.lock_task_for_core_read(uuid,text,boolean)",
                "aap_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean)",
                "aap_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean)",
            ),
            "0352_labeling_core_read",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_MASK_QUALITY"],
            (
                "aap_labeling_v1.mask_qc_command(jsonb)",
                "aap_labeling_v1.mask_repair_command(jsonb)",
                "aap_labeling_v1.raster_mask_retention_query(jsonb)",
            ),
            "0353_labeling_mask_quality",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_SPATIAL_QUALITY"],
            ("aap_labeling_v1.spatial_quality_command(jsonb)",),
            "0354_labeling_spatial_quality",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_TASK_MAINTENANCE"],
            ("aap_labeling_v1.task_maintenance_command(jsonb)",),
            "0355_labeling_task_maintenance",
        ),
        (
            verifier.IDENTITY_CLIENT_ROLES["LABELING_SCENE_READ"],
            ("aap_labeling_v1.scene_read_query(jsonb)",),
            "0349_labeling_scene_read",
        ),
    ],
)
def test_capability_logins_have_no_temporary_grant(
    monkeypatch, role, functions, artifact
):
    captured = []
    monkeypatch.setattr(
        verifier, "_psql", lambda *args, **kwargs: captured.append(kwargs["sql"])
    )
    arguments = {
        name: "fixture-value"
        for name in inspect.signature(verifier._create_module_roles).parameters
    }
    arguments["database"] = "jobs_fixture_test"
    verifier._create_module_roles(**arguments)
    sql = captured[0]
    # main() creates mapped capability logins after the base roles.
    if role not in verifier.IDENTITY_CLIENT_ROLES.values():
        assert (
            f"CREATE ROLE {role} LOGIN PASSWORD 'fixture-value' "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
        ) in sql
    assert role in verifier.MODULE_ROLES
    temporary_grant = sql.split("GRANT TEMPORARY ON DATABASE", 1)[1]
    assert role not in temporary_grant
    assert artifact + ".sql" in verifier.SQL_ARTIFACTS
    if artifact != "0352_labeling_core_read":
        assert artifact + ".schema.json" in verifier.SCHEMA_ARTIFACTS
    for signature in functions:
        assert verifier.FUNCTION_BODY_ARTIFACTS[signature] == artifact + ".sql"


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
