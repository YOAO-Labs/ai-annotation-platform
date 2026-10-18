from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.services.dataset_unlink_contract import (
    DatasetUnlinkCommand,
    DatasetUnlinkFailure,
    DatasetUnlinkPreview,
    DatasetUnlinkReceipt,
    decode_unlink_reply,
)
from app.services.dataset_unlink_data import DatasetUnlinkData
from app.services.project_deletion_contract import ProjectDeletionAuditContext


ACTOR_ID = uuid.uuid4()
DATASET_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()
OPERATION_ID = uuid.uuid4()
ASSOCIATION_ID = uuid.uuid4()
AUDIT_ID = 731
BATCH_ID = uuid.uuid4()
CLEANUP_BUCKET = "media-cache-test"


def _command(**overrides) -> DatasetUnlinkCommand:
    values = {
        "operation_id": OPERATION_ID,
        "actor_id": ACTOR_ID,
        "dataset_id": DATASET_ID,
        "project_id": PROJECT_ID,
        "cleanup_bucket": CLEANUP_BUCKET,
        "audit_context": ProjectDeletionAuditContext(
            method="DELETE",
            path=f"/api/v1/datasets/{DATASET_ID}/link/{PROJECT_ID}",
            ip="127.0.0.1",
            request_id="request-123",
        ),
    }
    values.update(overrides)
    return DatasetUnlinkCommand(**values)


def _delete_data(**overrides) -> dict:
    values = {
        "operation_id": str(OPERATION_ID),
        "actor_id": str(ACTOR_ID),
        "dataset_id": str(DATASET_ID),
        "project_id": str(PROJECT_ID),
        "association_id": str(ASSOCIATION_ID),
        "cleanup_bucket": CLEANUP_BUCKET,
        "audit_id": AUDIT_ID,
        "deleted_tasks": 4,
        "deleted_annotations": 3,
        "deleted_batches": 1,
        "deleted_batch_ids": [str(BATCH_ID)],
        "soft": False,
    }
    values.update(overrides)
    return values


def _delete_reply(data: dict | None = None) -> dict:
    return {
        "schema": "aap-dataset-unlink/v1",
        "ok": True,
        "data": _delete_data() if data is None else data,
    }


class _FakeDb:
    def __init__(self, payload: object):
        self.payload = payload
        self.events: list[tuple[str, object]] = []
        self.commits = 0

    async def flush(self) -> None:
        self.events.append(("flush", None))

    async def scalar(self, statement, params: dict) -> object:
        self.events.append(("scalar", (str(statement), params)))
        return self.payload

    async def commit(self) -> None:
        self.commits += 1


def test_frozen_native_schema_matches_the_typed_bindings():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/sql/0205_dataset_unlink.schema.json"
    )
    definitions = json.loads(path.read_text())["$defs"]
    command = DatasetUnlinkCommand.model_json_schema()
    assert (
        definitions["ProjectDeletionAuditContext"]
        == command.pop("$defs")["ProjectDeletionAuditContext"]
    )
    assert definitions["command"] == command
    assert definitions["receipt"] == DatasetUnlinkReceipt.model_json_schema()
    assert definitions["previewResult"] == DatasetUnlinkPreview.model_json_schema()


def test_command_and_frozen_receipt_reject_noncanonical_inputs():
    command_data = _command().model_dump()
    with pytest.raises(ValidationError):
        DatasetUnlinkCommand.model_validate(command_data | {"private": "field"})
    with pytest.raises(ValidationError):
        DatasetUnlinkCommand.model_validate(
            command_data | {"cleanup_bucket": "bad bucket"}
        )

    decoded = decode_unlink_reply(_delete_reply())
    assert isinstance(decoded, DatasetUnlinkReceipt)
    assert decoded.deleted_batch_ids == (BATCH_ID,)

    invalid_replies = [
        {**_delete_reply(), "schema": "aap-dataset-unlink/v2"},
        _delete_reply(_delete_data() | {"private": "field"}),
        _delete_reply(_delete_data() | {"deleted_tasks": True}),
        _delete_reply(_delete_data() | {"soft": 0}),
        _delete_reply(
            _delete_data() | {"deleted_batch_ids": [str(BATCH_ID), str(BATCH_ID)]}
        ),
    ]
    for payload in invalid_replies:
        with pytest.raises((ValidationError, ValueError)):
            decode_unlink_reply(payload)

    with pytest.raises(ValueError, match="Unexpected Dataset unlink contract"):
        decode_unlink_reply(
            {
                "schema": "aap-dataset-unlink/v1",
                "ok": True,
                "data": {
                    "will_delete_tasks": 1,
                    "will_delete_annotations": 0,
                    "will_delete_batches": 0,
                },
            },
            preview=True,
        )


def test_preview_reply_uses_strict_nonnegative_integer_counts():
    payload = {
        "schema": "aap-dataset-unlink-preview/v1",
        "ok": True,
        "data": {
            "will_delete_tasks": 2,
            "will_delete_annotations": 1,
            "will_delete_batches": 0,
        },
    }
    result = decode_unlink_reply(payload, preview=True)
    assert isinstance(result, DatasetUnlinkPreview)
    assert result.will_delete_tasks == 2

    for field, value in (
        ("will_delete_tasks", True),
        ("will_delete_annotations", -1),
        ("will_delete_batches", "0"),
    ):
        invalid = payload | {"data": payload["data"] | {field: value}}
        with pytest.raises(ValidationError):
            decode_unlink_reply(invalid, preview=True)


@pytest.mark.asyncio
async def test_binding_uses_versioned_scope_and_leaves_commit_to_caller():
    command = _command()
    db = _FakeDb(_delete_reply())

    receipt = await DatasetUnlinkData(db).unlink(command)

    assert receipt.operation_id == command.operation_id
    assert receipt.actor_id == command.actor_id
    assert receipt.deleted_tasks == 4
    assert receipt.public_result() == {
        "deleted_tasks": 4,
        "deleted_annotations": 3,
        "deleted_batches": 1,
        "deleted_batch_ids": [str(BATCH_ID)],
        "soft": False,
    }
    assert [event[0] for event in db.events] == ["flush", "scalar"]
    statement, params = db.events[1][1]
    assert "aap_workspace_v1.unlink_dataset" in statement
    assert "public.dataset_unlink_operations" not in statement
    assert params == {
        "operation": command.operation_id,
        "actor": command.actor_id,
        "dataset": command.dataset_id,
        "project": command.project_id,
        "bucket": command.cleanup_bucket,
        "audit": command.audit_context.model_dump_json(),
    }
    assert json.loads(params["audit"]) == command.audit_context.model_dump(mode="json")
    assert db.commits == 0


@pytest.mark.asyncio
async def test_binding_rejects_a_receipt_from_another_command_scope():
    payload = _delete_reply(_delete_data(actor_id=str(uuid.uuid4())))
    db = _FakeDb(payload)

    with pytest.raises(ValueError, match="does not match its command scope"):
        await DatasetUnlinkData(db).unlink(_command())

    assert db.commits == 0


@pytest.mark.asyncio
async def test_binding_preserves_owner_failure_code():
    db = _FakeDb(
        {
            "schema": "aap-dataset-unlink/v1",
            "ok": False,
            "error": {"code": "active_learning_reference"},
        }
    )

    with pytest.raises(DatasetUnlinkFailure, match="active_learning_reference"):
        await DatasetUnlinkData(db).unlink(_command())

    assert db.commits == 0


@pytest.mark.asyncio
async def test_preview_binding_is_read_only_and_validates_its_response():
    db = _FakeDb(
        {
            "schema": "aap-dataset-unlink-preview/v1",
            "ok": True,
            "data": {
                "will_delete_tasks": 5,
                "will_delete_annotations": 2,
                "will_delete_batches": 1,
            },
        }
    )

    preview = await DatasetUnlinkData(db).preview(
        actor_id=ACTOR_ID, dataset_id=DATASET_ID, project_id=PROJECT_ID
    )

    assert preview.will_delete_tasks == 5
    statement, params = db.events[0][1]
    assert "aap_workspace_v1.preview_dataset_unlink" in statement
    assert params == {"actor": ACTOR_ID, "dataset": DATASET_ID, "project": PROJECT_ID}
    assert db.commits == 0
