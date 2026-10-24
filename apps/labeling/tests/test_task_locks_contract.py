from uuid import uuid4

import pytest

from aap_labeling.task_locks_contract import (
    TaskLocksUnavailable,
    decode_task_locks_reply,
    task_locks_command,
)
from aap_labeling.task_locks_data import TaskLocksData


@pytest.mark.asyncio
async def test_batch_final_write_command_round_trips_json_statuses():
    batch_id = uuid4()
    actor_id = uuid4()
    seen = {}

    async def call(request):
        seen.update(request)
        return {
            "schema": "aap-labeling-task-locks-data/v1",
            "action": "batch_final_write_available",
            "ok": True,
            "data": {"available": True},
        }

    command = task_locks_command(
        "batch_final_write_available",
        batch_id=batch_id,
        actor_id=actor_id,
        allow_own=True,
        allowed_statuses=("pending", "in_progress"),
    )
    result = await TaskLocksData(call).execute(command)

    assert result == {"available": True}
    assert seen == {
        "schema": "aap-labeling-task-locks/v1",
        "action": "batch_final_write_available",
        "batch_id": str(batch_id),
        "actor_id": str(actor_id),
        "allow_own": True,
        "allowed_statuses": ["pending", "in_progress"],
    }

    read_command = task_locks_command("active_lock", task_id=batch_id)
    wrong_scope_reply = {
        "schema": "aap-labeling-task-locks-data/v1",
        "action": "active_lock",
        "ok": True,
        "data": {
            "lock": {
                "id": str(uuid4()),
                "task_id": str(actor_id),
                "user_id": str(actor_id),
                "expire_at": "2026-10-25T00:00:00+00:00",
                "unique_id": str(uuid4()),
                "created_at": "2026-10-25T00:00:00+00:00",
            },
            "deleted_ids": [],
        },
    }
    with pytest.raises(TaskLocksUnavailable):
        decode_task_locks_reply(read_command, wrong_scope_reply)
