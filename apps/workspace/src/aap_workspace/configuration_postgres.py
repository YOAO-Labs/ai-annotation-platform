"""Function-only PostgreSQL binding for configuration mutation transactions."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from .configuration_contract import (
    ConfigurationError,
    ConfigurationUnavailable,
    decode_configuration_policy,
)
from aap_workspace_contract.wire import _json_bytes

CONFIGURATION_SCHEMA = "aap_workspace_configuration_v1"
CONFIGURATION_FUNCTIONS = {
    ("configuration_policy", ""),
    ("configuration_step", "jsonb, text, jsonb, text[], text[]"),
}
_BUSY_SQLSTATES = {"55P03", "40001", "40P01"}
_BUSY_MARKERS = {
    "aap.workspace.configuration.account_busy": "account_busy",
    "aap.workspace.configuration.project_busy": "project_busy",
    "aap.workspace.configuration.membership_busy": "membership_busy",
    "aap.workspace.configuration.template_busy": "template_busy",
}
_logger = logging.getLogger(__name__)


class _RollbackOnlySignal(Exception):
    """Tell asyncpg's managed transaction context to roll back normally."""


def configuration_busy_error(state: str | None, hint: str | None):
    if state not in _BUSY_SQLSTATES:
        return None
    reason = _BUSY_MARKERS.get((hint or "").split("\n", 1)[0])
    if reason is None:
        return None
    return ConfigurationError(status_code=409, detail={"reason": reason})


class ConfigurationPostgresTransaction:
    autoflush = True

    def __init__(self, connection):
        self.connection = connection
        self.rollback_on_exit = False

    async def before_project_insert(self):
        """Standalone transactions have no unrelated ORM unit of work to flush."""

    async def step(self, request_json, action, args, columns, values):
        import asyncpg

        from .configuration import ConfigurationCommandFailure

        try:
            return await self.connection.fetchval(
                f"SELECT {CONFIGURATION_SCHEMA}.configuration_step("
                "$1::jsonb, $2::text, $3::jsonb, $4::text[], $5::text[])",
                _json_bytes(request_json).decode("ascii"),
                action,
                json.dumps(args, ensure_ascii=True, allow_nan=False),
                columns,
                values,
            )
        except asyncpg.PostgresError as error:
            if action == "backend" and error.sqlstate == "55000":
                raise ConfigurationUnavailable() from None
            mapped = configuration_busy_error(error.sqlstate, error.hint)
            if mapped is not None:
                raise ConfigurationCommandFailure(mapped) from error
            raise


class ConfigurationPostgresData:
    independent = True

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
                server_settings={"application_name": "aap-workspace-configuration"},
            )
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise ConfigurationUnavailable() from None

    async def _verify_policy(self, connection):
        import asyncpg

        from aap_owner_data.postgres import (
            FunctionRoleUnavailable,
            verify_function_only_role,
        )

        try:
            await verify_function_only_role(
                connection,
                schema=CONFIGURATION_SCHEMA,
                functions=CONFIGURATION_FUNCTIONS,
            )
            decode_configuration_policy(
                await connection.fetchval(
                    f"SELECT {CONFIGURATION_SCHEMA}.configuration_policy()"
                )
            )
        except ConfigurationUnavailable:
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
            raise ConfigurationUnavailable() from None

    @asynccontextmanager
    async def transaction(self, *, write):
        import asyncpg

        if write is not True:
            raise ConfigurationUnavailable()
        connection = None
        try:
            connection = await self._connect()
            try:
                async with connection.transaction():
                    await self._verify_policy(connection)
                    transaction = ConfigurationPostgresTransaction(connection)
                    yield transaction
                    if transaction.rollback_on_exit:
                        raise _RollbackOnlySignal()
            except _RollbackOnlySignal:
                pass
        except asyncpg.PostgresError as error:
            state = error.sqlstate or ""
            if state.startswith("08") or state in {"57P01", "57P02", "57P03"}:
                raise ConfigurationUnavailable() from None
            raise
        except (asyncpg.InterfaceError, OSError, TimeoutError):
            raise ConfigurationUnavailable() from None
        finally:
            if connection is not None:
                try:
                    await asyncio.shield(connection.close(timeout=2))
                except (
                    asyncpg.PostgresError,
                    asyncpg.InterfaceError,
                    OSError,
                    TimeoutError,
                ):
                    # Commit/rollback has already completed. Reporting a close
                    # failure as a failed create could provoke a duplicate on a
                    # new public request, and could mask an earlier rejection.
                    connection.terminate()
                    _logger.warning("Workspace configuration connection cleanup failed")

    async def configuration_policy(self):
        connection = await self._connect()
        try:
            await self._verify_policy(connection)
        finally:
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
                raise ConfigurationUnavailable() from None
