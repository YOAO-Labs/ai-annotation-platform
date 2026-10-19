import asyncio
from uuid import uuid4

import pytest

from aap_assets_cleanup import input_gc
from aap_assets_cleanup.config import load_config
from aap_assets_cleanup.input_gc import (
    build_input_garbage_collector,
    collect_inputs_before_stop,
)
from aap_assets_inputs import contract as inputs


def _environment():
    return {
        "ASSET_CLEANUP_DATABASE_URL": "postgresql://asset_cleanup:secret@postgres/annotation",
        "ASSET_CLEANUP_BUCKET": "media-cache",
        "ASSET_CLEANUP_S3_ENDPOINT": "http://minio:9000",
        "ASSET_CLEANUP_S3_ACCESS_KEY": "media-cleanup-user",
        "ASSET_CLEANUP_S3_SECRET_KEY": "media-cleanup-secret",
        "ASSET_CLEANUP_INPUTS_BUCKET": "accepted-inputs",
    }


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Connection:
    def __init__(self, garbage_id, lease_token, prefix):
        self.garbage_id = garbage_id
        self.lease_token = lease_token
        self.prefix = prefix
        self.operations = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, *_arguments):
        assert "set_config('statement_timeout'" in query

    async def fetchval(self, query, *arguments, timeout=None):
        if "claim_accepted_input_garbage" in query:
            self.operations.append(("claim", arguments))
            return {
                "schema": inputs.INPUT_SCHEMA,
                "ok": True,
                "data": {
                    "items": [
                        {
                            "garbage_id": str(self.garbage_id),
                            "prefix": self.prefix,
                            "lease_token": str(self.lease_token),
                        }
                    ]
                },
            }
        if "complete_accepted_input_garbage" in query:
            self.operations.append(("complete", arguments))
            return {
                "schema": inputs.INPUT_SCHEMA,
                "ok": True,
                "data": {
                    "garbage_id": str(self.garbage_id),
                    "completed": True,
                },
            }
        raise AssertionError("unexpected native input operation")


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self, timeout=None):
        return _Acquire(self.connection)


class _InputStorage:
    def __init__(self, config, *, source_storage):
        self.config = config
        self.source_storage = source_storage
        self.deleted = []
        self.versioning_checked = False

    async def assert_versioning(self):
        self.versioning_checked = True

    async def delete_attempt_versions(self, prefix):
        self.deleted.append(prefix)
        return 2

    async def close(self):
        return None


def _garbage():
    input_id, lease_token = uuid4(), uuid4()
    prefix = f"accepted-inputs/{input_id}/g3/a{lease_token}/"
    return input_id, lease_token, prefix


def test_input_contract_exposes_only_the_two_cleanup_capabilities():
    claim = inputs.OPERATIONS["claim_accepted_input_garbage"]
    complete = inputs.OPERATIONS["complete_accepted_input_garbage"]

    assert claim.role == complete.role == "cleanup"
    assert claim.arguments == ("worker_id", "lease_seconds", "page_size")
    assert claim.sql_types == ("uuid", "integer", "integer")
    assert complete.arguments == ("garbage_id", "lease_token")
    assert complete.sql_types == ("uuid", "uuid")
    assert inputs.OPERATIONS["sweep_accepted_image_inputs"].role == "system"


@pytest.mark.asyncio
async def test_factory_uses_dedicated_input_bucket_and_portable_collector(
    monkeypatch,
):
    config = load_config(_environment())
    _input_id, lease_token, prefix = _garbage()
    connection = _Connection(uuid4(), lease_token, prefix)
    created = []

    def storage_factory(storage_config, *, source_storage):
        value = _InputStorage(storage_config, source_storage=source_storage)
        created.append(value)
        return value

    monkeypatch.setattr(input_gc, "S3InputStorage", storage_factory)
    collector, storage = build_input_garbage_collector(config, _Pool(connection))

    assert await collector.collect_once() == 1
    assert len(created) == 1
    assert storage is created[0]
    assert storage.source_storage is None
    assert storage.config.inputs_bucket == "accepted-inputs"
    assert storage.config.inputs_bucket != config.bucket
    assert storage.config.cache_bucket == config.bucket
    assert storage.config.access_key == config.s3_access_key
    assert storage.config.secret_key == config.s3_secret_key
    assert storage.deleted == [prefix]
    assert [operation for operation, _arguments in connection.operations] == [
        "claim",
        "complete",
    ]
    assert connection.operations[0][1][1:] == (600, 100)
    assert connection.operations[1][1] == (connection.garbage_id, lease_token)


@pytest.mark.asyncio
async def test_failed_versioned_delete_leaves_owner_receipt_unacknowledged(
    monkeypatch,
):
    config = load_config(_environment())
    _input_id, lease_token, prefix = _garbage()
    connection = _Connection(uuid4(), lease_token, prefix)

    class FailedStorage(_InputStorage):
        async def delete_attempt_versions(self, prefix):
            raise inputs.InputFailure("storage_unavailable")

    monkeypatch.setattr(
        input_gc,
        "S3InputStorage",
        lambda storage_config, *, source_storage: FailedStorage(
            storage_config, source_storage=source_storage
        ),
    )
    collector, _storage = build_input_garbage_collector(config, _Pool(connection))

    with pytest.raises(inputs.InputFailure, match="storage_unavailable"):
        await collector.collect_once()
    assert [operation for operation, _arguments in connection.operations] == ["claim"]


@pytest.mark.asyncio
async def test_sigterm_cancels_before_ack_and_leaves_claim_for_native_reclaim(
    monkeypatch,
):
    config = load_config(_environment())
    _input_id, lease_token, prefix = _garbage()
    connection = _Connection(uuid4(), lease_token, prefix)
    started, cancelled = asyncio.Event(), asyncio.Event()

    class BlockingStorage(_InputStorage):
        async def delete_attempt_versions(self, prefix):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(
        input_gc,
        "S3InputStorage",
        lambda storage_config, *, source_storage: BlockingStorage(
            storage_config, source_storage=source_storage
        ),
    )
    collector, _storage = build_input_garbage_collector(config, _Pool(connection))
    stop = asyncio.Event()
    work = asyncio.create_task(collect_inputs_before_stop(collector, stop))
    await started.wait()
    stop.set()

    assert await work == 0
    assert cancelled.is_set()
    assert [operation for operation, _arguments in connection.operations] == ["claim"]
