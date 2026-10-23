"""Function-only PostgreSQL account transaction binding."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from .account_contract import (
    NativeAccountPolicyReply,
    NativeAccountReply,
    NativePrepareReply,
    decode_account_policy,
    decode_native_account_reply,
    decode_native_apply_command,
    decode_native_prepare_command,
    decode_native_prepare_reply,
)
from .contract import IdentityUnavailable


class AccountPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def prepare_account_command(
        self, payload: dict | bytes
    ) -> NativePrepareReply:
        command = decode_native_prepare_command(payload)
        try:
            encoded = json.dumps(
                command.model_dump(mode="json", by_alias=True),
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            raw = await self.connection.fetchval(
                "SELECT aap_identity_accounts_v1.prepare_account_command($1::jsonb)",
                encoded,
            )
            result = json.loads(raw) if isinstance(raw, str) else raw
            validated = decode_native_prepare_reply(command, result)
            return validated
        except IdentityUnavailable:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise IdentityUnavailable() from None

    async def apply_account_command(self, payload: dict | bytes) -> NativeAccountReply:
        command = decode_native_apply_command(payload)
        try:
            encoded = json.dumps(
                command.model_dump(mode="json", by_alias=True),
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            raw = await self.connection.fetchval(
                "SELECT aap_identity_accounts_v1.apply_account_command($1::jsonb)",
                encoded,
            )
            result = json.loads(raw) if isinstance(raw, str) else raw
            validated = decode_native_account_reply(command, result)
            return validated
        except IdentityUnavailable:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise IdentityUnavailable() from None


class AccountPostgresData:
    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 10
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    @asynccontextmanager
    async def transaction(self):
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-account-commands"},
            )
            async with connection.transaction():
                yield AccountPostgresTransaction(connection)
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

    async def account_policy(self) -> NativeAccountPolicyReply:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-account-commands"},
            )
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_identity_accounts_v1.account_policy()"
                )
                required = [
                    "aap_identity_accounts_v1.account_policy()",
                    "aap_identity_accounts_v1.prepare_account_command(jsonb)",
                    "aap_identity_accounts_v1.apply_account_command(jsonb)",
                ]
                allowed = await connection.fetchval(
                    "SELECT bool_and(has_function_privilege(current_user, capability, 'EXECUTE')) "
                    "FROM unnest($1::text[]) AS required(capability)",
                    required,
                )
                if allowed is not True:
                    raise IdentityUnavailable()
                value = json.loads(raw) if isinstance(raw, str) else raw
                return decode_account_policy(value)
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
