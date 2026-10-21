"""Function-only PostgreSQL binding for the standalone Workspace owner."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from aap_workspace_contract.wire import _json_bytes
from .owner_transfer_contract import (
    OWNER_TRANSFER_PROTOCOL,
    OwnerTransferError,
    OwnerTransferUnavailable,
    decode_owner_transfer_policy,
    decode_owner_transfer_request,
    owner_transfer_failure_reply,
)

_OWNER_TRANSFER_SCHEMA = "aap_workspace_owner_transfer_v1"
_OWNER_TRANSFER_FUNCTIONS = {
    ("owner_transfer_policy", ""),
    ("transfer_project_owner", "jsonb"),
}
_BUSY_SQLSTATES = {"55P03", "40001", "40P01"}
_ACCOUNT_LOCK_HINT = "aap.workspace.owner_transfer.account_lock"
_PROJECT_LOCK_HINT = "aap.workspace.owner_transfer.project_lock"
_ACCOUNT_BUSY_DETAIL = "账号正在更新，请刷新后重试"


def _mapped_busy_error(error) -> OwnerTransferError | None:
    """Map only the explicit native HINT marker and declared lock SQLSTATE."""

    if getattr(error, "sqlstate", None) not in _BUSY_SQLSTATES:
        return None
    hint = getattr(error, "hint", None)
    if not isinstance(hint, str) or not hint:
        return None
    marker = hint.splitlines()[0]
    if marker == _ACCOUNT_LOCK_HINT:
        return OwnerTransferError(
            status_code=409,
            detail=_ACCOUNT_BUSY_DETAIL,
            rollback=True,
        )
    if marker == _PROJECT_LOCK_HINT:
        return OwnerTransferError(
            status_code=409,
            detail={"reason": "project_busy"},
            rollback=False,
        )
    return None


class OwnerTransferPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def transfer_project_owner(self, request_json: dict) -> dict | bytes | str:
        import asyncpg

        request = decode_owner_transfer_request(request_json)
        try:
            return await self.connection.fetchval(
                f"SELECT {_OWNER_TRANSFER_SCHEMA}.transfer_project_owner($1::jsonb)",
                _json_bytes(request).decode("ascii"),
            )
        except asyncpg.PostgresError as error:
            mapped = _mapped_busy_error(error)
            if mapped is not None:
                return owner_transfer_failure_reply(mapped).model_dump(
                    mode="json", by_alias=True
                )
            raise


class OwnerTransferPostgresData:
    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout: float = 5,
        command_timeout: float = 30,
    ):
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
                server_settings={"application_name": "aap-workspace-owner-transfer"},
            )
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise OwnerTransferUnavailable() from None

    async def _verify_policy(self, connection) -> None:
        import asyncpg

        try:
            from aap_owner_data.postgres import (
                FunctionRoleUnavailable,
                verify_function_only_role,
            )

            await verify_function_only_role(
                connection,
                schema=_OWNER_TRANSFER_SCHEMA,
                functions=_OWNER_TRANSFER_FUNCTIONS,
            )
            raw = await connection.fetchval(
                f"SELECT {_OWNER_TRANSFER_SCHEMA}.owner_transfer_policy()"
            )
            decode_owner_transfer_policy(raw)
        except OwnerTransferUnavailable:
            raise
        except FunctionRoleUnavailable:
            raise OwnerTransferUnavailable() from None
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
            ImportError,
        ):
            raise OwnerTransferUnavailable() from None

    @asynccontextmanager
    async def transaction(self, *, write: bool):
        import asyncpg

        if write is not True:
            raise OwnerTransferUnavailable()
        connection = None
        try:
            connection = await self._connect()
            async with connection.transaction(readonly=False):
                # The execution gate and the operation share this exact
                # connection/transaction, so readiness cannot go stale.
                await self._verify_policy(connection)
                yield OwnerTransferPostgresTransaction(connection)
        except OwnerTransferUnavailable:
            raise
        except asyncpg.PostgresError as error:
            state = error.sqlstate or ""
            if state.startswith("08") or state in {"57P01", "57P02", "57P03"}:
                raise OwnerTransferUnavailable() from None
            raise
        except (asyncpg.InterfaceError, OSError, TimeoutError, ValueError, TypeError):
            raise OwnerTransferUnavailable() from None
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
                    if not asyncio.current_task().cancelling():
                        raise OwnerTransferUnavailable() from None

    async def owner_transfer_policy(self) -> dict:
        """Check function-only grants and exact policy before advertising ready."""

        connection = None
        try:
            connection = await self._connect()
            await self._verify_policy(connection)
            return {"schema": OWNER_TRANSFER_PROTOCOL}
        finally:
            if connection is not None:
                import asyncpg

                try:
                    await asyncio.shield(connection.close(timeout=2))
                except (
                    asyncpg.PostgresError,
                    asyncpg.InterfaceError,
                    OSError,
                    TimeoutError,
                ):
                    if not asyncio.current_task().cancelling():
                        raise OwnerTransferUnavailable() from None
