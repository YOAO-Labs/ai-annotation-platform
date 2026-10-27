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

from aap_assets_source.connections_contract import (
    CONNECTIONS_SCHEMA,
    ConnectionCursor,
    ConnectionMutation,
    ConnectionFailure,
    CaptureConnectionRequest,
    ConnectionCapture,
    ConnectionRecord,
    ConnectionRead,
    ConnectionsPage,
    CreateConnectionRequest,
    NativeConnectionFacts,
    NativeConnectionRead,
    ReadConnectionRequest,
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
    assert "aap_assets_v1.read_storage_connection" in str(statement)
    assert parameters == {"actor_id": actor_id, "connection_id": connection_id}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_native_capture_uses_frozen_three_argument_signature():
    actor_id, connection_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    expected = ConnectionCapture(
        actor_id=actor_id,
        platform_role="project_admin",
        connection_id=connection_id,
        access="admin",
        connection=_native_record(connection_id, actor_id, "archive", now),
        secret_enc=None,
        source_snapshot="b" * 64,
    )
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = SimpleNamespace(scalar_one=lambda: _wire(expected))

    result = await AssetsConnectionsGateway(db).capture_storage_connection(
        CaptureConnectionRequest(
            actor_id=actor_id, connection_id=connection_id, access="admin"
        )
    )

    assert result.source_snapshot == "b" * 64
    statement, parameters = db.execute.await_args.args
    assert "aap_assets_v1.capture_storage_connection" in str(statement)
    assert parameters == {
        "actor_id": actor_id,
        "connection_id": connection_id,
        "access": "admin",
    }


@pytest.mark.asyncio
async def test_remote_connection_list_collects_complete_scoped_pages(monkeypatch):
    actor_id = uuid.uuid4()
    first_id, second_id = uuid.uuid4(), uuid.uuid4()
    base_time = datetime.now(UTC)
    first = _record(first_id, actor_id, "newer", base_time)
    second = _record(second_id, actor_id, "older", base_time - timedelta(seconds=1))
    cursor = ConnectionCursor(created_at=first.created_at, id=first.id)
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        body = json.loads(request.content)
        if body["after"] is None:
            page = ConnectionsPage(
                actor_id=actor_id,
                after=None,
                next_cursor=cursor,
                snapshot_digest="a" * 64,
                total=2,
                complete=False,
                connections=[first],
            )
        else:
            page = ConnectionsPage(
                actor_id=actor_id,
                after=cursor,
                next_cursor=None,
                snapshot_digest="a" * 64,
                total=2,
                complete=True,
                connections=[second],
            )
        return httpx.Response(200, json=_wire(page))

    _mock_http(monkeypatch, handler)
    db = AsyncMock(spec=AsyncSession)
    result = await AssetsConnectionsGateway(db).list_connections(actor_id)

    assert [item.id for item in result] == [first_id, second_id]
    assert len(requests) == 2
    assert all(request.url.path == "/v1/connections/list" for request in requests)
    assert all(
        request.headers["authorization"] == f"Bearer {'t' * 40}" for request in requests
    )
    assert json.loads(requests[1].content)["after"]["id"] == str(first_id)
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_connection_list_rejects_wrong_cursor_echo(monkeypatch):
    actor_id, connection_id = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    wrong_after = ConnectionCursor(
        created_at=now - timedelta(seconds=1), id=uuid.uuid4()
    )
    page = ConnectionsPage(
        actor_id=actor_id,
        after=wrong_after,
        next_cursor=None,
        snapshot_digest="c" * 64,
        total=1,
        complete=True,
        connections=[_record(connection_id, actor_id, "archive", now)],
    )
    _mock_http(monkeypatch, lambda _request: httpx.Response(200, json=_wire(page)))
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(AssetsConnectionsGatewayError) as failure:
        await AssetsConnectionsGateway(db).list_connections(actor_id)

    assert failure.value.code == "invalid_response"
    assert failure.value.detail == "Assets connection reply escaped request scope"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_connection_list_preserves_results_beyond_one_page_budget(
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
            ConnectionsPage(
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
    remaining = iter(pages)
    _mock_http(
        monkeypatch,
        lambda _request: httpx.Response(200, json=_wire(next(remaining))),
    )
    db = AsyncMock(spec=AsyncSession)

    result = await AssetsConnectionsGateway(db).list_connections(actor_id)

    assert [record.id for record in result] == [record.id for record in records]
    assert sum(len(record.model_dump_json()) for record in result) > maximum
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_connection_scope_failure_does_not_fall_back(monkeypatch):
    actor_id, connection_id = uuid.uuid4(), uuid.uuid4()
    wrong_id = uuid.uuid4()
    now = datetime.now(UTC)
    wrong = ConnectionRead(
        actor_id=actor_id,
        connection_id=wrong_id,
        connection=_record(wrong_id, actor_id, "escaped", now),
    )
    _mock_http(
        monkeypatch,
        lambda _request: httpx.Response(200, json=_wire(wrong)),
    )
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(AssetsConnectionsGatewayError) as failure:
        await AssetsConnectionsGateway(db).read_connection(
            ReadConnectionRequest(actor_id=actor_id, connection_id=connection_id)
        )

    assert failure.value.code == "invalid_response"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_connection_failure_never_falls_back_to_local_sql(monkeypatch):
    _mock_http(monkeypatch, lambda _request: httpx.Response(503))
    db = AsyncMock(spec=AsyncSession)

    with pytest.raises(AssetsConnectionsGatewayError) as failure:
        await AssetsConnectionsGateway(db).read_connection(
            ReadConnectionRequest(actor_id=uuid.uuid4(), connection_id=uuid.uuid4())
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
        await AssetsConnectionsGateway(db).read_connection(
            ReadConnectionRequest(actor_id=uuid.uuid4(), connection_id=uuid.uuid4())
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
        await AssetsConnectionsGateway(db).read_connection(
            ReadConnectionRequest(actor_id=actor_id, connection_id=connection_id)
        )

    assert failure.value.status_code == 404
    assert failure.value.detail == "连接器不存在"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_local_mutation_budget_failure_rolls_back_before_commit(monkeypatch):
    actor_id, operation_id = uuid.uuid4(), uuid.uuid4()
    created_at = datetime.now(UTC)
    connection = _record(operation_id, actor_id, "archive", created_at)
    result = ConnectionMutation(
        operation_id=operation_id,
        actor_id=actor_id,
        action="create",
        connection_id=operation_id,
        connection=connection,
    )

    class Workflow:
        async def invoke(self, operation, request):
            assert operation == "create"
            assert request.operation_id == operation_id
            return result

    monkeypatch.setattr(assets_connections.settings, "assets_source_url", "")
    monkeypatch.setattr(assets_connections, "_MAX_RESPONSE_BYTES", 1)
    db = AsyncMock(spec=AsyncSession)
    gateway = AssetsConnectionsGateway(db)
    gateway._workflow = lambda: Workflow()

    with pytest.raises(AssetsConnectionsGatewayError) as failure:
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

    assert failure.value.code == "invalid_response"
    db.rollback.assert_awaited_once()
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
