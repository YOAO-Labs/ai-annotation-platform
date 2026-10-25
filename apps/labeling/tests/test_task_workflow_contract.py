"""Focused pure checks for the closed Labeling workflow command contract."""

import json
from datetime import datetime, timezone
import uuid

import pytest

from aap_labeling.task_workflow_contract import (
    TASK_WORKFLOW_PROTOCOL,
    SubmitTask,
    TaskWorkflowUnavailable,
    decode_task_workflow_command,
    task_workflow_command_json,
)


def test_submit_command_is_closed_and_carries_no_authority_claim():
    command = SubmitTask(
        schema=TASK_WORKFLOW_PROTOCOL,
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        batch_id=None,
        actor_id=uuid.uuid4(),
        expected_status="in_progress",
        expected_review_round_id=None,
        operation="submit",
        at=datetime.now(timezone.utc),
    )

    payload = json.loads(task_workflow_command_json(command))
    assert payload["operation"] == "submit"
    assert "is_manager" not in payload
    payload["is_manager"] = True
    with pytest.raises(TaskWorkflowUnavailable):
        decode_task_workflow_command(payload)
