"""Function-only PostgreSQL binding; every call has a fresh bounded transaction."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime

from .contract import IdentityUnavailable


class PostgresData:
    def __init__(
        self, dsn: str, *, connect_timeout: float = 5, command_timeout: float = 10
    ):
        self.dsn = dsn
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout

    async def _call(self, function: str, *args) -> dict:
        import asyncpg

        connection = None
        try:
            connection = await asyncpg.connect(
                self.dsn,
                timeout=self.connect_timeout,
                command_timeout=self.command_timeout,
                server_settings={"application_name": "aap-identity-admission"},
            )
            async with connection.transaction():
                params = ",".join(f"${i}" for i in range(1, len(args) + 1))
                raw = await connection.fetchval(
                    f"SELECT aap_identity_v1.{function}({params})", *args
                )
                if function == "authentication_policy":
                    allowed = await connection.fetchval(
                        "SELECT bool_and(has_function_privilege(current_user, capability, 'EXECUTE')) "
                        "FROM unnest($1::text[]) AS required(capability)",
                        [
                            "aap_identity_v1.authentication_policy()",
                            "aap_identity_v1.read_jwt_principal(uuid,uuid,bigint,timestamptz,boolean)",
                            "aap_identity_v1.read_api_key_candidates(text,uuid,integer)",
                            "aap_identity_v1.confirm_api_key_principal(uuid,text)",
                        ],
                    )
                    if allowed is not True:
                        raise IdentityUnavailable()
                return json.loads(raw) if isinstance(raw, str) else raw
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

    async def authentication_policy(self) -> dict:
        return await self._call("authentication_policy")

    async def read_jwt_principal(
        self,
        actor_id: uuid.UUID,
        jti: uuid.UUID,
        generation: int,
        expires_at: datetime,
        for_refresh: bool = False,
    ) -> dict:
        return await self._call(
            "read_jwt_principal", actor_id, jti, generation, expires_at, for_refresh
        )

    async def read_api_key_candidates(
        self, prefix: str, after_id: uuid.UUID | None, limit: int
    ) -> dict:
        return await self._call("read_api_key_candidates", prefix, after_id, limit)

    async def confirm_api_key_principal(
        self, key_id: uuid.UUID, expected_hash: str
    ) -> dict:
        return await self._call("confirm_api_key_principal", key_id, expected_hash)
