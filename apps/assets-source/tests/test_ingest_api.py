from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import json
from types import SimpleNamespace
from uuid import uuid4
import zipfile

from fastapi.testclient import TestClient
import pytest

from aap_assets_source.ingest_contract import INGEST_SCHEMA
from aap_assets_source_runtime import api as api_module
from aap_assets_source_runtime.api import BoundedHttpBodyMiddleware, create_app
from aap_assets_source_runtime.config import load_config
from aap_assets_source_runtime.runtime import SourceRuntime


def _config():
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
        }
    )


class _Runtime:
    def __init__(self) -> None:
        self.calls = []
        self.body = None
        self.spool = None

    async def upload_zip(self, command, source):
        self.calls.append(("zip", command))
        self.spool = source
        self.body = source.read()
        return json.dumps(
            {"schema": INGEST_SCHEMA, "ok": False, "error": {"code": "invalid_request"}}
        ).encode()

    async def scan(self, command):
        self.calls.append(("scan", command))
        return json.dumps(
            {"schema": INGEST_SCHEMA, "ok": False, "error": {"code": "invalid_request"}}
        ).encode()

    async def ready(self):
        return True


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer " + "s" * 40}


def _raw_scope(command, *, headers=None):
    query = "&".join(f"{key}={value}" for key, value in command.items()).encode()
    raw_headers = [
        (b"host", b"assets-source.test"),
        (b"content-type", b"application/zip"),
    ]
    if headers:
        raw_headers.extend(
            (name.lower().encode(), value.encode()) for name, value in headers.items()
        )
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/source/upload-zip",
        "raw_path": b"/v1/source/upload-zip",
        "query_string": query,
        "headers": raw_headers,
        "server": ("assets-source.test", 80),
        "client": ("test", 1),
        "root_path": "",
    }


async def _asgi_request(app, scope, receive):
    messages = []

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


def test_raw_upload_authenticates_before_body_read_and_streams_to_spool() -> None:
    runtime = _Runtime()
    app = create_app(config=_config(), runtime=runtime)
    command = {
        name: str(uuid4()) for name in ("operation_id", "actor_id", "dataset_id")
    }
    with TestClient(app) as client:
        denied = client.post(
            "/v1/source/upload-zip",
            params=command,
            headers={"Content-Type": "application/zip"},
            content=b"must not be consumed",
        )
        response = client.post(
            "/v1/source/upload-zip",
            params=command,
            headers={**_headers(), "Content-Type": "application/zip"},
            content=b"streamed zip bytes",
        )
        retry_after_failure = client.post(
            "/v1/source/upload-zip",
            params=command,
            headers={**_headers(), "Content-Type": "application/zip"},
            content=b"another streamed body",
        )

    assert denied.status_code == 401
    assert response.status_code == retry_after_failure.status_code == 200
    assert [kind for kind, _command in runtime.calls] == ["zip", "zip"]
    assert runtime.body == b"another streamed body"
    assert runtime.spool is not None and runtime.spool.closed


def test_scan_validation_uses_ingest_schema_and_old_validation_stays_source_schema() -> (
    None
):
    runtime = _Runtime()
    app = create_app(config=_config(), runtime=runtime)
    with TestClient(app) as client:
        scan = client.post(
            "/v1/source/scan", headers=_headers(), json={"operation_id": str(uuid4())}
        )
        old = client.post(
            "/v1/source/upload-init",
            headers=_headers(),
            json={"actor_id": str(uuid4())},
        )

    assert scan.status_code == old.status_code == 200
    assert scan.json() == {
        "schema": INGEST_SCHEMA,
        "ok": False,
        "error": {"code": "invalid_request"},
    }
    assert old.json()["schema"] == "aap-assets-source/v1"
    assert runtime.calls == []


@pytest.mark.asyncio
async def test_zip_body_counter_stops_at_limit_and_closes_spool(monkeypatch) -> None:
    monkeypatch.setattr(api_module, "ZIP_MAX_BYTES", 4)
    messages = iter(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5", "more_body": False},
        ]
    )
    responses = []
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/source/upload-zip",
        "headers": [],
    }

    async def receive():
        return next(messages)

    async def send(message):
        responses.append(message)

    async def endpoint(_scope, receive, send):
        while (await receive()).get("more_body", False):
            pass
        state = scope["state"]
        status = 413 if state["_source_zip_too_large"] else 200
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"done", "more_body": False})

    middleware = BoundedHttpBodyMiddleware(
        endpoint,
        max_request_bytes=1024,
        max_response_bytes=1024,
    )
    await middleware(scope, receive, send)

    assert (
        next(
            item["status"]
            for item in responses
            if item["type"] == "http.response.start"
        )
        == 413
    )
    assert scope["state"]["_source_zip_spool"].closed
    assert scope["state"]["_source_zip_too_large"] is True


@pytest.mark.asyncio
async def test_busy_zip_slot_returns_without_consuming_or_spooling_body() -> None:
    config = load_config(
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
            "ASSETS_SOURCE_POOL_TIMEOUT_SECONDS": "0.1",
        }
    )
    runtime = _Runtime()
    app = create_app(config=config, runtime=runtime)
    app.state.config = config
    app.state.runtime = runtime
    command = {
        name: str(uuid4()) for name in ("operation_id", "actor_id", "dataset_id")
    }
    scope = _raw_scope(command, headers=_headers())
    consumed = 0

    async def receive():
        nonlocal consumed
        consumed += 1
        return {
            "type": "http.request",
            "body": b"must remain unread",
            "more_body": False,
        }

    await app.state.zip_upload_semaphore.acquire()
    try:
        responses = await _asgi_request(app, scope, receive)
    finally:
        app.state.zip_upload_semaphore.release()

    response_body = next(
        message["body"]
        for message in responses
        if message["type"] == "http.response.body"
    )
    assert (
        next(
            message["status"]
            for message in responses
            if message["type"] == "http.response.start"
        )
        == 200
    )
    assert json.loads(response_body)["error"]["code"] == "service_unavailable"
    assert consumed == 0
    assert runtime.calls == []
    assert scope["state"]["_source_zip_spool"] is None


@pytest.mark.asyncio
async def test_zip_disconnect_releases_admission_and_closes_spool() -> None:
    config = _config()
    runtime = _Runtime()
    app = create_app(config=config, runtime=runtime)
    app.state.config = config
    app.state.runtime = runtime
    command = {
        name: str(uuid4()) for name in ("operation_id", "actor_id", "dataset_id")
    }
    scope = _raw_scope(command, headers=_headers())
    messages = iter(
        [
            {"type": "http.request", "body": b"partial zip", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    responses = await _asgi_request(app, scope, receive)
    assert (
        next(
            message["status"]
            for message in responses
            if message["type"] == "http.response.start"
        )
        == 503
    )
    assert scope["state"]["_source_zip_spool"].closed
    assert runtime.calls == []
    await asyncio.wait_for(app.state.zip_upload_semaphore.acquire(), timeout=0.01)
    app.state.zip_upload_semaphore.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["zip", "scan"])
async def test_disconnect_after_body_cancels_ingest_and_rolls_back(kind) -> None:
    config = _config()
    operation_id, actor_id, dataset_id = uuid4(), uuid4(), uuid4()
    command = {
        "operation_id": str(operation_id),
        "actor_id": str(actor_id),
        "dataset_id": str(dataset_id),
    }

    class _BlockingData:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def begin(self, _command, _descriptor):
            self.started.set()
            await asyncio.Event().wait()

    data = _BlockingData()

    class _TransactionDatabase:
        def __init__(self, ports) -> None:
            self.ports = ports
            self.events = []

        @asynccontextmanager
        async def transaction(self):
            self.events.append("begin")
            try:
                yield self.ports
            except BaseException:
                self.events.append("rollback")
                raise
            else:
                self.events.append("commit")

    database = _TransactionDatabase(
        SimpleNamespace(
            ingest=data,
            uploads=object(),
            legacy_uploads=object(),
            deletions=object(),
            images=object(),
        )
    )
    runtime = SourceRuntime(
        config,
        database,
        SimpleNamespace(client=object(), datasets_bucket="datasets", managed=object()),
    )
    app = create_app(config=config, runtime=runtime)
    app.state.config = config
    app.state.runtime = runtime
    if kind == "zip":
        archive = BytesIO()
        with zipfile.ZipFile(archive, "w"):
            pass
        scope = _raw_scope(command, headers=_headers())
        body = archive.getvalue()
    else:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/source/scan",
            "raw_path": b"/v1/source/scan",
            "query_string": b"",
            "headers": [
                (b"host", b"assets-source.test"),
                (b"content-type", b"application/json"),
                (b"authorization", ("Bearer " + "s" * 40).encode()),
            ],
            "server": ("assets-source.test", 80),
            "client": ("test", 1),
            "root_path": "",
        }
        body = json.dumps(command).encode()
    body_pending = True

    async def receive():
        nonlocal body_pending
        if body_pending:
            body_pending = False
            return {"type": "http.request", "body": body, "more_body": False}
        await data.started.wait()
        return {"type": "http.disconnect"}

    responses = await _asgi_request(app, scope, receive)

    assert data.started.is_set()
    assert database.events == ["begin", "rollback"]
    assert (
        next(
            message["status"]
            for message in responses
            if message["type"] == "http.response.start"
        )
        == 204
    )
    if kind == "zip":
        assert scope["state"]["_source_zip_spool"].closed
        await asyncio.wait_for(app.state.zip_upload_semaphore.acquire(), timeout=0.01)
        app.state.zip_upload_semaphore.release()
