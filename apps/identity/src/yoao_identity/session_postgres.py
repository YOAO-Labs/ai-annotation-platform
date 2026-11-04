"""Function-only PostgreSQL binding for private session commands."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from .contract import IdentityUnavailable
from .session_contract import (
    decode_login_candidate,
    decode_native_session_command,
    decode_native_session_reply,
)


class SessionPostgresData:
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
                server_settings={"application_name": "aap-identity-session-commands"},
            )
            async with connection.transaction():
                if function == "session_policy":
                    raw = await connection.fetchval(
                        "SELECT aap_identity_sessions_v1.session_policy()"
                    )
                    required = [
                        "aap_identity_sessions_v1.session_policy()",
                        "aap_identity_sessions_v1.read_login_candidate(text)",
                        "aap_identity_sessions_v1.execute_session_command(jsonb)",
                    ]
                    allowed = await connection.fetchval(
                        "SELECT bool_and(has_function_privilege(current_user, capability, 'EXECUTE')) "
                        "FROM unnest($1::text[]) AS required(capability)",
                        required,
                    )
                    if allowed is not True:
                        raise IdentityUnavailable()
                elif function == "read_login_candidate":
                    raw = await connection.fetchval(
                        "SELECT aap_identity_sessions_v1.read_login_candidate($1::text)",
                        *args,
                    )
                elif function == "execute_session_command":
                    raw = await connection.fetchval(
                        "SELECT aap_identity_sessions_v1.execute_session_command($1::jsonb)",
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

    async def session_policy(self) -> dict:
        return await self._call("session_policy")

    async def read_login_candidate(self, email: str) -> dict:
        return await self._call(
            "read_login_candidate", email, validate_reply=decode_login_candidate
        )

    async def execute_session_command(self, payload: dict) -> dict:
        try:
            command = decode_native_session_command(payload)
            encoded = json.dumps(
                command.model_dump(mode="json", by_alias=True),
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise IdentityUnavailable() from None
        return await self._call(
            "execute_session_command",
            encoded,
            validate_reply=lambda result: decode_native_session_reply(command, result),
        )
