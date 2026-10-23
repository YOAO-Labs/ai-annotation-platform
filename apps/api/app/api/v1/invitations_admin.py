"""Invitation management HTTP presentation for the selected Identity owner."""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime
from typing import Literal

from aap_identity.contract import Principal
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from app.api.v1.invitation_commands import (
    execute_invitation,
    get_invitation_manager_commands,
    require_invitation_manager,
)
from app.deps import bearer_scheme
from app.schemas.invitation import InvitationOut, InvitationResendResponse
from app.schemas.management import (
    InvitationPage,
    InvitationSendEmailResponse,
    InvitationStats,
)
from app.services.audit import export_metadata_header
from app.services.csv_export import csv_literal
from app.services.identity_invitation_commands import InvitationCommandsPort

router = APIRouter()
_StatusFilter = Literal["pending", "accepted", "expired", "revoked", "all"]
_ScopeFilter = Literal["me", "all"]


def _filters(
    status_filter, scope, project_id, role, email, search, created_from, created_to
):
    return {
        "status": status_filter,
        "scope": scope,
        "project_id": str(project_id) if project_id is not None else None,
        "role": role,
        "email": email,
        "search": search,
        "created_from": created_from.isoformat() if created_from is not None else None,
        "created_to": created_to.isoformat() if created_to is not None else None,
    }


@router.get("", response_model=list[InvitationOut])
async def list_invitations(
    request: Request,
    status_filter: _StatusFilter = Query("all", alias="status"),
    scope: _ScopeFilter = Query("me"),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: InvitationCommandsPort = Depends(get_invitation_manager_commands),
):
    data = await execute_invitation(
        request,
        credentials,
        commands,
        "list_invitations",
        {"status": status_filter, "scope": scope},
    )
    return data["items"]


@router.get("/query", response_model=InvitationPage)
async def query_invitations(
    request: Request,
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
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: InvitationCommandsPort = Depends(get_invitation_manager_commands),
):
    payload = _filters(
        status_filter, scope, project_id, role, email, search, created_from, created_to
    )
    return await execute_invitation(
        request,
        credentials,
        commands,
        "query_invitations",
        {**payload, "page": page, "page_size": page_size},
    )


@router.get("/stats", response_model=InvitationStats)
async def invitation_stats_endpoint(
    request: Request,
    status_filter: _StatusFilter = Query("all", alias="status"),
    scope: _ScopeFilter = Query("me"),
    project_id: uuid.UUID | None = Query(None),
    role: str | None = Query(None),
    email: str | None = Query(None, max_length=255),
    search: str | None = Query(None, max_length=255),
    created_from: datetime | None = Query(None),
    created_to: datetime | None = Query(None),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: InvitationCommandsPort = Depends(get_invitation_manager_commands),
):
    return await execute_invitation(
        request,
        credentials,
        commands,
        "invitation_stats",
        _filters(
            status_filter,
            scope,
            project_id,
            role,
            email,
            search,
            created_from,
            created_to,
        ),
    )


@router.get("/export")
async def export_invitations(
    request: Request,
    format: Literal["csv", "json"] = Query("csv"),
    status_filter: _StatusFilter = Query("all", alias="status"),
    scope: _ScopeFilter = Query("me"),
    project_id: uuid.UUID | None = Query(None),
    role: str | None = Query(None),
    email: str | None = Query(None, max_length=255),
    search: str | None = Query(None, max_length=255),
    created_from: datetime | None = Query(None),
    created_to: datetime | None = Query(None),
    actor: Principal = Depends(require_invitation_manager),
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: InvitationCommandsPort = Depends(get_invitation_manager_commands),
):
    payload = _filters(
        status_filter, scope, project_id, role, email, search, created_from, created_to
    )
    data = await execute_invitation(
        request,
        credentials,
        commands,
        "export_invitations",
        {**payload, "format": format},
    )
    items = [InvitationOut.model_validate(item) for item in data["items"]]
    if format == "json":
        body = json.dumps(
            {
                "_export_meta": {
                    "exported_by": data["exported_by"],
                    "exported_at": data["exported_at"].isoformat(),
                    "count": len(items),
                },
                "invitations": [item.model_dump(mode="json") for item in items],
            },
            ensure_ascii=False,
            indent=2,
        )
        return StreamingResponse(
            iter([body]),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="invitations.json"'},
        )

    buf = io.StringIO()
    buf.write("\ufeff")
    buf.write(
        export_metadata_header(
            actor=request.state.identity_principal or actor, fmt="csv", request=request
        )
    )
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
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="invitations.csv"'},
    )


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: InvitationCommandsPort = Depends(get_invitation_manager_commands),
):
    await execute_invitation(
        request,
        credentials,
        commands,
        "revoke_invitation",
        {"invitation_id": str(invitation_id)},
    )


@router.post("/{invitation_id}/resend", response_model=InvitationResendResponse)
async def resend_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: InvitationCommandsPort = Depends(get_invitation_manager_commands),
):
    return await execute_invitation(
        request,
        credentials,
        commands,
        "resend_invitation",
        {"invitation_id": str(invitation_id)},
    )


@router.post("/{invitation_id}/send-email", response_model=InvitationSendEmailResponse)
async def send_invitation_email_endpoint(
    invitation_id: uuid.UUID,
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    commands: InvitationCommandsPort = Depends(get_invitation_manager_commands),
):
    """Await the durable sender without holding a business transaction open."""
    return await execute_invitation(
        request,
        credentials,
        commands,
        "send_invitation_email",
        {"invitation_id": str(invitation_id)},
    )
