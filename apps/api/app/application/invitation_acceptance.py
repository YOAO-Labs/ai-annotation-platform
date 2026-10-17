"""Application workflows composing identity and project invitation acceptance."""

from __future__ import annotations


from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from app.services.invitation import (
    InvitationService,
    acceptance_summary,
    assert_existing_role_compatible,
    assert_project_role_compatible,
    invitation_project_role,
)
from app.services.project_membership import grant_invitation_membership


async def register_account_from_invitation(
    db: AsyncSession,
    *,
    token: str,
    name: str,
    password: str,
) -> tuple[User, UserInvitation, dict[str, object]]:
    """Create an invited account and optional membership in one caller transaction."""

    invitation, project = await InvitationService.lock_acceptance_context(db, token)
    inviter = await InvitationService.validate_acceptance_authority(
        db, invitation=invitation, project=project
    )

    user = await InvitationService.create_registration_account(
        db, invitation=invitation, name=name, password=password
    )

    member_role = None
    if project is not None:
        member_role = invitation_project_role(invitation)
        assert_project_role_compatible(member_role)
        await grant_invitation_membership(
            db,
            project_id=project.id,
            user_id=user.id,
            project_role=member_role,
            assigned_by=inviter.id,
        )

    await InvitationService.mark_accepted(db, invitation=invitation, user=user)
    acceptance = await acceptance_summary(
        db, user=user, project=project, member_role=member_role
    )
    return user, invitation, acceptance


async def accept_existing_project_invitation(
    db: AsyncSession,
    *,
    token: str,
    user: User,
) -> tuple[User, UserInvitation, dict[str, object]]:
    """Attach a matching active account to its invited project, without committing."""

    invitation, project = await InvitationService.lock_acceptance_context(db, token)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该邀请仅用于创建新账号，已有账号只能接受带目标项目的邀请",
        )

    inviter = await InvitationService.validate_acceptance_authority(
        db, invitation=invitation, project=project
    )
    accepted_user = await InvitationService.lock_acceptance_user(db, user.id)
    if accepted_user is None or not accepted_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号已停用，请重新登录",
        )
    if accepted_user.email.strip().lower() != invitation.email.strip().lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="当前登录邮箱与邀请邮箱不一致，请使用被邀请邮箱登录",
        )

    member_role = invitation_project_role(invitation)
    assert_project_role_compatible(member_role)
    assert_existing_role_compatible(accepted_user, member_role)
    await grant_invitation_membership(
        db,
        project_id=project.id,
        user_id=accepted_user.id,
        project_role=member_role,
        assigned_by=inviter.id,
    )
    await InvitationService.mark_accepted(db, invitation=invitation, user=accepted_user)
    acceptance = await acceptance_summary(
        db, user=accepted_user, project=project, member_role=member_role
    )
    return accepted_user, invitation, acceptance
