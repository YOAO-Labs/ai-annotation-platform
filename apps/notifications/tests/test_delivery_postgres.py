from __future__ import annotations

import json
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from aap_notifications.notification_delivery_contract import (
    NOTIFICATION_DELIVERY_POLICY_PROTOCOL,
    NOTIFICATION_DELIVERY_PROTOCOL,
    NotificationDeliveryUnavailable,
)
from aap_notifications.notification_delivery_postgres import (
    NotificationDeliveryPostgresData,
)


WORKER_ID = uuid.UUID("50000000-0000-4000-8000-000000000001")


def _claim_reply(worker_id: uuid.UUID) -> dict:
    now = datetime.now(UTC)
    return {
        "schema": NOTIFICATION_DELIVERY_PROTOCOL,
        "ok": True,
        "data": {
            "items": [
                {
                    "id": "50000000-0000-4000-8000-000000000002",
                    "user_id": "50000000-0000-4000-8000-000000000003",
                    "type": "user.deactivation_requested",
                    "target_type": "user",
                    "target_id": "50000000-0000-4000-8000-000000000004",
                    "payload": {
                        "email": "person@example.test",
                        "name": "Person",
                        "role": "employee",
                        "scheduled_at": now.isoformat(),
                    },
                    "created_at": now.isoformat(),
                    "worker_id": str(worker_id),
                    "lease_id": "50000000-0000-4000-8000-000000000005",
                    "lease_expires_at": (now + timedelta(seconds=60)).isoformat(),
                }
            ]
        },
    }


class FakeConnection:
    def __init__(self, result):
        self.result = result
        self.in_transaction = False
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.calls = []

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
        return json.dumps(self.result)

    async def close(self, *, timeout):
        assert timeout == 2
        self.closed = True


def _asyncpg(connection):
    async def connect(*args, **kwargs):
        assert kwargs["server_settings"] == {
            "application_name": "aap-notification-publisher"
        }
        return connection

    class PostgresError(Exception):
        pass

    class InterfaceError(Exception):
        pass

    return SimpleNamespace(
        connect=connect,
        PostgresError=PostgresError,
        InterfaceError=InterfaceError,
    )


@pytest.mark.asyncio
async def test_claim_validates_reply_and_worker_binding_before_commit(monkeypatch):
    connection = FakeConnection(_claim_reply(WORKER_ID))
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))

    leases = await NotificationDeliveryPostgresData(
        "postgresql://mail/db"
    ).claim_deliveries(WORKER_ID)

    assert len(leases) == 1
    assert leases[0].worker_id == WORKER_ID
    assert connection.committed and not connection.rolled_back
    assert connection.closed
    assert connection.calls == [
        (
            "SELECT aap_notifications_v1.claim_identity_deliveries($1::jsonb)",
            (connection.calls[0][1][0],),
            True,
        )
    ]
    assert json.loads(connection.calls[0][1][0])["worker_id"] == str(WORKER_ID)

    connection = FakeConnection(_claim_reply(uuid.uuid4()))
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    with pytest.raises(NotificationDeliveryUnavailable):
        await NotificationDeliveryPostgresData("postgresql://mail/db").claim_deliveries(
            WORKER_ID
        )
    assert connection.rolled_back and not connection.committed
    assert connection.closed


@pytest.mark.asyncio
async def test_malformed_finish_reply_rolls_back_owner_mutation(monkeypatch):
    lease = _claim_reply(WORKER_ID)["data"]["items"][0]
    from aap_notifications.notification_delivery_contract import (
        decode_notification_claim_reply,
    )

    item = decode_notification_claim_reply(_claim_reply(WORKER_ID)).data.items[0]
    connection = FakeConnection(
        {
            "schema": NOTIFICATION_DELIVERY_PROTOCOL,
            "ok": True,
            "data": {"accepted": 1},
        }
    )
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))

    with pytest.raises(NotificationDeliveryUnavailable):
        await NotificationDeliveryPostgresData("postgresql://mail/db").finish_delivery(
            item, outcome="delivered", reason="published"
        )

    assert connection.rolled_back and not connection.committed
    assert connection.closed
    assert json.loads(connection.calls[0][1][0]) == {
        "schema": NOTIFICATION_DELIVERY_PROTOCOL,
        "notification_id": lease["id"],
        "worker_id": str(WORKER_ID),
        "lease_id": lease["lease_id"],
        "outcome": "delivered",
        "reason": "published",
    }


class PolicyConnection(FakeConnection):
    def __init__(self, grants, *, table_access=False, column_access=False):
        super().__init__(
            {
                "schema": NOTIFICATION_DELIVERY_POLICY_PROTOCOL,
                "policy": NOTIFICATION_DELIVERY_PROTOCOL,
            }
        )
        self.grants = grants
        self.table_access = table_access
        self.column_access = column_access
        self.queries = []

    async def fetchval(self, sql, *args):
        self.queries.append(sql)
        if "has_table_privilege" in sql:
            return self.table_access or (
                self.column_access and "has_any_column_privilege" in sql
            )
        return await super().fetchval(sql, *args)

    async def fetch(self, sql):
        self.queries.append(sql)
        return [
            {
                "nspname": schema,
                "proname": name,
                "args": args,
                "prosecdef": prosecdef,
            }
            for schema, name, args, prosecdef in self.grants
            if schema.startswith("aap_") or prosecdef
        ]


@pytest.mark.asyncio
async def test_readiness_requires_exact_global_function_capabilities_and_no_tables(
    monkeypatch,
):
    grants = [
        ("aap_notifications_v1", "identity_delivery_policy", "", False),
        ("aap_notifications_v1", "claim_identity_deliveries", "jsonb", True),
        ("aap_notifications_v1", "finish_identity_delivery", "jsonb", True),
        ("public", "safe_extension_helper", "text", False),
    ]
    connection = PolicyConnection(grants)
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    policy = await NotificationDeliveryPostgresData(
        "postgresql://publisher/db"
    ).delivery_policy()
    assert policy.policy == NOTIFICATION_DELIVERY_PROTOCOL
    assert connection.committed and connection.closed
    capability_sql = next(sql for sql in connection.queries if "pg_proc" in sql)
    assert "oidvectortypes(function.proargtypes)" in capability_sql
    assert "pg_catalog.left(namespace.nspname, 4) = 'aap_'" in capability_sql
    assert "OR function.prosecdef" in capability_sql

    connection = PolicyConnection(
        [*grants, ("aap_identity_v1", "unsafe", "uuid", True)]
    )
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    with pytest.raises(NotificationDeliveryUnavailable):
        await NotificationDeliveryPostgresData(
            "postgresql://publisher/db"
        ).delivery_policy()
    assert connection.rolled_back and connection.closed

    connection = PolicyConnection(grants, column_access=True)
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    with pytest.raises(NotificationDeliveryUnavailable):
        await NotificationDeliveryPostgresData(
            "postgresql://publisher/db"
        ).delivery_policy()
    assert connection.rolled_back and connection.closed

    connection = PolicyConnection([*grants, ("public", "unsafe_definer", "uuid", True)])
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    with pytest.raises(NotificationDeliveryUnavailable):
        await NotificationDeliveryPostgresData(
            "postgresql://publisher/db"
        ).delivery_policy()
    assert connection.rolled_back and connection.closed

    connection = PolicyConnection(grants, table_access=True)
    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg(connection))
    with pytest.raises(NotificationDeliveryUnavailable):
        await NotificationDeliveryPostgresData(
            "postgresql://publisher/db"
        ).delivery_policy()
    assert connection.rolled_back and connection.closed
