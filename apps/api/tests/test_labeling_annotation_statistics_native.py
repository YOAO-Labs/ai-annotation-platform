"""Exercise Annotation-derived Task statistics through the restricted LOGIN."""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.annotation_statistics_data import LabelingAnnotationStatisticsData
from aap_owner_data.postgres import verify_function_only_role
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from tests.factory import create_project, create_task, create_user
from tests.test_labeling_class_usage_reads_legacy import (
    _cleanup_owned,
    _create_annotation,
)
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_ANNOTATION_WRITE_FUNCTIONS = {
    ("annotation_statistics_command", "jsonb"),
    ("scene_track_domain_command", "jsonb"),
    ("annotation_write_command", "jsonb"),
}


def _annotation_write_dsn(test_engine):
    return _dsn(
        test_engine,
        "AAP_MODULE_DATA_LABELING_ANNOTATION_WRITE_URL",
        "labeling_annotation_write_client",
    )


def _data(connection):
    async def execute(request_json):
        return await connection.fetchval(
            "SELECT aap_labeling_v1.annotation_statistics_command($1::jsonb)",
            request_json,
        )

    return LabelingAnnotationStatisticsData(execute)


async def test_function_only_statistics_recompute_preserves_caller_transaction(
    test_engine,
):
    dsn = _annotation_write_dsn(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids, project_ids, task_ids = [], [], []
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"annotation-statistics-{uuid.uuid4().hex}@example.test",
                "Annotation statistics owner",
            )
            actor_ids.append(actor.id)
            project = await create_project(db, owner_id=actor.id)
            project_ids.append(project.id)
            task = await create_task(db, project_id=project.id)
            task_ids.append(task.id)
            active = await _create_annotation(
                db,
                project_id=project.id,
                task_id=task.id,
                user_id=actor.id,
                class_name="active",
                attributes={},
            )
            await _create_annotation(
                db,
                project_id=project.id,
                task_id=task.id,
                user_id=actor.id,
                class_name="cancelled",
                attributes={},
                cancelled=True,
            )
            await _create_annotation(
                db,
                project_id=project.id,
                task_id=task.id,
                user_id=actor.id,
                class_name="inactive",
                attributes={},
                active=False,
            )

        connection = await asyncpg.connect(dsn)
        try:
            await verify_function_only_role(
                connection,
                schema="aap_labeling_v1",
                functions=_ANNOTATION_WRITE_FUNCTIONS,
            )
            for statement in (
                "SELECT id FROM public.annotations LIMIT 1",
                "SELECT id FROM public.tasks LIMIT 1",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.fetch(statement)

            transaction = connection.transaction()
            await transaction.start()
            rolled_back = await _data(connection).recompute_task(task.id)
            assert rolled_back.total_annotations == 1
            assert rolled_back.is_labeled is True
            assert rolled_back.status == "in_progress"
            assert rolled_back.status_changed is True
            await transaction.rollback()

            async with maker() as db:
                task_after_rollback = await db.get(Task, task.id)
                assert task_after_rollback is not None
                assert task_after_rollback.status == "pending"
                assert task_after_rollback.total_annotations == 0

            async with connection.transaction():
                committed = await _data(connection).recompute_task(task.id)
                assert committed.total_annotations == 1
                assert committed.is_labeled is True
                assert committed.status == "in_progress"
                assert committed.status_changed is True

            async with maker() as db:
                task_after_commit = await db.get(Task, task.id)
                assert task_after_commit is not None
                assert task_after_commit.status == "in_progress"
                assert task_after_commit.total_annotations == 1
                original_updated_at = task_after_commit.updated_at

            async with connection.transaction():
                unchanged = await _data(connection).recompute_task(task.id)
                assert unchanged.status_changed is False
                assert unchanged.updated_at == original_updated_at

            async with maker.begin() as db:
                annotation = await db.get(Annotation, active.id)
                assert annotation is not None
                annotation.is_active = False

            async with connection.transaction():
                cleared = await _data(connection).recompute_task(task.id)
                assert cleared.total_annotations == 0
                assert cleared.is_labeled is False
                assert cleared.status == "pending"
                assert cleared.status_changed is True

            async with maker() as db:
                task_after_clear = await db.scalar(
                    select(Task).where(Task.id == task.id)
                )
                assert task_after_clear is not None
                assert task_after_clear.status == "pending"
                assert task_after_clear.total_annotations == 0
                assert task_after_clear.is_labeled is False
        finally:
            await connection.close()
    finally:
        await _cleanup_owned(
            maker, actor_ids=actor_ids, project_ids=project_ids, task_ids=task_ids
        )
