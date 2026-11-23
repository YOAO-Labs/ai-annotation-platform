"""Materialize public template responses from the caller's transaction."""

from __future__ import annotations

from fastapi import HTTPException
from yoao_workspace.template_queries import TemplateQueryManager
from yoao_workspace.template_query_contract import (
    TemplateQueryUnavailable,
    materialize_template_query_data,
    template_query_request,
)
from app.services.workspace_template_query_data import WorkspaceTemplateQueryData


async def execute_template_query(
    db, *, operation, actor, target_id=None, filters=None, configuration_receipt=None
):
    request = template_query_request(
        operation,
        actor.id,
        getattr(actor.role, "value", actor.role),
        target_id=target_id,
        filters=filters,
        configuration_receipt=configuration_receipt,
    )
    try:
        reply = await TemplateQueryManager(WorkspaceTemplateQueryData(db)).execute(
            request
        )
        if not reply.ok:
            raise HTTPException(
                status_code=reply.error.status_code,
                detail=reply.error.detail,
                headers=reply.error.headers,
            )
        return materialize_template_query_data(reply)
    except (TemplateQueryUnavailable, OSError):
        raise HTTPException(status_code=503, detail="模板查询服务暂不可用") from None
