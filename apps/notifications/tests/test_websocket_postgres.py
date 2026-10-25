from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import uuid

import pytest

from aap_notifications.websocket_postgres import (
    NotificationSocketPostgresData,
    configure_notification_jsonb_codec,
    open_notification_socket_data,
)


class _Connection:
    def __init__(self, notification=None, *, allowed_pairs=()):
        self.notification = notification
        self.allowed_pairs = set(allowed_pairs)
        self.fetchrow_calls = []
        self.fetch_calls = []
        self.codec = None
        self.transaction_options = None
        self.commits = 0
        self.rollbacks = 0

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self.notification

    async def fetch(self, query, *args):
        if "delivery_job_scopes" in query:
            self.fetch_calls.append(("jobs", args))
            return []
        for function_name in (
            "delivery_member_pairs",
            "delivery_reviewer_pairs",
            "delivery_owner_pairs",
            "delivery_super_pairs",
        ):
            if function_name in query:
                self.fetch_calls.append((function_name, args))
                if function_name == "delivery_member_pairs":
                    return [
                        {"project_id": project_id, "user_id": user_id}
                        for project_id, user_id in self.allowed_pairs
                    ]
                return []
        raise AssertionError(f"unexpected owner query: {query}")

    async def set_type_codec(self, *args, **kwargs):
        self.codec = (args, kwargs)

    def transaction(self, **kwargs):
        self.transaction_options = kwargs
        return _Transaction(self)


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def start(self):
        return None

    async def rollback(self):
        self.connection.rollbacks += 1


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


def _notification(
    *,
    notification_id=None,
    user_id=None,
    target_type="task",
    target_id=None,
    payload=None,
):
    return {
        "id": notification_id or uuid.uuid4(),
        "user_id": user_id or uuid.uuid4(),
        "type": "task.approved",
        "target_type": target_type,
        "target_id": target_id or uuid.uuid4(),
        "payload": payload,
        "read_at": None,
        "created_at": datetime.now(timezone.utc),
    }


@pytest.mark.asyncio
async def test_jsonb_codec_preserves_native_shapes_and_large_integers():
    connection = _Connection()

    await configure_notification_jsonb_codec(connection)

    args, kwargs = connection.codec
    assert args == ("jsonb",)
    assert kwargs["schema"] == "pg_catalog"
    assert kwargs["format"] == "text"
    assert kwargs["encoder"] is json.dumps
    value = [None, False, "雪", 2**80, {"nested": [1, None]}]
    assert kwargs["decoder"](kwargs["encoder"](value)) == value


@pytest.mark.asyncio
async def test_found_notification_is_cached_for_the_second_delivery_read():
    actor_id, notification_id = uuid.uuid4(), uuid.uuid4()
    connection = _Connection(
        _notification(
            notification_id=notification_id, user_id=actor_id, payload=[None, 7]
        )
    )
    data = NotificationSocketPostgresData(connection)

    row = await data.notification_by_id(notification_id)
    assert row is not None
    assert row.payload == [None, 7]
    assert await data.notification_deliverable(notification_id, actor_id)

    assert len(connection.fetchrow_calls) == 1
    assert connection.fetch_calls == []
    assert connection.fetchrow_calls[0][1] == (notification_id,)
    assert not hasattr(connection, "commit")


@pytest.mark.asyncio
async def test_missing_notification_is_not_cached_between_retry_looks():
    connection = _Connection()
    data = NotificationSocketPostgresData(connection)
    notification_id = uuid.uuid4()

    assert await data.notification_by_id(notification_id) is None
    assert await data.notification_by_id(notification_id) is None
    assert len(connection.fetchrow_calls) == 2


@pytest.mark.asyncio
async def test_scope_reads_use_native_jsonb_pairs_in_the_original_order():
    actor_id, project_id, notification_id = (uuid.uuid4() for _ in range(3))
    pair = (project_id, actor_id)
    connection = _Connection(
        _notification(
            notification_id=notification_id,
            user_id=actor_id,
            payload={"project_id": str(project_id)},
        ),
        allowed_pairs=[pair],
    )
    data = NotificationSocketPostgresData(connection)

    assert await data.notification_deliverable(notification_id, actor_id)

    assert [name for name, _args in connection.fetch_calls] == [
        "delivery_member_pairs",
        "delivery_reviewer_pairs",
        "delivery_owner_pairs",
        "delivery_super_pairs",
    ]
    for _name, args in connection.fetch_calls:
        assert args == ([{"project_id": str(project_id), "user_id": str(actor_id)}],)
        assert isinstance(args[0], list)
        assert isinstance(args[0][0]["project_id"], str)
        assert isinstance(args[0][0]["user_id"], str)


@pytest.mark.asyncio
async def test_borrowed_connection_uses_read_committed_read_only_transaction():
    connection = _Connection()
    pool = _Pool(connection)

    async with open_notification_socket_data(pool) as data:
        assert isinstance(data, NotificationSocketPostgresData)

    assert connection.transaction_options == {
        "isolation": "read_committed",
        "readonly": True,
    }
    assert connection.commits == 0
    assert connection.rollbacks == 1
