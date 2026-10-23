"""Contract and reply-validation tests for the local scale-seed boundary."""

import json
from pathlib import Path
import uuid

import pytest

from aap_workspace.scale_seed_contract import (
    ASSETS_NATIVE_SCHEMA,
    NATIVE_SCHEMA,
    PROTOCOL,
    SOURCE_GUARD_PROTOCOL,
    ScaleSeedCleanup,
    ScaleSeedSourceGuard,
    WorkspaceScaleSeedUnavailable,
    decode_scale_seed_cleanup,
    decode_scale_seed_count,
    decode_scale_seed_project_id,
    decode_scale_seed_source_guard,
    scale_seed_schema,
)


def _cleanup_reply(*, marker: str, project_ids: list[uuid.UUID], ok: bool = True):
    return {
        "schema": PROTOCOL,
        "marker": marker,
        "project_ids": [str(project_id) for project_id in project_ids],
        "ok": ok,
        "error": None if ok else {"code": "retained_training_runs"},
    }


def _source_reply(*, dataset_id: uuid.UUID, after_task_cleanup: bool, ok: bool = True):
    return {
        "schema": SOURCE_GUARD_PROTOCOL,
        "dataset_id": str(dataset_id),
        "after_task_cleanup": after_task_cleanup,
        "ok": ok,
        "error": None if ok else {"code": "source_in_use"},
    }


def test_project_id_decoder_accepts_only_uuid_or_canonical_string():
    project_id = uuid.uuid4()

    assert decode_scale_seed_project_id(project_id) == project_id
    assert decode_scale_seed_project_id(str(project_id)) == project_id
    for payload in (
        str(project_id).upper(),
        "not-a-uuid",
        project_id.bytes,
        {"project_id": str(project_id)},
        True,
    ):
        with pytest.raises(WorkspaceScaleSeedUnavailable):
            decode_scale_seed_project_id(payload)


def test_count_decoder_rejects_bool_coercion_negative_and_excess_values():
    assert decode_scale_seed_count(0) == 0
    assert decode_scale_seed_count(4, maximum=4) == 4

    for payload in (True, -1, 1.0, "1", None):
        with pytest.raises(WorkspaceScaleSeedUnavailable):
            decode_scale_seed_count(payload)
    for maximum in (-1, True, 1.0):
        with pytest.raises(WorkspaceScaleSeedUnavailable):
            decode_scale_seed_count(0, maximum=maximum)
    with pytest.raises(WorkspaceScaleSeedUnavailable):
        decode_scale_seed_count(5, maximum=4)


def test_cleanup_decoder_binds_marker_and_returns_closed_models():
    project_ids = sorted((uuid.uuid4(), uuid.uuid4()))
    accepted = _cleanup_reply(marker="scale-seed", project_ids=project_ids)
    denied = _cleanup_reply(marker="scale-seed", project_ids=project_ids, ok=False)

    success = decode_scale_seed_cleanup(accepted, marker="scale-seed")
    blocked = decode_scale_seed_cleanup(json.dumps(denied), marker="scale-seed")
    assert isinstance(success, ScaleSeedCleanup)
    assert success.ok is True and success.error is None
    assert blocked.ok is False
    assert blocked.error is not None
    assert blocked.error.code == "retained_training_runs"

    for payload in (
        {**accepted, "marker": "other"},
        {**accepted, "extra": True},
        {**accepted, "protocol": PROTOCOL},
        {**accepted, "project_ids": list(reversed(accepted["project_ids"]))},
        {**accepted, "project_ids": [str(project_ids[0])] * 2},
        {**accepted, "ok": False},
        {**denied, "error": {"code": ""}},
    ):
        with pytest.raises(WorkspaceScaleSeedUnavailable):
            decode_scale_seed_cleanup(payload, marker="scale-seed")
    with pytest.raises(WorkspaceScaleSeedUnavailable):
        decode_scale_seed_cleanup(accepted, marker=" ")


def test_source_guard_decoder_binds_dataset_and_cleanup_phase():
    dataset_id = uuid.uuid4()
    accepted = _source_reply(dataset_id=dataset_id, after_task_cleanup=True)
    denied = _source_reply(dataset_id=dataset_id, after_task_cleanup=False, ok=False)

    success = decode_scale_seed_source_guard(
        accepted, dataset_id=dataset_id, after_task_cleanup=True
    )
    blocked = decode_scale_seed_source_guard(
        denied, dataset_id=dataset_id, after_task_cleanup=False
    )
    assert isinstance(success, ScaleSeedSourceGuard)
    assert success.ok is True and success.error is None
    assert blocked.ok is False
    assert blocked.error is not None
    assert blocked.error.code == "source_in_use"

    for payload in (
        {**accepted, "dataset_id": str(uuid.uuid4())},
        {**accepted, "after_task_cleanup": False},
        {**accepted, "extra": True},
        {**accepted, "protocol": SOURCE_GUARD_PROTOCOL},
        {**accepted, "ok": False},
        {**denied, "error": {"code": "source_in_use", "extra": True}},
    ):
        with pytest.raises(WorkspaceScaleSeedUnavailable):
            decode_scale_seed_source_guard(
                payload, dataset_id=dataset_id, after_task_cleanup=True
            )
    with pytest.raises(WorkspaceScaleSeedUnavailable):
        decode_scale_seed_source_guard(
            accepted, dataset_id=str(dataset_id), after_task_cleanup=True
        )
    with pytest.raises(WorkspaceScaleSeedUnavailable):
        decode_scale_seed_source_guard(
            accepted, dataset_id=dataset_id, after_task_cleanup=1
        )


def test_schema_describes_only_the_fixed_owner_capabilities():
    schema = scale_seed_schema()
    capabilities = schema["x-native-capabilities"]
    functions = capabilities["functions"]

    assert schema["$id"] == PROTOCOL
    assert capabilities["protocol"] == PROTOCOL
    assert capabilities["postgres_schema"] == NATIVE_SCHEMA
    assert capabilities["owner_domains"]["Workspace"] == NATIVE_SCHEMA
    assert capabilities["owner_domains"]["Assets"] == ASSETS_NATIVE_SCHEMA
    assert [function["name"] for function in functions] == [
        "create_scale_seed_project",
        "ensure_scale_seed_member",
        "guard_scale_seed_cleanup",
        "delete_scale_seed_projects",
        "guard_scale_seed_dataset_cleanup",
    ]
    assert [function["owner_domain"] for function in functions] == [
        "Workspace",
        "Workspace",
        "Workspace",
        "Workspace",
        "Assets",
    ]
    assert [function["signature"] for function in functions] == [
        "aap_workspace_v1.create_scale_seed_project(uuid, text)",
        "aap_workspace_v1.ensure_scale_seed_member(uuid, uuid, uuid)",
        "aap_workspace_v1.guard_scale_seed_cleanup(text)",
        "aap_workspace_v1.delete_scale_seed_projects(text, uuid[])",
        "aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid, boolean)",
    ]
    assert any(
        guard["signature"] == "aap_assets_v1._lock_dataset_source_deletion(uuid,uuid)"
        and guard["private"] is True
        for guard in capabilities["delegated_guards"]
    )
    assert "no arbitrary" in schema["$comment"]


def test_checked_in_schemas_match_the_contract_descriptor():
    expected = scale_seed_schema()
    package_schema = (
        Path(__file__).resolve().parents[1]
        / "src/aap_workspace/scale_seed_protocol.schema.json"
    )
    migration_schema = (
        Path(__file__).resolve().parents[2]
        / "api/alembic/sql/0310_workspace_scale_seed.schema.json"
    )

    assert json.loads(package_schema.read_text(encoding="utf-8")) == expected
    assert json.loads(migration_schema.read_text(encoding="utf-8")) == expected
