"""Function-only PostgreSQL provider for the offline video restore operator."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping
from typing import Any

from .restore_contract import (
    RESTORE_ACTIONS,
    RESTORE_NATIVE_FUNCTIONS,
    RESTORE_NATIVE_ROLE,
    RESTORE_NATIVE_SCHEMA,
    RestoreUnavailable,
    decode_native_envelope,
)
from .restore import _arguments_json

_STEP_SQL = f"SELECT {RESTORE_NATIVE_SCHEMA}.video_restore_step($1::text, $2::text)"
_FUNCTIONS = {("video_restore_step", "text, text")}
_logger = logging.getLogger(__name__)


class VideoRestorePostgresProvider:
    """Verify the fixed function-only LOGIN and transact one Native step."""

    def __init__(
        self,
        dsn: str,
        *,
        connect_timeout_seconds: float = 5.0,
        command_timeout_seconds: float = 30.0,
    ) -> None:
        if (
            type(dsn) is not str
            or not dsn.strip()
            or any(character.isspace() for character in dsn)
        ):
            raise RestoreUnavailable()
        if (
            type(connect_timeout_seconds) not in {int, float}
            or not math.isfinite(float(connect_timeout_seconds))
            or connect_timeout_seconds <= 0
            or type(command_timeout_seconds) not in {int, float}
            or not math.isfinite(float(command_timeout_seconds))
            or command_timeout_seconds <= 0
        ):
            raise ValueError("Invalid video restore database timeout")
        self.dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.command_timeout_seconds = float(command_timeout_seconds)

    async def step(self, action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if action not in RESTORE_ACTIONS:
            raise RestoreUnavailable()
        # Build and bound the exact JSON before opening a DB connection.
        encoded_arguments = _arguments_json(action, arguments)
        try:
            import asyncpg
        except ImportError:
            raise RestoreUnavailable() from None

        from aap_owner_data.postgres import (
            FunctionRoleUnavailable,
            verify_function_only_role,
        )

        connection = None
        transaction = None
        primary_failure: BaseException | None = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout_seconds,
                command_timeout=self.command_timeout_seconds,
                server_settings={"application_name": "aap-video-restore-operator"},
            )
            transaction = connection.transaction(readonly=False)
            await transaction.start()
            await verify_function_only_role(
                connection,
                schema=RESTORE_NATIVE_SCHEMA,
                functions=_FUNCTIONS,
                role=RESTORE_NATIVE_ROLE,
            )
            policy_raw = await connection.fetchval(_STEP_SQL, "policy", "{}")
            policy = decode_native_envelope(policy_raw, action="policy")
            if (
                policy["functions"] != list(RESTORE_NATIVE_FUNCTIONS)
                or policy["ready"] is not True
            ):
                raise RestoreUnavailable()
            if action == "policy":
                await transaction.commit()
                transaction = None
                return policy
            raw_result = await connection.fetchval(
                _STEP_SQL,
                action,
                encoded_arguments,
            )
            result = decode_native_envelope(raw_result, action=action)
            await transaction.commit()
            transaction = None
            return result
        except RestoreUnavailable as error:
            primary_failure = error
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as error:
            primary_failure = error
            raise
        except (
            FunctionRoleUnavailable,
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ) as error:
            primary_failure = error
            raise RestoreUnavailable() from None
        except BaseException as error:
            primary_failure = error
            raise
        finally:
            if transaction is not None:
                try:
                    await asyncio.shield(transaction.rollback())
                except BaseException:
                    if primary_failure is None:
                        raise RestoreUnavailable() from None
            if connection is not None:
                await self._close_connection(
                    connection,
                    primary_failure=primary_failure is not None,
                )

    async def readiness(self) -> bool:
        try:
            await self.step("policy", {})
        except RestoreUnavailable:
            return False
        return True

    async def _close_connection(self, connection, *, primary_failure: bool) -> None:
        try:
            await asyncio.shield(connection.close(timeout=2))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning("Inference video restore connection cleanup failed")
                return
            raise
        except BaseException:
            try:
                connection.terminate()
            except BaseException:
                pass
            if primary_failure:
                _logger.warning("Inference video restore connection cleanup failed")
                return
            raise RestoreUnavailable() from None

    async def close(self) -> None:
        """The provider has no persistent connections to release."""


__all__ = ["VideoRestorePostgresProvider"]
