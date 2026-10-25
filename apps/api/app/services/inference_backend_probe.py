"""Select the credential-owning Inference probe without changing caller commits."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from aap_inference.backend_probe_manager import BackendProbeManager
from aap_inference.backend_probe_postgres import BackendProbePostgresData
from aap_inference_contract.client import BackendProbeHttpClient
from aap_inference_contract.probe import (
    BackendProbeUnavailable,
    backend_probe_policy_ready,
)
from aap_workspace.config import secret_source
from app.config import settings

_POLICY = text("SELECT aap_inference_backend_probe_v1.backend_probe_policy()")
_OPEN_SUBJECT = text(
    "SELECT aap_inference_backend_probe_v1.open_backend_probe_subject("
    "CAST(:subject AS text),CAST(:registry_id AS uuid))"
)


class BorrowedBackendProbeData:
    """Read only the encrypted snapshot on the existing caller connection."""

    def __init__(self, db) -> None:
        self.db = db

    async def readiness(self) -> bool:
        if self.db is None:
            return False
        try:
            return backend_probe_policy_ready(await self.db.scalar(_POLICY))
        except SQLAlchemyError:
            return False

    async def open_subject(self, probe_subject, registry_id) -> str:
        if self.db is None:
            raise BackendProbeUnavailable()
        try:
            result = await self.db.scalar(
                _OPEN_SUBJECT,
                {"subject": probe_subject, "registry_id": registry_id},
            )
        except SQLAlchemyError:
            raise BackendProbeUnavailable() from None
        if type(result) is not str:
            raise BackendProbeUnavailable()
        return result


def backend_probe(db):
    """Defer optional-provider selection until a configuration probe is needed."""

    async def probe_modalities(registry_id, probe_subject, *, timeout, limits):
        try:
            dsn = secret_source(
                settings.inference_backend_probe_database_url,
                settings.inference_backend_probe_database_url_file,
            )
            url = settings.inference_backend_probe_url
            if url and dsn:
                raise BackendProbeUnavailable()
            if url:
                token = secret_source(
                    settings.inference_backend_probe_token,
                    settings.inference_backend_probe_token_file,
                )
                client = BackendProbeHttpClient(
                    url,
                    token,
                    timeout_seconds=settings.inference_backend_probe_timeout_seconds,
                )
                return await client.probe_modalities(
                    registry_id, probe_subject, timeout=timeout, limits=limits
                )
            if dsn:
                data = BackendProbePostgresData(dsn)
                try:
                    return await BackendProbeManager(data).probe_modalities(
                        registry_id, probe_subject, timeout=timeout, limits=limits
                    )
                finally:
                    await data.close()
        except ValueError:
            raise BackendProbeUnavailable() from None
        return await BackendProbeManager(BorrowedBackendProbeData(db)).probe_modalities(
            registry_id, probe_subject, timeout=timeout, limits=limits
        )

    return probe_modalities
