"""Function-only PostgreSQL binding for template read transactions."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager

from aap_workspace_contract.wire import _json_bytes
from .template_query_contract import (
    TemplateQueryUnavailable,
    decode_template_query_policy,
)

TEMPLATE_QUERY_SCHEMA = "aap_workspace_template_query_v1"
TEMPLATE_QUERY_FUNCTIONS = {
    ("template_query_policy", ""),
    ("template_query_step", "jsonb, text, jsonb, text, text[], text"),
}
_logger = logging.getLogger(__name__)


def template_query_step_arguments(request, action, parameters=None, filters=None):
    """Keep opaque request text separate from the late PostgreSQL text binds."""
    return (
        _json_bytes(request).decode("ascii"),
        action,
        (
            json.dumps(parameters, ensure_ascii=True, allow_nan=False)
            if parameters is not None
            else None
        ),
        filters["scope"] if filters is not None else None,
        filters["type_key"] if filters is not None else None,
        filters["search"] if filters is not None else None,
    )


class TemplateQueryPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def step(self, request, action, parameters=None, filters=None):
        return await self.connection.fetchval(
            f"SELECT {TEMPLATE_QUERY_SCHEMA}.template_query_step("
            "$1::jsonb, $2::text, $3::jsonb, $4::text, $5::text[], $6::text)",
            *template_query_step_arguments(request, action, parameters, filters),
        )


class TemplateQueryPostgresData:
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
                server_settings={"application_name": "aap-workspace-template-query"},
            )
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise TemplateQueryUnavailable() from None

    async def _verify_policy(self, connection):
        import asyncpg

        from aap_owner_data.postgres import (
            FunctionRoleUnavailable,
            verify_function_only_role,
        )

        try:
            await verify_function_only_role(
                connection,
                schema=TEMPLATE_QUERY_SCHEMA,
                functions=TEMPLATE_QUERY_FUNCTIONS,
            )
            decode_template_query_policy(
                await connection.fetchval(
                    f"SELECT {TEMPLATE_QUERY_SCHEMA}.template_query_policy()"
                )
            )
        except TemplateQueryUnavailable:
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
            raise TemplateQueryUnavailable() from None

    async def _close(self, connection, *, primary_failure):
        import asyncpg

        try:
            await asyncio.shield(connection.close(timeout=2))
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
        ):
            connection.terminate()
            if not primary_failure:
                raise TemplateQueryUnavailable() from None
            _logger.warning("Workspace template-query connection cleanup failed")

    @asynccontextmanager
    async def transaction(self):
        import asyncpg

        connection = None
        try:
            connection = await self._connect()
            async with connection.transaction(readonly=True):
                await self._verify_policy(connection)
                yield TemplateQueryPostgresTransaction(connection)
        except asyncpg.PostgresError as error:
            state = error.sqlstate or ""
            if state.startswith("08") or state in {"57P01", "57P02", "57P03"}:
                raise TemplateQueryUnavailable() from None
            raise
        except (asyncpg.InterfaceError, OSError, TimeoutError):
            raise TemplateQueryUnavailable() from None
        finally:
            if connection is not None:
                await self._close(
                    connection, primary_failure=sys.exc_info()[0] is not None
                )

    async def template_query_policy(self):
        connection = await self._connect()
        try:
            await self._verify_policy(connection)
        finally:
            await self._close(connection, primary_failure=sys.exc_info()[0] is not None)
