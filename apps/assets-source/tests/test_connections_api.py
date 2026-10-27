from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from aap_assets_source.connections_contract import (
    CONNECTION_OPERATIONS,
    ConnectionFailure,
    CreateConnectionRequest,
    DeleteConnectionRequest,
    ListConnectionsRequest,
    ReadConnectionRequest,
    TestConnectionRequest,
    UpdateConnectionRequest,
)
from aap_assets_source_runtime.api import create_app
from aap_assets_source_runtime.config import load_config


def _config(**overrides):
    environment = {
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
    return load_config({**environment, **overrides})


def _headers(token: str = "s" * 40) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _Runtime:
    def __init__(self) -> None:
        self.calls = []

    async def manage_connection(self, operation: str, request):
        self.calls.append((operation, request))
        return json.dumps(
            ConnectionFailure("connection_missing").payload(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    async def ready(self) -> bool:
        return True


def _requests():
    actor_id, connection_id, operation_id = uuid4(), uuid4(), uuid4()
    secret_marker = "credential-marker-should-never-return"
    return secret_marker, [
        (
            "list",
            "/v1/connections/list",
            ListConnectionsRequest(actor_id=actor_id),
        ),
        (
            "read",
            "/v1/connections/read",
            ReadConnectionRequest(actor_id=actor_id, connection_id=connection_id),
        ),
        (
            "create",
            "/v1/connections/create",
            CreateConnectionRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                name="test connector",
                kind="s3",
                config={
                    "endpoint": "https://storage.example.test",
                    "bucket": "datasets",
                },
                secret={"access_key": "access", "secret_key": secret_marker},
            ),
        ),
        (
            "update",
            "/v1/connections/update",
            UpdateConnectionRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                connection_id=connection_id,
                expected_revision=1,
                name="updated connector",
            ),
        ),
        (
            "delete",
            "/v1/connections/delete",
            DeleteConnectionRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                connection_id=connection_id,
            ),
        ),
        (
            "test",
            "/v1/connections/test",
            TestConnectionRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                connection_id=connection_id,
            ),
        ),
    ]


def test_six_typed_connection_routes_require_bearer_and_forward_only_owner_operations(
    caplog,
) -> None:
    runtime = _Runtime()
    app = create_app(config=_config(), runtime=runtime)
    secret_marker, cases = _requests()
    with TestClient(app) as client:
        denied = client.post(cases[0][1], json=cases[0][2].model_dump(mode="json"))
        responses = [
            client.post(
                path,
                headers=_headers(),
                json=request.model_dump(mode="json"),
            )
            for _operation, path, request in cases
        ]

    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == "Bearer"
    assert all(response.status_code == 200 for response in responses)
    assert all(
        response.json()["schema"] == "aap-assets-connections/v1"
        for response in responses
    )
    assert [operation for operation, _request in runtime.calls] == [
        operation for operation, _path, _request in cases
    ]
    assert [type(request) for _operation, request in runtime.calls] == [
        CONNECTION_OPERATIONS[operation][0] for operation, _path, _request in cases
    ]
    assert [type(request) for _operation, request in runtime.calls] == [
        type(expected) for _operation, _path, expected in cases
    ]
    assert secret_marker not in "\n".join(response.text for response in responses)
    assert secret_marker not in caplog.text
    assert not any("secret_enc" in response.text for response in responses)


@pytest.mark.parametrize(
    "extra",
    [
        {"fallback_allowlist": ["fallback-secret-marker"]},
        {"encryption_key": "credential-marker"},
        {"secret_enc": "ciphertext-marker"},
        {"permission": "super_admin"},
    ],
)
def test_connection_routes_reject_extra_security_fields_and_hide_validation_input(
    extra, caplog
) -> None:
    runtime = _Runtime()
    app = create_app(config=_config(), runtime=runtime)
    marker = next(iter(extra.values()))
    if isinstance(marker, list):
        marker = marker[0]
    with TestClient(app) as client:
        response = client.post(
            "/v1/connections/list",
            headers=_headers(),
            json={
                "actor_id": str(uuid4()),
                **extra,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema": "aap-assets-connections/v1",
        "ok": False,
        "error": {"code": "invalid_request"},
    }
    assert runtime.calls == []
    assert marker not in response.text
    assert marker not in caplog.text


def test_connection_body_budget_and_paths_are_bounded() -> None:
    runtime = _Runtime()
    app = create_app(
        config=_config(ASSETS_SOURCE_MAX_REQUEST_BYTES="256"), runtime=runtime
    )
    with TestClient(app) as client:
        too_large = client.post(
            "/v1/connections/create",
            headers=_headers(),
            json={
                "operation_id": str(uuid4()),
                "actor_id": str(uuid4()),
                "name": "x" * 512,
                "kind": "s3",
                "config": {"endpoint": "https://storage.example.test", "bucket": "b"},
                "secret": {"access_key": "a", "secret_key": "b"},
            },
        )
        generic_native = client.post(
            "/v1/connections/execute",
            headers=_headers(),
            json={"operation": "delete_storage_connection", "arguments": []},
        )

    assert too_large.status_code == 413
    assert generic_native.status_code == 404
    assert runtime.calls == []
    registered_paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/v1/connections/")
    }
    assert registered_paths == {
        "/v1/connections/list",
        "/v1/connections/read",
        "/v1/connections/create",
        "/v1/connections/update",
        "/v1/connections/delete",
        "/v1/connections/test",
    }
