"""Stale workers stop before touching the replacement's Labeling state."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aap_preannotation_results.video_execution import (
    ExecutionClaim,
    VideoExecutionFailure,
)
from app.services.video_tracking import execution, runner, tracker_mask_roots

pytestmark = pytest.mark.asyncio


def _writer_rejection_port(monkeypatch, job_id, codes):
    from aap_preannotation_results.video_execution import ExecutionProof
    from aap_labeling.video_writer_contract import LABELING_VIDEO_WRITER_DATA_SCHEMA
    from app.services.video_tracking import writer

    proof = ExecutionProof(
        job_id=job_id,
        run_id=uuid4(),
        worker_id=uuid4(),
        generation=1,
        lease_token=uuid4(),
    )
    monkeypatch.setattr(writer, "execution_proof", lambda _job_id: proof)
    monkeypatch.setattr(writer.settings, "labeling_video_writer_url", "")
    monkeypatch.setattr(writer.settings, "labeling_video_writer_database_url", "")
    monkeypatch.setattr(writer.settings, "labeling_video_writer_database_url_file", "")
    replies = iter(codes)
    instances = []

    class RejectionData:
        def __init__(self, _db):
            self.calls = []
            instances.append(self)

        @asynccontextmanager
        async def transaction(self):
            yield self

        async def call(self, action, arguments):
            self.calls.append((action, arguments))
            return {
                "schema": LABELING_VIDEO_WRITER_DATA_SCHEMA,
                "ok": False,
                "error": {"code": next(replies)},
            }

    monkeypatch.setattr(writer, "SqlAlchemyVideoWriterData", RejectionData)
    monkeypatch.setattr(writer.asyncio, "sleep", AsyncMock())
    return proof, instances


async def test_exhausted_route_contention_defers_the_video_execution():
    from app.services.ml_routing.authority import RoutingRejected
    from app.services.ml_routing.router import MLBackendRouter
    from app.services.ml_routing.contracts import RejectionReason

    authority = SimpleNamespace(
        acquire=AsyncMock(side_effect=RoutingRejected("management_busy"))
    )
    router = MLBackendRouter(SimpleNamespace(), authority=authority)
    result = await router.acquire(
        uuid4(), owner="tracker:private", operation="tracker", execution={}
    )
    assert result.lease is None
    assert result.rejection == RejectionReason.ROUTER_UNAVAILABLE


async def test_stale_failure_is_rejected_before_orm_projection(monkeypatch):
    from app.services.video_tracking import writer

    job_id = uuid4()
    replacement = SimpleNamespace(
        id=job_id,
        status="running",
        staged_result={"replacement": True},
        error_message=None,
        completed_at=None,
    )
    db = SimpleNamespace(
        rollback=AsyncMock(),
        commit=AsyncMock(),
        get=AsyncMock(return_value=replacement),
    )
    proof, instances = _writer_rejection_port(monkeypatch, job_id, ["lease_lost"])
    with pytest.raises(execution.TrackerExecutionLost):
        await writer.commit_tracker_outcome(
            db, job_id, "failed", error_message="old worker crashed"
        )
    assert replacement.status == "running"
    assert replacement.staged_result == {"replacement": True}
    assert replacement.error_message is None
    db.commit.assert_awaited_once()
    db.rollback.assert_awaited_once()
    db.get.assert_not_awaited()
    assert [action for action, _ in instances[0].calls] == ["prepare"]
    assert instances[0].calls[0][0] == "prepare"
    assert instances[0].calls[0][1].execution == proof


async def test_stale_candidate_write_is_rejected_by_writer_authority(monkeypatch):
    from app.services.video_tracking import writer

    job_id = uuid4()
    job = SimpleNamespace(
        id=job_id, task_id=uuid4(), staged_result={"replacement": True}
    )
    db = SimpleNamespace(
        rollback=AsyncMock(), commit=AsyncMock(), get=AsyncMock(return_value=job)
    )
    proof, instances = _writer_rejection_port(monkeypatch, job_id, ["lease_lost"])
    with pytest.raises(execution.TrackerExecutionLost):
        await writer.commit_tracker_outcome(
            db,
            job_id,
            "completed",
            status=SimpleNamespace(
                input_id=uuid4(),
                request_digest="a" * 64,
                plan_digest="b" * 64,
            ),
            source_version=SimpleNamespace(
                version_id="test-source-version", sha256="c" * 64
            ),
        )
    assert job.staged_result == {"replacement": True}
    db.commit.assert_awaited_once()
    db.rollback.assert_awaited_once()
    db.get.assert_not_awaited()
    assert [action for action, _ in instances[0].calls] == ["prepare"]
    assert instances[0].calls[0][0] == "prepare"
    assert instances[0].calls[0][1].execution == proof


async def test_stale_mask_root_never_reaches_gc_registration(monkeypatch):
    register = AsyncMock()
    monkeypatch.setattr(
        tracker_mask_roots, "register_tracker_running_mask_root", register
    )
    monkeypatch.setattr(
        tracker_mask_roots,
        "fence_tracker_write",
        AsyncMock(side_effect=execution.TrackerExecutionLost()),
    )
    with pytest.raises(execution.TrackerExecutionLost):
        await tracker_mask_roots.retain_tracker_output_mask(
            object(),
            job_id=uuid4(),
            task_id=uuid4(),
            object_key="mask/private.json",
        )
    register.assert_not_awaited()


async def test_current_cancellation_is_observed_without_failing_the_job(monkeypatch):
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock(), execute=AsyncMock())
    monkeypatch.setattr(
        runner,
        "fence_tracker_write",
        AsyncMock(side_effect=execution.TrackerExecutionCancelled()),
    )
    assert (
        await runner._tracker_work_authorized_and_running(
            db, job_id=uuid4(), actor_id=uuid4()
        )
        is False
    )
    db.execute.assert_not_awaited()
    db.rollback.assert_awaited_once()


async def test_writer_contention_retries_the_same_command(monkeypatch):
    from app.services.video_tracking import writer

    job_id = uuid4()
    db = SimpleNamespace(rollback=AsyncMock(), commit=AsyncMock())
    _proof, instances = _writer_rejection_port(
        monkeypatch, job_id, ["input_busy", "input_busy", "lease_lost"]
    )
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())
    with pytest.raises(execution.TrackerExecutionLost):
        await writer.commit_tracker_outcome(
            db,
            job_id,
            "failed",
            error_message="held failure",
        )
    arguments = [args for action, args in instances[0].calls if action == "prepare"]
    assert len(arguments) == 3
    assert len({item.operation_id for item in arguments}) == 1
    assert len({item.request_digest for item in arguments}) == 1
    assert all(item.execution == _proof for item in arguments)
    assert db.commit.await_count == 1
    assert db.rollback.await_count == 3


async def test_repeated_execution_contention_retries_held_mask_without_inference_loss(
    monkeypatch,
):
    from app.services import raster_mask_storage as storage
    from app.services.video_tracking.adapters import TrackerFrameResult

    rle = {"encoding": "coco_rle", "size": [1, 1], "counts": [0, 1]}
    reference = storage.build_rle_reference(rle)
    store = AsyncMock(return_value=reference)
    register = AsyncMock(side_effect=[execution.TrackerExecutionBusy()] * 12 + [None])
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(runner, "store_generated_mask_content", store)
    monkeypatch.setattr(
        runner,
        "read_mask_write_epochs",
        AsyncMock(return_value={reference["object_key"]: 7}),
    )
    monkeypatch.setattr(runner, "lock_raster_mask_references", AsyncMock())
    monkeypatch.setattr(runner, "retain_tracker_output_mask", register)
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())
    result = await runner._materialize_tracker_mask_result(
        TrackerFrameResult(frame_index=4, geometry={"type": "mask", "rle": rle}),
        db=db,
        job_id=uuid4(),
        task_id=uuid4(),
        still_running=AsyncMock(return_value=True),
    )
    assert result.frame_index == 4
    assert result.geometry["mask"] == reference
    store.assert_awaited_once()
    assert register.await_count == 13
    assert db.rollback.await_count == 12
    db.commit.assert_awaited_once()


async def test_unknown_renewal_stops_subsequent_writes_without_exposing_capability(
    monkeypatch,
):
    job_id = uuid4()
    renewal_attempted = asyncio.Event()
    private_token = uuid4()

    async def native_call(_db, action, request):
        if action == "claim":
            return {
                "execution": ExecutionClaim(
                    job_id=request.job_id,
                    run_id=uuid4(),
                    worker_id=request.worker_id,
                    generation=1,
                    lease_token=private_token,
                    lease_expires_at=datetime.now(timezone.utc),
                    state="ready",
                )
            }
        if action == "renew":
            renewal_attempted.set()
            raise VideoExecutionFailure("storage_unavailable")
        raise AssertionError("a lost worker must not call the writer guard")

    db = SimpleNamespace(
        commit=AsyncMock(), rollback=AsyncMock(), no_autoflush=nullcontext()
    )

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(execution, "_call", native_call)
    monkeypatch.setattr(execution, "_RENEW_SECONDS", 0)
    async with execution.tracker_execution(db, job_id, session_factory=session):
        await asyncio.wait_for(renewal_attempted.wait(), 1)
        await asyncio.sleep(0)
        with pytest.raises(execution.TrackerExecutionLost) as failure:
            await execution.fence_tracker_write(db, job_id, "fail")
        assert str(private_token) not in str(failure.value)
    with pytest.raises(execution.TrackerExecutionLost):
        execution.execution_proof(job_id)


@pytest.mark.parametrize(
    "renewal_failure",
    ["storage_unavailable", "lease_lost", "transport", "canonical_read_outage"],
)
async def test_lost_renewal_keeps_active_tracker_delivery_retryable(
    monkeypatch, renewal_failure
):
    job_id = uuid4()
    job = SimpleNamespace(id=job_id, status="running", staged_result={"original": True})
    renewal_attempted = asyncio.Event()

    async def native_call(_db, action, request):
        if action == "claim":
            return {
                "execution": ExecutionClaim(
                    job_id=job_id,
                    run_id=uuid4(),
                    worker_id=request.worker_id,
                    generation=1,
                    lease_token=uuid4(),
                    lease_expires_at=datetime.now(timezone.utc),
                    state="ready",
                )
            }
        assert action == "renew"
        renewal_attempted.set()
        if renewal_failure == "transport":
            raise OSError("test: renewal reply unavailable")
        raise VideoExecutionFailure(
            "storage_unavailable"
            if renewal_failure == "canonical_read_outage"
            else renewal_failure
        )

    async def computation(db, job_id, **_kwargs):
        await asyncio.wait_for(renewal_attempted.wait(), 1)
        await asyncio.sleep(0)
        await execution.fence_tracker_write(db, job_id, "stage")

    db = SimpleNamespace(
        commit=AsyncMock(),
        rollback=AsyncMock(),
        get=AsyncMock(return_value=job),
    )
    if renewal_failure == "canonical_read_outage":
        db.get.side_effect = [job, OSError("test: canonical read unavailable")]

    @asynccontextmanager
    async def sessions():
        yield db

    monkeypatch.setattr(execution, "_call", native_call)
    monkeypatch.setattr(execution, "_RENEW_SECONDS", 0)
    monkeypatch.setattr(runner, "_run_tracker_job_claimed", computation)
    with pytest.raises(execution.TrackerExecutionDeferred):
        await runner.run_tracker_job(db, job_id, shadow_session_factory=sessions)
    assert job.status == "running"
    assert job.staged_result == {"original": True}


@pytest.mark.parametrize("kind", ["video_tracker", "video_correction"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_broker_signal_cannot_terminalize_claimed_tracker_summary(
    kind, cancelled
):
    from app.application.job_terminal import apply_signal_terminal

    summary = SimpleNamespace(
        kind=kind, payload={"video_execution_protocol": "aap-video-execution-data/v1"}
    )
    assert (
        await apply_signal_terminal(
            object(), summary, cancelled=cancelled, error="old process crashed"
        )
        is False
    )


async def test_running_replacement_is_never_projected_as_failed_or_complete(
    monkeypatch,
):
    from app.workers import video_tracker as worker

    result = SimpleNamespace(first=lambda: ("running", None))
    db = SimpleNamespace(
        rollback=AsyncMock(), execute=AsyncMock(return_value=result), commit=AsyncMock()
    )
    failed, completed, cancelled, notify = (
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
    )
    monkeypatch.setattr(worker.async_job_svc, "mark_failed", failed)
    monkeypatch.setattr(worker.async_job_svc, "mark_complete", completed)
    monkeypatch.setattr(worker.async_job_svc, "mark_cancelled", cancelled)
    monkeypatch.setattr(worker, "notify_job_terminal", notify)
    await worker._sync_terminal_summary(
        db, tracker_job_id=uuid4(), async_job_id=uuid4(), gpu_arbiter_error=None
    )
    for method in (failed, completed, cancelled, notify, db.commit):
        method.assert_not_awaited()


async def test_route_heartbeat_contention_does_not_end_the_tracker(monkeypatch):
    from aap_inference_contract.routing import RoutingUnavailable
    from app.services.ml_routing.authority import RoutingRejected

    heartbeat = AsyncMock(
        side_effect=[RoutingRejected("management_busy")] * 12
        + [RoutingUnavailable(), True, RoutingRejected("scope_closed")]
    )
    monkeypatch.setattr(runner, "execution_proof", lambda _job_id: object())
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())
    assert (
        await runner._renew_tracker_route(
            SimpleNamespace(heartbeat=heartbeat), object(), uuid4()
        )
        is False
    )
    assert heartbeat.await_count == 15


async def test_terminal_renewal_keeps_valid_proof_for_cleanup_but_not_business_writes(
    monkeypatch,
):
    job_id = uuid4()
    renewed = asyncio.Event()
    calls = []
    claim = None

    async def native_call(_db, action, request):
        nonlocal claim
        calls.append(action)
        if action == "claim":
            claim = ExecutionClaim(
                job_id=job_id,
                run_id=uuid4(),
                worker_id=request.worker_id,
                generation=1,
                lease_token=uuid4(),
                lease_expires_at=datetime.now(timezone.utc),
                state="ready",
            )
            return {"execution": claim}
        if action == "renew":
            renewed.set()
            raise VideoExecutionFailure("job_not_active")
        if action == "guard":
            raise VideoExecutionFailure("job_not_active")
        assert action == "release"
        assert request.run_id == claim.run_id
        return {"released": True}

    db = SimpleNamespace(
        commit=AsyncMock(), rollback=AsyncMock(), no_autoflush=nullcontext()
    )

    @asynccontextmanager
    async def sessions():
        yield db

    monkeypatch.setattr(execution, "_call", native_call)
    monkeypatch.setattr(execution, "_RENEW_SECONDS", 0)
    async with execution.tracker_execution(db, job_id, session_factory=sessions):
        await asyncio.wait_for(renewed.wait(), 1)
        await asyncio.sleep(0)
        assert execution.execution_proof(job_id) == claim.proof()
        with pytest.raises(execution.TrackerExecutionLost):
            await execution.fence_tracker_write(db, job_id, "stage")
    assert calls == ["claim", "renew", "guard", "release"]
