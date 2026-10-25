"""Function-only PostgreSQL transactions for the Directory query owner."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import sys

from aap_workspace_contract.wire import _json_bytes
from .directory_query_contract import (
    DirectoryQueryUnavailable,
    decode_directory_query_policy,
)

DIRECTORY_QUERY_SCHEMA = "aap_workspace_directory_query_v1"
DIRECTORY_QUERY_FUNCTIONS = {
    ("directory_query_policy", ""),
    (
        "directory_query_step",
        "jsonb, text, jsonb",
    ),
}
_logger = logging.getLogger(__name__)


def directory_query_step_arguments(request, action, parameters=None):
    return (
        _json_bytes(request).decode("ascii"),
        action,
        _json_bytes(parameters).decode("ascii") if parameters is not None else None,
    )


class DirectoryQueryPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def step(self, request, action, parameters=None):
        return await self.connection.fetchval(
            f"SELECT {DIRECTORY_QUERY_SCHEMA}.directory_query_step("
            "$1::jsonb,$2::text,$3::jsonb)",
            *directory_query_step_arguments(request, action, parameters),
        )


class DirectoryQueryPostgresData:
    def __init__(self, dsn, *, connect_timeout=5, command_timeout=30):
        self.dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def _connect(self):
        import asyncpg

        try:
            return await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-workspace-directory-query"},
            )
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise DirectoryQueryUnavailable() from None

    async def _verify_policy(self, connection):
        import asyncpg
        from aap_owner_data.postgres import (
            FunctionRoleUnavailable,
            verify_function_only_role,
        )

        try:
            await verify_function_only_role(
                connection,
                schema=DIRECTORY_QUERY_SCHEMA,
                functions=DIRECTORY_QUERY_FUNCTIONS,
            )
            decode_directory_query_policy(
                await connection.fetchval(
                    f"SELECT {DIRECTORY_QUERY_SCHEMA}.directory_query_policy()"
                )
            )
        except DirectoryQueryUnavailable:
            raise
        except (
            FunctionRoleUnavailable,
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise DirectoryQueryUnavailable() from None

    async def _close(self, connection, *, primary_failure):
        import asyncpg

        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            connection.terminate()
            if not primary_failure:
                raise DirectoryQueryUnavailable() from None
            _logger.warning("Workspace Directory-query connection cleanup failed")

    @asynccontextmanager
    async def transaction(self):
        import asyncpg

        connection = None
        try:
            connection = await self._connect()
            async with connection.transaction(readonly=True):
                await self._verify_policy(connection)
                yield DirectoryQueryPostgresTransaction(connection)
        except asyncpg.PostgresError as error:
            if (error.sqlstate or "").startswith("08") or error.sqlstate in {
                "57P01",
                "57P02",
                "57P03",
            }:
                raise DirectoryQueryUnavailable() from None
            raise
        except (asyncpg.InterfaceError, OSError, TimeoutError):
            raise DirectoryQueryUnavailable() from None
        finally:
            if connection is not None:
                await self._close(
                    connection, primary_failure=sys.exc_info()[0] is not None
                )

    async def directory_query_policy(self):
        connection = await self._connect()
        try:
            await self._verify_policy(connection)
        finally:
            await self._close(connection, primary_failure=sys.exc_info()[0] is not None)
