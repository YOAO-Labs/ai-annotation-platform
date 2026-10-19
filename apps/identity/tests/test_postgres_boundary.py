from contextlib import asynccontextmanager
import json

import asyncpg
import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.postgres import PostgresData


@pytest.mark.asyncio
async def test_readiness_requires_every_admission_capability_and_closes_connection(
    monkeypatch,
):
    class Connection:
        closed = False

        @asynccontextmanager
        async def transaction(self):
            yield

        async def fetchval(self, sql, *args):
            if "has_function_privilege" in sql:
                return False
            return json.dumps(
                {
                    "schema": "aap-identity-policy/v1",
                    "issuer": "aap-identity/v1",
                    "refresh_grace_seconds": 604800,
                }
            )

        async def close(self, **kwargs):
            self.closed = True

    connection = Connection()

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    with pytest.raises(IdentityUnavailable):
        await PostgresData("postgresql://not-used").authentication_policy()
    assert connection.closed
