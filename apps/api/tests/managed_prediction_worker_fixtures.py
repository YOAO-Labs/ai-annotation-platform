"""Committed managed-image worker inputs with controlled physical I/O only.

Import ``input_world`` and ``managed_worker_world`` into a test module to expose
both fixtures. Business rows use the migrated test schema; independent input
owners and permits use input_world's disposable schema. Fresh credential/source
checks and transaction-bound prediction receipts remain production code.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.notification import Notification
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.prediction_execution import PredictionExecutionSnapshot
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services import async_job as jobs
from app.services import prediction_originals as originals
from app.services import prediction_references as refs
from app.services.prediction_execution import create_snapshot, load_snapshot
from app.services.dataset_assets import ensure_legacy_bindings
from app.workers import tasks as worker
from tests.test_managed_prediction_requests import managed_task
from tests.test_prediction_inputs import input_world as input_world


@pytest.fixture
async def managed_worker_world(input_world, test_engine, monkeypatch):
    """Own committed business fixtures and keep every real guard session visible."""
    world = SimpleNamespace(
        factory=input_world.factory,
        source_bytes={},
        captured=[],
        projects=set(),
        datasets=set(),
        actors=set(),
        backends=set(),
        monkeypatch=monkeypatch,
    )
    world.factory.configure(info={"managed_worker_world": world})
    # Canonical AssetLocation FKs point at the migrated business schema's
    # platform location; source/target permits remain in the private schema.
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
        await ensure_legacy_bindings(db)
        await db.commit()

    def track(session, row):
        if session.info.get("managed_worker_world") is not world:
            return
        for model, collection in (
            (Project, world.projects),
            (Dataset, world.datasets),
            (User, world.actors),
            (MLBackendRegistry, world.backends),
        ):
            if isinstance(row, model):
                collection.add(row.id)

    event.listen(Session, "pending_to_persistent", track)

    def capture(spec, maximum, seconds, deadline, stop, release, **kwargs):
        # The native-reader boundary is the only substituted original read.
        # Resolution, saved locator/digest validation and durable permits run.
        raw = world.source_bytes[spec.bucket, spec.object_key]
        assert len(raw) == spec.size_bytes <= maximum
        assert hashlib.sha256(raw).hexdigest() == spec.sha256
        assert not stop.is_set()
        world.captured.append(spec)
        return originals.reader.BinaryStage(
            io.BytesIO(raw),
            spec.sha256,
            len(raw),
            {"size": len(raw)},
            _release=release,
            _retain_on_close_error=True,
        )

    monkeypatch.setattr(originals.reader, "_capture", capture)
    monkeypatch.setattr(worker, "_publish_progress", lambda *args, **kwargs: None)
    try:
        yield world
    finally:
        event.remove(Session, "pending_to_persistent", track)
        async with world.factory() as db:
            predictions = select(Prediction.id).where(
                Prediction.project_id.in_(world.projects)
            )
            failures = select(FailedPrediction.id).where(
                FailedPrediction.project_id.in_(world.projects)
            )
            await db.execute(
                delete(PredictionMeta).where(
                    PredictionMeta.prediction_id.in_(predictions)
                    | PredictionMeta.failed_prediction_id.in_(failures)
                )
            )
            for model in (Annotation, Prediction, FailedPrediction, AsyncJob):
                await db.execute(
                    delete(model).where(model.project_id.in_(world.projects))
                )
            await db.execute(
                delete(PredictionExecutionSnapshot).where(
                    PredictionExecutionSnapshot.project_id.in_(world.projects)
                )
            )
            await db.execute(delete(Task).where(Task.project_id.in_(world.projects)))
            await db.execute(
                delete(TaskBatch).where(TaskBatch.project_id.in_(world.projects))
            )
            await db.execute(delete(Project).where(Project.id.in_(world.projects)))
            await db.execute(delete(Dataset).where(Dataset.id.in_(world.datasets)))
            await db.execute(
                delete(Notification).where(Notification.user_id.in_(world.actors))
            )
            await db.execute(
                delete(MLBackendRegistry).where(
                    MLBackendRegistry.id.in_(world.backends)
                )
            )
            await db.execute(delete(User).where(User.id.in_(world.actors)))
            await db.commit()
        world.source_bytes.clear()


async def freeze_job(
    db,
    *,
    project,
    actor,
    backend,
    tasks,
    job=None,
    batch_id=None,
    predict_mode="overwrite",
    scope="bulk",
    config=None,
):
    """Mint real canonical facts, execution snapshot and original JWT authority.

    Return the pending AsyncJob. No inference or physical source PUT occurs.
    A configured managed_worker_world serves the exact registered source bytes.
    Existing snapshots are retained when config is omitted, including stage IDs.
    """
    world = db.info["managed_worker_world"]
    for task in tasks:
        if task.dataset_item_id is None and task.original_id is None:
            member = await managed_task(db, project, actor, task=task)
            source = await originals.resolve(db, task.id)
            world.source_bytes[source.bucket, source.object_key] = member.raw
    if config is None and job is not None and job.execution_snapshot_id:
        payload, _ = await load_snapshot(db, job.execution_snapshot_id, project.id)
        payload = {**payload, "predict_mode": predict_mode}
    else:
        payload = {
            "stages": [
                {"stage": 0, "parent_stage": None, "ml_backend_id": str(backend.id)}
            ],
            "prompt": None,
            "output_mode": "mask",
            "execution_scope": scope,
            "execution_unit": None,
            "box_threshold": float(project.box_threshold),
            "text_threshold": float(project.text_threshold),
            "predict_mode": predict_mode,
            **deepcopy(config or {}),
        }
    snapshot = None
    if job is not None and job.execution_snapshot_id:
        previous, _ = await load_snapshot(db, job.execution_snapshot_id, project.id)
        if previous == payload:
            snapshot = await db.get(
                PredictionExecutionSnapshot, job.execution_snapshot_id
            )
    snapshot = snapshot or await create_snapshot(db, project.id, payload)
    token = create_access_token(subject=str(actor.id), role=actor.role)
    request = SimpleNamespace(
        state=SimpleNamespace(api_key_id=None, api_key_scopes=None),
        headers={"authorization": "Bearer " + token},
    )
    proof = await refs.capture_authority(
        db,
        project,
        actor,
        request,
        tasks,
        snapshot,
        scope=scope,
        batch_id=batch_id,
        predict_mode=predict_mode,
    )
    if job is None:
        job = await jobs.create_job(
            db,
            kind="batch_predict",
            user_id=actor.id,
            project_id=project.id,
            payload={},
        )
    job.execution_snapshot_id = snapshot.id
    job.celery_task_id = str(job.id)
    job.payload = {
        **(job.payload or {}),
        "prediction_authority": proof,
        "scope": {"task_ids": [str(task.id) for task in tasks]},
    }
    await db.commit()
    return job


@asynccontextmanager
async def worker_engine(world):
    """Redirect only worker construction; its sessions are real and independent."""
    import sqlalchemy.ext.asyncio as sa_async

    class Engine:
        async def dispose(self):
            pass

    with world.monkeypatch.context() as patch:
        patch.setattr(sa_async, "create_async_engine", lambda *args, **kw: Engine())
        patch.setattr(sa_async, "async_sessionmaker", lambda *args, **kw: world.factory)
        yield


async def run_managed_batch(db, *, before_worker=None, **kwargs):
    """Prepare a qualified pending job, optionally change scope, then run Worker."""
    world = db.info["managed_worker_world"]
    project = await db.get(Project, uuid.UUID(kwargs["project_id"]))
    backend = await db.get(MLBackendRegistry, uuid.UUID(kwargs["ml_backend_id"]))
    actor = await db.get(
        User, uuid.UUID(kwargs.get("user_id") or str(project.owner_id))
    )
    tasks = [await db.get(Task, uuid.UUID(value)) for value in kwargs["task_ids"]]
    semantic = worker.normalize_task_fields(
        {key: kwargs.get(key) for key in ("task", "task_type", "prompt_type")}
    )
    stage = {
        "stage": 0,
        "parent_stage": None,
        "ml_backend_id": str(backend.id),
        **semantic,
        **{
            key: kwargs[key]
            for key in ("model_id", "params", "model_variants", "class_filter")
            if key in kwargs
        },
    }
    job = await freeze_job(
        db,
        project=project,
        actor=actor,
        backend=backend,
        tasks=tasks,
        batch_id=kwargs.get("batch_id"),
        predict_mode=kwargs.get("predict_mode", "skip_predicted"),
        scope=kwargs.get("execution_scope", "bulk"),
        config={
            "stages": kwargs.get("pipeline_stages") or [stage],
            "prompt": kwargs.get("prompt"),
            "output_mode": kwargs.get("output_mode", "mask"),
        },
    )
    if before_worker is not None:
        await before_worker()
        await db.commit()
    async with worker_engine(world):
        await worker._run_batch(
            **{
                **kwargs,
                "user_id": str(actor.id),
                "async_job_id": str(job.id),
                "celery_task_id": str(job.id),
                "worker_node_id": kwargs.get("worker_node_id")
                or "worker:managed-regression@test",
            }
        )
    return job.id
