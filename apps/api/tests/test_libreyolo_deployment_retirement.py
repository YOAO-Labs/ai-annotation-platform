"""Retirement stops new references without destroying frozen execution evidence."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from training_contract.inference_runtime import inference_runtime

import pytest
from fastapi import HTTPException

from app.db.models.prediction import FailedPrediction
from app.db.models.trained_model import ModelRelease, ProjectModelSelection, TrainedModelVersion
from app.services import async_job, prediction_execution, trained_models
from app.services.inference_topology_data import InferenceTopologyData
from tests.test_trained_models import (controlled_artifacts, ready_deployment, register, seed_source)

from tests.trained_model_reference_fixtures import build_reference

async def test_retirement_blocks_live_release_secondary_stage_and_retry(
    db_session, super_admin, httpx_client, controlled_artifacts, monkeypatch
):
    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    run.config = {**run.config, "framework": "libreyolo", "framework_version": "1.6.0",
                  "base_model": {"id": "LibreYOLO9t"}}
    monkeypatch.setattr(trained_models, "read_json_artifact", lambda *_, **__: {
        "validation_digest": run.config["validation_digest"],
        "evaluation": {"framework": "libreyolo", "version": "1.6.0", "task": "detection"},
        "summary": {"mAP50-95": 0.4}, "per_class": [], "per_class_available": False,
    })
    await db_session.flush()
    data, _ = await register(httpx_client, token, project, run, attempt)
    version = await db_session.get(TrainedModelVersion, uuid.UUID(data["id"]))
    backend, deployment = await ready_deployment(db_session, project, version)
    old_receipt, old_trial = dict(deployment.receipt), dict(deployment.trial)
    release = ModelRelease(project_id=project.id, model_version_id=version.id,
        deployment_id=deployment.id, creator_id=owner.id, request_id=uuid.uuid4(),
        request_digest="a" * 64, generation=1, task="detect", snapshot={"retained": True})
    db_session.add(release)
    await db_session.flush()
    selection = ProjectModelSelection(project_id=project.id, task="detect", generation=1, release_id=release.id)
    db_session.add(selection)
    original_ref = build_reference(version, deployment)
    await db_session.flush()
    await trained_models.retire_deployment(db_session, project.id, version.id, deployment.id, terminal=False)
    with pytest.raises(HTTPException):
        await trained_models.resolve_reference(
            db_session, project, f"trained:{version.id}", backend.id, allow_candidate=True
        )
    assert await trained_models.validate_frozen_reference(db_session, project.id, original_ref) == backend.id
    assert deployment.status == "draining"
    with pytest.raises(HTTPException) as error:
        await trained_models.retire_deployment(db_session, project.id, version.id, deployment.id, terminal=True)
    assert error.value.detail["active_releases"] == 1
    selection.release_id = None
    await db_session.flush()

    snapshot = await prediction_execution.create_snapshot(db_session, project.id, {
        "stages": [{"parent_stage": None}, {"parent_stage": 0,
            "frozen_model_ref": {"deployment_id": str(deployment.id)}}],
    })
    job = await async_job.create_job(db_session, kind="batch_predict", project_id=project.id,
                                     user_id=owner.id, payload={})
    await db_session.flush()
    await async_job.bind_execution_snapshot(db_session, job, actor_id=owner.id, snapshot_id=snapshot.id)
    failure = FailedPrediction(project_id=project.id, error_type="backend", message="failed",
                               execution_snapshot_id=snapshot.id, extra={})
    db_session.add(failure)
    legacy_failure = FailedPrediction(
        project_id=project.id, ml_backend_id=backend.id, error_type="backend",
        message="legacy failure", extra={"request_context": {
            "frozen_model_ref": {"model_version_id": str(version.id)}
        }},
    )
    db_session.add(legacy_failure)
    await db_session.flush()
    await InferenceTopologyData(db_session).lock_plugin_registry(backend.id)
    usage = await trained_models.registry_usage(db_session, backend.id)
    assert usage.active_releases == 0 and usage.frozen_executions == 3
    with pytest.raises(HTTPException) as error:
        await trained_models.retire_deployment(db_session, project.id, version.id, deployment.id, terminal=True)
    assert error.value.detail["active_jobs"] == 1
    assert error.value.detail["retryable_failures"] == 2
    await async_job.mark_complete(db_session, job.id)
    failure.dismissed_at = trained_models.now()
    legacy_failure.dismissed_at = trained_models.now()
    await db_session.flush()
    await trained_models.retire_deployment(db_session, project.id, version.id, deployment.id, terminal=True)
    assert deployment.status == "retired"
    assert deployment.receipt == old_receipt and deployment.trial == old_trial
    assert release.snapshot == {"retained": True}


async def test_publish_rejects_qualified_but_different_live_runtime(
    db_session, super_admin, httpx_client, controlled_artifacts, monkeypatch
):
    from app.services import inference_backend_control

    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    run.config = {**run.config, "framework": "libreyolo", "framework_version": "1.6.0",
                  "base_model": {"id": "LibreYOLO9t"}}
    monkeypatch.setattr(trained_models, "read_json_artifact", lambda *_, **__: {
        "validation_digest": run.config["validation_digest"],
        "evaluation": {"framework": "libreyolo", "version": "1.6.0", "task": "detection"},
        "summary": {"mAP50-95": 0.4}, "per_class": [], "per_class_available": False,
    })
    await db_session.flush()
    data, _ = await register(httpx_client, token, project, run, attempt)
    version = await db_session.get(TrainedModelVersion, uuid.UUID(data["id"]))
    backend, deployment = await ready_deployment(db_session, project, version)
    receipt = trained_models.expected_receipt(version)
    cpu = inference_runtime(receipt, "1.6.0", device="cpu")
    cuda = inference_runtime(receipt, "1.6.0", device="cuda")
    deployment.receipt = {**receipt, "inference_runtime": cpu}
    deployment.trial = {"inference_runtime": cpu, "result": []}
    await db_session.flush()
    monkeypatch.setattr(
        inference_backend_control, "backend_control_service",
        lambda _: SimpleNamespace(
            trained_model_receipt=AsyncMock(return_value={**receipt, "inference_runtime": cuda})
        ),
    )
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/model-releases",
        headers={"Authorization": f"Bearer {token}"},
        json={"request_id": str(uuid.uuid4()), "expected_generation": 0,
              "model_version_id": str(version.id), "deployment_id": str(deployment.id),
              "sample_confirmed": True},
    )
    assert response.status_code == 409
    assert deployment.trial["inference_runtime"] == cpu
