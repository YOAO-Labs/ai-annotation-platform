"""Reject ambiguous or mismatched deletion receipts before caller commit."""

import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.application.project_deletion import _restore_failure
from app.services.project_deletion_contract import (
    ProjectDeletionFailure,
    ProjectDeletionReceipt,
    decode_project_deletion_reply,
)
from app.services.project_deletion_data import ProjectDeletionData
from fastapi import HTTPException


def _receipt():
    return {
        **{key: str(uuid4()) for key in ("operation_id", "actor_id", "project_id")},
        "deleted_tasks": 2,
        "deleted_annotations": 3,
        "audit_id": 1,
    }


@pytest.mark.parametrize(
    "changed",
    [
        {"schema": "aap-project-delete/v2"},
        {"ok": 1},
        {"error": {"code": "project_busy"}},
        {"extra": "private"},
    ],
)
def test_invalid_deletion_envelope_is_rejected(changed):
    with pytest.raises(ValidationError):
        decode_project_deletion_reply(
            {
                "schema": "aap-project-delete/v1",
                "ok": True,
                "data": _receipt(),
                **changed,
            }
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"deleted_tasks": -1},
        {"deleted_annotations": True},
        {"audit_id": 0},
        {"actor_email": "private@example.test"},
    ],
)
def test_deletion_receipt_rejects_invalid_counts_and_private_facts(changed):
    with pytest.raises(ValidationError):
        ProjectDeletionReceipt.model_validate({**_receipt(), **changed})


async def test_scope_mismatch_does_not_let_the_client_report_success():
    db = AsyncMock()
    db.scalar.return_value = {
        "schema": "aap-project-delete/v1",
        "ok": True,
        "data": _receipt(),
    }
    with pytest.raises(ValueError, match="command scope"):
        await ProjectDeletionData(db).delete(
            operation_id=uuid4(),
            actor_id=uuid4(),
            project_id=uuid4(),
        )
    db.commit.assert_not_called()


def test_receipt_binding_matches_frozen_public_fields():
    frozen = json.loads(
        (
            Path(__file__).parents[1] / "alembic/sql/0199_project_deletion.schema.json"
        ).read_text()
    )["$defs"]["receipt"]
    actual = ProjectDeletionReceipt.model_json_schema()
    assert set(frozen["properties"]) == set(actual["properties"])
    assert set(frozen["required"]) == set(actual["required"])
    assert actual["additionalProperties"] is frozen["additionalProperties"] is False


def test_retention_failure_preserves_public_active_learning_detail():
    with pytest.raises(HTTPException) as raised:
        _restore_failure(ProjectDeletionFailure("active_learning_reference"))
    assert raised.value.status_code == 409
    assert raised.value.detail == {
        "code": "active_learning_reference",
        "message": "主动学习历史仍引用此来源；未删除数据或存储对象",
    }
