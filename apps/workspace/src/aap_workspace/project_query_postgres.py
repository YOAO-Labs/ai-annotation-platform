"""Function-only PostgreSQL transactions for the Project query owner."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import sys

from aap_workspace_contract.wire import _json_bytes
from aap_workspace_contract.project_query_contract import ProjectQueryUnavailable, decode_project_query_policy

PROJECT_QUERY_SCHEMA = "aap_workspace_project_query_v1"
PROJECT_QUERY_FUNCTIONS = {
    ("project_query_policy", ""),
    (
        "project_query_step",
        "jsonb, text, jsonb, text, text, text[], text[], uuid, text, timestamp with time zone, timestamp with time zone, bigint, integer",
    ),
}
_logger = logging.getLogger(__name__)


def project_query_step_arguments(request, action, parameters=None, filters=None):
    """Keep user text in late binds and preserve the count-before-offset phase."""
    values = [None] * 10
    if action in ("list_rows", "count_rows"):
        if filters is None:
            raise ProjectQueryUnavailable()
        values[:8] = [
            filters[key]
            for key in (
                "status",
                "search",
                "type_key",
                "data_type",
                "member_id",
                "project_role",
                "created_from",
                "created_to",
            )
        ]
        if action == "list_rows" and request.pagination is not None:
            values[8] = (
                int(request.pagination.page) - 1
            ) * request.pagination.page_size
            values[9] = request.pagination.page_size
    elif action == "search_rows":
        if filters is None:
            raise ProjectQueryUnavailable()
        values[1], values[9] = filters["q"], filters["limit"]
    return (
        _json_bytes(request).decode("ascii"),
        action,
        _json_bytes(parameters).decode("ascii") if parameters is not None else None,
        *values,
    )


class ProjectQueryPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def step(self, request, action, parameters=None, filters=None):
        return await self.connection.fetchval(
            f"SELECT {PROJECT_QUERY_SCHEMA}.project_query_step("
            "$1::jsonb,$2::text,$3::jsonb,$4::text,$5::text,$6::text[],"
            "$7::text[],$8::uuid,$9::text,$10::timestamptz,$11::timestamptz,"
            "$12::bigint,$13::integer)",
            *project_query_step_arguments(request, action, parameters, filters),
        )


class ProjectQueryPostgresData:
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
                server_settings={"application_name": "aap-workspace-project-query"},
            )
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise ProjectQueryUnavailable() from None

    async def _verify_policy(self, connection):
        import asyncpg
        from aap_owner_data.postgres import (
            FunctionRoleUnavailable,
            verify_function_only_role,
        )

        try:
            await verify_function_only_role(
                connection,
                schema=PROJECT_QUERY_SCHEMA,
                functions=PROJECT_QUERY_FUNCTIONS,
            )
            decode_project_query_policy(
                await connection.fetchval(
                    f"SELECT {PROJECT_QUERY_SCHEMA}.project_query_policy()"
                )
            )
        except ProjectQueryUnavailable:
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
            raise ProjectQueryUnavailable() from None

    async def _close(self, connection, *, primary_failure):
        import asyncpg

        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            connection.terminate()
            if not primary_failure:
                raise ProjectQueryUnavailable() from None
            _logger.warning("Workspace Project-query connection cleanup failed")

    @asynccontextmanager
    async def transaction(self):
        import asyncpg

        connection = None
        try:
            connection = await self._connect()
            async with connection.transaction(readonly=True):
                await self._verify_policy(connection)
                yield ProjectQueryPostgresTransaction(connection)
        except asyncpg.PostgresError as error:
            if (error.sqlstate or "").startswith("08") or error.sqlstate in {
                "57P01",
                "57P02",
                "57P03",
            }:
                raise ProjectQueryUnavailable() from None
            raise
        except (asyncpg.InterfaceError, OSError, TimeoutError):
            raise ProjectQueryUnavailable() from None
        finally:
            if connection is not None:
                await self._close(
                    connection, primary_failure=sys.exc_info()[0] is not None
                )

    async def project_query_policy(self):
        connection = await self._connect()
        try:
            await self._verify_policy(connection)
        finally:
            await self._close(connection, primary_failure=sys.exc_info()[0] is not None)
