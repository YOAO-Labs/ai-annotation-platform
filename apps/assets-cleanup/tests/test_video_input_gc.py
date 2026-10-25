"""The video cleanup lane leases, deletes and acknowledges exact attempt scopes."""

import asyncio
import threading
from uuid import UUID, uuid4

import pytest

from aap_assets_video_inputs import execution as video
from aap_assets_video_inputs_runtime.database import AsyncpgVideoInputDatabase
from aap_assets_video_inputs_runtime.garbage import VideoGarbageCollector
from aap_assets_video_inputs_runtime.storage import S3VideoInputStorage
from aap_assets_inputs.contract import InputFailure

from aap_assets_cleanup import input_gc
from aap_assets_cleanup.config import load_config
from aap_assets_cleanup.input_gc import (
    build_video_garbage_collector,
    collect_video_inputs_before_stop,
)


WORKER_ID = UUID("00000000-0000-4000-8000-000000000077")


def _config():
    return load_config(
        {
            "ASSET_CLEANUP_DATABASE_URL": "postgresql://cleanup:secret@postgres/anno",
            "ASSET_CLEANUP_BUCKET": "media-cache",
            "ASSET_CLEANUP_INPUTS_BUCKET": "accepted-inputs",
            "ASSET_CLEANUP_S3_ENDPOINT": "http://minio:9000",
            "ASSET_CLEANUP_S3_ACCESS_KEY": "cleanup-user",
            "ASSET_CLEANUP_S3_SECRET_KEY": "cleanup-secret",
        }
    )


def _claim():
    input_id, attempt_id = uuid4(), uuid4()
    return video.VideoGarbage(
        garbage_id=uuid4(),
        prefix=f"accepted-video-inputs/{input_id}/g2/a{attempt_id}/",
        lease_token=uuid4(),
    )


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.connection.events.append(("commit", self.connection.operation))
        return False


class _Connection:
    def __init__(self, claim, events):
        self.claimed = claim
        self.events = events
        self.operation = None
        self.statements = []

    def transaction(self):
        return _Transaction(self)

    async def execute(self, query, *_arguments):
        assert "set_config('statement_timeout'" in query

    async def fetchval(self, query, *arguments, timeout=None):
        self.statements.append(query)
        if "claim_accepted_video_input_garbage" in query:
            self.operation = "claim"
            self.events.append(("claim", arguments))
            return {
                "schema": video.EXECUTION_SCHEMA,
                "ok": True,
                "data": {"items": [self.claimed.model_dump(mode="json")]},
            }
        if "complete_accepted_video_input_garbage" in query:
            self.operation = "complete"
            self.events.append(("complete", arguments))
            return {
                "schema": video.EXECUTION_SCHEMA,
                "ok": True,
                "data": {"garbage_id": str(self.claimed.garbage_id), "completed": True},
            }
        raise AssertionError(f"unexpected native video cleanup operation: {query}")


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


class _VideoStorage:
    def __init__(self, config, *, source_storage, events, failure=None):
        self.config = config
        self.source_storage = source_storage
        self.events = events
        self.failure = failure
        self.closed = False

    async def assert_versioning(self):
        self.events.append(("versioning", self.config.inputs_bucket))

    async def delete_attempt_versions(self, prefix):
        self.events.append(("delete", prefix))
        if self.failure is not None:
            raise self.failure
        return 3

    async def close(self):
        self.closed = True


def _factory(monkeypatch, events, *, failure=None):
    created = []

    def storage_factory(config, *, source_storage):
        storage = _VideoStorage(
            config, source_storage=source_storage, events=events, failure=failure
        )
        created.append(storage)
        return storage

    monkeypatch.setattr(input_gc, "S3VideoInputStorage", storage_factory)
    return created


@pytest.mark.asyncio
async def test_video_factory_uses_only_cleanup_capabilities_and_committed_claims(
    monkeypatch,
):
    claim = _claim()
    events = []
    connection = _Connection(claim, events)
    created = _factory(monkeypatch, events)
    collector, storage = build_video_garbage_collector(_config(), _Pool(connection))
    collector.worker_id = WORKER_ID

    assert await collector.collect_once() == 1
    assert storage is created[0] and storage.source_storage is None
    assert storage.config.inputs_bucket == "accepted-inputs"
    assert [event[0] for event in events] == [
        "claim",
        "commit",
        "delete",
        "complete",
        "commit",
    ]
    assert events[2] == ("delete", claim.prefix)
    assert events[3][1] == (claim.garbage_id, claim.lease_token)
    assert all(
        "aap_assets_v1.claim_accepted_video_input_garbage" in statement
        or "aap_assets_v1.complete_accepted_video_input_garbage" in statement
        for statement in connection.statements
    )
    cleanup_ops = {
        name
        for name, operation in video.OPERATIONS.items()
        if operation.role == "cleanup"
    }
    assert cleanup_ops == {
        "claim_accepted_video_input_garbage",
        "complete_accepted_video_input_garbage",
    }


@pytest.mark.asyncio
async def test_video_delete_failure_never_acknowledges_lease(monkeypatch):
    claim = _claim()
    events = []
    connection = _Connection(claim, events)
    _factory(
        monkeypatch,
        events,
        failure=video.VideoInputFailure("storage_unavailable"),
    )
    collector, _storage = build_video_garbage_collector(_config(), _Pool(connection))

    with pytest.raises(video.VideoInputFailure, match="storage_unavailable"):
        await collector.collect_once()
    assert [event[0] for event in events] == ["claim", "commit", "delete"]
    assert len(connection.statements) == 1
    assert "complete_accepted_video_input_garbage" not in connection.statements[0]


@pytest.mark.asyncio
async def test_video_collector_cancels_before_ack_when_shutdown_arrives():
    started, cancelled = asyncio.Event(), asyncio.Event()
    stop = asyncio.Event()

    class Collector:
        async def collect_once(self):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    work = asyncio.create_task(collect_video_inputs_before_stop(Collector(), stop))
    await started.wait()
    stop.set()
    assert await work == 0
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_video_shutdown_waits_for_current_s3_call_and_skips_acknowledgement():
    started, release = threading.Event(), threading.Event()

    class BlockingVersionedClient:
        calls = 0

        def list_multipart_uploads(self, **_parameters):
            self.calls += 1
            started.set()
            assert release.wait(2)
            return {"Uploads": []}

    claim = _claim()
    events = []
    connection = _Connection(claim, events)
    client = BlockingVersionedClient()
    storage = S3VideoInputStorage(
        _config().inputs_storage,
        source_storage=None,
        client=client,
        signer=client,
    )
    collector = VideoGarbageCollector(
        AsyncpgVideoInputDatabase(_Pool(connection), allowed_roles={"cleanup"}),
        storage,
        worker_id=WORKER_ID,
    )
    stop = asyncio.Event()
    work = asyncio.create_task(collect_video_inputs_before_stop(collector, stop))

    assert await asyncio.to_thread(started.wait, 2)
    stop.set()
    await asyncio.sleep(0.05)
    assert not work.done()
    release.set()

    assert await asyncio.wait_for(work, 2) == 0
    assert client.calls == 1
    assert [event[0] for event in events] == ["claim", "commit"]


def test_video_storage_has_a_video_only_prefix_allowlist():
    input_id, attempt_id = uuid4(), uuid4()
    video_prefix = f"accepted-video-inputs/{input_id}/g4/a{attempt_id}/"
    image_prefix = f"accepted-inputs/{input_id}/g4/a{attempt_id}/"

    S3VideoInputStorage._check_prefix(video_prefix)
    with pytest.raises(InputFailure):
        S3VideoInputStorage._check_prefix(image_prefix)
