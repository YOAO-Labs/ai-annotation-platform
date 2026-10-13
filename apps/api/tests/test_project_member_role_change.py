"""Member add / role-change (preview + CAS + handoff) / removal over PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.services.project_membership import (
    preview_role_change,
    remove_member,
)
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


async def _preview(
    client: httpx.AsyncClient,
    project_id,
    member_id,
    owner: User,
    target_role: str,
    **extra,
):
    body = {"project_role": target_role, **extra}
    return await client.post(
        f"/api/v1/projects/{project_id}/members/{member_id}/role/preview",
        json=body,
        headers=_headers(owner),
    )


async def test_role_change_happy_path_and_cas(
    httpx_client: httpx.AsyncClient, project_admin, db_session: AsyncSession
):
    owner, _ = project_admin
    project = await create_project(db_session, owner_id=owner.id, name="Role QA")
    employee = await create_user(
        db_session, "employee", f"rc-{uuid.uuid4()}@test.local", "Member"
    )
    member = await _add_member(
        db_session,
        project_id=project.id,
        user=employee,
        role="annotator",
        assigned_by=owner.id,
    )

    preview = await _preview(httpx_client, project.id, member.id, owner, "reviewer")
    assert preview.status_code == 200, preview.text
    payload = preview.json()
    assert payload["blockers"] == []
    assert payload["current_version"] == 1
    token = payload["preview_token"]

    changed = await httpx_client.patch(
        f"/api/v1/projects/{project.id}/members/{member.id}/role",
        json={
            "project_role": "reviewer",
            "expected_version": 1,
            "preview_token": token,
            "reason": "rotation",
        },
        headers=_headers(owner),
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["role"] == "reviewer"
    assert changed.json()["version"] == 2

    # Same version cannot be applied twice.
    replay = await httpx_client.patch(
        f"/api/v1/projects/{project.id}/members/{member.id}/role",
        json={
            "project_role": "reviewer",
            "expected_version": 1,
            "preview_token": token,
            "reason": "rotation",
        },
        headers=_headers(owner),
    )
    assert replay.status_code == 409, replay.text


async def test_stale_preview_token_conflicts(
    httpx_client: httpx.AsyncClient, project_admin, db_session: AsyncSession
):
    owner, _ = project_admin
    project = await create_project(db_session, owner_id=owner.id, name="Stale QA")
    employee = await create_user(
        db_session, "employee", f"stale-{uuid.uuid4()}@test.local", "Member"
    )
    member = await _add_member(
        db_session,
        project_id=project.id,
        user=employee,
        role="annotator",
        assigned_by=owner.id,
    )

    preview = await _preview(httpx_client, project.id, member.id, owner, "reviewer")
    assert preview.status_code == 200, preview.text
    token = preview.json()["preview_token"]

    # A new assignment invalidates the previewed resource snapshot.
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    task.assignee_id = employee.id
    await db_session.flush()

    changed = await httpx_client.patch(
        f"/api/v1/projects/{project.id}/members/{member.id}/role",
        json={
            "project_role": "reviewer",
            "expected_version": 1,
            "preview_token": token,
            "reason": "rotation",
        },
        headers=_headers(owner),
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["reason"] == "stale_resource_snapshot"


async def test_unfinished_annotation_work_requires_and_applies_handoff(
    httpx_client: httpx.AsyncClient, project_admin, db_session: AsyncSession
):
    owner, _ = project_admin
    project = await create_project(db_session, owner_id=owner.id, name="Handoff QA")
    member_user = await create_user(
        db_session, "employee", f"hand-{uuid.uuid4()}@test.local", "Member"
    )
    receiver = await create_user(
        db_session, "employee", f"recv-{uuid.uuid4()}@test.local", "Receiver"
    )
    member = await _add_member(
        db_session,
        project_id=project.id,
        user=member_user,
        role="annotator",
        assigned_by=owner.id,
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=receiver,
        role="annotator",
        assigned_by=owner.id,
    )
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    task.assignee_id = member_user.id
    await db_session.flush()

    blocked = await _preview(httpx_client, project.id, member.id, owner, "reviewer")
    assert blocked.status_code == 200, blocked.text
    assert "unfinished_annotation_work" in blocked.json()["blockers"]
    assert blocked.json()["requires_handoff"] is True

    no_handoff = await httpx_client.patch(
        f"/api/v1/projects/{project.id}/members/{member.id}/role",
        json={
            "project_role": "reviewer",
            "expected_version": 1,
            "preview_token": blocked.json()["preview_token"],
            "reason": "rotation",
        },
        headers=_headers(owner),
    )
    assert no_handoff.status_code == 409, no_handoff.text

    handed = await _preview(
        httpx_client,
        project.id,
        member.id,
        owner,
        "reviewer",
        replacement_annotator_id=str(receiver.id),
    )
    assert handed.status_code == 200, handed.text
    assert handed.json()["blockers"] == []

    changed = await httpx_client.patch(
        f"/api/v1/projects/{project.id}/members/{member.id}/role",
        json={
            "project_role": "reviewer",
            "expected_version": 1,
            "preview_token": handed.json()["preview_token"],
            "reason": "rotation",
            "replacement_annotator_id": str(receiver.id),
        },
        headers=_headers(owner),
    )
    assert changed.status_code == 200, changed.text

    refreshed = await db_session.scalar(select(Task).where(Task.id == task.id))
    assert refreshed is not None
    assert refreshed.assignee_id == receiver.id


async def test_add_member_platform_project_role_compatibility(
    httpx_client: httpx.AsyncClient, project_admin, db_session: AsyncSession
):
    owner, _ = project_admin
    project = await create_project(db_session, owner_id=owner.id, name="Add QA")
    viewer = await create_user(
        db_session, "viewer", f"add-view-{uuid.uuid4()}@test.local", "View"
    )
    employee = await create_user(
        db_session, "employee", f"add-emp-{uuid.uuid4()}@test.local", "Emp"
    )

    incompatible = await httpx_client.post(
        f"/api/v1/projects/{project.id}/members",
        json={"user_id": str(viewer.id), "role": "annotator"},
        headers=_headers(owner),
    )
    assert incompatible.status_code == 400, incompatible.text

    viewer_ok = await httpx_client.post(
        f"/api/v1/projects/{project.id}/members",
        json={"user_id": str(viewer.id), "role": "viewer"},
        headers=_headers(owner),
    )
    assert viewer_ok.status_code == 201, viewer_ok.text
    assert viewer_ok.json()["platform_role"] == "viewer"

    employee_ok = await httpx_client.post(
        f"/api/v1/projects/{project.id}/members",
        json={"user_id": str(employee.id), "role": "reviewer"},
        headers=_headers(owner),
    )
    assert employee_ok.status_code == 201, employee_ok.text
    assert employee_ok.json()["role"] == "reviewer"
    assert employee_ok.json()["platform_role"] == "employee"

    duplicate = await httpx_client.post(
        f"/api/v1/projects/{project.id}/members",
        json={"user_id": str(employee.id), "role": "reviewer"},
        headers=_headers(owner),
    )
    assert duplicate.status_code == 409, duplicate.text


async def test_remove_idle_member_and_block_member_with_work(
    httpx_client: httpx.AsyncClient, project_admin, db_session: AsyncSession
):
    owner, _ = project_admin
    project = await create_project(db_session, owner_id=owner.id, name="Remove QA")
    idle_user = await create_user(
        db_session, "employee", f"rm-idle-{uuid.uuid4()}@test.local", "Idle"
    )
    busy_user = await create_user(
        db_session, "employee", f"rm-busy-{uuid.uuid4()}@test.local", "Busy"
    )
    idle = await _add_member(
        db_session,
        project_id=project.id,
        user=idle_user,
        role="viewer",
        assigned_by=owner.id,
    )
    busy = await _add_member(
        db_session,
        project_id=project.id,
        user=busy_user,
        role="annotator",
        assigned_by=owner.id,
    )
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    task.assignee_id = busy_user.id
    await db_session.flush()

    removed = await httpx_client.delete(
        f"/api/v1/projects/{project.id}/members/{idle.id}",
        headers=_headers(owner),
    )
    assert removed.status_code == 204, removed.text

    blocked = await httpx_client.delete(
        f"/api/v1/projects/{project.id}/members/{busy.id}",
        headers=_headers(owner),
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["reason"] == "member_removal_blocked"


async def test_invalid_and_self_receivers_rejected_without_counted_work(
    db_session: AsyncSession,
):
    """A supplied receiver is validated even when the member has no counted work."""

    owner = await create_user(
        db_session, "project_admin", f"recv-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    project = await create_project(db_session, owner_id=owner.id, name="Receiver QA")
    employee = await create_user(
        db_session, "employee", f"recv-emp-{uuid.uuid4()}@test.local", "Emp"
    )
    viewer = await create_user(
        db_session, "viewer", f"recv-view-{uuid.uuid4()}@test.local", "View"
    )
    member = await _add_member(
        db_session,
        project_id=project.id,
        user=employee,
        role="annotator",
        assigned_by=owner.id,
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=viewer,
        role="viewer",
        assigned_by=owner.id,
    )

    # Self receiver is rejected even with no outstanding work.
    self_preview = await preview_role_change(
        db_session,
        project=project,
        actor=owner,
        member_id=member.id,
        target_role="reviewer",
        replacement_annotator_id=employee.id,
        replacement_reviewer_id=None,
    )
    assert "self_replacement" in self_preview["blockers"]

    # An incompatible platform/project receiver is rejected with no counted work.
    invalid_preview = await preview_role_change(
        db_session,
        project=project,
        actor=owner,
        member_id=member.id,
        target_role="reviewer",
        replacement_annotator_id=viewer.id,
        replacement_reviewer_id=None,
    )
    assert "invalid_replacement_annotator" in invalid_preview["blockers"]


async def test_invalid_receiver_does_not_bypass_active_lock_blocking(
    db_session: AsyncSession,
):
    """The draft returned ``ok`` for an invalid receiver and hid the lock blocker."""

    owner = await create_user(
        db_session, "project_admin", f"lock-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    project = await create_project(db_session, owner_id=owner.id, name="Lock QA")
    employee = await create_user(
        db_session, "employee", f"lock-emp-{uuid.uuid4()}@test.local", "Emp"
    )
    viewer = await create_user(
        db_session, "viewer", f"lock-view-{uuid.uuid4()}@test.local", "View"
    )
    member = await _add_member(
        db_session,
        project_id=project.id,
        user=employee,
        role="annotator",
        assigned_by=owner.id,
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=viewer,
        role="viewer",
        assigned_by=owner.id,
    )
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    task.assignee_id = employee.id
    db_session.add(
        TaskLock(
            task_id=task.id,
            user_id=employee.id,
            expire_at=datetime(2999, 1, 1, tzinfo=timezone.utc),
        )
    )
    await db_session.flush()

    no_replacement = await preview_role_change(
        db_session,
        project=project,
        actor=owner,
        member_id=member.id,
        target_role="viewer",
        replacement_annotator_id=None,
        replacement_reviewer_id=None,
    )
    assert "unfinished_annotation_work" in no_replacement["blockers"]
    assert "active_locks" in no_replacement["blockers"]

    invalid_replacement = await preview_role_change(
        db_session,
        project=project,
        actor=owner,
        member_id=member.id,
        target_role="viewer",
        replacement_annotator_id=viewer.id,
        replacement_reviewer_id=None,
    )
    assert "invalid_replacement_annotator" in invalid_replacement["blockers"]
    # The invalid receiver must not clear the active-lock blocker.
    assert "active_locks" in invalid_replacement["blockers"]


async def test_last_reviewer_coverage_includes_unassigned_review_work(
    db_session: AsyncSession,
):
    """Demoting the last reviewer is blocked by any remaining review work."""

    owner = await create_user(
        db_session, "project_admin", f"cov-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    project = await create_project(db_session, owner_id=owner.id, name="Coverage QA")
    reviewer_user = await create_user(
        db_session, "employee", f"cov-qa-{uuid.uuid4()}@test.local", "QA"
    )
    other_reviewer = await create_user(
        db_session, "employee", f"cov-qa2-{uuid.uuid4()}@test.local", "QA2"
    )
    member = await _add_member(
        db_session,
        project_id=project.id,
        user=reviewer_user,
        role="reviewer",
        assigned_by=owner.id,
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=other_reviewer,
        role="reviewer",
        assigned_by=owner.id,
    )

    owned_review = await create_task(db_session, project_id=project.id, status="review")
    owned_review.reviewer_id = reviewer_user.id
    unassigned_review = await create_task(
        db_session, project_id=project.id, status="review"
    )
    await db_session.flush()
    assert unassigned_review.reviewer_id is None

    # With another eligible reviewer left, coverage is not lost, but the
    # target-owned review work still needs an explicit handoff.
    with_peer = await preview_role_change(
        db_session,
        project=project,
        actor=owner,
        member_id=member.id,
        target_role="annotator",
        replacement_annotator_id=None,
        replacement_reviewer_id=None,
    )
    assert "unfinished_review_work" in with_peer["blockers"]
    assert "reviewer_coverage_lost" not in with_peer["blockers"]

    # Removing the alternate reviewer makes the member the last eligible one.
    alternate = (
        await db_session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == other_reviewer.id,
            )
        )
    ).scalar_one()
    await remove_member(
        db_session, project=project, actor=owner, member_id=alternate.id
    )

    last = await preview_role_change(
        db_session,
        project=project,
        actor=owner,
        member_id=member.id,
        target_role="annotator",
        replacement_annotator_id=None,
        replacement_reviewer_id=None,
    )
    assert "reviewer_coverage_lost" in last["blockers"]

    covered = await preview_role_change(
        db_session,
        project=project,
        actor=owner,
        member_id=member.id,
        target_role="annotator",
        replacement_annotator_id=None,
        replacement_reviewer_id=other_reviewer.id,
    )
    # The alternate membership was removed, so it is no longer a valid receiver.
    assert "invalid_replacement_reviewer" in covered["blockers"]


async def test_reviewer_coverage_checks_frozen_evidence_of_remaining_reviewer(
    db_session: AsyncSession,
):
    """A remaining reviewer membership is not coverage when that reviewer is
    evidence-blocked (annotator submitted the task, then switched to reviewer)."""

    owner = await create_user(
        db_session, "project_admin", f"evc-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    project = await create_project(db_session, owner_id=owner.id, name="Evidence QA")
    reviewer_user = await create_user(
        db_session, "employee", f"evc-qa-{uuid.uuid4()}@test.local", "QA"
    )
    other_reviewer = await create_user(
        db_session, "employee", f"evc-qa2-{uuid.uuid4()}@test.local", "QA2"
    )
    member = await _add_member(
        db_session,
        project_id=project.id,
        user=reviewer_user,
        role="reviewer",
        assigned_by=owner.id,
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user=other_reviewer,
        role="reviewer",
        assigned_by=owner.id,
    )

    # Open-pool review work submitted by the remaining reviewer: their claim
    # would be denied with self_review_denied even though the membership exists.
    unassigned = await create_task(db_session, project_id=project.id, status="review")
    unassigned.review_round_id = uuid.uuid4()
    unassigned.annotation_contributor_ids = [str(other_reviewer.id)]
    unassigned.review_contributor_ids = [str(other_reviewer.id)]
    unassigned.review_submitter_id = other_reviewer.id
    await db_session.flush()

    preview = await preview_role_change(
        db_session,
        project=project,
        actor=owner,
        member_id=member.id,
        target_role="annotator",
        replacement_annotator_id=None,
        replacement_reviewer_id=None,
    )
    assert "reviewer_coverage_lost" in preview["blockers"]
    details = [
        entry
        for entry in preview["blocker_details"]
        if entry.get("code") == "reviewer_coverage_lost"
    ]
    assert details and str(unassigned.id) in details[0].get("task_ids", [])

    # A replacement receiver restores coverage.
    covered = await preview_role_change(
        db_session,
        project=project,
        actor=owner,
        member_id=member.id,
        target_role="annotator",
        replacement_annotator_id=None,
        replacement_reviewer_id=other_reviewer.id,
    )
    assert "reviewer_coverage_lost" not in covered["blockers"]


async def test_removal_ignores_target_role_pairing_and_allows_idle_admin(
    db_session: AsyncSession,
):
    """Removal never grants a role, so an idle administrative member is removable."""

    owner = await create_user(
        db_session, "project_admin", f"adm-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    project = await create_project(db_session, owner_id=owner.id, name="Admin Remove")
    admin_user = await create_user(
        db_session, "project_admin", f"adm-mem-{uuid.uuid4()}@test.local", "AdminMem"
    )
    member = await _add_member(
        db_session,
        project_id=project.id,
        user=admin_user,
        role="viewer",
        assigned_by=owner.id,
    )

    preview = await preview_role_change(
        db_session,
        project=project,
        actor=owner,
        member_id=member.id,
        target_role="viewer",
        replacement_annotator_id=None,
        replacement_reviewer_id=None,
    )
    assert "target_role_incompatible" in preview["blockers"]

    # Removal does not change the platform role, so the idle admin is removable.
    await remove_member(db_session, project=project, actor=owner, member_id=member.id)
    remaining = await db_session.scalar(
        select(ProjectMember).where(ProjectMember.id == member.id)
    )
    assert remaining is None


async def test_removal_allows_idle_disabled_member(db_session: AsyncSession):
    """A disabled idle membership must not become impossible to remove."""

    owner = await create_user(
        db_session, "project_admin", f"dis-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    project = await create_project(
        db_session, owner_id=owner.id, name="Disabled Remove"
    )
    disabled_user = await create_user(
        db_session, "employee", f"dis-mem-{uuid.uuid4()}@test.local", "Disabled"
    )
    member = await _add_member(
        db_session,
        project_id=project.id,
        user=disabled_user,
        role="annotator",
        assigned_by=owner.id,
    )
    disabled_user.is_active = False
    await db_session.flush()

    await remove_member(db_session, project=project, actor=owner, member_id=member.id)
    remaining = await db_session.scalar(
        select(ProjectMember).where(ProjectMember.id == member.id)
    )
    assert remaining is None
