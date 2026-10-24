"""Function-only PostgreSQL adapter for current account activity."""

from __future__ import annotations

import asyncio
import uuid

from .contract import IdentityUnavailable


class ActorStatusPostgresData:
    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 10
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def read_actor_status(self, actor_id: uuid.UUID) -> dict | None:
        try:
            import asyncpg
        except ImportError:
            raise IdentityUnavailable() from None

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-actor-status"},
            )
            async with connection.transaction(readonly=True):
                row = await connection.fetchrow(
                    "SELECT actor_id, is_active "
                    "FROM aap_identity_v1.read_actor_facts($1::uuid)",
                    actor_id,
                )
            if row is None:
                return None
            return {"actor_id": row["actor_id"], "is_active": row["is_active"]}
        except (
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            KeyError,
            OSError,
            TimeoutError,
            TypeError,
            ValueError,
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
                    task = asyncio.current_task()
                    if task is None or not task.cancelling():
                        raise IdentityUnavailable() from None
