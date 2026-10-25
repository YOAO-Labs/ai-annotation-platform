"""Task-scoped bulk annotation mutation contract regressions."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, select

from app.application.annotation_mutation import _AnnotationBulkUpdateSqlAdapter
from app.db.models.annotation import Annotation
from app.db.models.audit_log import AuditLog
from app.schemas.annotation import (
    AnnotationBulkPatch,
    AnnotationBulkUpdateRequest,
)
from app.services.annotation_mutation_contract import AnnotationBulkUpdateCommand
from app.services.audit_origin_contract import AuditOriginContext
from tests.factory import create_project, create_task


@pytest.mark.asyncio
async def test_wrong_bulk_actor_binding_rejects_before_sql_or_side_effects(
    db_session, super_admin
):
    actor, _token = super_admin
    adapter = _AnnotationBulkUpdateSqlAdapter(db_session, actor=actor)
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        with pytest.raises(ValueError):
            await adapter.update(
                AnnotationBulkUpdateCommand(
                    actor_id=uuid.uuid4(),
                    payload=AnnotationBulkUpdateRequest(
                        ids=[uuid.uuid4()],
                        patch=AnnotationBulkPatch(is_hidden=True),
                    ),
                    audit_origin=AuditOriginContext(
                        method="POST", path="/test", ip="127.0.0.1"
                    ),
                )
            )
        assert not statements
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)


@pytest.mark.asyncio
async def test_bulk_adapter_flushes_dirty_version_inside_no_autoflush(
    db_session, super_admin
):
    actor, _token = super_admin
    project = await create_project(db_session, owner_id=actor.id)
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=actor.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        is_active=True,
        attributes={},
    )
    db_session.add(annotation)
    await db_session.flush()
    pending_version = annotation.version + 10
    annotation.version = pending_version
    with db_session.no_autoflush:
        response = await _AnnotationBulkUpdateSqlAdapter(
            db_session, actor=actor
        ).update(
            AnnotationBulkUpdateCommand(
                actor_id=actor.id,
                payload=AnnotationBulkUpdateRequest(
                    ids=[annotation.id], patch=AnnotationBulkPatch(is_hidden=True)
                ),
                audit_origin=AuditOriginContext(
                    method="POST", path="/test/bulk-no-autoflush", ip="127.0.0.1"
                ),
            )
        )

    assert response.updated_ids == [annotation.id]
    await db_session.refresh(annotation)
    assert annotation.version == pending_version + 1
    assert annotation.is_hidden is True


@pytest.mark.asyncio
async def test_bulk_route_preserves_task_lock_order_empty_patch_and_errors(
    db_session, httpx_client, super_admin
):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id)
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        is_active=True,
        attributes={},
    )
    db_session.add(annotation)
    await db_session.flush()
    original_version = annotation.version
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Forwarded-For": "203.0.113.27",
    }
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        no_op = await httpx_client.post(
            "/api/v1/annotations/bulk-update",
            json={"ids": [str(annotation.id)], "patch": {}},
            headers=headers,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert no_op.status_code == 200, no_op.text
    assert no_op.json() == {
        "updated_ids": [str(annotation.id)],
        "updated_count": 1,
    }
    identity_select = next(
        index
        for index, statement in enumerate(statements)
        if "from aap_labeling_v1.annotation_rows" in statement.lower()
    )
    task_lock = next(
        index
        for index, statement in enumerate(statements)
        if "from aap_labeling_v1.lock_task_for_core_read(" in statement.lower()
    )
    assert identity_select < task_lock

    all_none = await httpx_client.post(
        "/api/v1/annotations/bulk-update",
        json={"ids": [str(annotation.id)], "patch": {"is_hidden": None}},
        headers=headers,
    )
    assert all_none.status_code == 200, all_none.text
    await db_session.refresh(annotation)
    assert annotation.version == original_version + 2
    assert annotation.is_hidden is False
    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "annotation.bulk_update",
            AuditLog.target_type == "task",
            AuditLog.target_id == str(task.id),
        )
    )
    assert audit is not None
    assert audit.method == "POST"
    assert audit.path == "/api/v1/annotations/bulk-update"
    assert str(audit.ip) == "203.0.113.27"
    assert audit.detail_json == {
        "annotation_ids": [str(annotation.id)],
        "patch": {},
        "count": 1,
    }

    duplicate = await httpx_client.post(
        "/api/v1/annotations/bulk-update",
        json={
            "ids": [str(annotation.id), str(annotation.id)],
            "patch": {"is_hidden": True},
        },
        headers=headers,
    )
    assert duplicate.status_code == 404
    assert duplicate.json() == {"detail": "annotations not found: []"}

    empty = await httpx_client.post(
        "/api/v1/annotations/bulk-update",
        json={"ids": [], "patch": {}},
        headers=headers,
    )
    assert empty.status_code == 422
    assert empty.json() == {"detail": "ids must not be empty"}

    other_task = await create_task(
        db_session, project_id=project.id, status="in_progress"
    )
    other_annotation = Annotation(
        task_id=other_task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.2, "y": 0.2, "w": 0.2, "h": 0.2},
        is_active=True,
        attributes={},
    )
    db_session.add(other_annotation)
    await db_session.flush()
    other_version = other_annotation.version
    mixed_tasks = await httpx_client.post(
        "/api/v1/annotations/bulk-update",
        json={
            "ids": [str(annotation.id), str(other_annotation.id)],
            "patch": {"is_hidden": True},
        },
        headers=headers,
    )
    assert mixed_tasks.status_code == 422
    assert mixed_tasks.json() == {
        "detail": "bulk operation requires all annotations belong to a single task"
    }

    await db_session.refresh(annotation)
    await db_session.refresh(other_annotation)
    assert annotation.version == original_version + 2
    assert annotation.is_hidden is False
    assert other_annotation.version == other_version
    assert other_annotation.is_hidden is False
