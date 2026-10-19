"""Process-level polling, selection, storage ownership and cleanup tests."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import logging
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_assets_video_inputs import execution as video_contract
from aap_assets_media_runtime import worker
from aap_assets_media_runtime.config import (
    MediaExecutionSettings,
    MediaStorageConfig,
    MediaWorkerConfig,
)
from aap_assets_media_runtime.process import (
    ClaimTrackedExecutor,
    ClaimTrackingDatabase,
    FairWorkerPoller,
    WorkerRuntime,
    create_worker_runtime,
    image_storage_config,
)


def _media_storage_config():
    return MediaStorageConfig(
        endpoint="http://minio.internal:9000",
        public_endpoint="http://minio.public:9000",
        region="us-east-1",
        datasets_bucket="datasets-bucket",
        cache_bucket="media-cache-bucket",
        inputs_bucket="retained-inputs-bucket",
        access_key="worker-access",
        secret_key="worker-secret",
        connect_timeout=3,
        read_timeout=20,
        max_attempts=4,
    )


def test_video_storage_config_reuses_image_schema_and_media_settings():
    configured = image_storage_config(_media_storage_config())

    assert configured.datasets_bucket == "datasets-bucket"
    assert configured.cache_bucket == "media-cache-bucket"
    assert configured.inputs_bucket == "retained-inputs-bucket"
    assert configured.legacy_bucket == configured.datasets_bucket
    assert configured.endpoint == "http://minio.internal:9000"
    assert configured.public_endpoint == "http://minio.public:9000"
    assert configured.access_key == "worker-access"
    assert configured.secret_key == "worker-secret"
    assert configured.connect_timeout == 3
    assert configured.read_timeout == 20
    assert configured.max_attempts == 4


@pytest.mark.asyncio
async def test_runtime_shares_one_pool_and_s3_pair_and_closes_each_owner_once(
    monkeypatch,
):
    from aap_assets_media_runtime import process
    from aap_assets_media_runtime import worker as worker_module

    events: list[str] = []

    class Client:
        def __init__(self, name):
            self.name = name

        def close(self):
            events.append(self.name + "-close")

    client, signer = Client("client"), Client("signer")

    class Database:
        pool = object()

        @classmethod
        async def connect(cls, dsn, *, allowed_roles):
            assert dsn == "postgresql://assets-worker@db.local/aap"
            assert allowed_roles == {"worker"}
            instance = cls()
            events.append("database-open")
            return instance

        async def close(self):
            events.append("database-close")

    class MediaStorage:
        def __init__(self, **kwargs):
            assert "inputs_bucket" not in kwargs
            self.config = kwargs
            self._client, self._signer = client, signer

    class BinarySourceStorage:
        def __init__(self, config, *, client, signer):
            self.config, self.client, self.signer = config, client, signer

        async def close(self):
            events.append("binary-close")

    class VideoStorage:
        def __init__(self, config, *, source_storage, client, signer):
            self.config = config
            self.source_storage = source_storage
            self.client, self.signer = client, signer

        async def close(self):
            events.append("video-storage-close")

    class VideoDatabase:
        def __init__(self, pool, *, allowed_roles):
            assert pool is Database.pool
            assert allowed_roles == {"worker", "system"}
            self.pool = pool

    class TrackerDatabase:
        def __init__(self, pool, *, allowed_roles):
            assert pool is Database.pool
            assert allowed_roles == {"worker"}
            self.pool = pool

    class TrackerExecutionDatabase:
        def __init__(self, database):
            assert isinstance(database, TrackerDatabase)
            self.database = database

    class MediaExecutor:
        worker_id = uuid4()

        def __init__(self, actual_db, actual_storage, **_values):
            assert isinstance(actual_db, ClaimTrackingDatabase)
            assert isinstance(actual_db.database, Database)
            assert isinstance(actual_storage, MediaStorage)

        async def close(self):
            events.append("media-executor-close")

    class VideoExecutor:
        def __init__(self, actual_db, actual_storage, *, worker_id):
            assert isinstance(actual_db, ClaimTrackingDatabase)
            assert isinstance(
                actual_db.database, (VideoDatabase, TrackerExecutionDatabase)
            )
            assert isinstance(actual_storage, VideoStorage)
            assert worker_id == MediaExecutor.worker_id

    async def capabilities(pool, role):
        assert pool is Database.pool and role == "worker"
        return True

    monkeypatch.setattr(process, "check_capabilities", capabilities)
    monkeypatch.setattr(process, "AsyncpgMediaDatabase", Database)
    monkeypatch.setattr(process, "S3MediaStorage", MediaStorage)
    monkeypatch.setattr(process, "S3ImageStorage", BinarySourceStorage)
    monkeypatch.setattr(process, "S3VideoInputStorage", VideoStorage)
    monkeypatch.setattr(process, "AsyncpgVideoInputDatabase", VideoDatabase)
    monkeypatch.setattr(process, "AsyncpgTrackerVideoInputDatabase", TrackerDatabase)
    monkeypatch.setattr(
        process, "TrackerVideoInputExecutorDatabase", TrackerExecutionDatabase
    )
    monkeypatch.setattr(worker_module, "MediaExecutor", MediaExecutor)
    monkeypatch.setattr(process, "VideoInputExecutor", VideoExecutor)

    runtime = await create_worker_runtime(
        MediaWorkerConfig(
            database_url="postgresql://assets-worker@db.local/aap",
            storage=_media_storage_config(),
            execution=MediaExecutionSettings(),
        )
    )
    assert runtime.video_executor is not None
    assert runtime.tracker_executor is not None
    assert runtime.tracker_database is not None
    await runtime.close()
    await runtime.close()

    assert events == [
        "database-open",
        "media-executor-close",
        "video-storage-close",
        "binary-close",
        "client-close",
        "signer-close",
        "database-close",
    ]


@pytest.mark.asyncio
async def test_worker_rejects_unqualified_sql_role_before_creating_storage(monkeypatch):
    from aap_assets_media_runtime import process

    closed = []

    class Database:
        pool = object()

        @classmethod
        async def connect(cls, *_args, **_kwargs):
            return cls()

        async def close(self):
            closed.append(True)

    async def capabilities(_pool, role):
        assert role == "worker"
        return False

    def storage(**_kwargs):
        pytest.fail("storage must not be constructed before SQL role qualification")

    monkeypatch.setattr(process, "AsyncpgMediaDatabase", Database)
    monkeypatch.setattr(process, "check_capabilities", capabilities)
    monkeypatch.setattr(process, "S3MediaStorage", storage)
    with pytest.raises(RuntimeError, match="capability qualification failed"):
        await create_worker_runtime(
            MediaWorkerConfig(
                database_url="postgresql://worker@db.local/aap",
                storage=_media_storage_config(),
                execution=MediaExecutionSettings(),
            )
        )
    assert closed == [True]


class _MediaLane:
    def __init__(self, results=None, *, block=None, events=None):
        self.results = list(results or [])
        self.calls = []
        self.block = block
        self.events = events

    async def execute_once(self, **selectors):
        self.calls.append(selectors)
        if self.events is not None:
            self.events.append("media")
        if self.block is not None:
            self.block.set()
            await asyncio.Event().wait()
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return None


class _VideoLane:
    def __init__(self, results=None, *, events=None):
        self.input_results = list((results or {}).get("input", []))
        self.frame_results = list((results or {}).get("frame", []))
        self.calls = []
        self.events = events

    async def execute_once(self, **selectors):
        self.calls.append(("input", selectors))
        if self.events is not None:
            self.events.append("video_input")
        if self.input_results:
            result = self.input_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return None

    async def execute_frame_once(self, **selectors):
        self.calls.append(("frame", selectors))
        if self.events is not None:
            self.events.append("video_frame")
        if self.frame_results:
            result = self.frame_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return None


class _TrackerLane:
    def __init__(self, results=None, *, events=None):
        self.results = list(results or [])
        self.calls = []
        self.events = events

    async def execute_once(self, **selectors):
        self.calls.append(selectors)
        if self.events is not None:
            self.events.append("tracker_input")
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return None


@pytest.mark.asyncio
async def test_polling_does_not_claim_another_unit_after_lost_claim_response():
    class Database:
        async def call(self, operation, _request):
            assert operation == "claim_video_media"
            return SimpleNamespace(claim=object())

    class Executor:
        def __init__(self, database):
            self.database = database

        async def execute_once(self, **_selectors):
            await self.database.call("claim_video_media", None)
            # A confirmed claim can finish with a lease-loss `None` result.
            return None

    tracking = ClaimTrackingDatabase(Database())
    tracked = ClaimTrackedExecutor(Executor(tracking), tracking)
    video = _VideoLane({"input": [object()]})
    attempt = await FairWorkerPoller(tracked, video).execute_once()

    assert attempt is not None and attempt.claimed is True and attempt.value is None
    assert video.calls == []


@pytest.mark.asyncio
async def test_unfiltered_polling_rotates_media_original_and_frame_lanes():
    events = []
    media = _MediaLane([object() for _ in range(3)], events=events)
    video = _VideoLane(
        {"input": [object() for _ in range(3)], "frame": [object() for _ in range(3)]},
        events=events,
    )
    poller = FairWorkerPoller(media, video)

    for _ in range(6):
        assert await poller.execute_once() is not None

    assert events == ["media", "video_input", "video_frame"] * 2
    assert len(media.calls) == 2
    assert [kind for kind, _ in video.calls] == [
        "input",
        "frame",
        "input",
        "frame",
    ]


@pytest.mark.asyncio
async def test_unfiltered_polling_includes_tracker_source_lane_fairly():
    events = []
    media = _MediaLane([object()], events=events)
    video = _VideoLane({"input": [object()], "frame": [object()]}, events=events)
    tracker = _TrackerLane([object()], events=events)
    poller = FairWorkerPoller(media, video, tracker)

    for _ in range(4):
        assert await poller.execute_once() is not None

    assert events == ["media", "video_input", "video_frame", "tracker_input"]


@pytest.mark.asyncio
async def test_polling_rotates_after_an_executor_error_and_targets_one_selector():
    media = _MediaLane([RuntimeError("media lane failure"), object()])
    video = _VideoLane({"input": [object()]})
    poller = FairWorkerPoller(media, video)
    with pytest.raises(RuntimeError, match="media lane failure"):
        await poller.execute_once()
    assert await poller.execute_once() is not None
    assert video.calls == [("input", {})]

    job_id, input_id, frame_id = uuid4(), uuid4(), uuid4()
    selected = FairWorkerPoller(
        _MediaLane([object()]),
        _VideoLane({"input": [object()], "frame": [object()]}),
        _TrackerLane([object()]),
    )
    await selected.execute_once(job_id=job_id)
    await selected.execute_once(input_id=input_id)
    await selected.execute_once(frame_id=frame_id)
    tracker_input_id = uuid4()
    await selected.execute_once(tracker_input_id=tracker_input_id)
    assert selected.media_executor.calls == [{"job_id": job_id}]
    assert selected.video_executor.calls == [
        ("input", {"input_id": input_id}),
        ("frame", {"frame_id": frame_id}),
    ]
    assert selected.tracker_executor.calls == [{"input_id": tracker_input_id}]
    with pytest.raises(ValueError, match="mutually exclusive"):
        await selected.execute_once(job_id=job_id, frame_id=frame_id)


@pytest.mark.asyncio
async def test_once_stops_after_at_most_one_claim_and_routes_selectors(monkeypatch):
    calls, close_count = [], []
    config = SimpleNamespace(poll_seconds=0.01)

    class Runtime:
        def __init__(self):
            self.media_executor = _MediaLane([object()])
            self.video_executor = _VideoLane({"input": [object()], "frame": [object()]})
            self.tracker_executor = _TrackerLane([object()])

        async def maintenance_once(self):
            calls.append("maintenance")

        async def close(self):
            close_count.append("closed")

    runtime = Runtime()
    monkeypatch.setattr(worker, "load_config", lambda: config)
    monkeypatch.setattr(
        worker, "create_worker_runtime", lambda _config: _return(runtime)
    )

    await worker._run_worker(once=True)
    assert runtime.media_executor.calls == [{}]
    assert runtime.video_executor.calls == []
    assert calls == [] and close_count == ["closed"]

    input_id, frame_id = uuid4(), uuid4()
    runtime.video_executor.input_results = [object()]
    await worker._run_worker(once=True, input_id=input_id)
    assert runtime.video_executor.calls == [("input", {"input_id": input_id})]
    runtime.video_executor.calls.clear()
    runtime.video_executor.frame_results = [object()]
    await worker._run_worker(once=True, frame_id=frame_id)
    assert runtime.video_executor.calls == [("frame", {"frame_id": frame_id})]
    tracker_input_id = uuid4()
    await worker._run_worker(once=True, tracker_input_id=tracker_input_id)
    assert runtime.tracker_executor.calls == [{"input_id": tracker_input_id}]
    assert close_count == ["closed", "closed", "closed", "closed"]


def test_iteration_failure_log_uses_only_known_video_input_error_codes(caplog):
    with caplog.at_level(logging.ERROR, logger=worker._LOGGER.name):
        worker._log_iteration_failure(video_contract.VideoInputFailure("frame_busy"))

    assert "VideoInputFailure, code=frame_busy" in caplog.text
    caplog.clear()
    secret = "https://user:private-token@example.test/object"
    with caplog.at_level(logging.ERROR, logger=worker._LOGGER.name):
        worker._log_iteration_failure(video_contract.VideoInputFailure(secret))

    assert "VideoInputFailure" in caplog.text
    assert secret not in caplog.text


async def _return(value):
    return value


@pytest.mark.asyncio
async def test_worker_closes_runtime_after_error_and_cancellation(monkeypatch):
    config = SimpleNamespace(poll_seconds=0.001)
    closed = []

    class Runtime:
        def __init__(self, media):
            self.media_executor = media
            self.video_executor = _VideoLane()

        async def maintenance_once(self):
            return None

        async def close(self):
            closed.append("closed")

    monkeypatch.setattr(worker, "load_config", lambda: config)
    error_runtime = Runtime(_MediaLane([RuntimeError("poll failed")]))
    monkeypatch.setattr(
        worker, "create_worker_runtime", lambda _config: _return(error_runtime)
    )
    with pytest.raises(RuntimeError, match="poll failed"):
        await worker._run_worker(once=True)
    assert closed == ["closed"]

    started = asyncio.Event()
    cancel_runtime = Runtime(_MediaLane(block=started))
    monkeypatch.setattr(
        worker, "create_worker_runtime", lambda _config: _return(cancel_runtime)
    )
    task = asyncio.create_task(worker._run_worker(once=False))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == ["closed", "closed"]


@pytest.mark.asyncio
async def test_video_maintenance_sweeps_and_collects_bounded_pages():
    calls = []

    class VideoDatabase:
        def __init__(self):
            self.sweeps = iter([100, 7])

        async def call(self, operation, request):
            calls.append((operation, request))
            return SimpleNamespace(expired=next(self.sweeps))

    class TrackerDatabase:
        def __init__(self):
            self.sweeps = iter([100, 3])

        async def call(self, operation, request):
            calls.append((operation, request))
            return SimpleNamespace(expired=next(self.sweeps))

    runtime = WorkerRuntime(
        database=object(),
        media_executor=object(),
        video_executor=object(),
        video_database=VideoDatabase(),
        _resources=AsyncExitStack(),
        tracker_database=TrackerDatabase(),
    )
    await runtime.maintenance_once()
    assert [operation for operation, _ in calls] == [
        "sweep_accepted_video_inputs",
        "sweep_accepted_video_inputs",
        "sweep_video_tracker_inputs",
        "sweep_video_tracker_inputs",
    ]
    assert all(request.limit == 100 for _operation, request in calls)


@pytest.mark.asyncio
async def test_tracker_retention_sweep_runs_when_accepted_video_sweep_fails(caplog):
    calls = []

    class VideoDatabase:
        async def call(self, operation, _request):
            calls.append(operation)
            raise RuntimeError("video sweep unavailable")

    class TrackerDatabase:
        async def call(self, operation, _request):
            calls.append(operation)
            return SimpleNamespace(expired=0)

    runtime = WorkerRuntime(
        database=object(),
        media_executor=object(),
        video_executor=object(),
        video_database=VideoDatabase(),
        _resources=AsyncExitStack(),
        tracker_database=TrackerDatabase(),
    )
    await runtime.maintenance_once()
    assert calls == [
        "sweep_accepted_video_inputs",
        "sweep_video_tracker_inputs",
    ]
    assert "Accepted video retention sweep failed" in caplog.text


def test_cli_rejects_multiple_work_selectors(monkeypatch):
    job_id, frame_id = uuid4(), uuid4()
    monkeypatch.setattr(
        sys,
        "argv",
        ["assets-media-worker", "--job-id", str(job_id), "--frame-id", str(frame_id)],
    )
    with pytest.raises(SystemExit) as raised:
        worker.main()
    assert raised.value.code == 2


def test_cli_keeps_tracker_input_selector_separate_from_batch_input(monkeypatch):
    input_id, tracker_input_id = uuid4(), uuid4()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "assets-media-worker",
            "--input-id",
            str(input_id),
            "--tracker-input-id",
            str(tracker_input_id),
        ],
    )
    with pytest.raises(SystemExit) as raised:
        worker.main()
    assert raised.value.code == 2
