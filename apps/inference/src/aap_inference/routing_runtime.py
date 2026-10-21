"""Fresh-connection runtime assembly for the independent routing owner."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aap_inference_contract.routing import (
    RoutingCommand,
    RoutingRuntimeRequest,
    RoutingRuntimeResult,
    RoutingStatusRequest,
    RoutingUnavailable,
)

from .routing import RoutingManager
from .routing_backend_control import execute_backend_control
from .routing_postgres import RoutingPostgresData

_logger = logging.getLogger(__name__)


class RoutingRuntime:
    """Run the portable manager with one restricted PostgreSQL connection per call.

    No pool or connection is shared across event loops. Each manager operation
    opens an independent transaction, binds the function-only Native port, and
    commits only that routing operation.
    """

    def __init__(
        self,
        dsn: str,
        *,
        health_max_age_seconds: int = 90,
        lease_ttl_ms: int = 120_000,
        backend_control: Callable[[Mapping[str, Any]], Awaitable[object]] | None = None,
        transport_subject_opener=None,
        connect_timeout_seconds: float = 5.0,
        command_timeout_seconds: float = 30.0,
    ) -> None:
        if (
            type(dsn) is not str
            or not dsn.strip()
            or any(character.isspace() for character in dsn)
            or type(health_max_age_seconds) is not int
            or not 1 <= health_max_age_seconds <= 3600
            or type(lease_ttl_ms) is not int
            or not 1 <= lease_ttl_ms <= 3_600_000
            or type(connect_timeout_seconds) not in {int, float}
            or not math.isfinite(float(connect_timeout_seconds))
            or connect_timeout_seconds <= 0
            or type(command_timeout_seconds) not in {int, float}
            or not math.isfinite(float(command_timeout_seconds))
            or command_timeout_seconds <= 0
            or (backend_control is not None and not callable(backend_control))
            or (
                transport_subject_opener is not None
                and not callable(transport_subject_opener)
            )
        ):
            raise ValueError("Invalid Inference routing runtime settings")
        self.dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.command_timeout_seconds = float(command_timeout_seconds)
        self.manager = RoutingManager(
            self._transaction,
            health_max_age_seconds=health_max_age_seconds,
            lease_ttl_ms=lease_ttl_ms,
            backend_control=(
                execute_backend_control if backend_control is None else backend_control
            ),
            transport_subject_opener=transport_subject_opener,
        )

    async def execute(self, command: RoutingCommand | dict | bytes | str):
        return await self.manager.execute(command)

    async def status(self, request: RoutingStatusRequest | dict | bytes | str):
        return await self.manager.status(request)

    async def runtime(
        self, request: RoutingRuntimeRequest | dict | bytes | str
    ) -> RoutingRuntimeResult:
        return await self.manager.runtime(request)

    async def readiness(self) -> bool:
        return await self.manager.readiness()

    async def close(self) -> None:
        await self.manager.close()

    @asynccontextmanager
    async def _transaction(self):
        import asyncpg

        connection = None
        transaction = None
        primary_error: BaseException | None = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout_seconds,
                command_timeout=self.command_timeout_seconds,
                server_settings={"application_name": "aap-inference-routing"},
            )
            transaction = connection.transaction(readonly=False)
            await transaction.start()
            data = await RoutingPostgresData.bind(connection)
            try:
                yield data
            except BaseException:
                try:
                    await asyncio.shield(transaction.rollback())
                except BaseException:
                    # Preserve the failure that prevented this operation from
                    # reaching its normal commit path.
                    pass
                raise
            await transaction.commit()
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if connection is not None:
                await self._close_connection(
                    connection,
                    primary_failure=primary_error is not None,
                )

    @staticmethod
    async def _close_connection(connection, *, primary_failure: bool) -> None:
        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning("Inference routing connection cleanup failed")
                return
            raise
        except BaseException:
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning("Inference routing connection cleanup failed")
                return
            raise RoutingUnavailable() from None


__all__ = ["RoutingRuntime"]
