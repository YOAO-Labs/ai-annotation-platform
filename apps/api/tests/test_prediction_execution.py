"""Published detectors freeze per stage and replay a failed task's whole pipeline."""

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image
from sqlalchemy import select

from app.db.models.async_job import AsyncJob
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.prediction_execution import PredictionExecutionSnapshot
from app.db.models.trained_model import TrainedModelVersion, ModelDeployment
from app.services import trained_models
from app.services.ml_client import PredictionResult
from tests.factory import create_task, create_membership
from tests.test_failed_predictions import _passthrough_session_factory
from tests.test_prediction_inputs import input_world as input_world
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
    db_session,
    httpx_client,
    super_admin,
    monkeypatch,
    child_fails,
    input_world,
):
    from app.workers.predictions_retry import _run_snapshot_retry
    from app.services.prediction_execution import create_snapshot

    project, backend, version, deployment, *_ = await seed_published(
        db_session, httpx_client, super_admin, monkeypatch
    )
    owner, _ = super_admin
    task = await create_task(db_session, project_id=project.id)
    stages = pipeline_body(backend, task)["pipeline_stages"]
    ref = trained_models.frozen_reference(version, deployment)
    for stage in stages:
        stage["model_id"] = f"trained:{version.id}"
        stage["frozen_model_ref"] = ref
    snapshot = await create_snapshot(
        db_session,
        project.id,
        {
            "stages": stages,
            "prompt": None,
            "output_mode": "box",
            "execution_scope": "bulk",
            "execution_unit": None,
            "box_threshold": 0.3,
            "text_threshold": 0.25,
        },
    )
    job = AsyncJob(
        kind="batch_predict",
        project_id=project.id,
        user_id=owner.id,
        status="failed",
        execution_snapshot_id=snapshot.id,
    )
    db_session.add(job)
    failed = FailedPrediction(
        project_id=project.id,
        task_id=task.id,
        ml_backend_id=backend.id,
        execution_snapshot_id=snapshot.id,
        error_type="TimeoutError",
        message="transient",
    )
    db_session.add(failed)
    await db_session.flush()
    fid = failed.id
    await db_session.delete(job)
    await db_session.commit()
    calls = []

    class Client:
        def __init__(self, _db, target, **_kwargs):
            self.last_instance_id, self.pool_id = target.id, None

        async def predict(self, inputs, context=None):
            calls.append((copy.deepcopy(inputs), copy.deepcopy(context)))
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

    monkeypatch.setattr("app.services.ml_routing.client.RoutedMLBackendClient", Client)
    monkeypatch.setattr(
        "app.workers.tasks._load_task_image",
        lambda _task: Image.new("RGB", (1000, 1000)),
    )
    result = await _run_snapshot_retry(
        _passthrough_session_factory(db_session), fid, owner.id
    )
    if child_fails:
        assert result["status"] == "failed", result
        await db_session.refresh(failed)
        assert failed.retry_count == 1
        assert failed.execution_snapshot_id == snapshot.id
        assert failed.extra["last_pipeline_errors"]
        assert not (
            await db_session.scalars(
                select(Prediction).where(Prediction.task_id == task.id)
            )
        ).all()
        assert len(calls) == 2
        return
    assert result["status"] == "succeeded", result
    assert len(calls) == 2
    assert calls[0][0][0]["id"] == str(task.id)
    assert "/prediction-inputs/" in calls[1][0][0]["file_path"]
    assert not input_world.objects
    assert calls[1][1]["frozen_model_ref"] == ref and calls[1][1]["classes"] == [1]
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
    lineage = meta.extra["pipeline"]["shape_lineage"]
    assert lineage[1]["parent_shape_id"] == lineage[0]["shape_id"]
    assert lineage[1]["parent_stage"] == 7
    assert await db_session.get(FailedPrediction, fid) is None


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
    monkeypatch.setattr(
        async_jobs_cleanup, "task_session", _passthrough_session_factory(db_session)
    )
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
