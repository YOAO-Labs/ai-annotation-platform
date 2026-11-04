"""Function-only PostgreSQL binding for presence maintenance."""

from __future__ import annotations

import asyncio

from .contract import IdentityUnavailable
from .presence_contract import PresenceReply, presence_reply


class PresencePostgresData:
    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 10
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def check_capability(self) -> None:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-presence"},
            )
            allowed = await connection.fetchval(
                "SELECT has_function_privilege(current_user, "
                "'aap_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)', "
                "'EXECUTE')"
            )
            if allowed is not True:
                raise IdentityUnavailable()
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

    async def mark_inactive_accounts_offline(self, cutoff) -> PresenceReply:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-presence"},
            )
            async with connection.transaction():
                raw = await connection.fetchval(
                    "SELECT aap_identity_accounts_v1.mark_inactive_accounts_offline($1::timestamptz)",
                    cutoff,
                )
                if type(raw) is not int:
                    raise IdentityUnavailable()
                reply = presence_reply(raw)
            return reply
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
