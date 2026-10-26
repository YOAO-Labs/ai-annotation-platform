"""Opt-in acceptance against the worktree's isolated Redis and PostgreSQL.

Run via dev:worktree exec --mode test with AAP_FRAME_CELERY_ACCEPTANCE=1.
Inference and retained-byte references are fixtures; dispatch, retries, and writes are real.
"""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import redis
from app.application.preannotation_contract import (
    FramePreannotationDispatchRequest,
    VideoFrameTaskPlan,
)
from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.task import Task
from app.workers import frame_preannotate
from app.workers.celery_app import celery_app
from celery import signals
from celery.contrib.testing.worker import start_worker
from sqlalchemy import select
from sqlalchemy.engine import make_url

from tests.test_frame_preannotate_persistence import (
    _install_frame_execution_fake,
    _install_video_gateway,
    frame_world as frame_world,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("AAP_FRAME_CELERY_ACCEPTANCE") != "1"
    or os.environ.get("AAP_WORKTREE_MODE") != "test",
    reason="Requires explicitly enabled, isolated Celery acceptance environment",
)


@pytest.mark.parametrize("after_wait", ["release", "cancel", "scope_changed"])
async def test_frame_chord_retains_results_across_real_retries(
    frame_world, test_db_url, monkeypatch, after_wait
):
    world = frame_world
    runtime, owner = make_url(settings.database_url), make_url(test_db_url)
    assert (runtime.host, runtime.port, runtime.database) == (
        owner.host,
        owner.port,
        owner.database,
    )
    assert not celery_app.conf.task_always_eager
    inference_ready = threading.Event()
    allow_write = threading.Event()
    retried = threading.Event()
    finalized = threading.Event()
    retries = []
    deliveries = []
    finalizations = []

    async def predict(tasks, context=None):
        frame = int(tasks[0]["id"].split(":")[-1])
        if frame == 2:
            inference_ready.set()
            assert await asyncio.to_thread(allow_write.wait, 15)
        return [
            SimpleNamespace(
                result=[
                    {
                        "type": "rectanglelabels",
                        "value": {
                            "x": 10,
                            "y": 20,
                            "width": 30,
                            "height": 40,
                            "rectanglelabels": ["car"],
                        },
                    }
                ],
                score=0.9,
                model_version="live-fixture",
                inference_time_ms=12,
                meta={"total_tokens": 7},
            )
        ]

    infer = AsyncMock(side_effect=predict)
    _install_frame_execution_fake(
        monkeypatch, infer, actual_instance_id=world.backend_id
    )
    _install_video_gateway(monkeypatch, world)
    monkeypatch.setattr(frame_preannotate.settings, "frame_preannotate_chunk_size", 1)

    def on_retry(request=None, **_kwargs):
        if (
            request.task == frame_preannotate.predict_video_segment.name
            and request.kwargs.get("job_id") == str(world.job_id)
        ):
            retries.append(request.retries)
            if len(retries) >= 3:
                retried.set()

    def on_run(task=None, **_kwargs):
        if (
            task.name == frame_preannotate.predict_video_segment.name
            and task.request.kwargs.get("job_id") == str(world.job_id)
        ):
            deliveries.append(
                {
                    "id": task.request.id,
                    "group": task.request.group,
                    "retries": task.request.retries,
                    "resume": task.request.kwargs.get("resume"),
                    "queue": task.request.delivery_info["routing_key"],
                }
            )

    def on_done(task=None, state=None, **_kwargs):
        if (
            task.name == frame_preannotate.finalize_frame_job.name
            and task.request.kwargs.get("job_id") == str(world.job_id)
        ):
            finalizations.append(state)
            finalized.set()

    signals.task_retry.connect(on_retry, weak=False)
    signals.task_prerun.connect(on_run, weak=False)
    signals.task_postrun.connect(on_done, weak=False)
    try:
        with start_worker(
            celery_app,
            pool="solo",
            concurrency=1,
            queues=("default", "ml"),
            perform_ping_check=False,
            shutdown_timeout=20,
        ):
            dispatcher = frame_preannotate.JobsFramePreannotationDispatcher()
            async with world.maker() as acceptance:
                prepared = await dispatcher.prepare(
                    FramePreannotationDispatchRequest(
                        project_id=str(world.project_id),
                        ml_backend_id=str(world.backend_id),
                        stage0={},
                        job_id=str(world.job_id),
                        root_execution_id="root-frame-test",
                        actor_id=str(world.actor_id),
                        video_task_plans=(
                            VideoFrameTaskPlan(
                                task_id=str(world.task_id),
                                input_id=str(world.input_id),
                                input_job_id=str(world.job_id),
                                plan_digest=world.plan.plan_digest,
                                selected_count=world.plan.selected_count,
                            ),
                        ),
                        accepted_video_manifest_id=str(world.job_id),
                        accepted_video_selection_digest=world.selection_digest,
                        accepted_video_sealed_digest=world.sealed_digest,
                    ),
                    db=acceptance,
                )
                job = await acceptance.get(AsyncJob, world.job_id)
                job.result = {
                    **(job.result or {}),
                    "frame_total": prepared.total_frames,
                    "frame_dispatch": {
                        "intent_id": str(prepared.lease.intent_id),
                        "segment_ids": list(prepared.segment_ids),
                        "total_frames": prepared.total_frames,
                    },
                }
                await acceptance.commit()
                receipt = await dispatcher(prepared, db=acceptance)
            assert receipt.dispatched and receipt.segments == 3
            assert await asyncio.to_thread(inference_ready.wait, 20)
            async with world.maker() as locker:
                task = await locker.scalar(
                    select(Task).where(Task.id == world.task_id).with_for_update()
                )
                allow_write.set()
                with redis.from_url(settings.redis_url) as broker:
                    group = deliveries[-1]["group"]
                    keys = [
                        celery_app.backend.get_key_for_group(group, suffix)
                        for suffix in (".j", ".s")
                    ]
                    # Force a short horizon, then prove deferred writes renew it.
                    for key in keys:
                        assert broker.exists(key)
                        broker.expire(key, 20)
                    broker.expire(
                        frame_preannotate._progress_key(str(world.job_id)), 20
                    )
                    broker.expire(
                        frame_preannotate._progress_total_key(str(world.job_id)), 20
                    )
                    assert await asyncio.to_thread(retried.wait, 45)
                    assert not finalized.is_set()
                    assert (
                        await locker.get(AsyncJob, world.job_id)
                    ).status == "running"
                    assert retries[:3] == [0, 1, 2]
                    assert infer.await_count == 3
                    for key in keys:
                        assert broker.ttl(key) > 60
                    assert (
                        broker.ttl(frame_preannotate._progress_key(str(world.job_id)))
                        > 60
                    )
                    assert (
                        broker.ttl(
                            frame_preannotate._progress_total_key(str(world.job_id))
                        )
                        > 60
                    )
                    assert (
                        broker.ttl(
                            frame_preannotate._segment_results_key(str(world.job_id))
                        )
                        > 60
                    )
                    assert (
                        int(
                            broker.get(
                                frame_preannotate._progress_key(str(world.job_id))
                            )
                        )
                        == 2
                    )
                resumed = [delivery for delivery in deliveries if delivery["resume"]]
                assert len(resumed) >= 2
                assert len({delivery["id"] for delivery in resumed}) == 1
                assert all(delivery["queue"] == "ml" for delivery in deliveries)
                for delivery in resumed:
                    checkpoint = delivery["resume"]
                    assert checkpoint["frame_ordinals"] == [3]
                    assert checkpoint["stats"]["frames_done"] == 0
                    assert checkpoint["stats"]["failed"] == 0
                    assert checkpoint["write_scope"]["task_id"] == world.task_id
                    assert checkpoint["write_scope"]["task_version"] == task.version
                job = await locker.get(AsyncJob, world.job_id)
                assert job.status == "running"
                if after_wait == "cancel":
                    job.payload = {**job.payload, "cancel_requested": True}
                elif after_wait == "scope_changed":
                    task.version += 1
                await locker.commit()
            assert await asyncio.to_thread(finalized.wait, 45)
            assert finalizations == ["SUCCESS"]
            assert infer.await_count == 3
            assert max(delivery["retries"] for delivery in deliveries) >= 3
            async with world.maker() as db:
                job = await db.get(AsyncJob, world.job_id)
                saved = 3 if after_wait == "release" else 2
                assert job.status == (
                    "cancelled"
                    if after_wait == "cancel"
                    else "failed"
                    if after_wait == "scope_changed"
                    else "completed"
                )
                if after_wait != "scope_changed":
                    assert job.result["frames_done"] == saved
                    assert job.result["failed"] == 0
                    assert job.result["skipped"] == int(after_wait == "cancel")
                rows = list(
                    (
                        await db.scalars(
                            select(Prediction).where(
                                Prediction.task_id == world.task_id
                            )
                        )
                    ).all()
                )
                assert len(rows) == saved
                assert {
                    row.result[0]["geometry"]["frame_index"] for row in rows
                } == set(range(saved))
                assert (await db.get(Task, world.task_id)).total_predictions == saved
                metas = list(
                    (
                        await db.scalars(
                            select(PredictionMeta).where(
                                PredictionMeta.prediction_id.in_(
                                    [row.id for row in rows]
                                )
                            )
                        )
                    ).all()
                )
                assert len(metas) == saved
                assert all(meta.total_tokens == 7 for meta in metas)
            with redis.from_url(settings.redis_url) as broker:
                assert int(
                    broker.get(frame_preannotate._progress_key(str(world.job_id)))
                ) == (2 if after_wait == "scope_changed" else 3)
    finally:
        allow_write.set()
        signals.task_retry.disconnect(on_retry)
        signals.task_prerun.disconnect(on_run)
        signals.task_postrun.disconnect(on_done)
