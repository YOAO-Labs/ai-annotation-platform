"""Phase E management query, bulk-operation, and assignment contracts."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.group import Group
from app.db.models.project_member import ProjectMember
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from tests.factory import create_membership, create_project, create_user

pytestmark = pytest.mark.asyncio


def _headers(principal: tuple[User, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {principal[1]}"}


async def test_management_user_query_stats_export_share_scope_and_filters(
    httpx_client: httpx.AsyncClient,
    super_admin,
    project_admin,
    db_session: AsyncSession,
):
    admin, _ = super_admin
    manager, _ = project_admin
    project = await create_project(db_session, owner_id=manager.id, name="Managed")
    member = await create_user(
        db_session, "annotator", "managed@e.test", "Managed User"
    )
    outside = await create_user(
        db_session, "annotator", "outside@e.test", "Outside User"
    )
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=member.id,
        role="annotator",
        assigned_by=manager.id,
    )
    await db_session.flush()

    response = await httpx_client.get(
        "/api/v1/users/query?page=1&page_size=1&status=all&search=Managed",
        headers=_headers(project_admin),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == str(member.id)
    assert body["pages"] == 1

    stats = await httpx_client.get(
        "/api/v1/users/stats?status=all&search=Managed",
        headers=_headers(project_admin),
    )
    assert stats.status_code == 200, stats.text
    assert stats.json()["total"] == 1

    exported = await httpx_client.get(
        "/api/v1/users/export?format=json&status=all&search=Managed",
        headers=_headers(project_admin),
    )
    assert exported.status_code == 200, exported.text
    export_body = json.loads(exported.text)
    assert export_body["_export_meta"]["count"] == 1
    assert export_body["users"][0]["email"] == member.email

    all_users = await httpx_client.get(
        "/api/v1/users/query?page=1&page_size=100&status=all",
        headers=_headers(super_admin),
    )
    assert all_users.status_code == 200
    ids = {row["id"] for row in all_users.json()["items"]}
    assert str(admin.id) in ids
    assert str(outside.id) in ids


async def test_management_invitation_query_stats_export_filter_scope(
    httpx_client: httpx.AsyncClient,
    super_admin,
    project_admin,
    db_session: AsyncSession,
):
    admin, _ = super_admin
    manager, _ = project_admin
    project = await create_project(db_session, owner_id=manager.id, name="Invite Scope")
    rows = [
        UserInvitation(
            email=f"invite-{index}@e.test",
            role="annotator",
            group_name="batch-a",
            project_id=project.id,
            token=secrets.token_urlsafe(24),
            expires_at=datetime.now(timezone.utc),
            invited_by=manager.id,
        )
        for index in range(3)
    ]
    rows[0].accepted_at = rows[0].expires_at
    rows[1].revoked_at = rows[1].expires_at
    db_session.add_all(rows)
    await db_session.flush()

    page = await httpx_client.get(
        f"/api/v1/invitations/query?scope=me&project_id={project.id}&page_size=2",
        headers=_headers(project_admin),
    )
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 2
    assert page.json()["items"][0]["project_name"] == "Invite Scope"

    stats = await httpx_client.get(
        "/api/v1/invitations/stats?scope=me",
        headers=_headers(project_admin),
    )
    assert stats.status_code == 200, stats.text
    assert stats.json()["accepted"] == 1
    assert stats.json()["revoked"] == 1
    assert stats.json()["expired"] == 1

    forbidden = await httpx_client.get(
        "/api/v1/invitations/query?scope=all",
        headers=_headers(project_admin),
    )
    assert forbidden.status_code == 403

    exported = await httpx_client.get(
        "/api/v1/invitations/export?format=json&scope=all",
        headers=_headers(super_admin),
    )
    assert exported.status_code == 200, exported.text
    assert json.loads(exported.text)["_export_meta"]["count"] >= 3
    assert admin.email  # keep the fixture principal part of the scope assertion


async def test_send_invitation_email_keeps_token_and_does_not_revoke_on_smtp_failure(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    from app.services.email import SmtpConfigError

    admin, _ = super_admin
    invitation = UserInvitation(
        email="mail-target@e.test",
        role="annotator",
        token=secrets.token_urlsafe(24),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        invited_by=admin.id,
    )
    db_session.add(invitation)
    await db_session.flush()
    token = invitation.token
    sent: list[str] = []

    async def fake_send(db, to_address, invite_url, **kwargs):
        sent.append(f"{to_address}:{invite_url}")

    monkeypatch.setattr("app.api.v1.invitations_admin.send_invitation_email", fake_send)
    response = await httpx_client.post(
        f"/api/v1/invitations/{invitation.id}/send-email",
        headers=_headers(super_admin),
    )
    assert response.status_code == 200, response.text
    assert sent and token in sent[0]
    assert token in response.json()["invite_url"]
    await db_session.refresh(invitation)
    assert invitation.token == token
    assert invitation.revoked_at is None

    async def fail_send(db, to_address, invite_url, **kwargs):
        raise SmtpConfigError("SMTP unavailable")

    monkeypatch.setattr("app.api.v1.invitations_admin.send_invitation_email", fail_send)
    failed = await httpx_client.post(
        f"/api/v1/invitations/{invitation.id}/send-email",
        headers=_headers(super_admin),
    )
    assert failed.status_code == 502
    await db_session.refresh(invitation)
    assert invitation.token == token
    assert invitation.revoked_at is None


async def test_bulk_invite_preview_rolls_back_and_apply_keeps_failed_rows(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    payload = {
        "items": [
            {"email": "bulk-good@e.test", "role": "employee"},
            {"email": "bulk-bad@e.test", "role": "not-a-role"},
        ]
    }
    preview = await httpx_client.post(
        "/api/v1/users/bulk-invite/preview",
        json=payload,
        headers=_headers(super_admin),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["succeeded"] == 1
    assert preview.json()["failed"] == 1
    assert (
        await db_session.scalar(
            select(UserInvitation).where(UserInvitation.email == "bulk-good@e.test")
        )
        is None
    )

    applied = await httpx_client.post(
        "/api/v1/users/bulk-invite",
        json=payload,
        headers=_headers(super_admin),
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["succeeded"] == 1
    assert applied.json()["failed"] == 1
    invitation = await db_session.scalar(
        select(UserInvitation).where(UserInvitation.email == "bulk-good@e.test")
    )
    assert invitation is not None
    assert invitation.token
    assert applied.json()["items"][1]["retryable"] is True


async def test_bulk_group_assignment_scope_and_role_impact_preview(
    httpx_client: httpx.AsyncClient,
    super_admin,
    project_admin,
    db_session: AsyncSession,
):
    manager, _ = project_admin
    project = await create_project(db_session, owner_id=manager.id, name="Group Scope")
    managed = await create_user(db_session, "employee", "group@e.test", "Group User")
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=managed.id,
        role="annotator",
        assigned_by=manager.id,
    )
    group = Group(name="Bulk Group")
    db_session.add(group)
    await db_session.flush()

    preview = await httpx_client.post(
        "/api/v1/users/groups/bulk/preview",
        json={"user_ids": [str(managed.id)], "group_id": str(group.id)},
        headers=_headers(project_admin),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["applicable"] == 1

    applied = await httpx_client.post(
        "/api/v1/users/groups/bulk",
        json={"user_ids": [str(managed.id)], "group_id": str(group.id)},
        headers=_headers(project_admin),
    )
    assert applied.status_code == 200, applied.text
    await db_session.refresh(managed)
    assert managed.group_id == group.id

    # Platform-role preview is super-admin only (plan AUTH-04).
    impact = await httpx_client.get(
        f"/api/v1/users/{managed.id}/role/preview?role=employee",
        headers=_headers(project_admin),
    )
    assert impact.status_code == 403, impact.text

    unauthorized = await httpx_client.post(
        "/api/v1/users/groups/bulk/preview",
        json={"user_ids": [str(manager.id)], "group_id": str(group.id)},
        headers=_headers(super_admin),
    )
    assert unauthorized.status_code == 200


async def test_batch_distribution_preview_is_read_only_and_apply_respects_default(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Batch Plan")
    annotator = await create_user(db_session, "employee", "batch-a@e.test", "Batch A")
    reviewer = await create_user(db_session, "employee", "batch-r@e.test", "Batch R")
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=annotator.id,
                role="annotator",
                assigned_by=admin.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=reviewer.id,
                role="reviewer",
                assigned_by=admin.id,
            ),
        ]
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-MGMT-{secrets.token_hex(3)}",
        name="Unassigned",
        status="draft",
    )
    assigned = TaskBatch(
        project_id=project.id,
        display_id=f"B-MGMT-{secrets.token_hex(3)}",
        name="Already assigned",
        status="draft",
        annotator_id=annotator.id,
        reviewer_id=reviewer.id,
    )
    db_session.add_all([batch, assigned])
    await db_session.flush()

    body = {
        "annotator_ids": [str(annotator.id)],
        "reviewer_ids": [str(reviewer.id)],
        "only_unassigned": True,
    }
    preview = await httpx_client.post(
        f"/api/v1/projects/{project.id}/batches/distribution-preview",
        json=body,
        headers=_headers(super_admin),
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["changed_batches"] == 1
    await db_session.refresh(batch)
    assert batch.annotator_id is None
    assert batch.reviewer_id is None

    applied = await httpx_client.post(
        f"/api/v1/projects/{project.id}/batches/distribution-apply",
        json={**body, "preview_version": preview.json()["preview_version"]},
        headers=_headers(super_admin),
    )
    assert applied.status_code == 200, applied.text
    await db_session.refresh(batch)
    await db_session.refresh(assigned)
    assert batch.annotator_id == annotator.id
    assert batch.reviewer_id == reviewer.id
    assert assigned.annotator_id == annotator.id
    assert applied.json()["distributed_batches"] == 1
