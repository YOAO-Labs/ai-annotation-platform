"""Task-level Data Manager action invariants."""

from __future__ import annotations

import uuid
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.async_job import AsyncJob
from app.schemas.data_manager_actions import (
    DataManagerTaskAssignmentApplyRequest,
    DataManagerTaskAssignmentRequest,
)
from app.services.data_management.actions import DataManagerTaskActionService
from tests.factory import create_membership, create_project


pytestmark = pytest.mark.asyncio


async def _task(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    display_id: str,
    batch_id: uuid.UUID | None = None,
) -> Task:
    task = Task(
        project_id=project_id,
        batch_id=batch_id,
        display_id=display_id,
        file_name=f"{display_id}.jpg",
        file_path=f"items/{display_id}.jpg",
        file_type="image",
        status="pending",
    )
    db.add(task)
    await db.flush()
    return task


async def test_assignment_updates_only_explicit_tasks_across_batch_boundaries(
    db_session: AsyncSession, super_admin, annotator
):
    owner, _ = super_admin
    target, _ = annotator
    project = await create_project(db_session, owner_id=owner.id)
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=target.id,
        role="annotator",
        assigned_by=owner.id,
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-DM-ACTION-{uuid.uuid4().hex[:8]}",
        name="partial selection",
        status="active",
    )
    db_session.add(batch)
    await db_session.flush()

    selected_in_batch = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-DM-SELECTED-{uuid.uuid4().hex[:8]}",
        batch_id=batch.id,
    )
    sibling_in_batch = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-DM-SIBLING-{uuid.uuid4().hex[:8]}",
        batch_id=batch.id,
    )
    selected_unbatched = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-DM-UNBATCHED-{uuid.uuid4().hex[:8]}",
    )

    payload = DataManagerTaskAssignmentRequest(
        task_ids=[selected_unbatched.id, selected_in_batch.id],
        annotator_id=target.id,
    )
    service = DataManagerTaskActionService(db_session)
    preview = await service.preview_assignment(project.id, payload, actor=owner)

    assert preview.eligible_count == 2
    assert preview.failed_count == 0
    assert {item.task_id for item in preview.items} == {
        selected_in_batch.id,
        selected_unbatched.id,
    }

    result = await service.apply_assignment(
        project.id,
        DataManagerTaskAssignmentApplyRequest(
            task_ids=payload.task_ids,
            annotator_id=target.id,
            preview_version=preview.preview_version,
        ),
        actor=owner,
    )
    assert result.succeeded == payload.task_ids

    await db_session.refresh(selected_in_batch)
    await db_session.refresh(selected_unbatched)
    await db_session.refresh(sibling_in_batch)
    assert selected_in_batch.assignee_id == target.id
    assert selected_unbatched.assignee_id == target.id
    assert sibling_in_batch.assignee_id is None


async def test_assignment_preview_becomes_stale_after_task_state_changes(
    db_session: AsyncSession, super_admin, annotator
):
    owner, _ = super_admin
    target, _ = annotator
    project = await create_project(db_session, owner_id=owner.id)
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=target.id,
        role="annotator",
        assigned_by=owner.id,
    )
    task = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-DM-STALE-{uuid.uuid4().hex[:8]}",
    )
    payload = DataManagerTaskAssignmentRequest(
        task_ids=[task.id], annotator_id=target.id
    )
    service = DataManagerTaskActionService(db_session)
    preview = await service.preview_assignment(project.id, payload, actor=owner)

    task.status = "completed"
    await db_session.flush()

    apply_payload = DataManagerTaskAssignmentApplyRequest(
        task_ids=payload.task_ids,
        annotator_id=target.id,
        preview_version=preview.preview_version,
    )
    with pytest.raises(HTTPException) as exc:
        await service.apply_assignment(project.id, apply_payload, actor=owner)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "data_manager_assignment_preview_stale"


async def test_assignment_route_rejects_non_owner_and_skips_owner_edit_lock(
    httpx_client, db_session, super_admin, annotator
):
    owner, owner_token = super_admin
    member, member_token = annotator
    project = await create_project(db_session, owner_id=owner.id)
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=member.id,
        role="annotator",
        assigned_by=owner.id,
    )
    task = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-DM-LOCK-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(
        TaskLock(
            task_id=task.id,
            user_id=owner.id,
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    )
    await db_session.commit()
    endpoint = f"/api/v1/projects/{project.id}/data-manager/tasks/assignment-preview"
    payload = {"task_ids": [str(task.id)], "annotator_id": str(member.id)}
    denied = await httpx_client.post(
        endpoint, headers={"Authorization": f"Bearer {member_token}"}, json=payload
    )
    assert denied.status_code == 403
    preview = await httpx_client.post(
        endpoint, headers={"Authorization": f"Bearer {owner_token}"}, json=payload
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["eligible_count"] == 0
    assert preview.json()["items"][0]["reason"] == "task_locked"


async def test_export_keeps_exact_task_scope_in_job_worker_and_artifact(
    httpx_client, db_session, super_admin, monkeypatch
):
    from app.api.v1 import data_manager
    from app.services.exporting.service import ExportService
    from app.workers.export import _assert_export_task_scope

    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    selected = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-DM-EXPORT-{uuid.uuid4().hex[:8]}",
    )
    sibling = await _task(
        db_session, project_id=project.id, display_id=f"T-DM-SIB-{uuid.uuid4().hex[:8]}"
    )
    await db_session.commit()
    dispatch = Mock(return_value=SimpleNamespace(id=str(uuid.uuid4())))
    monkeypatch.setattr(data_manager.run_export, "delay", dispatch)
    endpoint = f"/api/v1/projects/{project.id}/data-manager/tasks/export"
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": f"export-{uuid.uuid4()}",
    }
    body = {"task_ids": [str(selected.id)], "targets": ["aap_json"]}
    first = await httpx_client.post(endpoint, headers=headers, json=body)
    assert first.status_code == 202, first.text
    job_id = uuid.UUID(first.json()["job_id"])
    job = await db_session.get(AsyncJob, job_id)
    assert job.payload["scope"] == {"task_ids": [str(selected.id)]}
    assert dispatch.call_args.kwargs["task_ids"] == [str(selected.id)]
    assert dispatch.call_args.kwargs["batch_id"] is None

    # Pending publication can be retried using the same durable job; a running
    # job must not be dispatched again, and a changed request conflicts.
    retried = await httpx_client.post(endpoint, headers=headers, json=body)
    assert retried.json()["job_id"] == str(job_id)
    assert dispatch.call_count == 2
    job.status = "running"
    await db_session.commit()
    running = await httpx_client.post(endpoint, headers=headers, json=body)
    assert running.json()["job_id"] == str(job_id)
    assert dispatch.call_count == 2
    conflict = await httpx_client.post(
        endpoint, headers=headers, json={**body, "task_ids": [str(sibling.id)]}
    )
    assert conflict.status_code == 409

    await _assert_export_task_scope(
        db_session, project_id=project.id, task_ids=[selected.id], job_uuid=job_id
    )
    with pytest.raises(ValueError, match="does not match"):
        await _assert_export_task_scope(
            db_session, project_id=project.id, task_ids=[sibling.id], job_uuid=job_id
        )
    artifact = json.loads(
        await ExportService(db_session, task_ids=[selected.id]).export_aap_json(
            project.id
        )
    )
    assert [row["task_match"]["display_id"] for row in artifact["tasks"]] == [
        selected.display_id
    ]


async def test_export_rejects_foreign_tasks_empty_selection_and_partial_scene_format(
    httpx_client, db_session, super_admin, monkeypatch
):
    from app.api.v1 import data_manager

    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    other = await create_project(db_session, owner_id=owner.id)
    foreign = await _task(
        db_session,
        project_id=other.id,
        display_id=f"T-DM-FOREIGN-{uuid.uuid4().hex[:8]}",
    )
    local = await _task(
        db_session,
        project_id=project.id,
        display_id=f"T-DM-LOCAL-{uuid.uuid4().hex[:8]}",
    )
    await db_session.commit()
    dispatch = Mock()
    monkeypatch.setattr(data_manager.run_export, "delay", dispatch)
    endpoint = f"/api/v1/projects/{project.id}/data-manager/tasks/export"
    headers = {"Authorization": f"Bearer {token}"}
    for body, status in [
        ({"task_ids": [str(foreign.id)], "targets": ["aap_json"]}, 404),
        ({"task_ids": [], "targets": ["aap_json"]}, 422),
        ({"task_ids": [str(local.id)], "targets": ["voc"]}, 422),
        (
            {
                "task_ids": [str(uuid.uuid4()) for _ in range(201)],
                "targets": ["aap_json"],
            },
            422,
        ),
    ]:
        response = await httpx_client.post(endpoint, headers=headers, json=body)
        assert response.status_code == status, response.text
    dispatch.assert_not_called()
