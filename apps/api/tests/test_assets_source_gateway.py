import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from starlette.responses import Response

from aap_assets_source.contract import (
    SourceDeletionRequest,
    UploadInitRequest,
    UploadInitResult,
    UploadOutcome,
)
from aap_assets_source.deletion_contract import DatasetSourceDeletionReceipt
from aap_assets_source.legacy_upload_contract import (
    LegacyUploadCompletion,
    LegacyUploadLockRequest,
)
from aap_assets_source.upload_contract import DatasetUploadCompletedResult
from aap_assets_source_runtime.workflow import UploadWorkflow
from app.api.v1 import datasets as dataset_routes
from app.config import settings
from app.services.assets_source import (
    AssetsSourceGateway,
    AssetsSourceGatewayError,
    _LegacyUploadData,
)

pytestmark = pytest.mark.asyncio

ACTOR = UUID("10000000-0000-4000-8000-000000000001")
DATASET = UUID("20000000-0000-4000-8000-000000000002")
ITEM = UUID("30000000-0000-4000-8000-000000000003")
OPERATION = UUID("40000000-0000-4000-8000-000000000004")
OTHER_ITEM = UUID("50000000-0000-4000-8000-000000000005")
TOKEN = "source-test-token-" + ("x" * 32)


def _upload_init_request() -> UploadInitRequest:
    return UploadInitRequest(
        item_id=ITEM,
        actor_id=ACTOR,
        dataset_id=DATASET,
        file_name="image.jpg",
        content_type="image/jpeg",
    )


def _legacy_state(**updates) -> dict:
    payload = {
        "operation_id": str(OPERATION),
        "actor_id": str(ACTOR),
        "dataset_id": str(DATASET),
        "item_id": str(ITEM),
        "file_path": "legacy/image.jpg",
        "file_type": "image",
        "width": None,
        "height": None,
        "state": "pending",
        "result": None,
        "duplicate_of": None,
    }
    payload.update(updates)
    return payload


def _deletion_receipt(request: SourceDeletionRequest) -> DatasetSourceDeletionReceipt:
    item = request.item_id is not None
    return DatasetSourceDeletionReceipt(
        operation_id=request.operation_id,
        actor_id=request.actor_id,
        dataset_id=request.dataset_id,
        item_id=request.item_id,
        cleanup_bucket="source-cache",
        audit_id=1,
        deleted_datasets=int(not item),
        deleted_items=int(item),
        deleted_scenes=0,
        deleted_associations=0,
        discarded_uploads=0,
        queued_storage_deletions=0,
        cancelled_creation_jobs=0,
    )


async def test_remote_init_sends_scoped_contract_and_returns_typed_result(monkeypatch):
    monkeypatch.setattr(settings, "assets_source_url", "http://source.internal:8767")
    monkeypatch.setattr(settings, "assets_source_token", TOKEN)
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "schema": "aap-assets-source/v1",
                "ok": True,
                "data": {
                    "actor_id": str(ACTOR),
                    "dataset_id": str(DATASET),
                    "item_id": str(ITEM),
                    "upload_url": "/minio/import/item?signature=abc",
                    "expires_in": 900,
                },
            },
        )

    db = AsyncMock()
    gateway = AssetsSourceGateway(db, transport=httpx.MockTransport(handler))
    result = await gateway.init_upload(_upload_init_request())

    assert result.upload_url == "/minio/import/item?signature=abc"
    assert gateway.is_remote
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/source/upload-init"
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(seen[0].content) == _upload_init_request().model_dump(mode="json")
    db.scalar.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "payload", "expected_status"),
    [
        (503, {"detail": "secret SQL text"}, 503),
        (
            200,
            {
                "schema": "aap-assets-source/v2",
                "ok": True,
                "data": {},
            },
            502,
        ),
        (
            200,
            {
                "schema": "aap-assets-source/v1",
                "ok": True,
                "data": {
                    "actor_id": str(OTHER_ITEM),
                    "dataset_id": str(DATASET),
                    "item_id": str(ITEM),
                    "upload_url": "https://storage.example/put",
                    "expires_in": 900,
                },
            },
            502,
        ),
    ],
)
async def test_remote_failure_never_falls_back_or_leaks_peer_details(
    monkeypatch, status: int, payload: dict, expected_status: int
):
    monkeypatch.setattr(settings, "assets_source_url", "https://source.internal")
    monkeypatch.setattr(settings, "assets_source_token", TOKEN)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    db = AsyncMock()
    gateway = AssetsSourceGateway(db, transport=httpx.MockTransport(handler))
    with pytest.raises(AssetsSourceGatewayError) as raised:
        await gateway.init_upload(_upload_init_request())

    assert raised.value.status_code == expected_status
    assert "secret SQL text" not in str(raised.value.detail)
    db.scalar.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_invalid_remote_configuration_does_not_select_local_mode(monkeypatch):
    monkeypatch.setattr(settings, "assets_source_url", "https://source.internal")
    monkeypatch.setattr(settings, "assets_source_token", "short")
    db = AsyncMock()

    with pytest.raises(AssetsSourceGatewayError) as raised:
        AssetsSourceGateway(db)

    assert raised.value.status_code == 503
    db.scalar.assert_not_awaited()


async def test_local_gateway_composes_the_shared_runtime_workflow(monkeypatch):
    monkeypatch.setattr(settings, "assets_source_url", "")
    monkeypatch.setattr(settings, "assets_source_token", "")
    gateway = AssetsSourceGateway(
        AsyncMock(), storage=SimpleNamespace(media_cache_bucket="source-cache")
    )

    assert isinstance(gateway._local_workflow(), UploadWorkflow)
    assert not gateway.is_remote


async def test_remote_response_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(settings, "assets_source_url", "https://source.internal")
    monkeypatch.setattr(settings, "assets_source_token", TOKEN)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"x" * (16 * 1024 * 1024 + 1),
            headers={"content-type": "application/json"},
        )

    gateway = AssetsSourceGateway(AsyncMock(), transport=httpx.MockTransport(handler))
    with pytest.raises(AssetsSourceGatewayError) as raised:
        await gateway.init_upload(_upload_init_request())
    assert raised.value.status_code == 502


async def test_legacy_local_binding_uses_frozen_scope_and_metadata_arguments():
    db = AsyncMock()
    db.scalar.return_value = {
        "schema": "aap-legacy-dataset-upload/v1",
        "ok": True,
        "data": _legacy_state(),
    }
    port = _LegacyUploadData(db)
    lock = LegacyUploadLockRequest(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        item_id=ITEM,
    )

    state = await port.lock(lock)
    sql, params = db.scalar.await_args.args
    assert "lock_legacy_dataset_upload" in str(sql)
    assert params == {
        "operation": OPERATION,
        "actor": ACTOR,
        "dataset": DATASET,
        "item": ITEM,
    }
    assert state.state == "pending"

    db.scalar.return_value = {
        "schema": "aap-legacy-dataset-upload/v1",
        "ok": True,
        "data": _legacy_state(
            state="completed",
            result={"item_id": str(ITEM), "linked_tasks": 0},
        ),
    }
    completion = LegacyUploadCompletion(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        item_id=ITEM,
        expected_file_path="legacy/image.jpg",
        file_size=12,
        content_hash="A" * 32,
        width=640,
        height=480,
    )
    state = await port.complete(completion)
    sql, params = db.scalar.await_args.args
    assert "complete_legacy_dataset_upload" in str(sql)
    assert params["file_path"] == "legacy/image.jpg"
    assert params["content_hash"] == "a" * 32
    assert (params["width"], params["height"]) == (640, 480)
    assert state.state == "completed"


async def test_upload_init_route_reuses_key_or_returns_generated_item_key(monkeypatch):
    captured = []

    class Gateway:
        is_remote = False

        async def init_upload(self, request):
            captured.append(request)
            return UploadInitResult(
                actor_id=ACTOR,
                dataset_id=DATASET,
                item_id=request.item_id,
                upload_url="/minio/import/item?signature=abc",
            )

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    db = SimpleNamespace(commit=AsyncMock())
    response = Response()
    body = dataset_routes.DatasetUploadInitRequest(file_name="image.jpg")
    result = await dataset_routes.upload_init(
        DATASET,
        body,
        response,
        db,
        SimpleNamespace(id=ACTOR),
        idempotency_key=OPERATION,
    )

    assert captured[0].item_id == OPERATION
    assert response.headers["Idempotency-Key"] == str(OPERATION)
    assert result.item_id == OPERATION
    db.commit.assert_awaited_once()

    response = Response()
    await dataset_routes.upload_init(
        DATASET,
        body,
        response,
        db,
        SimpleNamespace(id=ACTOR),
        idempotency_key=None,
    )
    assert response.headers["Idempotency-Key"] == str(captured[1].item_id)
    assert captured[1].item_id != OPERATION


async def test_upload_complete_commits_before_media_dispatch_and_echoes_key(
    monkeypatch,
):
    events = []

    class Gateway:
        is_remote = False

        async def complete_upload(self, request):
            events.append(("complete", request))
            return UploadOutcome(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                item_id=request.item_id,
                file_type="image",
                result=DatasetUploadCompletedResult(item_id=ITEM, linked_tasks=1),
                duplicate_of=None,
            )

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    db = SimpleNamespace(
        commit=AsyncMock(side_effect=lambda: events.append(("commit", None)))
    )
    dispatcher = SimpleNamespace(
        dispatch_thumbnail=lambda item: events.append(("thumbnail", item)),
        dispatch_video_metadata=lambda item: events.append(("video", item)),
    )
    response = Response()
    result = await dataset_routes.upload_complete(
        DATASET,
        ITEM,
        response,
        db,
        SimpleNamespace(id=ACTOR),
        dispatcher,
        idempotency_key=OPERATION,
    )

    assert [event[0] for event in events] == ["complete", "commit", "thumbnail"]
    assert events[0][1].operation_id == OPERATION
    assert response.headers["Idempotency-Key"] == str(OPERATION)
    assert result["linked_tasks"] == 1


@pytest.mark.parametrize(
    ("result", "duplicate_of", "status_code"),
    [(None, None, 400), (None, OTHER_ITEM, 409)],
)
async def test_upload_missing_or_duplicate_disposition_is_committed_before_error(
    monkeypatch, result, duplicate_of, status_code
):
    events = []

    class Gateway:
        is_remote = False

        async def complete_upload(self, request):
            events.append("complete")
            return UploadOutcome(
                operation_id=request.operation_id,
                actor_id=request.actor_id,
                dataset_id=request.dataset_id,
                item_id=request.item_id,
                file_type="image",
                result=result,
                duplicate_of=duplicate_of,
            )

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    db = SimpleNamespace(commit=AsyncMock(side_effect=lambda: events.append("commit")))
    dispatcher = SimpleNamespace(
        dispatch_thumbnail=AsyncMock(), dispatch_video_metadata=AsyncMock()
    )
    with pytest.raises(HTTPException) as raised:
        await dataset_routes.upload_complete(
            DATASET,
            ITEM,
            Response(),
            db,
            SimpleNamespace(id=ACTOR),
            dispatcher,
            idempotency_key=OPERATION,
        )
    assert raised.value.status_code == status_code
    assert events == ["complete", "commit"]
    dispatcher.dispatch_thumbnail.assert_not_awaited()
    dispatcher.dispatch_video_metadata.assert_not_awaited()


@pytest.mark.parametrize("item_id", [None, ITEM])
async def test_delete_routes_forward_operation_key_and_commit_only_local(
    monkeypatch, item_id
):
    captured = []

    class Gateway:
        is_remote = False

        async def delete_source(self, request):
            captured.append(request)
            return _deletion_receipt(request)

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    monkeypatch.setattr(
        dataset_routes, "_deletion_audit_context", lambda _request: None
    )
    db = SimpleNamespace(commit=AsyncMock())
    response = Response()

    if item_id is None:
        await dataset_routes.delete_dataset(
            DATASET,
            SimpleNamespace(),
            response,
            db,
            SimpleNamespace(id=ACTOR),
            idempotency_key=OPERATION,
        )
    else:
        await dataset_routes.delete_item(
            DATASET,
            item_id,
            SimpleNamespace(),
            response,
            db,
            SimpleNamespace(id=ACTOR),
            idempotency_key=OPERATION,
        )
    assert captured[0].operation_id == OPERATION
    assert captured[0].item_id == item_id
    assert "cleanup_bucket" not in SourceDeletionRequest.model_fields
    assert response.headers["Idempotency-Key"] == str(OPERATION)
    db.commit.assert_awaited_once()


async def test_delete_route_does_not_commit_local_session_for_remote_owner(monkeypatch):
    class Gateway:
        is_remote = True

        async def delete_source(self, request):
            return _deletion_receipt(request)

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    monkeypatch.setattr(
        dataset_routes, "_deletion_audit_context", lambda _request: None
    )
    db = SimpleNamespace(commit=AsyncMock())
    await dataset_routes.delete_dataset(
        DATASET,
        SimpleNamespace(),
        Response(),
        db,
        SimpleNamespace(id=ACTOR),
        idempotency_key=None,
    )
    db.commit.assert_not_awaited()
