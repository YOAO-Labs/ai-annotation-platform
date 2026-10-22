from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from aap_workspace.seed_project_contract import (
    PROTOCOL,
    NuScenesSeedDefaultsWrite,
    PointCloudSeedProjectCreated,
    ProjectMaintenanceUnavailable,
    decode_nuscenes_seed_defaults_write,
    decode_point_cloud_seed_project_created,
    seed_project_schema,
)


def _created(*, display_id: str, project_id: uuid.UUID) -> dict:
    return {
        "schema": PROTOCOL,
        "display_id": display_id,
        "project_id": str(project_id),
    }


def _defaults_write(*, project_id: uuid.UUID, updated_at: str | None) -> dict:
    return {
        "schema": PROTOCOL,
        "project_id": str(project_id),
        "updated_at": updated_at,
    }


def test_seed_project_create_receipt_binds_display_and_generated_id():
    project_id = uuid.uuid4()
    reply = _created(display_id="P-PC-MULTI", project_id=project_id)

    assert (
        decode_point_cloud_seed_project_created(
            reply,
            display_id="P-PC-MULTI",
            expected_project_id=project_id,
        )
        == project_id
    )
    assert (
        decode_point_cloud_seed_project_created(
            json.dumps(reply),
            display_id="P-PC-MULTI",
            expected_project_id=project_id,
        )
        == project_id
    )
    assert (
        decode_point_cloud_seed_project_created(
            PointCloudSeedProjectCreated.model_validate_json(json.dumps(reply)),
            display_id="P-PC-MULTI",
            expected_project_id=project_id,
        )
        == project_id
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"display_id": "P-PC-OTHER"},
        {"project_id": str(uuid.uuid4())},
    ],
)
def test_seed_project_create_receipt_rejects_scope_mismatch(changes):
    project_id = uuid.uuid4()
    reply = _created(display_id="P-PC-MULTI", project_id=project_id)
    reply.update(changes)

    with pytest.raises(ProjectMaintenanceUnavailable):
        decode_point_cloud_seed_project_created(
            reply,
            display_id="P-PC-MULTI",
            expected_project_id=project_id,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": PROTOCOL, "project_id": str(uuid.uuid4())},
        {
            "schema": PROTOCOL,
            "display_id": "P-PC-MULTI",
            "project_id": None,
        },
        {
            "schema": PROTOCOL,
            "display_id": "P-PC-MULTI",
            "project_id": str(uuid.uuid4()),
            "extra": True,
        },
        {
            "protocol": PROTOCOL,
            "display_id": "P-PC-MULTI",
            "project_id": str(uuid.uuid4()),
        },
        {
            "schema": "aap-workspace-seed-project/v2",
            "display_id": "P-PC-MULTI",
            "project_id": str(uuid.uuid4()),
        },
    ],
)
def test_seed_project_create_receipt_rejects_invalid_closed_payloads(payload):
    with pytest.raises(ProjectMaintenanceUnavailable):
        decode_point_cloud_seed_project_created(
            payload,
            display_id="P-PC-MULTI",
            expected_project_id=uuid.uuid4(),
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        (
            b'{"schema":"aap-workspace-seed-project/v1",'
            b'"schema":"aap-workspace-seed-project/v1",'
            b'"display_id":"P-PC-MULTI","project_id":"'
            + str(uuid.uuid4()).encode()
            + b'"}'
        ),
        (
            b'{"schema":"aap-workspace-seed-project/v1",'
            b'"display_id":"P-PC-MULTI","project_id":NaN}'
        ),
    ],
)
def test_seed_project_create_receipt_rejects_malformed_json(payload):
    with pytest.raises(ProjectMaintenanceUnavailable):
        decode_point_cloud_seed_project_created(
            payload,
            display_id="P-PC-MULTI",
            expected_project_id=uuid.uuid4(),
        )


def test_nuscenes_seed_defaults_write_accepts_aware_and_missing_receipts():
    project_id = uuid.uuid4()
    timestamp = "2026-10-22T07:40:12.345678Z"

    assert decode_nuscenes_seed_defaults_write(
        _defaults_write(project_id=project_id, updated_at=timestamp),
        project_id=project_id,
    ) == datetime(2026, 10, 22, 7, 40, 12, 345678, tzinfo=UTC)
    assert (
        decode_nuscenes_seed_defaults_write(
            _defaults_write(project_id=project_id, updated_at=None),
            project_id=project_id,
        )
        is None
    )
    assert NuScenesSeedDefaultsWrite.model_config["frozen"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": str(uuid.uuid4())},
        {"updated_at": "2026-10-22T07:40:12"},
        {"updated_at": "not-a-timestamp"},
        {"unexpected": True},
        {"schema": "aap-workspace-seed-project/v2"},
    ],
)
def test_nuscenes_seed_defaults_write_rejects_invalid_receipts(changes):
    project_id = uuid.uuid4()
    reply = {
        **_defaults_write(
            project_id=project_id,
            updated_at="2026-10-22T07:40:12+00:00",
        ),
        **changes,
    }

    with pytest.raises(ProjectMaintenanceUnavailable):
        decode_nuscenes_seed_defaults_write(reply, project_id=project_id)


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        (
            b'{"schema":"aap-workspace-seed-project/v1",'
            b'"schema":"aap-workspace-seed-project/v1",'
            b'"project_id":"' + str(uuid.uuid4()).encode() + b'","updated_at":null}'
        ),
        (
            b'{"schema":"aap-workspace-seed-project/v1",'
            b'"project_id":"' + str(uuid.uuid4()).encode() + b'","updated_at":NaN}'
        ),
    ],
)
def test_nuscenes_seed_defaults_write_rejects_malformed_json(payload):
    with pytest.raises(ProjectMaintenanceUnavailable):
        decode_nuscenes_seed_defaults_write(payload, project_id=uuid.uuid4())


def test_seed_project_schema_freezes_functions_and_transaction_boundaries():
    schema = seed_project_schema()
    descriptor = schema["x-native-capabilities"]
    functions = descriptor["functions"]

    assert schema["$id"] == PROTOCOL
    assert descriptor["postgres_schema"] == "aap_workspace_v1"
    assert [function["signature"] for function in functions] == [
        "aap_workspace_v1.create_point_cloud_seed_project(uuid, text, text, uuid, jsonb)",
        "aap_workspace_v1.write_nuscenes_seed_defaults(uuid, text, jsonb)",
    ]
    assert [
        [argument["name"] for argument in function["arguments"]]
        for function in functions
    ] == [
        [
            "p_project_id",
            "p_display_id",
            "p_name",
            "p_owner_id",
            "p_tool_bindings",
        ],
        ["p_project_id", "p_name", "p_tool_bindings"],
    ]
    assert [function["result_model"] for function in functions] == [
        "PointCloudSeedProjectCreated",
        "NuScenesSeedDefaultsWrite",
    ]
    assert all(function["result_postgres_type"] == "jsonb" for function in functions)
    assert "not an upsert" in functions[0]["transaction_semantics"]
    assert "null updated_at" in functions[1]["transaction_semantics"]
    assert "PointCloudSeedProjectCreated" in schema["$defs"]
    assert "NuScenesSeedDefaultsWrite" in schema["$defs"]
    assert PointCloudSeedProjectCreated.model_config["frozen"] is True
