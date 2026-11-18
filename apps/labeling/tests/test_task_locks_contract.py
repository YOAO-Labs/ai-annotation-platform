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


def test_task_locks_use_the_current_advisory_domain():
    repository = Path(__file__).resolve().parents[3]
    schema = task_locks_schema()
    assert TASK_LOCK_SCOPE_PREFIX == "task-edit-lock:"
    assert (
        schema["x-postgresql-functions"][
            "aap_labeling_v1.task_edit_lock_command(jsonb)"
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
        json.loads(
            files("yoao_labeling")
            .joinpath("sql/0466_task_edit_lock_domain.schema.json")
            .read_text(encoding="utf-8")
        )
        == schema
    )
    assert (
        json.loads(
            (
                repository
                / "apps/api/alembic/sql/0466_task_edit_lock_domain.schema.json"
            ).read_text(encoding="utf-8")
        )
        == schema
    )
    packaged_sql = (
        files("yoao_labeling")
        .joinpath("sql/0466_task_edit_lock_domain.sql")
        .read_text(encoding="utf-8")
    )
    migration_sql = (
        repository / "apps/api/alembic/sql/0466_task_edit_lock_domain.sql"
    ).read_text(encoding="utf-8")
    assert packaged_sql == migration_sql
    assert not re.search(
        r"pg_(?:try_)?advisory_xact_lock(?:_shared)?\(\s*"
        r"(?:pg_catalog\.)?hashtextextended\(\s*'aap:task-edit-lock:",
        packaged_sql,
    )
    assert "hashtextextended('task-edit-lock:'" in packaged_sql


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
