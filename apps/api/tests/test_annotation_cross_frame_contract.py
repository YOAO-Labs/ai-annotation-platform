"""Identity-bound cross-frame command regressions."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, func, select

from app.application.annotation_cross_frame import (
    _AnnotationCrossFrameSqlAdapter,
    bind_cross_frame_batch_stager,
)
from app.db.models.annotation import Annotation
from app.db.models.task import Task
from app.schemas.annotation import (
    InterpolateRangeRequest,
    PropagateBatchRequest,
    PropagateRequest,
)
from app.services.audit_origin_contract import AuditOriginContext
from app.services.task_access import resolve_task_access
from tests.factory import create_project, create_task
from tests.test_annotation_propagate import _add_annotation, _box3d, _seed_scene


@pytest.mark.asyncio
async def test_wrong_cross_frame_binding_rejects_before_sql_or_side_effects(
    db_session, super_admin
):
    actor, _token = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    source_task = await create_task(
        db_session, project_id=project.id, status="in_progress"
    )
    original_total_annotations = source_task.total_annotations
    access = await resolve_task_access(db_session, source_task, actor)
    adapter = _AnnotationCrossFrameSqlAdapter(
        db_session,
        source_task=source_task,
        actor=actor,
        access=access,
    )
    origin = AuditOriginContext(method="POST", path="/test", ip="127.0.0.1")
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        with pytest.raises(ValueError):
            await adapter.propagate(
                source_task_id=uuid.uuid4(),
                annotation_id=uuid.uuid4(),
                actor_id=actor.id,
                payload=PropagateRequest(target_task_id=uuid.uuid4()),
                audit_origin=origin,
            )
        with pytest.raises(ValueError):
            await adapter.propagate_batch(
                source_task_id=source_task.id,
                actor_id=uuid.uuid4(),
                payload=PropagateBatchRequest(target_task_id=uuid.uuid4()),
                audit_origin=origin,
            )
        with pytest.raises(ValueError):
            await adapter.interpolate_range(
                from_task_id=uuid.uuid4(),
                actor_id=actor.id,
                payload=InterpolateRangeRequest(
                    track_id="trk_contract", to_task_id=uuid.uuid4()
                ),
                audit_origin=origin,
            )
        assert not statements
        assert source_task.total_annotations == original_total_annotations
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)


async def test_cross_frame_stager_leaves_commit_and_rollback_to_caller(
    db_session, super_admin
):
    actor, _ = super_admin
    project, _, _, tasks = await _seed_scene(db_session, owner_id=actor.id, n=2)
    source = await _add_annotation(
        db_session,
        task=tasks[0],
        project=project,
        user_id=actor.id,
        geometry=_box3d(),
    )
    source_task_id, target_task_id = tasks[0].id, tasks[1].id
    source_id, actor_id = source.id, actor.id
    savepoint = await db_session.begin_nested()
    try:
        count = await bind_cross_frame_batch_stager(db_session).stage_batch(
            source_task_id=source_task_id,
            target_task_id=target_task_id,
            annotation_ids=[source_id],
            actor_id=actor_id,
        )
        assert count == 1
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(Annotation)
                .where(Annotation.task_id == target_task_id)
            )
            == 1
        )
    finally:
        await savepoint.rollback()

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Annotation)
            .where(Annotation.task_id == target_task_id)
        )
        == 0
    )
    target = await db_session.get(Task, target_task_id, populate_existing=True)
    assert target.total_annotations == 0
    source = await db_session.get(Annotation, source_id, populate_existing=True)
    assert source.track_id is None
