"""Select the template-query owner and materialize its public response values."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
import httpx

from aap_workspace.config import secret_source
from aap_workspace.template_queries import TemplateQueryManager
from aap_workspace.template_query_contract import (
    TemplateQueryUnavailable,
    decode_template_query_reply,
    materialize_template_query_data,
    template_query_request,
)
from aap_workspace.template_query_postgres import TemplateQueryPostgresData
from app.config import settings
from app.services.workspace_template_query_data import WorkspaceTemplateQueryData


class TemplateQueryOwnerInternalError(RuntimeError):
    """Retain the public plain500 path for internal query-owner failures."""


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
        url = settings.workspace_template_query_url
        try:
            dsn = secret_source(
                settings.workspace_template_query_database_url,
                settings.workspace_template_query_database_url_file,
            )
        except ValueError:
            raise TemplateQueryUnavailable() from None
        if url and dsn:
            raise TemplateQueryUnavailable()
        if not url and not dsn:
            reply = await TemplateQueryManager(WorkspaceTemplateQueryData(db)).execute(
                request
            )
        else:
            timeout = settings.workspace_template_query_timeout_seconds
            async with asyncio.timeout(timeout):
                if url:
                    try:
                        token = secret_source(
                            settings.workspace_membership_control_token,
                            settings.workspace_membership_control_token_file,
                        )
                    except ValueError:
                        raise TemplateQueryUnavailable() from None
                    if not token:
                        raise TemplateQueryUnavailable()
                    async with httpx.AsyncClient(
                        trust_env=False, timeout=timeout
                    ) as client:
                        response = await client.post(
                            url.rstrip("/") + "/v1/workspace/template-queries/execute",
                            headers={"Authorization": f"Bearer {token}"},
                            json=request.model_dump(mode="json", by_alias=True),
                        )
                    if response.status_code == 500:
                        raise TemplateQueryOwnerInternalError(
                            "Template-query owner failed"
                        )
                    if response.status_code != 200:
                        raise TemplateQueryUnavailable()
                    reply = decode_template_query_reply(request, response.content)
                else:
                    reply = await TemplateQueryManager(
                        TemplateQueryPostgresData(dsn, command_timeout=timeout)
                    ).execute(request)
        if not reply.ok:
            raise HTTPException(
                status_code=reply.error.status_code,
                detail=reply.error.detail,
                headers=reply.error.headers,
            )
        return materialize_template_query_data(reply)
    except (
        TemplateQueryUnavailable,
        httpx.HTTPError,
        httpx.InvalidURL,
        OSError,
        TimeoutError,
    ):
        raise HTTPException(status_code=503, detail="模板查询服务暂不可用") from None
