"""Function-only Workspace binding; prepare and apply share one transaction."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json

from aap_owner_data.postgres import FunctionRoleUnavailable, verify_function_only_role
from .contract import (
    MEMBERSHIP_NATIVE_TEXT_CODEC,
    MembershipUnavailable,
    MembershipInvalidRequest,
    NativeMembershipApplyRequest,
    decode_membership_request,
    decode_membership_policy,
    decode_native_membership_reply,
    native_membership_command_json,
)

_NATIVE_SCHEMA = "aap-workspace-membership-native/v1"
_OPERATIONS = {
    "add_member",
    "preview_role_change",
    "change_role",
    "remove_member",
}


def _result(value):
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        try:
            return json.loads(bytes(value) if isinstance(value, memoryview) else value)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise MembershipUnavailable() from None
    return value


class MembershipPostgresTransaction:
    def __init__(self, connection):
        self.connection = connection

    async def _command(self, function: str, payload: dict):
        try:
            if function == "apply_membership_command":
                payload = NativeMembershipApplyRequest.model_validate_json(
                    json.dumps(payload, ensure_ascii=True, allow_nan=False)
                ).model_dump(mode="json", by_alias=True)
            request = decode_membership_request(
                payload["request"]
                if function == "apply_membership_command"
                else payload
            )
            raw = await self.connection.fetchval(
                f"SELECT aap_workspace_membership_v1.{function}($1::jsonb)",
                native_membership_command_json(payload),
            )
            reply = decode_native_membership_reply(
                request, raw.encode("utf-8") if isinstance(raw, str) else raw
            )
            return reply.model_dump(mode="json", by_alias=True)
        except (
            MembershipInvalidRequest,
            KeyError,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ):
            raise MembershipUnavailable() from None

    async def prepare_membership_command(self, payload: dict):
        return await self._command("prepare_membership_command", payload)

    async def apply_membership_command(self, payload: dict):
        return await self._command("apply_membership_command", payload)


class MembershipPostgresData:
    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 30
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
                server_settings={"application_name": "aap-workspace-membership"},
            )
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
            ValueError,
            TypeError,
        ):
            raise MembershipUnavailable() from None

    @asynccontextmanager
    async def transaction(self, *, write: bool):
        import asyncpg

        connection = None
        try:
            connection = await self._connect()
            async with connection.transaction(readonly=not write):
                try:
                    policy = await connection.fetchval(
                        "SELECT aap_workspace_membership_v1.membership_policy()"
                    )
                except asyncpg.PostgresError:
                    raise MembershipUnavailable() from None
                decode_membership_policy(policy)
                yield MembershipPostgresTransaction(connection)
        except MembershipUnavailable:
            raise
        except asyncpg.PostgresError as error:
            state = error.sqlstate or ""
            if state.startswith("08") or state in {"57P01", "57P02", "57P03"}:
                raise MembershipUnavailable() from None
            raise
        except (asyncpg.InterfaceError, OSError, TimeoutError, ValueError, TypeError):
            raise MembershipUnavailable() from None
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
                        raise MembershipUnavailable() from None

    async def membership_policy(self) -> dict:
        """Reject a broad or incomplete credential before marking an owner ready."""
        import asyncpg

        connection = None
        try:
            connection = await self._connect()
            raw = _result(
                await connection.fetchval(
                    "SELECT aap_workspace_membership_v1.membership_policy()"
                )
            )
            if (
                not isinstance(raw, dict)
                or set(raw) != {"schema", "operations", "opaque_text_codec"}
                or raw["schema"] != _NATIVE_SCHEMA
                or raw["opaque_text_codec"] != MEMBERSHIP_NATIVE_TEXT_CODEC
                or not isinstance(raw["operations"], list)
                or not all(isinstance(value, str) for value in raw["operations"])
                or len(raw["operations"]) != len(_OPERATIONS)
                or set(raw["operations"]) != _OPERATIONS
            ):
                raise MembershipUnavailable()
            await verify_function_only_role(
                connection,
                schema="aap_workspace_membership_v1",
                functions={
                    ("membership_policy", ""),
                    ("prepare_membership_command", "jsonb"),
                    ("apply_membership_command", "jsonb"),
                },
            )
            return raw
        except MembershipUnavailable:
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
            raise MembershipUnavailable() from None
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
                        raise MembershipUnavailable() from None
