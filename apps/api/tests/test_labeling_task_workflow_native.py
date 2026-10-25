"""One outer-transaction regression for the independent Labeling task owner."""

import os

import asyncpg
import pytest
from sqlalchemy import delete, text

from aap_labeling.task_workflow_contract import (
    TASK_WORKFLOW_PROTOCOL,
    RecordAnnotationActor,
)
from aap_labeling.task_workflow_data import LabelingTaskWorkflowData
from app.db.models.task import Task
from tests.factory import create_task
from tests.test_workspace_project_access_step_native import _cleanup, _seed
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned Labeling workflow capability",
    ),
]


async def test_contributor_write_uses_the_callers_commit_and_rollback(test_engine):
    maker, world = await _seed(test_engine)
    task_id = None
    connection = None
    try:
        async with maker.begin() as db:
            task = await create_task(
                db, project_id=world["project_id"], status="in_progress"
            )
            task_id = task.id
        dsn = _dsn(
            test_engine,
            "AAP_MODULE_DATA_LABELING_TASK_CONTROL_URL",
            "labeling_task_control_client",
        )
        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        assert (
            await connection.fetchval("SELECT current_user")
            == "labeling_task_control_client"
        )

        async def call(request_json):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.task_workflow_command($1::jsonb)",
                request_json,
            )

        data = LabelingTaskWorkflowData(call)
        request = RecordAnnotationActor(
            schema=TASK_WORKFLOW_PROTOCOL,
            task_id=task_id,
            project_id=world["project_id"],
            batch_id=None,
            actor_id=world["member_id"],
            expected_status="in_progress",
            expected_review_round_id=None,
            operation="record_annotation_actor",
        )
        transaction = connection.transaction()
        await transaction.start()
        reply = await data.command(request)
        assert reply.changed is True
        assert reply.task.annotation_contributor_ids == [str(world["member_id"])]
        await transaction.rollback()

        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT annotation_contributor_ids FROM public.tasks WHERE id=:id"
                    ),
                    {"id": task_id},
                )
                == []
            )

        transaction = connection.transaction()
        await transaction.start()
        reply = await data.command(request)
        assert reply.changed is True
        await transaction.commit()

        async with maker() as db:
            assert await db.scalar(
                text(
                    "SELECT annotation_contributor_ids FROM public.tasks WHERE id=:id"
                ),
                {"id": task_id},
            ) == [str(world["member_id"])]
    finally:
        if connection is not None:
            await connection.close()
        async with maker.begin() as db:
            if task_id is not None:
                await db.execute(delete(Task).where(Task.id == task_id))
        await _cleanup(maker, world)
