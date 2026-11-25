import json
import re
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest

from yoao_labeling.task_locks_contract import (
    TASK_LOCK_SCOPE_PREFIX,
    TaskLocksUnavailable,
    decode_task_locks_reply,
    task_locks_command,
    task_locks_schema,
)
from yoao_labeling.task_locks_data import TaskLocksData

_LEGACY_NAMESPACE = re.compile(r"\baap_(?=[a-z0-9_]*_v[0-9]+\b)")


def _current_names(value):
    """Compare frozen artifacts through the current module namespace names."""
    return json.loads(_LEGACY_NAMESPACE.sub("yoao_", json.dumps(value)))


def test_task_locks_use_the_current_advisory_domain():
    repository = Path(__file__).resolve().parents[3]
    schema = task_locks_schema()
    assert TASK_LOCK_SCOPE_PREFIX == "task-edit-lock:"
    assert (
        schema["x-postgresql-functions"][
            "yoao_labeling_v1.task_edit_lock_command(jsonb)"
        ]["task_scope_key"]
        == "task-edit-lock:<task UUID>"
    )
    assert (
        json.loads(
            files("yoao_labeling")
            .joinpath("task-locks.schema.json")
            .read_text(encoding="utf-8")
        )
        == schema
    )
    assert (
        _current_names(
            json.loads(
                (repository / "apps/api/alembic/sql/labeling.contracts.json").read_text(
                    encoding="utf-8"
                )
            )["artifacts"]["0466_task_edit_lock_domain.schema.json"]
        )
        == schema
    )
    assert (
        repository / "apps/api/alembic/sql/0466_task_edit_lock_domain.sql"
    ).is_file()


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
