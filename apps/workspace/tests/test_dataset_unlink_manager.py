from __future__ import annotations

import uuid

import pytest

from aap_workspace.dataset_unlink import DatasetUnlinkManager
from aap_workspace.dataset_unlink_contract import (
    DATASET_UNLINK_PREVIEW_PROTOCOL,
    DATASET_UNLINK_PROTOCOL,
    DatasetUnlinkFailure,
    DatasetUnlinkPreview,
    DatasetUnlinkReceipt,
)

_OPERATION_ID = uuid.UUID("c15bb44c-9032-407d-9c8b-f7a39c6e4984")
_ACTOR_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_DATASET_ID = uuid.UUID("23b973f5-a1fa-4973-bde0-c61d36c93e8e")
_PROJECT_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")
_ASSOCIATION_ID = uuid.UUID("c71a1997-c3c5-457c-8cf3-53be3f7cf207")
_BATCH_ID = uuid.UUID("5d49ffae-67f8-4893-8c75-86df553cc772")
_CLEANUP_BUCKET = "media-cache-test"


def _command(**changes):
    return {
        "operation_id": str(_OPERATION_ID),
        "actor_id": str(_ACTOR_ID),
        "dataset_id": str(_DATASET_ID),
        "project_id": str(_PROJECT_ID),
        "cleanup_bucket": _CLEANUP_BUCKET,
        "audit_context": None,
        **changes,
    }


def _receipt(**changes):
    return {
        "operation_id": str(_OPERATION_ID),
        "actor_id": str(_ACTOR_ID),
        "dataset_id": str(_DATASET_ID),
        "project_id": str(_PROJECT_ID),
        "association_id": str(_ASSOCIATION_ID),
        "cleanup_bucket": _CLEANUP_BUCKET,
        "audit_id": 731,
        "deleted_tasks": 4,
        "deleted_annotations": 3,
        "deleted_batches": 1,
        "deleted_batch_ids": [str(_BATCH_ID)],
        "soft": False,
        **changes,
    }


class BorrowedData:
    def __init__(self, unlink_reply=None, preview_reply=None, error=None):
        self.unlink_reply = unlink_reply
        self.preview_reply = preview_reply
        self.error = error
        self.commands = []
        self.previews = []
        self.events = []

    async def unlink_dataset(self, command):
        self.events.append("unlink")
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.unlink_reply

    async def preview_dataset_unlink(self, command):
        self.events.append("preview")
        self.previews.append(command)
        if self.error is not None:
            raise self.error
        return self.preview_reply


@pytest.mark.asyncio
async def test_manager_validates_unlink_command_and_binds_receipt_scope():
    reply = {
        "schema": DATASET_UNLINK_PROTOCOL,
        "ok": True,
        "data": _receipt(),
    }
    data = BorrowedData(unlink_reply=reply)

    receipt = await DatasetUnlinkManager(data).execute(_command())

    assert isinstance(receipt, DatasetUnlinkReceipt)
    assert receipt.operation_id == _OPERATION_ID
    assert receipt.cleanup_bucket == _CLEANUP_BUCKET
    assert data.commands[0].audit_context is None
    assert data.events == ["unlink"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatched",
    ["operation_id", "actor_id", "dataset_id", "project_id", "cleanup_bucket"],
)
async def test_manager_rejects_receipt_bound_to_different_command_scope(mismatched):
    changed = {
        "operation_id": str(uuid.uuid4()),
        "actor_id": str(uuid.uuid4()),
        "dataset_id": str(uuid.uuid4()),
        "project_id": str(uuid.uuid4()),
        "cleanup_bucket": "other-media-cache",
    }[mismatched]
    reply = {
        "schema": DATASET_UNLINK_PROTOCOL,
        "ok": True,
        "data": _receipt(**{mismatched: changed}),
    }

    with pytest.raises(ValueError, match="does not match its command scope"):
        await DatasetUnlinkManager(BorrowedData(unlink_reply=reply)).execute(_command())


@pytest.mark.asyncio
async def test_manager_preview_uses_the_existing_preview_command_and_protocol():
    reply = {
        "schema": DATASET_UNLINK_PREVIEW_PROTOCOL,
        "ok": True,
        "data": {
            "will_delete_tasks": 2,
            "will_delete_annotations": 1,
            "will_delete_batches": 1,
        },
    }
    data = BorrowedData(preview_reply=reply)

    preview = await DatasetUnlinkManager(data).preview(
        {
            "actor_id": str(_ACTOR_ID),
            "dataset_id": str(_DATASET_ID),
            "project_id": str(_PROJECT_ID),
        }
    )

    assert isinstance(preview, DatasetUnlinkPreview)
    assert preview.will_delete_tasks == 2
    assert data.previews[0].actor_id == _ACTOR_ID
    assert data.events == ["preview"]


@pytest.mark.asyncio
async def test_manager_preserves_open_failures_and_data_errors_without_owning_transaction():
    failure = {
        "schema": DATASET_UNLINK_PROTOCOL,
        "ok": False,
        "error": {"code": "retained_video_execution_record"},
    }
    data = BorrowedData(unlink_reply=failure)
    with pytest.raises(DatasetUnlinkFailure) as raised:
        await DatasetUnlinkManager(data).execute(_command())
    assert raised.value.code == "retained_video_execution_record"
    assert not {"commit", "rollback", "close", "begin"}.intersection(data.events)

    error = RuntimeError("outer transaction rejected")
    data = BorrowedData(error=error)
    with pytest.raises(RuntimeError) as raised:
        await DatasetUnlinkManager(data).execute(_command())
    assert raised.value is error
    assert data.events == ["unlink"]


@pytest.mark.asyncio
async def test_manager_revalidates_forged_model_before_calling_data():
    from aap_workspace.dataset_unlink_contract import decode_unlink_command

    forged = decode_unlink_command(_command()).model_copy(
        update={"cleanup_bucket": "invalid bucket"}
    )
    data = BorrowedData()
    with pytest.raises(ValueError):
        await DatasetUnlinkManager(data).execute(forged)
    assert data.events == []
