from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError

from aap_audit_contract import ProjectDeletionAuditContext
from aap_workspace.project_deletion_contract import (
    PROJECT_DELETION_PROTOCOL,
    ProjectDeletionCommand,
    ProjectDeletionFailure,
    ProjectDeletionReceipt,
    decode_project_deletion_command,
    decode_project_deletion_reply,
    project_deletion_schema,
)

_OPERATION_ID = uuid.UUID("c15bb44c-9032-407d-9c8b-f7a39c6e4984")
_ACTOR_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_PROJECT_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")


def _receipt(**changes):
    return {
        "operation_id": str(_OPERATION_ID),
        "actor_id": str(_ACTOR_ID),
        "project_id": str(_PROJECT_ID),
        "deleted_tasks": 2,
        "deleted_annotations": 3,
        "audit_id": 1,
        **changes,
    }


def _success(**receipt_changes):
    return {
        "schema": PROJECT_DELETION_PROTOCOL,
        "ok": True,
        "data": _receipt(**receipt_changes),
    }


def _command(**changes):
    return {
        "operation_id": str(_OPERATION_ID),
        "actor_id": str(_ACTOR_ID),
        "project_id": str(_PROJECT_ID),
        "audit_context": None,
        **changes,
    }


def test_receipt_schema_remains_identical_to_the_frozen_v1_shape():
    frozen = json.loads(
        (
            Path(__file__).parents[2]
            / "api"
            / "alembic"
            / "sql"
            / "0199_project_deletion.schema.json"
        ).read_text(encoding="utf-8")
    )["$defs"]["receipt"]
    actual = ProjectDeletionReceipt.model_json_schema()
    assert set(actual["properties"]) == set(frozen["properties"])
    assert set(actual["required"]) == set(frozen["required"])
    assert actual["additionalProperties"] is frozen["additionalProperties"] is False


def test_command_is_the_existing_four_field_shape_and_audit_context_is_required():
    command = decode_project_deletion_command(_command())
    assert command.audit_context is None
    assert set(command.model_dump(mode="json")) == {
        "operation_id",
        "actor_id",
        "project_id",
        "audit_context",
    }
    assert command.model_dump(mode="json")["audit_context"] is None

    schema = ProjectDeletionCommand.model_json_schema()
    assert set(schema["properties"]) == {
        "operation_id",
        "actor_id",
        "project_id",
        "audit_context",
    }
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False

    with pytest.raises(ValidationError):
        decode_project_deletion_command(
            {key: value for key, value in _command().items() if key != "audit_context"}
        )
    with pytest.raises(ValidationError):
        decode_project_deletion_command(
            {**_command(), "schema": PROJECT_DELETION_PROTOCOL}
        )


def test_command_accepts_shared_audit_context_and_coerces_uuid_strings():
    command = decode_project_deletion_command(
        {
            **_command(),
            "audit_context": {
                "method": "DELETE",
                "path": "/api/v1/projects/one",
                "ip": "192.0.2.8",
                "request_id": "request-42",
            },
        }
    )
    assert command.operation_id == _OPERATION_ID
    assert command.audit_context == ProjectDeletionAuditContext(
        method="DELETE",
        path="/api/v1/projects/one",
        ip="192.0.2.8",
        request_id="request-42",
    )


@pytest.mark.parametrize(
    "payload",
    [
        b'{"operation_id":"x","operation_id":"y"}',
        b'{"operation_id":"x","bad":NaN}',
        b'{"operation_id":"x","bad":1e9999}',
    ],
)
def test_command_raw_json_rejects_duplicate_and_non_finite_values(payload):
    with pytest.raises(ValueError):
        decode_project_deletion_command(payload)


@pytest.mark.parametrize(
    "changed",
    [
        {"schema": "aap-project-delete/v2"},
        {"ok": 1},
        {"error": {"code": "project_busy"}},
        {"extra": "private"},
    ],
)
def test_invalid_reply_envelope_is_rejected(changed):
    with pytest.raises(ValidationError):
        decode_project_deletion_reply({**_success(), **changed})


@pytest.mark.parametrize(
    "changed",
    [
        {"deleted_tasks": -1},
        {"deleted_annotations": True},
        {"audit_id": 0},
        {"actor_email": "private@example.test"},
    ],
)
def test_receipt_rejects_invalid_counts_and_private_facts(changed):
    with pytest.raises(ValidationError):
        ProjectDeletionReceipt.model_validate(_receipt(**changed))


def test_reply_preserves_uuid_coercion_and_open_failure_codes():
    receipt = decode_project_deletion_reply(_success())
    assert receipt.operation_id == _OPERATION_ID
    assert receipt.actor_id == _ACTOR_ID
    assert receipt.project_id == _PROJECT_ID

    with pytest.raises(ProjectDeletionFailure) as raised:
        decode_project_deletion_reply(
            {
                "schema": PROJECT_DELETION_PROTOCOL,
                "ok": False,
                "error": {"code": "retained_video_execution_record"},
            }
        )
    assert raised.value.code == "retained_video_execution_record"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"aap-project-delete/v1","schema":"other"}',
        b'{"schema":"aap-project-delete/v1","ok":true,"bad":NaN}',
        b'{"schema":"aap-project-delete/v1","ok":true,"bad":1e9999}',
    ],
)
def test_reply_raw_json_rejects_duplicate_and_non_finite_values(payload):
    with pytest.raises(ValueError):
        decode_project_deletion_reply(payload)


def test_portable_descriptor_names_only_the_existing_delete_capability():
    schema = project_deletion_schema()
    descriptor = schema["x-native-capabilities"]
    assert schema["$id"] == PROJECT_DELETION_PROTOCOL
    assert len(schema["oneOf"]) == 3
    assert [item["name"] for item in descriptor["functions"]] == ["delete_project"]
    assert descriptor["functions"][0]["signature"] == (
        "aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)"
    )
    assert descriptor["functions"][0]["arguments"][-1]["nullable"] is True
    assert "caller" in descriptor["transaction_boundary"]
