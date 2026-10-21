"""Function-only PostgreSQL row sets for Labeling class usage."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import sys

from .class_query_contract import (
    LabelingClassQueryUnavailable,
    decode_labeling_class_query_policy,
)

CLASS_QUERY_SCHEMA = "aap_labeling_class_query_v1"
CLASS_QUERY_FUNCTIONS = {
    ("class_query_policy", ""),
    ("class_counts", "uuid"),
    ("attribute_rows", "uuid"),
}
_logger = logging.getLogger(__name__)


class LabelingClassQueryPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def class_rows(self, project_id):
        rows = await self.connection.fetch(
            f"SELECT class_name,annotation_count FROM {CLASS_QUERY_SCHEMA}.class_counts($1::uuid)",
            project_id,
        )
        return [(row["class_name"], row["annotation_count"]) for row in rows]

    async def attribute_rows(self, project_id):
        rows = await self.connection.fetch(
            f"SELECT attributes_json FROM {CLASS_QUERY_SCHEMA}.attribute_rows($1::uuid)",
            project_id,
        )
        return [row["attributes_json"] for row in rows]


class LabelingClassQueryPostgresData:
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
                server_settings={"application_name": "aap-labeling-class-query"},
            )
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            TypeError,
            ValueError,
        ):
            raise LabelingClassQueryUnavailable() from None

    async def _verify_policy(self, connection):
        import asyncpg
        from aap_owner_data.postgres import (
            FunctionRoleUnavailable,
            verify_function_only_role,
        )

        try:
            await verify_function_only_role(
                connection, schema=CLASS_QUERY_SCHEMA, functions=CLASS_QUERY_FUNCTIONS
            )
            decode_labeling_class_query_policy(
                await connection.fetchval(
                    f"SELECT {CLASS_QUERY_SCHEMA}.class_query_policy()"
                )
            )
        except LabelingClassQueryUnavailable:
            raise
        except (
            FunctionRoleUnavailable,
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            TypeError,
            ValueError,
        ):
            raise LabelingClassQueryUnavailable() from None

    async def _close(self, connection, *, primary_failure):
        import asyncpg

        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            connection.terminate()
            if not primary_failure:
                raise LabelingClassQueryUnavailable() from None
            _logger.warning("Labeling class-query connection cleanup failed")

    @asynccontextmanager
    async def transaction(self):
        import asyncpg

        connection = None
        try:
            connection = await self._connect()
            async with connection.transaction(
                isolation="read_committed", readonly=True
            ):
                await self._verify_policy(connection)
                yield LabelingClassQueryPostgresTransaction(connection)
        except asyncpg.PostgresError as error:
            if (error.sqlstate or "").startswith("08") or error.sqlstate in {
                "57P01",
                "57P02",
                "57P03",
            }:
                raise LabelingClassQueryUnavailable() from None
            raise
        except (asyncpg.InterfaceError, OSError, TimeoutError):
            raise LabelingClassQueryUnavailable() from None
        finally:
            if connection is not None:
                await self._close(
                    connection, primary_failure=sys.exc_info()[0] is not None
                )

    async def class_query_policy(self):
        async with self.transaction():
            pass
