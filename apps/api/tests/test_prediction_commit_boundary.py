"""Committed worker transactions: replacement, multi-result failure and cancellation.

These fixtures deliberately use independent connections, not the API test
SAVEPOINT. An inference callback can commit a real concurrent cancellation or
scope change; worker rollback must not undo either operation.
"""

import asyncio
import hashlib
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select, text

from yoao_assets_images.contract import ObjectIdentity
from yoao_assets_images_runtime.storage import S3ImageStorage
from yoao_assets_input_manifests import contract as manifests
from yoao_assets_inputs import contract as inputs
from yoao_assets_inputs_runtime.storage import S3InputStorage
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
from app.deps import get_job_cancellation_port, get_job_execution_control
from app.services import async_job as jobs
from app.services.accepted_input_manifests import get_input_manifest_capture
from yoao_inference.backend_transport import PredictionResult
from app.services.labeling_prediction_contract import ApplyPredictionResultRequest
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.application.preannotation_results import BatchResultExecution
from app.services.prediction import PredictionService
from app.workers import tasks as worker
from tests.factory import create_batch, create_project, create_task, create_user
from tests.inference_execution_test_ports import (
    PredictionClientExecutionPort,
    pipeline_output,
)
from tests.managed_prediction_worker_fixtures import (
    freeze_job,
    input_world as input_world,
    managed_worker_world as managed_worker_world,
    worker_engine,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def retained_image_storage(managed_worker_world, monkeypatch):
    """Substitute physical bytes only; capture, preparation and sealing stay Native."""
    blobs = {}

    def source_bytes(source):
        return {
            key: raw for (_, key), raw in managed_worker_world.source_bytes.items()
        }[source.file_path]

    async def head_source(_storage, source):
        raw = source_bytes(source)
        return ObjectIdentity(etag=hashlib.sha256(raw).hexdigest(), byte_size=len(raw))

    async def download_source(storage, source, destination):
        raw = source_bytes(source)
        identity = await head_source(storage, source)
        assert identity == source.object_identity
        destination.write_bytes(raw)
        return SimpleNamespace(
            identity=identity,
            byte_size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )

    async def upload_blob(_storage, path, key, *, sha256, byte_size):
        raw = path.read_bytes()
        assert len(raw) == byte_size and hashlib.sha256(raw).hexdigest() == sha256
        blob = inputs.InputBlob(
            key=key,
            version_id=str(uuid.uuid4()),
            etag=sha256,
            sha256=sha256,
            byte_size=byte_size,
        )
        blobs[key, blob.version_id] = blob, raw
        return blob

    async def verify_blob(_storage, blob):
        recorded, raw = blobs[blob.key, blob.version_id]
        assert recorded == blob
        assert hashlib.sha256(raw).hexdigest() == blob.sha256

    async def download_blob(storage, blob, destination):
        await verify_blob(storage, blob)
        destination.write_bytes(blobs[blob.key, blob.version_id][1])

    async def sign_blob(storage, blob, *, expires_in=900):
        await verify_blob(storage, blob)
        return f"http://retained.test/{blob.key}?versionId={blob.version_id}"

    monkeypatch.setattr(S3ImageStorage, "head_source", head_source)
    monkeypatch.setattr(S3ImageStorage, "download_source", download_source)
    monkeypatch.setattr(S3InputStorage, "assert_versioning", AsyncMock())
    monkeypatch.setattr(S3InputStorage, "upload_blob", upload_blob)
    monkeypatch.setattr(S3InputStorage, "verify_blob", verify_blob)
    monkeypatch.setattr(S3InputStorage, "download_blob", download_blob)
    monkeypatch.setattr(S3InputStorage, "sign_blob", sign_blob)


@pytest.fixture
async def commit_world(managed_worker_world, retained_image_storage, monkeypatch):
    factory = managed_worker_world.factory
    async with factory() as db:
        user = await create_user(
            db, "super_admin", f"commit-{uuid.uuid4()}@example.test", "Commit"
        )
        project = await create_project(db, owner_id=user.id)
        batch = await create_batch(db, project_id=project.id, status="active")
        backend = MLBackendRegistry(
            name="commit-fixture",
            url=f"http://commit-{uuid.uuid4().hex}.invalid",
            state="connected",
        )
        db.add(backend)
        await db.flush()
        task = await create_task(db, project_id=project.id)
        task.batch_id = batch.id
        task.file_path = "http://owned.invalid/image.png"
        await db.flush()
        await db.refresh(task)
        old = await PredictionService(db).apply_prediction_result(
            ApplyPredictionResultRequest(
                task_id=task.id,
                project_id=project.id,
                ml_backend_id=backend.id,
                result=[],
                model_version="old",
                write_scope=capture_labeling_write_scope(
                    task,
                    actor_id=user.id,
                    actor_known=True,
                    operation="prediction_import",
                    execution_scope=None,
                    execution_scope_known=True,
                    job_id=None,
                    job_id_known=True,
                ),
            )
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
        job = await freeze_job(
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
        selection = manifests.ManifestSelection(
            task_ids=sorted(
                [uuid.UUID(value) for value in (task_ids or [str(world.task)])],
                key=str,
            ),
            batch_id=world.batch,
            predict_mode=mode,
        )
        job.payload = {
            **job.payload,
            "task_ids": [str(value) for value in selection.task_ids],
            "scope": {"task_ids": [str(value) for value in selection.task_ids]},
            "accepted_input_manifest_protocol": manifests.MANIFEST_SCHEMA,
            "accepted_input_manifest_id": str(job.id),
            "input_selection": selection.model_dump(mode="json"),
        }
        world.manifest = await get_input_manifest_capture()(db).capture(
            manifests.CaptureManifestRequest(
                job_id=job.id,
                actor_id=world.user,
                project_id=world.project,
                selection=selection,
            )
        )
        job.payload = {
            **job.payload,
            "accepted_input_manifest_digest": world.manifest.selection_digest,
            "total_tasks": world.manifest.selected_count,
            "input_candidate_count": world.manifest.candidate_count,
        }
        await db.commit()


async def run(world, **kwargs):
    from app.application.preannotation_contract import BatchPredictionRequest
    from app.application.preannotation_execution import run_preannotation

    task_ids = kwargs.pop("task_ids", [str(world.task)])
    mode = kwargs.pop("predict_mode", "overwrite")
    if kwargs.pop("freeze", True) and kwargs.get("execution_unit") != "frame":
        await prepare(world, task_ids=task_ids, mode=mode)
    if kwargs.get("execution_unit") != "frame":
        kwargs.update(
            accepted_input_manifest_protocol=manifests.MANIFEST_SCHEMA,
            accepted_input_manifest_id=str(world.manifest.job_id),
            accepted_input_manifest_digest=world.manifest.selection_digest,
        )
    async with worker_engine(world.worker_world):
        await run_preannotation(
            BatchPredictionRequest(
                project_id=str(world.project),
                ml_backend_id=str(world.backend),
                task_ids=task_ids,
                batch_id=str(world.batch),
                user_id=str(world.user),
                async_job_id=str(world.job),
                predict_mode=mode,
                **kwargs,
            ),
            execution_id=str(world.job),
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
        response = await cancel_async_job(
            world.job,
            db=db,
            actor_id=world.user,
            cancellation=get_job_cancellation_port(db, get_job_execution_control()),
        )
        return response.model_dump(mode="json")


async def test_model_selection_does_not_block_independent_health_settlement(
    managed_worker_world, retained_image_storage, monkeypatch
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
            name="health-fixture",
            url=f"http://health-{uuid.uuid4().hex}.invalid",
            state="connected",
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
    inferences = 0
    original = PredictionService._persist_prediction_result

    async def write(service, *args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            if failure == "second_write":
                raise RuntimeError("second result rejected")
            if failure == "sql_write":
                await service.db.execute(text("SELECT 1 / 0"))
        return await original(service, *args, **kwargs)

    async def pipeline(_task, *args, **kwargs):
        nonlocal inferences
        inferences += 1
        async with world.factory() as db:
            assert (
                await db.scalar(select(Prediction.id).where(Prediction.id == world.old))
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
        return pipeline_output(
            [] if failure == "empty" else prediction_results(world.task),
            instance_id=world.backend,
        )

    monkeypatch.setattr(
        "app.application.preannotation_execution._run_task_pipeline", pipeline
    )
    monkeypatch.setattr(PredictionService, "_persist_prediction_result", write)
    if failure == "sql_write":
        from app.application.preannotation_results import PreannotationResultDeferred

        with pytest.raises(PreannotationResultDeferred, match="storage_unavailable"):
            await run(world, predict_mode=mode)
        async with world.factory() as db:
            assert list(
                await db.scalars(
                    select(Prediction.id).where(Prediction.task_id == world.task)
                )
            ) == [world.old]
            assert list(
                await db.scalars(
                    select(Annotation.is_active).where(Annotation.task_id == world.task)
                )
            ) == [True, True]
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(FailedPrediction)
                    .where(FailedPrediction.task_id == world.task)
                )
                == 0
            )
            assert (await db.get(AsyncJob, world.job)).status == "running"
        await run(world, predict_mode=mode, freeze=False)
    else:
        await run(world, predict_mode=mode)
    assert inferences == 1
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
        if failure in {"none", "sql_write"}:
            assert job.status == "completed", [item.message for item in failures]
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
        async def predict(self, tasks, context=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("first task failed")
            return [
                PredictionResult(
                    task_id=tasks[0]["id"],
                    result=[],
                    model_version="new",
                    meta={"total_cost": 0.5},
                )
            ]

    monkeypatch.setattr(
        "app.application.preannotation_execution._model_execution_port",
        lambda *_args, **_kwargs: PredictionClientExecutionPort(Client()),
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
    original = BatchResultExecution.call

    async def complete(execution, operation, **kwargs):
        result = await original(execution, operation, **kwargs)
        if (
            operation == "finish_batch_result_execution"
            and kwargs.get("status") == "completed"
        ):
            final_locked.set()
            await release_final.wait()
        return result

    async def pipeline(*args, **kwargs):
        return pipeline_output(
            prediction_results(world.task), instance_id=world.backend
        )

    monkeypatch.setattr(
        "app.application.preannotation_execution._run_task_pipeline", pipeline
    )
    monkeypatch.setattr(BatchResultExecution, "call", complete)
    pending = asyncio.create_task(run(world))
    cancellation = None
    try:
        await asyncio.wait_for(final_locked.wait(), timeout=10)
        pid = []
        cancellation = asyncio.create_task(cancel(world, pid=pid))

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
        return pipeline_output(
            prediction_results(world.task),
            instance_id=world.backend,
            extra={"pipeline": {"partial": True, "stage_errors": [{"stage": 9}]}},
        )

    monkeypatch.setattr(
        "app.application.preannotation_execution._run_task_pipeline", pipeline
    )
    if record_failure:
        original = BatchResultExecution.call
        attempts = 0

        async def record(execution, operation, **kwargs):
            nonlocal attempts
            if operation == "finish_batch_result_write":
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("partial failure record rejected")
            return await original(execution, operation, **kwargs)

        monkeypatch.setattr(BatchResultExecution, "call", record)
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


async def test_partial_completion_rejects_foreign_candidate_ids(
    commit_world, monkeypatch
):
    from yoao_preannotation_results.contract import BatchResultFailure

    world = commit_world
    original = PredictionService.create_failed

    async def pipeline(*args, **kwargs):
        return pipeline_output(
            prediction_results(world.task),
            instance_id=world.backend,
            extra={"pipeline": {"partial": True, "stage_errors": [{"stage": 9}]}},
        )

    async def tamper(service, *args, **kwargs):
        failure = await original(service, *args, **kwargs)
        failure.extra = {**failure.extra, "partial_prediction_ids": [str(world.old)]}
        await service.db.flush()
        return failure

    monkeypatch.setattr(
        "app.application.preannotation_execution._run_task_pipeline", pipeline
    )
    monkeypatch.setattr(PredictionService, "create_failed", tamper)
    with pytest.raises(BatchResultFailure) as rejected:
        await run(world)
    assert rejected.value.code == "result_conflict"
    async with world.factory() as db:
        assert list(
            await db.scalars(
                select(Prediction.id).where(Prediction.task_id == world.task)
            )
        ) == [world.old]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(FailedPrediction)
                .where(FailedPrediction.task_id == world.task)
            )
            == 0
        )
        assert list(
            await db.scalars(
                select(Annotation.is_active).where(Annotation.task_id == world.task)
            )
        ) == [True, True]


@pytest.mark.parametrize("mode", ["overwrite", "append"])
async def test_frame_fanout_preserves_its_existing_reset_contract(
    managed_worker_world, monkeypatch, mode
):
    from yoao_assets_inputs_runtime.storage import S3InputStorage
    from app.application.preannotation_contract import BatchPredictionRequest
    from app.application.preannotation_execution import run_preannotation
    from app.workers.frame_preannotate import JobsFramePreannotationDispatcher
    from tests import test_video_input_ledger_execution as video_seed
    from tests.test_video_input_ledger_lifecycle import _ready_plan

    world = managed_worker_world
    original_selection = video_seed._selection
    monkeypatch.setattr(
        video_seed,
        "_selection",
        lambda **kwargs: {**original_selection(**kwargs), "predict_mode": mode},
    )
    async with world.factory() as db:
        actor_id, project_id, plan = await _ready_plan(db, count=2)
        task = await db.get(Task, plan.input.task.task_id)
        backend = MLBackendRegistry(
            name=f"frame-commit-{uuid.uuid4().hex}",
            url=f"http://frame-{uuid.uuid4().hex}.invalid",
            state="connected",
        )
        db.add(backend)
        await db.flush()
        await PredictionService(db).apply_prediction_result(
            ApplyPredictionResultRequest(
                task_id=task.id,
                project_id=project_id,
                ml_backend_id=backend.id,
                result=[],
                model_version="old",
                write_scope=capture_labeling_write_scope(
                    task,
                    actor_id=actor_id,
                    actor_known=True,
                    operation="prediction_import",
                    execution_scope=None,
                    execution_scope_known=True,
                    job_id=None,
                    job_id_known=True,
                ),
            )
        )
        job = await db.get(AsyncJob, plan.input.job_id)
        job.celery_task_id = str(job.id)
        payload = job.payload
        await db.commit()
        task_id, job_id, backend_id = task.id, job.id, backend.id

    async def head_source(_storage, source):
        assert source.dataset_item_id == plan.input.source.dataset_item_id
        assert source.source_snapshot == plan.input.source.source_snapshot
        return plan.input.source.object_identity

    async def verify_blob(_storage, blob):
        assert blob == plan.input.blob

    monkeypatch.setattr(S3InputStorage, "head_source", head_source)
    monkeypatch.setattr(S3InputStorage, "verify_blob", verify_blob)
    monkeypatch.setattr("app.workers.frame_preannotate._init_progress", lambda *a: None)
    calls = []

    async def publish(envelope):
        async with world.factory() as observer:
            count = await observer.scalar(
                select(func.count())
                .select_from(Prediction)
                .where(Prediction.task_id == task_id)
            )
            assert count == (0 if mode == "overwrite" else 1)
        calls.append(envelope)

    monkeypatch.setattr("app.services.jobs_queue.publish_queue_envelope", publish)
    request = BatchPredictionRequest(
        project_id=str(project_id),
        ml_backend_id=str(backend_id),
        task_ids=[str(task_id)],
        user_id=str(actor_id),
        async_job_id=str(job_id),
        predict_mode=mode,
        execution_scope="bulk",
        execution_unit="frame",
        accepted_video_inputs_protocol=payload["accepted_video_inputs_protocol"],
        accepted_video_manifest_id=payload["accepted_video_manifest_id"],
        accepted_video_selection_digest=payload["accepted_video_selection_digest"],
        accepted_video_selection=payload["accepted_video_selection"],
    )
    async with worker_engine(world):
        await run_preannotation(
            request,
            execution_id=str(job_id),
            frame_dispatcher=JobsFramePreannotationDispatcher(),
        )
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
    original = PredictionService._persist_prediction_result
    inferred = []

    async def write(service, *args, **kwargs):
        result_locked.set()
        await release_result.wait()
        return await original(service, *args, **kwargs)

    async def pipeline(task, *args, **kwargs):
        inferred.append(task.id)
        return pipeline_output(prediction_results(task.id), instance_id=world.backend)

    monkeypatch.setattr(PredictionService, "_persist_prediction_result", write)
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
            assert job.result["success_count"] == 1
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
