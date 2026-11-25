"""Committed managed-image worker inputs with controlled physical I/O only.

Import ``input_world`` and ``managed_worker_world`` into a test module to expose
both fixtures. All sessions commit to input_world's per-test migrated database.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace
import uuid

import pytest
from starlette.requests import Request

from app.config import settings
from app.core.security import create_access_token
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.prediction_execution import PredictionExecutionSnapshot
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.deps import bearer_scheme, get_current_principal
from app.services import async_job as jobs
from app.services import prediction_originals as originals
from app.services import prediction_references as refs
from app.services.identity_gateway import IdentityGateway
from app.services.prediction_execution import create_snapshot, load_snapshot
from tests.inference_execution_test_ports import run_batch_request
from tests.test_managed_prediction_requests import managed_task
from tests.test_prediction_inputs import input_world as input_world


@pytest.fixture
def managed_worker_world(input_world, monkeypatch):
    """Keep committed worker inputs visible to independent guard sessions."""
    world = SimpleNamespace(
        factory=input_world.factory,
        source_bytes={},
        captured=[],
        monkeypatch=monkeypatch,
    )
    world.factory.configure(info={"managed_worker_world": world})

    def capture(spec, maximum, seconds, deadline, stop, release, **kwargs):
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
    monkeypatch.setattr(
        "app.services.prediction_progress.publish_progress",
        lambda *args, **kwargs: None,
    )
    return world


async def admitted_jwt_request(db, actor):
    token = create_access_token(subject=str(actor.id), role=actor.role)
    request = Request(
        {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]}
    )
    await get_current_principal(
        request, await bearer_scheme(request), db, IdentityGateway()
    )
    return request


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
    request = await admitted_jwt_request(db, actor)
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
    task_ids = [str(task.id) for task in tasks]
    if job is None:
        job = await jobs.create_job(
            db,
            kind="batch_predict",
            user_id=actor.id,
            project_id=project.id,
            payload={
                "execution_scope": scope,
                "batch_id": str(batch_id) if batch_id else None,
                "task_ids": task_ids,
            },
        )
    await jobs.bind_execution_snapshot(
        db, job, actor_id=actor.id, snapshot_id=snapshot.id
    )
    job.celery_task_id = str(job.id)
    job.payload = {
        **(job.payload or {}),
        "prediction_authority": proof,
        "scope": {"task_ids": task_ids},
    }
    await db.commit()
    return job


@asynccontextmanager
async def worker_engine(world):
    """Point the business worker at the case DB without replacing executor logins."""
    url = world.factory.kw["bind"].url.render_as_string(hide_password=False)

    with world.monkeypatch.context() as patch:
        patch.setattr(settings, "database_url", url)
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
    from yoao_task_contract import normalize_task_fields

    semantic = normalize_task_fields(
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
        await run_batch_request(
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
