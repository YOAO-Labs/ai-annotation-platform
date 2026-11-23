"""Run public owner transfer on the caller's Workspace transaction."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import HTTPException

from yoao_workspace.owner_transfer import OwnerTransferManager
from yoao_workspace.owner_transfer_contract import (
    OwnerTransferSuccess,
    OwnerTransferUnavailable,
    decode_owner_transfer_request,
)
from app.config import settings
from app.services.workspace_owner_transfer_data import WorkspaceOwnerTransferData


async def transfer_project_owner(
    db, *, actor_id, project_id, new_owner_id
) -> OwnerTransferSuccess:
    request = decode_owner_transfer_request(
        {
            "schema": "aap-workspace-owner-transfer/v1",
            "operation_id": str(uuid.uuid4()),
            "actor_id": str(actor_id),
            "project_id": str(project_id),
            "new_owner_id": str(new_owner_id),
        }
    )
    try:
        async with asyncio.timeout(settings.workspace_membership_timeout_seconds):
            reply = await OwnerTransferManager(WorkspaceOwnerTransferData(db)).execute(
                request
            )
    except (OwnerTransferUnavailable, OSError, TimeoutError, ValueError, TypeError):
        raise HTTPException(status_code=503, detail="项目所有权服务暂不可用") from None
    if not reply.ok:
        raise HTTPException(
            status_code=reply.error.status_code, detail=reply.error.detail
        )
    return reply
