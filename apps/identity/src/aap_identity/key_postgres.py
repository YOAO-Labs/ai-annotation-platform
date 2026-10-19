"""Function-only PostgreSQL binding for private API-key commands."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from .contract import IdentityUnavailable
from .key_contract import (
    decode_native_key_command,
    decode_native_key_command_reply,
)


class KeyPostgresData:
    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 10
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def _call(
        self,
        function: str,
        *args,
        validate_reply: Callable[[dict], object] | None = None,
    ) -> dict:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-key-commands"},
            )
            async with connection.transaction():
                if function == "key_policy":
                    raw = await connection.fetchval(
                        "SELECT aap_identity_keys_v1.key_policy()"
                    )
                    required = [
                        "aap_identity_keys_v1.key_policy()",
                        "aap_identity_keys_v1.execute_key_command(jsonb)",
                    ]
                    allowed = await connection.fetchval(
                        "SELECT bool_and(has_function_privilege(current_user, capability, 'EXECUTE')) "
                        "FROM unnest($1::text[]) AS required(capability)",
                        required,
                    )
                    if allowed is not True:
                        raise IdentityUnavailable()
                elif function == "execute_key_command":
                    raw = await connection.fetchval(
                        "SELECT aap_identity_keys_v1.execute_key_command($1::jsonb)",
                        *args,
                    )
                else:
                    raise IdentityUnavailable()
                result = json.loads(raw) if isinstance(raw, str) else raw
                if validate_reply is not None:
                    validate_reply(result)
                return result
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

    async def key_policy(self) -> dict:
        return await self._call("key_policy")

    async def execute_key_command(self, payload: dict) -> dict:
        try:
            command = decode_native_key_command(payload)
            encoded = json.dumps(
                command.model_dump(mode="json", by_alias=True, exclude_unset=True),
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise IdentityUnavailable() from None
        return await self._call(
            "execute_key_command",
            encoded,
            validate_reply=lambda result: decode_native_key_command_reply(
                command, result
            ),
        )
