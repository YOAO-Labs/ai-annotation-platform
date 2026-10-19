import asyncio
import hashlib
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aap_assets_images.contract import ImageSource, ObjectIdentity as TransportIdentity
from aap_assets_inputs.contract import InputFailure
from aap_assets_video_inputs import execution as c
from aap_assets_video_inputs import values as v
from aap_assets_video_inputs_runtime import worker as w
from aap_assets_video_inputs_runtime.storage import VideoDownload, VideoSourceStorage
from video_input_fixtures import (
    FRAME_ID,
    FRAME_LEASE,
    LEASE,
    WORKER,
    record,
    write_video,
)


class Database:
    def __init__(self, input_, source, *, transient=None):
        self.input, self.source = input_, source
        self.transient = transient
        self.calls, self.pages, self.finishes = [], {}, []
        self.frame = None
        self.failed = []

    async def call(self, operation, request):
        self.calls.append(operation)
        kind = "frame" if operation.endswith("_frame") else "input"
        if operation == "claim_accepted_video_input":
            self.input = self.input.model_copy(update={"status": "materializing"})
            return c.ClaimedVideoInput(
                claim=c.VideoInputClaim(
                    input=self.input,
                    source=self.source,
                    generation=1,
                    lease_token=LEASE,
                    lease_expires_at="2026-10-19T01:00:00Z",
                    blob_key=c.attempt_prefix(self.input.input_id, 1, LEASE) + "source",
                )
            )
        if operation == "claim_accepted_video_frame":
            self.frame = self.frame.model_copy(update={"status": "materializing"})
            return c.ClaimedVideoFrame(
                claim=c.VideoFrameClaim(
                    frame=self.frame,
                    generation=2,
                    lease_token=FRAME_LEASE,
                    lease_expires_at="2026-10-19T01:00:00Z",
                    blob_key=c.attempt_prefix(self.input.input_id, 2, FRAME_LEASE)
                    + "frames/2/w32-"
                    + self.input.sampling.format,
                )
            )
        if operation.startswith("heartbeat_"):
            model = c.VideoInputLease if kind == "input" else c.VideoFrameLease
            return model(
                **request.model_dump(exclude={"lease_seconds"}),
                lease_expires_at="2026-10-19T01:00:00Z",
            )
        if operation == "stage_accepted_video_times":
            if request.cursor in self.pages:
                assert self.pages[request.cursor] == request.entries
            self.pages[request.cursor] = request.entries
            if self.transient == "lost_stage":
                self.transient = None
                raise c.VideoInputFailure("storage_unavailable")
            if self.transient == "lease_lost":
                raise c.VideoInputFailure("lease_lost")
            return c.StagedVideoTimes(
                **request.model_dump(exclude={"cursor", "entries"}),
                next_cursor=request.cursor + len(request.entries),
            )
        if operation.startswith("finish_"):
            self.finishes.append(request)
            if self.transient == "busy_finish":
                self.transient = None
                raise c.VideoInputFailure(
                    "input_busy" if kind == "input" else "frame_busy"
                )
            if kind == "input":
                self.input = c.VideoInputRecord(
                    **{
                        **self.input.model_dump(),
                        "status": "ready",
                        "plan": request.plan,
                    }
                )
                result = self.input
            else:
                self.frame = c.VideoFrameRecord(
                    **{
                        **self.frame.model_dump(),
                        "status": "ready",
                        "blob": request.blob,
                    }
                )
                result = self.frame
            if self.transient == "lost_finish":
                self.transient = None
                raise c.VideoInputFailure("storage_unavailable")
            return result
        if operation.startswith("fail_"):
            self.failed.append(request.error_code)
            if kind == "input":
                self.input = c.VideoInputRecord(
                    **{
                        **self.input.model_dump(),
                        "status": "failed",
                        "error": request.error_code,
                        "plan": None,
                    }
                )
                return self.input
            self.frame = c.VideoFrameRecord(
                **{
                    **self.frame.model_dump(),
                    "status": "failed",
                    "error": request.error_code,
                    "blob": None,
                }
            )
            return self.frame
        raise AssertionError(operation)

    def require_frame(self):
        self.frame = c.VideoFrameRecord(
            frame_id=FRAME_ID,
            plan=self.input.plan,
            entry=self.pages[0][1],
            status="pending",
            blob=None,
            error=None,
            created_at=self.input.created_at,
        )


class Storage:
    def __init__(self, source_bytes, identity):
        self.source_bytes, self.identity = source_bytes, identity
        self.blobs = {}
        self.downloads, self.uploads, self.original_reads = 0, 0, 0
        self.source_changed = False
        self.source_forbidden = False

    async def assert_versioning(self):
        pass

    async def download_source(self, source, destination):
        assert not self.source_forbidden, (
            "frame extraction must never resolve the current DatasetItem"
        )
        self.original_reads += 1
        destination.write_bytes(self.source_bytes)
        return VideoDownload(
            self.identity,
            len(self.source_bytes),
            hashlib.sha256(self.source_bytes).hexdigest(),
        )

    async def head_source(self, source):
        assert not self.source_forbidden
        return (
            self.identity.model_copy(update={"etag": "replacement"})
            if self.source_changed
            else self.identity
        )

    async def upload_blob(self, path, key, *, sha256, byte_size):
        data = path.read_bytes()
        assert len(data) == byte_size and hashlib.sha256(data).hexdigest() == sha256
        self.uploads += 1
        assert not any(existing[0] == key for existing in self.blobs), (
            "retry uploaded another version"
        )
        model = v.VideoBlob if key.endswith("/source") else v.FrameBlob
        blob = model(
            key=key,
            version_id=f"version-{self.uploads}",
            etag=sha256[:32],
            byte_size=byte_size,
            sha256=sha256,
        )
        self.blobs[(key, blob.version_id)] = data
        return blob

    async def download_blob(self, blob, destination):
        self.downloads += 1
        data = self.blobs.get((blob.key, blob.version_id))
        if data is None or hashlib.sha256(data).hexdigest() != blob.sha256:
            raise InputFailure("input_corrupt")
        destination.write_bytes(data)


def setup(tmp_path, *, transient=None, format_="jpeg"):
    video = write_video(tmp_path / "origin.mp4").read_bytes()
    raw = record().model_dump(mode="json")
    raw["source"]["object_identity"]["byte_size"] = len(video)
    raw["sampling"] = {"max_frames": 3, "step": 2, "width": 32, "format": format_}
    input_ = c.VideoInputRecord.model_validate(raw)
    source = c.VideoCaptureSource(
        **input_.source.model_dump(),
        source_drifted=False,
        file_path="dataset/original.mp4",
        file_type="video",
        file_size=len(video),
        content_hash=None,
    )
    db = Database(input_, source, transient=transient)
    storage = Storage(video, input_.source.object_identity)
    executor = w.VideoInputExecutor(
        db, storage, worker_id=WORKER, temp_root=tmp_path / "work"
    )
    return db, storage, executor


@pytest.mark.parametrize(
    "transient", [None, "lost_stage", "busy_finish", "lost_finish"]
)
async def test_video_preparation_replays_pages_and_publication_without_recopies(
    tmp_path, transient
):
    db, storage, executor = setup(tmp_path, transient=transient)
    ready = await executor.execute_once(input_id=db.input.input_id)
    assert ready.status == "ready" and ready.plan.input.video.frame_count == 8
    assert [(e.frame_index, e.pts_ms) for e in db.pages[0]] == [
        (0, 0),
        (2, 200),
        (4, 400),
    ]
    assert storage.original_reads == storage.uploads == 1
    assert not db.failed and not list((tmp_path / "work").iterdir())
    assert all(finish == db.finishes[0] for finish in db.finishes)
    if transient == "lost_stage":
        assert db.calls.count("stage_accepted_video_times") == 2
    if transient in {"busy_finish", "lost_finish"}:
        assert len(db.finishes) == 2


@pytest.mark.parametrize("format_", ["jpeg", "webp"])
async def test_frame_extraction_reads_the_original_retained_version_after_source_replacement(
    tmp_path, format_
):
    db, storage, executor = setup(tmp_path, format_=format_)
    await executor.execute_once(input_id=db.input.input_id)
    original_blob = db.input.plan.input.blob
    db.require_frame()
    storage.source_bytes, storage.source_forbidden = b"replacement is not a video", True
    # A new latest version at the same key cannot retarget extraction.
    storage.blobs[(original_blob.key, "latest-replacement")] = b"new bytes"
    db.transient = "lost_finish"
    ready = await executor.execute_frame_once(frame_id=FRAME_ID)
    assert (
        ready.status == "ready"
        and ready.entry.frame_index == 2
        and ready.entry.pts_ms == 200
    )
    assert ready.plan.input.blob == original_blob
    assert storage.original_reads == storage.downloads == 1 and storage.uploads == 2
    assert ready.blob.key.endswith("frames/2/w32-" + format_)
    assert ready.blob.version_id == "version-2"
    assert not db.failed and not list((tmp_path / "work").iterdir())


async def test_missing_retained_version_records_fixed_failure_and_never_reads_current_source(
    tmp_path,
):
    db, storage, executor = setup(tmp_path)
    await executor.execute_once(input_id=db.input.input_id)
    db.require_frame()
    storage.blobs.clear()
    storage.source_forbidden = True
    failed = await executor.execute_frame_once(frame_id=FRAME_ID)
    assert failed.status == "failed" and failed.error == "input_corrupt"
    assert storage.original_reads == storage.downloads == storage.uploads == 1
    assert db.failed == ["input_corrupt"]


async def test_current_origin_drift_never_publishes_a_ready_video_plan(tmp_path):
    db, storage, executor = setup(tmp_path)
    storage.source_changed = True
    failed = await executor.execute_once(input_id=db.input.input_id)
    assert failed.status == "failed" and failed.error == "source_changed"
    assert "finish_accepted_video_input" not in db.calls
    assert db.failed == ["source_changed"]


async def test_selected_pts_failure_is_durable_and_does_not_publish_a_partial_plan(
    tmp_path, monkeypatch
):
    db, storage, executor = setup(tmp_path)
    monkeypatch.setattr(
        w,
        "probe_retained_video",
        AsyncMock(side_effect=c.VideoInputFailure("timebase_incomplete")),
    )
    failed = await executor.execute_once(input_id=db.input.input_id)
    assert failed.status == "failed" and failed.error == "timebase_incomplete"
    assert not db.pages and "finish_accepted_video_input" not in db.calls


async def test_lost_lease_never_marks_a_new_owner_failed_or_publishes(tmp_path):
    db, storage, executor = setup(tmp_path, transient="lease_lost")
    assert await executor.execute_once(input_id=db.input.input_id) is None
    assert not db.failed and "finish_accepted_video_input" not in db.calls


async def test_cancellation_preserves_lease_takeover_and_cleans_local_files(tmp_path):
    db, storage, executor = setup(tmp_path)
    entered = asyncio.Event()

    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()

    storage.download_source = blocked
    task = asyncio.create_task(executor.execute_once(input_id=db.input.input_id))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not db.failed and not db.finishes
    assert not list((tmp_path / "work").iterdir())


async def test_resource_budget_rejects_before_source_or_blob_io(tmp_path):
    db, storage, executor = setup(tmp_path)
    executor.policy = w.VideoExecutionPolicy(max_source_bytes=1, max_timebase_bytes=1)
    failed = await executor.execute_once(input_id=db.input.input_id)
    assert failed.error == "resource_limit"
    assert storage.original_reads == storage.uploads == storage.downloads == 0


async def test_video_source_bridge_uses_binary_facts_without_fabricating_image_domain_owner():
    input_ = record()
    source = c.VideoCaptureSource(
        **input_.source.model_dump(),
        source_drifted=False,
        file_path="dataset/original.mp4",
        file_type="video",
        file_size=4096,
        content_hash=None,
    )
    transport_identity = TransportIdentity(**input_.source.object_identity.model_dump())
    binary = SimpleNamespace(
        head_source=AsyncMock(return_value=transport_identity),
        download_source=AsyncMock(
            return_value=SimpleNamespace(
                identity=transport_identity, byte_size=4096, sha256="c" * 64
            )
        ),
    )
    bridge = VideoSourceStorage(binary)
    assert await bridge.head_source(source) == input_.source.object_identity
    downloaded = await bridge.download_source(source, Path("destination"))
    assert (
        downloaded.identity == input_.source.object_identity
        and downloaded.sha256 == "c" * 64
    )
    forwarded = binary.download_source.await_args.args[0]
    assert not isinstance(forwarded, ImageSource) and not hasattr(forwarded, "owner")
    assert (
        forwarded.bucket_kind == "datasets"
        and type(forwarded.object_identity) is TransportIdentity
    )


async def test_frame_worker_rejects_storage_receipt_for_a_different_frame_key(tmp_path):
    db, storage, executor = setup(tmp_path)
    await executor.execute_once(input_id=db.input.input_id)
    db.require_frame()
    upload = storage.upload_blob

    async def misrouted(*args, **kwargs):
        blob = await upload(*args, **kwargs)
        return blob.model_copy(
            update={"key": blob.key.replace("frames/2/", "frames/4/")}
        )

    storage.upload_blob = misrouted
    failed = await executor.execute_frame_once(frame_id=FRAME_ID)
    assert failed.error == "input_corrupt"
    assert "finish_accepted_video_frame" not in db.calls


@pytest.mark.parametrize("code", ["lease_lost", "invalid_response"])
async def test_initial_lease_loss_or_unknown_heartbeat_stops_before_io(tmp_path, code):
    db, storage, executor = setup(tmp_path)
    original = db.call

    async def disconnected(operation, request):
        if operation == "heartbeat_accepted_video_input":
            raise c.VideoInputFailure(code)
        return await original(operation, request)

    db.call = disconnected
    assert await executor.execute_once(input_id=db.input.input_id) is None
    assert not db.failed and storage.original_reads == storage.uploads == 0


async def test_initial_heartbeat_retries_transient_before_starting_source_io(tmp_path):
    db, storage, executor = setup(tmp_path)
    original_call = db.call
    original_download = storage.download_source
    heartbeat_requests = []
    events = []

    async def busy_then_confirmed(operation, request):
        if operation == "heartbeat_accepted_video_input":
            heartbeat_requests.append(request)
            events.append("heartbeat")
            if len(heartbeat_requests) <= 2:
                raise c.VideoInputFailure("input_busy")
            result = await original_call(operation, request)
            events.append("confirmed")
            return result
        return await original_call(operation, request)

    async def tracked_download(source, destination):
        events.append("source_io")
        return await original_download(source, destination)

    db.call = busy_then_confirmed
    storage.download_source = tracked_download

    ready = await executor.execute_once(input_id=db.input.input_id)

    assert ready.status == "ready"
    assert [event for event in events if event == "heartbeat"] == [
        "heartbeat",
        "heartbeat",
        "heartbeat",
    ]
    assert events.index("confirmed") < events.index("source_io")
    assert all(
        (request.input_id, request.generation, request.lease_token)
        == (
            db.input.input_id,
            heartbeat_requests[0].generation,
            heartbeat_requests[0].lease_token,
        )
        for request in heartbeat_requests
    )
    assert storage.original_reads == storage.uploads == 1
    assert not db.failed


async def test_periodic_heartbeat_retries_transient_with_same_claim(tmp_path):
    db, _, executor = setup(tmp_path)
    await executor.execute_once(input_id=db.input.input_id)
    db.require_frame()
    claim = (
        await db.call(
            "claim_accepted_video_frame",
            c.ClaimVideoFrameRequest(
                worker_id=WORKER,
                frame_id=FRAME_ID,
                lease_seconds=executor.policy.lease_seconds,
            ),
        )
    ).claim
    original_call = db.call
    heartbeat_requests = []
    third_heartbeat = asyncio.Event()

    async def busy_once(operation, request):
        if operation == "heartbeat_accepted_video_frame":
            heartbeat_requests.append(request)
            if len(heartbeat_requests) == 2:
                raise c.VideoInputFailure("frame_busy")
            if len(heartbeat_requests) >= 3:
                third_heartbeat.set()
        return await original_call(operation, request)

    db.call = busy_once
    lease = w._Lease(db, claim, executor.policy)
    lease.interval = 0.01
    await lease.start()
    try:
        await asyncio.wait_for(third_heartbeat.wait(), timeout=2)
        assert len(heartbeat_requests) == 3
        assert all(
            (request.frame_id, request.generation, request.lease_token)
            == (claim.frame.frame_id, claim.generation, claim.lease_token)
            for request in heartbeat_requests
        )
        assert lease.error is None and not lease.lost.is_set()
    finally:
        await lease.close()


async def test_initial_heartbeat_contention_stops_at_execution_deadline(tmp_path):
    db, storage, executor = setup(tmp_path)
    executor.policy = w.VideoExecutionPolicy(job_timeout_seconds=1, lease_seconds=62)
    original_call = db.call
    heartbeat_requests = []

    async def continuously_busy(operation, request):
        if operation == "heartbeat_accepted_video_input":
            heartbeat_requests.append(request)
            raise c.VideoInputFailure("frame_busy")
        return await original_call(operation, request)

    db.call = continuously_busy
    started = time.monotonic()

    failed = await executor.execute_once(input_id=db.input.input_id)

    elapsed = time.monotonic() - started
    assert failed.status == "failed" and failed.error == "timeout"
    assert len(heartbeat_requests) >= 2
    assert all(
        (request.input_id, request.generation, request.lease_token)
        == (
            db.input.input_id,
            heartbeat_requests[0].generation,
            heartbeat_requests[0].lease_token,
        )
        for request in heartbeat_requests
    )
    assert elapsed < 1.5
    assert storage.original_reads == storage.uploads == 0
    assert db.failed == ["timeout"]


async def test_periodic_heartbeat_contention_cannot_extend_execution_deadline(tmp_path):
    db, _, _ = setup(tmp_path)
    policy = w.VideoExecutionPolicy(job_timeout_seconds=1, lease_seconds=62)
    claim = (
        await db.call(
            "claim_accepted_video_input",
            c.ClaimVideoInputRequest(
                worker_id=WORKER,
                input_id=db.input.input_id,
                lease_seconds=policy.lease_seconds,
            ),
        )
    ).claim
    original_call = db.call
    heartbeat_count = 0

    async def busy_after_initial(operation, request):
        nonlocal heartbeat_count
        if operation == "heartbeat_accepted_video_input":
            heartbeat_count += 1
            if heartbeat_count > 1:
                raise c.VideoInputFailure("frame_busy")
        return await original_call(operation, request)

    db.call = busy_after_initial
    lease = w._Lease(db, claim, policy)
    lease.interval = 0.01
    await lease.start()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_operation():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    started = time.monotonic()
    try:
        with pytest.raises(c.VideoInputFailure, match="timeout"):
            await lease.call(blocked_operation)
        assert entered.is_set() and cancelled.is_set()
        assert lease.error is not None and lease.error.code == "timeout"
        assert heartbeat_count >= 2
        assert time.monotonic() - started < 1.5
    finally:
        await lease.close()
