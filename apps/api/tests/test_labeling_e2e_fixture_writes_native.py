"""Exercise the test-only fixture capability with private Labeling DML denied."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import event, text

from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.labeling_e2e_fixture_writes import LabelingE2EFixtureWrites
from app.services.workspace_e2e_fixtures import create_fixture_project
from tests.factory import create_user
from tests.test_workspace_consumer_profile_native import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verifier-owned disposable PostgreSQL",
    ),
]


async def test_fixture_defaults_and_caller_transaction_use_the_owner(test_engine):
    async with _world(test_engine) as (owner, api, role, _url):
        async with owner.begin() as db:
            actor = await create_user(
                db,
                "super_admin",
                f"labeling-fixture-{uuid.uuid4()}@e2e.test",
                "Fixture",
            )
            actor_id = actor.id
            project = await create_fixture_project(
                db,
                owner_id=actor_id,
                name=f"E2E fixture protocol {uuid.uuid4().hex[:10]}",
            )
            project_id = project.id

        statements: list[str] = []
        engine = api.kw["bind"].sync_engine

        def capture(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement.lower())

        event.listen(engine, "before_cursor_execute", capture)
        batch_id = uuid.uuid4()
        task_id = None
        parent_id = None
        child_id = None
        try:
            async with api() as db:
                assert await db.scalar(text("SELECT current_user")) == role
                assert await db.scalar(
                    text(
                        "SELECT has_function_privilege(current_user, "
                        "'aap_labeling_v1.e2e_fixture_command(jsonb)', 'EXECUTE')"
                    )
                )
                for table in ("tasks", "task_batches", "annotations"):
                    assert not await db.scalar(
                        text(
                            "SELECT has_table_privilege(current_user,:relation,"
                            "'SELECT,INSERT,UPDATE,DELETE') OR "
                            "has_any_column_privilege(current_user,:relation,"
                            "'SELECT,INSERT,UPDATE,REFERENCES')"
                        ),
                        {"relation": f"public.{table}"},
                    ), table

                writes = LabelingE2EFixtureWrites(db)
                batch = (
                    await writes.insert_batches(
                        [
                            TaskBatch(
                                id=batch_id,
                                project_id=project_id,
                                display_id=f"B-E2E-{batch_id.hex[:10]}",
                                name="Defaulted batch",
                                status=None,
                                total_tasks=None,
                                reviewer_id=None,
                            )
                        ]
                    )
                )[0]
                task = (
                    await writes.insert_tasks(
                        [
                            Task(
                                project_id=project_id,
                                batch_id=batch.id,
                                display_id=f"T-E2E-{uuid.uuid4().hex[:10]}",
                                file_name="default.png",
                                file_path="fixture/default.png",
                                status=None,
                                file_type=None,
                                created_at=None,
                                total_annotations=None,
                            )
                        ]
                    )
                )[0]
                task_id = task.id
                task = await writes.patch_task(task, status="completed")
                parent_id = uuid.uuid4()
                child_id = uuid.uuid4()
                annotations = await writes.insert_annotations(
                    [
                        Annotation(
                            id=child_id,
                            task_id=task.id,
                            project_id=project_id,
                            user_id=actor_id,
                            source="manual",
                            annotation_type="bbox",
                            tool_unit_id="bbox",
                            class_name="car",
                            geometry={
                                "type": "bbox",
                                "x": 0.1,
                                "y": 0.1,
                                "w": 0.2,
                                "h": 0.2,
                            },
                            parent_annotation_id=parent_id,
                        ),
                        Annotation(
                            id=parent_id,
                            task_id=task.id,
                            project_id=project_id,
                            user_id=actor_id,
                            source="manual",
                            annotation_type="bbox",
                            tool_unit_id="bbox",
                            class_name="car",
                            geometry={
                                "type": "bbox",
                                "x": 0.1,
                                "y": 0.1,
                                "w": 0.2,
                                "h": 0.2,
                            },
                            parent_annotation_id=None,
                        ),
                    ]
                )
                assert [row.id for row in annotations] == [child_id, parent_id]

                async with owner() as observer:
                    assert (
                        await observer.scalar(
                            text("SELECT count(*) FROM tasks WHERE id=:id"),
                            {"id": task_id},
                        )
                        == 0
                    )
                await db.commit()

            async with owner() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM task_batches WHERE id=:id "
                            "AND status='draft' AND total_tasks=0 AND reviewer_id IS NULL"
                        ),
                        {"id": batch_id},
                    )
                    == 1
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM tasks WHERE id=:id AND status='completed' "
                            "AND file_type='image' AND tags='[]'::jsonb AND total_annotations=0"
                        ),
                        {"id": task_id},
                    )
                    == 1
                )
                assert (
                    await db.scalar(
                        text(
                            "SELECT parent_annotation_id FROM annotations WHERE id=:id"
                        ),
                        {"id": child_id},
                    )
                    == parent_id
                )
        finally:
            event.remove(engine, "before_cursor_execute", capture)
            async with owner.begin() as db:
                if task_id is not None:
                    for annotation_id in (child_id, parent_id):
                        if annotation_id is not None:
                            await db.execute(
                                text("DELETE FROM annotations WHERE id=:id"),
                                {"id": annotation_id},
                            )
                    await db.execute(
                        text("DELETE FROM tasks WHERE id=:id"), {"id": task_id}
                    )
                await db.execute(
                    text("DELETE FROM task_batches WHERE id=:id"), {"id": batch_id}
                )
                await db.execute(
                    text(
                        "SELECT aap_workspace_fixture_v1.delete_projects(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [project_id]},
                )
                await db.execute(
                    text("DELETE FROM users WHERE id=:id"), {"id": actor_id}
                )

        assert any("aap_labeling_v1.e2e_fixture_command" in sql for sql in statements)
        assert not any(
            sql.lstrip().startswith(
                (
                    "insert into public.tasks",
                    "update public.tasks",
                    "insert into public.task_batches",
                    "update public.task_batches",
                )
            )
            for sql in statements
        )
