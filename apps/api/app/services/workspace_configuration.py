"""Select the configuration owner without changing public request admission."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import uuid

import httpx
from fastapi import HTTPException

from aap_workspace.config import secret_source
from aap_workspace.configuration import ConfigurationManager
from aap_workspace.configuration_contract import (
    ConfigurationUnavailable,
    ConfigurationRenameResult,
    ConfigurationRenameSuccess,
    ConfigurationGuideSuccess,
    ConfigurationRequest,
    GUIDE_OPERATIONS,
    configuration_request,
    decode_configuration_reply,
)
from aap_workspace.configuration_postgres import ConfigurationPostgresData
from app.config import settings
from app.services.inference_limits import capability_limits_from_settings
from app.services.workspace_configuration_data import WorkspaceConfigurationData
from app.services.workspace_project_query import (
    _select_project_query_reply,
)


class ConfigurationOwnerInternalError(RuntimeError):
    """Preserve the public plain 500 response from a selected remote owner."""


@dataclass(frozen=True)
class ConfigurationExecution:
    resource_id: uuid.UUID
    template_id: uuid.UUID | None
    operation_id: uuid.UUID
    operation: str
    actor_id: uuid.UUID
    rename_result: ConfigurationRenameResult | None = None
    guide_result: ConfigurationGuideSuccess | None = None
    guide_request: ConfigurationRequest | None = None


def configuration_manager(data, *, db=None, noop_projection=None):
    from app.services.inference_backend_probe import backend_probe

    return ConfigurationManager(
        data,
        capability_limits=capability_limits_from_settings(),
        video_segment_size_frames=settings.video_segment_size_frames,
        setup_timeout=settings.ml_health_timeout,
        backend_probe=backend_probe(),
        noop_projection=noop_projection,
    )


async def execute_guide_action(
    db,
    *,
    operation,
    actor_id,
    target_id,
    payload,
    action,
    args,
):
    """Run one native guide action in the caller-owned transaction."""
    request = configuration_request(
        operation=operation,
        actor_id=actor_id,
        target_id=target_id,
        payload={} if payload is None else payload,
    )
    manager = configuration_manager(WorkspaceConfigurationData(db), db=db)
    try:
        return await manager.run_guide_action(request, action, args)
    except ConfigurationUnavailable:
        raise HTTPException(status_code=503, detail="项目配置服务暂不可用") from None


async def execute_configuration(
    db, *, operation, actor_id, target_id=None, payload=None
):
    request = configuration_request(
        operation=operation,
        actor_id=actor_id,
        target_id=target_id,
        payload={} if payload is None else payload,
    )
    try:
        url = settings.workspace_configuration_url
        try:
            dsn = secret_source(
                settings.workspace_configuration_database_url,
                settings.workspace_configuration_database_url_file,
            )
        except ValueError:
            raise ConfigurationUnavailable() from None
        if url and dsn:
            raise ConfigurationUnavailable()
        if not url and not dsn:
            # Retain the existing blocking pool lock for borrowed transactions.
            data = WorkspaceConfigurationData(db)
            reply = await configuration_manager(data, db=db).execute(request)
        else:
            timeout = settings.workspace_configuration_timeout_seconds
            async with asyncio.timeout(timeout):
                if url:
                    try:
                        token = secret_source(
                            settings.workspace_membership_control_token,
                            settings.workspace_membership_control_token_file,
                        )
                    except ValueError:
                        raise ConfigurationUnavailable() from None
                    if not token:
                        raise ConfigurationUnavailable()
                    async with httpx.AsyncClient(
                        trust_env=False, timeout=timeout
                    ) as client:
                        attempts = (
                            1
                            if request.operation == "rename_project_class"
                            or request.operation in GUIDE_OPERATIONS
                            else 2
                        )
                        for attempt in range(attempts):
                            try:
                                response = await client.post(
                                    url.rstrip("/")
                                    + "/v1/workspace/configuration/execute",
                                    headers={"Authorization": f"Bearer {token}"},
                                    json=request.model_dump(mode="json", by_alias=True),
                                )
                                break
                            except httpx.RequestError:
                                if attempt + 1 >= attempts:
                                    raise
                    if response.status_code == 500:
                        raise ConfigurationOwnerInternalError(
                            "Configuration owner failed"
                        )
                    if response.status_code != 200:
                        raise ConfigurationUnavailable()
                    reply = decode_configuration_reply(request, response.content)
                else:

                    async def noop_projection(project_request):
                        return await _select_project_query_reply(db, project_request)

                    data = ConfigurationPostgresData(dsn, command_timeout=timeout)
                    if request.operation == "rename_project_class":
                        manager = configuration_manager(
                            data, db=db, noop_projection=noop_projection
                        )
                    else:
                        manager = configuration_manager(data, db=db)
                    reply = await manager.execute(request)
        if not reply.ok:
            raise HTTPException(
                status_code=reply.error.status_code,
                detail=reply.error.detail,
                headers=reply.error.headers,
            )
        if isinstance(reply, ConfigurationRenameSuccess):
            if (
                not reply.result.changed
                and (url or dsn)
                and reply.result.projection is None
            ):
                raise ConfigurationUnavailable()
            return ConfigurationExecution(
                resource_id=reply.result.resource_id,
                template_id=None,
                operation_id=request.operation_id,
                operation=request.operation,
                actor_id=request.actor_id,
                rename_result=reply.result,
            )
        if isinstance(reply, ConfigurationGuideSuccess):
            if reply.result.refreshed:
                raise ConfigurationUnavailable()
            return ConfigurationExecution(
                resource_id=reply.result.resource_id,
                template_id=None,
                operation_id=request.operation_id,
                operation=request.operation,
                actor_id=request.actor_id,
                guide_result=reply,
                guide_request=request,
            )
        return ConfigurationExecution(
            resource_id=reply.result.resource_id,
            template_id=reply.result.template_id,
            operation_id=request.operation_id,
            operation=request.operation,
            actor_id=request.actor_id,
        )
    except (
        ConfigurationUnavailable,
        httpx.HTTPError,
        httpx.InvalidURL,
        OSError,
        TimeoutError,
    ):
        raise HTTPException(status_code=503, detail="项目配置服务暂不可用") from None
