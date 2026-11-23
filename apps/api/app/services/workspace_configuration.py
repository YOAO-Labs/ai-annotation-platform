"""Run public configuration operations on the caller's Workspace transaction."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from fastapi import HTTPException
from httpx import HTTPError, InvalidURL

from yoao_workspace.configuration import ConfigurationManager
from yoao_workspace.configuration_contract import (
    ConfigurationGuideSuccess,
    ConfigurationRenameResult,
    ConfigurationRenameSuccess,
    ConfigurationUnavailable,
    ConfigurationRequest,
    configuration_request,
)
from app.config import settings
from app.services.inference_limits import capability_limits_from_settings
from app.services.workspace_configuration_data import WorkspaceConfigurationData


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


def _configuration_manager(db):
    from app.services.inference_backend_probe import backend_probe

    return ConfigurationManager(
        WorkspaceConfigurationData(db),
        capability_limits=capability_limits_from_settings(),
        video_segment_size_frames=settings.video_segment_size_frames,
        setup_timeout=settings.ml_health_timeout,
        backend_probe=backend_probe(),
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
    manager = _configuration_manager(db)
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
        reply = await _configuration_manager(db).execute(request)
        if not reply.ok:
            raise HTTPException(
                status_code=reply.error.status_code,
                detail=reply.error.detail,
                headers=reply.error.headers,
            )
        if isinstance(reply, ConfigurationRenameSuccess):
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
    except (ConfigurationUnavailable, HTTPError, InvalidURL, OSError, TimeoutError):
        raise HTTPException(status_code=503, detail="项目配置服务暂不可用") from None
