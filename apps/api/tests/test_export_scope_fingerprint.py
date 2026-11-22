"""Native export cache scope remains readable without private business tables."""

import uuid
from datetime import timedelta

from sqlalchemy import text

from app.db.models.annotation import Annotation
from app.workers.export import _scope_fingerprint
from tests.export_worker_native_fixtures import setup, worker_db as worker_db
from tests.factory import create_batch


async def test_fingerprint_uses_public_rows_and_preserves_scope(worker_db):
    db = worker_db
    project, _, _, _, task, annotation = await setup(db, "image")
    _, _, _, _, foreign_task, _ = await setup(db, "image")
    batch = await create_batch(db, project_id=project.id)
    task.batch_id = batch.id
    for inactive, cancelled in ((True, False), (False, True)):
        db.add(
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=annotation.user_id,
                source="manual",
                class_name="car",
                annotation_type="bbox",
                tool_unit_id="bbox",
                geometry=annotation.geometry,
                attributes={},
                is_active=not inactive,
                was_cancelled=cancelled,
                updated_at=annotation.updated_at + timedelta(days=1),
            )
        )
    await db.commit()
    await db.refresh(project)
    await db.refresh(annotation)
    expected = max(project.updated_at, annotation.updated_at)
    project_updated_at = project.updated_at
    project_id, task_id, batch_id = project.id, task.id, batch.id
    foreign_task_id = foreign_task.id
    await db.rollback()

    role = f"export_fingerprint_{uuid.uuid4().hex}"
    try:
        await db.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        await db.execute(
            text(
                f'GRANT USAGE ON SCHEMA yoao_labeling_v1, yoao_workspace_v1 TO "{role}"'
            )
        )
        await db.execute(
            text(
                "GRANT SELECT ON yoao_labeling_v1.annotation_rows, "
                "yoao_labeling_v1.task_rows, yoao_workspace_v1.project_facts "
                f'TO "{role}"'
            )
        )
        await db.execute(text(f'SET LOCAL ROLE "{role}"'))
        for table in ("annotations", "tasks", "projects"):
            assert not await db.scalar(
                text("SELECT has_table_privilege(current_user, :relation, 'SELECT')"),
                {"relation": f"public.{table}"},
            )
        assert await _scope_fingerprint(db, project_id, None) == (expected, 1)
        assert await _scope_fingerprint(db, project_id, batch_id, [task_id]) == (
            expected,
            1,
        )
        assert await _scope_fingerprint(db, project_id, None, []) == (
            project_updated_at,
            0,
        )
        assert await _scope_fingerprint(db, project_id, None, [foreign_task_id]) == (
            project_updated_at,
            0,
        )
        assert await _scope_fingerprint(db, project_id, uuid.uuid4()) == (
            project_updated_at,
            0,
        )
    finally:
        await db.rollback()
