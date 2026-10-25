"""Select the directory owner without changing request admission or fallback."""

import asyncio

from fastapi import HTTPException
import httpx

from aap_workspace.config import secret_source
from aap_workspace.directory_queries import DirectoryQueryManager
from aap_workspace.directory_query_contract import (
    DirectoryQueryUnavailable,
    directory_query_request,
)
from aap_workspace.directory_query_postgres import DirectoryQueryPostgresData
from aap_workspace.directory_query_values import (
    decode_directory_query_reply,
    materialize_directory_query_data,
)
from aap_workspace_contract.project_query_contract import ProjectQueryUnavailable
from app.config import settings
from app.services.workspace_directory_query_data import WorkspaceDirectoryQueryData
from app.services.workspace_project_query import _ApiProfiles


class DirectoryQueryOwnerInternalError(RuntimeError):
    """The selected owner failed through the existing public plain-500 boundary."""


async def execute_directory_query(db, *, operation, actor_id, target_id):
    request = directory_query_request(operation, actor_id, target_id)
    try:
        url = settings.workspace_directory_query_url
        try:
            dsn = secret_source(
                settings.workspace_directory_query_database_url,
                settings.workspace_directory_query_database_url_file,
            )
        except ValueError:
            raise DirectoryQueryUnavailable() from None
        if url and dsn:
            raise DirectoryQueryUnavailable()
        if not url and not dsn:
            reply = await DirectoryQueryManager(
                WorkspaceDirectoryQueryData(db), _ApiProfiles(db)
            ).execute(request)
        else:
            timeout = settings.workspace_directory_query_timeout_seconds
            async with asyncio.timeout(timeout):
                if url:
                    try:
                        token = secret_source(
                            settings.workspace_membership_control_token,
                            settings.workspace_membership_control_token_file,
                        )
                    except ValueError:
                        raise DirectoryQueryUnavailable() from None
                    if not token:
                        raise DirectoryQueryUnavailable()
                    async with httpx.AsyncClient(
                        trust_env=False, timeout=timeout
                    ) as client:
                        response = await client.post(
                            url.rstrip("/") + "/v1/workspace/directory-queries/execute",
                            headers={"Authorization": f"Bearer {token}"},
                            json=request.model_dump(mode="json", by_alias=True),
                        )
                    if response.status_code == 500:
                        raise DirectoryQueryOwnerInternalError(
                            "Directory query owner failed"
                        )
                    if response.status_code != 200:
                        raise DirectoryQueryUnavailable()
                    reply = decode_directory_query_reply(request, response.content)
                else:
                    reply = await DirectoryQueryManager(
                        DirectoryQueryPostgresData(dsn, command_timeout=timeout),
                        _ApiProfiles(db),
                    ).execute(request)
        if not reply.ok:
            raise HTTPException(
                status_code=reply.error.status_code,
                detail=reply.error.detail,
                headers=reply.error.headers,
            )
        return materialize_directory_query_data(reply)
    except (
        DirectoryQueryUnavailable,
        ProjectQueryUnavailable,
        httpx.HTTPError,
        httpx.InvalidURL,
        OSError,
        TimeoutError,
    ):
        raise HTTPException(status_code=503, detail="项目目录服务暂不可用") from None
