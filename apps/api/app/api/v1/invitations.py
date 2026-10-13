from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.deps import get_db, get_current_user
from app.db.models.user import User
from app.schemas.invitation import (
    AcceptInvitationRequest,
    AcceptInvitationResponse,
    InvitationResolve,
    RegisterRequest,
    RegisterResponse,
)
from app.schemas.user import UserOut
from app.services.invitation import InvitationService, invitation_project_role
from app.services.audit import AuditService, AuditAction

router = APIRouter()


@router.get("/invitations/{token}", response_model=InvitationResolve)
async def resolve_invitation(token: str, db: AsyncSession = Depends(get_db)):
    inv = await InvitationService.resolve(db, token)
    inviter_name: str | None = None
    inviter = await db.get(User, inv.invited_by)
    if inviter is not None:
        inviter_name = inviter.name
    project_name = None
    if inv.project_id:
        from app.db.models.project import Project

        project_name = await db.scalar(
            select(Project.name).where(Project.id == inv.project_id)
        )
    return InvitationResolve(
        email=inv.email,
        role=inv.role,
        group_name=inv.group_name,
        project_id=inv.project_id,
        project_name=project_name,
        project_member_role=invitation_project_role(inv),
        expires_at=inv.expires_at,
        invited_by_name=inviter_name,
    )


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register_via_invitation(
    payload: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user, inv, acceptance = await InvitationService.accept(
        db,
        token=payload.token,
        name=payload.name,
        password=payload.password,
    )
    await AuditService.log(
        db,
        actor=user,
        action=AuditAction.USER_REGISTER,
        target_type="user",
        target_id=str(user.id),
        request=request,
        status_code=201,
        detail={
            "email": user.email,
            "role": user.role,
            "invitation_id": str(inv.id),
            "project_id": str(inv.project_id) if inv.project_id else None,
        },
    )
    await db.commit()
    await db.refresh(user)

    token = create_access_token(subject=str(user.id), role=user.role)
    return RegisterResponse(
        access_token=token,
        token_type="bearer",
        user=UserOut.model_validate(user),
        acceptance=acceptance,
    )


@router.post(
    "/invitations/accept",
    response_model=AcceptInvitationResponse,
)
async def accept_invitation_for_existing_user(
    payload: AcceptInvitationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Explicitly attach an existing, matching account to an invitation project."""

    return await _accept_existing_invitation(db, payload.token, request, user)


async def _accept_existing_invitation(
    db: AsyncSession, token: str, request: Request, user: User
) -> AcceptInvitationResponse:
    accepted_user, inv, acceptance = await InvitationService.accept_existing(
        db, token=token, user=user
    )
    await AuditService.log(
        db,
        actor=accepted_user,
        action="user.invite_accept_existing",
        target_type="invitation",
        target_id=str(inv.id),
        request=request,
        status_code=200,
        detail={
            "email": accepted_user.email,
            "project_id": str(inv.project_id) if inv.project_id else None,
        },
    )
    await db.commit()
    await db.refresh(accepted_user)
    return AcceptInvitationResponse(
        user=UserOut.model_validate(accepted_user),
        acceptance=acceptance,
    )
