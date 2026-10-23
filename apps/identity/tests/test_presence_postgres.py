from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

import asyncpg
import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.presence_contract import PRESENCE_PROTOCOL
from aap_identity.presence_postgres import PresencePostgresData


class Connection:
    def __init__(self, *, result=3, allowed=True):
        self.result = result
        self.allowed = allowed
        self.calls = []
        self.in_transaction = False
        self.committed = False
        self.rolled_back = False
        self.closed = False

    @asynccontextmanager
    async def transaction(self):
        self.in_transaction = True
        try:
            yield
        except Exception:
            self.rolled_back = True
            raise
        else:
            self.committed = True
        finally:
            self.in_transaction = False

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args, self.in_transaction))
        if "has_function_privilege" in sql:
            return self.allowed
        return self.result

    async def close(self, **kwargs):
        self.closed = True


@pytest.mark.asyncio
async def test_presence_data_calls_only_operator_function_in_own_transaction(
    monkeypatch,
):
    connection = Connection()

    async def connect(*args, **kwargs):
        assert kwargs["server_settings"] == {
            "application_name": "aap-identity-presence"
        }
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    cutoff = datetime(2026, 10, 20, tzinfo=UTC)
    reply = await PresencePostgresData(
        "postgresql://presence"
    ).mark_inactive_accounts_offline(cutoff)
    assert reply.model_dump(mode="json", by_alias=True) == {
        "schema": PRESENCE_PROTOCOL,
        "affected": 3,
    }
    assert connection.calls == [
        (
            "SELECT aap_identity_accounts_v1.mark_inactive_accounts_offline($1::timestamptz)",
            (cutoff,),
            True,
        )
    ]
    assert connection.committed and connection.closed


@pytest.mark.asyncio
async def test_presence_data_rejects_non_int64_and_rolls_back(monkeypatch):
    connection = Connection(result=True)

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    with pytest.raises(IdentityUnavailable):
        await PresencePostgresData(
            "postgresql://presence"
        ).mark_inactive_accounts_offline(datetime.now(UTC))
    assert connection.rolled_back and not connection.committed and connection.closed


@pytest.mark.asyncio
async def test_presence_readiness_checks_only_execute_grant(monkeypatch):
    connection = Connection(allowed=True)

    async def connect(*args, **kwargs):
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    await PresencePostgresData("postgresql://presence").check_capability()
    assert len(connection.calls) == 1
    assert "mark_inactive_accounts_offline(timestamptz)" in connection.calls[0][0]
    assert connection.calls[0][1] == () and connection.calls[0][2] is False
    assert connection.closed
