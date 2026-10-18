from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from kombu.utils.json import dumps, loads
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.prediction import Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.labeling_prediction_contract import (
    ApplyPredictionResultError,
    ApplyPredictionResultRequest,
)
from app.services.prediction import PredictionService, to_video_bbox_result
from app.workers import frame_preannotate
from tests.factory import create_project, create_task, create_user


@pytest.fixture
async def frame_world(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        owner = await create_user(
            db, "project_admin", f"frame-{uuid.uuid4()}@test.local", "Frame owner"
        )
        project = await create_project(db, owner_id=owner.id)
        task = await create_task(db, project_id=project.id)
        backend = MLBackendRegistry(name="frame-test", url="http://frame.test")
        job = AsyncJob(
            kind="batch_predict",
            status="running",
            project_id=project.id,
            user_id=owner.id,
            payload={"batch_id": None, "execution_scope": "bulk"},
        )
        db.add_all([backend, job])
        await db.commit()
        world = SimpleNamespace(
            maker=maker,
            owner_id=owner.id,
            project_id=project.id,
            task_id=task.id,
            backend_id=backend.id,
            job_id=job.id,
        )
    try:
        yield world
    finally:
        async with maker() as db:
            await db.execute(
                delete(PredictionMeta).where(
                    PredictionMeta.prediction_id.in_(
                        select(Prediction.id).where(Prediction.task_id == world.task_id)
                    )
                )
            )
            await db.execute(
                delete(Prediction).where(Prediction.task_id == world.task_id)
            )
            await db.execute(delete(AsyncJob).where(AsyncJob.id == world.job_id))
            await db.execute(delete(Task).where(Task.id == world.task_id))
            await db.execute(delete(Project).where(Project.id == world.project_id))
            await db.execute(delete(User).where(User.id == world.owner_id))
            await db.execute(
                delete(MLBackendRegistry).where(
                    MLBackendRegistry.id == world.backend_id
                )
            )
            await db.commit()


@pytest.mark.parametrize(
    "change", [None, "task_status", "job_terminal", "account_disabled", "still_busy"]
)
async def test_frame_persistence_retries_contention_and_rechecks_scope(
    frame_world, test_db_url, monkeypatch, change
):
    world = frame_world
    raw = [
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
    ]
    predict = AsyncMock(
        return_value=[
            SimpleNamespace(
                result=raw,
                score=0.9,
                model_version="model",
                inference_time_ms=12,
                meta={"total_tokens": 7},
            )
        ]
    )
    client = SimpleNamespace(predict=predict, last_instance_id=None, pool_id=None)
    monkeypatch.setattr(settings, "database_url", test_db_url)
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
        AsyncMock(return_value={0: "http://frame/0"}),
    )
    monkeypatch.setattr(frame_preannotate, "_bump_progress", lambda *_a: (1, 1))
    monkeypatch.setattr(
        frame_preannotate, "_publish_frame_progress", lambda *_a, **_k: None
    )
    monkeypatch.setattr(frame_preannotate, "_write_job_progress", AsyncMock())
    contended = asyncio.Event()
    failures = []
    apply_result = PredictionService.apply_prediction_result

    async def observed_apply(self, request):
        try:
            return await apply_result(self, request)
        except ApplyPredictionResultError as exc:
            failures.append(exc)
            if exc.code == "task_busy":
                contended.set()
            raise

    async with world.maker() as writer:
        task = await writer.get(Task, world.task_id)
        scope = capture_labeling_write_scope(
            task,
            actor_id=world.owner_id,
            actor_known=True,
            operation="preannotation",
            execution_scope="bulk",
            execution_scope_known=True,
            job_id=world.job_id,
            job_id_known=True,
        )
        await PredictionService(writer).apply_prediction_result(
            ApplyPredictionResultRequest(
                task_id=world.task_id,
                project_id=world.project_id,
                ml_backend_id=world.backend_id,
                result=to_video_bbox_result(raw, 99),
                write_scope=scope,
            )
        )
        monkeypatch.setattr(
            PredictionService, "apply_prediction_result", observed_apply
        )
        runner = asyncio.create_task(
            frame_preannotate._run_segment(
                segment={"task_id": str(world.task_id), "frame_indices": [0]},
                project_id=str(world.project_id),
                ml_backend_id=str(world.backend_id),
                stage0={},
                job_id=str(world.job_id),
                celery_root_task_id=None,
            )
        )
        try:
            await asyncio.wait_for(contended.wait(), timeout=5)
            if change == "task_status":
                task.status = "review"
            elif change == "job_terminal":
                job = await writer.get(AsyncJob, world.job_id)
                job.status = "cancelled"
            elif change == "account_disabled":
                owner = await writer.get(User, world.owner_id)
                owner.is_active = False
            if change == "still_busy":
                with pytest.raises(frame_preannotate._FrameWriteDeferred) as deferred:
                    await asyncio.wait_for(runner, timeout=10)
                resume = loads(dumps(deferred.value.resume))
                assert resume["stats"]["failed"] == 0
                assert resume["pending_write"]["ml_backend_id"] == world.backend_id
                assert resume["write_scope"]["task_version"] == scope.task_version
                await writer.commit()
                context_read = AsyncMock(side_effect=RuntimeError("source unavailable"))
                monkeypatch.setattr(
                    "app.services.video_frame_service.AssetsVideoContextReader.from_task_summary",
                    context_read,
                )
                cache_read = AsyncMock(side_effect=RuntimeError("cache unavailable"))
                monkeypatch.setattr(frame_preannotate, "_read_frame_urls", cache_read)
                stats = await frame_preannotate._run_segment(
                    segment={"task_id": str(world.task_id), "frame_indices": [0]},
                    project_id=str(world.project_id),
                    ml_backend_id=str(world.backend_id),
                    stage0={},
                    job_id=str(world.job_id),
                    celery_root_task_id=None,
                    resume=resume,
                )
                context_read.assert_not_awaited()
                cache_read.assert_not_awaited()
            else:
                await writer.commit()
                stats = await asyncio.wait_for(runner, timeout=10)
        finally:
            if not runner.done():
                runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    succeeded = change in {None, "still_busy"}
    assert stats["frames_done"] == int(succeeded)
    assert stats["boxes"] == int(succeeded)
    assert stats["failed"] == int(not succeeded)
    predict.assert_awaited_once()
    assert all(exc.detail == {"reason": "task_busy"} for exc in failures[:-1])
    if change == "still_busy":
        assert len(failures) == frame_preannotate._WRITE_ATTEMPTS
    elif change == "task_status":
        assert failures[-1].detail == {"reason": "prediction_task_scope_changed"}
    elif change == "job_terminal":
        assert failures[-1].detail == {"reason": "prediction_execution_not_writable"}
    elif change == "account_disabled":
        assert failures[-1].status_code == 401
    async with world.maker() as db:
        rows = list(
            (
                await db.scalars(
                    select(Prediction).where(Prediction.task_id == world.task_id)
                )
            ).all()
        )
        assert len(rows) == 1 + int(succeeded)
        task = await db.get(Task, world.task_id)
        assert task.total_predictions == len(rows)
        assert await db.scalar(
            select(func.count())
            .select_from(PredictionMeta)
            .where(PredictionMeta.prediction_id.in_([row.id for row in rows]))
        ) == len(rows)
        if succeeded:
            retried = next(
                row for row in rows if row.result[0]["geometry"]["frame_index"] == 0
            )
            assert retried.ml_backend_id == world.backend_id
            assert retried.score == 0.9 and retried.model_version == "model"
            meta = await db.scalar(
                select(PredictionMeta).where(PredictionMeta.prediction_id == retried.id)
            )
            assert meta.inference_time_ms == 12 and meta.total_tokens == 7


@pytest.mark.parametrize("status", ["cancelled", "failed", "completed"])
async def test_late_frame_progress_and_finalize_preserve_terminal_job(
    frame_world, test_db_url, monkeypatch, status
):
    world = frame_world
    monkeypatch.setattr(settings, "database_url", test_db_url)
    publish = Mock()
    progress = Mock()
    monkeypatch.setattr(frame_preannotate, "_publish_frame_progress", publish)
    monkeypatch.setattr(frame_preannotate, "_bump_progress", progress)
    async with world.maker() as db:
        job = await db.get(AsyncJob, world.job_id)
        job.status = status
        job.progress_pct = 42
        job.result = {"retained": True}
        await db.commit()
    await frame_preannotate._finish_segment(
        {"frames_done": 9, "skipped": 0, "failed": 0},
        [],
        project_id=str(world.project_id),
        job_id=str(world.job_id),
    )
    await frame_preannotate._finalize(
        [{"frames_done": 9, "boxes": 9}],
        project_id=str(world.project_id),
        job_id=str(world.job_id),
    )
    async with world.maker() as db:
        job = await db.get(AsyncJob, world.job_id)
        assert job.status == status
        assert job.progress_pct == 42
        assert job.result == {"retained": True}
    publish.assert_not_called()
    progress.assert_not_called()


@pytest.mark.parametrize("missing", ["task", "backend"])
async def test_missing_frame_resources_are_counted_as_failures(
    frame_world, test_db_url, monkeypatch, missing
):
    world = frame_world
    monkeypatch.setattr(settings, "database_url", test_db_url)
    progress = AsyncMock()
    client = Mock(side_effect=AssertionError("No inference for missing resources"))
    monkeypatch.setattr(frame_preannotate, "_write_job_progress", progress)
    monkeypatch.setattr("app.services.ml_routing.client.RoutedMLBackendClient", client)
    stats = await frame_preannotate._run_segment(
        segment={
            "task_id": str(uuid.uuid4() if missing == "task" else world.task_id),
            "frame_indices": [0, 1],
        },
        project_id=str(world.project_id),
        ml_backend_id=str(uuid.uuid4() if missing == "backend" else world.backend_id),
        stage0={},
        job_id=str(world.job_id),
        celery_root_task_id=None,
    )
    assert stats["failed"] == 2 and stats["frames_done"] == 0
    progress.assert_awaited_once_with(str(world.job_id), 2, str(world.project_id))
    client.assert_not_called()
