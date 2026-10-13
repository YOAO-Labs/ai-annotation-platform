"""Invitation authorization, lifecycle, and group-integrity regressions."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.group import Group
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from app.schemas.invitation import InvitationCreate
from app.services.invitation import (
    InvitationService,
    _lock_invitation_email,
    _resolve_or_create_group,
)

pytestmark = pytest.mark.asyncio


def _headers(principal: tuple[User, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {principal[1]}"}


async def test_invitation_create_normalizes_group_name() -> None:
    payload = InvitationCreate(
        email="schema@invite.test", role="annotator", group_name="  Team A  "
    )

    assert payload.group_name == "Team A"


async def test_invitation_create_normalizes_blank_group_name() -> None:
    payload = InvitationCreate(
        email="blank-schema@invite.test", role="annotator", group_name="   "
    )

    assert payload.group_name is None


async def _invitation(
    db: AsyncSession,
    *,
    invited_by: User,
    email: str,
    role: str = "employee",
    group_name: str | None = None,
    revoked: bool = False,
) -> UserInvitation:
    now = datetime.now(timezone.utc)
    invitation = UserInvitation(
        email=email,
        role=role,
        group_name=group_name,
        token=secrets.token_urlsafe(32),
        expires_at=now + timedelta(days=1),
        invited_by=invited_by.id,
        revoked_at=now if revoked else None,
    )
    db.add(invitation)
    await db.flush()
    return invitation


@pytest.mark.parametrize("role", ["super_admin", "project_admin"])
async def test_project_admin_cannot_invite_privileged_roles(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
    role: str,
):
    email = f"blocked-{role}@invite.test"
    response = await httpx_client.post(
        "/api/v1/users/invite",
        json={"email": email, "role": role},
        headers=_headers(project_admin),
    )

    assert response.status_code == 403
    count = await db_session.scalar(
        select(func.count(UserInvitation.id)).where(UserInvitation.email == email)
    )
    assert count == 0


@pytest.mark.parametrize("role", ["employee", "viewer"])
async def test_project_admin_can_invite_supported_roles(
    httpx_client: httpx.AsyncClient,
    project_admin,
    role: str,
):
    response = await httpx_client.post(
        "/api/v1/users/invite",
        json={"email": f"allowed-{role}@invite.test", "role": role},
        headers=_headers(project_admin),
    )

    assert response.status_code == 201


@pytest.mark.parametrize("role", ["super_admin", "project_admin"])
async def test_super_admin_can_invite_privileged_roles(
    httpx_client: httpx.AsyncClient,
    super_admin,
    role: str,
):
    response = await httpx_client.post(
        "/api/v1/users/invite",
        json={"email": f"super-{role}@invite.test", "role": role},
        headers=_headers(super_admin),
    )

    assert response.status_code == 201


async def test_project_admin_does_not_expire_another_inviter_record(
    httpx_client: httpx.AsyncClient,
    super_admin,
    project_admin,
    db_session: AsyncSession,
):
    invitation = await _invitation(
        db_session,
        invited_by=super_admin[0],
        email="shared-email@invite.test",
        role="reviewer",
    )
    original_expiry = invitation.expires_at

    response = await httpx_client.post(
        "/api/v1/users/invite",
        json={"email": invitation.email, "role": "employee"},
        headers=_headers(project_admin),
    )

    assert response.status_code == 201
    await db_session.refresh(invitation)
    assert invitation.expires_at == original_expiry


async def test_invitation_mutations_use_database_locks() -> None:
    now = datetime.now(timezone.utc)
    actor = User(
        id=uuid.uuid4(),
        email="locking-admin@invite.test",
        name="Locking Admin",
        password_hash="unused",
        role="project_admin",
        is_active=True,
    )
    invitation = UserInvitation(
        id=uuid.uuid4(),
        email="locking-target@invite.test",
        role="reviewer",
        token=secrets.token_urlsafe(32),
        expires_at=now + timedelta(days=1),
        invited_by=actor.id,
    )

    resolve_result = MagicMock()
    resolve_result.scalar_one_or_none.return_value = invitation
    resolve_db = AsyncMock(spec=AsyncSession)
    resolve_db.execute.return_value = resolve_result
    resolve_db.scalar.return_value = actor
    await InvitationService.resolve(resolve_db, invitation.token, for_update=True)
    resolve_query = resolve_db.execute.await_args.args[0]

    manage_db = AsyncMock(spec=AsyncSession)
    manage_db.scalar.return_value = invitation
    await InvitationService.revoke(manage_db, invitation.id, actor=actor)
    manage_query = manage_db.scalar.await_args.args[0]

    email_db = AsyncMock(spec=AsyncSession)
    await _lock_invitation_email(email_db, invitation.email)
    email_query = email_db.execute.await_args.args[0]

    group = Group(id=uuid.uuid4(), name="Locking Group")
    group_db = AsyncMock(spec=AsyncSession)
    group_db.scalar.return_value = group
    assert await _resolve_or_create_group(group_db, group.name) is group
    group_query = group_db.scalar.await_args.args[0]

    assert "FOR UPDATE" in str(resolve_query.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in str(manage_query.compile(dialect=postgresql.dialect()))
    assert "pg_advisory_xact_lock" in str(
        email_query.compile(dialect=postgresql.dialect())
    )
    assert "FOR SHARE" in str(group_query.compile(dialect=postgresql.dialect()))


async def test_revoked_invitation_cannot_be_resolved(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    invitation = await _invitation(
        db_session,
        invited_by=super_admin[0],
        email="revoked-resolve@invite.test",
        revoked=True,
    )

    response = await httpx_client.get(f"/api/v1/auth/invitations/{invitation.token}")

    assert response.status_code == 410
    assert response.json()["detail"] == "该邀请已撤销"


async def test_revoked_invitation_cannot_register(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    invitation = await _invitation(
        db_session,
        invited_by=super_admin[0],
        email="revoked-register@invite.test",
        role="reviewer",
        revoked=True,
    )

    response = await httpx_client.post(
        "/api/v1/auth/register",
        json={
            "token": invitation.token,
            "name": "Revoked",
            "password": "Strong123",
        },
    )

    assert response.status_code == 410
    created = await db_session.scalar(
        select(User).where(User.email == invitation.email)
    )
    assert created is None


@pytest.mark.parametrize("operation", ["revoke", "resend"])
async def test_project_admin_cannot_manage_another_inviter_record(
    httpx_client: httpx.AsyncClient,
    super_admin,
    project_admin,
    db_session: AsyncSession,
    operation: str,
):
    invitation = await _invitation(
        db_session,
        invited_by=super_admin[0],
        email=f"foreign-{operation}@invite.test",
        role="super_admin",
    )
    original_token = invitation.token
    path = f"/api/v1/invitations/{invitation.id}"

    if operation == "resend":
        response = await httpx_client.post(
            f"{path}/resend", headers=_headers(project_admin)
        )
    else:
        response = await httpx_client.delete(path, headers=_headers(project_admin))

    assert response.status_code == 404
    await db_session.refresh(invitation)
    assert invitation.token == original_token
    assert invitation.revoked_at is None


@pytest.mark.parametrize("operation", ["revoke", "resend"])
async def test_project_admin_can_manage_own_supported_invitation(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
    operation: str,
):
    invitation = await _invitation(
        db_session,
        invited_by=project_admin[0],
        email=f"own-{operation}@invite.test",
        role="employee",
    )
    original_token = invitation.token
    path = f"/api/v1/invitations/{invitation.id}"

    if operation == "resend":
        response = await httpx_client.post(
            f"{path}/resend", headers=_headers(project_admin)
        )
        assert response.status_code == 200
    else:
        response = await httpx_client.delete(path, headers=_headers(project_admin))
        assert response.status_code == 204

    await db_session.refresh(invitation)
    if operation == "resend":
        assert invitation.token != original_token
    else:
        assert invitation.revoked_at is not None
        assert invitation.expires_at == invitation.revoked_at


async def test_super_admin_can_revoke_another_inviter_record(
    httpx_client: httpx.AsyncClient,
    super_admin,
    project_admin,
    db_session: AsyncSession,
):
    invitation = await _invitation(
        db_session,
        invited_by=project_admin[0],
        email="global-revoke@invite.test",
    )

    response = await httpx_client.delete(
        f"/api/v1/invitations/{invitation.id}", headers=_headers(super_admin)
    )

    assert response.status_code == 204
    await db_session.refresh(invitation)
    assert invitation.revoked_at is not None
    assert invitation.expires_at == invitation.revoked_at


async def test_project_admin_cannot_resend_legacy_privileged_invitation(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    invitation = await _invitation(
        db_session,
        invited_by=project_admin[0],
        email="legacy-privileged@invite.test",
        role="super_admin",
    )
    original_token = invitation.token

    response = await httpx_client.post(
        f"/api/v1/invitations/{invitation.id}/resend",
        headers=_headers(project_admin),
    )

    assert response.status_code == 403
    await db_session.refresh(invitation)
    assert invitation.token == original_token


async def test_resend_normalizes_legacy_staff_role(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    """Resending a pre-cutover staff invitation normalizes the role like
    migration 0174 does, so acceptance creates a usable employee account."""

    invitation = await _invitation(
        db_session,
        invited_by=super_admin[0],
        email="legacy-resend@invite.test",
        role="annotator",
    )

    response = await httpx_client.post(
        f"/api/v1/invitations/{invitation.id}/resend",
        headers=_headers(super_admin),
    )
    assert response.status_code == 200, response.text

    await db_session.refresh(invitation)
    assert invitation.role == "employee"

    accept = await httpx_client.post(
        "/api/v1/auth/register",
        json={
            "token": invitation.token,
            "name": "Legacy Resend User",
            "password": "Strong123",
        },
    )
    assert accept.status_code == 201, accept.text
    user = await db_session.scalar(select(User).where(User.email == invitation.email))
    assert user is not None
    assert user.role == "employee"


async def test_accept_refuses_unconverted_legacy_role(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    """An unconverted legacy invitation fails closed instead of creating an
    account the authorization model rejects."""

    invitation = await _invitation(
        db_session,
        invited_by=super_admin[0],
        email="legacy-accept@invite.test",
        role="annotator",
    )

    accept = await httpx_client.post(
        "/api/v1/auth/register",
        json={
            "token": invitation.token,
            "name": "Legacy Accept User",
            "password": "Strong123",
        },
    )
    assert accept.status_code == 410
    user = await db_session.scalar(select(User).where(User.email == invitation.email))
    assert user is None


async def test_legacy_privileged_invitation_requires_active_super_admin_issuer(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    invitation = await _invitation(
        db_session,
        invited_by=project_admin[0],
        email="legacy-privileged-resolve@invite.test",
        role="project_admin",
    )

    response = await httpx_client.get(f"/api/v1/auth/invitations/{invitation.token}")

    assert response.status_code == 410
    assert response.json()["detail"] == "该邀请权限已失效"


async def test_accept_invitation_creates_and_assigns_group(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    invite_response = await httpx_client.post(
        "/api/v1/users/invite",
        json={
            "email": "new-group@invite.test",
            "role": "employee",
            "group_name": "  New Group  ",
        },
        headers=_headers(super_admin),
    )
    assert invite_response.status_code == 201

    register_response = await httpx_client.post(
        "/api/v1/auth/register",
        json={
            "token": invite_response.json()["token"],
            "name": "New Group User",
            "password": "Strong123",
        },
    )

    assert register_response.status_code == 201
    user = await db_session.scalar(
        select(User).where(User.email == "new-group@invite.test")
    )
    group = await db_session.scalar(select(Group).where(Group.name == "New Group"))
    assert user is not None
    assert group is not None
    assert user.group_id == group.id
    assert user.group_name == group.name


async def test_accept_invitation_reuses_existing_group(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    group = Group(name="Existing Group")
    db_session.add(group)
    await db_session.flush()
    invitation = await _invitation(
        db_session,
        invited_by=super_admin[0],
        email="existing-group@invite.test",
        group_name="Existing Group",
    )

    response = await httpx_client.post(
        "/api/v1/auth/register",
        json={
            "token": invitation.token,
            "name": "Existing Group User",
            "password": "Strong123",
        },
    )

    assert response.status_code == 201
    user = await db_session.scalar(select(User).where(User.email == invitation.email))
    group_count = await db_session.scalar(
        select(func.count(Group.id)).where(Group.name == group.name)
    )
    assert user is not None
    assert user.group_id == group.id
    assert group_count == 1


async def test_invitation_normalizes_blank_group_to_none(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    invite_response = await httpx_client.post(
        "/api/v1/users/invite",
        json={
            "email": "blank-group@invite.test",
            "role": "employee",
            "group_name": "   ",
        },
        headers=_headers(super_admin),
    )
    assert invite_response.status_code == 201
    invitation = await db_session.scalar(
        select(UserInvitation).where(UserInvitation.email == "blank-group@invite.test")
    )
    assert invitation is not None
    assert invitation.group_name is None


async def test_invitation_rejects_group_name_over_100_characters(
    httpx_client: httpx.AsyncClient,
    super_admin,
):
    response = await httpx_client.post(
        "/api/v1/users/invite",
        json={
            "email": "long-group@invite.test",
            "role": "employee",
            "group_name": "x" * 101,
        },
        headers=_headers(super_admin),
    )

    assert response.status_code == 422


async def test_accept_invitation_rejects_legacy_group_name_over_100_characters(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    invitation = await _invitation(
        db_session,
        invited_by=super_admin[0],
        email="legacy-long-group@invite.test",
        group_name="x" * 101,
    )

    response = await httpx_client.post(
        "/api/v1/auth/register",
        json={
            "token": invitation.token,
            "name": "Legacy Long Group",
            "password": "Strong123",
        },
    )

    assert response.status_code == 409
    created = await db_session.scalar(
        select(User).where(User.email == invitation.email)
    )
    assert created is None


@pytest.mark.parametrize("offset_us", [-1, 0, 1])
async def test_rolling_invitation_quota_boundary_is_inclusive_and_actor_scoped(
    db_session, super_admin, project_admin, monkeypatch, offset_us
):
    from fastapi import HTTPException
    from app.services.system_settings_service import SystemSettingsService
    import app.services.invitation as invitation_module

    now = datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(invitation_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(SystemSettingsService, "get", AsyncMock(return_value=1))
    actor, _ = super_admin
    other, _ = project_admin
    row = await _invitation(db_session, invited_by=actor, email="boundary@invite.test")
    row.created_at = now - timedelta(hours=24) + timedelta(microseconds=offset_us)
    other_row = await _invitation(
        db_session, invited_by=other, email="other-quota@invite.test"
    )
    other_row.created_at = now
    await db_session.flush()
    if offset_us >= 0:
        with pytest.raises(HTTPException) as raised:
            await InvitationService.check_daily_limit(db_session, actor.id)
        assert raised.value.status_code == 429
    else:
        await InvitationService.check_daily_limit(db_session, actor.id)
