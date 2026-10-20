"""Function-only PostgreSQL binding for recovery command transactions."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from .contract import IdentityUnavailable
from .recovery_contract import (
    NativeRecoveryPolicyReply,
    decode_native_recovery_apply_command,
    decode_native_recovery_prepare_command,
    decode_native_recovery_prepare_reply,
    decode_native_recovery_reply,
    decode_recovery_policy,
)


class RecoveryPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def prepare_recovery_command(self, payload: dict | bytes):
        command = decode_native_recovery_prepare_command(payload)
        try:
            encoded = json.dumps(
                command.model_dump(mode="json", by_alias=True),
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            raw = await self.connection.fetchval(
                "SELECT aap_identity_recovery_v1.prepare_recovery_command($1::jsonb)",
                encoded,
            )
            value = json.loads(raw) if isinstance(raw, str) else raw
            return decode_native_recovery_prepare_reply(command, value)
        except IdentityUnavailable:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise IdentityUnavailable() from None

    async def apply_recovery_command(self, payload: dict | bytes):
        command = decode_native_recovery_apply_command(payload)
        try:
            encoded = json.dumps(
                command.model_dump(mode="json", by_alias=True),
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            raw = await self.connection.fetchval(
                "SELECT aap_identity_recovery_v1.apply_recovery_command($1::jsonb)",
                encoded,
            )
            value = json.loads(raw) if isinstance(raw, str) else raw
            return decode_native_recovery_reply(command, value)
        except IdentityUnavailable:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise IdentityUnavailable() from None


class RecoveryPostgresData:
    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 10
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def _connect(self, asyncpg):
        return await asyncpg.connect(
            self.dsn,
            timeout=self.connect_timeout,
            command_timeout=self.command_timeout,
            server_settings={"application_name": "aap-identity-recovery"},
        )

    @asynccontextmanager
    async def transaction(self):
        import asyncpg

        connection = None
        try:
            connection = await self._connect(asyncpg)
            async with connection.transaction():
                yield RecoveryPostgresTransaction(connection)
        except IdentityUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise IdentityUnavailable() from None
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
                        raise IdentityUnavailable() from None

    async def recovery_policy(self) -> NativeRecoveryPolicyReply:
        import asyncpg

        connection = None
        try:
            connection = await self._connect(asyncpg)
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_identity_recovery_v1.recovery_policy()"
                )
                required = [
                    "aap_identity_recovery_v1.recovery_policy()",
                    "aap_identity_recovery_v1.prepare_recovery_command(jsonb)",
                    "aap_identity_recovery_v1.apply_recovery_command(jsonb)",
                ]
                allowed = await connection.fetchval(
                    "SELECT bool_and(has_function_privilege(current_user, capability, 'EXECUTE')) "
                    "FROM unnest($1::text[]) AS required(capability)",
                    required,
                )
                if allowed is not True:
                    raise IdentityUnavailable()
                value = json.loads(raw) if isinstance(raw, str) else raw
                return decode_recovery_policy(value)
        except IdentityUnavailable:
            raise
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise IdentityUnavailable() from None
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
                        raise IdentityUnavailable() from None
