from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

import aap_assets_source_runtime.config as config_module
import aap_assets_source_runtime.runtime as runtime_module
from aap_assets_source.guide_object_contract import (
    GUIDE_OBJECT_SCHEMA,
    GuideObjectFailure,
    decode_guide_object_reply,
    decode_guide_object_request,
    guide_object_success_reply,
)
from aap_assets_source_runtime.api import create_app
from aap_assets_source_runtime.config import (
    ConfigurationError,
    GuideOnlyConfig,
    SourceConfig,
    load_config,
)
from aap_assets_source_runtime.runtime import SourceRuntime


_TOKEN = "g" * 40
_GUIDE_PATH = "/v1/guide-objects/execute"
_PROJECT_ID = UUID("c0f0a802-c56b-4b49-9437-c38fc2d84e5e")
_ASSET_ID = UUID("ba2efde0-3bf0-4cc1-b3c1-5c86ca118d21")
_KEY = f"projects/{_PROJECT_ID}/guide/{_ASSET_ID}-guide.png"


def _guide_environment(**overrides) -> dict[str, str]:
    return {
        "ASSETS_SOURCE_MODE": "guide-only",
        "ASSETS_SOURCE_TOKEN": _TOKEN,
        "ASSETS_SOURCE_GUIDE_S3_ENDPOINT": "http://minio.internal:9000",
        "ASSETS_SOURCE_GUIDE_S3_ACCESS_KEY": "guide-access",
        "ASSETS_SOURCE_GUIDE_S3_SECRET_KEY": "guide-secret",
        "ASSETS_SOURCE_GUIDE_S3_BUCKET": "project-guides",
        **overrides,
    }


def _source_environment(**overrides) -> dict[str, str]:
    return {
        "ASSETS_SOURCE_DATABASE_URL": "postgresql://assets_source:secret@db.test/aap",
        "ASSETS_SOURCE_TOKEN": _TOKEN,
        "ASSETS_SOURCE_S3_ENDPOINT": "http://minio.internal:9000",
        "ASSETS_SOURCE_S3_PUBLIC_ENDPOINT": "https://storage.example.test",
        "ASSETS_SOURCE_S3_ACCESS_KEY": "source-access",
        "ASSETS_SOURCE_S3_SECRET_KEY": "source-secret",
        "ASSETS_SOURCE_DATASETS_BUCKET": "datasets",
        "ASSETS_SOURCE_IMPORT_BUCKET": "imports",
        "ASSETS_SOURCE_MEDIA_CACHE_BUCKET": "media-cache",
        **overrides,
    }


def _request(operation: str = "head", *, request_id: UUID | None = None) -> dict:
    content_type = "image/png" if operation == "presign_put" else None
    return {
        "schema": GUIDE_OBJECT_SCHEMA,
        "request_id": str(uuid4() if request_id is None else request_id),
        "operation": operation,
        "project_id": str(_PROJECT_ID),
        "key": _KEY,
        "content_type": content_type,
    }


def _result(operation: str, *, url: str = "https://objects.example.test/signed"):
    if operation == "presign_put":
        return {"url": url, "expires_in": 900}
    if operation == "head":
        return {"content_length": 81}
    if operation == "delete":
        return {"deleted": True}
    if operation == "presign_get":
        return {"url": url, "expires_in": 3600}
    raise AssertionError(operation)


def _headers(token: str = _TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _GuideRuntime:
    def __init__(self):
        self.calls = []
        self.error = None
        self.url = "https://objects.example.test/signed"
        self.response_override = None
        self.ready_calls = 0

    async def ready(self):
        self.ready_calls += 1
        return True

    async def guide_object(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        if self.response_override is not None:
            return self.response_override
        return (
            guide_object_success_reply(
                request, _result(request.operation, url=self.url)
            )
            .model_dump_json(by_alias=True)
            .encode("utf-8")
        )


class _FakeDatabase:
    def __init__(self):
        self.close_count = 0

    async def ready(self):
        return True

    async def close(self):
        self.close_count += 1


class _GuideStorage:
    def __init__(self):
        self.calls = []
        self.close_count = 0
        self.started = None
        self.release = None
        self.error = None

    async def generate_upload_url(self, key, *, content_type):
        self.calls.append(("presign_put", key, content_type))
        await self._wait_if_blocked()
        self._raise_if_needed()
        return "https://objects.example.test/put"

    async def verify_upload(self, key):
        self.calls.append(("head", key))
        await self._wait_if_blocked()
        self._raise_if_needed()
        return 81

    async def delete_object(self, key):
        self.calls.append(("delete", key))
        await self._wait_if_blocked()
        self._raise_if_needed()

    async def generate_download_url(self, key):
        self.calls.append(("presign_get", key))
        await self._wait_if_blocked()
        self._raise_if_needed()
        return "https://objects.example.test/get"

    async def _wait_if_blocked(self):
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()

    def _raise_if_needed(self):
        if self.error is not None:
            raise self.error

    async def close(self):
        self.close_count += 1


def test_source_default_and_source_and_guide_configuration_modes() -> None:
    source = load_config(
        _source_environment(
            ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT="not-read-in-source-mode",
            ASSETS_SOURCE_GUIDE_S3_ENDPOINT="invalid-unused-guide-endpoint",
        )
    )
    assert isinstance(source, SourceConfig)
    assert source.guide is None
    assert source.guide_max_in_flight == 4

    combined = load_config(
        _source_environment(
            ASSETS_SOURCE_MODE="source-and-guide",
            ASSETS_SOURCE_GUIDE_S3_ENDPOINT="http://guide-minio.internal:9000",
            ASSETS_SOURCE_GUIDE_S3_ACCESS_KEY="guide-access",
            ASSETS_SOURCE_GUIDE_S3_SECRET_KEY="guide-secret",
            ASSETS_SOURCE_GUIDE_S3_BUCKET="project-guides",
            ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT="7",
        )
    )
    assert isinstance(combined, SourceConfig)
    assert combined.guide is not None
    assert combined.guide.bucket == "project-guides"
    assert combined.guide_max_in_flight == 7


def test_guide_only_configuration_does_not_read_source_or_image_settings(monkeypatch):
    def forbidden_image_policy(_environment):
        raise AssertionError("guide-only mode read image policy settings")

    monkeypatch.setattr(
        config_module, "image_policy_from_environment", forbidden_image_policy
    )
    guide = load_config(
        _guide_environment(
            ASSETS_SOURCE_DATABASE_URL="not a postgres URL",
            ASSETS_SOURCE_S3_ENDPOINT="not an origin",
            ASSETS_SOURCE_S3_ACCESS_KEY="source-access",
            ASSETS_SOURCE_S3_SECRET_KEY="source-secret",
            ASSETS_SOURCE_DATASETS_BUCKET="invalid source bucket",
            IMAGE_PYRAMID_AUTO_GENERATE="not-a-boolean",
            ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT="3",
        )
    )
    assert isinstance(guide, GuideOnlyConfig)
    assert guide.service_token == _TOKEN
    assert guide.host == "0.0.0.0" and guide.port == 8767
    assert guide.max_request_bytes == 1_048_576
    assert guide.max_response_bytes == 16_777_216
    assert guide.guide.bucket == "project-guides"
    assert guide.guide_max_in_flight == 3
    assert not hasattr(guide, "database_url")
    assert not hasattr(guide, "s3_access_key")
    assert _TOKEN not in repr(guide)
    assert "guide-secret" not in repr(guide)


@pytest.mark.parametrize("mode", ["", "source-only", "Guide-Only", "source_and_guide"])
def test_unknown_runtime_mode_fails_closed(mode: str) -> None:
    with pytest.raises(ConfigurationError, match="ASSETS_SOURCE_MODE"):
        load_config(_guide_environment(ASSETS_SOURCE_MODE=mode))


@pytest.mark.parametrize("limit", ["0", "33", "NaN", "not-an-integer"])
def test_guide_capacity_is_bounded_when_guide_is_enabled(limit: str) -> None:
    with pytest.raises(ConfigurationError, match="ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT"):
        load_config(_guide_environment(ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT=limit))


def test_guide_only_asgi_authenticates_and_does_not_register_source_routes() -> None:
    runtime = _GuideRuntime()
    app = create_app(config=load_config(_guide_environment()), runtime=runtime)
    with TestClient(app) as client:
        denied = client.post(_GUIDE_PATH, json=_request())
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == "Bearer"

        replies = []
        for operation in ("presign_put", "head", "delete", "presign_get"):
            payload = _request(operation)
            response = client.post(
                _GUIDE_PATH,
                headers=_headers(),
                json=payload,
            )
            assert response.status_code == 200, response.text
            request = decode_guide_object_request(payload)
            replies.append(decode_guide_object_reply(request, response.content))

        source_route = client.post(
            "/v1/source/upload-init", headers=_headers(), json={}
        )
        connection_route = client.post(
            "/v1/connections/list", headers=_headers(), json={}
        )
        ready = client.get("/ready")

    assert [reply.operation for reply in replies] == [
        "presign_put",
        "head",
        "delete",
        "presign_get",
    ]
    assert [request.operation for request in runtime.calls] == [
        "presign_put",
        "head",
        "delete",
        "presign_get",
    ]
    assert source_route.status_code == connection_route.status_code == 404
    assert ready.status_code == 200 and runtime.ready_calls == 1


def test_source_default_does_not_register_guide_route() -> None:
    runtime = _GuideRuntime()
    app = create_app(config=load_config(_source_environment()), runtime=runtime)
    with TestClient(app) as client:
        response = client.post(
            _GUIDE_PATH,
            headers=_headers(),
            json=_request(),
        )
    assert response.status_code == 404
    assert runtime.calls == []


def test_source_and_guide_registers_both_private_route_groups() -> None:
    config = load_config(
        _source_environment(
            ASSETS_SOURCE_MODE="source-and-guide",
            ASSETS_SOURCE_GUIDE_S3_ENDPOINT="http://guide-minio.internal:9000",
            ASSETS_SOURCE_GUIDE_S3_ACCESS_KEY="guide-access",
            ASSETS_SOURCE_GUIDE_S3_SECRET_KEY="guide-secret",
            ASSETS_SOURCE_GUIDE_S3_BUCKET="project-guides",
        )
    )
    app = create_app(config=config, runtime=_GuideRuntime())
    paths = {route.path for route in app.routes}
    assert _GUIDE_PATH in paths
    assert "/v1/source/upload-init" in paths
    assert "/v1/connections/list" in paths


def test_malformed_guide_body_is_rejected_without_echo_or_runtime_call() -> None:
    runtime = _GuideRuntime()
    app = create_app(config=load_config(_guide_environment()), runtime=runtime)
    with TestClient(app) as client:
        response = client.post(
            _GUIDE_PATH,
            headers=_headers(),
            content=b'{"secret":"do-not-echo"',
        )
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid guide object request"}
    assert "do-not-echo" not in response.text
    assert runtime.calls == []


def test_guide_request_and_response_body_budgets_are_enforced() -> None:
    runtime = _GuideRuntime()
    app = create_app(
        config=load_config(
            _guide_environment(
                ASSETS_SOURCE_MAX_REQUEST_BYTES="512",
                ASSETS_SOURCE_MAX_RESPONSE_BYTES="128",
            )
        ),
        runtime=runtime,
    )
    with TestClient(app) as client:
        too_large_request = client.post(
            _GUIDE_PATH,
            headers=_headers(),
            content=b"x" * 513,
        )
        runtime.url = "https://objects.example.test/" + "x" * 512
        too_large_response = client.post(
            _GUIDE_PATH,
            headers=_headers(),
            json=_request("presign_put"),
        )
    assert too_large_request.status_code == 413
    assert too_large_response.status_code == 503
    assert [request.operation for request in runtime.calls] == ["presign_put"]


def test_unexpected_guide_storage_fault_returns_safe_http_500() -> None:
    runtime = _GuideRuntime()
    runtime.error = RuntimeError("secret MinIO bucket and credentials")
    app = create_app(config=load_config(_guide_environment()), runtime=runtime)
    with TestClient(app) as client:
        response = client.post(
            _GUIDE_PATH,
            headers=_headers(),
            json=_request(),
        )
    assert response.status_code == 500
    assert response.json() == {"detail": "Assets guide storage failed"}
    assert "secret MinIO" not in response.text


def test_expected_guide_failure_keeps_only_the_scope_bound_code() -> None:
    runtime = _GuideRuntime()
    runtime.error = GuideObjectFailure("storage_error")
    app = create_app(config=load_config(_guide_environment()), runtime=runtime)
    payload = _request("delete")
    request = decode_guide_object_request(payload)
    with TestClient(app) as client:
        response = client.post(_GUIDE_PATH, headers=_headers(), json=payload)
    with pytest.raises(GuideObjectFailure) as failure:
        decode_guide_object_reply(request, response.content)
    assert response.status_code == 200
    assert failure.value.code == "storage_error"


def test_runtime_reply_that_escapes_request_scope_returns_safe_http_500() -> None:
    runtime = _GuideRuntime()
    other_request = decode_guide_object_request(_request("head"))
    runtime.response_override = (
        guide_object_success_reply(other_request, {"content_length": 10})
        .model_dump_json(by_alias=True)
        .encode("utf-8")
    )
    app = create_app(config=load_config(_guide_environment()), runtime=runtime)
    payload = _request("head")
    with TestClient(app) as client:
        response = client.post(_GUIDE_PATH, headers=_headers(), json=payload)
    assert response.status_code == 500
    assert response.json() == {"detail": "Assets guide storage failed"}


@pytest.mark.asyncio
async def test_guide_runtime_dispatches_four_operations_and_binds_replies() -> None:
    config = load_config(_guide_environment())
    storage = _GuideStorage()
    runtime = SourceRuntime(config, None, None, storage)
    try:
        replies = []
        for operation in ("presign_put", "head", "delete", "presign_get"):
            request = decode_guide_object_request(_request(operation))
            body = await runtime.guide_object(request)
            replies.append(decode_guide_object_reply(request, body))
    finally:
        await runtime.close()

    assert [reply.operation for reply in replies] == [
        "presign_put",
        "head",
        "delete",
        "presign_get",
    ]
    assert [call[0] for call in storage.calls] == [
        "presign_put",
        "head",
        "delete",
        "presign_get",
    ]
    assert storage.close_count == 1


@pytest.mark.asyncio
async def test_guide_storage_failure_is_a_bound_code_but_raw_faults_escape() -> None:
    request = decode_guide_object_request(_request("head"))
    storage = _GuideStorage()
    storage.error = GuideObjectFailure("storage_error")
    runtime = SourceRuntime(load_config(_guide_environment()), None, None, storage)
    failure_body = await runtime.guide_object(request)
    with pytest.raises(GuideObjectFailure) as failure:
        decode_guide_object_reply(request, failure_body)
    assert failure.value.code == "storage_error"

    storage.error = ValueError("unexpected local storage failure")
    with pytest.raises(ValueError, match="unexpected local storage failure"):
        await runtime.guide_object(request)
    await runtime.close()


@pytest.mark.asyncio
async def test_guide_runtime_rejects_over_capacity_without_queueing() -> None:
    config = load_config(_guide_environment(ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT="1"))
    storage = _GuideStorage()
    storage.started = asyncio.Event()
    storage.release = asyncio.Event()
    runtime = SourceRuntime(config, None, None, storage)
    first_request = decode_guide_object_request(_request("head"))
    second_request = decode_guide_object_request(_request("delete"))

    first = asyncio.create_task(runtime.guide_object(first_request))
    try:
        await asyncio.wait_for(storage.started.wait(), timeout=1)
        second_body = await asyncio.wait_for(
            runtime.guide_object(second_request), timeout=0.1
        )
        with pytest.raises(GuideObjectFailure) as busy:
            decode_guide_object_reply(second_request, second_body)
        assert busy.value.code == "busy"
        assert [call[0] for call in storage.calls] == ["head"]

        storage.release.set()
        first_body = await asyncio.wait_for(first, timeout=1)
        assert decode_guide_object_reply(first_request, first_body).operation == "head"
    finally:
        storage.release.set()
        await asyncio.gather(first, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_guide_only_start_does_not_open_source_database_or_storage(monkeypatch):
    config = load_config(_guide_environment())
    storage_module = __import__(
        "aap_assets_source_runtime.guide_storage", fromlist=["GuideStorage"]
    )
    guide_storage = _GuideStorage()

    class _GuideStorageFactory:
        def __new__(cls, storage_config):
            assert storage_config == config.guide
            return guide_storage

    class _NeverDatabase:
        @classmethod
        async def connect(cls, _config):
            raise AssertionError("guide-only mode opened PostgreSQL")

    class _NeverSourceStorage:
        def __init__(self, _config):
            raise AssertionError("guide-only mode created the Source S3 client")

    monkeypatch.setattr(storage_module, "GuideStorage", _GuideStorageFactory)
    monkeypatch.setattr(runtime_module, "SourceDatabase", _NeverDatabase)
    monkeypatch.setattr(runtime_module, "SourceStorage", _NeverSourceStorage)

    runtime = await SourceRuntime.start(config)
    try:
        assert runtime._database is None
        assert runtime._storage is None
        assert runtime._guide_storage is guide_storage
        assert await runtime.ready() is True
    finally:
        await runtime.close()
    assert guide_storage.close_count == 1


@pytest.mark.asyncio
async def test_source_and_guide_start_retains_both_resource_owners(monkeypatch):
    config = load_config(
        _source_environment(
            ASSETS_SOURCE_MODE="source-and-guide",
            ASSETS_SOURCE_GUIDE_S3_ENDPOINT="http://guide-minio.internal:9000",
            ASSETS_SOURCE_GUIDE_S3_ACCESS_KEY="guide-access",
            ASSETS_SOURCE_GUIDE_S3_SECRET_KEY="guide-secret",
            ASSETS_SOURCE_GUIDE_S3_BUCKET="project-guides",
        )
    )
    database = _FakeDatabase()
    source_storage = object()
    guide_storage = _GuideStorage()

    class _DatabaseFactory:
        @classmethod
        async def connect(cls, actual_config):
            assert actual_config is config
            return database

    class _SourceStorageFactory:
        def __new__(cls, actual_config):
            assert actual_config is config
            return source_storage

    class _GuideStorageFactory:
        def __new__(cls, actual_config):
            assert actual_config == config.guide
            return guide_storage

    storage_module = __import__(
        "aap_assets_source_runtime.guide_storage", fromlist=["GuideStorage"]
    )
    monkeypatch.setattr(runtime_module, "SourceDatabase", _DatabaseFactory)
    monkeypatch.setattr(runtime_module, "SourceStorage", _SourceStorageFactory)
    monkeypatch.setattr(storage_module, "GuideStorage", _GuideStorageFactory)

    runtime = await SourceRuntime.start(config)
    assert runtime._database is database
    assert runtime._storage is source_storage
    assert runtime._guide_storage is guide_storage
    assert await runtime.ready() is True
    await runtime.close()
    assert database.close_count == 1
    assert guide_storage.close_count == 1
