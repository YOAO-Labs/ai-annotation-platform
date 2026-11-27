import asyncio
import io
import tempfile
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response

from yoao_assets_source.contract import (
    SourceFailure,
    SourceDeletionRequest,
    UploadInitRequest,
    UploadInitResult,
    UploadOutcome,
)
from yoao_assets_source.deletion_contract import DatasetSourceDeletionReceipt
from yoao_assets_source.ingest_contract import (
    INGEST_SCHEMA,
    SCAN_REQUEST_DIGEST,
    IngestCommand,
    IngestFailure,
    IngestFailureDetail,
    IngestReceipt,
)
from yoao_assets_source.legacy_upload_contract import (
    LegacyUploadCompletion,
    LegacyUploadLockRequest,
)
from yoao_assets_source.upload_contract import DatasetUploadCompletedResult
from yoao_assets_source_runtime.workflow import UploadWorkflow
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


def _ingest_command():
    return IngestCommand(
        operation_id=OPERATION,
        actor_id=ACTOR,
        dataset_id=DATASET,
        authority={
            "credential_type": "api_key",
            "credential_id": ITEM,
            "generation": None,
            "expires_at": None,
            "api_key_revision": "a" * 64,
            "api_key_scopes": ["datasets:write"],
            "dataset_lifecycle_revision": 1,
            "directory_protected": False,
        },
    )


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


async def test_upload_init_binds_original_workflow_even_with_source_host_configured(
    monkeypatch,
):
    from app.services import assets_source

    monkeypatch.setattr(settings, "assets_source_url", "http://source.internal:8767")
    monkeypatch.setattr(settings, "assets_source_token", "unused")
    monkeypatch.setattr(
        assets_source.managed_original_creation, "bootstrap", AsyncMock()
    )
    request = _upload_init_request()
    init = AsyncMock(
        return_value=UploadInitResult(
            actor_id=ACTOR,
            dataset_id=DATASET,
            item_id=ITEM,
            upload_url="/minio/import/item?signature=abc",
            expires_in=900,
        )
    )
    gateway = AssetsSourceGateway(AsyncMock())
    monkeypatch.setattr(
        gateway, "_local_workflow", lambda: SimpleNamespace(init_upload=init)
    )
    assert (await gateway.init_upload(request)).upload_url.startswith("/minio/import/")
    init.assert_awaited_once_with(request)
    gateway._db.commit.assert_not_awaited()


@pytest.mark.parametrize("invalid_scope", [False, True])
async def test_local_upload_init_validation_never_commits(monkeypatch, invalid_scope):
    init = AsyncMock()
    if invalid_scope:
        init.return_value = UploadInitResult(
            actor_id=OTHER_ITEM,
            dataset_id=DATASET,
            item_id=ITEM,
            upload_url="https://storage.example/put",
            expires_in=900,
        )
    else:
        init.side_effect = ValueError("private native result")
    db = AsyncMock()
    gateway = AssetsSourceGateway(db)
    monkeypatch.setattr(
        gateway, "_local_workflow", lambda: SimpleNamespace(init_upload=init)
    )
    with pytest.raises(AssetsSourceGatewayError) as raised:
        await gateway.init_upload(_upload_init_request())
    assert raised.value.status_code == 502
    assert "private native result" not in str(raised.value.detail)
    db.commit.assert_not_awaited()


async def test_local_gateway_composes_the_shared_runtime_workflow():
    gateway = AssetsSourceGateway(
        AsyncMock(), storage=SimpleNamespace(media_cache_bucket="source-cache")
    )
    db = gateway._db

    workflow = gateway._local_workflow()
    assert isinstance(workflow, UploadWorkflow)
    assert workflow._image_admission._native.caller_transaction is True
    assert workflow._image_admission._native.provider.db is db
    assert workflow._image_admission._native.provider.commit is False


async def test_local_ingest_uses_shared_workflow_sql_port_and_explicit_bucket():
    from yoao_assets_source_runtime.ingest_storage import IngestStorage
    from yoao_assets_source_runtime.ingest_workflow import IngestWorkflow
    from app.services.assets_source_ingest import DatasetSourceIngestData

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


async def test_upload_complete_commits_without_postcommit_thumbnail_dispatch_and_echoes_key(
    monkeypatch,
):
    events = []

    class Gateway:
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
async def test_delete_routes_forward_operation_key_and_commit_caller_transaction(
    monkeypatch, item_id
):
    captured = []

    class Gateway:
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


async def test_failed_delete_does_not_commit_caller_session(monkeypatch):
    failure = HTTPException(409, "source is retained")
    gateway = SimpleNamespace(delete_source=AsyncMock(side_effect=failure))
    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: gateway)
    monkeypatch.setattr(
        dataset_routes, "_deletion_audit_context", lambda _request: None
    )
    db = SimpleNamespace(commit=AsyncMock())
    with pytest.raises(HTTPException) as raised:
        await dataset_routes.delete_dataset(
            DATASET,
            SimpleNamespace(),
            Response(),
            db,
            SimpleNamespace(id=ACTOR),
            idempotency_key=None,
        )
    assert raised.value is failure
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("kind", ["zip", "scan"])
async def test_ingest_passes_exact_command_and_stream_to_owner(monkeypatch, kind):
    from app.services import assets_source

    monkeypatch.setattr(
        assets_source.managed_original_creation, "bootstrap", AsyncMock()
    )
    command = _ingest_command()
    receipt = IngestReceipt.model_validate(_ingest_reply(command, kind=kind)["data"])
    run = AsyncMock(return_value=receipt)
    gateway = AssetsSourceGateway(AsyncMock())
    monkeypatch.setattr(
        gateway,
        "_local_ingest_workflow",
        lambda: SimpleNamespace(upload_zip=run, scan=run),
    )
    if kind == "zip":
        with io.BytesIO(b"zip bytes") as source:
            assert await gateway.ingest_zip(command, source) == receipt
            run.assert_awaited_once_with(command, source)
    else:
        assert await gateway.ingest_scan(command) == receipt
        run.assert_awaited_once_with(command)


async def test_ingest_rejects_receipt_for_another_dataset(monkeypatch):
    command = _ingest_command()
    wrong = IngestReceipt.model_validate(_ingest_reply(command, kind="scan")["data"])
    wrong = wrong.model_copy(update={"dataset_id": OTHER_ITEM})
    gateway = AssetsSourceGateway(AsyncMock())
    monkeypatch.setattr(
        gateway,
        "_local_ingest_workflow",
        lambda: SimpleNamespace(scan=AsyncMock(return_value=wrong)),
    )
    with pytest.raises(AssetsSourceGatewayError) as raised:
        await gateway.ingest_scan(command)
    assert raised.value.status_code == 502
    gateway._db.commit.assert_not_awaited()


@pytest.fixture
def zip_spools(monkeypatch):
    spools = []

    class TrackedSpool(tempfile.SpooledTemporaryFile):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.written = 0
            self.close_calls = 0
            spools.append(self)

        def write(self, data):
            written = super().write(data)
            self.written += written
            return written

        def close(self):
            self.close_calls += 1
            super().close()

    monkeypatch.setattr(dataset_routes, "SpooledTemporaryFile", TrackedSpool)
    monkeypatch.setattr(dataset_routes, "_ZIP_SPOOL_MEMORY_BYTES", 1)
    return spools


@pytest.fixture
def zip_route(monkeypatch):
    monkeypatch.setattr(
        dataset_routes,
        "_ingest_authority",
        AsyncMock(return_value=_ingest_command().authority),
    )
    ingest = AsyncMock()

    class Gateway:
        ingest_zip = ingest

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())

    async def upload(request):
        return await dataset_routes.upload_zip(
            DATASET, request, db, SimpleNamespace(id=ACTOR), idempotency_key=OPERATION
        )

    return upload, ingest, db


async def test_zip_route_rejects_large_content_length_without_reading_body(
    monkeypatch, zip_route, zip_spools
):
    upload, ingest, db = zip_route
    monkeypatch.setattr(dataset_routes, "ZIP_MAX_BYTES", 16)
    monkeypatch.setattr(dataset_routes, "_ZIP_MULTIPART_OVERHEAD_BYTES", 8)
    receive_calls = []
    request = _multipart_request(
        b"unused", content_length=25, receive_calls=receive_calls
    )

    with pytest.raises(HTTPException) as raised:
        await upload(request)

    assert raised.value.status_code == 413
    assert raised.value.detail == dataset_routes._zip_too_large_error().detail
    assert receive_calls == []
    assert zip_spools == []
    ingest.assert_not_awaited()
    db.rollback.assert_awaited_once()


_ZIP_PART = (
    b"--assets-ingest-test\r\n"
    b'Content-Disposition: form-data; name="file"; filename="scene.zip"\r\n'
    b"Content-Type: application/zip\r\n\r\n"
)
_ZIP_END = b"\r\n--assets-ingest-test--\r\n"


def _assert_closed_zip_spool(spools, *, written):
    assert len(spools) == 1
    assert spools[0].written == written
    assert spools[0].closed and spools[0].close_calls == 1


@pytest.mark.parametrize("declared", [False, True])
async def test_zip_route_rejects_streamed_413_after_spooling(
    monkeypatch, zip_route, zip_spools, declared
):
    upload, ingest, db = zip_route
    first_chunk = _ZIP_PART + b"first"
    limit = len(first_chunk) + 4
    monkeypatch.setattr(dataset_routes, "ZIP_MAX_BYTES", limit)
    monkeypatch.setattr(dataset_routes, "_ZIP_MULTIPART_OVERHEAD_BYTES", 0)
    receive_calls = []
    request = _multipart_request(
        b"unused",
        content_length=limit if declared else None,
        chunks=[first_chunk, b"12345", b"unread tail"],
        receive_calls=receive_calls,
    )

    with pytest.raises(HTTPException) as raised:
        await upload(request)

    assert raised.value.status_code == 413
    assert raised.value.detail == dataset_routes._zip_too_large_error().detail
    assert len(receive_calls) == 2
    _assert_closed_zip_spool(zip_spools, written=len(b"first"))
    ingest.assert_not_awaited()
    db.rollback.assert_awaited_once()


async def test_zip_route_rejects_declared_body_with_oversize_zip_part(
    monkeypatch, zip_route, zip_spools
):
    upload, ingest, db = zip_route
    payload = b"large zip"
    overall_size = len(_ZIP_PART) + len(payload) + len(_ZIP_END)
    monkeypatch.setattr(dataset_routes, "ZIP_MAX_BYTES", len(payload) - 1)

    with pytest.raises(HTTPException) as raised:
        await upload(_multipart_request(payload, content_length=overall_size))

    assert raised.value.status_code == 413
    assert raised.value.detail == dataset_routes._zip_too_large_error().detail
    _assert_closed_zip_spool(zip_spools, written=len(payload))
    ingest.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.parametrize(
    ("second_part", "detail"),
    [
        (
            b'form-data; name="other"; filename="other.zip"',
            "Too many files. Maximum number of files is 1.",
        ),
        (b'form-data; name="other"', "Too many fields. Maximum number of fields is 0."),
        (
            b'form-data; filename="other.zip"',
            'The Content-Disposition header field "name" must be provided.',
        ),
    ],
)
async def test_zip_route_rejects_multipart_validation_errors_after_spooling(
    zip_route, zip_spools, second_part, detail
):
    upload, ingest, db = zip_route
    payload = b"existing zip"
    request = _multipart_request(
        b"unused",
        chunks=[
            _ZIP_PART + payload,
            b"\r\n--assets-ingest-test\r\nContent-Disposition: "
            + second_part
            + b"\r\n\r\nextra"
            + _ZIP_END,
        ],
    )

    with pytest.raises(HTTPException) as raised:
        await upload(request)

    assert (raised.value.status_code, raised.value.detail) == (400, detail)
    _assert_closed_zip_spool(zip_spools, written=len(payload))
    ingest.assert_not_awaited()
    db.rollback.assert_awaited_once()


async def test_zip_route_rejects_incomplete_second_part(zip_route, zip_spools):
    upload, ingest, db = zip_route
    payload = b"valid first part"
    request = _multipart_request(
        b"unused", chunks=[_ZIP_PART + payload + b"\r\n--assets-ingest-test\r\n"]
    )

    with pytest.raises(HTTPException) as raised:
        await upload(request)

    assert (raised.value.status_code, raised.value.detail) == (
        400,
        "Incomplete multipart.",
    )
    _assert_closed_zip_spool(zip_spools, written=len(payload))
    ingest.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.parametrize("cancel", [False, True])
async def test_zip_route_closes_spool_on_disconnect_or_cancel(
    zip_route, zip_spools, cancel
):
    upload, ingest, db = zip_route
    received_partial = asyncio.Event()
    payload = b"partial zip bytes"

    async def receive():
        if not zip_spools:
            return {
                "type": "http.request",
                "body": _ZIP_PART + payload,
                "more_body": True,
            }
        received_partial.set()
        if cancel:
            await asyncio.Future()
        return {"type": "http.disconnect"}

    request = Request(_multipart_request(b"unused").scope, receive)
    if cancel:
        task = asyncio.create_task(upload(request))
        try:
            await asyncio.wait_for(received_partial.wait(), timeout=5)
        finally:
            task.cancel("cancelled after spooling")
        with pytest.raises(asyncio.CancelledError, match="cancelled after spooling"):
            await task
    else:
        with pytest.raises(ClientDisconnect):
            await upload(request)
        db.rollback.assert_awaited_once()

    assert received_partial.is_set()
    _assert_closed_zip_spool(zip_spools, written=len(payload))
    ingest.assert_not_awaited()


async def test_zip_route_drains_running_spool_write_before_cancel_closes_file(
    monkeypatch, zip_route, zip_spools
):
    upload, ingest, _ = zip_route
    spool_factory = dataset_routes.SpooledTemporaryFile
    loop = asyncio.get_running_loop()
    writing = asyncio.Event()
    release = Event()

    class BlockingSpool(spool_factory):
        def write(self, data):
            loop.call_soon_threadsafe(writing.set)
            assert release.wait(timeout=5)
            return super().write(data)

    monkeypatch.setattr(dataset_routes, "SpooledTemporaryFile", BlockingSpool)
    payload = b"zip while writing"
    task = asyncio.create_task(upload(_multipart_request(payload)))
    try:
        await asyncio.wait_for(writing.wait(), timeout=5)
        task.cancel("cancelled during write")
        await asyncio.sleep(0)
        assert not zip_spools[0].closed and not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError, match="cancelled during write"):
        await task
    _assert_closed_zip_spool(zip_spools, written=len(payload))
    ingest.assert_not_awaited()


async def test_zip_route_closes_spool_when_gateway_fails(monkeypatch):
    monkeypatch.setattr(
        dataset_routes,
        "_ingest_authority",
        AsyncMock(return_value=_ingest_command().authority),
    )
    streams = []

    class Gateway:
        async def ingest_zip(self, _command, source):
            streams.append(source)
            assert source.read() == b"zip bytes"
            raise HTTPException(503, detail="source unavailable")

    monkeypatch.setattr(dataset_routes, "AssetsSourceGateway", lambda _db: Gateway())
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    body = _ZIP_PART + b"zip bytes" + _ZIP_END

    with pytest.raises(HTTPException) as raised:
        await dataset_routes.upload_zip(
            DATASET,
            _multipart_request(
                b"unused", chunks=[body[:29], body[29:47], body[47:-5], body[-5:]]
            ),
            db,
            SimpleNamespace(id=ACTOR),
            idempotency_key=OPERATION,
        )

    assert raised.value.status_code == 503
    assert len(streams) == 1
    assert streams[0].closed
    db.rollback.assert_awaited_once()


async def test_ingest_public_response_is_size_bounded(monkeypatch):
    command = _ingest_command()
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
    from app.services import assets_source

    monkeypatch.setattr(
        assets_source.managed_original_creation, "bootstrap", AsyncMock()
    )
    command = _ingest_command()
    error = IngestFailure(
        code, IngestFailureDetail.model_validate(detail) if detail is not None else None
    )
    db = AsyncMock()
    gateway = AssetsSourceGateway(db)
    monkeypatch.setattr(
        gateway,
        "_local_ingest_workflow",
        lambda: SimpleNamespace(scan=AsyncMock(side_effect=error)),
    )
    with pytest.raises(AssetsSourceGatewayError) as raised:
        await gateway.ingest_scan(command)

    assert raised.value.status_code == status
    assert raised.value.detail == public_detail
    db.scalar.assert_not_awaited()
