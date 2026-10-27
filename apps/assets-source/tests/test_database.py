from __future__ import annotations

import json
from uuid import uuid4

import pytest

from aap_assets_source.contract import SourceFailure, UploadInitRequest
from aap_assets_source.connections_contract import (
    CONNECTION_NATIVE_ARGUMENTS,
    CONNECTION_NATIVE_OPERATIONS,
    CONNECTION_NATIVE_SIGNATURES,
    CaptureConnectionRequest,
    ConnectionFailure,
    ConnectionPolicyRequest,
    ConnectionProbeResult,
    ConnectionReceiptRequest,
    CreateConnectionIntent,
    CreateConnectionNativeRequest,
    DeleteConnectionRequest,
    EmptyConnectionIntent,
    ListConnectionsRequest,
    NativeConnectionsPage,
    ReadConnectionRequest,
    RecordConnectionTestRequest,
    UpdateConnectionAuthorizedRequest,
    UpdateConnectionIntent,
)
from aap_assets_source.deletion_contract import DatasetSourceDeletionCommand
from aap_assets_source_runtime.config import load_config
from aap_assets_source_runtime.database import (
    NativeDeletionData,
    NativeLegacyUploadData,
    NativeConnectionsData,
    SourceDatabase,
    _NativeData,
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


@pytest.mark.asyncio
async def test_all_nine_connection_capabilities_use_frozen_name_types_and_argument_order() -> (
    None
):
    actor_id, connection_id, operation_id = uuid4(), uuid4(), uuid4()
    digest = "a" * 64
    create_intent = CreateConnectionIntent(
        name="test connection",
        kind="s3",
        config={"endpoint": "https://storage.example.test", "bucket": "media"},
        scope="owner",
        secret_digest=digest,
    )
    update_intent = UpdateConnectionIntent(name=None, config=None, secret_digest=None)
    probe = ConnectionProbeResult(
        ok=False, message="连接失败: TimeoutError", sample_count=None
    )
    requests_and_args = [
        (
            "list_storage_connections",
            ListConnectionsRequest(actor_id=actor_id, page_size=37),
            (actor_id, None, None, 37),
        ),
        (
            "read_storage_connection",
            ReadConnectionRequest(actor_id=actor_id, connection_id=connection_id),
            (actor_id, connection_id),
        ),
        (
            "read_storage_connection_policy",
            ConnectionPolicyRequest(
                actor_id=actor_id, fallback_allowlist=["host.test"]
            ),
            (actor_id, ["host.test"]),
        ),
        (
            "capture_storage_connection",
            CaptureConnectionRequest(
                actor_id=actor_id,
                connection_id=connection_id,
                access="use",
            ),
            (actor_id, connection_id, "use"),
        ),
        (
            "read_storage_connection_receipt",
            ConnectionReceiptRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                action="test",
                connection_id=connection_id,
                intent=EmptyConnectionIntent(),
            ),
            (
                operation_id,
                actor_id,
                "test",
                connection_id,
                EmptyConnectionIntent().model_dump(mode="json"),
            ),
        ),
        (
            "create_storage_connection",
            CreateConnectionNativeRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                intent=create_intent,
                config=create_intent.config,
                secret_enc="encrypted-secret",
                policy_digest=digest,
                fallback_allowlist=["host.test"],
            ),
            (
                operation_id,
                actor_id,
                create_intent.model_dump(mode="json"),
                create_intent.config,
                "encrypted-secret",
                digest,
                ["host.test"],
                CreateConnectionNativeRequest(
                    operation_id=operation_id,
                    actor_id=actor_id,
                    intent=create_intent,
                    config=create_intent.config,
                    secret_enc="encrypted-secret",
                    policy_digest=digest,
                    fallback_allowlist=["host.test"],
                ).audit_context.model_dump(mode="json"),
            ),
        ),
        (
            "update_storage_connection_authorized",
            UpdateConnectionAuthorizedRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                connection_id=connection_id,
                intent=update_intent,
                config={"endpoint": "https://storage.example.test", "bucket": "media"},
                secret_enc=None,
                source_snapshot=digest,
                policy_digest=None,
                fallback_allowlist=["host.test"],
                api_key_id=None,
                required_scopes=[],
                expected_proof=None,
            ),
            (
                operation_id,
                actor_id,
                connection_id,
                update_intent.model_dump(mode="json"),
                {"endpoint": "https://storage.example.test", "bucket": "media"},
                None,
                digest,
                None,
                ["host.test"],
                UpdateConnectionAuthorizedRequest(
                    operation_id=operation_id,
                    actor_id=actor_id,
                    connection_id=connection_id,
                    intent=update_intent,
                    config={
                        "endpoint": "https://storage.example.test",
                        "bucket": "media",
                    },
                    secret_enc=None,
                    source_snapshot=digest,
                    policy_digest=None,
                    fallback_allowlist=["host.test"],
                ).audit_context.model_dump(mode="json"),
                None,
                [],
                None,
            ),
        ),
        (
            "delete_storage_connection",
            DeleteConnectionRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                connection_id=connection_id,
            ),
            (
                operation_id,
                actor_id,
                connection_id,
                DeleteConnectionRequest(
                    operation_id=operation_id,
                    actor_id=actor_id,
                    connection_id=connection_id,
                ).audit_context.model_dump(mode="json"),
            ),
        ),
        (
            "record_storage_connection_test",
            RecordConnectionTestRequest(
                operation_id=operation_id,
                actor_id=actor_id,
                connection_id=connection_id,
                source_snapshot=digest,
                policy_digest=digest,
                fallback_allowlist=["host.test"],
                result=probe,
            ),
            (
                operation_id,
                actor_id,
                connection_id,
                digest,
                digest,
                ["host.test"],
                probe.model_dump(mode="json"),
                RecordConnectionTestRequest(
                    operation_id=operation_id,
                    actor_id=actor_id,
                    connection_id=connection_id,
                    source_snapshot=digest,
                    policy_digest=digest,
                    fallback_allowlist=["host.test"],
                    result=probe,
                ).audit_context.model_dump(mode="json"),
            ),
        ),
    ]
    failure = ConnectionFailure("connection_missing").payload()
    connection = _Connection(failure)
    native = NativeConnectionsData(connection, 16_777_216)

    for operation, request, expected_arguments in requests_and_args:
        method = getattr(native, operation)
        with pytest.raises(ConnectionFailure) as error:
            await method(request)
        assert error.value.code == "connection_missing"
        expected_types = CONNECTION_NATIVE_SIGNATURES[operation].split(",")
        placeholders = ", ".join(
            f"${index}::{type_name}"
            for index, type_name in enumerate(expected_types, start=1)
        )
        assert connection.calls[-1] == (
            f"SELECT aap_assets_v1.{operation}({placeholders})",
            expected_arguments,
        )
        assert len(expected_arguments) == len(CONNECTION_NATIVE_ARGUMENTS[operation])
        assert type(request) is CONNECTION_NATIVE_OPERATIONS[operation][0]
    assert len(connection.calls) == 9


@pytest.mark.asyncio
async def test_connection_namespace_scope_and_response_budget_fail_closed() -> None:
    actor_id = uuid4()
    wrong_namespace = _Connection(
        {
            "schema": "aap-assets-source/v1",
            "ok": False,
            "error": {"code": "connection_missing"},
        }
    )
    native = NativeConnectionsData(wrong_namespace, 16_777_216)
    request = ListConnectionsRequest(actor_id=actor_id)
    with pytest.raises(ConnectionFailure) as error:
        await native.list_storage_connections(request)
    assert error.value.code == "invalid_response"

    wrong_scope = NativeConnectionsPage(
        actor_id=uuid4(),
        after=None,
        next_cursor=None,
        snapshot_digest="a" * 64,
        total=0,
        complete=True,
        connections=[],
    ).model_dump(mode="json")
    scope_connection = _Connection(
        {"schema": "aap-assets-connections/v1", "ok": True, "data": wrong_scope}
    )
    with pytest.raises(ConnectionFailure) as error:
        await NativeConnectionsData(
            scope_connection, 16_777_216
        ).list_storage_connections(request)
    assert error.value.code == "invalid_response"

    large_page = {
        "schema": "aap-assets-connections/v1",
        "ok": True,
        "data": {
            **wrong_scope,
            "actor_id": str(actor_id),
            "snapshot_digest": "a" * 64,
            "connections": [],
        },
    }
    budget_connection = _Connection(large_page)
    database = SourceDatabase(_Pool(budget_connection), _config(max_response_bytes=32))
    with pytest.raises(ConnectionFailure) as error:
        async with database.transaction() as ports:
            await ports.connections.list_storage_connections(request)
    assert error.value.code == "invalid_response"
    assert budget_connection.transaction_events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_unlisted_native_connection_operation_never_executes_sql() -> None:
    connection = _Connection(ConnectionFailure("invalid_request").payload())
    native = NativeConnectionsData(connection, 16_777_216)
    with pytest.raises(ConnectionFailure) as error:
        await native._call_connection(
            "arbitrary_sql_function", None, (), NativeConnectionsPage
        )
    assert error.value.code == "invalid_request"
    assert connection.calls == []


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

    assert (await data.read(lock_request)).state == "pending"
    assert (await data.lock(lock_request)).state == "pending"
    assert (await data.complete(completion)).state == "completed"
    assert "read_legacy_dataset_upload" in connection.calls[0][0]
    assert connection.calls[0][1] == (
        operation_id,
        actor_id,
        dataset_id,
        item_id,
    )
    assert "lock_legacy_dataset_upload" in connection.calls[1][0]
    assert connection.calls[1][1] == (
        operation_id,
        actor_id,
        dataset_id,
        item_id,
    )
    assert "complete_legacy_dataset_upload" in connection.calls[2][0]
    assert connection.calls[2][1] == (
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


def _material_reply(*, origin="direct", staging=True):
    dataset_id, item_id, publication_id = uuid4(), uuid4(), uuid4()
    file_path = f"dataset-sources/{dataset_id}/{item_id}/{publication_id}/photo.png"
    data = {
        "creation_id": str(uuid4()),
        "dataset_id": str(dataset_id),
        "location_id": "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7",
        "bucket": "datasets",
        "object_key": file_path,
        "key_prefix": "",
        "endpoint_url": "http://s3",
        "storage_identity": "a" * 64,
        "config_revision": 1,
        "binding_generation": 1,
        "max_bytes": 4096,
        "content_type": "image/png",
        "origin": origin,
        "mode": "write",
        "publication_id": str(publication_id),
        "file_path": file_path,
        "status": "allocated",
    }
    if staging:
        data["staging"] = {
            "bucket": "imports",
            "key": f"dataset-uploads/{item_id}/source",
        }
    return {
        "schema": "aap-managed-original/v1",
        "ok": True,
        "data": data,
    }


def test_native_reply_decoder_is_bounded_and_rejects_nonfinite_json():
    data = _NativeData(_Connection(None), 64)
    assert data._parse_payload('{"ok":true}') == {"ok": True}
    assert data._parse_payload(b'{"ok":true}') == {"ok": True}
    for reply in (
        '{"value": NaN}',
        '{"value": Infinity}',
        '{"value": ' + '"' + "x" * 64 + '"}',
        "not-json",
    ):
        with pytest.raises(SourceFailure) as error:
            data._parse_payload(reply)
        assert error.value.code == "invalid_response"
    with pytest.raises(SourceFailure):
        data._parse_payload({"value": float("inf")})
    with pytest.raises(SourceFailure):
        data._parse_payload({"value": "x" * 64})


@pytest.mark.asyncio
async def test_transaction_exposes_connection_and_probe_ports_and_allocates_objects_once():
    reply = _material_reply()
    connection = _Connection(reply)
    database = SourceDatabase(_Pool(connection), _config())

    async with database.transaction() as ports:
        assert isinstance(ports.connections, NativeConnectionsData)
        material = await ports.uploads.allocate(
            {
                "dataset_id": str(reply["data"]["dataset_id"]),
                "item_id": str(uuid4()),
                "publication_id": str(uuid4()),
                "authority": {"kind": "dataset_upload"},
                "origin": "direct",
                "mode": "write",
                "file_path": reply["data"]["file_path"],
                "logical_path": "photo.png",
                "file_type": "image",
                "content_type": "image/png",
                "max_bytes": 4096,
                "staging": {"key": "dataset-uploads/item/source"},
                "requested_by": None,
            }
        )

    assert str(material.creation_id) == reply["data"]["creation_id"]
    # Direct-origin material carries the owner-persisted staging ownership.
    assert material.staging is not None and material.staging.bucket == "imports"
