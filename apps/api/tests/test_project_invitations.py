"""Target-project invitation acceptance and scope regressions."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from tests.factory import create_project, create_user
from app.services.invitation import InvitationService

pytestmark = pytest.mark.asyncio


def _headers(principal: tuple[User, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {principal[1]}"}


async def _target_invitation(
    db: AsyncSession,
    *,
    inviter: User,
    email: str,
    project_id,
    role: str = "annotator",
) -> UserInvitation:
    invitation = UserInvitation(
        email=email,
        role="employee",
        project_role=role if project_id is not None else None,
        project_id=project_id,
        token=secrets.token_urlsafe(32),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        invited_by=inviter.id,
    )
    db.add(invitation)
    await db.flush()
    return invitation


async def test_new_account_acceptance_creates_project_member_atomically(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Road QA")

    invite = await httpx_client.post(
        "/api/v1/users/invite",
        json={
            "email": "project-new@invite.test",
            "role": "employee",
            "project_member_role": "annotator",
            "project_id": str(project.id),
        },
        headers=_headers(super_admin),
    )
    assert invite.status_code == 201, invite.text
    assert invite.json()["project_name"] == "Road QA"

    registered = await httpx_client.post(
        "/api/v1/auth/register",
        json={
            "token": invite.json()["token"],
            "name": "Project New",
            "password": "Strong123",
        },
    )
    assert registered.status_code == 201, registered.text
    user = await db_session.scalar(
        select(User).where(User.email == "project-new@invite.test")
    )
    member = await db_session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == user.id,
        )
    )
    assert user is not None
    # A project invitation normalises the platform role to employee while the
    # membership keeps the requested project responsibility.
    assert user.role == "employee"
    assert member is not None
    assert member.role == "annotator"
    assert registered.json()["acceptance"]["next_action"] == "wait_for_allocation"


async def test_existing_account_must_explicitly_confirm_matching_project_invitation(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    admin, _ = super_admin
    existing = await create_user(
        db_session, "employee", "existing-project@invite.test", "Existing"
    )
    project = await create_project(db_session, owner_id=admin.id, name="Existing QA")
    created = await httpx_client.post(
        "/api/v1/users/invite",
        json={
            "email": existing.email,
            "project_id": str(project.id),
            "role": "employee",
            "project_member_role": "reviewer",
        },
        headers=_headers(super_admin),
    )
    assert created.status_code == 201, created.text
    invitation_token = created.json()["token"]
    token = __import__(
        "app.core.security", fromlist=["create_access_token"]
    ).create_access_token(subject=str(existing.id), role=existing.role)

    accepted = await httpx_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": invitation_token},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["acceptance"]["project_name"] == "Existing QA"
    assert accepted.json()["user"]["role"] == "employee"
    assert await db_session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == existing.id,
        )
    )

    repeated = await httpx_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": invitation_token},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert repeated.status_code == 410


async def test_existing_account_cannot_consume_legacy_invitation(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    admin, _ = super_admin
    existing = await create_user(
        db_session, "annotator", "legacy-existing@invite.test", "Legacy Existing"
    )
    invitation = UserInvitation(
        email=existing.email,
        role="annotator",
        token=secrets.token_urlsafe(32),
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        invited_by=admin.id,
    )
    db_session.add(invitation)
    await db_session.flush()
    token = __import__(
        "app.core.security", fromlist=["create_access_token"]
    ).create_access_token(subject=str(existing.id), role=existing.role)

    response = await httpx_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": invitation.token},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    await db_session.refresh(invitation)
    assert invitation.accepted_at is None


async def test_existing_employee_accepts_different_project_role_without_platform_change(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    """An employee may hold a different project responsibility elsewhere.

    The platform role is never silently rewritten; only the membership is added.
    """

    admin, _ = super_admin
    existing = await create_user(
        db_session, "employee", "cross-role@invite.test", "Cross Role"
    )
    project = await create_project(db_session, owner_id=admin.id, name="Role QA")
    invitation = await _target_invitation(
        db_session,
        inviter=admin,
        email=existing.email,
        project_id=project.id,
        role="reviewer",
    )
    token = __import__(
        "app.core.security", fromlist=["create_access_token"]
    ).create_access_token(subject=str(existing.id), role=existing.role)

    response = await httpx_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": invitation.token},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["acceptance"]["project_member_role"] == "reviewer"
    await db_session.refresh(existing)
    assert existing.role == "employee"

    member = await db_session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == existing.id,
        )
    )
    assert member is not None
    assert member.role == "reviewer"


async def test_deleted_project_and_transferred_project_fail_closed(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    owner, _ = project_admin
    other_owner = await create_user(
        db_session, "project_admin", "other-owner@invite.test", "Other Owner"
    )
    project = await create_project(db_session, owner_id=owner.id, name="Transfer QA")
    invitation = await _target_invitation(
        db_session,
        inviter=owner,
        email="transfer-target@invite.test",
        project_id=project.id,
    )

    project.owner_id = other_owner.id
    await db_session.flush()
    transferred = await httpx_client.get(f"/api/v1/auth/invitations/{invitation.token}")
    assert transferred.status_code == 410
    assert "不再管理" in transferred.json()["detail"]

    project.owner_id = owner.id
    await db_session.flush()
    await db_session.delete(project)
    await db_session.flush()
    deleted = await httpx_client.get(f"/api/v1/auth/invitations/{invitation.token}")
    assert deleted.status_code == 410
    assert "已删除" in deleted.json()["detail"]


async def test_deactivated_project_inviter_cannot_accept_target_invitation(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    owner, _ = project_admin
    project = await create_project(db_session, owner_id=owner.id, name="Inactive QA")
    invitation = await _target_invitation(
        db_session,
        inviter=owner,
        email="inactive-target@invite.test",
        project_id=project.id,
    )
    owner.is_active = False
    await db_session.flush()

    response = await httpx_client.get(f"/api/v1/auth/invitations/{invitation.token}")
    assert response.status_code == 410
    assert "停用" in response.json()["detail"]


async def test_create_refreshes_stale_authenticated_inviter(db_session, super_admin):
    admin, _ = super_admin
    await db_session.execute(
        update(User)
        .where(User.id == admin.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    assert admin.is_active is True
    with pytest.raises(HTTPException) as error:
        await InvitationService.create(
            db_session,
            email="stale-admin@invite.test",
            role="annotator",
            group_name=None,
            actor=admin,
        )
    assert error.value.status_code == 403


async def test_legacy_invitation_rechecks_stale_inviter(db_session, super_admin):
    admin, _ = super_admin
    invitation = await _target_invitation(
        db_session, inviter=admin, email="stale-legacy@invite.test", project_id=None
    )
    await db_session.execute(
        update(User)
        .where(User.id == admin.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    with pytest.raises(HTTPException) as error:
        await InvitationService.accept(
            db_session, token=invitation.token, name="Blocked", password="Strong123"
        )
    assert error.value.status_code == 410
    assert (
        await db_session.scalar(select(User.id).where(User.email == invitation.email))
        is None
    )
