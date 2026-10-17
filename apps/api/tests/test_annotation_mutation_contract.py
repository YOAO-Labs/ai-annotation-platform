"""Identity-bound Labeling annotation mutation commands."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event

from app.application.annotation_mutation import _AnnotationMutationSqlAdapter
from app.schemas.annotation import AnnotationUpdate
from app.services.annotation_mutation_contract import (
    AnnotationDeleteCommand,
    AnnotationUpdateCommand,
)
from app.services.audit_origin_contract import AuditOriginContext
from app.services.project_access import resolve_project_access
from tests.factory import create_project, create_task


async def test_wrong_mutation_binding_rejects_before_sql_or_side_effects(
    db_session, super_admin
):
    actor, _token = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    task = await create_task(db_session, project_id=project.id)
    await db_session.flush()
    adapter = _AnnotationMutationSqlAdapter(
        db_session,
        task=task,
        actor=actor,
        access=await resolve_project_access(db_session, user=actor, project=project),
    )
    origin = AuditOriginContext(method="PATCH", path="/test", ip="127.0.0.1")
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        with pytest.raises(ValueError):
            await adapter.update(
                AnnotationUpdateCommand(
                    task_id=uuid.uuid4(),
                    annotation_id=uuid.uuid4(),
                    actor_id=actor.id,
                    data=AnnotationUpdate(class_name="car"),
                    if_match=None,
                    audit_origin=origin,
                )
            )
        with pytest.raises(ValueError):
            await adapter.delete(
                AnnotationDeleteCommand(
                    task_id=task.id,
                    annotation_id=uuid.uuid4(),
                    actor_id=uuid.uuid4(),
                    audit_origin=origin,
                )
            )
        assert not statements
        assert task.total_annotations == 0
        assert not db_session.new
        assert not db_session.dirty
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
