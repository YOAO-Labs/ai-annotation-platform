"""Project-role aggregate/delivery access (Increment B3) over real PostgreSQL.

Employees are authorized by their project membership role.  A legacy global
``annotator``/``reviewer`` account is rejected and never falls back, an
employee's other-project membership does not leak work, and a revoked
membership stops job/result access.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.async_jobs import _can_access_job
from app.core.security import create_access_token
from app.db.models.async_job import AsyncJob
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.services.notification import NotificationService
from app.workers.export import _assert_export_task_scope
from tests.factory import (
    create_membership,
    create_project,
    create_task,
    create_user,
)

pytestmark = pytest.mark.asyncio


def _headers(user: User) -> dict[str, str]:
    token = create_access_token(subject=str(user.id), role=user.role)
    return {"Authorization": f"Bearer {token}"}


async def _add_member(
    db: AsyncSession, *, project_id, user: User, role: str, assigned_by
) -> ProjectMember:
    return await create_membership(
        db,
        project_id=project_id,
        user_id=user.id,
        role=role,
        assigned_by=assigned_by,
    )


async def _employee(db: AsyncSession, label: str) -> User:
    return await create_user(
        db, "employee", f"agg-{label}-{uuid.uuid4()}@test.local", label
    )


async def _make_job(
    db: AsyncSession, *, project_id, user_id, kind: str = "export"
) -> AsyncJob:
    job = AsyncJob(
        kind=kind,
        project_id=project_id,
        user_id=user_id,
        status="completed",
        payload={},
        result={},
    )
    db.add(job)
    await db.flush()
    return job


async def test_annotator_dashboard_scopes_to_annotator_membership(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Agg Anno")
    employee = await _employee(db_session, "anno-only")
    await _add_member(
        db_session,
        project_id=project.id,
        user=employee,
        role="reviewer",
        assigned_by=admin.id,
    )
    task = await create_task(db_session, project_id=project.id, status="pending")
    task.assignee_id = employee.id
    await db_session.commit()

    resp = await httpx_client.get(
        "/api/v1/dashboard/annotator", headers=_headers(employee)
    )
    assert resp.status_code == 200
    # Assigned work in a project where the account is only a reviewer must not
    # appear in the annotation queue.
    assert resp.json()["assigned_tasks"] == 0
    assert resp.json()["rejected_tasks_count"] == 0


async def test_reviewer_dashboard_accepts_employee_and_filters_role(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Agg Rev")
    reviewer = await _employee(db_session, "rev-only")
    annotator = await _employee(db_session, "anno-other")
    await _add_member(
        db_session,
        project_id=project.id,
        user=reviewer,
        role="reviewer",
        assigned_by=admin.id,
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=annotator,
        role="annotator",
        assigned_by=admin.id,
    )
    review_task = await create_task(db_session, project_id=project.id, status="review")
    # Unbatched review work is actionable only when assigned to the reviewer.
    review_task.reviewer_id = reviewer.id
    await db_session.commit()

    reviewer_resp = await httpx_client.get(
        "/api/v1/dashboard/reviewer", headers=_headers(reviewer)
    )
    assert reviewer_resp.status_code == 200
    assert reviewer_resp.json()["pending_review_count"] == 1

    annotator_resp = await httpx_client.get(
        "/api/v1/dashboard/reviewer", headers=_headers(annotator)
    )
    assert annotator_resp.status_code == 200
    assert annotator_resp.json()["pending_review_count"] == 0


@pytest.mark.parametrize("legacy_role", ["annotator", "reviewer"])
async def test_legacy_global_staff_role_is_rejected(
    legacy_role: str,
    httpx_client: httpx.AsyncClient,
    db_session: AsyncSession,
):
    legacy = await create_user(
        db_session,
        legacy_role,
        f"legacy-{legacy_role}-{uuid.uuid4()}@test.local",
        legacy_role,
    )
    await db_session.commit()

    resp = await httpx_client.get(
        "/api/v1/dashboard/reviewer", headers=_headers(legacy)
    )
    assert resp.status_code == 403


async def test_async_job_access_drops_after_membership_revocation(
    db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Agg Job")
    employee = await _employee(db_session, "job-owner")
    await _add_member(
        db_session,
        project_id=project.id,
        user=employee,
        role="reviewer",
        assigned_by=admin.id,
    )
    job = await _make_job(db_session, project_id=project.id, user_id=employee.id)
    await db_session.commit()

    assert await _can_access_job(db_session, job=job, user=employee) is True

    await db_session.execute(
        delete(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == employee.id,
        )
    )
    await db_session.commit()
    await db_session.refresh(employee)

    # Job ownership is not enough once current project access is gone.
    assert await _can_access_job(db_session, job=job, user=employee) is False


async def test_export_job_result_denied_to_annotator_membership(
    httpx_client: httpx.AsyncClient, db_session: AsyncSession, super_admin
):
    """Export result/URL is a reviewer/manager capability on every read path."""

    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Agg Exp Job")
    annotator = await _employee(db_session, "exp-job-anno")
    reviewer = await _employee(db_session, "exp-job-rev")
    await _add_member(
        db_session,
        project_id=project.id,
        user=annotator,
        role="annotator",
        assigned_by=admin.id,
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=reviewer,
        role="reviewer",
        assigned_by=admin.id,
    )
    job = AsyncJob(
        kind="export",
        project_id=project.id,
        user_id=annotator.id,
        status="completed",
        payload={},
        result={"download_url": "https://download.invalid/export.zip"},
    )
    db_session.add(job)
    await db_session.commit()

    denied = await httpx_client.get(
        f"/api/v1/async-jobs/{job.id}", headers=_headers(annotator)
    )
    assert denied.status_code == 403
    listed = await httpx_client.get(
        f"/api/v1/async-jobs?project_id={project.id}", headers=_headers(annotator)
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 0

    job.user_id = reviewer.id
    await db_session.commit()
    # The server handler shares this session; refresh the server-updated
    # columns so serialization does not trigger an async lazy load.
    await db_session.refresh(job)
    allowed = await httpx_client.get(
        f"/api/v1/async-jobs/{job.id}", headers=_headers(reviewer)
    )
    assert allowed.status_code == 200
    assert allowed.json()["result"]["download_url"].endswith("export.zip")


async def test_project_notification_delivery_rechecks_membership(
    db_session: AsyncSession, super_admin, monkeypatch
):
    """Revoking project access suppresses future restricted deliveries."""

    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Agg Notify")
    employee = await _employee(db_session, "notify-rev")
    await _add_member(
        db_session,
        project_id=project.id,
        user=employee,
        role="reviewer",
        assigned_by=admin.id,
    )
    await db_session.commit()

    published: list[uuid.UUID] = []

    async def fake_publish(*, user_id, message):
        published.append(user_id)

    monkeypatch.setattr("app.services.notification._publish", fake_publish)
    svc = NotificationService(db_session)

    allowed = await svc.notify(
        user_id=employee.id,
        type="task.mentioned",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={"project_id": str(project.id)},
        defer_publish=True,
    )
    await db_session.commit()
    await svc.publish_committed([allowed])
    assert published == [employee.id]

    await db_session.execute(
        delete(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == employee.id,
        )
    )
    await db_session.commit()
    await db_session.refresh(employee)

    suppressed = await svc.notify(
        user_id=employee.id,
        type="task.mentioned",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={"project_id": str(project.id)},
        defer_publish=True,
    )
    await db_session.commit()
    await svc.publish_committed([suppressed])
    assert published == [employee.id]


async def test_export_worker_requires_export_capability(
    db_session: AsyncSession, super_admin
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Agg Export")
    reviewer = await _employee(db_session, "exp-rev")
    annotator = await _employee(db_session, "exp-anno")
    await _add_member(
        db_session,
        project_id=project.id,
        user=reviewer,
        role="reviewer",
        assigned_by=admin.id,
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=annotator,
        role="annotator",
        assigned_by=admin.id,
    )
    job = await _make_job(db_session, project_id=project.id, user_id=reviewer.id)
    await db_session.commit()

    await _assert_export_task_scope(
        db_session, project_id=project.id, task_ids=None, job_uuid=job.id
    )

    job.user_id = annotator.id
    await db_session.commit()
    with pytest.raises(ValueError):
        await _assert_export_task_scope(
            db_session, project_id=project.id, task_ids=None, job_uuid=job.id
        )
