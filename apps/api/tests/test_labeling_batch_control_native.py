"""Exercise Batch cleanup through its function-only Labeling capability."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, text

from aap_labeling.batch_control_contract import PrepareBatchCleanup
from aap_labeling.batch_control_data import LabelingBatchControlData
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from tests.factory import create_task
from tests.test_workspace_project_access_step_native import _cleanup, _seed
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned Labeling batch-control capability",
    ),
]


async def test_batch_cleanup_preserves_json_null_and_caller_transaction(test_engine):
    maker, world = await _seed(test_engine)
    batch_id = uuid.uuid4()
    task_id = None
    connection = None
    try:
        async with maker.begin() as db:
            batch = TaskBatch(
                id=batch_id,
                project_id=world["project_id"],
                display_id=f"BT-native-{batch_id.hex[:12]}",
                name="Batch cleanup native",
                status="reviewing",
                created_by=world["member_id"],
            )
            db.add(batch)
            await db.flush()
            task = await create_task(
                db, project_id=world["project_id"], status="review"
            )
            task.batch_id = batch_id
            task.review_contributor_ids = [str(world["member_id"])]
            task.review_submitter_id = world["member_id"]
            task_id = task.id
            db.add(
                TaskLock(
                    task_id=task.id,
                    user_id=world["member_id"],
                    expire_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    unique_id=uuid.uuid4(),
                )
            )

        connection = await asyncpg.connect(
            _dsn(
                test_engine,
                "AAP_MODULE_DATA_LABELING_BATCH_CONTROL_URL",
                "labeling_batch_control_client",
            ),
            timeout=5,
            command_timeout=10,
        )
        assert (
            await connection.fetchval("SELECT current_user")
            == "labeling_batch_control_client"
        )
        await verify_function_only_role(
            connection,
            schema="aap_labeling_v1",
            functions={("batch_control_command", "jsonb")},
            role="labeling_batch_control_client",
        )

        async def call(request_json):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.batch_control_command($1::jsonb)",
                request_json,
            )

        data = LabelingBatchControlData(call)
        request = PrepareBatchCleanup(
            schema="aap-labeling-batch-control/v1",
            operation="prepare_batch_cleanup",
            batch_id=batch_id,
        )
        transaction = connection.transaction()
        await transaction.start()
        rolled_back = await data.command(request)
        assert rolled_back.tasks_reset == 1
        assert rolled_back.task_ids == [task_id]
        assert (
            await connection.fetchval(
                "SELECT jsonb_typeof(review_contributor_ids) FROM public.tasks WHERE id=$1",
                task_id,
            )
            == "null"
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM public.task_locks WHERE task_id=$1", task_id
            )
            == 0
        )
        await transaction.rollback()

        async with maker() as db:
            assert (await db.get(Task, task_id)).status == "review"
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM public.task_locks WHERE task_id=:id"),
                    {"id": task_id},
                )
                == 1
            )

        async with connection.transaction():
            committed = await data.command(request)
            assert committed.tasks_reset == 1

        async with maker() as db:
            task_after_commit = await db.get(Task, task_id)
            assert task_after_commit is not None
            assert task_after_commit.status == "pending"
            assert task_after_commit.review_contributor_ids is None
            assert (
                await db.scalar(
                    text(
                        "SELECT jsonb_typeof(review_contributor_ids) FROM public.tasks WHERE id=:id"
                    ),
                    {"id": task_id},
                )
                == "null"
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM public.task_locks WHERE task_id=:id"),
                    {"id": task_id},
                )
                == 0
            )
    finally:
        if connection is not None:
            await connection.close()
        async with maker.begin() as db:
            if task_id is not None:
                await db.execute(delete(TaskLock).where(TaskLock.task_id == task_id))
                await db.execute(delete(Task).where(Task.id == task_id))
            await db.execute(delete(TaskBatch).where(TaskBatch.id == batch_id))
        await _cleanup(maker, world)
