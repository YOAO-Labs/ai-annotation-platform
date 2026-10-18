"""Opt-in acceptance against the worktree's isolated Redis and PostgreSQL.

Run via dev:worktree exec --mode test with AAP_FRAME_CELERY_ACCEPTANCE=1.
Inference and frame-cache reads are fixtures; dispatch, retries and writes are real.
"""

from __future__ import annotations

import asyncio
import os
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import redis
from celery import signals
from celery.contrib.testing.worker import start_worker
from sqlalchemy import select

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.task import Task
from app.workers import frame_preannotate
from app.workers.celery_app import celery_app
from tests.test_frame_preannotate_persistence import frame_world as frame_world

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
    assert settings.database_url == test_db_url
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
    client = SimpleNamespace(predict=infer, last_instance_id=None, pool_id=None)
    monkeypatch.setattr(
        "app.services.ml_routing.client.RoutedMLBackendClient", lambda *_a, **_k: client
    )
    monkeypatch.setattr(
        "app.services.ml_backend.MLBackendService.pool_id_for_registry",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.video_frame_service.AssetsVideoContextReader.from_task_summary",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(
        frame_preannotate,
        "_read_frame_urls",
        AsyncMock(return_value={frame: f"http://frame/{frame}" for frame in range(3)}),
    )

    def on_retry(request=None, **_kwargs):
        if request.task == frame_preannotate.predict_video_segment.name:
            retries.append(request.retries)
            if len(retries) >= 3:
                retried.set()

    def on_run(task=None, **_kwargs):
        if task.name == frame_preannotate.predict_video_segment.name:
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
        if task.name == frame_preannotate.finalize_frame_job.name:
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
            frame_preannotate._init_progress(str(world.job_id), 3)
            launched = frame_preannotate.launch_predict_phase.delay(
                [],
                project_id=str(world.project_id),
                ml_backend_id=str(world.backend_id),
                stage0={},
                job_id=str(world.job_id),
                segments=[
                    {"task_id": str(world.task_id), "frame_indices": [0]},
                    {"task_id": str(world.task_id), "frame_indices": [1, 2]},
                ],
            )
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
                    assert retries[:3] == [0, 1, 2]
                    assert infer.await_count == 3
                    for key in keys:
                        assert broker.ttl(key) > 60
                    assert (
                        broker.ttl(
                            frame_preannotate._progress_total_key(str(world.job_id))
                        )
                        > 60
                    )
                    assert (
                        int(
                            broker.get(
                                frame_preannotate._progress_key(str(world.job_id))
                            )
                        )
                        == 1
                    )
                resumed = [delivery for delivery in deliveries if delivery["resume"]]
                assert len(resumed) >= 2
                assert len({delivery["id"] for delivery in resumed}) == 1
                assert all(delivery["queue"] == "ml" for delivery in deliveries)
                for delivery in resumed:
                    checkpoint = delivery["resume"]
                    assert checkpoint["frame_indices"] == [2]
                    assert checkpoint["stats"]["frames_done"] == 1
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
            await asyncio.to_thread(launched.get, timeout=5)
            async with world.maker() as db:
                job = await db.get(AsyncJob, world.job_id)
                saved = 3 if after_wait == "release" else 2
                assert job.status == (
                    "cancelled" if after_wait == "cancel" else "completed"
                )
                assert job.result["frames_done"] == saved
                assert job.result["failed"] == int(after_wait == "scope_changed")
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
                assert (
                    int(broker.get(frame_preannotate._progress_key(str(world.job_id))))
                    == 3
                )
    finally:
        allow_write.set()
        signals.task_retry.disconnect(on_retry)
        signals.task_prerun.disconnect(on_run)
        signals.task_postrun.disconnect(on_done)
