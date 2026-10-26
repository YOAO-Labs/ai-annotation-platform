"""Owner-private persisted GPU health refresh over the existing health owner."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from aap_inference_contract.capabilities import CapabilityLimits

from .backend_transport import BackendOptions, BackendTransport, Observer
from .health import BackendHealthCoordinator
from .health_data_postgres import HealthDataPostgresData

_logger = logging.getLogger(__name__)
_CHALLENGE_RE = re.compile(r"[0-9a-f]{64}\Z")


class GPUHealthRefreshUnavailable(RuntimeError):
    """The persisted health check could not be completed safely."""


class PersistedGPUHealthRefresher:
    """Persist one real health/setup observation before returning to GPU proofs."""

    def __init__(
        self,
        dsn: str,
        *,
        options: BackendOptions,
        capability_limits_factory: Callable[[], CapabilityLimits],
        observe: Observer | None = None,
        connect_timeout_seconds: float = 5.0,
        command_timeout_seconds: float = 30.0,
    ) -> None:
        if (
            type(dsn) is not str
            or not dsn.strip()
            or any(character.isspace() for character in dsn)
            or not callable(capability_limits_factory)
            or type(connect_timeout_seconds) not in {int, float}
            or not math.isfinite(float(connect_timeout_seconds))
            or connect_timeout_seconds <= 0
            or type(command_timeout_seconds) not in {int, float}
            or not math.isfinite(float(command_timeout_seconds))
            or command_timeout_seconds <= 0
        ):
            raise ValueError("Invalid GPU health refresh settings")
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self._options = options
        self._capability_limits_factory = capability_limits_factory
        self._observe = observe or _ignore_observation
        self._connect_timeout = float(connect_timeout_seconds)
        self._command_timeout = float(command_timeout_seconds)

    async def __call__(self, registry_id: UUID, challenge: str) -> bool:
        if (
            type(registry_id) is not UUID
            or type(challenge) is not str
            or not _CHALLENGE_RE.fullmatch(challenge)
        ):
            raise ValueError("Invalid GPU health refresh identity")
        try:
            import asyncpg
        except ImportError:
            raise RuntimeError("GPU health PostgreSQL adapter is unavailable") from None

        connection = None
        transaction = None
        primary_failure = False
        try:
            connection = await asyncpg.connect(
                self._dsn,
                timeout=self._connect_timeout,
                command_timeout=self._command_timeout,
                server_settings={"application_name": "aap-inference-gpu-health"},
            )
            transaction = connection.transaction(readonly=False)
            await transaction.start()
            data = await HealthDataPostgresData.bind(connection)
            coordinator = BackendHealthCoordinator(
                data,
                client_factory=self._client_factory,
                capability_limits_factory=self._capability_limits_factory,
            )
            healthy = await coordinator.check_health(
                registry_id,
                gpu_health_challenge=challenge,
            )
            await transaction.commit()
            return healthy
        except BaseException as error:
            primary_failure = True
            if transaction is not None:
                try:
                    await asyncio.shield(transaction.rollback())
                except BaseException:
                    pass
            if isinstance(error, Exception):
                raise GPUHealthRefreshUnavailable(
                    "Persisted GPU health refresh unavailable"
                ) from None
            raise
        finally:
            if connection is not None:
                await self._close_connection(
                    connection,
                    primary_failure=primary_failure,
                )

    def _client_factory(self, backend: Any) -> BackendTransport:
        return BackendTransport(
            backend,
            options=self._options,
            dispatch_context=_reject_gpu_dispatch,
            observe=self._observe,
        )

    @staticmethod
    async def _close_connection(connection: Any, *, primary_failure: bool) -> None:
        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning("GPU health PostgreSQL cleanup failed")
                return
            raise
        except BaseException:
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning("GPU health PostgreSQL cleanup failed")
                return
            raise RuntimeError("GPU health PostgreSQL cleanup failed") from None


@asynccontextmanager
async def _reject_gpu_dispatch(_operation, _scope):
    raise RuntimeError("Persisted health reads cannot enter GPU dispatch")
    yield None


def _ignore_observation(
    _backend_id: str | None, _outcome: str, _seconds: float
) -> None:
    return None


__all__ = ["GPUHealthRefreshUnavailable", "PersistedGPUHealthRefresher"]
