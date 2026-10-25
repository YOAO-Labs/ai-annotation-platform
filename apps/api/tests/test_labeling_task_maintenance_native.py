"""Exercise source-less maintenance through the restricted API LOGIN."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import event, text

from app.application.project_task_cleanup import delete_source_less_tasks
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from tests.test_workspace_project_authority_locking import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier",
    ),
]


async def test_source_less_cleanup_uses_labeling_owner_and_preserves_uow(test_engine):
    async with _world(test_engine) as (owner, api, world, _url):
        batch_id = uuid.uuid4()
        task_id = uuid.uuid4()
        async with owner.begin() as db:
            db.add(
                TaskBatch(
                    id=batch_id,
                    project_id=world["project_id"],
                    display_id=f"B-MAINT-{batch_id.hex[:10]}",
                    name="maintenance",
                    status="active",
                    total_tasks=7,
                    completed_tasks=4,
                    review_tasks=2,
                )
            )
            await db.flush()
            db.add(
                Task(
                    id=task_id,
                    project_id=world["project_id"],
                    batch_id=batch_id,
                    display_id=f"T-MAINT-{task_id.hex[:10]}",
                    file_name="orphan.png",
                    file_path=f"orphan/{task_id}.png",
                    file_type="image",
                    status="completed",
                )
            )

        statements = []
        engine = None
        listener_installed = False

        def capture(conn, cursor, statement, parameters, context, many):
            statements.append(statement.lower())

        try:
            async with api.begin() as db:
                connection = await db.connection()
                engine = connection.sync_connection.engine
                event.listen(engine, "before_cursor_execute", capture)
                listener_installed = True
                assert (
                    await db.scalar(text("SELECT current_user"))
                    == "identity_api_consumer"
                )
                assert await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user, "
                        "'aap_labeling_v1.task_maintenance_command(jsonb)', 'EXECUTE')"
                    )
                )
                result = await delete_source_less_tasks(db, world["project_id"])
                assert result == {"deleted_tasks": 1, "deleted_annotations": 0}
        finally:
            if engine is not None and listener_installed:
                event.remove(engine, "before_cursor_execute", capture)

        assert any(
            "aap_labeling_v1.task_maintenance_command" in sql for sql in statements
        )
        assert not any(
            sql.lstrip().startswith(
                (
                    "delete from tasks",
                    "delete from annotations",
                    "delete from annotation_drafts",
                    "delete from task_locks",
                    "update annotations",
                    "update task_batches",
                )
            )
            for sql in statements
        )
        async with owner() as db:
            assert await db.get(Task, task_id) is None
            batch = await db.get(TaskBatch, batch_id)
            assert batch is not None
            assert (batch.total_tasks, batch.completed_tasks, batch.review_tasks) == (
                0,
                0,
                0,
            )
