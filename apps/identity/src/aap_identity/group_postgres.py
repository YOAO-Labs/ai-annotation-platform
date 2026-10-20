"""Function-only PostgreSQL binding for Identity group commands."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from .contract import IdentityUnavailable
from .group_contract import (
    GroupPolicyReply,
    NativeGroupCommand,
    decode_group_policy,
    decode_native_group_command,
)


class GroupPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def execute_group_command(self, payload: dict | bytes) -> dict:
        command: NativeGroupCommand = decode_native_group_command(payload)
        try:
            encoded = json.dumps(
                command.model_dump(mode="json", by_alias=True),
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            raw = await self.connection.fetchval(
                "SELECT aap_identity_groups_v1.execute_group_command($1::jsonb)",
                encoded,
            )
            return json.loads(raw) if isinstance(raw, str) else raw
        except IdentityUnavailable:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise IdentityUnavailable() from None


class GroupPostgresData:
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
                server_settings={"application_name": "aap-identity-group-commands"},
            )
            async with connection.transaction():
                yield GroupPostgresTransaction(connection)
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

    async def group_policy(self) -> GroupPolicyReply:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-group-commands"},
            )
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_identity_groups_v1.group_policy()"
                )
                granted = await connection.fetch(
                    """
                    SELECT p.proname, oidvectortypes(p.proargtypes) AS args
                    FROM pg_proc AS p
                    JOIN pg_namespace AS n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'aap_identity_groups_v1'
                      AND has_function_privilege(current_user, p.oid, 'EXECUTE')
                    ORDER BY p.proname, args
                    """
                )
                actual = {f"{row['proname']}({row['args']})" for row in granted}
                if actual != {
                    "execute_group_command(jsonb)",
                    "group_policy()",
                }:
                    raise IdentityUnavailable()
                value = json.loads(raw) if isinstance(raw, str) else raw
                return decode_group_policy(value)
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
