from __future__ import annotations

from unittest.mock import AsyncMock, call
from uuid import UUID

import pytest


@pytest.fixture(autouse=True)
def original_content_epoch(monkeypatch):
    from app.services.video_tracking import runner

    monkeypatch.setattr(
        runner,
        "read_mask_write_epochs",
        AsyncMock(side_effect=lambda _db, keys: dict.fromkeys(keys, 7)),
    )


@pytest.mark.asyncio
async def test_tracker_root_bridge_requires_the_exact_running_owner(monkeypatch):
    from app.services.video_tracking import tracker_mask_roots as roots

    db = object()
    job_id = UUID("00000000-0000-0000-0000-000000000001")
    task_id = UUID("00000000-0000-0000-0000-000000000002")
    key = "raster-masks/sha256/a0/a0/" + "a0a0" + "a" * 60 + ".json"
    register = AsyncMock(return_value=True)
    monkeypatch.setattr(roots, "register_tracker_running_mask_root", register)
    guard = AsyncMock()
    monkeypatch.setattr(roots, "fence_tracker_write", guard)

    await roots.retain_tracker_output_mask(
        db, job_id=job_id, task_id=task_id, object_key=key
    )

    register.assert_awaited_once_with(
        db, job_id=job_id, task_id=task_id, object_key=key
    )
    guard.assert_awaited_once_with(db, job_id, "mask_root")
    register.return_value = False
    with pytest.raises(roots.TrackerMaskRootOwnerChanged):
        await roots.retain_tracker_output_mask(
            db, job_id=job_id, task_id=task_id, object_key=key
        )


@pytest.mark.asyncio
async def test_materialize_registers_the_root_before_committing_producer_grace(
    monkeypatch,
):
    from types import SimpleNamespace

    from app.services import raster_mask_storage as storage
    from app.services.video_tracking import runner
    from app.services.video_tracking.adapters import TrackerFrameResult

    rle = {"encoding": "coco_rle", "size": [1, 1], "counts": [0, 1]}
    reference = storage.build_rle_reference(rle)
    job_id = UUID("00000000-0000-0000-0000-000000000001")
    task_id = UUID("00000000-0000-0000-0000-000000000002")
    events = []

    async def store(_db, _rle, *, writer):
        assert writer is runner.store_coco_rle
        events.append("stored")
        return reference

    async def register(_db, *, job_id, task_id, object_key):
        assert (job_id, task_id) == (
            UUID("00000000-0000-0000-0000-000000000001"),
            UUID("00000000-0000-0000-0000-000000000002"),
        )
        assert object_key == reference["object_key"]
        events.append("rooted")

    async def commit():
        events.append("committed")

    db = SimpleNamespace(commit=AsyncMock(side_effect=commit))
    monkeypatch.setattr(runner, "store_generated_mask_content", store)
    monkeypatch.setattr(runner, "retain_tracker_output_mask", register)

    await runner._materialize_tracker_mask_result(
        TrackerFrameResult(frame_index=0, geometry={"type": "mask", "rle": rle}),
        db=db,
        job_id=job_id,
        task_id=task_id,
    )

    assert events == ["stored", "rooted", "committed"]


@pytest.mark.asyncio
async def test_materialize_rolls_back_when_the_tracker_job_stops_owning_output(
    monkeypatch,
):
    from types import SimpleNamespace

    from app.services import raster_mask_storage as storage
    from app.services.video_tracking import runner
    from app.services.video_tracking.adapters import TrackerFrameResult

    rle = {"encoding": "coco_rle", "size": [1, 1], "counts": [0, 1]}
    reference = storage.build_rle_reference(rle)
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(
        runner, "store_generated_mask_content", AsyncMock(return_value=reference)
    )
    monkeypatch.setattr(
        runner,
        "retain_tracker_output_mask",
        AsyncMock(side_effect=runner.TrackerMaskRootOwnerChanged),
    )

    with pytest.raises(runner.TrackerMaskWriteCancelled):
        await runner._materialize_tracker_mask_result(
            TrackerFrameResult(frame_index=0, geometry={"type": "mask", "rle": rle}),
            db=db,
            job_id=UUID("00000000-0000-0000-0000-000000000001"),
            task_id=UUID("00000000-0000-0000-0000-000000000002"),
        )

    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_materialize_retries_root_contention_with_the_same_stored_mask(
    monkeypatch,
):
    from types import SimpleNamespace

    from app.services import raster_mask_storage as storage
    from app.services.video_tracking import runner
    from app.services.video_tracking.adapters import TrackerFrameResult

    rle = {"encoding": "coco_rle", "size": [1, 1], "counts": [0, 1]}
    reference = storage.build_rle_reference(rle)
    events = []
    store = AsyncMock(return_value=reference)
    relock = AsyncMock(
        side_effect=lambda _db, _ref, *, verify, content_epochs: events.append("relock")
    )
    attempts = 0

    async def register(_db, *, job_id, task_id, object_key):
        nonlocal attempts
        attempts += 1
        events.append("register")
        if attempts == 1:
            raise storage.RasterMaskContractError(
                status_code=503,
                reason="mask_storage_unavailable",
                message="Job finalization owns the root transaction",
                retryable=True,
            )

    async def commit():
        events.append("commit")

    db = SimpleNamespace(commit=AsyncMock(side_effect=commit), rollback=AsyncMock())
    monkeypatch.setattr(runner, "store_generated_mask_content", store)
    monkeypatch.setattr(runner, "lock_raster_mask_references", relock)
    monkeypatch.setattr(runner, "retain_tracker_output_mask", register)
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())
    running = AsyncMock(return_value=True)

    result = await runner._materialize_tracker_mask_result(
        TrackerFrameResult(frame_index=0, geometry={"type": "mask", "rle": rle}),
        db=db,
        job_id=UUID("00000000-0000-0000-0000-000000000001"),
        task_id=UUID("00000000-0000-0000-0000-000000000002"),
        still_running=running,
    )

    assert result.geometry["mask"] == reference
    store.assert_awaited_once()
    relock.assert_awaited_once_with(
        db, reference, verify=False, content_epochs={reference["object_key"]: 7}
    )
    assert attempts == 2
    db.rollback.assert_awaited_once()
    db.commit.assert_awaited_once()
    assert events == ["register", "relock", "register", "commit"]


def test_mask_retention_includes_only_roots_of_running_tracker_jobs():
    from app.application.raster_mask_retention import (
        _MASK_REFERENCE_EXISTS_QUERIES,
        _MASK_REFERENCE_QUERIES,
    )

    active_list_queries = [
        query
        for query in _MASK_REFERENCE_QUERIES
        if "video_tracker_running_outputs" in query
    ]
    active_exists_queries = [
        query
        for query in _MASK_REFERENCE_EXISTS_QUERIES
        if "video_tracker_running_outputs" in query
    ]
    assert len(active_list_queries) == len(active_exists_queries) == 1
    for query in active_list_queries + active_exists_queries:
        assert "JOIN video_tracker_jobs AS job ON job.id::text = root.root_id" in query
        assert "job.status = 'running'" in query


@pytest.mark.asyncio
async def test_materialize_republishes_held_rle_after_a_collector_advances_epoch(
    monkeypatch,
):
    from types import SimpleNamespace

    from app.services import raster_mask_storage as storage
    from app.services.video_tracking import runner
    from app.services.video_tracking.adapters import TrackerFrameResult

    rle = {"encoding": "coco_rle", "size": [1, 1], "counts": [0, 1]}
    reference = storage.build_rle_reference(rle)
    key = reference["object_key"]
    store = AsyncMock(return_value=reference)
    relock = AsyncMock(
        side_effect=storage.RasterMaskContractError(
            status_code=409,
            reason="mask_content_epoch_changed",
            message="The old content fact was collected",
            retryable=True,
        )
    )
    register = AsyncMock(
        side_effect=[
            storage.RasterMaskContractError(
                status_code=503,
                reason="mask_storage_unavailable",
                message="The Job is locked",
                retryable=True,
            ),
            None,
        ]
    )
    epochs = AsyncMock(side_effect=[{key: 7}, {key: 8}])
    db = SimpleNamespace(commit=AsyncMock(), rollback=AsyncMock())
    monkeypatch.setattr(runner, "store_generated_mask_content", store)
    monkeypatch.setattr(runner, "read_mask_write_epochs", epochs)
    monkeypatch.setattr(runner, "lock_raster_mask_references", relock)
    monkeypatch.setattr(runner, "retain_tracker_output_mask", register)
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())

    result = await runner._materialize_tracker_mask_result(
        TrackerFrameResult(frame_index=0, geometry={"type": "mask", "rle": rle}),
        db=db,
        job_id=UUID("00000000-0000-0000-0000-000000000001"),
        task_id=UUID("00000000-0000-0000-0000-000000000002"),
        still_running=AsyncMock(return_value=True),
    )

    assert result.geometry["mask"] == reference
    assert store.await_args_list == [
        call(db, rle, writer=runner.store_coco_rle),
        call(db, rle, writer=runner.store_coco_rle),
    ]
    relock.assert_awaited_once_with(
        db, reference, verify=False, content_epochs={key: 7}
    )
    assert epochs.await_count == 2
    assert register.await_count == 2
    assert db.rollback.await_count == 2
    db.commit.assert_awaited_once()
