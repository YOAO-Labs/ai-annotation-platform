"""Target-project invitation acceptance and scope regressions."""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from tests.factory import create_project, create_user

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
    commit_seed: bool = False,
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
    if commit_seed:
        await db.commit()
    return invitation


async def test_new_account_acceptance_creates_project_member_atomically(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    admin, _ = super_admin
    project = await create_project(db_session, owner_id=admin.id, name="Road QA")

    await db_session.commit()
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

    await db_session.commit()
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
    assert member.assigned_by == admin.id
    await db_session.commit()
    authenticated = await httpx_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {registered.json()['access_token']}"},
    )
    assert authenticated.status_code == 200
    assert authenticated.json()["id"] == str(user.id)
    assert registered.json()["acceptance"]["next_action"] == "wait_for_allocation"
    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "user.register",
            AuditLog.target_id == str(user.id),
        )
    )
    assert audit is not None
    assert audit.status_code == 201
    accepted_invitation = await db_session.scalar(
        select(UserInvitation).where(UserInvitation.token == invite.json()["token"])
    )
    assert accepted_invitation is not None
    assert accepted_invitation.accepted_user_id == user.id


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
    await db_session.commit()
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
    token = create_access_token(subject=str(existing.id), role=existing.role)

    await db_session.commit()
    accepted = await httpx_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": invitation_token},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["acceptance"]["project_name"] == "Existing QA"
    assert accepted.json()["user"]["role"] == "employee"
    member = await db_session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == existing.id,
        )
    )
    assert member is not None
    assert member.assigned_by == admin.id
    accepted_invitation = await db_session.scalar(
        select(UserInvitation).where(UserInvitation.token == invitation_token)
    )
    assert accepted_invitation is not None
    assert accepted_invitation.accepted_user_id == existing.id
    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "user.invite_accept_existing",
            AuditLog.target_id == str(accepted_invitation.id),
        )
    )
    assert audit is not None
    await db_session.commit()
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
        db_session, "employee", "legacy-existing@invite.test", "Legacy Existing"
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
    token = create_access_token(subject=str(existing.id), role=existing.role)

    await db_session.commit()
    response = await httpx_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": invitation.token},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    await db_session.refresh(invitation)
    assert invitation.accepted_at is None


async def test_existing_account_must_match_invitation_email(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    admin, _ = super_admin
    invited = await create_user(
        db_session, "employee", "expected-email@invite.test", "Invited"
    )
    logged_in = await create_user(
        db_session, "employee", "different-email@invite.test", "Wrong Account"
    )
    project = await create_project(db_session, owner_id=admin.id)
    invitation = await _target_invitation(
        db_session,
        commit_seed=True,
        inviter=admin,
        email=invited.email,
        project_id=project.id,
        role="annotator",
    )
    token = create_access_token(subject=str(logged_in.id), role=logged_in.role)

    await db_session.commit()
    response = await httpx_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": invitation.token},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    await db_session.refresh(invitation)
    assert invitation.accepted_at is None


async def test_expired_invitation_cannot_register(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    admin, _ = super_admin
    invitation = await _target_invitation(
        db_session,
        commit_seed=True,
        inviter=admin,
        email="expired-register@invite.test",
        project_id=None,
    )
    invitation.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.flush()

    await db_session.commit()
    response = await httpx_client.post(
        "/api/v1/auth/register",
        json={
            "token": invitation.token,
            "name": "Expired",
            "password": "Strong123",
        },
    )

    assert response.status_code == 410
    await db_session.refresh(invitation)
    assert (
        await db_session.scalar(select(User.id).where(User.email == invitation.email))
        is None
    )


async def test_incompatible_existing_platform_role_cannot_accept_project_invitation(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    admin, _ = super_admin
    existing = await create_user(
        db_session, "viewer", "incompatible-project@invite.test", "Viewer"
    )
    project = await create_project(db_session, owner_id=admin.id)
    invitation = await _target_invitation(
        db_session,
        commit_seed=True,
        inviter=admin,
        email=existing.email,
        project_id=project.id,
        role="annotator",
    )
    token = create_access_token(subject=str(existing.id), role=existing.role)

    await db_session.commit()
    response = await httpx_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": invitation.token},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 409
    await db_session.refresh(invitation)
    await db_session.refresh(project)
    await db_session.refresh(existing)
    assert invitation.accepted_at is None
    assert (
        await db_session.scalar(
            select(ProjectMember.id).where(
                ProjectMember.project_id == project.id,
                ProjectMember.user_id == existing.id,
            )
        )
        is None
    )


async def test_existing_project_member_cannot_accept_duplicate_invitation(
    httpx_client: httpx.AsyncClient,
    super_admin,
    db_session: AsyncSession,
):
    admin, _ = super_admin
    existing = await create_user(
        db_session, "employee", "duplicate-project@invite.test", "Duplicate"
    )
    project = await create_project(db_session, owner_id=admin.id)
    member = ProjectMember(
        project_id=project.id,
        user_id=existing.id,
        role="annotator",
        assigned_by=admin.id,
    )
    db_session.add(member)
    invitation = await _target_invitation(
        db_session,
        commit_seed=True,
        inviter=admin,
        email=existing.email,
        project_id=project.id,
        role="annotator",
    )
    token = create_access_token(subject=str(existing.id), role=existing.role)

    await db_session.commit()
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
        commit_seed=True,
        inviter=admin,
        email=existing.email,
        project_id=project.id,
        role="reviewer",
    )
    token = create_access_token(subject=str(existing.id), role=existing.role)

    await db_session.commit()
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
        commit_seed=True,
        inviter=owner,
        email="transfer-target@invite.test",
        project_id=project.id,
    )

    owner_id = owner.id
    invitation_token = invitation.token
    invitation_email = invitation.email
    project.owner_id = other_owner.id
    await db_session.flush()
    await db_session.commit()
    transferred = await httpx_client.get(f"/api/v1/auth/invitations/{invitation_token}")
    assert transferred.status_code == 410
    await db_session.commit()
    rejected = await httpx_client.post(
        "/api/v1/auth/register",
        json={
            "token": invitation_token,
            "name": "Transfer Target",
            "password": "Strong123",
        },
    )
    assert rejected.status_code == 410
    assert (
        await db_session.scalar(select(User.id).where(User.email == invitation_email))
        is None
    )

    await db_session.refresh(project)
    project.owner_id = owner_id
    await db_session.flush()
    await db_session.delete(project)
    await db_session.flush()
    await db_session.commit()
    deleted = await httpx_client.get(f"/api/v1/auth/invitations/{invitation_token}")
    assert deleted.status_code == 410


async def test_deactivated_project_inviter_cannot_accept_target_invitation(
    httpx_client: httpx.AsyncClient,
    project_admin,
    db_session: AsyncSession,
):
    owner, _ = project_admin
    project = await create_project(db_session, owner_id=owner.id, name="Inactive QA")
    invitation = await _target_invitation(
        db_session,
        commit_seed=True,
        inviter=owner,
        email="inactive-target@invite.test",
        project_id=project.id,
    )
    accepting_user = await create_user(
        db_session, "employee", "inactive-target@invite.test", "Inactive Target"
    )
    await db_session.commit()
    token = create_access_token(
        subject=str(accepting_user.id), role=accepting_user.role
    )
    invitation_token = invitation.token
    owner.is_active = False
    await db_session.flush()

    headers = {"Authorization": f"Bearer {token}"}
    await db_session.commit()
    response = await httpx_client.get(f"/api/v1/auth/invitations/{invitation_token}")
    assert response.status_code == 410
    await db_session.commit()
    accepted = await httpx_client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": invitation_token},
        headers=headers,
    )
    assert accepted.status_code == 410


async def test_create_refreshes_stale_authenticated_inviter(
    httpx_client, db_session, super_admin
):
    admin, _ = super_admin
    await db_session.execute(
        update(User)
        .where(User.id == admin.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    assert admin.is_active is True
    await db_session.commit()
    response = await httpx_client.post(
        "/api/v1/users/invite",
        json={"email": "stale-admin@invite.test", "role": "employee"},
        headers=_headers(super_admin),
    )
    assert response.status_code == 401
    assert (
        await db_session.scalar(
            select(UserInvitation.id).where(
                UserInvitation.email == "stale-admin@invite.test"
            )
        )
        is None
    )


async def test_legacy_invitation_rechecks_stale_inviter(
    httpx_client: httpx.AsyncClient, db_session, super_admin
):
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
    await db_session.commit()
    response = await httpx_client.post(
        "/api/v1/auth/register",
        json={
            "token": invitation.token,
            "name": "Blocked",
            "password": "Strong123",
        },
    )
    assert response.status_code == 410
    await db_session.refresh(invitation)
    assert (
        await db_session.scalar(select(User.id).where(User.email == invitation.email))
        is None
    )


async def test_register_http_transaction_rolls_back_account_membership_invite_and_audit(
    test_engine,
    app_module,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as setup:
        assert (
            await setup.scalar(
                select(User.id).where(User.email == "rollback-target@invite.test")
            )
            is None
        )
        admin = await create_user(
            setup, "super_admin", "rollback-owner@invite.test", "Rollback Owner"
        )
        project = await create_project(setup, owner_id=admin.id)
        invitation = await _target_invitation(
            setup,
            inviter=admin,
            email="rollback-target@invite.test",
            project_id=project.id,
        )
        invitation_token = invitation.token
        await setup.commit()

    try:
        connection = await test_engine.connect()
        outer_transaction = await connection.begin()
        session = async_sessionmaker(
            connection, class_=AsyncSession, expire_on_commit=False
        )()
        await connection.begin_nested()
        from app.deps import get_db

        async def override_db():
            yield session

        app_module.dependency_overrides[get_db] = override_db
        transport = httpx.ASGITransport(app=app_module)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/auth/register",
                    json={
                        "token": invitation_token,
                        "name": "Rollback Target",
                        "password": "Strong123",
                    },
                )
            assert response.status_code == 201, response.text
            user_id = response.json()["user"]["id"]
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(
                        AuditLog.action == "user.register",
                        AuditLog.target_id == user_id,
                    )
                )
                == 1
            )
        finally:
            app_module.dependency_overrides.pop(get_db, None)
            if outer_transaction.is_active:
                await outer_transaction.rollback()
            await session.close()
            await connection.close()

        async with maker() as observer:
            assert (
                await observer.scalar(
                    select(User.id).where(User.email == invitation.email)
                )
                is None
            )
            assert (
                await observer.scalar(
                    select(ProjectMember.id).where(
                        ProjectMember.project_id == project.id,
                        ProjectMember.user_id == user_id,
                    )
                )
                is None
            )
            stored_invitation = await observer.get(UserInvitation, invitation.id)
            assert stored_invitation.accepted_at is None
            assert stored_invitation.accepted_user_id is None
            assert (
                await observer.scalar(
                    select(AuditLog.id).where(
                        AuditLog.action == "user.register",
                        AuditLog.target_id == user_id,
                    )
                )
                is None
            )
    finally:
        async with maker() as cleanup:
            target_id = await cleanup.scalar(
                select(User.id).where(User.email == invitation.email)
            )
            user_ids = [admin.id] + ([target_id] if target_id is not None else [])
            await cleanup.execute(
                delete(AuditLog).where(AuditLog.actor_id.in_(user_ids))
            )
            await cleanup.execute(
                delete(ProjectMember).where(ProjectMember.project_id == project.id)
            )
            await cleanup.execute(
                delete(UserInvitation).where(UserInvitation.id == invitation.id)
            )
            await cleanup.execute(delete(Project).where(Project.id == project.id))
            await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
            await cleanup.commit()


async def test_concurrent_acceptance_on_independent_postgres_sessions_returns_one_409(
    test_engine,
):
    from aap_identity.invitation_contract import (
        InvitationFailure,
        decode_invitation_request,
    )
    from app.services.identity_gateway import get_identity_gateway
    from app.services.identity_invitation_commands import IdentityInvitationCommands
    from uuid import uuid4

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as setup:
        admin = await create_user(
            setup, "super_admin", "concurrent-owner@invite.test", "Concurrent Owner"
        )
        target = await create_user(
            setup, "employee", "concurrent-target@invite.test", "Concurrent Target"
        )
        project = await create_project(setup, owner_id=admin.id)
        invitations = [
            await _target_invitation(
                setup,
                inviter=admin,
                email=target.email,
                project_id=project.id,
                role=role,
            )
            for role in ("annotator", "reviewer")
        ]
        target_id = target.id
        target_email = target.email
        credential = create_access_token(subject=str(target.id), role=target.role)
        invitation_ids = [invitation.id for invitation in invitations]
        invitation_tokens = [invitation.token for invitation in invitations]
        project_id = project.id
        admin_id = admin.id
        await setup.commit()

    try:
        barrier = asyncio.Barrier(2)

        async def accept(token: str) -> tuple[int, int]:
            async with maker() as db:
                backend_pid = int(await db.scalar(text("SELECT pg_backend_pid()")))
                await barrier.wait()
                try:
                    command = decode_invitation_request(
                        {
                            "schema": "aap-identity-invitations/v1",
                            "operation": "accept_invitation",
                            "operation_id": str(uuid4()),
                            "token": credential,
                            "payload": {"invitation_token": token},
                            "audit_context": {
                                "method": "POST",
                                "path": "/api/v1/auth/invitations/accept",
                                "ip": None,
                                "request_id": None,
                            },
                        }
                    )
                    await IdentityInvitationCommands(
                        db, get_identity_gateway()
                    ).execute(command)
                    status_code = 200
                except InvitationFailure as error:
                    await db.rollback()
                    assert error.code in {
                        "already_member",
                        "policy_rejected",
                        "current_scope_busy",
                        "credential_busy",
                    }
                    status_code = error.status_code
                return backend_pid, status_code

        results = await asyncio.wait_for(
            asyncio.gather(*(accept(token) for token in invitation_tokens)),
            timeout=20,
        )
        assert len({backend_pid for backend_pid, _ in results}) == 2
        assert sorted(status_code for _, status_code in results) == [200, 409]

        async with maker() as observer:
            member = await observer.scalar(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == target_id,
                )
            )
            assert member.assigned_by == admin_id
            accepted_count = await observer.scalar(
                select(func.count(UserInvitation.id)).where(
                    UserInvitation.id.in_(invitation_ids),
                    UserInvitation.accepted_user_id == target_id,
                )
            )
            assert accepted_count == 1
            pending_count = await observer.scalar(
                select(func.count(UserInvitation.id)).where(
                    UserInvitation.id.in_(invitation_ids),
                    UserInvitation.accepted_at.is_(None),
                )
            )
            assert pending_count == 1
            assert (await observer.get(User, target_id)).email == target_email
    finally:
        async with maker() as cleanup:
            await cleanup.execute(text("SET LOCAL app.allow_audit_update='true'"))
            user_ids = [admin_id, target_id]
            await cleanup.execute(
                text(
                    "DELETE FROM public.identity_invitation_operation_receipts WHERE actor_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": user_ids},
            )
            await cleanup.execute(
                delete(AuditLog).where(AuditLog.actor_id.in_(user_ids))
            )
            await cleanup.execute(
                delete(ProjectMember).where(ProjectMember.project_id == project_id)
            )
            await cleanup.execute(
                delete(UserInvitation).where(UserInvitation.id.in_(invitation_ids))
            )
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
            await cleanup.commit()
