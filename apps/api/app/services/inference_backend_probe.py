"""Select the independent credential-owning probe without caller-side secret reads."""

from __future__ import annotations

from aap_inference_contract.client import BackendProbeHttpClient
from aap_inference_contract.probe import BackendProbeUnavailable
from aap_workspace.config import secret_source

from app.config import settings


def backend_probe():
    """Defer authenticated owner selection until configuration needs a probe."""

    async def probe_modalities(registry_id, probe_subject, *, timeout, limits):
        try:
            client = BackendProbeHttpClient(
                settings.inference_backend_probe_url,
                secret_source(
                    settings.inference_backend_probe_token,
                    settings.inference_backend_probe_token_file,
                ),
                timeout_seconds=settings.inference_backend_probe_timeout_seconds,
            )
            return await client.probe_modalities(
                registry_id, probe_subject, timeout=timeout, limits=limits
            )
        except (ValueError, OSError):
            raise BackendProbeUnavailable() from None

    return probe_modalities
