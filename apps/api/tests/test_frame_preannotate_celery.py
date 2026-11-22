"""Opt-in acceptance against the worktree's isolated Redis and PostgreSQL.

Run via the module-data verifier with AAP_FRAME_CELERY_ACCEPTANCE=1.
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
from app.db.models.notification import Notification
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.task import Task
from app.schemas.async_job import AsyncJobOut
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
    or os.environ.get("YOAO_WORKTREE_MODE") != "test",
    reason="Requires explicitly enabled, isolated Celery acceptance environment",
)


@pytest.mark.parametrize("after_wait", ["release", "cancel", "scope_changed"])
async def test_frame_receipts_outlive_real_celery_results_and_retries(
    frame_world, test_engine, test_db_url, monkeypatch, after_wait
):
    from tests.test_assets_media_live import _guard_role_url, _runtime_database_url

    world = frame_world
    runtime, owner = make_url(settings.database_url), make_url(test_db_url)
    assert (runtime.host, runtime.port, runtime.database) == (
        owner.host,
        owner.port,
        owner.database,
    )
    publication = _guard_role_url(
        "AAP_MODULE_DATA_JOBS_PUBLICATION_URL",
        "yoao_jobs_publication_client",
        test_engine,
    )
    if (publication.host, publication.port, publication.database) != (
        runtime.host,
        runtime.port,
        runtime.database,
    ) or publication.username in {runtime.username, owner.username}:
        pytest.fail("Jobs publication must use its own login on the test database")
    monkeypatch.setattr(
        settings, "jobs_publication_database_url", _runtime_database_url(publication)
    )
    monkeypatch.setattr(settings, "jobs_publication_database_url_file", "")
    assert not celery_app.conf.task_always_eager
    redis_url = make_url(os.environ["REDIS_URL"])
    assert redis_url.host == "127.0.0.1"
    assert make_url(settings.redis_url) == redis_url
    assert make_url(settings.effective_celery_broker) == redis_url
    monkeypatch.setattr(settings, "jobs_queue_url", None)
    monkeypatch.setitem(celery_app.conf, "result_expires", 5)
    inference_ready = threading.Event()
    allow_write = threading.Event()
    retried = threading.Event()
    last_segment_done = threading.Event()
    replay_done = threading.Event()
    replaying = threading.Event()
    retries = []
    deliveries = []
    terminal_progress = []
    observed_callbacks = []
    callback_done = threading.Event()
    publish_progress = frame_preannotate._publish_frame_progress

    def on_progress(project_id, current, total, *, status, **kwargs):
        if project_id == str(world.project_id) and status != "running":
            terminal_progress.append(status)
        publish_progress(project_id, current, total, status=status, **kwargs)

    monkeypatch.setattr(frame_preannotate, "_publish_frame_progress", on_progress)

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
            task.backend.expires = 5
            deliveries.append(
                {
                    "id": task.request.id,
                    "group": task.request.group,
                    "args": task.request.args,
                    "kwargs": task.request.kwargs,
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
            observed_callbacks.append(task.request.id)
            callback_done.set()
        if (
            task.name == frame_preannotate.predict_video_segment.name
            and task.request.kwargs.get("job_id") == str(world.job_id)
        ):
            if replaying.is_set():
                assert state == "SUCCESS"
                replay_done.set()
            elif state == "SUCCESS" and task.request.retries:
                last_segment_done.set()

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
            assert {task["task_name"] for task in prepared.lease.envelope["tasks"]} == {
                frame_preannotate.predict_video_segment.name
            }
            frozen_execution_ids = [
                task["execution_id"] for task in prepared.lease.envelope["tasks"]
            ]
            assert await asyncio.to_thread(inference_ready.wait, 20)
            async with world.maker() as locker:
                task = await locker.scalar(
                    select(Task).where(Task.id == world.task_id).with_for_update()
                )
                with redis.from_url(settings.redis_url) as broker:
                    done_key = frame_preannotate._progress_key(str(world.job_id))
                    total_key = frame_preannotate._progress_total_key(str(world.job_id))
                    finished_siblings = frozen_execution_ids[:2]
                    backend = celery_app.backend
                    assert all(
                        backend.get_task_meta(task_id, cache=False)["status"]
                        == "SUCCESS"
                        for task_id in finished_siblings
                    )
                    assert broker.get(done_key) == b"2"
                    assert broker.expire(done_key, 1)
                    assert broker.expire(total_key, 1)
                    allow_write.set()
                    async with asyncio.timeout(15):
                        while broker.exists(done_key, total_key) or any(
                            backend.get_task_meta(task_id, cache=False)["status"]
                            != "PENDING"
                            for task_id in finished_siblings
                        ):
                            await asyncio.sleep(0.1)
                    assert await asyncio.to_thread(retried.wait, 45)
                    live_job = await locker.get(AsyncJob, world.job_id)
                    assert live_job.status == "running"
                    expected = prepared.segment_ids
                    assert set(live_job.payload["frame_segment_receipts"]) == set(
                        expected[:2]
                    )
                    assert (
                        "frame_segment_receipts"
                        not in AsyncJobOut.model_validate(live_job).payload
                    )
                    assert not terminal_progress
                    assert retries[:3] == [0, 1, 2]
                    assert infer.await_count == 3
                assert [
                    delivery["id"] for delivery in deliveries[:3]
                ] == frozen_execution_ids
                resumed = [delivery for delivery in deliveries if delivery["resume"]]
                assert len(resumed) >= 2
                assert len({delivery["id"] for delivery in resumed}) == 1
                assert len({delivery["group"] for delivery in deliveries}) == 1
                assert all(delivery["queue"] == "ml" for delivery in deliveries)
                for delivery in resumed:
                    checkpoint = delivery["resume"]
                    assert checkpoint["frame_ordinals"] == [3]
                    assert checkpoint["segment_id"] == prepared.segment_ids[2]
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
            assert await asyncio.to_thread(last_segment_done.wait, 45)
            first = deliveries[0]
            replaying.set()
            frame_preannotate.predict_video_segment.apply_async(
                args=first["args"], kwargs=first["kwargs"], task_id=first["id"]
            )
            assert await asyncio.to_thread(replay_done.wait, 15)
            late_callback = frame_preannotate.finalize_frame_job.delay(
                [{"frames_done": 999, "boxes": 999}],
                project_id=str(world.project_id),
                job_id=str(world.job_id),
            )
            await asyncio.to_thread(late_callback.get, timeout=15)
            assert await asyncio.to_thread(callback_done.wait, 5)
            assert observed_callbacks == [late_callback.id]
            expected_status = {
                "release": "completed",
                "cancel": "cancelled",
                "scope_changed": "failed",
            }[after_wait]
            assert terminal_progress == [expected_status]
            assert infer.await_count == 3
            assert max(delivery["retries"] for delivery in deliveries) >= 3
            async with world.maker() as db:
                job = await db.get(AsyncJob, world.job_id)
                saved = 3 if after_wait == "release" else 2
                assert job.status == expected_status
                if after_wait != "scope_changed":
                    assert set(job.payload["frame_segment_receipts"]) == set(
                        prepared.segment_ids
                    )
                    assert job.result["frames_done"] == saved
                    assert job.result["failed"] == 0
                    assert job.result["skipped"] == int(after_wait == "cancel")
                    assert job.progress_pct == (99 if after_wait == "cancel" else 100)
                    assert job.result["boxes"] == saved
                notifications = (
                    await db.scalars(
                        select(Notification.type).where(
                            Notification.target_id == world.job_id,
                            Notification.user_id == world.owner_id,
                        )
                    )
                ).all()
                assert (
                    notifications
                    == {
                        "release": [],
                        "cancel": ["job.cancelled"],
                        "scope_changed": ["job.failed"],
                    }[after_wait]
                )
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
                assert all(
                    meta.extra["accepted_video_input"]["input_id"]
                    == str(world.input_id)
                    and meta.extra["accepted_video_input"]["plan_digest"]
                    == world.plan.plan_digest
                    and meta.extra["accepted_video_execution_job_id"]
                    == str(world.job_id)
                    for meta in metas
                )
            with redis.from_url(settings.redis_url) as broker:
                current = broker.get(frame_preannotate._progress_key(str(world.job_id)))
                assert current == (b"2" if after_wait == "scope_changed" else b"3")
    finally:
        allow_write.set()
        signals.task_retry.disconnect(on_retry)
        signals.task_prerun.disconnect(on_run)
        signals.task_postrun.disconnect(on_done)
