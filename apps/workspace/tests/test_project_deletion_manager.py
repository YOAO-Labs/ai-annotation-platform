from __future__ import annotations

import pytest
import uuid

from aap_workspace.project_deletion import ProjectDeletionManager
from aap_workspace.project_deletion_contract import (
    PROJECT_DELETION_PROTOCOL,
    ProjectDeletionCommand,
    ProjectDeletionFailure,
    ProjectDeletionReceipt,
)

_OPERATION_ID = uuid.UUID("c15bb44c-9032-407d-9c8b-f7a39c6e4984")
_ACTOR_ID = uuid.UUID("4c524267-d44b-4419-9dcb-1afd9630d80a")
_PROJECT_ID = uuid.UUID("705ffe3a-dddc-4d15-8bb8-870f88af6586")


def _command():
    return ProjectDeletionCommand(
        operation_id=_OPERATION_ID,
        actor_id=_ACTOR_ID,
        project_id=_PROJECT_ID,
        audit_context=None,
    )


def _reply(**ids):
    return {
        "schema": PROJECT_DELETION_PROTOCOL,
        "ok": True,
        "data": {
            "operation_id": str(ids.get("operation_id", _OPERATION_ID)),
            "actor_id": str(ids.get("actor_id", _ACTOR_ID)),
            "project_id": str(ids.get("project_id", _PROJECT_ID)),
            "deleted_tasks": 2,
            "deleted_annotations": 3,
            "audit_id": 1,
        },
    }


class BorrowedData:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.commands = []
        self.events = []

    async def delete_project(self, command):
        self.events.append("call")
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.mark.asyncio
async def test_manager_validates_command_and_binds_success_receipt_scope():
    data = BorrowedData(_reply())
    receipt = await ProjectDeletionManager(data).execute(_command())

    assert isinstance(receipt, ProjectDeletionReceipt)
    assert receipt.operation_id == _OPERATION_ID
    assert receipt.actor_id == _ACTOR_ID
    assert receipt.project_id == _PROJECT_ID
    assert data.commands == [_command()]
    assert data.events == ["call"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatched", ["operation_id", "actor_id", "project_id"])
async def test_manager_rejects_receipt_bound_to_a_different_command_scope(mismatched):
    data = BorrowedData(_reply(**{mismatched: uuid.uuid4()}))

    with pytest.raises(
        ValueError, match="Deletion receipt does not match its command scope"
    ):
        await ProjectDeletionManager(data).execute(_command())


@pytest.mark.asyncio
async def test_manager_preserves_legacy_failure_code_without_transaction_ownership():
    data = BorrowedData(
        {
            "schema": PROJECT_DELETION_PROTOCOL,
            "ok": False,
            "error": {"code": "project_manager_required"},
        }
    )

    with pytest.raises(ProjectDeletionFailure) as raised:
        await ProjectDeletionManager(data).execute(_command())

    assert raised.value.code == "project_manager_required"
    assert data.events == ["call"]


@pytest.mark.asyncio
async def test_invalid_or_forged_command_is_rejected_before_data_call():
    data = BorrowedData(_reply())
    malformed = _command().model_copy(update={"audit_context": {"unknown": True}})

    with pytest.raises(ValueError):
        await ProjectDeletionManager(data).execute(malformed)
    assert data.events == []


@pytest.mark.asyncio
async def test_manager_does_not_catch_data_failures_or_own_rollback():
    error = RuntimeError("outer transaction failed")
    data = BorrowedData(error=error)

    with pytest.raises(RuntimeError) as raised:
        await ProjectDeletionManager(data).execute(_command())

    assert raised.value is error
    assert data.events == ["call"]
    assert not {"commit", "rollback", "close", "begin"}.intersection(data.events)
