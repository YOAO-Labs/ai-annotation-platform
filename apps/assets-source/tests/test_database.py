from __future__ import annotations

import json
from uuid import uuid4

import pytest

from aap_assets_source.contract import SourceFailure, UploadInitRequest
from aap_assets_source.deletion_contract import DatasetSourceDeletionCommand
from aap_assets_source_runtime.config import load_config
from aap_assets_source_runtime.database import (
    EXPECTED_ASSET_SOURCE_FUNCTIONS,
    NativeDeletionData,
    NativeLegacyUploadData,
    ROLE_PREFLIGHT_SQL,
    SourceDatabase,
)
from aap_assets_source.legacy_upload_contract import (
    LegacyUploadCompletion,
    LegacyUploadLockRequest,
)
from aap_assets_source.upload_contract import DatasetUploadFailure


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


def _upload_intent(actor_id, dataset_id, item_id, *, file_name="photo.png"):
    return {
        "item_id": str(item_id),
        "actor_id": str(actor_id),
        "dataset_id": str(dataset_id),
        "file_name": file_name,
        "file_path": None,
        "file_type": "image",
        "content_type": "image/png",
        "staging_key": f"dataset-uploads/{item_id}/source",
        "publication_id": None,
        "state": "pending",
        "result": None,
    }


class _Transaction:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        self.events.append("begin")

    async def __aexit__(self, exc_type, _exc, _traceback):
        self.events.append("rollback" if exc_type else "commit")


class _Connection:
    def __init__(self, reply):
        self.reply = reply
        self.replies = []
        self.calls = []
        self.transaction_events = []

    def transaction(self):
        return _Transaction(self.transaction_events)

    async def fetchval(self, statement, *arguments):
        self.calls.append((statement, arguments))
        if statement == "SELECT 1":
            return 1
        if self.replies:
            return self.replies.pop(0)
        return self.reply


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return None


class _Pool:
    def __init__(self, connection):
        self.connection = connection
        self.closed = False

    def acquire(self, *, timeout=None):
        return _Acquire(self.connection)

    async def close(self):
        self.closed = True


def _request(*, file_name="photo.png"):
    return UploadInitRequest(
        item_id=uuid4(),
        actor_id=uuid4(),
        dataset_id=uuid4(),
        file_name=file_name,
        content_type="image/png",
    )


@pytest.mark.asyncio
async def test_upload_reserve_calls_exact_native_capability_and_commits() -> None:
    request = _request()
    connection = _Connection(
        {
            "schema": "aap-dataset-upload/v1",
            "ok": True,
            "data": _upload_intent(
                request.actor_id, request.dataset_id, request.item_id
            ),
        }
    )
    database = SourceDatabase(_Pool(connection), _config())

    async with database.transaction() as ports:
        result = await ports.uploads.reserve(request)

    assert result.item_id == request.item_id
    sql, arguments = connection.calls[0]
    assert "aap_assets_v1.reserve_dataset_upload" in sql
    assert arguments == (
        request.item_id,
        request.actor_id,
        request.dataset_id,
        request.file_name,
        request.content_type,
    )
    assert connection.transaction_events == ["begin", "commit"]


@pytest.mark.asyncio
async def test_native_business_failure_rolls_back_without_losing_its_code() -> None:
    request = _request()
    connection = _Connection(
        {
            "schema": "aap-dataset-upload/v1",
            "ok": False,
            "error": {"code": "account_not_active"},
        }
    )
    database = SourceDatabase(_Pool(connection), _config())

    with pytest.raises(DatasetUploadFailure) as error:
        async with database.transaction() as ports:
            await ports.uploads.reserve(request)

    assert error.value.code == "account_not_active"
    assert connection.transaction_events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_unicode_reply_budget_is_exact_and_oversize_rolls_back() -> None:
    request = _request(file_name="咖啡.png")
    payload = {
        "schema": "aap-dataset-upload/v1",
        "ok": True,
        "data": _upload_intent(
            request.actor_id,
            request.dataset_id,
            request.item_id,
            file_name=request.file_name,
        ),
    }
    encoded_size = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    exact_connection = _Connection(payload)
    exact_database = SourceDatabase(
        _Pool(exact_connection), _config(max_response_bytes=encoded_size)
    )

    async with exact_database.transaction() as ports:
        result = await ports.uploads.reserve(request)
    assert result.file_name == "咖啡.png"
    assert exact_connection.transaction_events == ["begin", "commit"]

    small_connection = _Connection(payload)
    small_database = SourceDatabase(
        _Pool(small_connection), _config(max_response_bytes=encoded_size - 1)
    )
    with pytest.raises(SourceFailure) as error:
        async with small_database.transaction() as ports:
            await ports.uploads.reserve(request)
    assert error.value.code == "invalid_response"
    assert small_connection.transaction_events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_scope_mismatch_and_wrong_legacy_receipt_rollback() -> None:
    request = _request()
    wrong_scope = _upload_intent(uuid4(), request.dataset_id, request.item_id)
    connection = _Connection(
        {"schema": "aap-dataset-upload/v1", "ok": True, "data": wrong_scope}
    )
    database = SourceDatabase(_Pool(connection), _config())

    with pytest.raises(SourceFailure) as error:
        async with database.transaction() as ports:
            await ports.uploads.reserve(request)
    assert error.value.code == "invalid_response"
    assert connection.transaction_events == ["begin", "rollback"]


def test_role_preflight_is_exactly_fourteen_source_functions_and_rejects_role_links() -> (
    None
):
    assert EXPECTED_ASSET_SOURCE_FUNCTIONS == {
        "reserve_dataset_upload(uuid,uuid,uuid,text,text)",
        "lock_dataset_upload(uuid,uuid,uuid)",
        "find_dataset_upload_duplicate(uuid,uuid,uuid,text)",
        "complete_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer)",
        "discard_dataset_upload(uuid,uuid,uuid,text)",
        "delete_dataset(uuid,uuid,uuid,text,jsonb)",
        "delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb)",
        "lock_legacy_dataset_upload(uuid,uuid,uuid,uuid)",
        "complete_legacy_dataset_upload(uuid,uuid,uuid,uuid,text,bigint,text,integer,integer)",
        "begin_dataset_ingest(uuid,uuid,uuid,jsonb)",
        "prepare_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)",
        "complete_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb)",
        "read_dataset_ingest_layout(uuid,uuid,uuid,uuid,integer)",
        "finish_dataset_ingest(uuid,uuid,uuid,jsonb)",
    }
    assert "membership.member = login_role.oid" in ROLE_PREFLIGHT_SQL
    assert "membership.roleid = login_role.oid" in ROLE_PREFLIGHT_SQL
    assert "runtime_privileges.no_table_privileges" in ROLE_PREFLIGHT_SQL
    assert "runtime_privileges.no_sequence_privileges" in ROLE_PREFLIGHT_SQL
    assert len(EXPECTED_ASSET_SOURCE_FUNCTIONS) == 14
    for signature in EXPECTED_ASSET_SOURCE_FUNCTIONS:
        assert f"('{signature}'::text)" in ROLE_PREFLIGHT_SQL


@pytest.mark.asyncio
async def test_legacy_lock_and_complete_use_exact_native_argument_order() -> None:
    operation_id, actor_id, dataset_id, item_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    file_path = f"legacy/{item_id}/image.png"
    pending = {
        "operation_id": str(operation_id),
        "actor_id": str(actor_id),
        "dataset_id": str(dataset_id),
        "item_id": str(item_id),
        "file_path": file_path,
        "file_type": "image",
        "width": None,
        "height": None,
        "state": "pending",
        "result": None,
        "duplicate_of": None,
    }
    completed = {
        **pending,
        "width": 640,
        "height": 480,
        "state": "completed",
        "result": {"item_id": str(item_id), "linked_tasks": 2},
    }
    connection = _Connection(None)
    connection.replies = [
        {"schema": "aap-legacy-dataset-upload/v1", "ok": True, "data": pending},
        {"schema": "aap-legacy-dataset-upload/v1", "ok": True, "data": completed},
    ]
    data = NativeLegacyUploadData(connection, 16_777_216)
    lock_request = LegacyUploadLockRequest(
        operation_id=operation_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
        item_id=item_id,
    )
    completion = LegacyUploadCompletion(
        operation_id=operation_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
        item_id=item_id,
        expected_file_path=file_path,
        file_size=12,
        content_hash=None,
        width=640,
        height=480,
    )

    assert (await data.lock(lock_request)).state == "pending"
    assert (await data.complete(completion)).state == "completed"
    assert "lock_legacy_dataset_upload" in connection.calls[0][0]
    assert connection.calls[0][1] == (
        operation_id,
        actor_id,
        dataset_id,
        item_id,
    )
    assert "complete_legacy_dataset_upload" in connection.calls[1][0]
    assert connection.calls[1][1] == (
        operation_id,
        actor_id,
        dataset_id,
        item_id,
        file_path,
        12,
        None,
        640,
        480,
    )


@pytest.mark.asyncio
async def test_delete_item_uses_configured_native_function_and_scope_order() -> None:
    operation_id, actor_id, dataset_id, item_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    reply = {
        "schema": "aap-dataset-source-delete/v1",
        "ok": True,
        "data": {
            "operation_id": str(operation_id),
            "actor_id": str(actor_id),
            "dataset_id": str(dataset_id),
            "item_id": str(item_id),
            "cleanup_bucket": "media-cache",
            "audit_id": 4,
            "deleted_datasets": 0,
            "deleted_items": 1,
            "deleted_scenes": 0,
            "deleted_associations": 0,
            "discarded_uploads": 0,
            "queued_storage_deletions": 0,
            "cancelled_creation_jobs": 0,
        },
    }
    connection = _Connection(reply)
    data = NativeDeletionData(connection, 16_777_216)
    command = DatasetSourceDeletionCommand(
        operation_id=operation_id,
        actor_id=actor_id,
        dataset_id=dataset_id,
        item_id=item_id,
        cleanup_bucket="media-cache",
        audit_context=None,
    )

    receipt = await data.delete(command)

    assert receipt.deleted_items == 1
    sql, arguments = connection.calls[0]
    assert "aap_assets_v1.delete_dataset_item" in sql
    assert arguments == (
        operation_id,
        actor_id,
        dataset_id,
        item_id,
        "media-cache",
        None,
    )
