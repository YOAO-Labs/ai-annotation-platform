"""Select the configuration owner without changing public request admission."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import HTTPException

from aap_workspace.config import secret_source
from aap_workspace.configuration import ConfigurationManager
from aap_workspace.configuration_contract import (
    ConfigurationUnavailable,
    configuration_request,
    decode_configuration_reply,
)
from aap_workspace.configuration_postgres import ConfigurationPostgresData
from app.config import settings
from app.services.inference_limits import capability_limits_from_settings
from app.services.workspace_configuration_data import WorkspaceConfigurationData


class ConfigurationOwnerInternalError(RuntimeError):
    """Preserve the public plain 500 response from a selected remote owner."""


def configuration_manager(data):
    return ConfigurationManager(
        data,
        capability_limits=capability_limits_from_settings(),
        video_segment_size_frames=settings.video_segment_size_frames,
        setup_timeout=settings.ml_health_timeout,
    )


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
            reply = await configuration_manager(WorkspaceConfigurationData(db)).execute(
                request
            )
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
                        for attempt in range(2):
                            try:
                                response = await client.post(
                                    url.rstrip("/")
                                    + "/v1/workspace/configuration/execute",
                                    headers={"Authorization": f"Bearer {token}"},
                                    json=request.model_dump(mode="json", by_alias=True),
                                )
                                break
                            except httpx.RequestError:
                                if attempt:
                                    raise
                    if response.status_code == 500:
                        raise ConfigurationOwnerInternalError(
                            "Configuration owner failed"
                        )
                    if response.status_code != 200:
                        raise ConfigurationUnavailable()
                    reply = decode_configuration_reply(request, response.content)
                else:
                    reply = await configuration_manager(
                        ConfigurationPostgresData(dsn, command_timeout=timeout)
                    ).execute(request)
        if not reply.ok:
            raise HTTPException(
                status_code=reply.error.status_code,
                detail=reply.error.detail,
                headers=reply.error.headers,
            )
        return reply.result
    except (
        ConfigurationUnavailable,
        httpx.HTTPError,
        httpx.InvalidURL,
        OSError,
        TimeoutError,
    ):
        raise HTTPException(status_code=503, detail="项目配置服务暂不可用") from None
