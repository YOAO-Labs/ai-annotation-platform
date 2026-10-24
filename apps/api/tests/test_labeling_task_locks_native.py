"""Exercise Labeling task locks through the fixed function-only LOGIN."""

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.task_locks_contract import task_locks_command
from aap_labeling.task_locks_data import TaskLocksData
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.task_lock import TaskLock
from tests.factory import create_project, create_task, create_user
from tests.test_labeling_class_usage_reads_legacy import _cleanup_owned
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data boundary verifier",
    ),
]

_TASK_LOCK_FUNCTIONS = {
    ("task_edit_lock_command", "jsonb"),
    ("task_workflow_command", "jsonb"),
}


def _task_control_dsn(test_engine):
    return _dsn(
        test_engine,
        "AAP_MODULE_DATA_LABELING_TASK_CONTROL_URL",
        "labeling_task_control_client",
    )


def _data(connection):
    async def call(request):
        return await connection.fetchval(
            "SELECT aap_labeling_v1.task_edit_lock_command($1::jsonb)",
            json.dumps(request, ensure_ascii=True, allow_nan=False),
        )

    return TaskLocksData(call)


async def test_function_only_task_lock_client_keeps_scope_locks_in_caller_transaction(
    test_engine,
):
    dsn = _task_control_dsn(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids, project_ids, task_ids = [], [], []
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"task-lock-native-{uuid.uuid4().hex}@example.test",
                "Task lock native owner",
            )
            actor_ids.append(actor.id)
            project = await create_project(db, owner_id=actor.id)
            project_ids.append(project.id)
            task = await create_task(db, project_id=project.id)
            task_ids.append(task.id)

        first = await asyncpg.connect(dsn)
        second = await asyncpg.connect(dsn)
        try:
            await verify_function_only_role(
                first,
                schema="aap_labeling_v1",
                functions=_TASK_LOCK_FUNCTIONS,
            )
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await first.fetchval("SELECT id FROM public.task_locks LIMIT 1")

            async with first.transaction():
                await _data(first).execute(
                    task_locks_command("lock_scope", task_id=task.id)
                )
                async with second.transaction():
                    result = await _data(second).execute(
                        task_locks_command("try_final_scope", task_id=task.id)
                    )
                    assert result == {"available": False}

            async with first.transaction():
                data = _data(first)
                acquired = await data.execute(
                    task_locks_command(
                        "acquire",
                        task_id=task.id,
                        user_id=actor.id,
                        ttl=None,
                        force_takeover=False,
                    )
                )
                assert acquired["lock"] is not None
                active = await data.execute(
                    task_locks_command("active_locks", task_id=task.id)
                )
                assert len(active["locks"]) == 1
                assert (
                    await data.execute(
                        task_locks_command(
                            "heartbeat", task_id=task.id, user_id=actor.id, ttl=None
                        )
                    )
                )["renewed"]
                assert (
                    await data.execute(
                        task_locks_command("release", task_id=task.id, user_id=actor.id)
                    )
                )["released"]
        finally:
            await first.close()
            await second.close()
    finally:
        if task_ids:
            async with maker.begin() as db:
                await db.execute(delete(TaskLock).where(TaskLock.task_id.in_(task_ids)))
        await _cleanup_owned(
            maker, actor_ids=actor_ids, project_ids=project_ids, task_ids=task_ids
        )
