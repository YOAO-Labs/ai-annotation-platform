from __future__ import annotations

import sys
from types import ModuleType

import pytest

from aap_inference.routing_postgres import RoutingPostgresData
from aap_inference.routing_runtime import RoutingRuntime


class _Transaction:
    def __init__(self) -> None:
        self.started = False
        self.committed = False
        self.rolled_back = False

    async def start(self) -> None:
        self.started = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _Connection:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.tx = _Transaction()
        self.close_error = close_error
        self.closed = False
        self.terminated = False

    def transaction(self, *, readonly: bool = False) -> _Transaction:
        assert readonly is False
        return self.tx

    async def close(self, *, timeout: int) -> None:
        assert timeout == 2
        if self.close_error is not None:
            raise self.close_error
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True


@pytest.mark.asyncio
async def test_runtime_uses_a_fresh_connection_and_commits_each_operation(
    monkeypatch,
):
    connections = []

    async def connect(*_args, **_kwargs):
        connection = _Connection()
        connections.append(connection)
        return connection

    async def bind(_cls, connection):
        return ("bound", connection)

    asyncpg_module = ModuleType("asyncpg")
    asyncpg_module.connect = connect
    monkeypatch.setitem(sys.modules, "asyncpg", asyncpg_module)
    monkeypatch.setattr(RoutingPostgresData, "bind", classmethod(bind))
    runtime = RoutingRuntime("postgresql://routing:test@localhost/aap")

    async with runtime._transaction() as first:
        assert first == ("bound", connections[0])
    async with runtime._transaction() as second:
        assert second == ("bound", connections[1])

    assert len(connections) == 2
    assert connections[0] is not connections[1]
    assert all(connection.tx.committed for connection in connections)
    assert all(connection.closed for connection in connections)


@pytest.mark.asyncio
async def test_body_failure_rolls_back_and_cleanup_does_not_replace_it(monkeypatch):
    connection = _Connection(close_error=RuntimeError("cleanup detail"))

    async def connect(*_args, **_kwargs):
        return connection

    async def bind(_cls, _connection):
        return object()

    asyncpg_module = ModuleType("asyncpg")
    asyncpg_module.connect = connect
    monkeypatch.setitem(sys.modules, "asyncpg", asyncpg_module)
    monkeypatch.setattr(RoutingPostgresData, "bind", classmethod(bind))
    runtime = RoutingRuntime("postgresql://routing:test@localhost/aap")

    with pytest.raises(ValueError, match="operation failed"):
        async with runtime._transaction():
            raise ValueError("operation failed")

    assert connection.tx.rolled_back is True
    assert connection.tx.committed is False
    assert connection.terminated is True


def test_runtime_rejects_invalid_independent_owner_settings():
    with pytest.raises(ValueError):
        RoutingRuntime("postgresql://bad dsn")
    with pytest.raises(ValueError):
        RoutingRuntime(
            "postgresql://routing:test@localhost/aap",
            health_max_age_seconds=0,
        )
    with pytest.raises(ValueError):
        RoutingRuntime(
            "postgresql://routing:test@localhost/aap",
            lease_ttl_ms=0,
        )
