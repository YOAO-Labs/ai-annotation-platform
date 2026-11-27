from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from yoao_assets_source.connections_contract import (
    CONNECTIONS_SCHEMA,
    ConnectionCursor,
    ConnectionMutation,
    ConnectionFailure,
    ConnectionRecord,
    ConnectionProbeReceipt,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    NativeConnectionFacts,
    NativeConnectionRead,
    NativeConnectionsPage,
    ReadConnectionRequest,
    TestConnectionRequest,
)
from app.api.v1 import storage_connections as routes
from app.db.enums import UserRole
from app.schemas.storage_connection import StorageConnectionCreate
from app.services import assets_connections
from app.services.assets_connections import (
    AssetsConnectionsGateway,
    AssetsConnectionsGatewayError,
)


def _record(connection_id: uuid.UUID, actor_id: uuid.UUID, name: str, created_at):
    return ConnectionRecord(
        id=connection_id,
        revision=1,
        name=name,
        kind="s3",
        config={"endpoint": "https://s3.example.test", "bucket": "archive"},
        scope="owner",
        project_id=None,
        secret_set=True,
        created_by=actor_id,
        created_at=created_at,
        updated_at=created_at,
    )


def _native_record(
    connection_id: uuid.UUID, actor_id: uuid.UUID, name: str, created_at
):
    return NativeConnectionFacts(
        id=connection_id,
        revision=1,
        name=name,
        kind="s3",
        config={"endpoint": "https://s3.example.test", "bucket": "archive"},
        scope="owner",
        project_id=None,
        secret_set=True,
        created_by=actor_id,
        created_at=created_at,
        updated_at=created_at,
    )


def _wire(model):
    return {
        "schema": CONNECTIONS_SCHEMA,
        "ok": True,
        "data": model.model_dump(mode="json"),
    }


def _mock_http(monkeypatch, handler):
    monkeypatch.setattr(
        assets_connections.settings, "assets_source_url", "http://source.test"
    )
    monkeypatch.setattr(assets_connections.settings, "assets_source_token", "t" * 40)
    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        assets_connections.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )


@pytest.mark.asyncio
async def test_local_native_read_uses_exact_connection_owner_function():
    actor_id, connection_id = uuid.uuid4(), uuid.uuid4()
    created_at = datetime.now(UTC)
    expected = NativeConnectionRead(
        actor_id=actor_id,
        connection_id=connection_id,
        connection=_native_record(connection_id, actor_id, "archive", created_at),
    )
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: _wire(expected))

    result = await AssetsConnectionsGateway(db).read_storage_connection(
        ReadConnectionRequest(actor_id=actor_id, connection_id=connection_id)
    )

    assert result.connection.id == connection_id
    statement, parameters = db.execute.await_args.args
    assert "yoao_assets_v1.read_storage_connection" in str(statement)
    assert parameters == {"actor_id": actor_id, "connection_id": connection_id}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_connection_list_uses_complete_native_pages_even_with_source_host(
    monkeypatch,
):
    actor_id = uuid.uuid4()
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    base_time = datetime.now(UTC)
    first = _record(first_id, actor_id, "newer", base_time)
    second = _record(second_id, actor_id, "older", base_time - timedelta(seconds=1))
    cursor = ConnectionCursor(created_at=first.created_at, id=first.id)
    pages = (
        NativeConnectionsPage(
            actor_id=actor_id,
            after=None,
            next_cursor=cursor,
            snapshot_digest="a" * 64,
            total=2,
            complete=False,
            connections=[first],
        ),
        NativeConnectionsPage(
            actor_id=actor_id,
            after=cursor,
            next_cursor=None,
            snapshot_digest="a" * 64,
            total=2,
            complete=True,
            connections=[second],
        ),
    )
    _mock_http(
        monkeypatch, lambda _request: pytest.fail("sent a metadata read to Source")
    )
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = [
        SimpleNamespace(scalar_one=lambda page=page: _wire(page)) for page in pages
    ]
    result = await AssetsConnectionsGateway(db).list_connections(actor_id)

    assert [item.id for item in result] == [first_id, second_id]
    first_call, second_call = [call.args for call in db.execute.await_args_list]
    assert all(
        "yoao_assets_v1.list_storage_connections" in str(statement)
        and arguments["actor_id"] == actor_id
        for statement, arguments in (first_call, second_call)
    )
    assert first_call[1]["after_id"] is None
    assert second_call[1]["after_id"] == first_id
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_connection_list_rejects_wrong_cursor_echo():
    actor_id, connection_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    wrong_after = ConnectionCursor(
        created_at=now - timedelta(seconds=1), id=uuid.uuid4()
    )
    page = NativeConnectionsPage(
        actor_id=actor_id,
        after=wrong_after,
        next_cursor=None,
        snapshot_digest="c" * 64,
        total=1,
        complete=True,
        connections=[_record(connection_id, actor_id, "archive", now)],
    )
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: _wire(page))

    with pytest.raises(AssetsConnectionsGatewayError) as failure:
        await AssetsConnectionsGateway(db).list_connections(actor_id)

    assert failure.value.code == "invalid_response"
    assert failure.value.detail == "Assets source returned an invalid response"
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_connection_list_preserves_results_beyond_one_page_budget(
    monkeypatch,
):
    actor_id = uuid.uuid4()
    now = datetime.now(UTC)
    records = [
        _record(uuid.uuid4(), actor_id, str(index), now - timedelta(seconds=index))
        for index in range(3)
    ]
    for record in records:
        record.config["base_prefix"] = "p" * 1000
    pages = []
    for index, record in enumerate(records):
        pages.append(
            NativeConnectionsPage(
                actor_id=actor_id,
                after=None if index == 0 else pages[-1].next_cursor,
                next_cursor=ConnectionCursor(created_at=record.created_at, id=record.id)
                if index < len(records) - 1
                else None,
                snapshot_digest="a" * 64,
                total=len(records),
                complete=index == len(records) - 1,
                connections=[record],
            )
        )
    maximum = max(len(json.dumps(_wire(page)).encode()) for page in pages)
    monkeypatch.setattr(assets_connections, "_MAX_RESPONSE_BYTES", maximum)
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = [
        SimpleNamespace(scalar_one=lambda page=page: _wire(page)) for page in pages
    ]

    result = await AssetsConnectionsGateway(db).list_connections(actor_id)

    assert [record.id for record in result] == [record.id for record in records]
    assert sum(len(record.model_dump_json()) for record in result) > maximum
    assert db.execute.await_count == len(pages)


@pytest.mark.asyncio
async def test_remote_connection_scope_failure_does_not_fall_back(monkeypatch):
    actor_id, connection_id = uuid.uuid4(), uuid.uuid4()
    wrong_id = uuid.uuid4()
    request = TestConnectionRequest(
        operation_id=uuid.uuid4(), actor_id=actor_id, connection_id=connection_id
    )
    wrong = ConnectionProbeReceipt(
        operation_id=request.operation_id,
        actor_id=actor_id,
        connection_id=wrong_id,
        ok=True,
        sample_count=1,
        message="连接成功",
    )

    def response(http_request: httpx.Request):
        assert http_request.url.path == "/v1/connections/test"
        assert http_request.headers["authorization"] == f"Bearer {'t' * 40}"
        assert json.loads(http_request.content) == request.model_dump(
            mode="json", exclude_unset=True
        )
        return httpx.Response(200, json=_wire(wrong))

    _mock_http(monkeypatch, response)
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(AssetsConnectionsGatewayError) as failure:
        await AssetsConnectionsGateway(db).test_connection(request)

    assert failure.value.code == "invalid_response"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_connection_failure_never_falls_back_to_local_sql(monkeypatch):
    _mock_http(monkeypatch, lambda _request: httpx.Response(503))
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(AssetsConnectionsGatewayError) as failure:
        await AssetsConnectionsGateway(db).test_connection(
            TestConnectionRequest(
                operation_id=uuid.uuid4(),
                actor_id=uuid.uuid4(),
                connection_id=uuid.uuid4(),
            )
        )

    assert failure.value.status_code == 503
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_non_finite_json_is_invalid_response_without_local_fallback(
    monkeypatch,
):
    _mock_http(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            content=b'{"schema":NaN}',
            headers={"content-type": "application/json"},
        ),
    )
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(AssetsConnectionsGatewayError) as failure:
        await AssetsConnectionsGateway(db).test_connection(
            TestConnectionRequest(
                operation_id=uuid.uuid4(),
                actor_id=uuid.uuid4(),
                connection_id=uuid.uuid4(),
            )
        )

    assert failure.value.code == "invalid_response"
    assert failure.value.status_code == 502
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_inaccessible_connection_is_concealed_as_missing(monkeypatch):
    actor_id, connection_id = uuid.uuid4(), uuid.uuid4()
    payload = ConnectionFailure("connection_management_forbidden").payload()
    _mock_http(monkeypatch, lambda _request: httpx.Response(200, json=payload))
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(AssetsConnectionsGatewayError) as failure:
        await AssetsConnectionsGateway(db).test_connection(
            TestConnectionRequest(
                operation_id=uuid.uuid4(),
                actor_id=actor_id,
                connection_id=connection_id,
            )
        )

    assert failure.value.status_code == 404
    assert failure.value.detail == "连接器不存在"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_mutation_budget_failure_rolls_back_before_commit(monkeypatch):
    actor_id, operation_id = uuid.uuid4(), uuid.uuid4()
    result = ConnectionMutation(
        operation_id=operation_id,
        actor_id=actor_id,
        action="delete",
        connection_id=operation_id,
        connection=None,
    )

    class Workflow:
        async def invoke(self, operation, request):
            assert operation == "delete"
            assert request.operation_id == operation_id
            return result

    monkeypatch.setattr(assets_connections.settings, "assets_source_url", "")
    monkeypatch.setattr(assets_connections, "_MAX_RESPONSE_BYTES", 1)
    db = AsyncMock(spec=AsyncSession)
    gateway = AssetsConnectionsGateway(db)
    gateway._workflow = lambda: Workflow()

    with pytest.raises(AssetsConnectionsGatewayError) as local_create_failure:
        await gateway.create_connection(
            CreateConnectionRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                name="archive",
                kind="s3",
                config={"endpoint": "https://s3.example.test", "bucket": "archive"},
                secret={"access_key": "AK", "secret_key": "SK"},
            )
        )
    assert local_create_failure.value.code == "service_unavailable"

    with pytest.raises(AssetsConnectionsGatewayError) as failure:
        await gateway.delete_connection(
            DeleteConnectionRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                connection_id=operation_id,
            )
        )

    assert failure.value.code == "invalid_response"
    assert db.rollback.await_count == 1
    db.commit.assert_not_awaited()


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 43123),
            "headers": [(b"host", b"testserver")],
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
        }
    )


@pytest.mark.asyncio
async def test_public_create_preserves_response_and_ignores_legacy_project_id(
    monkeypatch,
):
    actor_id, operation_id, legacy_project_id = (
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    )
    now = datetime.now(UTC)
    response_connection = _record(operation_id, actor_id, "archive", now)
    captured = {}

    class Gateway:
        async def create_connection(self, request):
            captured["request"] = request
            return ConnectionMutation(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                action="create",
                connection_id=request.operation_id,
                connection=response_connection,
            )

    monkeypatch.setattr(routes, "AssetsConnectionsGateway", lambda _db: Gateway())
    response = Response()
    result = await routes.create_connection(
        payload=StorageConnectionCreate(
            name="archive",
            kind="s3",
            config={"endpoint": "https://s3.example.test", "bucket": "archive"},
            secret={"access_key": "AK", "secret_key": "SK"},
            project_id=legacy_project_id,
        ),
        request=_request("POST", "/api/v1/storage-connections"),
        response=response,
        db=object(),
        user=SimpleNamespace(id=actor_id, role=UserRole.PROJECT_ADMIN),
        idempotency_key=operation_id,
    )

    assert result.id == operation_id
    assert result.scope == "owner"
    assert result.project_id is None
    assert captured["request"].operation_id == operation_id
    assert not hasattr(captured["request"], "project_id")
    assert captured["request"].audit_context.path == "/api/v1/storage-connections"
    assert captured["request"].audit_context.ip == "127.0.0.1"
    assert response.headers["Idempotency-Key"] == str(operation_id)


@pytest.mark.asyncio
async def test_public_create_generates_a_fresh_operation_id_when_key_is_omitted(
    monkeypatch,
):
    actor_id = uuid.uuid4()
    now = datetime.now(UTC)
    operation_ids = []

    class Gateway:
        async def create_connection(self, request):
            operation_ids.append(request.operation_id)
            connection = _record(request.operation_id, actor_id, request.name, now)
            return ConnectionMutation(
                operation_id=request.operation_id,
                actor_id=actor_id,
                action="create",
                connection_id=request.operation_id,
                connection=connection,
            )

    monkeypatch.setattr(routes, "AssetsConnectionsGateway", lambda _db: Gateway())
    payload = StorageConnectionCreate(
        name="archive",
        kind="s3",
        config={"endpoint": "https://s3.example.test", "bucket": "archive"},
        secret={"access_key": "AK", "secret_key": "SK"},
    )

    for _ in range(2):
        response = Response()
        await routes.create_connection(
            payload=payload,
            request=_request("POST", "/api/v1/storage-connections"),
            response=response,
            db=object(),
            user=SimpleNamespace(id=actor_id, role=UserRole.PROJECT_ADMIN),
            idempotency_key=None,
        )
        assert response.headers["Idempotency-Key"]

    assert len(set(operation_ids)) == 2


@pytest.mark.asyncio
async def test_public_create_maps_invalid_owner_dto_to_safe_422(monkeypatch):
    actor_id = uuid.uuid4()

    class Gateway:
        async def create_connection(self, _request):
            pytest.fail("invalid contract data must be rejected before owner access")

    monkeypatch.setattr(routes, "AssetsConnectionsGateway", lambda _db: Gateway())
    with pytest.raises(routes.HTTPException) as failure:
        await routes.create_connection(
            payload=StorageConnectionCreate(
                name="archive",
                kind="s3",
                config={
                    "endpoint": "https://s3.example.test",
                    "bucket": "archive",
                    "unsafe": float("nan"),
                },
                secret={"access_key": "AK", "secret_key": "SK"},
            ),
            request=_request("POST", "/api/v1/storage-connections"),
            response=Response(),
            db=object(),
            user=SimpleNamespace(id=actor_id, role=UserRole.PROJECT_ADMIN),
            idempotency_key=None,
        )

    assert failure.value.status_code == 422
    assert failure.value.detail == "连接器请求无效"
