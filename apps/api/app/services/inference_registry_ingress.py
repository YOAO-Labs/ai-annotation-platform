"""Select the owner of an entire startup environment synchronization command."""

from __future__ import annotations

from aap_inference_contract.registry_ingress import (
    RegistryEnvSyncUnavailable,
    decode_registry_env_sync_result,
    make_registry_env_sync_request,
    registry_env_sync_status_request,
)
from aap_inference_contract.registry_ingress_client import RegistryEnvSyncHttpClient
from aap_workspace.config import secret_source

from app.config import settings
from app.services.inference_limits import capability_limits_from_settings


async def sync_selected_environment(urls, *, borrowed):
    try:
        dsn = secret_source(
            settings.inference_registry_ingress_database_url,
            settings.inference_registry_ingress_database_url_file,
        )
        url = settings.inference_registry_ingress_url
        if url and dsn:
            raise RegistryEnvSyncUnavailable()
    except (ValueError, OSError):
        raise RegistryEnvSyncUnavailable() from None
    if not url and not dsn:
        return await borrowed(urls)
    try:
        command = make_registry_env_sync_request(
            urls,
            timeout_seconds=settings.ml_health_timeout,
            capability_limits=capability_limits_from_settings(),
        )
        if url:
            token = secret_source(
                settings.inference_registry_ingress_token,
                settings.inference_registry_ingress_token_file,
            )
            client = RegistryEnvSyncHttpClient(
                url,
                token,
                timeout_seconds=settings.inference_registry_ingress_timeout_seconds,
            )
            result = await client.sync(command)
        else:
            from aap_inference.registry_ingress_manager import RegistryEnvSyncManager

            manager = RegistryEnvSyncManager(dsn)
            try:
                try:
                    result = await manager.sync(command)
                except RegistryEnvSyncUnavailable:
                    # A lost commit acknowledgement never authorizes a second write.
                    result = await manager.status(
                        registry_env_sync_status_request(command)
                    )
            finally:
                await manager.close()
        result = decode_registry_env_sync_result(command, result)
        if result.state != "committed":
            raise RegistryEnvSyncUnavailable()
        return result.observed_urls
    except (ValueError, OSError):
        raise RegistryEnvSyncUnavailable() from None
