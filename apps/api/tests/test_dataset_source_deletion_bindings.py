"""Deletion bindings reject malformed and mismatched owner receipts."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.dataset_source_deletion_contract import (
    DatasetSourceDeletionCommand,
    DatasetSourceDeletionFailure,
    decode_source_deletion_reply,
)
from app.services.dataset_source_deletion_data import DatasetSourceDeletionData


def _command():
    return DatasetSourceDeletionCommand(
        operation_id=uuid4(),
        actor_id=uuid4(),
        dataset_id=uuid4(),
        cleanup_bucket="media-cache",
    )


def _reply(command):
    return {
        "schema": "aap-dataset-source-delete/v1",
        "ok": True,
        "data": {
            **command.model_dump(mode="json", exclude={"audit_context"}),
            "audit_id": 1,
            "deleted_datasets": 1,
            "deleted_items": 2,
            "deleted_scenes": 0,
            "deleted_associations": 0,
            "discarded_uploads": 0,
            "queued_storage_deletions": 1,
            "cancelled_creation_jobs": 0,
        },
    }


@pytest.mark.asyncio
async def test_binding_flushes_without_committing_and_checks_frozen_scope():
    command = _command()
    db = AsyncMock()
    db.scalar.return_value = _reply(command)
    receipt = await DatasetSourceDeletionData(db).delete(command)
    assert receipt.deleted_items == 2
    db.flush.assert_awaited_once()
    db.commit.assert_not_called()
    db.scalar.return_value["data"]["dataset_id"] = str(uuid4())
    with pytest.raises(ValueError, match="scope"):
        await DatasetSourceDeletionData(db).delete(command)


@pytest.mark.parametrize("bad", ["ok", "extra", "count", "branch", "scope"])
def test_contract_rejects_ambiguous_or_invalid_receipts(bad):
    payload = _reply(_command())
    if bad == "ok":
        payload["ok"] = 1
    elif bad == "extra":
        payload["data"]["private_path"] = "elsewhere"
    elif bad == "count":
        payload["data"]["queued_storage_deletions"] = True
    elif bad == "branch":
        payload["error"] = {"code": "source_busy"}
    else:
        payload["data"]["item_id"] = str(uuid4())
    with pytest.raises(ValidationError):
        decode_source_deletion_reply(payload)


def test_contract_preserves_a_stable_retention_failure():
    with pytest.raises(DatasetSourceDeletionFailure) as error:
        decode_source_deletion_reply(
            {
                "schema": "aap-dataset-source-delete/v1",
                "ok": False,
                "error": {"code": "active_learning_reference"},
            }
        )
    assert error.value.code == "active_learning_reference"
