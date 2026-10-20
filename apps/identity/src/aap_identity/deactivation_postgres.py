"""Function-only PostgreSQL bindings for Identity deactivation commands."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from .contract import IdentityUnavailable
from .deactivation_contract import (
    DeactivationDueFailure,
    DeactivationDuePolicyReply,
    DeactivationDueReply,
    DeactivationDueRequest,
    DeactivationPolicyReply,
    NativeDeactivationCommand,
    decode_deactivation_due_policy,
    decode_deactivation_due_reply,
    decode_deactivation_policy,
    decode_native_deactivation_command,
    decode_native_deactivation_due_reply,
    native_deactivation_due_command,
)


class DeactivationPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def execute_deactivation_command(self, payload: dict | bytes) -> dict:
        command: NativeDeactivationCommand = decode_native_deactivation_command(payload)
        try:
            encoded = json.dumps(
                command.model_dump(mode="json", by_alias=True),
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            raw = await self.connection.fetchval(
                "SELECT aap_identity_deactivation_v1.execute_deactivation_command("
                "$1::jsonb)",
                encoded,
            )
            return json.loads(raw) if isinstance(raw, str) else raw
        except IdentityUnavailable:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise IdentityUnavailable() from None


class DeactivationPostgresData:
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
                server_settings={"application_name": "aap-identity-self-deactivation"},
            )
            async with connection.transaction():
                yield DeactivationPostgresTransaction(connection)
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

    async def deactivation_policy(self) -> DeactivationPolicyReply:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-self-deactivation"},
            )
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_identity_deactivation_v1.deactivation_policy()"
                )
                granted = await connection.fetch(
                    """
                    SELECT p.proname, oidvectortypes(p.proargtypes) AS args
                    FROM pg_proc AS p
                    JOIN pg_namespace AS n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'aap_identity_deactivation_v1'
                      AND has_function_privilege(current_user, p.oid, 'EXECUTE')
                    ORDER BY p.proname, args
                    """
                )
                actual = {f"{row['proname']}({row['args']})" for row in granted}
                if actual != {
                    "deactivation_policy()",
                    "execute_deactivation_command(jsonb)",
                }:
                    raise IdentityUnavailable()
                value = json.loads(raw) if isinstance(raw, str) else raw
                return decode_deactivation_policy(value)
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


class DeactivationDuePostgresData:
    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 10
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def process_due_deactivations(
        self, request: DeactivationDueRequest
    ) -> DeactivationDueReply:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-deactivation-due"},
            )
            async with connection.transaction():
                command = native_deactivation_due_command(request)
                encoded = json.dumps(
                    command.model_dump(mode="json", by_alias=True),
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                raw = await connection.fetchval(
                    "SELECT aap_identity_deactivation_v1.process_due_deactivations("
                    "$1::jsonb)",
                    encoded,
                )
                native = decode_native_deactivation_due_reply(
                    request, json.loads(raw) if isinstance(raw, str) else raw
                )
                # Validate the exact public projection before transaction commit.
                return decode_deactivation_due_reply(
                    request,
                    {
                        "schema": "aap-identity-deactivation-due/v1",
                        "ok": True,
                        "operation_id": str(native.operation_id),
                        "processed": native.processed,
                        "examined": native.examined,
                        "blocked_last_super_admin": native.blocked_last_super_admin,
                        "has_more": native.has_more,
                    },
                )
        except (DeactivationDueFailure, IdentityUnavailable):
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

    async def due_policy(self) -> DeactivationDuePolicyReply:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-deactivation-due"},
            )
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_identity_deactivation_v1.due_policy()"
                )
                granted = await connection.fetch(
                    """
                    SELECT p.proname, oidvectortypes(p.proargtypes) AS args
                    FROM pg_proc AS p
                    JOIN pg_namespace AS n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'aap_identity_deactivation_v1'
                      AND has_function_privilege(current_user, p.oid, 'EXECUTE')
                    ORDER BY p.proname, args
                    """
                )
                actual = {f"{row['proname']}({row['args']})" for row in granted}
                if actual != {
                    "due_policy()",
                    "process_due_deactivations(jsonb)",
                }:
                    raise IdentityUnavailable()
                value = json.loads(raw) if isinstance(raw, str) else raw
                return decode_deactivation_due_policy(value)
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
