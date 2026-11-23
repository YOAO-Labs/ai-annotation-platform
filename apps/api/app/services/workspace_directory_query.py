"""Read directory facts from the caller's transaction."""

from fastapi import HTTPException
from yoao_workspace.directory_queries import DirectoryQueryManager
from yoao_workspace.directory_query_contract import (
    DirectoryQueryUnavailable,
    directory_query_request,
)
from yoao_workspace.directory_query_values import materialize_directory_query_data
from yoao_workspace_contract.project_query_contract import ProjectQueryUnavailable
from app.services.workspace_directory_query_data import WorkspaceDirectoryQueryData
from app.services.workspace_project_query import _ApiProfiles


async def execute_directory_query(db, *, operation, actor_id, target_id):
    request = directory_query_request(operation, actor_id, target_id)
    try:
        reply = await DirectoryQueryManager(
            WorkspaceDirectoryQueryData(db), _ApiProfiles(db)
        ).execute(request)
        if not reply.ok:
            raise HTTPException(
                status_code=reply.error.status_code,
                detail=reply.error.detail,
                headers=reply.error.headers,
            )
        return materialize_directory_query_data(reply)
    except (DirectoryQueryUnavailable, ProjectQueryUnavailable, OSError):
        raise HTTPException(status_code=503, detail="项目目录服务暂不可用") from None
