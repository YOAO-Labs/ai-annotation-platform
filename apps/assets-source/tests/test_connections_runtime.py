from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_assets_source.connections_contract import (
    ConnectionFailure,
    ConnectionRecord,
    ConnectionsPage,
    ListConnectionsRequest,
    decode_connection_reply,
)
from aap_assets_source_runtime.config import load_config
from aap_assets_source_runtime import runtime as runtime_module
from aap_assets_source_runtime.runtime import SourceRuntime


def _config(*, max_response_bytes: int = 16_777_216):
    return load_config(
        {
            "ASSETS_SOURCE_DATABASE_URL": "postgresql://assets_source:secret@db.test/aap",
            "ASSETS_SOURCE_TOKEN": "s" * 40,
            "ASSETS_SOURCE_S3_ENDPOINT": "http://minio.internal:9000",
            "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": "https://storage.example.test",
            "ASSETS_SOURCE_S3_ACCESS_KEY": "source-access",
            "ASSETS_SOURCE_S3_SECRET_KEY": "source-secret",
            "ASSETS_SOURCE_DATASETS_BUCKET": "datasets",
            "ASSETS_SOURCE_IMPORT_BUCKET": "imports",
            "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": "media-cache",
            "ASSETS_SOURCE_MAX_RESPONSE_BYTES": str(max_response_bytes),
        }
    )


class _Database:
    def __init__(self) -> None:
        self.events = []
        self.ports = SimpleNamespace(connections=object())

    @asynccontextmanager
    async def transaction(self):
        self.events.append("begin")
        try:
            yield self.ports
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")


class _Workflow:
    outcome = None
    options = None
    native = None

    def __init__(self, *, native, options):
        type(self).native = native
        type(self).options = options

    async def invoke(self, _operation, _request):
        if isinstance(type(self).outcome, Exception):
            raise type(self).outcome
        return type(self).outcome


def _runtime(monkeypatch, *, outcome, max_response_bytes=16_777_216):
    _Workflow.outcome = outcome
    _Workflow.options = None
    _Workflow.native = None
    monkeypatch.setattr(runtime_module, "ConnectionsWorkflow", _Workflow)
    database = _Database()
    return SourceRuntime(
        _config(max_response_bytes=max_response_bytes), database, None
    ), database


def _page(actor_id, *, config=None):
    timestamp = datetime(2026, 10, 19, tzinfo=UTC)
    connection = ConnectionRecord(
        id=uuid4(),
        revision=1,
        name="connection",
        kind="s3",
        config=config or {"endpoint": "https://storage.example.test", "bucket": "data"},
        scope="owner",
        project_id=None,
        secret_set=False,
        created_by=actor_id,
        created_at=timestamp,
        updated_at=timestamp,
    )
    return ConnectionsPage(
        actor_id=actor_id,
        after=None,
        next_cursor=None,
        snapshot_digest="a" * 64,
        total=1,
        complete=True,
        connections=[connection],
    )


@pytest.mark.asyncio
async def test_connection_reply_is_encoded_and_validated_before_commit(
    monkeypatch,
) -> None:
    actor_id = uuid4()
    expected = _page(actor_id)
    runtime, database = _runtime(monkeypatch, outcome=expected)
    request = ListConnectionsRequest(actor_id=actor_id)

    body = await runtime.manage_connection("list", request)

    result = decode_connection_reply(json.loads(body), ConnectionsPage)
    assert result == expected
    assert database.events == ["begin", "commit"]
    assert "secret_enc" not in body.decode()
    assert _Workflow.native is database.ports.connections
    assert _Workflow.options.encryption_key == ""
    assert _Workflow.options.fallback_allowlist == ()
    assert _Workflow.options.known_hosts_file is None


@pytest.mark.asyncio
async def test_connection_business_failure_is_encoded_before_rollback(
    monkeypatch,
) -> None:
    failure = ConnectionFailure("host_denied", "连接失败: TimeoutError")
    runtime, database = _runtime(monkeypatch, outcome=failure)
    request = ListConnectionsRequest(actor_id=uuid4())

    body = await runtime.manage_connection("list", request)

    with pytest.raises(ConnectionFailure) as error:
        decode_connection_reply(json.loads(body), ConnectionsPage)
    assert (error.value.code, error.value.message) == (failure.code, failure.message)
    assert database.events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_connection_scope_or_response_budget_failure_rolls_back(
    monkeypatch,
) -> None:
    actor_id = uuid4()
    wrong_scope = _page(uuid4())
    runtime, database = _runtime(monkeypatch, outcome=wrong_scope)
    request = ListConnectionsRequest(actor_id=actor_id)

    body = await runtime.manage_connection("list", request)

    with pytest.raises(ConnectionFailure) as error:
        decode_connection_reply(json.loads(body), ConnectionsPage)
    assert error.value.code == "invalid_response"
    assert database.events == ["begin", "rollback"]

    long_config = {
        "endpoint": "https://storage.example.test/" + "x" * 512,
        "bucket": "data",
    }
    oversized, oversized_database = _runtime(
        monkeypatch,
        outcome=_page(actor_id, config=long_config),
        max_response_bytes=128,
    )
    response = await oversized.manage_connection("list", request)
    with pytest.raises(ConnectionFailure) as error:
        decode_connection_reply(json.loads(response), ConnectionsPage)
    assert error.value.code == "resource_budget_exceeded"
    assert oversized_database.events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_invalid_public_result_never_commits(monkeypatch) -> None:
    runtime, database = _runtime(monkeypatch, outcome={"secret_enc": "ciphertext"})
    request = ListConnectionsRequest(actor_id=uuid4())

    body = await runtime.manage_connection("list", request)

    with pytest.raises(ConnectionFailure) as error:
        decode_connection_reply(json.loads(body), ConnectionsPage)
    assert error.value.code == "invalid_response"
    assert "ciphertext" not in body.decode()
    assert database.events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_unknown_connection_operation_is_rejected_without_sql(
    monkeypatch,
) -> None:
    runtime, database = _runtime(monkeypatch, outcome=_page(uuid4()))

    with pytest.raises(ConnectionFailure) as error:
        await runtime.manage_connection(
            "native-command", ListConnectionsRequest(actor_id=uuid4())
        )

    assert error.value.code == "invalid_request"
    assert database.events == []
