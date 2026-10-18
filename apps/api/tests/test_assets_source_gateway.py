import json
import io
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response

from aap_assets_source.contract import (
    SourceFailure,
    SourceDeletionRequest,
    UploadInitRequest,
    UploadInitResult,
    UploadOutcome,
)
from aap_assets_source.deletion_contract import DatasetSourceDeletionReceipt
from aap_assets_source.ingest_contract import (
    INGEST_SCHEMA,
    SCAN_REQUEST_DIGEST,
    IngestCommand,
    IngestReceipt,
)
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
    _LocalStoragePort,
    encode_ingest_public_result,
)

pytestmark = pytest.mark.asyncio

ACTOR = UUID("10000000-0000-4000-8000-000000000001")
DATASET = UUID("20000000-0000-4000-8000-000000000002")
ITEM = UUID("30000000-0000-4000-8000-000000000003")
OPERATION = UUID("40000000-0000-4000-8000-000000000004")
OTHER_ITEM = UUID("50000000-0000-4000-8000-000000000005")
TOKEN = "source-test-token-" + ("x" * 32)


def _ingest_reply(command: IngestCommand, *, kind: str) -> dict:
    receipt = IngestReceipt(
        operation_id=command.operation_id,
        actor_id=command.actor_id,
        dataset_id=command.dataset_id,
        kind=kind,
        request_digest=SCAN_REQUEST_DIGEST if kind == "scan" else "a" * 64,
        added=0,
        deduped=0,
        skipped=0,
        errors=[],
        error_count=0,
        total_in_zip=0 if kind == "zip" else None,
        linked_tasks=0,
        scene_inference_notes=[],
    )
    return {
        "schema": INGEST_SCHEMA,
        "ok": True,
        "data": receipt.model_dump(mode="json"),
    }


def _multipart_request(
    payload: bytes,
    *,
    content_length: int | None = None,
    chunks: list[bytes] | None = None,
    receive_calls: list[int] | None = None,
) -> Request:
    boundary = b"assets-ingest-test"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="scene.zip"\r\n'
        b"Content-Type: application/zip\r\n\r\n"
        + payload
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )
    messages = chunks if chunks is not None else [body]
    index = 0

    async def receive():
        nonlocal index
        if receive_calls is not None:
            receive_calls.append(1)
        if index >= len(messages):
            return {"type": "http.disconnect"}
        chunk = messages[index]
        index += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(messages),
        }

    headers = [(b"content-type", b"multipart/form-data; boundary=" + boundary)]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/datasets/items/upload-zip",
            "headers": headers,
        },
        receive,
    )


@pytest.mark.parametrize(
    "code,expected",
    [
        ("PreconditionFailed", "source_changed"),
        ("NoSuchKey", "source_changed"),
        ("AccessDenied", "storage_unavailable"),
    ],
)
async def test_local_conditional_dimensions_fail_closed_like_independent_storage(
    code, expected
):
    from botocore.exceptions import ClientError
    from unittest.mock import Mock

    storage = SimpleNamespace(
        datasets_bucket="datasets",
        import_bucket="imports",
        read_image_dimensions=Mock(
            side_effect=ClientError({"Error": {"Code": code}}, "GetObject")
        ),
    )
    port = _LocalStoragePort(storage)
    with pytest.raises(SourceFailure) as failure:
        await port.read_legacy_image_dimensions("source", if_match='"original"')
    assert failure.value.code == expected


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
    db = gateway._db

    workflow = gateway._local_workflow()
    assert isinstance(workflow, UploadWorkflow)
    assert workflow._image_admission._native.caller_transaction is True
    assert workflow._image_admission._native.provider.db is db
    assert workflow._image_admission._native.provider.commit is False
    assert not gateway.is_remote


async def test_local_ingest_uses_shared_workflow_sql_port_and_explicit_bucket(
    monkeypatch,
):
    from aap_assets_source_runtime.ingest_storage import IngestStorage
    from aap_assets_source_runtime.ingest_workflow import IngestWorkflow
    from app.services.assets_source_ingest import DatasetSourceIngestData

    monkeypatch.setattr(settings, "assets_source_url", "")
    monkeypatch.setattr(settings, "assets_source_token", "")
    client = object()
    storage = SimpleNamespace(
        client=client,
        datasets_bucket="source-datasets",
        media_cache_bucket="source-cache",
    )
    db = AsyncMock()
    gateway = AssetsSourceGateway(db, storage=storage)

    workflow = gateway._local_ingest_workflow()

    assert isinstance(workflow, IngestWorkflow)
    assert isinstance(workflow._data, DatasetSourceIngestData)
    assert isinstance(workflow._storage, IngestStorage)
    assert workflow._storage._client is client
    assert workflow._storage.datasets_bucket == "source-datasets"
    assert workflow._image_admission._native.caller_transaction is True
    assert workflow._image_admission._native.provider.db is db
    assert workflow._image_admission._native.provider.commit is False
    db.scalar.assert_not_awaited()


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

    state = await port.read(lock)
    sql, params = db.scalar.await_args.args
    assert "read_legacy_dataset_upload" in str(sql)
    assert params == {
        "operation": OPERATION,
        "actor": ACTOR,
        "dataset": DATASET,
        "item": ITEM,
    }
    assert state.state == "pending"

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


async def test_upload_complete_commits_without_postcommit_thumbnail_dispatch_and_echoes_key(
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
    response = Response()
    result = await dataset_routes.upload_complete(
        DATASET,
        ITEM,
        response,
        db,
        SimpleNamespace(id=ACTOR),
        idempotency_key=OPERATION,
    )

    assert [event[0] for event in events] == ["complete", "commit"]
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
    with pytest.raises(HTTPException) as raised:
        await dataset_routes.upload_complete(
            DATASET,
            ITEM,
            Response(),
            db,
            SimpleNamespace(id=ACTOR),
            idempotency_key=OPERATION,
        )
    assert raised.value.status_code == status_code
    assert events == ["complete", "commit"]


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


class _SizedReadStream(io.BytesIO):
    def read(self, size=-1):
        assert size >= 0, "ZIP forwarding must not read the full archive at once"
        return super().read(size)


async def test_remote_ingest_zip_streams_raw_bytes_and_never_falls_back(monkeypatch):
    monkeypatch.setattr(settings, "assets_source_url", "http://source.internal:8767")
    monkeypatch.setattr(settings, "assets_source_token", TOKEN)
    command = IngestCommand(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
    )
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ingest_reply(command, kind="zip"))

    db = AsyncMock()
    gateway = AssetsSourceGateway(db, transport=httpx.MockTransport(handler))
    receipt = await gateway.ingest_zip(command, _SizedReadStream(b"zip bytes"))

    assert receipt.kind == "zip"
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v1/source/upload-zip"
    assert dict(request.url.params) == {
        "operation_id": str(OPERATION),
        "actor_id": str(ACTOR),
        "dataset_id": str(DATASET),
    }
    assert request.headers["content-type"] == "application/zip"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.content == b"zip bytes"
    db.scalar.assert_not_awaited()


async def test_remote_ingest_scan_sends_typed_command_and_receipt(monkeypatch):
    monkeypatch.setattr(settings, "assets_source_url", "https://source.internal")
    monkeypatch.setattr(settings, "assets_source_token", TOKEN)
    command = IngestCommand(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
    )
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_ingest_reply(command, kind="scan"))

    db = AsyncMock()
    gateway = AssetsSourceGateway(db, transport=httpx.MockTransport(handler))
    receipt = await gateway.ingest_scan(command)

    assert receipt.kind == "scan"
    assert len(seen) == 1
    assert seen[0].url.path == "/v1/source/scan"
    assert seen[0].headers["content-type"] == "application/json"
    assert json.loads(seen[0].content) == command.model_dump(mode="json")
    db.scalar.assert_not_awaited()


async def test_remote_ingest_preserves_archive_failure_and_maps_bad_reply(monkeypatch):
    monkeypatch.setattr(settings, "assets_source_url", "https://source.internal")
    monkeypatch.setattr(settings, "assets_source_token", TOKEN)
    command = IngestCommand(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
    )
    failure = {
        "schema": INGEST_SCHEMA,
        "ok": False,
        "error": {
            "code": "resource_budget_exceeded",
            "detail": {
                "reason": "resource_budget_exceeded",
                "message": "ZIP package exceeds the compressed-byte limit",
                "budget": "compressed_bytes",
                "limit": 209715200,
                "observed": 209715201,
            },
        },
    }

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json=failure)

    db = AsyncMock()
    gateway = AssetsSourceGateway(db, transport=httpx.MockTransport(handler))
    with pytest.raises(AssetsSourceGatewayError) as raised:
        await gateway.ingest_zip(command, _SizedReadStream(b"zip"))
    assert raised.value.status_code == 413
    assert raised.value.detail == failure["error"]["detail"]
    db.scalar.assert_not_awaited()

    async def bad_reply(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"schema": INGEST_SCHEMA, "ok": "yes"})

    bad_gateway = AssetsSourceGateway(db, transport=httpx.MockTransport(bad_reply))
    with pytest.raises(AssetsSourceGatewayError) as bad:
        await bad_gateway.ingest_scan(command)
    assert bad.value.status_code == 502
    db.scalar.assert_not_awaited()


async def test_zip_route_commits_after_receipt_validation_and_echoes_key(monkeypatch):
    command_data = []
    uploaded = []
    uploaded_streams = []

    class Gateway:
        is_remote = False

        async def ingest_zip(self, command, source):
            command_data.append(command)
            uploaded_streams.append(source)
            uploaded.append(source.read())
            return IngestReceipt.model_validate(
                _ingest_reply(command, kind="zip")["data"]
            )

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    response = await dataset_routes.upload_zip(
        DATASET,
        _multipart_request(b"zip bytes"),
        db,
        SimpleNamespace(id=ACTOR),
        idempotency_key=OPERATION,
    )

    assert uploaded == [b"zip bytes"]
    assert uploaded_streams[0].closed
    assert command_data[0].operation_id == OPERATION
    assert command_data[0].actor_id == ACTOR
    assert command_data[0].dataset_id == DATASET
    assert json.loads(response.body) == {
        "added": 0,
        "deduped": 0,
        "skipped": 0,
        "errors": [],
        "total_in_zip": 0,
        "linked_tasks": 0,
        "scene_inference_notes": [],
    }
    assert response.headers["Idempotency-Key"] == str(OPERATION)
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


async def test_zip_route_rejects_large_content_length_without_reading_body(monkeypatch):
    monkeypatch.setattr(dataset_routes, "ZIP_MAX_BYTES", 16)
    monkeypatch.setattr(dataset_routes, "_ZIP_MULTIPART_OVERHEAD_BYTES", 8)
    receive_calls = []
    request = _multipart_request(
        b"unused",
        content_length=25,
        receive_calls=receive_calls,
    )
    ingest = AsyncMock()

    class Gateway:
        is_remote = False
        ingest_zip = ingest

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    with pytest.raises(HTTPException) as raised:
        await dataset_routes.upload_zip(
            DATASET,
            request,
            db,
            SimpleNamespace(id=ACTOR),
            idempotency_key=OPERATION,
        )

    assert raised.value.status_code == 413
    assert receive_calls == []
    ingest.assert_not_awaited()
    db.rollback.assert_awaited_once()


async def test_zip_route_stops_chunked_body_and_parser_closes_spooled_file(monkeypatch):
    boundary = b"assets-ingest-test"
    part_headers = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="scene.zip"\r\n'
        b"Content-Type: application/zip\r\n\r\n"
    )
    first_chunk = part_headers + b"first"
    monkeypatch.setattr(dataset_routes, "ZIP_MAX_BYTES", len(first_chunk) + 4)
    monkeypatch.setattr(dataset_routes, "_ZIP_MULTIPART_OVERHEAD_BYTES", 0)
    receive_calls = []
    request = _multipart_request(
        b"unused",
        chunks=[first_chunk, b"12345", b"unread tail"],
        receive_calls=receive_calls,
    )
    real_spooled_file = tempfile.SpooledTemporaryFile
    spooled_files = []

    def tracked_spooled_file(*args, **kwargs):
        file = real_spooled_file(*args, **kwargs)
        spooled_files.append(file)
        return file

    monkeypatch.setattr(
        "starlette.formparsers.SpooledTemporaryFile", tracked_spooled_file
    )
    ingest = AsyncMock()

    class Gateway:
        is_remote = False
        ingest_zip = ingest

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    with pytest.raises(HTTPException) as raised:
        await dataset_routes.upload_zip(
            DATASET,
            request,
            db,
            SimpleNamespace(id=ACTOR),
            idempotency_key=OPERATION,
        )

    assert raised.value.status_code == 413
    assert len(receive_calls) == 2
    assert len(spooled_files) == 1
    assert spooled_files[0].closed
    ingest.assert_not_awaited()
    db.rollback.assert_awaited_once()


async def test_zip_route_closes_parser_spool_and_preserves_disconnect(monkeypatch):
    boundary = b"assets-ingest-test"
    first_chunk = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="scene.zip"\r\n'
        b"Content-Type: application/zip\r\n\r\n"
        b"partial zip bytes"
    )
    receive_calls = []

    async def receive():
        receive_calls.append(1)
        if len(receive_calls) == 1:
            return {"type": "http.request", "body": first_chunk, "more_body": True}
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/datasets/items/upload-zip",
            "headers": [
                (b"content-type", b"multipart/form-data; boundary=" + boundary)
            ],
        },
        receive,
    )
    real_spooled_file = tempfile.SpooledTemporaryFile
    spooled_files = []

    def tracked_spooled_file(*args, **kwargs):
        file = real_spooled_file(*args, **kwargs)
        spooled_files.append(file)
        return file

    monkeypatch.setattr(
        "starlette.formparsers.SpooledTemporaryFile", tracked_spooled_file
    )
    ingest = AsyncMock()

    class Gateway:
        is_remote = False
        ingest_zip = ingest

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    with pytest.raises(ClientDisconnect):
        await dataset_routes.upload_zip(
            DATASET,
            request,
            db,
            SimpleNamespace(id=ACTOR),
            idempotency_key=OPERATION,
        )

    assert len(receive_calls) == 2
    assert len(spooled_files) == 1
    assert spooled_files[0].closed
    ingest.assert_not_awaited()
    db.rollback.assert_awaited_once()


async def test_zip_route_closes_parsed_form_when_gateway_fails(monkeypatch):
    streams = []

    class Gateway:
        is_remote = False

        async def ingest_zip(self, _command, source):
            streams.append(source)
            raise HTTPException(503, detail="source unavailable")

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    with pytest.raises(HTTPException) as raised:
        await dataset_routes.upload_zip(
            DATASET,
            _multipart_request(b"zip bytes"),
            db,
            SimpleNamespace(id=ACTOR),
            idempotency_key=OPERATION,
        )

    assert raised.value.status_code == 503
    assert len(streams) == 1
    assert streams[0].closed
    db.rollback.assert_awaited_once()


async def test_scan_route_does_not_commit_remote_session(monkeypatch):
    command_data = []

    class Gateway:
        is_remote = True

        async def ingest_scan(self, command):
            command_data.append(command)
            return IngestReceipt.model_validate(
                _ingest_reply(command, kind="scan")["data"]
            )

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    response = await dataset_routes.scan_items(
        DATASET,
        db,
        SimpleNamespace(id=ACTOR),
        idempotency_key=OPERATION,
    )

    assert command_data[0].operation_id == OPERATION
    assert json.loads(response.body) == {
        "status": "ok",
        "new_items": 0,
        "linked_tasks": 0,
    }
    assert response.headers["Idempotency-Key"] == str(OPERATION)
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


async def test_ingest_public_response_is_size_bounded(monkeypatch):
    command = IngestCommand(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
    )
    receipt = IngestReceipt.model_validate(_ingest_reply(command, kind="zip")["data"])
    monkeypatch.setattr("app.services.assets_source.MAX_SOURCE_RESPONSE_BYTES", 1)

    with pytest.raises(AssetsSourceGatewayError) as raised:
        encode_ingest_public_result(receipt)

    assert raised.value.status_code == 502


@pytest.mark.parametrize(
    ("code", "detail", "status", "public_detail"),
    [
        (
            "reserved_namespace",
            None,
            422,
            "Dataset name uses a reserved storage namespace",
        ),
        (
            "temporal_scenes_missing",
            None,
            422,
            "声明为时序数据集但未识别出任何 scene,请检查目录结构",
        ),
        (
            "scene_conflict",
            {
                "reason": "scene_conflict",
                "message": "Scene name already exists",
                "conflicts_with": "scene-a",
            },
            409,
            {"reason": "scene_conflict", "conflicts_with": "scene-a"},
        ),
        ("operation_conflict", None, 409, {"reason": "operation_conflict"}),
    ],
)
async def test_ingest_public_error_status_and_detail_mapping(
    monkeypatch, code, detail, status, public_detail
):
    monkeypatch.setattr(settings, "assets_source_url", "https://source.internal")
    monkeypatch.setattr(settings, "assets_source_token", TOKEN)
    command = IngestCommand(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
    )
    error = {"code": code}
    if detail is not None:
        error["detail"] = detail

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"schema": INGEST_SCHEMA, "ok": False, "error": error},
        )

    db = AsyncMock()
    gateway = AssetsSourceGateway(db, transport=httpx.MockTransport(handler))
    with pytest.raises(AssetsSourceGatewayError) as raised:
        await gateway.ingest_scan(command)

    assert raised.value.status_code == status
    assert raised.value.detail == public_detail
    db.scalar.assert_not_awaited()


async def test_remote_ingest_transport_failure_is_503_without_local_fallback(
    monkeypatch,
):
    monkeypatch.setattr(settings, "assets_source_url", "https://source.internal")
    monkeypatch.setattr(settings, "assets_source_token", TOKEN)
    command = IngestCommand(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("source is offline")

    db = AsyncMock()
    gateway = AssetsSourceGateway(db, transport=httpx.MockTransport(handler))
    with pytest.raises(AssetsSourceGatewayError) as raised:
        await gateway.ingest_scan(command)

    assert raised.value.status_code == 503
    db.scalar.assert_not_awaited()

    async def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="source unavailable")

    unavailable_gateway = AssetsSourceGateway(
        db, transport=httpx.MockTransport(unavailable)
    )
    with pytest.raises(AssetsSourceGatewayError) as unavailable_error:
        await unavailable_gateway.ingest_scan(command)
    assert unavailable_error.value.status_code == 503
    db.scalar.assert_not_awaited()
