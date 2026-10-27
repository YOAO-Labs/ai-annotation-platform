"""Published detectors freeze per stage and replay a failed task's whole pipeline."""

import copy
import hashlib
import io
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import Mock

import pytest
from PIL import Image
from sqlalchemy import delete, select

from app.db.models.async_job import AsyncJob
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.prediction_execution import PredictionExecutionSnapshot
from app.db.models.trained_model import TrainedModelVersion, ModelDeployment
from aap_inference.backend_transport import PredictionResult
from app.services import trained_models
from tests.factory import create_task, create_membership
from tests.inference_execution_test_ports import PredictionClientExecutionPort
from tests.managed_prediction_worker_fixtures import (
    freeze_job,
    input_world as input_world,
    managed_worker_world as managed_worker_world,
)
from tests.test_managed_prediction_requests import managed_task
from tests.test_trained_models import (
    controlled_artifacts as controlled_artifacts,
    seed_source,
    register,
    ready_deployment,
    publish,
    headers,
    mock_live_receipts,
    next_successful_attempt,
)


async def seed_published(db, client, admin, monkeypatch):
    owner, token = admin
    project, run, attempt = await seed_source(db, owner)
    record, _ = await register(client, token, project, run, attempt)
    version = await db.get(TrainedModelVersion, uuid.UUID(record["id"]))
    backend, deployment = await ready_deployment(db, project, version)
    mock_live_receipts(monkeypatch, db)
    await publish(client, token, project, version, deployment, 0)
    return project, backend, version, deployment, run, attempt


@pytest.fixture
async def trained_retry_world(managed_worker_world, app_module):
    """Commit only these retry cases while preserving the ordinary test fixtures."""
    import httpx

    from app.deps import get_db
    from app.db.models.trained_model import ModelRelease, ProjectModelSelection
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.training_run import TrainingRun
    from tests.conftest import _create_user

    world = managed_worker_world
    missing = object()
    previous = app_module.dependency_overrides.get(get_db, missing)
    try:
        async with world.factory() as db:
            admin = await _create_user(
                db,
                "super_admin",
                f"trained-retry-{uuid.uuid4().hex}@test.local",
                "Trained retry owner",
            )

            async def override():
                yield db

            app_module.dependency_overrides[get_db] = override
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app_module),
                    base_url="http://test",
                ) as client:
                    yield SimpleNamespace(db=db, client=client, admin=admin)
            finally:
                if previous is missing:
                    app_module.dependency_overrides.pop(get_db, None)
                else:
                    app_module.dependency_overrides[get_db] = previous
                await db.rollback()
    finally:
        # Model owners RESTRICT project deletion. Remove only this world's
        # training fixtures before its general business-row cleanup runs.
        async with world.factory() as db:
            versions = select(TrainedModelVersion.id).where(
                TrainedModelVersion.project_id.in_(world.projects)
            )
            for model in (ProjectModelSelection, ModelRelease):
                await db.execute(
                    delete(model).where(model.project_id.in_(world.projects))
                )
            await db.execute(
                delete(ModelDeployment).where(
                    ModelDeployment.model_version_id.in_(versions)
                )
            )
            for model in (TrainedModelVersion, TrainingRun, TrainingDatasetVersion):
                await db.execute(
                    delete(model).where(model.project_id.in_(world.projects))
                )
            await db.commit()


def pipeline_body(backend, task):
    return {
        "ml_backend_id": str(backend.id),
        "model_id": "project-release",
        "task": "detect",
        "model_variants": {},
        "task_ids": [str(task.id)],
        "pipeline_stages": [
            {
                "stage": 7,
                "parent_stage": None,
                "ml_backend_id": str(backend.id),
                "model_id": "project-release",
                "task": "detect",
                "model_variants": {},
                "write": {"target": "geometry"},
                "input": {"mode": "full_image"},
            },
            {
                "stage": 9,
                "parent_stage": 7,
                "ml_backend_id": str(backend.id),
                "model_id": "project-release",
                "task": "detect",
                "model_variants": {},
                "params": {"conf": 0.37},
                "class_filter": [1],
                "parent_class_filter": ["car"],
                "write": {"target": "geometry"},
                "input": {"mode": "crop"},
                "roi": {"mode": "crop", "pad": 0},
            },
        ],
    }


@pytest.mark.usefixtures("controlled_artifacts")
async def test_each_stage_is_frozen_and_idempotent_replay_keeps_original_release(
    db_session,
    httpx_client,
    super_admin,
    monkeypatch,
):
    from app.workers.tasks import batch_predict

    project, backend, version, _, run, attempt = await seed_published(
        db_session, httpx_client, super_admin, monkeypatch
    )
    _, token = super_admin
    task = await create_task(db_session, project_id=project.id)
    await managed_task(db_session, project, super_admin[0], task=task)
    dispatch = Mock(side_effect=lambda **kw: SimpleNamespace(id=kw["task_id"]))
    monkeypatch.setattr(batch_predict, "apply_async", dispatch)
    endpoint = f"/api/v1/projects/{project.id}/preannotate"
    body = pipeline_body(backend, task)
    body["pipeline_stages"][1]["params"]["large_configuration"] = "x" * 9000
    body["params"] = {"large_configuration": "r" * 9000}
    invalid = copy.deepcopy(body)
    invalid["pipeline_stages"][1]["class_filter"] = [999]
    rejected = await httpx_client.post(endpoint, headers=headers(token), json=invalid)
    assert rejected.status_code == 422, rejected.text
    dispatch.assert_not_called()
    request_headers = {**headers(token), "Idempotency-Key": uuid.uuid4().hex}
    response = await httpx_client.post(endpoint, headers=request_headers, json=body)
    assert response.status_code == 200, response.text
    job = await db_session.get(AsyncJob, uuid.UUID(response.json()["job_id"]))
    snapshot = await db_session.get(
        PredictionExecutionSnapshot, job.execution_snapshot_id
    )
    original = copy.deepcopy(snapshot.payload)
    assert len(str(original)) > 8192
    assert [stage["stage"] for stage in original["stages"]] == [7, 9]
    assert all(
        stage["frozen_model_ref"]["model_version_id"] == str(version.id)
        for stage in original["stages"]
    )
    assert original["stages"][1]["class_filter"] == [1]
    next_attempt = await next_successful_attempt(db_session, run, attempt)
    second, _ = await register(
        httpx_client, token, project, run, next_attempt, name="new release"
    )
    version2 = await db_session.get(TrainedModelVersion, uuid.UUID(second["id"]))
    deployment2 = ModelDeployment(
        model_version_id=version2.id,
        registry_id=backend.id,
        status="ready",
        receipt=trained_models.expected_receipt(version2),
        trial={},
    )
    db_session.add(deployment2)
    await db_session.flush()
    await publish(httpx_client, token, project, version2, deployment2, 1)
    replay = await httpx_client.post(endpoint, headers=request_headers, json=body)
    assert replay.status_code == 200 and replay.json()["job_id"] == str(job.id)
    assert dispatch.call_args.kwargs["kwargs"]["pipeline_stages"] == original["stages"]
    assert snapshot.payload == original


@pytest.mark.usefixtures("controlled_artifacts")
@pytest.mark.parametrize("child_fails", [False, True])
async def test_snapshot_retry_replays_root_and_crop_after_original_job_is_deleted(
    trained_retry_world,
    monkeypatch,
    child_fails,
    input_world,
    managed_worker_world,
):
    from datetime import timedelta

    from app.config import settings
    from app.db.models.prediction_input import PredictionInputLease
    from app.db.models.storage_maintenance import StorageOperation
    from app.services import async_job as jobs, prediction_inputs, prediction_originals
    from app.services.prediction_execution import load_snapshot
    from app.workers import predictions_retry

    db_session = trained_retry_world.db
    httpx_client = trained_retry_world.client
    owner, token = trained_retry_world.admin

    project, backend, version, deployment, *_ = await seed_published(
        db_session, httpx_client, (owner, token), monkeypatch
    )
    task = await create_task(db_session, project_id=project.id)
    member = await managed_task(
        db_session, project, owner, task=task, size=(1000, 1000)
    )
    source = await prediction_originals.resolve(db_session, task.id)
    managed_worker_world.source_bytes[source.bucket, source.object_key] = member.raw
    stages = pipeline_body(backend, task)["pipeline_stages"]
    ref = trained_models.frozen_reference(version, deployment)
    for stage in stages:
        stage["model_id"] = f"trained:{version.id}"
        stage["frozen_model_ref"] = copy.deepcopy(ref)
    config = {
        "stages": stages,
        "prompt": None,
        "output_mode": "box",
        "execution_scope": "bulk",
        "execution_unit": None,
        "box_threshold": 0.3,
        "text_threshold": 0.25,
    }
    job = await freeze_job(
        db_session,
        project=project,
        actor=owner,
        backend=backend,
        tasks=[task],
        config=config,
    )
    original_binding = copy.deepcopy(job.payload["prediction_authority"]["tasks"][0])
    snapshot = await db_session.get(
        PredictionExecutionSnapshot, job.execution_snapshot_id
    )
    original_config, digest = await load_snapshot(db_session, snapshot.id, project.id)
    assert original_config == {**config, "predict_mode": "overwrite"}
    failed = FailedPrediction(
        project_id=project.id,
        task_id=task.id,
        ml_backend_id=backend.id,
        execution_snapshot_id=snapshot.id,
        error_type="TimeoutError",
        message="transient",
        extra={
            "original_input": original_binding,
            "execution_snapshot": {"id": str(snapshot.id), "digest": digest},
        },
    )
    db_session.add(failed)
    await db_session.flush()
    fid = failed.id
    original_job_id = job.id
    await jobs.mark_failed(
        db_session,
        job.id,
        error="transient",
        result={"failed_prediction_ids": [str(fid)]},
    )
    await db_session.delete(job)
    await db_session.commit()
    assert await db_session.get(AsyncJob, original_job_id) is None
    assert (await load_snapshot(db_session, snapshot.id, project.id)) == (
        original_config,
        digest,
    )

    dispatch = Mock(side_effect=lambda **kw: SimpleNamespace(id=kw["task_id"]))
    monkeypatch.setattr(
        predictions_retry.retry_failed_prediction, "apply_async", dispatch
    )
    response = await httpx_client.post(
        f"/api/v1/admin/failed-predictions/{fid}/retry", headers=headers(token)
    )
    assert response.status_code == 202, response.text
    dispatch.assert_called_once()
    dispatched = dispatch.call_args.kwargs
    retry_id = uuid.UUID(dispatched["task_id"])
    assert dispatched == {
        "args": [str(fid), str(owner.id)],
        "queue": settings.preannotate_cpu_queue,
        "task_id": str(retry_id),
        "kwargs": {"async_job_id": str(retry_id)},
    }
    retry_job = await db_session.get(AsyncJob, retry_id, populate_existing=True)
    assert retry_id != original_job_id
    assert retry_job.execution_snapshot_id == snapshot.id
    proof = retry_job.payload["prediction_authority"]
    assert proof["schema"] == 2
    assert proof["tasks"][0]["original"] == original_binding["original"]
    assert (await db_session.get(FailedPrediction, fid)).extra["retry_job_id"] == str(
        retry_id
    )
    # Close the observer transaction before independent worker guard sessions.
    await db_session.commit()
    calls = []

    class Client:
        def __init__(self, _db, target, **_kwargs):
            self.last_instance_id, self.pool_id = target.id, None

        async def predict(self, inputs, context=None):
            calls.append((copy.deepcopy(inputs), copy.deepcopy(context)))
            assert context["frozen_model_ref"] == ref
            assert context["model_id"] == f"trained:{version.id}"
            for item in inputs:
                bucket, key = urlsplit(item["file_path"]).path.lstrip("/").split("/", 1)
                assert bucket == input_world.bucket
                assert key.startswith("prediction-inputs/")
                raw = input_world.objects[bucket, key]
                if len(calls) == 1:
                    assert "/original/" in key and raw == member.raw
                    assert hashlib.sha256(raw).hexdigest() == source.sha256
                else:
                    assert "/crops/" in key
                    with Image.open(io.BytesIO(raw)) as crop:
                        assert crop.size == (500, 500)
                        assert crop.getpixel((250, 250))[0] > 240
            if child_fails and len(calls) == 2:
                raise TimeoutError("crop detector transient timeout")
            value = (
                {
                    "x": 10,
                    "y": 10,
                    "width": 50,
                    "height": 50,
                    "rectanglelabels": ["car"],
                }
                if len(calls) == 1
                else {
                    "x": 0.2,
                    "y": 0.2,
                    "width": 0.4,
                    "height": 0.4,
                    "rectanglelabels": ["person"],
                }
            )
            return [
                PredictionResult(
                    task_id=item["id"],
                    result=[
                        {
                            "type": "rectanglelabels",
                            "coordinate_units": "percent",
                            "value": value,
                            "score": 0.9,
                        }
                    ],
                )
                for item in inputs
            ]

    client = Client(None, None)
    monkeypatch.setattr(
        "app.application.durable_prediction_retry._model_execution_port",
        lambda *_args, **_kwargs: PredictionClientExecutionPort(client),
    )
    result = await predictions_retry._do_retry_with_factory(
        managed_worker_world.factory,
        str(fid),
        str(owner.id),
        job_id=str(retry_id),
        celery_task_id=str(retry_id),
    )
    retry_job = await db_session.get(AsyncJob, retry_id, populate_existing=True)
    assert retry_job.status == ("failed" if child_fails else "completed")
    assert await db_session.get(AsyncJob, original_job_id) is None
    assert (await load_snapshot(db_session, snapshot.id, project.id)) == (
        original_config,
        digest,
    )
    assert len(calls) == 2
    assert calls[0][0][0]["id"] == str(task.id)
    assert calls[1][1]["classes"] == [1]
    assert calls[1][1]["params"] == {"conf": 0.37}
    assert managed_worker_world.captured

    # close_inputs retains cleanup responsibility until the source permit closes.
    # Exercise the next due purge only after all physical permits have settled.
    async with managed_worker_world.factory() as db:
        assert not (await db.scalars(select(StorageOperation.id))).all()
        leases = (
            await db.scalars(
                select(PredictionInputLease).where(
                    PredictionInputLease.owner_id == retry_id
                )
            )
        ).all()
        assert len(leases) == 1
        lease = leases[0]
        assert lease.state == "cleanup_pending" and not lease.uncertain_write
        assert len(lease.inventory["objects"]) == 2
        next_purge = lease.next_cleanup_at + timedelta(seconds=1)
    with monkeypatch.context() as clock:
        clock.setattr(prediction_inputs, "now", lambda: next_purge)
        assert (
            await prediction_inputs.purge_input(managed_worker_world.factory, lease.id)
            == "removed"
        )
    async with managed_worker_world.factory() as db:
        tombstone = await db.get(PredictionInputLease, lease.id)
        assert tombstone.state == "deleted"
        assert tombstone.inventory == lease.inventory
        assert not (await db.scalars(select(StorageOperation.id))).all()
    assert not input_world.objects
    assert all(body.closed for body in input_world.bodies)

    if child_fails:
        assert result["status"] == "failed", result
        await db_session.refresh(failed)
        assert failed.retry_count == 1
        assert failed.execution_snapshot_id == snapshot.id
        assert failed.extra["original_input"] == original_binding
        assert failed.extra["last_pipeline_errors"]
        assert not (
            await db_session.scalars(
                select(Prediction).where(Prediction.task_id == task.id)
            )
        ).all()
        return
    assert result["status"] == "succeeded", result
    assert "/prediction-inputs/" in calls[1][0][0]["file_path"]
    pred = await db_session.scalar(
        select(Prediction).where(Prediction.task_id == task.id)
    )
    assert len(pred.result) == 2
    child = pred.result[1]
    assert child["value"]["width"] == pytest.approx(0.2)
    assert child["value"]["x"] == pytest.approx(10.1)
    meta = await db_session.scalar(
        select(PredictionMeta).where(PredictionMeta.prediction_id == pred.id)
    )
    assert meta.extra["execution_snapshot"]["id"] == str(snapshot.id)
    assert meta.extra["execution_snapshot"]["digest"] == digest
    assert meta.extra["trained_model"] == ref
    assert meta.extra["original_input"]["job_id"] == str(retry_id)
    assert meta.extra["original_input"]["original"]["sha256"] == source.sha256
    lineage = meta.extra["pipeline"]["shape_lineage"]
    assert lineage[1]["parent_shape_id"] == lineage[0]["shape_id"]
    assert lineage[1]["parent_stage"] == 7
    assert await db_session.get(FailedPrediction, fid, populate_existing=True) is None


async def test_workbench_member_requires_effective_assignment_but_cannot_bulk_run(
    db_session,
    httpx_client,
    super_admin,
    annotator,
    monkeypatch,
):
    from tests.test_preannotate_workbench_scope import _seed
    from app.workers.tasks import batch_predict

    owner, _ = super_admin
    member, token = annotator
    project, backend, batch, task = await _seed(db_session, owner.id)
    await create_membership(
        db_session, project_id=project.id, user_id=member.id, role="annotator"
    )
    task.assignee_id = member.id
    await db_session.flush()
    dispatch = Mock(return_value=SimpleNamespace(id="queued"))
    monkeypatch.setattr(batch_predict, "apply_async", dispatch)
    body = {
        "ml_backend_id": str(backend.id),
        "task_ids": [str(task.id)],
        "execution_scope": "workbench",
    }
    endpoint = f"/api/v1/projects/{project.id}/preannotate"
    allowed = await httpx_client.post(endpoint, headers=headers(token), json=body)
    assert allowed.status_code == 200, allowed.text
    bulk = await httpx_client.post(
        endpoint, headers=headers(token), json={**body, "execution_scope": "bulk"}
    )
    assert bulk.status_code == 403
    task.assignee_id = owner.id
    await db_session.flush()
    denied = await httpx_client.post(endpoint, headers=headers(token), json=body)
    assert denied.status_code == 403


def test_builtin_defaults_are_frozen_and_invalid_combinations_rejected():
    from fastapi import HTTPException
    from app.services.prediction_execution import freeze_builtin_defaults

    backend = SimpleNamespace(
        health_meta={
            "capabilities": {"default_variants": {"series": "yolo11", "size": "n"}}
        }
    )
    model = {
        "supported_variants": [
            {"key": "series", "variants": [{"value": "yolo11"}, {"value": "yolo12"}]},
            {"key": "size", "variants": [{"value": "n"}, {"value": "s"}]},
        ],
        "variant_combinations": [["yolo11", "n"], ["yolo12", "s"]],
        "params": {"properties": {"conf": {"default": 0.25}}},
    }
    stage = {"params": {"iou": 0.7}}
    freeze_builtin_defaults(backend, model, stage)
    assert stage["model_variants"] == {"series": "yolo11", "size": "n"}
    assert stage["params"] == {"conf": 0.25, "iou": 0.7}
    with pytest.raises(HTTPException, match="422"):
        freeze_builtin_defaults(backend, model, {"model_variants": {"size": "s"}})


def test_freezing_preserves_project_thresholds_and_legacy_flat_parameters():
    from app.services.prediction_execution import (
        freeze_builtin_defaults,
        build_stage_context,
    )

    backend = SimpleNamespace(health_meta={})
    payload = {
        "prompt": "car",
        "output_mode": "box",
        "box_threshold": 0.63,
        "text_threshold": 0.47,
    }
    model = {
        "params": {
            "properties": {
                "box_threshold": {"default": 0.25},
                "text_threshold": {"default": 0.25},
            }
        }
    }
    root = {"parent_stage": None, "model_id": "grounded-detect", "task": "detect"}
    freeze_builtin_defaults(
        backend,
        model,
        root,
        project_thresholds={"box_threshold": 0.63, "text_threshold": 0.47},
    )
    context = build_stage_context(root, payload)
    assert (context["box_threshold"], context["text_threshold"]) == (0.63, 0.47)
    child = {
        "parent_stage": 0,
        "model_id": "sam-box",
        "task": "segment",
        "params": {"simplify_tolerance": 3.0},
    }
    freeze_builtin_defaults(backend, {}, child)
    context = build_stage_context(child, payload)
    assert context["simplify_tolerance"] == 3.0
    assert "params" not in context


async def test_snapshot_integrity_and_dismissed_failures_survive_job_cleanup(
    db_session, super_admin, monkeypatch
):
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from app.services.prediction_execution import create_snapshot, load_snapshot
    from app.workers import async_jobs_cleanup
    from tests.factory import create_project

    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    task = await create_task(db_session, project_id=project.id)
    snapshot = await create_snapshot(
        db_session, project.id, {"stages": [], "execution_scope": "bulk"}
    )
    orphan = await create_snapshot(
        db_session, project.id, {"stages": [], "execution_scope": "bulk"}
    )
    old = datetime.now(timezone.utc) - timedelta(days=31)
    snapshot.created_at = orphan.created_at = old
    job = AsyncJob(
        kind="batch_predict",
        project_id=project.id,
        user_id=owner.id,
        status="failed",
        created_at=old,
        execution_snapshot_id=snapshot.id,
    )
    failed = FailedPrediction(
        project_id=project.id,
        task_id=task.id,
        error_type="TimeoutError",
        message="transient",
        dismissed_at=old,
        execution_snapshot_id=snapshot.id,
    )
    db_session.add_all([job, failed])
    await db_session.flush()
    async with db_session.begin_nested():
        with pytest.raises(IntegrityError, match="snapshots are immutable"):
            async with db_session.begin_nested():
                await db_session.execute(
                    text(
                        "UPDATE prediction_execution_snapshots SET payload='{}'::jsonb WHERE id=:id"
                    ),
                    {"id": snapshot.id},
                )
    sid, oid, jid = snapshot.id, orphan.id, job.id

    @asynccontextmanager
    async def task_session():
        yield db_session

    monkeypatch.setattr(async_jobs_cleanup, "task_session", task_session)
    await async_jobs_cleanup._purge_async()
    assert (
        await db_session.scalar(select(AsyncJob.id).where(AsyncJob.id == jid)) is None
    )
    assert (
        await db_session.scalar(
            select(PredictionExecutionSnapshot.id).where(
                PredictionExecutionSnapshot.id == oid
            )
        )
        is None
    )
    payload, _ = await load_snapshot(db_session, sid, project.id)
    assert payload["execution_scope"] == "bulk"
    with pytest.raises(ValueError, match="快照"):
        await load_snapshot(db_session, sid, uuid.uuid4())
