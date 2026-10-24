"""Management consumers keep parent, project and task visibility before limits."""

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.security import create_access_token
from app.db.models.audit_log import AuditLog
from app.db.models.task_batch import TaskBatch
from app.db.models.async_job import AsyncJob
from sqlalchemy import select
from tests.factory import (
    create_batch,
    create_membership,
    create_project,
    create_task,
    create_user,
)

pytestmark = pytest.mark.asyncio


def _headers(user):
    return {
        "Authorization": "Bearer "
        + create_access_token(subject=str(user.id), role=user.role)
    }


async def test_batch_audit_rejects_foreign_parent_and_preserves_deleted_history(
    db_session, httpx_client, super_admin
):
    owner = await create_user(
        db_session, "project_admin", f"batch-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    other = await create_user(
        db_session, "project_admin", f"batch-other-{uuid.uuid4()}@test.local", "Other"
    )
    project = await create_project(db_session, owner_id=owner.id)
    foreign = await create_project(db_session, owner_id=other.id)
    batch = await create_batch(db_session, project_id=foreign.id)
    deleted_id = uuid.uuid4()
    path = f"/api/v1/projects/{project.id}/batches/{deleted_id}"

    def record(
        target_type, target_id, *, detail=None, path=None, action="batch.deleted"
    ):
        return AuditLog(
            actor_id=owner.id,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            detail_json=detail,
            path=path,
            created_at=datetime.now(timezone.utc),
        )

    visible = [
        record("batch", deleted_id, detail={"project_id": str(project.id)}),
        record("batch", deleted_id, path=path),
        record(
            "batch",
            deleted_id,
            path=path + "/transition",
            action="batch.status_changed",
        ),
        record(
            "project",
            project.id,
            detail={"batch_ids": [str(deleted_id)]},
            action="batch.bulk_delete",
        ),
    ]
    hidden = [
        record("batch", batch.id, detail={"project_id": str(project.id)}),
        record("batch", deleted_id, detail={"project_id": str(foreign.id)}),
        record("batch", deleted_id, path=path + "extra"),
        record("batch", deleted_id),
        record(
            "project",
            foreign.id,
            detail={"batch_ids": [str(deleted_id)]},
            action="batch.bulk_delete",
        ),
    ]
    db_session.add_all([*visible, *hidden])
    await db_session.flush()
    for headers in (_headers(owner), {"Authorization": f"Bearer {super_admin[1]}"}):
        refused = await httpx_client.get(
            f"/api/v1/projects/{project.id}/batches/{batch.id}/audit-logs",
            headers=headers,
        )
        assert refused.status_code == 404, refused.text
    response = await httpx_client.get(path + "/audit-logs", headers=_headers(owner))
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()} == {row.id for row in visible}
    unknown = await httpx_client.get(
        f"/api/v1/projects/{project.id}/batches/{uuid.uuid4()}/audit-logs",
        headers=_headers(owner),
    )
    assert unknown.status_code == 200 and unknown.json() == []


async def test_people_project_scope_covers_counts_export_and_timeline_before_limit(
    db_session, httpx_client, super_admin
):
    owner = await create_user(
        db_session, "project_admin", f"people-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    other = await create_user(
        db_session, "project_admin", f"people-other-{uuid.uuid4()}@test.local", "Other"
    )
    employee = await create_user(
        db_session, "employee", f"people-staff-{uuid.uuid4()}@test.local", "Staff"
    )
    projects = [
        await create_project(db_session, owner_id=user.id)
        for user in (owner, owner, other)
    ]
    tasks = []
    for project in projects:
        await create_membership(
            db_session, project_id=project.id, user_id=employee.id, role="annotator"
        )
        tasks.append(await create_task(db_session, project_id=project.id))
    now = datetime.now(timezone.utc)
    own = AuditLog(
        actor_id=employee.id,
        action="task.submit",
        target_type="task",
        target_id=str(tasks[0].id),
        detail_json={"reason": "owned", "task_display_id": tasks[0].display_id},
        created_at=now - timedelta(hours=1),
    )
    db_session.add(own)
    db_session.add_all(
        [
            AuditLog(
                actor_id=employee.id,
                action="task.reject",
                target_type="task",
                target_id=str(tasks[1 + i % 2].id),
                detail_json={
                    "reason": "cross-project-secret",
                    "project_id": str(projects[0].id),
                },
                created_at=now + timedelta(seconds=i),
            )
            for i in range(55)
        ]
    )
    await db_session.flush()
    query = {"project": str(projects[0].id)}
    listed = await httpx_client.get(
        "/api/v1/dashboard/admin/people", params=query, headers=_headers(owner)
    )
    assert listed.status_code == 200, listed.text
    assert [
        (row["user_id"], row["project_count"]) for row in listed.json()["items"]
    ] == [(str(employee.id), 1)]
    exported = await httpx_client.get(
        "/api/v1/dashboard/admin/people/export", params=query, headers=_headers(owner)
    )
    assert exported.status_code == 200
    assert (
        list(csv.DictReader(io.StringIO(exported.text.lstrip("\ufeff"))))[0][
            "project_count"
        ]
        == "1"
    )
    for headers in (_headers(owner), {"Authorization": f"Bearer {super_admin[1]}"}):
        detail = await httpx_client.get(
            f"/api/v1/dashboard/admin/people/{employee.id}",
            params=query,
            headers=headers,
        )
        assert detail.status_code == 200, detail.text
        data = detail.json()
        assert data["project_count"] == 1
        assert len(data["timeline"]) == 1 and data["timeline"][0]["detail"] == "owned"
        assert "cross-project-secret" not in detail.text
    global_detail = await httpx_client.get(
        f"/api/v1/dashboard/admin/people/{employee.id}",
        headers={"Authorization": f"Bearer {super_admin[1]}"},
    )
    assert global_detail.status_code == 200
    assert global_detail.json()["project_count"] == 3
    assert len(global_detail.json()["timeline"]) == 50


async def test_global_search_filters_assigned_tasks_before_limit_and_preserves_video_collaboration(
    db_session, httpx_client
):
    owner = await create_user(
        db_session, "project_admin", f"search-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    employee = await create_user(
        db_session, "employee", f"search-staff-{uuid.uuid4()}@test.local", "Staff"
    )
    other = await create_user(
        db_session, "employee", f"search-other-{uuid.uuid4()}@test.local", "Other"
    )
    project = await create_project(db_session, owner_id=owner.id)
    await create_membership(
        db_session, project_id=project.id, user_id=employee.id, role="annotator"
    )
    batch = await create_batch(db_session, project_id=project.id, status="active")
    batch.annotator_id = employee.id
    own = await create_task(db_session, project_id=project.id)
    own.batch_id = batch.id
    own.file_name = "search-scope-owned.jpg"
    own.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    hidden = await create_task(db_session, project_id=project.id)
    hidden.batch_id = batch.id
    hidden.assignee_id = other.id
    hidden.file_name = "search-scope-hidden.jpg"
    hidden.updated_at = datetime.now(timezone.utc)
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/search",
        params={"q": "search-scope", "limit": 1},
        headers=_headers(employee),
    )
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["tasks"]] == [str(own.id)]
    manager = await httpx_client.get(
        "/api/v1/search",
        params={"q": "search-scope", "limit": 20},
        headers=_headers(owner),
    )
    assert {row["id"] for row in manager.json()["tasks"]} == {
        str(own.id),
        str(hidden.id),
    }
    project.video_collaboration = {"enabled": True}
    hidden.file_type = "video"
    await db_session.flush()
    collaboration = await httpx_client.get(
        "/api/v1/search",
        params={"q": "search-scope", "limit": 1},
        headers=_headers(employee),
    )
    assert collaboration.status_code == 200
    assert [row["id"] for row in collaboration.json()["tasks"]] == [str(hidden.id)]


@pytest.mark.parametrize(
    "platform_role,project_role", [("employee", "reviewer"), ("viewer", "viewer")]
)
async def test_search_uses_membership_role_for_reviewers_and_viewers(
    db_session, httpx_client, platform_role, project_role
):
    owner = await create_user(
        db_session,
        "project_admin",
        f"search-manager-{uuid.uuid4()}@test.local",
        "Owner",
    )
    actor = await create_user(
        db_session, platform_role, f"search-role-{uuid.uuid4()}@test.local", "Actor"
    )
    project = await create_project(db_session, owner_id=owner.id)
    await create_membership(
        db_session, project_id=project.id, user_id=actor.id, role=project_role
    )
    batch = await create_batch(db_session, project_id=project.id, status="active")
    assigned = await create_task(db_session, project_id=project.id)
    assigned.batch_id, assigned.assignee_id, assigned.file_name = (
        batch.id,
        actor.id,
        "role-scope-assigned.jpg",
    )
    foreign = await create_task(db_session, project_id=project.id)
    foreign.batch_id, foreign.assignee_id, foreign.file_name = (
        batch.id,
        owner.id,
        "role-scope-foreign.jpg",
    )
    unbatched = await create_task(db_session, project_id=project.id)
    unbatched.assignee_id, unbatched.reviewer_id, unbatched.file_name = (
        actor.id,
        actor.id,
        "role-scope-unbatched.jpg",
    )
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/search",
        params={"q": "role-scope", "limit": 20},
        headers=_headers(actor),
    )
    assert response.status_code == 200, response.text
    expected = (
        {assigned.id, foreign.id, unbatched.id}
        if project_role == "reviewer"
        else {assigned.id}
    )
    assert {row["id"] for row in response.json()["tasks"]} == {
        str(id_) for id_ in expected
    }


async def test_split_rejects_more_batches_than_tasks_before_any_creation(
    db_session, httpx_client, super_admin
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    task = await create_task(db_session, project_id=project.id)
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/batches/split",
        json={"strategy": "random", "n_batches": 2},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400, response.text
    assert (
        list(
            (
                await db_session.execute(
                    select(TaskBatch).where(TaskBatch.project_id == project.id)
                )
            ).scalars()
        )
        == []
    )
    await db_session.refresh(task)
    assert task.batch_id is None


async def test_batch_export_requires_annotation_export_capability_before_dispatch(
    db_session, httpx_client, monkeypatch, super_admin
):
    owner = await create_user(
        db_session, "project_admin", f"export-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    project = await create_project(db_session, owner_id=owner.id)
    batch = await create_batch(db_session, project_id=project.id)
    actors = []
    for platform_role, project_role in (
        ("employee", "annotator"),
        ("viewer", "viewer"),
        ("employee", "reviewer"),
    ):
        actor = await create_user(
            db_session,
            platform_role,
            f"export-{project_role}-{uuid.uuid4()}@test.local",
            "Actor",
        )
        await create_membership(
            db_session, project_id=project.id, user_id=actor.id, role=project_role
        )
        actors.append(actor)
    dispatched = []

    async def _dispatch_job(**kwargs):
        dispatched.append(kwargs)
        return kwargs["execution_id"]

    monkeypatch.setattr("app.services.jobs_queue.dispatch_job", _dispatch_job)
    await db_session.flush()
    base = f"/api/v1/projects/{project.id}/batches/{batch.id}"
    for actor in actors[:2]:
        for path, body in (
            ("/export", None),
            ("/exports/lidar:preflight", {"targets": ["kitti"]}),
        ):
            response = await httpx_client.post(
                base + path, json=body, headers=_headers(actor)
            )
            assert response.status_code == 403, response.text
    assert dispatched == []
    assert (
        list(
            (
                await db_session.execute(
                    select(AsyncJob).where(AsyncJob.project_id == project.id)
                )
            ).scalars()
        )
        == []
    )
    for headers in (
        _headers(actors[2]),
        _headers(owner),
        {"Authorization": f"Bearer {super_admin[1]}"},
    ):
        response = await httpx_client.post(base + "/export", headers=headers)
        assert response.status_code == 202 and response.json()["job_id"], response.text
    assert len(dispatched) == 3
    project.data_type = "lidar"
    await db_session.flush()
    for headers in (_headers(actors[2]), _headers(owner)):
        preflight = await httpx_client.post(
            base + "/exports/lidar:preflight",
            json={"targets": ["kitti"]},
            headers=headers,
        )
        assert preflight.status_code == 200, preflight.text
    assert len(dispatched) == 3
