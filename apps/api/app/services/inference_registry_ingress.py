"""Select the owner of an entire startup environment synchronization command."""

from __future__ import annotations

from aap_inference_contract.registry_ingress import (
    RegistryEnvSyncUnavailable,
    decode_registry_env_sync_result,
    make_registry_env_sync_request,
)
from aap_inference_contract.registry_ingress_client import RegistryEnvSyncHttpClient
from aap_workspace.config import secret_source

from app.config import settings
from app.services.inference_limits import capability_limits_from_settings


async def sync_selected_environment(urls):
    url = settings.inference_registry_ingress_url
    try:
        command = make_registry_env_sync_request(
            urls,
            timeout_seconds=settings.ml_health_timeout,
            capability_limits=capability_limits_from_settings(),
        )
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
        result = decode_registry_env_sync_result(command, result)
        if result.state != "committed":
            raise RegistryEnvSyncUnavailable()
        return result.observed_urls
    except (ValueError, OSError):
        raise RegistryEnvSyncUnavailable() from None
