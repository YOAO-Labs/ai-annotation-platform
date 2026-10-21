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


async def test_stale_failure_cannot_fail_replacement(monkeypatch):
    job_id = uuid4()
    replacement = SimpleNamespace(
        id=job_id,
        status="running",
        staged_result={"replacement": True},
        error_message=None,
        completed_at=None,
    )
    db = SimpleNamespace(rollback=AsyncMock(), commit=AsyncMock())
    publisher = AsyncMock()
    monkeypatch.setattr(
        runner, "_load_job_for_update", AsyncMock(return_value=replacement)
    )
    monkeypatch.setattr(
        runner,
        "fence_tracker_write",
        AsyncMock(side_effect=execution.TrackerExecutionLost()),
    )
    with pytest.raises(execution.TrackerExecutionLost):
        await runner._mark_failed(db, job_id, "old worker crashed", publisher)
    assert replacement.status == "running"
    assert replacement.staged_result == {"replacement": True}
    assert replacement.error_message is None
    db.commit.assert_not_awaited()
    publisher.assert_not_awaited()


async def test_stale_staging_cannot_replace_candidates_after_input_validation(
    monkeypatch,
):
    job = SimpleNamespace(
        id=uuid4(), task_id=uuid4(), staged_result={"replacement": True}
    )
    db = SimpleNamespace(refresh=AsyncMock())
    accepted = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(runner, "_lock_tracker_input_for_stage", accepted)
    guard = AsyncMock(side_effect=execution.TrackerExecutionLost())
    monkeypatch.setattr(runner, "fence_tracker_write", guard)
    with pytest.raises(execution.TrackerExecutionLost):
        await runner._stage_tracker_candidates(
            db,
            job=job,
            results=[],
            grid_step=1,
            output_geometry="bbox",
            actor_id=uuid4(),
            status=None,
            gateway=None,
            source_version=None,
        )
    accepted.assert_awaited_once()
    guard.assert_awaited_once_with(db, job.id, "stage")
    db.refresh.assert_not_awaited()
    assert job.staged_result == {"replacement": True}


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


async def test_stage_contention_retains_primitive_scope_after_orm_expiry(monkeypatch):
    job_id, task_id = uuid4(), uuid4()

    class Job:
        expired = False
        staged_result = None

        @property
        def id(self):
            assert not self.expired, "implicit async ORM refresh after rollback"
            return job_id

        @property
        def task_id(self):
            assert not self.expired, "implicit async ORM refresh after rollback"
            return task_id

    job = Job()
    evidence = SimpleNamespace(
        source_version_id="retained",
        source_sha256="a" * 64,
        plan_digest="b" * 64,
        model_dump=lambda **kw: {},
    )
    scopes = []

    async def input_lock(_db, **kwargs):
        scopes.append((kwargs["job"].id, kwargs["job"].task_id))
        return SimpleNamespace(evidence=evidence)

    async def rollback():
        job.expired = True

    async def refresh(_job):
        job.expired = False

    db = SimpleNamespace(
        rollback=AsyncMock(side_effect=rollback), refresh=AsyncMock(side_effect=refresh)
    )
    guard = AsyncMock(side_effect=[execution.TrackerExecutionBusy(), None])
    monkeypatch.setattr(runner, "_lock_tracker_input_for_stage", input_lock)
    monkeypatch.setattr(runner, "fence_tracker_write", guard)
    monkeypatch.setattr(runner, "_source_map_from_tracker_input", lambda *args: {})
    monkeypatch.setattr(runner, "_stage_tracker_results", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())
    await runner._stage_tracker_candidates(
        db,
        job=job,
        results=[],
        grid_step=1,
        output_geometry="bbox",
        actor_id=uuid4(),
        status=SimpleNamespace(plan_digest="b" * 64, execution=None),
        gateway=None,
        source_version=SimpleNamespace(version_id="retained", sha256="a" * 64),
        cancelling=True,
    )
    assert scopes == [(job_id, task_id)] * 2
    assert all(item.args[2] == "cancel" for item in guard.await_args_list)
    db.rollback.assert_awaited_once()


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
