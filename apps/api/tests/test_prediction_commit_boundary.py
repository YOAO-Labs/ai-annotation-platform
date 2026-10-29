"""Committed worker transactions: replacement, multi-result failure and cancellation.

These fixtures deliberately use independent connections, not the API test
SAVEPOINT. An inference callback can commit a real concurrent cancellation or
scope change; worker rollback must not undo either operation.
"""

import asyncio
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select, text
from starlette.requests import Request

from app.api.v1.async_jobs import cancel_async_job
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.notification import Notification
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services import async_job as jobs
from aap_inference.backend_transport import PredictionResult
from app.services.prediction import PredictionService
from app.workers import tasks as worker
from tests.factory import create_batch, create_project, create_task, create_user
from tests.inference_execution_test_ports import (
    PredictionClientExecutionPort,
    run_batch_request,
)
from tests.managed_prediction_worker_fixtures import (
    freeze_job,
    input_world as input_world,
    managed_worker_world as managed_worker_world,
    worker_engine,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def commit_world(managed_worker_world, monkeypatch):
    factory = managed_worker_world.factory
    async with factory() as db:
        user = await create_user(
            db, "super_admin", f"commit-{uuid.uuid4()}@example.test", "Commit"
        )
        project = await create_project(db, owner_id=user.id)
        batch = await create_batch(db, project_id=project.id, status="active")
        backend = MLBackendRegistry(
            name="commit-fixture", url="http://owned.invalid", state="connected"
        )
        db.add(backend)
        await db.flush()
        task = await create_task(db, project_id=project.id)
        task.batch_id = batch.id
        task.file_path = "http://owned.invalid/image.png"
        old = await PredictionService(db).create_from_ml_result(
            task.id, project.id, backend.id, [], model_version="old"
        )
        for source in ("manual", "prediction_based"):
            db.add(
                Annotation(
                    task_id=task.id,
                    project_id=project.id,
                    user_id=user.id,
                    class_name="car",
                    source=source,
                    geometry={"x": 1, "y": 2, "w": 3, "h": 4},
                    parent_prediction_id=old.id
                    if source == "prediction_based"
                    else None,
                )
            )
        task.total_annotations = 2
        task.is_labeled = True
        job = await jobs.create_job(
            db,
            kind="batch_predict",
            user_id=user.id,
            project_id=project.id,
            payload={"total_tasks": 1, "task_ids": [str(task.id)]},
        )
        job.celery_task_id = str(job.id)
        await db.commit()
        world = SimpleNamespace(
            factory=factory,
            worker_world=managed_worker_world,
            user=user.id,
            project=project.id,
            batch=batch.id,
            backend=backend.id,
            task=task.id,
            old=old.id,
            job=job.id,
        )
    monkeypatch.setattr(
        "app.services.prediction_progress.publish_progress", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        "app.workers.celery_app.celery_app.control.revoke", lambda *a, **kw: None
    )
    yield world


async def prepare(world, *, task_ids=None, mode="overwrite"):
    async with world.factory() as db:
        await freeze_job(
            db,
            project=await db.get(Project, world.project),
            actor=await db.get(User, world.user),
            backend=await db.get(MLBackendRegistry, world.backend),
            tasks=[
                await db.get(Task, uuid.UUID(value))
                for value in (task_ids or [str(world.task)])
            ],
            job=await db.get(AsyncJob, world.job),
            batch_id=world.batch,
            predict_mode=mode,
        )


async def run(world, **kwargs):
    task_ids = kwargs.pop("task_ids", [str(world.task)])
    mode = kwargs.pop("predict_mode", "overwrite")
    if kwargs.pop("freeze", True) and kwargs.get("execution_unit") != "frame":
        await prepare(world, task_ids=task_ids, mode=mode)
    async with worker_engine(world.worker_world):
        await run_batch_request(
            project_id=str(world.project),
            ml_backend_id=str(world.backend),
            task_ids=task_ids,
            batch_id=str(world.batch),
            user_id=str(world.user),
            async_job_id=str(world.job),
            celery_task_id=str(world.job),
            worker_node_id="worker:commit-regression@test",
            predict_mode=mode,
            **kwargs,
        )


def prediction_results(task_id):
    return [
        PredictionResult(
            task_id=str(task_id),
            result=[],
            model_version="new",
            meta={"total_cost": 0.25},
        )
        for _ in range(2)
    ]


async def cancel(world, *, pid=None):
    async with world.factory() as db:
        if pid is not None:
            pid.append(await db.scalar(text("SELECT pg_backend_pid()")))
        user = await db.get(User, world.user)
        return await cancel_async_job(
            world.job,
            Request({"type": "http", "method": "POST", "path": "/"}),
            db=db,
            current_user=user,
        )


async def test_model_selection_does_not_block_independent_health_settlement(
    managed_worker_world, monkeypatch
):
    from app.application import preannotation_execution
    from app.services.inference_topology_data import InferenceTopologyData

    factory = managed_worker_world.factory
    async with factory() as db:
        user = await create_user(
            db, "super_admin", f"health-{uuid.uuid4()}@example.test", "Health"
        )
        project = await create_project(db, owner_id=user.id)
        batch = await create_batch(db, project_id=project.id, status="active")
        backend = MLBackendRegistry(
            name="health-fixture", url="http://owned.invalid", state="connected"
        )
        db.add(backend)
        await db.flush()
        task = await create_task(db, project_id=project.id)
        task.batch_id = batch.id
        task.file_path = "http://owned.invalid/image.png"
        job = await jobs.create_job(
            db,
            kind="batch_predict",
            user_id=user.id,
            project_id=project.id,
            payload={
                "total_tasks": 1,
                "task_ids": [str(task.id)],
                "execution_scope": "bulk",
                "batch_id": str(batch.id),
            },
        )
        job.celery_task_id = str(job.id)
        await db.commit()
        world = SimpleNamespace(
            factory=factory,
            worker_world=managed_worker_world,
            user=user.id,
            project=project.id,
            batch=batch.id,
            backend=backend.id,
            task=task.id,
            job=job.id,
        )

    class HealthSettlingClient:
        async def predict(self, inputs, context=None):
            # The model response waits for a different owner's real row lock,
            # as a cold GPU result does before terminal health settlement.
            async with world.factory() as health:
                await health.execute(text("SET LOCAL lock_timeout = '500ms'"))
                await health.get(MLBackendRegistry, world.backend, with_for_update=True)
                await health.commit()
            return [
                PredictionResult(
                    task_id=inputs[0]["id"], result=[], model_version="new"
                )
            ]

    class SelectingExecutionPort(PredictionClientExecutionPort):
        def __init__(self, db):
            super().__init__(HealthSettlingClient(), instance_id=world.backend)
            self.db = db

        async def execute(self, request, *, before_dispatch=None):
            await InferenceTopologyData(self.db).lock_plugin_registry(world.backend)
            return await super().execute(request, before_dispatch=before_dispatch)

    monkeypatch.setattr(
        preannotation_execution,
        "_model_execution_port",
        lambda db, **kwargs: SelectingExecutionPort(db),
    )
    await run(world)
    async with world.factory() as db:
        job = await db.get(AsyncJob, world.job)
        failures = (
            await db.scalars(
                select(FailedPrediction).where(FailedPrediction.task_id == world.task)
            )
        ).all()
        if job.status != "completed":
            pytest.fail(
                (job.error_message or "prediction failed")
                + "\n"
                + "\n".join(row.message for row in failures)
            )
        assert not failures


@pytest.mark.parametrize("mode", ["overwrite", "append"])
@pytest.mark.parametrize(
    "failure",
    ["inference", "empty", "second_write", "sql_write", "scope", "cancel", "none"],
)
async def test_result_transaction_keeps_or_replaces_the_whole_task(
    commit_world, monkeypatch, mode, failure
):
    world = commit_world
    writes = 0
    original = PredictionService.create_from_ml_result

    async def write(service, *args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            if failure == "second_write":
                raise RuntimeError("second result rejected")
            if failure == "sql_write":
                await service.db.execute(text("SELECT 1 / 0"))
        return await original(service, *args, **kwargs)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def predict(self, tasks, context=None):
            assert tasks[0]["file_path"].startswith("http://ml-objects.test/")
            # Old candidates and accepted AI annotations must survive the long
            # inference period, even in overwrite mode.
            async with world.factory() as db:
                assert (
                    await db.scalar(
                        select(Prediction.id).where(Prediction.id == world.old)
                    )
                    == world.old
                )
                assert (
                    await db.scalar(
                        select(Annotation.is_active).where(
                            Annotation.task_id == world.task,
                            Annotation.source == "prediction_based",
                        )
                    )
                    is True
                )
                if failure == "scope":
                    task = await db.get(Task, world.task)
                    task.status = "review"
                    await db.commit()
            if failure == "cancel":
                response = await asyncio.wait_for(cancel(world), timeout=5)
                assert response["status"] == "cancel_requested"
            if failure == "inference":
                raise RuntimeError("inference failed")
            return [] if failure == "empty" else prediction_results(world.task)

    monkeypatch.setattr(
        "app.application.preannotation_execution._model_execution_port",
        lambda *_args, **_kwargs: PredictionClientExecutionPort(Client()),
    )
    monkeypatch.setattr(PredictionService, "create_from_ml_result", write)
    await run(world, predict_mode=mode)
    assert len(world.worker_world.captured) == 1
    async with world.factory() as db:
        job = await db.get(AsyncJob, world.job)
        task = await db.get(Task, world.task)
        batch = await db.get(TaskBatch, world.batch)
        predictions = list(
            (
                await db.scalars(
                    select(Prediction).where(Prediction.task_id == world.task)
                )
            ).all()
        )
        failures = list(
            (
                await db.scalars(
                    select(FailedPrediction).where(
                        FailedPrediction.task_id == world.task
                    )
                )
            ).all()
        )
        annotations = list(
            (
                await db.scalars(
                    select(Annotation).where(Annotation.task_id == world.task)
                )
            ).all()
        )
        if failure == "none":
            assert job.status == "completed"
            assert job.result["total_cost"] == "0.5000"
            assert job.result["success_count"] == 1
            assert (
                task.total_predictions
                == len(predictions)
                == (2 if mode == "overwrite" else 3)
            )
            assert not failures
            assert batch.status == "pre_annotated"
            assert next(
                a for a in annotations if a.source == "prediction_based"
            ).is_active == (mode == "append")
        else:
            assert [p.id for p in predictions] == [world.old]
            assert task.total_predictions == 1
            assert task.total_annotations == 2 and task.is_labeled
            assert all(a.is_active for a in annotations)
            assert (
                next(
                    a for a in annotations if a.source == "prediction_based"
                ).parent_prediction_id
                == world.old
            )
            assert task.annotation_contributor_ids == []
            assert job.result["total_cost"] == "0.0000"
            assert job.result["success_count"] == 0
            assert (
                job.result["failed_count"]
                == len(failures)
                == (0 if failure == "cancel" else 1)
            )
            assert job.status == ("cancelled" if failure == "cancel" else "failed")
            assert batch.status == "active"
        assert next(a for a in annotations if a.source == "manual").is_active
        assert await db.scalar(
            select(func.count())
            .select_from(PredictionMeta)
            .where(PredictionMeta.prediction_id.in_([p.id for p in predictions]))
        ) == len(predictions)


async def test_failure_rollback_does_not_expire_the_remaining_selection(
    commit_world, monkeypatch
):
    world = commit_world
    async with world.factory() as db:
        task = await create_task(db, project_id=world.project)
        task.batch_id = world.batch
        task.file_path = "http://owned.invalid/second.png"
        second_id = task.id
        await db.commit()
    calls = 0

    class Client:
        def __init__(self, backend, **kwargs):
            self.backend = backend

        async def predict(self, tasks, context=None):
            nonlocal calls
            calls += 1
            # The real transport reads capabilities from this ORM object on
            # every request; rollback must not leave it expired here.
            assert self.backend.state == "connected"
            if calls == 1:
                raise RuntimeError("first task failed")
            return prediction_results(tasks[0]["id"])

    monkeypatch.setattr(
        "app.application.preannotation_execution._model_execution_port",
        lambda *_args, **_kwargs: PredictionClientExecutionPort(
            Client(SimpleNamespace(state="connected"))
        ),
    )
    await run(world, task_ids=[str(world.task), str(second_id)])
    async with world.factory() as db:
        job = await db.get(AsyncJob, world.job)
        assert calls == 2
        assert job.status == "completed"
        assert job.result["success_count"] == job.result["failed_count"] == 1
        assert job.result["total_cost"] == "0.5000"


async def test_cancel_rechecks_terminal_state_after_waiting_for_final_lock(
    commit_world, monkeypatch
):
    world = commit_world
    final_locked, release_final = asyncio.Event(), asyncio.Event()
    original = jobs.mark_complete

    async def complete(db, job_id, **kwargs):
        final_locked.set()
        await release_final.wait()
        await original(db, job_id, **kwargs)

    async def pipeline(*args, **kwargs):
        return prediction_results(world.task), None, None

    monkeypatch.setattr(
        "app.application.preannotation_execution._run_task_pipeline", pipeline
    )
    monkeypatch.setattr(jobs, "mark_complete", complete)
    pending = asyncio.create_task(run(world))
    cancellation = None
    try:
        await asyncio.wait_for(final_locked.wait(), timeout=10)
        pid = []
        cancellation = asyncio.create_task(cancel(world, pid=pid))

        # Observe the actual PostgreSQL waiter, rather than assuming a timed
        # sleep establishes that the cancellation has reached its row lock.
        async def wait_for_lock():
            while True:
                if pid:
                    async with world.factory() as db:
                        waiting = await db.scalar(
                            text(
                                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                            ),
                            {"pid": pid[0]},
                        )
                    if waiting:
                        return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_lock(), timeout=10)
        release_final.set()
        await asyncio.wait_for(pending, timeout=10)
        with pytest.raises(HTTPException) as rejected:
            await asyncio.wait_for(cancellation, timeout=10)
        assert rejected.value.status_code == 409
        async with world.factory() as db:
            job = await db.get(AsyncJob, world.job)
            batch = await db.get(TaskBatch, world.batch)
            assert job.status == "completed"
            assert not job.payload.get("cancel_requested")
            assert batch.status == "pre_annotated"
    finally:
        release_final.set()
        await asyncio.gather(
            pending, *([cancellation] if cancellation else []), return_exceptions=True
        )


@pytest.mark.parametrize("terminal", [False, True])
async def test_cancel_between_claim_and_metadata_merge_is_not_lost(
    commit_world, monkeypatch, terminal
):
    from app.services.task_lock import TaskLockService

    world = commit_world
    # Capture request authority before injecting the worker admission race.
    await prepare(world)
    original = TaskLockService.active_locks
    cancelled = False

    async def locks(service, task_id):
        nonlocal cancelled
        if not cancelled:
            cancelled = True
            await asyncio.wait_for(cancel(world), timeout=5)
            if terminal:
                async with world.factory() as db:
                    await jobs.mark_cancelled(db, world.job)
                    await db.commit()
        return await original(service, task_id)

    async def unexpected(*args, **kwargs):
        pytest.fail("cancelled job reached inference")

    monkeypatch.setattr(TaskLockService, "active_locks", locks)
    monkeypatch.setattr(
        "app.application.preannotation_execution._run_task_pipeline", unexpected
    )
    await run(world, freeze=False)
    async with world.factory() as db:
        job = await db.get(AsyncJob, world.job)
        assert job.status == "cancelled"
        assert job.payload["cancel_requested"]
        assert (
            await db.scalar(select(Prediction.id).where(Prediction.id == world.old))
            == world.old
        )


async def test_late_failure_hook_honors_a_committed_cancellation(commit_world):
    world = commit_world
    async with world.factory() as db:
        await jobs.mark_running(db, world.job)
        await jobs.request_cancel(db, world.job)
        await db.commit()
    async with worker_engine(world.worker_world):
        await worker._mark_job_failed(str(world.job), "late worker failure")
    async with world.factory() as db:
        job = await db.get(AsyncJob, world.job)
        assert job.status == "cancelled"
        assert job.error_message is None
        assert list(
            (
                await db.scalars(
                    select(Notification.type).where(Notification.user_id == world.user)
                )
            ).all()
        ) == ["job.cancelled"]


@pytest.mark.parametrize("draft", [False, True])
@pytest.mark.parametrize("record_failure", [False, True])
async def test_explicit_partial_pipeline_commits_candidates_and_retry_context(
    commit_world, monkeypatch, draft, record_failure
):
    from app.services.prediction_execution import create_snapshot

    world = commit_world
    async with world.factory() as db:
        snapshot = await create_snapshot(
            db,
            world.project,
            {
                "stages": [
                    {
                        "stage": 7,
                        "parent_stage": None,
                        "ml_backend_id": str(world.backend),
                    }
                ],
                "prompt": None,
                "output_mode": "mask",
                "execution_scope": "bulk",
                "execution_unit": None,
                "box_threshold": 0.3,
                "text_threshold": 0.25,
                "predict_mode": "overwrite",
            },
        )
        snapshot_id = snapshot.id
        job = await db.get(AsyncJob, world.job)
        await jobs.bind_execution_snapshot(
            db, job, actor_id=world.user, snapshot_id=snapshot_id
        )
        if draft:
            batch = await db.get(TaskBatch, world.batch)
            batch.status = "draft"
        await db.commit()

    async def pipeline(*args, **kwargs):
        return (
            prediction_results(world.task),
            {"pipeline": {"partial": True, "stage_errors": [{"stage": 9}]}},
            None,
        )

    monkeypatch.setattr(
        "app.application.preannotation_execution._run_task_pipeline", pipeline
    )
    if record_failure:
        original = PredictionService.create_failed
        attempts = 0

        async def record(service, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("partial failure record rejected")
            return await original(service, **kwargs)

        monkeypatch.setattr(PredictionService, "create_failed", record)
    await run(world)
    async with world.factory() as db:
        job = await db.get(AsyncJob, world.job)
        failure = await db.scalar(
            select(FailedPrediction).where(FailedPrediction.task_id == world.task)
        )
        prediction_ids = set(
            (
                await db.scalars(
                    select(Prediction.id).where(Prediction.task_id == world.task)
                )
            ).all()
        )
        if record_failure:
            assert prediction_ids == {world.old}
            assert failure.error_type == "RuntimeError"
            assert "partial_prediction_ids" not in failure.extra
            assert (await db.get(Task, world.task)).total_predictions == 1
            assert list(
                (
                    await db.scalars(
                        select(Annotation.is_active).where(
                            Annotation.task_id == world.task
                        )
                    )
                ).all()
            ) == [True, True]
        else:
            assert world.old not in prediction_ids
            assert len(prediction_ids) == 2
            assert set(failure.extra["partial_prediction_ids"]) == {
                str(v) for v in prediction_ids
            }
        assert failure.execution_snapshot_id == snapshot_id
        assert job.result["failed_prediction_ids"] == [str(failure.id)]
        assert job.result["success_count"] == 0 and job.result["failed_count"] == 1
        assert job.result["total_cost"] == ("0.0000" if record_failure else "0.5000")
        assert (await db.get(TaskBatch, world.batch)).status == (
            "draft" if draft else "active"
        )


@pytest.mark.parametrize("mode", ["overwrite", "append"])
async def test_frame_fanout_preserves_its_existing_reset_contract(
    commit_world, monkeypatch, mode
):
    world = commit_world
    # Frame fan-out is a video contract, separate from owned image execution.
    async with world.factory() as db:
        project = await db.get(Project, world.project)
        project.type_key, project.type_label = "video-track", "视频目标跟踪"
        project.data_type = "video"
        task = await db.get(Task, world.task)
        task.file_type, task.file_name = "video", "frames.mp4"
        task.file_path = "videos/frames.mp4"
        await db.commit()
    calls = []

    async def dispatch(db, **kwargs):
        async with world.factory() as observer:
            count = await observer.scalar(
                select(func.count())
                .select_from(Prediction)
                .where(Prediction.task_id == world.task)
            )
            assert count == (0 if mode == "overwrite" else 1)
        calls.append(kwargs)
        return {"dispatched": True, "total_frames": 2}

    monkeypatch.setattr(
        "app.workers.frame_preannotate.dispatch_frame_preannotate", dispatch
    )
    await run(world, predict_mode=mode, execution_unit="frame")
    assert len(calls) == 1


async def test_cancel_waiting_for_candidate_commit_retains_that_task_and_stops_next(
    commit_world, monkeypatch
):
    world = commit_world
    async with world.factory() as db:
        second = await create_task(db, project_id=world.project)
        second.batch_id = world.batch
        second_id = second.id
        await db.commit()
    result_locked, release_result = asyncio.Event(), asyncio.Event()
    original = PredictionService.create_from_ml_result
    inferred = []

    async def write(service, **kwargs):
        result_locked.set()
        await release_result.wait()
        return await original(service, **kwargs)

    async def pipeline(task, *args, **kwargs):
        inferred.append(task.id)
        return prediction_results(task.id), None, None

    monkeypatch.setattr(PredictionService, "create_from_ml_result", write)
    monkeypatch.setattr(
        "app.application.preannotation_execution._run_task_pipeline", pipeline
    )
    pending = asyncio.create_task(
        run(world, task_ids=[str(world.task), str(second_id)])
    )
    cancellation = None
    try:
        await asyncio.wait_for(result_locked.wait(), timeout=10)
        pid = []
        cancellation = asyncio.create_task(cancel(world, pid=pid))

        async def waiting():
            while True:
                if pid:
                    async with world.factory() as db:
                        locked = await db.scalar(
                            text(
                                "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                            ),
                            {"pid": pid[0]},
                        )
                    if locked:
                        return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(waiting(), timeout=10)
        release_result.set()
        response = await asyncio.wait_for(cancellation, timeout=10)
        assert response["status"] == "cancel_requested"
        await asyncio.wait_for(pending, timeout=10)
        async with world.factory() as db:
            job = await db.get(AsyncJob, world.job)
            assert len(inferred) == 1
            assert job.status == "cancelled"
            assert job.result["success_count"] == job.result["done_count"] == 1
            assert job.result["failed_count"] == 0
            assert job.result["skipped_count"] == 1
            assert job.result["total_cost"] == "0.5000"
            assert (await db.get(Task, inferred[0])).total_predictions == 2
            assert (await db.get(TaskBatch, world.batch)).status == "active"
    finally:
        release_result.set()
        await asyncio.gather(
            pending, *([cancellation] if cancellation else []), return_exceptions=True
        )
