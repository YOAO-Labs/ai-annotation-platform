"""Function-only asyncpg binding for the Labeling video-writer capability."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import sys
from typing import Any

from .video_writer_contract import (
    MAX_VIDEO_WRITER_NATIVE_BYTES,
    VideoWriterFailure,
    VideoWriterUnavailable,
    decode_video_writer_policy_reply,
    video_writer_data_arguments_bytes,
)

_SCHEMA = "aap_labeling_v1"
_STEP_SQL = f"SELECT {_SCHEMA}.video_writer_step($1::text,$2::text)"
_FUNCTIONS = {("video_writer_step", "text, text")}
_BIND_TRANSACTION_SQL = "SELECT pg_catalog.pg_current_xact_id()::text"
_TRANSACTION_SQL = "SELECT pg_catalog.pg_current_xact_id_if_assigned()::text"
_logger = logging.getLogger(__name__)


class VideoWriterPostgresTransaction:
    """One fixed Native capability bound to one active READ COMMITTED transaction."""

    def __init__(
        self,
        connection: Any,
        transaction_id: str,
        *,
        owns_transaction: bool,
        command_timeout: float,
    ) -> None:
        self._connection = connection
        self._transaction_id = transaction_id
        self._active = True
        self.owns_transaction = owns_transaction
        self._command_timeout = command_timeout

    @classmethod
    async def bind(
        cls,
        connection: Any,
        *,
        owns_transaction: bool = False,
        command_timeout: float = 30,
    ) -> VideoWriterPostgresTransaction:
        try:
            if connection.is_in_transaction() is not True:
                raise VideoWriterUnavailable()
            isolation = await connection.fetchval("SHOW transaction_isolation")
            if isolation != "read committed":
                raise VideoWriterUnavailable()
            from aap_owner_data.postgres import (
                FunctionRoleUnavailable,
                verify_function_only_role,
            )

            try:
                await verify_function_only_role(
                    connection,
                    schema=_SCHEMA,
                    functions=_FUNCTIONS,
                    role="labeling_video_writer_client",
                )
            except FunctionRoleUnavailable:
                raise VideoWriterUnavailable() from None
            transaction_id = await connection.fetchval(_BIND_TRANSACTION_SQL)
            if type(transaction_id) is not str or not transaction_id.isdecimal():
                raise VideoWriterUnavailable()
            return cls(
                connection,
                transaction_id,
                owns_transaction=owns_transaction,
                command_timeout=command_timeout,
            )
        except (VideoWriterUnavailable, asyncio.CancelledError):
            raise
        except Exception:
            raise VideoWriterUnavailable() from None

    async def _assert_transaction(self) -> None:
        try:
            if (
                not self._active
                or self._connection.is_in_transaction() is not True
                or await self._connection.fetchval(_TRANSACTION_SQL)
                != self._transaction_id
            ):
                raise VideoWriterUnavailable()
        except (VideoWriterUnavailable, asyncio.CancelledError):
            raise
        except Exception:
            raise VideoWriterUnavailable() from None

    async def call(self, action: str, arguments: Any) -> Any:
        """Return Native JSON for strict policy decoding before transaction exit."""
        try:
            request = video_writer_data_arguments_bytes(action, arguments)
            if (
                type(request) is not bytes
                or len(request) > MAX_VIDEO_WRITER_NATIVE_BYTES
            ):
                raise VideoWriterUnavailable()
            await self._assert_transaction()
            return await self._connection.fetchval(
                _STEP_SQL,
                action,
                request.decode("utf-8", errors="strict"),
                timeout=self._command_timeout,
            )
        except (VideoWriterFailure, asyncio.CancelledError):
            raise
        except Exception:
            raise VideoWriterUnavailable() from None

    def _end(self) -> None:
        self._active = False


class VideoWriterPostgresProvider:
    """Open an owned transaction, or lend an existing asyncpg transaction safely."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        connection: Any | None = None,
        connect_timeout: float = 5,
        command_timeout: float = 30,
    ) -> None:
        if (dsn is None) == (connection is None):
            raise ValueError("Select exactly one DSN or borrowed connection")
        if not 0 < connect_timeout <= 60 or not 0 < command_timeout <= 3600:
            raise ValueError("Invalid video-writer database timeout")
        self._dsn = (
            dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
            if dsn is not None
            else None
        )
        self._borrowed_connection = connection
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout

    @classmethod
    def borrowed(
        cls, connection: Any, *, command_timeout: float = 30
    ) -> VideoWriterPostgresProvider:
        return cls(
            connection=connection,
            command_timeout=command_timeout,
        )

    async def _connect(self):
        import asyncpg

        try:
            return await asyncpg.connect(
                self._dsn,
                timeout=self._connect_timeout,
                command_timeout=self._command_timeout,
                server_settings={"application_name": "aap-labeling-video-writer"},
            )
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            TypeError,
            ValueError,
        ):
            raise VideoWriterUnavailable() from None

    async def _close(self, connection, *, primary_failure: bool) -> None:
        import asyncpg

        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            connection.terminate()
            if not primary_failure:
                raise VideoWriterUnavailable() from None
            _logger.warning("Labeling video-writer connection cleanup failed")

    @asynccontextmanager
    async def transaction(self):
        """Yield a strict adapter; only this provider commits an owned transaction."""
        if self._borrowed_connection is not None:
            try:
                async with asyncio.timeout(self._command_timeout):
                    adapter = await VideoWriterPostgresTransaction.bind(
                        self._borrowed_connection,
                        owns_transaction=False,
                        command_timeout=self._command_timeout,
                    )
            except TimeoutError:
                raise VideoWriterUnavailable() from None
            try:
                yield adapter
            finally:
                adapter._end()
            return

        import asyncpg

        connection = await self._connect()
        adapter = None
        try:
            async with connection.transaction(isolation="read_committed"):
                async with asyncio.timeout(self._command_timeout):
                    adapter = await VideoWriterPostgresTransaction.bind(
                        connection,
                        owns_transaction=True,
                        command_timeout=self._command_timeout,
                    )
                yield adapter
        except (VideoWriterFailure, asyncio.CancelledError):
            raise
        except asyncpg.PostgresError as error:
            if (error.sqlstate or "").startswith("08") or error.sqlstate in {
                "57P01",
                "57P02",
                "57P03",
            }:
                raise VideoWriterUnavailable() from None
            raise
        except (asyncpg.InterfaceError, OSError, TimeoutError):
            raise VideoWriterUnavailable() from None
        finally:
            if adapter is not None:
                adapter._end()
            await self._close(
                connection,
                primary_failure=sys.exc_info()[0] is not None,
            )

    async def policy(self):
        """Return a validated policy only after the exact function-only LOGIN check."""
        async with self.transaction() as transaction:
            raw = await transaction.call("policy", {})
            return decode_video_writer_policy_reply(raw)
