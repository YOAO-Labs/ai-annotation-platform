from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from aap_assets_source.contract import (
    SourceDeletionRequest,
    UploadCompleteRequest,
    UploadInitRequest,
    UploadInitResult,
    UploadOutcome,
)
from aap_assets_source.deletion_contract import DatasetSourceDeletionReceipt
from aap_assets_source.upload_contract import DatasetUploadCompletedResult
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


class _Runtime:
    def __init__(self):
        self.calls = []
        self.response_override = None

    async def init_upload(self, request):
        self.calls.append(("init", request))
        if self.response_override is not None:
            return self.response_override
        return json.dumps(
            {
                "schema": "aap-assets-source/v1",
                "ok": True,
                "data": UploadInitResult(
                    actor_id=request.actor_id,
                    dataset_id=request.dataset_id,
                    item_id=request.item_id,
                    upload_url="https://storage.example.test/signed",
                ).model_dump(mode="json"),
            },
            separators=(",", ":"),
        ).encode()

    async def complete_upload(self, request):
        self.calls.append(("complete", request))
        return json.dumps(
            {
                "schema": "aap-assets-source/v1",
                "ok": True,
                "data": UploadOutcome(
                    operation_id=request.operation_id,
                    actor_id=request.actor_id,
                    dataset_id=request.dataset_id,
                    item_id=request.item_id,
                    file_type="image",
                    result=DatasetUploadCompletedResult(
                        item_id=request.item_id, linked_tasks=0
                    ),
                    duplicate_of=None,
                ).model_dump(mode="json"),
            },
            separators=(",", ":"),
        ).encode()

    async def delete_source(self, request):
        self.calls.append(("delete", request))
        return json.dumps(
            {
                "schema": "aap-assets-source/v1",
                "ok": True,
                "data": DatasetSourceDeletionReceipt(
                    operation_id=request.operation_id,
                    actor_id=request.actor_id,
                    dataset_id=request.dataset_id,
                    item_id=request.item_id,
                    cleanup_bucket="media-cache",
                    audit_id=1,
                    deleted_datasets=1,
                    deleted_items=0,
                    deleted_scenes=0,
                    deleted_associations=0,
                    discarded_uploads=0,
                    queued_storage_deletions=0,
                    cancelled_creation_jobs=0,
                ).model_dump(mode="json"),
            },
            separators=(",", ":"),
        ).encode()

    async def ready(self):
        return True


def _headers(token: str = "s" * 40) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_three_internal_operations_require_service_token_and_forward_typed_bodies() -> (
    None
):
    runtime = _Runtime()
    app = create_app(config=_config(), runtime=runtime)
    actor_id, dataset_id, item_id = uuid4(), uuid4(), uuid4()
    operation_id = uuid4()
    with TestClient(app) as client:
        denied = client.post(
            "/v1/source/upload-init",
            json={
                "actor_id": str(actor_id),
                "dataset_id": str(dataset_id),
                "item_id": str(item_id),
                "file_name": "photo.png",
                "content_type": "image/png",
            },
        )
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == "Bearer"

        init = client.post(
            "/v1/source/upload-init",
            headers=_headers(),
            json={
                "actor_id": str(actor_id),
                "dataset_id": str(dataset_id),
                "item_id": str(item_id),
                "file_name": "photo.png",
                "content_type": "image/png",
            },
        )
        complete = client.post(
            "/v1/source/upload-complete",
            headers=_headers(),
            json={
                "operation_id": str(operation_id),
                "actor_id": str(actor_id),
                "dataset_id": str(dataset_id),
                "item_id": str(item_id),
            },
        )
        deleted = client.post(
            "/v1/source/delete",
            headers=_headers(),
            json={
                "operation_id": str(operation_id),
                "actor_id": str(actor_id),
                "dataset_id": str(dataset_id),
            },
        )

    assert init.status_code == complete.status_code == deleted.status_code == 200
    assert (
        init.json()["schema"] == complete.json()["schema"] == deleted.json()["schema"]
    )
    assert [kind for kind, _ in runtime.calls] == ["init", "complete", "delete"]
    assert isinstance(runtime.calls[0][1], UploadInitRequest)
    assert isinstance(runtime.calls[1][1], UploadCompleteRequest)
    assert isinstance(runtime.calls[2][1], SourceDeletionRequest)


def test_non_ascii_bearer_is_rejected_without_compare_digest_error() -> None:
    runtime = _Runtime()
    app = create_app(config=_config(), runtime=runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/source/delete",
            headers={b"Authorization": b"Bearer caf\xe9"},
            json={
                "operation_id": str(uuid4()),
                "actor_id": str(uuid4()),
                "dataset_id": str(uuid4()),
            },
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert runtime.calls == []


def test_invalid_body_uses_source_failure_envelope_and_never_calls_runtime() -> None:
    runtime = _Runtime()
    app = create_app(config=_config(), runtime=runtime)
    with TestClient(app) as client:
        response = client.post(
            "/v1/source/upload-init",
            headers=_headers(),
            json={"actor_id": str(uuid4())},
        )
    assert response.status_code == 200
    assert response.json() == {
        "schema": "aap-assets-source/v1",
        "ok": False,
        "error": {"code": "invalid_request"},
    }
    assert runtime.calls == []


def test_request_and_response_budgets_are_enforced() -> None:
    runtime = _Runtime()
    app = create_app(
        config=_config(
            ASSETS_SOURCE_MAX_REQUEST_BYTES="512",
            ASSETS_SOURCE_MAX_RESPONSE_BYTES="128",
        ),
        runtime=runtime,
    )
    actor_id, dataset_id, item_id = uuid4(), uuid4(), uuid4()
    with TestClient(app) as client:
        too_large_request = client.post(
            "/v1/source/upload-init",
            headers=_headers(),
            json={
                "actor_id": str(actor_id),
                "dataset_id": str(dataset_id),
                "item_id": str(item_id),
                "file_name": "f" * 600,
                "content_type": "image/png",
            },
        )
        runtime.response_override = b"{" + b" " * 256 + b"}"
        too_large_response = client.post(
            "/v1/source/upload-init",
            headers=_headers(),
            json={
                "actor_id": str(actor_id),
                "dataset_id": str(dataset_id),
                "item_id": str(item_id),
                "file_name": "photo.png",
                "content_type": "image/png",
            },
        )
    assert too_large_request.status_code == 413
    assert too_large_response.status_code == 503
    assert [kind for kind, _ in runtime.calls] == ["init"]
