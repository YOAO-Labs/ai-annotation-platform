"""Caller-transaction and lock regressions for the Labeling task owner."""

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import delete, text

from yoao_labeling.task_workflow_contract import (
    TASK_WORKFLOW_PROTOCOL,
    ClearReviewEvidence,
    RecordAnnotationActor,
    TaskWorkflowFailure,
)
from yoao_labeling.task_workflow_data import LabelingTaskWorkflowData
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from tests.factory import create_batch, create_task
from tests.test_workspace_project_access_step_native import _cleanup, _owner_dsn, _seed
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


async def test_batched_contributor_lock_compatibility_and_fences(test_engine):
    maker, world = await _seed(test_engine)
    task_ids = []
    batch_ids = []
    owner = None
    client = None
    peer = None
    try:
        async with maker.begin() as db:
            batch = await create_batch(db, project_id=world["project_id"])
            other_batch = await create_batch(db, project_id=world["project_id"])
            batch_ids.extend((batch.id, other_batch.id))
            target = await create_task(
                db, project_id=world["project_id"], status="in_progress"
            )
            neighbor = await create_task(
                db, project_id=world["project_id"], status="in_progress"
            )
            independent = await create_task(
                db, project_id=world["project_id"], status="in_progress"
            )
            task_ids.extend((target.id, neighbor.id, independent.id))
            independent.batch_id = other_batch.id
            target.batch_id = neighbor.batch_id = batch.id

        owner = await asyncpg.connect(
            _owner_dsn(test_engine), timeout=5, command_timeout=10
        )
        dsn = _dsn(
            test_engine,
            "AAP_MODULE_DATA_LABELING_TASK_CONTROL_URL",
            "labeling_task_control_client",
        )
        client = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        peer = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        assert (
            await client.fetchval("SELECT current_user")
            == "labeling_task_control_client"
        )
        assert not await client.fetchval(
            "SELECT has_table_privilege(current_user, 'public.task_batches', 'SELECT')"
        )

        async def call(request_json):
            return await client.fetchval(
                "SELECT aap_labeling_v1.task_workflow_command($1::jsonb)",
                request_json,
            )

        data = LabelingTaskWorkflowData(call)

        async def peer_call(request_json):
            return await peer.fetchval(
                "SELECT aap_labeling_v1.task_workflow_command($1::jsonb)",
                request_json,
            )

        peer_data = LabelingTaskWorkflowData(peer_call)
        scope = dict(
            schema=TASK_WORKFLOW_PROTOCOL,
            task_id=target.id,
            project_id=world["project_id"],
            batch_id=batch.id,
            actor_id=world["member_id"],
            expected_status="in_progress",
            expected_review_round_id=None,
        )
        request = RecordAnnotationActor(**scope, operation="record_annotation_actor")
        other = ClearReviewEvidence(**scope, operation="clear_review_evidence")

        async with owner.transaction():
            # Hold the neighboring Task's batch exactly as an original-authority reader does.
            assert (
                await owner.fetchval(
                    "SELECT batch.id FROM public.tasks AS task "
                    "JOIN public.task_batches AS batch ON batch.id = task.batch_id "
                    "WHERE task.id = $1 FOR SHARE OF batch NOWAIT",
                    neighbor.id,
                )
                == batch.id
            )
            tx = client.transaction()
            await tx.start()
            try:
                reply = await data.command(request)
                assert reply.changed is True
                assert reply.task.annotation_contributor_ids == [
                    str(world["member_id"])
                ]
                # Two SHARE holders must not proceed to competing Batch upgrades.
                with pytest.raises(asyncpg.LockNotAvailableError) as caught:
                    await peer_data.command(
                        request.model_copy(update={"task_id": neighbor.id})
                    )
                assert caught.value.sqlstate == "55P03"
                independent_reply = await peer_data.command(
                    request.model_copy(
                        update={"task_id": independent.id, "batch_id": other_batch.id}
                    )
                )
                assert independent_reply.changed is True
                assert independent_reply.task.annotation_contributor_ids == [
                    str(world["member_id"])
                ]
            finally:
                await tx.rollback()
            assert (
                await owner.fetchval(
                    "SELECT annotation_contributor_ids FROM public.tasks WHERE id = $1",
                    target.id,
                )
                == "[]"
            )
            # Rollback releases the same-batch writer exclusion, not the reader.
            neighbor_reply = await peer_data.command(
                request.model_copy(update={"task_id": neighbor.id})
            )
            assert neighbor_reply.changed is True
            async with client.transaction():
                reply = await data.command(request)
                assert reply.changed is True
            assert (
                await owner.fetchval(
                    "SELECT annotation_contributor_ids FROM public.tasks WHERE id = $1",
                    target.id,
                )
                == f'["{world["member_id"]}"]'
            )
            # Commit also releases the exclusion; an identical write is idempotent.
            assert (await peer_data.command(request)).changed is False
            with pytest.raises(asyncpg.LockNotAvailableError) as caught:
                await data.command(other)
            assert caught.value.sqlstate == "55P03"

        async with owner.transaction():
            await owner.fetchval(
                "SELECT id FROM public.task_batches WHERE id = $1 FOR UPDATE", batch.id
            )
            with pytest.raises(asyncpg.LockNotAvailableError) as caught:
                await data.command(request)
            assert caught.value.sqlstate == "55P03"

        async with owner.transaction():
            await owner.fetchval(
                "SELECT id FROM public.tasks WHERE id = $1 FOR UPDATE", target.id
            )
            with pytest.raises(asyncpg.LockNotAvailableError) as caught:
                await data.command(request)
            assert caught.value.sqlstate == "55P03"

        await owner.execute(
            "UPDATE public.users SET is_active = false WHERE id = $1",
            world["member_id"],
        )
        with pytest.raises(TaskWorkflowFailure) as caught:
            await data.command(request)
        assert caught.value.code == "actor_inactive"
        await owner.execute(
            "UPDATE public.users SET is_active = true WHERE id = $1",
            world["member_id"],
        )
        for stale in (
            request.model_copy(update={"expected_status": "pending"}),
            request.model_copy(update={"expected_review_round_id": uuid.uuid4()}),
        ):
            with pytest.raises(TaskWorkflowFailure) as caught:
                await data.command(stale)
            assert caught.value.code == "task_state_changed"
        await owner.execute(
            "UPDATE public.tasks SET batch_id = $1 WHERE id = $2",
            other_batch.id,
            target.id,
        )
        with pytest.raises(TaskWorkflowFailure) as caught:
            await data.command(request)
        assert caught.value.code == "task_target_changed"
        assert (
            await owner.fetchval(
                "SELECT annotation_contributor_ids FROM public.tasks WHERE id = $1",
                target.id,
            )
            == f'["{world["member_id"]}"]'
        )
    finally:
        if peer is not None:
            await peer.close()
        if client is not None:
            await client.close()
        if owner is not None:
            await owner.close()
        async with maker.begin() as db:
            if task_ids:
                await db.execute(delete(Task).where(Task.id.in_(task_ids)))
            if batch_ids:
                await db.execute(delete(TaskBatch).where(TaskBatch.id.in_(batch_ids)))
        await _cleanup(maker, world)
