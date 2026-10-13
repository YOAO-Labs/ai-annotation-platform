"""Admin endpoints for managing user invitations (list / revoke / resend).

The /auth/invitations/{token} resolve and /auth/register endpoints live in
`invitations.py` and remain unauthenticated. This module mounts under
`/invitations` and requires the actor to be a project_admin or super_admin.
"""

from __future__ import annotations

import uuid
import csv
import io
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.enums import UserRole
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from app.db.models.project import Project
from app.deps import get_db, require_roles
from app.schemas.invitation import InvitationOut, InvitationResendResponse
from app.schemas.management import (
    InvitationPage,
    InvitationSendEmailResponse,
    InvitationStats,
)
from app.services.audit import AuditService, export_detail, export_metadata_header
from app.services.email import SmtpConfigError, send_invitation_email
from app.services.csv_export import csv_literal
from app.services.invitation import InvitationService, invitation_project_role
from app.services.management import (
    fetch_invitation_page,
    invitation_stats,
)
from app.services.system_settings_service import SystemSettingsService

router = APIRouter()

_MANAGERS = (UserRole.SUPER_ADMIN, UserRole.PROJECT_ADMIN)
_StatusFilter = Literal["pending", "accepted", "expired", "revoked", "all"]
_ScopeFilter = Literal["me", "all"]


def _to_out(
    inv: UserInvitation,
    inviter: User | None,
    project_name: str | None = None,
) -> InvitationOut:
    return InvitationOut(
        id=inv.id,
        email=inv.email,
        role=inv.role,
        group_name=inv.group_name,
        project_id=inv.project_id,
        project_name=project_name,
        project_member_role=invitation_project_role(inv),
        status=inv.status,
        expires_at=inv.expires_at,
        invited_by=inv.invited_by,
        invited_by_name=inviter.name if inviter else None,
        accepted_at=inv.accepted_at,
        revoked_at=inv.revoked_at,
        created_at=inv.created_at,
    )


@router.get("", response_model=list[InvitationOut])
async def list_invitations(
    status_filter: _StatusFilter = Query("all", alias="status"),
    scope: _ScopeFilter = Query("me"),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    if scope == "all" and actor.role != UserRole.SUPER_ADMIN.value:
        raise HTTPException(status_code=403, detail="仅超级管理员可查看全部邀请")

    q = select(UserInvitation)
    if scope == "me":
        q = q.where(UserInvitation.invited_by == actor.id)

    rows = (
        (await db.execute(q.order_by(UserInvitation.created_at.desc()))).scalars().all()
    )

    if status_filter != "all":
        rows = [r for r in rows if r.status == status_filter]

    inviter_ids = {r.invited_by for r in rows}
    inviters: dict[uuid.UUID, User] = {}
    if inviter_ids:
        u_rows = (
            (await db.execute(select(User).where(User.id.in_(inviter_ids))))
            .scalars()
            .all()
        )
        inviters = {u.id: u for u in u_rows}

    project_ids = {r.project_id for r in rows if r.project_id is not None}
    projects: dict[uuid.UUID, str] = {}
    if project_ids:
        project_rows = (
            await db.execute(select(Project).where(Project.id.in_(project_ids)))
        ).all()
        projects = {p.id: p.name for (p,) in project_rows}

    return [
        _to_out(r, inviters.get(r.invited_by), projects.get(r.project_id)) for r in rows
    ]


def _assert_management_scope(scope: _ScopeFilter, actor: User) -> None:
    if scope == "all" and actor.role != UserRole.SUPER_ADMIN.value:
        raise HTTPException(status_code=403, detail="仅超级管理员可查看全部邀请")


async def _query_args(
    *,
    db: AsyncSession,
    actor: User,
    page: int,
    page_size: int | None,
    status_filter: _StatusFilter,
    scope: _ScopeFilter,
    project_id: uuid.UUID | None,
    role: str | None,
    email: str | None,
    search: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
):
    _assert_management_scope(scope, actor)
    return await fetch_invitation_page(
        db,
        actor,
        page=page,
        page_size=page_size,
        status_filter=status_filter,
        scope=scope,
        project_id=project_id,
        role=role,
        email=email,
        search=search,
        created_from=created_from,
        created_to=created_to,
    )


@router.get("/query", response_model=InvitationPage)
async def query_invitations(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    status_filter: _StatusFilter = Query("all", alias="status"),
    scope: _ScopeFilter = Query("me"),
    project_id: uuid.UUID | None = Query(None),
    role: str | None = Query(None),
    email: str | None = Query(None, max_length=255),
    search: str | None = Query(None, max_length=255),
    created_from: datetime | None = Query(None),
    created_to: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    items, total = await _query_args(
        db=db,
        actor=actor,
        page=page,
        page_size=page_size,
        status_filter=status_filter,
        scope=scope,
        project_id=project_id,
        role=role,
        email=email,
        search=search,
        created_from=created_from,
        created_to=created_to,
    )
    return InvitationPage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size if total else 0,
    )


@router.get("/stats", response_model=InvitationStats)
async def invitation_stats_endpoint(
    status_filter: _StatusFilter = Query("all", alias="status"),
    scope: _ScopeFilter = Query("me"),
    project_id: uuid.UUID | None = Query(None),
    role: str | None = Query(None),
    email: str | None = Query(None, max_length=255),
    search: str | None = Query(None, max_length=255),
    created_from: datetime | None = Query(None),
    created_to: datetime | None = Query(None),
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    _assert_management_scope(scope, actor)
    return await invitation_stats(
        db,
        actor,
        status_filter=status_filter,
        scope=scope,
        project_id=project_id,
        role=role,
        email=email,
        search=search,
        created_from=created_from,
        created_to=created_to,
    )


@router.get("/export")
async def export_invitations(
    format: Literal["csv", "json"] = Query("csv"),
    status_filter: _StatusFilter = Query("all", alias="status"),
    scope: _ScopeFilter = Query("me"),
    project_id: uuid.UUID | None = Query(None),
    role: str | None = Query(None),
    email: str | None = Query(None, max_length=255),
    search: str | None = Query(None, max_length=255),
    created_from: datetime | None = Query(None),
    created_to: datetime | None = Query(None),
    request: Request = None,  # type: ignore[assignment]
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    _assert_management_scope(scope, actor)
    items, _ = await _query_args(
        db=db,
        actor=actor,
        page=1,
        page_size=None,
        status_filter=status_filter,
        scope=scope,
        project_id=project_id,
        role=role,
        email=email,
        search=search,
        created_from=created_from,
        created_to=created_to,
    )
    now = datetime.now().astimezone().isoformat()
    if format == "json":
        rows = [item.model_dump(mode="json") for item in items]
        body = json.dumps(
            {
                "_export_meta": {
                    "exported_by": actor.email,
                    "exported_at": now,
                    "count": len(rows),
                },
                "invitations": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        await AuditService.log(
            db,
            actor=actor,
            action="invitation.export",
            target_type="invitation",
            request=request,
            status_code=200,
            detail=export_detail(
                actor=actor,
                request=request,
                base={"format": "json", "count": len(rows), "scope": scope},
            ),
        )
        await db.commit()
        return StreamingResponse(
            iter([body]),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="invitations.json"'},
        )

    buf = io.StringIO()
    buf.write("﻿")
    buf.write(export_metadata_header(actor=actor, fmt="csv", request=request))
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "email",
            "role",
            "group_name",
            "project_id",
            "project_name",
            "status",
            "expires_at",
            "invited_by",
            "invited_by_name",
            "accepted_at",
            "revoked_at",
            "created_at",
        ]
    )
    for item in items:
        writer.writerow(
            [
                str(item.id),
                csv_literal(item.email),
                item.role,
                csv_literal(item.group_name or ""),
                str(item.project_id) if item.project_id else "",
                csv_literal(item.project_name or ""),
                item.status,
                item.expires_at.isoformat(),
                str(item.invited_by),
                csv_literal(item.invited_by_name or ""),
                item.accepted_at.isoformat() if item.accepted_at else "",
                item.revoked_at.isoformat() if item.revoked_at else "",
                item.created_at.isoformat(),
            ]
        )
    await AuditService.log(
        db,
        actor=actor,
        action="invitation.export",
        target_type="invitation",
        request=request,
        status_code=200,
        detail=export_detail(
            actor=actor,
            request=request,
            base={"format": "csv", "count": len(items), "scope": scope},
        ),
    )
    await db.commit()
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="invitations.csv"'},
    )


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    inv = await InvitationService.revoke(db, invitation_id, actor=actor)
    await AuditService.log(
        db,
        actor=actor,
        action="user.invite_revoke",
        target_type="invitation",
        target_id=str(invitation_id),
        request=request,
        status_code=204,
        detail={"email": inv.email, "role": inv.role},
    )
    await db.commit()


@router.post("/{invitation_id}/resend", response_model=InvitationResendResponse)
async def resend_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    inv = await InvitationService.resend(db, invitation_id, actor=actor)
    await AuditService.log(
        db,
        actor=actor,
        action="user.invite_resend",
        target_type="invitation",
        target_id=str(invitation_id),
        request=request,
        status_code=200,
        detail={"email": inv.email, "role": inv.role},
    )
    await db.commit()
    base_url = (
        await SystemSettingsService.get(db, "frontend_base_url")
        or settings.frontend_base_url
    )
    invite_url = f"{str(base_url).rstrip('/')}/register?token={inv.token}"
    return InvitationResendResponse(
        invite_url=invite_url,
        token=inv.token,
        expires_at=inv.expires_at,
    )


@router.post(
    "/{invitation_id}/send-email",
    response_model=InvitationSendEmailResponse,
)
async def send_invitation_email_endpoint(
    invitation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    actor: User = Depends(require_roles(*_MANAGERS)),
):
    """Send an existing invitation; the link and token are never rotated."""

    query = select(UserInvitation).where(UserInvitation.id == invitation_id)
    if actor.role != UserRole.SUPER_ADMIN.value:
        query = query.where(UserInvitation.invited_by == actor.id)
    invitation = await db.scalar(query.with_for_update())
    if invitation is None:
        raise HTTPException(status_code=404, detail="邀请不存在")

    # Resolve re-checks expiry, revocation, issuer state, project existence and
    # project ownership. It is deliberately called before SMTP so invalid
    # invitations cannot trigger mail delivery.
    invitation = await InvitationService.resolve(db, invitation.token)
    base_url = (
        await SystemSettingsService.get(db, "frontend_base_url")
        or settings.frontend_base_url
    )
    invite_url = f"{str(base_url).rstrip('/')}/register?token={invitation.token}"
    project_name = None
    if invitation.project_id:
        project_name = await db.scalar(
            select(Project.name).where(Project.id == invitation.project_id)
        )
    try:
        await send_invitation_email(
            db,
            invitation.email,
            invite_url,
            project_name=project_name,
            role=invitation_project_role(invitation),
            expires_at=invitation.expires_at,
        )
    except SmtpConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "invitation_email_failed",
                "message": str(exc),
                "invitation_id": str(invitation.id),
            },
        ) from exc

    await AuditService.log(
        db,
        actor=actor,
        action="invitation.send_email",
        target_type="invitation",
        target_id=str(invitation.id),
        request=request,
        status_code=200,
        detail={
            "email": invitation.email,
            "project_id": str(invitation.project_id) if invitation.project_id else None,
        },
    )
    await db.commit()
    return InvitationSendEmailResponse(
        invitation_id=invitation.id,
        email=invitation.email,
        invite_url=invite_url,
        message="邀请邮件已发送",
    )
