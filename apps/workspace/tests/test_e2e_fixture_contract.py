from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from aap_workspace.e2e_fixture_contract import (
    E2EProjectCleanupGuard,
    E2EProjectRecord,
    E2ETemplateRecord,
    PROTOCOL,
    WorkspaceE2EFixtureUnavailable,
    decode_fixture_cleanup_guard,
    decode_fixture_count,
    decode_fixture_project_id,
    e2e_fixture_schema,
)


def _project_id() -> uuid.UUID:
    return uuid.UUID("c2725b13-3c09-49ed-9a1d-e1dc0252716e")


def _cleanup_guard(*, ok: bool, reason: str | None) -> dict:
    return {"schema": PROTOCOL, "ok": ok, "reason": reason}


def test_project_and_template_requests_match_fixed_closed_record_shapes():
    project_id = _project_id()
    project = E2EProjectRecord(
        id=project_id,
        display_id="P-E2E-000001",
        name="E2E Project",
        type_label="图像目标检测",
        type_key="image-det",
        data_type="image",
        owner_id=uuid.uuid4(),
        tool_bindings={"bbox": {"enabled": True, "classes": ["car"]}},
        scene_mode=False,
        ai_enabled=False,
    )
    template = E2ETemplateRecord(
        id=uuid.uuid4(),
        display_id="TPL-E2E-FILTER-000001",
        name="Filter Template",
        description="private fixture",
        type_label="Image detection",
        type_key="image-det",
        data_type="image",
        tool_bindings={"bbox": {"enabled": True}},
        label_config={},
        scope="private",
        created_by=uuid.uuid4(),
        source_project_id=project_id,
    )

    assert tuple(E2EProjectRecord.model_fields) == (
        "id",
        "display_id",
        "name",
        "type_label",
        "type_key",
        "data_type",
        "owner_id",
        "tool_bindings",
        "scene_mode",
        "ai_enabled",
    )
    assert tuple(E2ETemplateRecord.model_fields) == (
        "id",
        "display_id",
        "name",
        "description",
        "type_label",
        "type_key",
        "data_type",
        "tool_bindings",
        "label_config",
        "scope",
        "created_by",
        "source_project_id",
    )
    assert project.model_dump(mode="json")["id"] == str(project_id)
    assert template.model_dump(mode="json")["source_project_id"] == str(project_id)


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": True},
        {"ai_enabled": 0},
        {"scene_mode": "false"},
        {"tool_bindings": []},
        {"id": "not-a-uuid"},
    ],
)
def test_project_request_rejects_extra_fields_and_coercion(changes):
    values = {
        "id": _project_id(),
        "display_id": "P-E2E-000001",
        "name": "E2E Project",
        "type_label": "图像目标检测",
        "type_key": "image-det",
        "data_type": "image",
        "owner_id": uuid.uuid4(),
        "tool_bindings": {},
        "scene_mode": False,
        "ai_enabled": False,
    }
    values.update(changes)
    with pytest.raises(ValidationError):
        E2EProjectRecord.model_validate(values)


def test_project_id_decoder_requires_canonical_uuid_and_exact_scope():
    project_id = _project_id()
    assert decode_fixture_project_id(project_id, project_id) == project_id
    assert decode_fixture_project_id(str(project_id), project_id) == project_id

    for payload, expected in (
        (uuid.uuid4(), project_id),
        (str(project_id).upper(), project_id),
        (True, project_id),
        (str(project_id), str(project_id)),
    ):
        with pytest.raises(WorkspaceE2EFixtureUnavailable):
            decode_fixture_project_id(payload, expected)


def test_count_decoder_rejects_bool_negative_and_unexpected_excess():
    assert decode_fixture_count(2) == 2
    assert decode_fixture_count(2, expected_max=2) == 2
    for payload, maximum in ((True, None), (-1, None), (3, 2), (1.0, None)):
        with pytest.raises(WorkspaceE2EFixtureUnavailable):
            decode_fixture_count(payload, expected_max=maximum)


def test_cleanup_guard_decoder_accepts_only_closed_retirement_result():
    assert decode_fixture_cleanup_guard(_cleanup_guard(ok=True, reason=None)) is True
    blocked = _cleanup_guard(ok=False, reason="video_execution_not_retired")
    assert decode_fixture_cleanup_guard(json.dumps(blocked)) is False
    assert E2EProjectCleanupGuard.model_validate(blocked).ok is False

    for payload in (
        {**blocked, "reason": None},
        {**blocked, "extra": True},
        {**blocked, "schema": "aap-workspace-e2e-fixtures/v2"},
        {"schema": PROTOCOL, "ok": True},
        (
            b'{"schema":"aap-workspace-e2e-fixtures/v1",'
            b'"ok":true,"ok":true,"reason":null}'
        ),
    ):
        with pytest.raises(WorkspaceE2EFixtureUnavailable):
            decode_fixture_cleanup_guard(payload)


def test_checked_in_schemas_match_the_closed_fixture_contract():
    expected = e2e_fixture_schema()
    workspace_schema = (
        Path(__file__).resolve().parents[1]
        / "src/aap_workspace/e2e_fixture_protocol.schema.json"
    )
    migration_schema = (
        Path(__file__).resolve().parents[2]
        / "api/alembic/sql/0309_workspace_e2e_fixtures.schema.json"
    )

    assert json.loads(workspace_schema.read_text(encoding="utf-8")) == expected
    assert json.loads(migration_schema.read_text(encoding="utf-8")) == expected
    assert [
        function["signature"]
        for function in expected["x-native-capabilities"]["functions"]
    ] == [
        "aap_workspace_fixture_v1.create_project(jsonb)",
        "aap_workspace_fixture_v1.create_templates(jsonb)",
        "aap_workspace_fixture_v1.guard_project_cleanup(uuid[])",
        "aap_workspace_fixture_v1.delete_projects(uuid[])",
        "aap_workspace_fixture_v1.delete_templates(boolean, text[])",
    ]
