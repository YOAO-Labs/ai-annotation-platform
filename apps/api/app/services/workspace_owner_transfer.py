"""Select the Workspace owner-transfer binding without changing public admission."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import HTTPException
import httpx

from aap_workspace.config import secret_source
from aap_workspace.owner_transfer import OwnerTransferManager
from aap_workspace.owner_transfer_contract import (
    OwnerTransferSuccess,
    OwnerTransferUnavailable,
    decode_owner_transfer_reply,
    decode_owner_transfer_request,
)
from aap_workspace.owner_transfer_postgres import OwnerTransferPostgresData
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
        timeout = settings.workspace_membership_timeout_seconds
        async with asyncio.timeout(timeout):
            if settings.workspace_owner_transfer_url:
                token = secret_source(
                    settings.workspace_membership_control_token,
                    settings.workspace_membership_control_token_file,
                )
                if not token:
                    raise OwnerTransferUnavailable()
                async with httpx.AsyncClient(
                    trust_env=False, timeout=timeout
                ) as client:
                    for attempt in range(2):
                        try:
                            response = await client.post(
                                settings.workspace_owner_transfer_url.rstrip("/")
                                + "/v1/workspace/owner-transfer/execute",
                                headers={"Authorization": f"Bearer {token}"},
                                json=request.model_dump(mode="json", by_alias=True),
                            )
                            break
                        except httpx.RequestError:
                            if attempt:
                                raise
                if response.status_code == 500:
                    raise HTTPException(status_code=500, detail="Internal Server Error")
                if response.status_code != 200:
                    raise OwnerTransferUnavailable()
                reply = decode_owner_transfer_reply(request, response.content)
            else:
                dsn = secret_source(
                    settings.workspace_owner_transfer_database_url,
                    settings.workspace_owner_transfer_database_url_file,
                )
                if dsn:
                    data = OwnerTransferPostgresData(dsn, command_timeout=timeout)
                    await data.owner_transfer_policy()
                else:
                    data = WorkspaceOwnerTransferData(db)
                reply = await OwnerTransferManager(data).execute(request)
        if not reply.ok:
            raise HTTPException(
                status_code=reply.error.status_code, detail=reply.error.detail
            )
        return reply
    except (
        OwnerTransferUnavailable,
        httpx.HTTPError,
        OSError,
        TimeoutError,
        ValueError,
        TypeError,
        UnicodeError,
    ):
        raise HTTPException(status_code=503, detail="项目所有权服务暂不可用") from None
