"""Controlled source artifacts, immutable bindings, releases and frozen jobs."""

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.prediction import PredictionMeta
from app.db.models.trained_model import ModelDeployment, TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.services import trained_models as service
from app.services.ml_client import MLBackendClient, PredictionResult
from app.services.prediction import PredictionService, to_internal_shape
from tests.conftest import create_registry_with_pool
from tests.factory import create_membership, create_project, create_task


def headers(token):
    return {"Authorization": f"Bearer {token}"}


async def seed_source(db, owner):
    project = await create_project(db, owner_id=owner.id)
    project.ai_enabled = True
    dataset = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest="a" * 64,
        snapshot_digest="b" * 64,
        name="Frozen",
        snapshot={},
        status="ready",
        manifest={"samples": []},
        package_key="owned/dataset.zip",
        package_sha256="c" * 64,
    )
    db.add(dataset)
    await db.flush()
    run = TrainingRun(
        project_id=project.id,
        creator_id=owner.id,
        dataset_id=dataset.id,
        request_id=uuid.uuid4(),
        request_digest="d" * 64,
        name="Training",
        status="succeeded",
        config={
            "framework": "ultralytics",
            "framework_version": "8.4.177",
            "classes": ["car", "person"],
            "dataset_digest": "e" * 64,
            "validation_digest": "f" * 64,
            "base_model": {"id": "yolo11n"},
        },
        config_digest="1" * 64,
    )
    db.add(run)
    await db.flush()
    attempt = TrainingAttempt(
        run_id=run.id,
        ordinal=1,
        status="succeeded",
        artifacts={
            "best.pt": {
                "role": "weights",
                "name": "best.pt",
                "key": "owned/best.pt",
                "sha256": "2" * 64,
                "size_bytes": 100,
            },
            "metrics.json": {
                "role": "metrics",
                "name": "metrics.json",
                "key": "owned/metrics.json",
                "sha256": "3" * 64,
                "size_bytes": 10,
            },
        },
    )
    db.add(attempt)
    await db.flush()
    run.current_attempt_id = attempt.id
    return project, run, attempt


@pytest.fixture
def controlled_artifacts(monkeypatch):
    monkeypatch.setattr(service, "verify_artifact", Mock())
    monkeypatch.setattr(
        service,
        "read_json_artifact",
        lambda *_: {
            "summary": {"mAP50-95": 0.4},
            "validation_digest": "f" * 64,
            "evaluation": {
                "framework": "ultralytics",
                "split": "val",
                "independent_test_set": False,
            },
        },
    )


async def register(client, token, project, run, attempt, **extra):
    body = {
        "request_id": str(uuid.uuid4()),
        "name": "Candidate",
        "run_id": str(run.id),
        "attempt_id": str(attempt.id),
        "bindings": ["car", "person"],
        **extra,
    }
    response = await client.post(
        f"/api/v1/projects/{project.id}/trained-models",
        headers=headers(token),
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json(), body


async def ready_deployment(db, project, version):
    backend, pool = await create_registry_with_pool(
        db,
        name="controlled-" + uuid.uuid4().hex[:8],
        state="connected",
        enabled_pool=True,
        auth_method="token",
        auth_token="a" * 40,
        last_checked_at=service.now(),
        health_meta={"capabilities": {}, "compute": {"effective_device": "cpu"}},
    )
    db.add(ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True))
    deployment = ModelDeployment(
        model_version_id=version.id,
        registry_id=backend.id,
        status="ready",
        receipt=service.expected_receipt(version),
        trial={"result": []},
    )
    db.add(deployment)
    await db.flush()
    return backend, deployment


def mock_live_receipts(monkeypatch, db):
    async def receipt(_client, version_id):
        return service.expected_receipt(
            await db.get(TrainedModelVersion, uuid.UUID(version_id))
        )

    monkeypatch.setattr(MLBackendClient, "trained_model_receipt", receipt)


async def publish(client, token, project, version, deployment, generation, **extra):
    body = {
        "request_id": str(uuid.uuid4()),
        "expected_generation": generation,
        "model_version_id": str(version.id),
        "deployment_id": str(deployment.id),
        "sample_confirmed": True,
        **extra,
    }
    response = await client.post(
        f"/api/v1/projects/{project.id}/model-releases",
        headers=headers(token),
        json=body,
    )
    assert response.status_code == 201, response.text
    return response.json(), body


async def test_registration_is_immutable_idempotent_and_guards_source_storage(
    db_session,
    super_admin,
    httpx_client,
    controlled_artifacts,
    monkeypatch,
):
    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    data, body = await register(httpx_client, token, project, run, attempt)
    endpoint = f"/api/v1/projects/{project.id}/trained-models"
    repeat = await httpx_client.post(endpoint, headers=headers(token), json=body)
    assert repeat.status_code == 201 and repeat.json()["id"] == data["id"]
    changed = await httpx_client.post(
        endpoint, headers=headers(token), json={**body, "bindings": ["person", "car"]}
    )
    assert changed.status_code == 409
    delete_objects = Mock()
    monkeypatch.setattr(
        "app.api.v1.training_runs.storage.delete_prefix", delete_objects
    )
    deleted = await httpx_client.delete(
        f"/api/v1/projects/{project.id}/training-runs/{run.id}", headers=headers(token)
    )
    assert deleted.status_code == 409
    delete_objects.assert_not_called()


async def test_registration_rejects_foreign_failed_and_unmapped_sources(
    db_session,
    super_admin,
    annotator,
    httpx_client,
    controlled_artifacts,
):
    owner, token = super_admin
    member, member_token = annotator
    project, run, attempt = await seed_source(db_session, owner)
    other = await create_project(db_session, owner_id=owner.id)
    await create_membership(
        db_session, project_id=project.id, user_id=member.id, role="annotator"
    )
    body = {
        "request_id": str(uuid.uuid4()),
        "name": "Candidate",
        "run_id": str(run.id),
        "attempt_id": str(attempt.id),
        "bindings": ["car", "person"],
    }
    endpoint = f"/api/v1/projects/{project.id}/trained-models"
    denied = await httpx_client.post(endpoint, headers=headers(member_token), json=body)
    assert denied.status_code == 403
    foreign = await httpx_client.post(
        f"/api/v1/projects/{other.id}/trained-models", headers=headers(token), json=body
    )
    assert foreign.status_code == 404
    for bindings in (["car"], ["car", "unknown"]):
        bad = await httpx_client.post(
            endpoint, headers=headers(token), json={**body, "bindings": bindings}
        )
        assert bad.status_code == 422
    attempt.status = "failed"
    await db_session.flush()
    failed = await httpx_client.post(endpoint, headers=headers(token), json=body)
    assert failed.status_code == 409


async def test_release_generation_idempotency_and_verified_rollback(
    db_session,
    super_admin,
    httpx_client,
    controlled_artifacts,
    monkeypatch,
):
    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    first, _ = await register(httpx_client, token, project, run, attempt)
    second, _ = await register(
        httpx_client,
        token,
        project,
        run,
        attempt,
        name="Second",
        bindings=["person", "car"],
    )
    v1 = await db_session.get(TrainedModelVersion, uuid.UUID(first["id"]))
    v2 = await db_session.get(TrainedModelVersion, uuid.UUID(second["id"]))
    _, d1 = await ready_deployment(db_session, project, v1)
    _, d2 = await ready_deployment(db_session, project, v2)
    mock_live_receipts(monkeypatch, db_session)
    r1, body1 = await publish(httpx_client, token, project, v1, d1, 0)
    stale = await httpx_client.post(
        f"/api/v1/projects/{project.id}/model-releases",
        headers=headers(token),
        json={**body1, "request_id": str(uuid.uuid4())},
    )
    assert stale.status_code == 409
    r2, _ = await publish(httpx_client, token, project, v2, d2, 1)
    replay = await httpx_client.post(
        f"/api/v1/projects/{project.id}/model-releases",
        headers=headers(token),
        json=body1,
    )
    assert replay.status_code == 201 and replay.json()["id"] == r1["id"]
    rollback, _ = await publish(
        httpx_client, token, project, v1, d1, 2, rollback_from_id=r1["id"]
    )
    assert rollback["generation"] == 3 and rollback["previous_release_id"] == r2["id"]
    assert rollback["snapshot"]["model_version_id"] == first["id"]


async def test_pending_job_replay_keeps_original_release_after_new_publication(
    db_session,
    super_admin,
    httpx_client,
    controlled_artifacts,
    monkeypatch,
):
    from app.workers.tasks import batch_predict

    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    first, _ = await register(httpx_client, token, project, run, attempt)
    second, _ = await register(
        httpx_client, token, project, run, attempt, name="Second"
    )
    v1 = await db_session.get(TrainedModelVersion, uuid.UUID(first["id"]))
    v2 = await db_session.get(TrainedModelVersion, uuid.UUID(second["id"]))
    backend, d1 = await ready_deployment(db_session, project, v1)
    d2 = ModelDeployment(
        model_version_id=v2.id,
        registry_id=backend.id,
        status="ready",
        receipt=service.expected_receipt(v2),
        trial={},
    )
    db_session.add(d2)
    await db_session.flush()
    task = await create_task(db_session, project_id=project.id)
    mock_live_receipts(monkeypatch, db_session)
    await publish(httpx_client, token, project, v1, d1, 0)
    dispatch = Mock(
        side_effect=lambda **kwargs: SimpleNamespace(
            id=kwargs.get("task_id", str(uuid.uuid4()))
        )
    )
    monkeypatch.setattr(batch_predict, "apply_async", dispatch)
    endpoint = f"/api/v1/projects/{project.id}/preannotate"
    body = {
        "ml_backend_id": str(backend.id),
        "model_id": "project-release",
        "task_type": "detection",
        "model_variants": {},
        "task_ids": [str(task.id)],
    }
    request_headers = {
        **headers(token),
        "Idempotency-Key": "frozen-" + uuid.uuid4().hex,
    }
    queued = await httpx_client.post(endpoint, headers=request_headers, json=body)
    assert queued.status_code == 200, queued.text
    job = await db_session.get(AsyncJob, uuid.UUID(queued.json()["job_id"]))
    original_ref = copy.deepcopy(job.payload["frozen_model_ref"])
    from app.config import settings

    assert dispatch.call_args.kwargs["queue"] == settings.preannotate_cpu_queue
    assert original_ref["model_version_id"] == first["id"]
    await publish(httpx_client, token, project, v2, d2, 1)
    replay = await httpx_client.post(endpoint, headers=request_headers, json=body)
    assert replay.status_code == 200 and replay.json()["job_id"] == str(job.id)
    assert dispatch.call_args.kwargs["kwargs"]["frozen_model_ref"] == original_ref
    fresh = await httpx_client.post(endpoint, headers=headers(token), json=body)
    assert fresh.status_code == 200, fresh.text
    assert (
        dispatch.call_args.kwargs["kwargs"]["frozen_model_ref"]["model_version_id"]
        == second["id"]
    )


async def test_ready_deployment_retry_does_not_interrupt_existing_predictions(
    db_session,
    super_admin,
    httpx_client,
    controlled_artifacts,
    monkeypatch,
):
    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    first, _ = await register(httpx_client, token, project, run, attempt)
    version = await db_session.get(TrainedModelVersion, uuid.UUID(first["id"]))
    backend, deployment = await ready_deployment(db_session, project, version)
    install = AsyncMock()
    monkeypatch.setattr(service, "install_and_trial", install)
    mock_live_receipts(monkeypatch, db_session)
    ref = service.frozen_reference(version, deployment)
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/trained-models/{version.id}/deploy",
        headers=headers(token),
        json={"registry_id": str(backend.id)},
    )
    assert response.status_code == 200 and response.json()["status"] == "ready"
    install.assert_not_awaited()
    assert (
        await service.validate_frozen_reference(db_session, project.id, ref)
        == backend.id
    )


@pytest.mark.parametrize("unavailable", [False, True])
async def test_ready_deployment_restores_a_missing_backend_installation(
    db_session,
    super_admin,
    httpx_client,
    controlled_artifacts,
    monkeypatch,
    unavailable,
):
    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    first, _ = await register(httpx_client, token, project, run, attempt)
    version = await db_session.get(TrainedModelVersion, uuid.UUID(first["id"]))
    backend, deployment = await ready_deployment(db_session, project, version)
    from fastapi import HTTPException

    monkeypatch.setattr(
        MLBackendClient,
        "trained_model_receipt",
        AsyncMock(
            side_effect=TimeoutError()
            if unavailable
            else HTTPException(status_code=409, detail="missing installation")
        ),
    )
    install = AsyncMock(
        return_value=(service.expected_receipt(version), {"result": []})
    )
    monkeypatch.setattr(service, "install_and_trial", install)
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/trained-models/{version.id}/deploy",
        headers=headers(token),
        json={"registry_id": str(backend.id)},
    )
    if unavailable:
        assert response.status_code == 503
        install.assert_not_awaited()
        assert deployment.status == "ready"
        return
    assert response.status_code == 200 and response.json()["status"] == "ready"
    install.assert_awaited_once()
    assert response.json()["id"] == str(deployment.id)


def prediction_result(ref, **extra):
    return PredictionResult(
        task_id=str(uuid.uuid4()),
        result=[
            {
                "type": "rectanglelabels",
                "native_class_index": 0,
                "score": 0.8,
                "value": {
                    "x": 0.1,
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.4,
                    "rectanglelabels": [ref["classes"][0]],
                },
                **extra,
            }
        ],
        meta={
            "trained_model": {
                k: ref[k] for k in ("model_version_id", "sha256", "classes_digest")
            }
        },
    )


async def test_mapping_canonicalization_and_provenance_survive_acceptance_aliases(
    db_session,
    super_admin,
    httpx_client,
    controlled_artifacts,
):
    from app.services.annotation import AnnotationService

    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    bindings = copy.deepcopy(project.tool_bindings)
    bindings["bbox"]["classes"].append({"name": "车辆", "alias": "car"})
    project.tool_bindings = bindings
    data, _ = await register(httpx_client, token, project, run, attempt)
    version = await db_session.get(TrainedModelVersion, uuid.UUID(data["id"]))
    _, deployment = await ready_deployment(db_session, project, version)
    ref = service.frozen_reference(version, deployment)
    result = prediction_result(
        ref,
        geometry={"type": "bbox", "x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5},
        class_name="person",
        tool_unit_id="polygon",
    )
    service.validate_and_map_results([result], ref)
    normalized = to_internal_shape(result.result[0])
    assert normalized["class_name"] == "car" and normalized["tool_unit_id"] == "bbox"
    assert normalized["geometry"]["x"] == pytest.approx(0.001)
    task = await create_task(db_session, project_id=project.id)
    prediction = await PredictionService(db_session).create_from_ml_result(
        task.id, project.id, deployment.registry_id, result.result, model_provenance=ref
    )
    meta = await db_session.scalar(
        select(PredictionMeta).where(PredictionMeta.prediction_id == prediction.id)
    )
    assert meta.extra["trained_model"] == ref
    accepted = await AnnotationService(db_session).accept_prediction(
        prediction.id, owner.id
    )
    assert accepted and accepted[0].class_name == "car"


@pytest.mark.parametrize("change", ["identity", "index", "name", "coordinates"])
def test_wrong_identity_native_class_and_coordinates_are_rejected(change):
    ref = {
        "model_version_id": str(uuid.uuid4()),
        "sha256": "a" * 64,
        "classes_digest": "b" * 64,
        "classes": ["car"],
        "bindings": ["车辆"],
    }
    result = prediction_result(ref)
    if change == "identity":
        result.meta["trained_model"]["sha256"] = "c" * 64
    elif change == "index":
        result.result[0]["native_class_index"] = 9
    elif change == "name":
        result.result[0]["value"]["rectanglelabels"] = ["person"]
    else:
        result.result[0]["value"]["x"] = float("nan")
    with pytest.raises(ValueError):
        service.validate_and_map_results([result], ref)


@pytest.mark.parametrize(
    "stage",
    [
        {"write": {"target": "attributes"}},
        {"input": {"mode": "parent_crop"}},
        {"parent_stage": 0},
        {"roi": {"x": 0}},
        {"source": {"kind": "scene"}},
        {"source": {"data_type": "video"}},
    ],
)
def test_controlled_scope_rejects_non_image_root_contracts(stage):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        service.assert_image_scope(
            SimpleNamespace(data_type="image"), pipeline_stages=[stage]
        )
    assert error.value.status_code == 422


def test_controlled_scope_accepts_nonzero_root_stage():
    service.assert_image_scope(
        SimpleNamespace(data_type="image"),
        pipeline_stages=[
            {
                "stage": 7,
                "parent_stage": None,
                "write": {"target": "geometry"},
                "input": {"mode": "full_image"},
                "source": {"kind": "dataset", "data_type": "image"},
            }
        ],
    )


def test_backend_download_is_absolute_with_a_relative_browser_proxy(monkeypatch):
    from urllib.parse import urlsplit
    from app.config import settings

    monkeypatch.setattr(settings, "minio_public_url", "/minio")
    monkeypatch.setattr(settings, "ml_backend_storage_host", "")
    url = service.download_url("owned/weights.pt")
    assert urlsplit(url).scheme in {"http", "https"}
    assert urlsplit(url).netloc
    assert "/owned/weights.pt" in urlsplit(url).path
    public_url = service.storage.generate_download_url("owned/input.png")
    assert public_url.startswith("/minio/")
    backend_url = service.storage.rewrite_host_for_ml_backend(public_url)
    assert urlsplit(backend_url).netloc == settings.minio_endpoint
    assert urlsplit(backend_url).query == urlsplit(public_url).query


async def test_frozen_model_revalidates_current_project_bindings(
    db_session,
    super_admin,
    httpx_client,
    controlled_artifacts,
):
    from fastapi import HTTPException

    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    data, _ = await register(httpx_client, token, project, run, attempt)
    version = await db_session.get(TrainedModelVersion, uuid.UUID(data["id"]))
    _, deployment = await ready_deployment(db_session, project, version)
    ref = service.frozen_reference(version, deployment)
    project.tool_bindings = {"bbox": {"classes": [{"name": "different"}]}}
    with pytest.raises(HTTPException):
        await service.validate_frozen_reference(db_session, project.id, ref)
