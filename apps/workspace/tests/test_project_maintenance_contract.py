from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from aap_workspace.project_maintenance_contract import (
    PROTOCOL,
    PointQualityConfigWrite,
    ProjectMaintenanceUnavailable,
    decode_import_project_reference,
    decode_point_quality_config_write,
    project_maintenance_schema,
)


def _reference(*, display_id: str, project_id: uuid.UUID | None) -> dict:
    return {
        "schema": PROTOCOL,
        "display_id": display_id,
        "project_id": str(project_id) if project_id is not None else None,
    }


def test_import_project_reference_preserves_opaque_display_ids_and_lookup_misses():
    project_id = uuid.uuid4()

    assert (
        decode_import_project_reference(
            _reference(display_id="", project_id=project_id), display_id=""
        )
        == project_id
    )
    assert (
        decode_import_project_reference(
            json.dumps(_reference(display_id=" 项目 / A🧪 ", project_id=None)),
            display_id=" 项目 / A🧪 ",
        )
        is None
    )


def test_import_project_create_reply_is_bound_to_caller_identity():
    project_id = uuid.uuid4()
    reply = _reference(display_id="nusc-02", project_id=project_id)

    assert (
        decode_import_project_reference(
            reply, display_id="nusc-02", expected_project_id=project_id
        )
        == project_id
    )

    with pytest.raises(ProjectMaintenanceUnavailable):
        decode_import_project_reference(
            reply, display_id="nusc-03", expected_project_id=project_id
        )
    with pytest.raises(ProjectMaintenanceUnavailable):
        decode_import_project_reference(
            _reference(display_id="nusc-02", project_id=None),
            display_id="nusc-02",
            expected_project_id=project_id,
        )
    with pytest.raises(ProjectMaintenanceUnavailable):
        decode_import_project_reference(
            _reference(display_id="nusc-02", project_id=uuid.uuid4()),
            display_id="nusc-02",
            expected_project_id=project_id,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"display_id": "x", "project_id": None},
        {"schema": PROTOCOL, "project_id": None},
        {"schema": PROTOCOL, "display_id": "x"},
        {"schema": PROTOCOL, "display_id": "x", "project_id": ""},
        {
            "schema": PROTOCOL,
            "display_id": "x",
            "project_id": None,
            "ignored": True,
        },
        {"protocol": PROTOCOL, "display_id": "x", "project_id": None},
        {
            "schema": "aap-workspace-project-maintenance/v2",
            "display_id": "x",
            "project_id": None,
        },
    ],
)
def test_import_project_reference_rejects_missing_or_unrecognized_fields(payload):
    with pytest.raises(ProjectMaintenanceUnavailable):
        decode_import_project_reference(payload, display_id="x")


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b'{"schema":"aap-workspace-project-maintenance/v1","schema":"aap-workspace-project-maintenance/v1","display_id":"x","project_id":null}',
        b'{"schema":"aap-workspace-project-maintenance/v1","display_id":"x","project_id":NaN}',
    ],
)
def test_import_project_reference_rejects_malformed_duplicate_and_nonfinite_json(
    payload,
):
    with pytest.raises(ProjectMaintenanceUnavailable):
        decode_import_project_reference(payload, display_id="x")


def test_point_quality_write_returns_aware_timestamp_for_matching_project():
    project_id = uuid.uuid4()
    timestamp = "2026-10-22T07:40:12.345678Z"
    reply = {
        "schema": PROTOCOL,
        "project_id": str(project_id),
        "updated_at": timestamp,
    }

    decoded = decode_point_quality_config_write(reply, project_id=project_id)

    assert decoded == datetime(2026, 10, 22, 7, 40, 12, 345678, tzinfo=UTC)
    assert decoded.utcoffset() is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": str(uuid.uuid4())},
        {"updated_at": "2026-10-22T07:40:12"},
        {"unexpected": True},
        {"schema": "aap-workspace-project-maintenance/v2"},
    ],
)
def test_point_quality_write_rejects_mismatched_or_invalid_receipts(changes):
    project_id = uuid.uuid4()
    reply = {
        "schema": PROTOCOL,
        "project_id": str(project_id),
        "updated_at": "2026-10-22T07:40:12+00:00",
        **changes,
    }

    with pytest.raises(ProjectMaintenanceUnavailable):
        decode_point_quality_config_write(reply, project_id=project_id)


def test_project_maintenance_schema_freezes_function_signatures_and_boundaries():
    schema = project_maintenance_schema()
    descriptor = schema["x-native-capabilities"]
    functions = descriptor["functions"]

    assert descriptor["protocol"] == PROTOCOL
    assert descriptor["postgres_schema"] == "aap_workspace_v1"
    assert [function["signature"] for function in functions] == [
        "aap_workspace_v1.read_import_project(text)",
        "aap_workspace_v1.create_import_project(uuid, text, text, uuid, jsonb)",
        "aap_workspace_v1.write_point_cloud_quality_config(uuid, jsonb)",
    ]
    assert [
        [argument["name"] for argument in function["arguments"]]
        for function in functions
    ] == [
        ["p_display_id"],
        ["p_project_id", "p_display_id", "p_name", "p_owner_id", "p_tool_bindings"],
        ["p_project_id", "p_config"],
    ]
    assert all(function["result_postgres_type"] == "jsonb" for function in functions)
    assert [function["result_model"] for function in functions] == [
        "ImportProjectReference",
        "ImportProjectCreated",
        "PointQualityConfigWrite",
    ]
    assert "Project FOR UPDATE" in functions[2]["transaction_semantics"]
    assert "not an upsert" in functions[1]["transaction_semantics"]
    assert "ImportProjectReference" in schema["$defs"]
    assert "ImportProjectCreated" in schema["$defs"]
    assert "PointQualityConfigWrite" in schema["$defs"]
    assert "project_id" in schema["$defs"]["ImportProjectCreated"]["required"]
    assert PointQualityConfigWrite.model_config["frozen"] is True
