"""Dedicated function-only Native role for proof-backed GPU tombstone GC."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg

from aap_inference.gpu.native import (
    GPUCollectorData,
    GPUCollectorPostgresData,
)
from aap_inference.gpu.settings import GPUAuthoritySettings


_MAX_DATABASE_URL_FILE_BYTES = 16 * 1024


class GPUCollectorDatabaseConfigError(ValueError):
    """The collector credential or database privilege boundary is unsafe."""


@dataclass(frozen=True)
class GPUCollectorDatabase:
    pool: asyncpg.Pool
    application_role: str
    collector_role: str

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[GPUCollectorData]:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                yield await GPUCollectorPostgresData.bind(connection)

    async def step(
        self,
        action: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        async with self.transaction() as data:
            return await data.step(action, arguments)

    async def aclose(self) -> None:
        await self.pool.close()


def load_gpu_collector_database_url(config: GPUAuthoritySettings) -> str:
    """Read and validate the collector URL without exposing it in settings/logs."""

    path = config.gpu_arbiter_collector_database_url_file
    if not path or path != path.strip():
        raise GPUCollectorDatabaseConfigError(
            "GPU collector database URL file is not configured"
        )
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_DATABASE_URL_FILE_BYTES + 1)
    except OSError as exc:
        raise GPUCollectorDatabaseConfigError(
            "GPU collector database URL file is unavailable"
        ) from exc
    if not raw:
        raise GPUCollectorDatabaseConfigError(
            "GPU collector database URL file is empty"
        )
    if len(raw) > _MAX_DATABASE_URL_FILE_BYTES:
        raise GPUCollectorDatabaseConfigError(
            "GPU collector database URL file is too large"
        )
    try:
        database_url = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise GPUCollectorDatabaseConfigError(
            "GPU collector database URL file must be UTF-8"
        ) from None
    if not database_url or "\n" in database_url or "\r" in database_url:
        raise GPUCollectorDatabaseConfigError(
            "GPU collector database URL file must contain exactly one URL"
        )
    try:
        parsed = urlsplit(database_url)
        port = parsed.port
    except ValueError as exc:
        raise GPUCollectorDatabaseConfigError(
            "GPU collector database URL is invalid"
        ) from exc
    if (
        parsed.scheme != "postgresql+asyncpg"
        or not parsed.username
        or not parsed.password
        or not parsed.hostname
        or not parsed.path.strip("/")
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise GPUCollectorDatabaseConfigError(
            "GPU collector database URL must be a complete postgresql+asyncpg URL"
        )
    return "postgresql://" + database_url.removeprefix("postgresql+asyncpg://")


def validate_gpu_collector_role_boundary(
    application_role: object,
    collector_role: object,
) -> tuple[str, str]:
    """Require verified Native profiles to be distinct dedicated PostgreSQL roles."""

    if (
        type(application_role) is not str
        or not application_role
        or type(collector_role) is not str
        or not collector_role
        or application_role == collector_role
    ):
        raise GPUCollectorDatabaseConfigError(
            "GPU collector must use a distinct PostgreSQL role"
        )
    return application_role, collector_role


async def open_gpu_collector_database(
    application_role_probe: Callable[[], Awaitable[str]],
    config: GPUAuthoritySettings,
) -> GPUCollectorDatabase:
    """Open the dedicated pool and prove its function-only Native role boundary."""

    database_url = load_gpu_collector_database_url(config)
    pool = await asyncpg.create_pool(dsn=database_url)
    try:
        application_role = await application_role_probe()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await GPUCollectorPostgresData.bind(connection)
                collector_role = await connection.fetchval("SELECT current_user")
        application_role, collector_role = validate_gpu_collector_role_boundary(
            application_role,
            collector_role,
        )
    except BaseException:
        await pool.close()
        raise
    return GPUCollectorDatabase(
        pool=pool,
        application_role=application_role,
        collector_role=collector_role,
    )


__all__ = [
    "GPUCollectorDatabase",
    "GPUCollectorDatabaseConfigError",
    "load_gpu_collector_database_url",
    "open_gpu_collector_database",
    "validate_gpu_collector_role_boundary",
]
