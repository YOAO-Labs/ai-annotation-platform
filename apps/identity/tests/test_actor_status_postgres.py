from __future__ import annotations

import asyncio
import sys
from types import ModuleType
import uuid

import pytest

from aap_identity.contract import IdentityUnavailable
from aap_identity.actor_status_postgres import ActorStatusPostgresData


class _PostgresError(Exception):
    pass


class _InterfaceError(Exception):
    pass


class _Transaction:
    def __init__(self, readonly):
        self.readonly = readonly

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Connection:
    def __init__(self, row=None, *, error=None, entered=None, close_event=None):
        self.row = row
        self.error = error
        self.entered = entered
        self.close_event = close_event
        self.calls = []
        self.closed = False
        self.readonly = []

    def transaction(self, *, readonly=False):
        self.readonly.append(readonly)
        return _Transaction(readonly)

    async def fetchrow(self, query, actor_id):
        self.calls.append((query, actor_id))
        if self.entered is not None:
            self.entered.set()
            await asyncio.Future()
        if self.error is not None:
            raise self.error
        return self.row

    async def close(self, *, timeout):
        assert timeout == 2
        self.closed = True
        if self.close_event is not None:
            self.close_event.set()


def _asyncpg_module(connect):
    module = ModuleType("asyncpg")
    module.connect = connect
    module.PostgresError = _PostgresError
    module.InterfaceError = _InterfaceError
    return module


@pytest.mark.asyncio
async def test_status_adapter_uses_only_the_existing_fact_function_and_fields(
    monkeypatch,
):
    actor_id = uuid.uuid4()
    connection = _Connection({"actor_id": actor_id, "is_active": False})
    connect_calls = []

    async def connect(dsn, **kwargs):
        connect_calls.append((dsn, kwargs))
        return connection

    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg_module(connect))
    data = ActorStatusPostgresData(
        "postgresql://identity-status.invalid/db",
        connect_timeout=1.5,
        command_timeout=2.5,
    )

    result = await data.read_actor_status(actor_id)

    assert result == {"actor_id": actor_id, "is_active": False}
    assert connect_calls == [
        (
            "postgresql://identity-status.invalid/db",
            {
                "timeout": 1.5,
                "command_timeout": 2.5,
                "server_settings": {"application_name": "aap-identity-actor-status"},
            },
        )
    ]
    query, bound_actor_id = connection.calls[0]
    assert query == (
        "SELECT actor_id, is_active FROM aap_identity_v1.read_actor_facts($1::uuid)"
    )
    assert bound_actor_id == actor_id
    assert "platform_role" not in query and connection.readonly == [True]
    assert connection.closed


@pytest.mark.asyncio
async def test_status_adapter_preserves_absence_and_sanitizes_database_failures(
    monkeypatch,
):
    missing = _Connection(None)

    async def connect_missing(*_args, **_kwargs):
        return missing

    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg_module(connect_missing))
    assert (
        await ActorStatusPostgresData("postgresql://status/db").read_actor_status(
            uuid.uuid4()
        )
        is None
    )

    secret_dsn = "postgresql://status:never-show-this@invalid/db"

    async def fail_connect(*_args, **_kwargs):
        raise _PostgresError(secret_dsn)

    monkeypatch.setitem(sys.modules, "asyncpg", _asyncpg_module(fail_connect))
    with pytest.raises(IdentityUnavailable) as error:
        await ActorStatusPostgresData(secret_dsn).read_actor_status(uuid.uuid4())
    assert secret_dsn not in str(error.value)


@pytest.mark.asyncio
async def test_status_adapter_closes_connection_when_cancelled(monkeypatch):
    entered = asyncio.Event()
    closed = asyncio.Event()
    connection = _Connection(entered=entered, close_event=closed)

    async def connect(*_args, **_kwargs):
        return connection

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        _asyncpg_module(connect),
    )
    task = asyncio.create_task(
        ActorStatusPostgresData("postgresql://status/db").read_actor_status(
            uuid.uuid4()
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set() and connection.closed
