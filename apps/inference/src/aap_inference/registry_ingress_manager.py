"""Standalone owner for authenticated Registry environment synchronization."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import math
from typing import Any, Callable

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.registry_ingress import (
    REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
    RegistryEnvSyncRequest,
    RegistryEnvSyncResult,
    RegistryEnvSyncStatusRequest,
    RegistryEnvSyncUnavailable,
    decode_registry_env_sync_request,
    decode_registry_env_sync_result,
    decode_registry_env_sync_status_request,
    decode_registry_env_sync_status_result,
    registry_env_sync_command_sha256,
)

from .registry_ingress import sync_environment
from .registry_ingress_postgres import RegistryIngressPostgresData


class _CommitUncertain(RegistryEnvSyncUnavailable):
    pass


class RegistryEnvSyncManager:
    """Own one PostgreSQL connection and transaction per ingress command."""

    def __init__(
        self,
        dsn: str,
        *,
        client_factory: Callable[[float], Any] | None = None,
        setup_probe: Callable[[Any, str], Any] | None = None,
        clock: Callable[[], Any] | None = None,
        connect_timeout: float = 5.0,
        command_timeout: float = 60.0,
    ) -> None:
        if (
            type(dsn) is not str
            or not dsn.strip()
            or any(character.isspace() for character in dsn)
        ):
            raise RegistryEnvSyncUnavailable()
        if type(connect_timeout) not in {int, float} or type(command_timeout) not in {
            int,
            float,
        }:
            raise RegistryEnvSyncUnavailable()
        try:
            connect_timeout = float(connect_timeout)
            command_timeout = float(command_timeout)
        except (OverflowError, TypeError, ValueError):
            raise RegistryEnvSyncUnavailable() from None
        if (
            not math.isfinite(connect_timeout)
            or connect_timeout <= 0
            or not math.isfinite(command_timeout)
            or command_timeout <= 0
        ):
            raise RegistryEnvSyncUnavailable()
        self.dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.client_factory = client_factory
        self.setup_probe = setup_probe
        self.clock = clock

    async def _connect(self):
        try:
            import asyncpg
        except ImportError:
            raise RegistryEnvSyncUnavailable() from None
        try:
            return await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-inference-registry-ingress"},
            )
        except Exception:
            raise RegistryEnvSyncUnavailable() from None

    async def _close_connection(self, connection, *, primary_failure: bool) -> None:
        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                return
            raise
        except BaseException:
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                return
            raise RegistryEnvSyncUnavailable() from None

    @asynccontextmanager
    async def _data_transaction(self):
        connection = await self._connect()
        primary_failure = False
        body_completed = False
        try:
            try:
                async with connection.transaction():
                    data = await RegistryIngressPostgresData.bind(connection)
                    yield data
                    body_completed = True
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                if body_completed:
                    raise _CommitUncertain() from None
                raise
        except BaseException:
            primary_failure = True
            raise
        finally:
            try:
                await self._close_connection(
                    connection,
                    primary_failure=primary_failure,
                )
            except RegistryEnvSyncUnavailable:
                if body_completed:
                    raise _CommitUncertain() from None
                raise

    async def readiness(self) -> bool:
        try:
            async with self._data_transaction():
                pass
        except Exception:
            return False
        return True

    async def sync(
        self,
        request: RegistryEnvSyncRequest | dict | bytes | str,
    ) -> RegistryEnvSyncResult:
        typed_request = decode_registry_env_sync_request(request)
        command_sha256 = registry_env_sync_command_sha256(typed_request)

        def capability_limits() -> CapabilityLimits:
            return CapabilityLimits(
                video_tracker_window_size_frames=(
                    typed_request.capability_limits.video_tracker_window_size_frames
                ),
                video_tracker_sam3_window_size_frames=(
                    typed_request.capability_limits.video_tracker_sam3_window_size_frames
                ),
            )

        raw_result = await sync_environment(
            typed_request.urls,
            timeout_seconds=typed_request.timeout_seconds,
            data_factory=self._data_transaction,
            capability_limits_factory=capability_limits,
            client_factory=self.client_factory,
            setup_probe=self.setup_probe,
            clock=self.clock,
            request_id=typed_request.request_id,
            command_sha256=command_sha256,
        )
        result = decode_registry_env_sync_result(typed_request, raw_result)
        if result.state == "unknown" or result.observed_urls is None:
            raise RegistryEnvSyncUnavailable()
        committed = result.model_dump(mode="json", by_alias=True)
        committed["state"] = "committed"
        return decode_registry_env_sync_result(typed_request, committed)

    async def status(
        self,
        request: RegistryEnvSyncStatusRequest | dict | bytes | str,
    ) -> RegistryEnvSyncResult:
        status_request = decode_registry_env_sync_status_request(request)
        receipt: dict[str, Any] | None = None
        async with self._data_transaction() as data:
            receipt = await data.sync_result(
                status_request.request_id,
                status_request.command_sha256,
            )
        if receipt is None:
            receipt = RegistryEnvSyncResult(
                schema=REGISTRY_ENV_SYNC_RESULT_PROTOCOL,
                request_id=status_request.request_id,
                command_sha256=status_request.command_sha256,
                state="unknown",
                observed_urls=None,
            )
        return decode_registry_env_sync_status_result(status_request, receipt)

    async def close(self) -> None:
        """Connections are operation-scoped; the manager owns no pool."""


__all__ = ["RegistryEnvSyncManager"]
