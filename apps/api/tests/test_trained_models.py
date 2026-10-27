"""Controlled source artifacts, immutable bindings, releases and frozen jobs."""

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select, update

from app.db.models.async_job import AsyncJob
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.prediction import PredictionMeta
from app.db.models.trained_model import ModelDeployment, TrainedModelVersion
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.services import trained_models as service, training_run_storage
from app.services import trained_model_backend as backend_io
from app.services.labeling_prediction_contract import ApplyPredictionResultRequest
from aap_inference_contract.execution import ModelTaskResult, thaw_json
from app.services.prediction import PredictionService, to_internal_shape
from app.services.labeling_write_scope import capture_labeling_write_scope
from app.services.trained_model_reference_contract import (
    FreezeModelReferenceError,
    ValidateFrozenModelReferenceRequest,
)
from app.services.training_dataset_bridge import content_digest
from tests.conftest import create_registry_with_pool
from tests.factory import create_membership, create_project, create_task
from tests.trained_model_reference_fixtures import build_reference


def headers(token):
    return {"Authorization": f"Bearer {token}"}


async def seed_source(db, owner, *, project=None):
    project = project or await create_project(db, owner_id=owner.id)
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
    from app.services.training_dataset_storage import allocate

    ref = await allocate(db, dataset)
    dataset.package_key = ref["prefix"] + "dataset.zip"
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
    await training_run_storage.allocate(db, run)
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
    storage_owner = training_run_storage.resolve(run)
    attempt.artifacts = {
        name: {**artifact, "key": storage_owner.key(f"attempts/{attempt.id}/{name}")}
        for name, artifact in attempt.artifacts.items()
    }
    run.current_attempt_id = attempt.id
    return project, run, attempt


@pytest.fixture
def controlled_artifacts(monkeypatch):
    from tests.training_consumer_fixtures import mock_legacy_transport

    mock_legacy_transport(monkeypatch)
    monkeypatch.setattr(service, "verify_artifact", Mock())
    monkeypatch.setattr(
        service,
        "read_json_artifact",
        lambda *_, bucket, transport=None: {
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


async def next_successful_attempt(db, run, previous):
    attempt = TrainingAttempt(
        run_id=run.id,
        ordinal=previous.ordinal + 1,
        status="succeeded",
        artifacts=copy.deepcopy(previous.artifacts),
    )
    db.add(attempt)
    await db.flush()
    storage_owner = training_run_storage.resolve(run)
    attempt.artifacts = {
        name: {**artifact, "key": storage_owner.key(f"attempts/{attempt.id}/{name}")}
        for name, artifact in attempt.artifacts.items()
    }
    run.current_attempt_id = attempt.id
    return attempt


async def test_libre_registration_retains_native_gaps_and_immutable_runtime_qualification(
    db_session, super_admin, httpx_client, controlled_artifacts, monkeypatch
):
    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    run.config = {
        **run.config,
        "framework": "libreyolo",
        "framework_version": "1.6.0",
        "base_model": {"id": "LibreYOLO9t"},
    }
    monkeypatch.setattr(
        service,
        "read_json_artifact",
        lambda *_, bucket, transport=None: {
            "validation_digest": run.config["validation_digest"],
            "evaluation": {
                "framework": "libreyolo",
                "version": "1.6.0",
                "task": "detection",
            },
            "summary": {
                "mAP50-95": 0.0,
                "AR100": 0.4,
                "precision": 0.99,
                "recall": 0.98,
                "mAP75": -1.0,
            },
            "per_class": [],
            "per_class_available": False,
        },
    )
    await db_session.flush()
    data, _ = await register(httpx_client, token, project, run, attempt)
    assert data["evaluation"]["summary"] == {
        "metrics/mAP50-95": 0.0,
        "metrics/AR100": 0.4,
        "metrics/mAP75": None,
    }
    assert data["evaluation"]["per_class"] == []
    assert data["evaluation"]["per_class_available"] is False
    assert data["evaluation"]["comparison_identity_status"] == "unknown"
    assert data["evaluation"]["comparison_compatibility_digest"] is None
    version = await db_session.get(TrainedModelVersion, uuid.UUID(data["id"]))
    receipt = service.expected_receipt(version)
    assert receipt["model_family"] == "yolo9" and receipt["model_size"] == "t"
    assert receipt["artifact_size_bytes"] == 100
    assert receipt["runtime_qualification"]["device"] == "cpu"
    service.check_receipt(version, receipt)
    for key, replacement in (
        ("framework", "ultralytics"),
        ("model_family", "yolo26"),
        ("artifact_format", "foreign"),
        ("artifact_size_bytes", 101),
        ("runtime_qualification", {"revision": 99}),
    ):
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            service.check_receipt(version, {**receipt, key: replacement})


@pytest.mark.parametrize(
    "fault", ["version", "family", "duplicate_weights", "duplicate_metrics"]
)
async def test_libre_registration_rejects_foreign_identity_and_nonunique_artifacts(
    db_session, super_admin, httpx_client, controlled_artifacts, fault
):
    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    run.config = {
        **run.config,
        "framework": "libreyolo",
        "framework_version": "1.5.0" if fault == "version" else "1.6.0",
        "base_model": {"id": "LibreYOLO9e" if fault == "family" else "LibreYOLO9t"},
    }
    if fault.startswith("duplicate"):
        role = "weights" if fault == "duplicate_weights" else "metrics"
        artifact = next(a for a in attempt.artifacts.values() if a["role"] == role)
        attempt.artifacts = {**attempt.artifacts, "foreign": artifact}
    await db_session.flush()
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/trained-models",
        headers=headers(token),
        json={
            "request_id": str(uuid.uuid4()),
            "name": "Rejected",
            "run_id": str(run.id),
            "attempt_id": str(attempt.id),
            "bindings": ["car", "person"],
        },
    )
    assert response.status_code == (422 if fault in {"version", "family"} else 409)
    assert (
        await db_session.scalar(
            select(TrainedModelVersion).where(
                TrainedModelVersion.project_id == project.id
            )
        )
        is None
    )


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
    from app.services import inference_backend_control

    async def receipt(_registry_id, version_id):
        return service.expected_receipt(
            await db.get(TrainedModelVersion, uuid.UUID(version_id))
        )

    control = SimpleNamespace(
        credential_policy=AsyncMock(
            return_value=SimpleNamespace(
                auth_method="token", auth_token_length_valid=True
            )
        ),
        trained_model_receipt=receipt,
    )
    monkeypatch.setattr(
        inference_backend_control, "backend_control_service", lambda _db: control
    )


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
    from app.db.models.training_storage_deletion import TrainingStorageDeletion
    from app.services.storage import storage_service

    # Physical deletion belongs to the staged cleanup owner; the retained model
    # must block before any intent is staged or any prefix is deleted.
    delete_objects = Mock()
    monkeypatch.setattr(storage_service, "delete_prefix", delete_objects)
    deleted = await httpx_client.delete(
        f"/api/v1/projects/{project.id}/training-runs/{run.id}", headers=headers(token)
    )
    assert deleted.status_code == 409
    delete_objects.assert_not_called()
    assert (
        await db_session.scalar(
            select(TrainingStorageDeletion.id).where(
                TrainingStorageDeletion.prefix
                == f"projects/{project.id}/runs/{run.id}/"
            )
        )
        is None
    )


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
    # One completed attempt feeds one version, so the second model needs its
    # own retained source inside the same project.
    _, second_run, second_attempt = await seed_source(
        db_session, owner, project=project
    )
    second, _ = await register(
        httpx_client,
        token,
        project,
        second_run,
        second_attempt,
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
    from app.workers.batch_prediction_dispatch import CeleryBatchPredictionDispatcher

    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    first, _ = await register(httpx_client, token, project, run, attempt)
    # The replayed job must observe a newer release; that newer model needs its
    # own completed attempt within the same project.
    _, second_run, second_attempt = await seed_source(
        db_session, owner, project=project
    )
    second, _ = await register(
        httpx_client, token, project, second_run, second_attempt, name="Second"
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
    await create_task(db_session, project_id=project.id)
    import io
    from PIL import Image
    from tests.sample_evaluation_original_fixtures import Objects, registered

    image = io.BytesIO()
    Image.new("RGB", (16, 12), (20, 50, 90)).save(image, format="PNG")
    _, _, replica = await registered(db_session, task, owner, image.getvalue())
    originals = Objects().install(monkeypatch)
    originals.put(replica.bucket, replica.object_key, image.getvalue())
    mock_live_receipts(monkeypatch, db_session)
    r1, _ = await publish(httpx_client, token, project, v1, d1, 0)
    dispatch = Mock(
        side_effect=lambda request, **_options: (
            request.async_job_id or str(uuid.uuid4())
        )
    )
    monkeypatch.setattr(CeleryBatchPredictionDispatcher, "dispatch", dispatch)
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
    assert job.payload["task_ids"] == [str(task.id)]
    original_ref = copy.deepcopy(job.payload["frozen_model_ref"])
    from app.config import settings

    assert dispatch.call_args.kwargs["queue"] == settings.preannotate_cpu_queue
    assert original_ref["model_version_id"] == first["id"]
    assert original_ref["release_id"] == r1["id"]
    assert original_ref["release_generation"] == r1["generation"]
    await publish(httpx_client, token, project, v2, d2, 1)
    assert (
        await service.bind_frozen_model_reference(db_session).validate(
            ValidateFrozenModelReferenceRequest(
                project_id=project.id, reference=original_ref
            )
        )
        == backend.id
    )
    replay = await httpx_client.post(endpoint, headers=request_headers, json=body)
    assert replay.status_code == 200 and replay.json()["job_id"] == str(job.id)
    assert dispatch.call_args.args[0].frozen_model_ref == original_ref
    fresh = await httpx_client.post(endpoint, headers=headers(token), json=body)
    assert fresh.status_code == 200, fresh.text
    assert (
        dispatch.call_args.args[0].frozen_model_ref["model_version_id"] == second["id"]
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
    monkeypatch.setattr(backend_io, "install_and_trial", install)
    mock_live_receipts(monkeypatch, db_session)
    ref = build_reference(version, deployment)
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/trained-models/{version.id}/deploy",
        headers=headers(token),
        json={"registry_id": str(backend.id)},
    )
    assert response.status_code == 200 and response.json()["status"] == "ready"
    install.assert_not_awaited()
    assert (
        await service.bind_frozen_model_reference(db_session).validate(
            ValidateFrozenModelReferenceRequest(project_id=project.id, reference=ref)
        )
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

    from app.services import inference_backend_control

    control = SimpleNamespace(
        trained_model_receipt=AsyncMock(
            side_effect=TimeoutError()
            if unavailable
            else HTTPException(status_code=409, detail="missing installation")
        ),
    )
    monkeypatch.setattr(
        inference_backend_control, "backend_control_service", lambda _db: control
    )

    observed = {}

    async def install_trial(_db, _project, model, checking):
        observed["project_id"] = _project.id
        observed["model_id"] = model.id
        observed["deployment_id"] = checking.id
        observed["status"] = checking.status
        return service.expected_receipt(model), {"result": []}

    install = AsyncMock(side_effect=install_trial)
    monkeypatch.setattr(backend_io, "install_and_trial", install)
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
    assert observed == {
        "project_id": project.id,
        "model_id": version.id,
        "deployment_id": deployment.id,
        "status": "checking",
    }
    assert response.json()["id"] == str(deployment.id)


async def test_stale_deployment_check_cannot_publish_older_trial(
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
    deployment.status = "failed"
    await db_session.flush()
    newer_check_id = uuid.uuid4()

    async def supersede_during_backend_io(db, _project, model, current):
        assert current.id == deployment.id
        assert current.status == "checking"
        await db.execute(
            update(ModelDeployment)
            .where(ModelDeployment.id == current.id)
            .values(check_id=newer_check_id, status="checking")
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return service.expected_receipt(model), {"stale": True}

    monkeypatch.setattr(backend_io, "install_and_trial", supersede_during_backend_io)
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/trained-models/{version.id}/deploy",
        headers=headers(token),
        json={"registry_id": str(backend.id)},
    )

    assert response.status_code == 409
    await db_session.refresh(deployment)
    assert deployment.check_id == newer_check_id
    assert deployment.status == "checking"
    assert deployment.receipt == service.expected_receipt(version)
    assert deployment.trial == {"result": []}


def prediction_result(ref, **extra):
    return ModelTaskResult(
        task_id=str(uuid.uuid4()),
        result=(
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
            },
        ),
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
    from app.application.candidate_decision import bind_candidate_decision_port
    from app.services.candidate_decision_contract import CandidateAcceptCommand

    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    bindings = copy.deepcopy(project.tool_bindings)
    bindings["bbox"]["classes"].append({"name": "车辆", "alias": "car"})
    project.tool_bindings = bindings
    data, _ = await register(httpx_client, token, project, run, attempt)
    version = await db_session.get(TrainedModelVersion, uuid.UUID(data["id"]))
    _, deployment = await ready_deployment(db_session, project, version)
    ref = build_reference(version, deployment)
    result = prediction_result(
        ref,
        geometry={"type": "bbox", "x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5},
        class_name="person",
        tool_unit_id="polygon",
    )
    mapped = service.validate_and_map_results([result], ref)
    shapes = thaw_json(mapped[0].result)
    normalized = to_internal_shape(shapes[0])
    assert result.model_version is None
    assert result.result[0]["value"]["rectanglelabels"] == (ref["classes"][0],)
    assert mapped[0].model_version == f"trained:{ref['model_version_id']}"
    assert normalized["class_name"] == "car" and normalized["tool_unit_id"] == "bbox"
    assert normalized["geometry"]["x"] == pytest.approx(0.001)
    task = await create_task(db_session, project_id=project.id)
    prediction_scope = capture_labeling_write_scope(
        task,
        actor_id=owner.id,
        actor_known=True,
        operation="prediction_import",
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
    )
    accept_scope = capture_labeling_write_scope(
        task,
        actor_id=owner.id,
        actor_known=True,
        operation="candidate_accept",
        execution_scope=None,
        execution_scope_known=True,
        job_id=None,
        job_id_known=True,
    )
    prediction = await PredictionService(db_session).apply_prediction_result(
        ApplyPredictionResultRequest(
            task_id=task.id,
            project_id=project.id,
            ml_backend_id=deployment.registry_id,
            result=shapes,
            model_provenance=ref,
            write_scope=prediction_scope,
        )
    )
    meta = await db_session.scalar(
        select(PredictionMeta).where(PredictionMeta.prediction_id == prediction.id)
    )
    assert meta.extra["trained_model"] == ref
    accepted = await bind_candidate_decision_port(
        db_session, task=task, actor=owner
    ).accept(
        CandidateAcceptCommand(
            write_scope=accept_scope,
            prediction_id=prediction.id,
            shape_index=None,
            override_class_name=None,
            attribute_overrides=None,
            video_segment_id=None,
        )
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
    shapes = thaw_json(result.result)
    meta = thaw_json(result.meta)
    if change == "identity":
        meta["trained_model"]["sha256"] = "c" * 64
    elif change == "index":
        shapes[0]["native_class_index"] = 9
    elif change == "name":
        shapes[0]["value"]["rectanglelabels"] = ["person"]
    else:
        shapes[0]["value"]["x"] = 100.5
    malformed = ModelTaskResult(
        task_id=result.task_id,
        result=shapes,
        meta=meta,
    )
    with pytest.raises(ValueError):
        service.validate_and_map_results([malformed], ref)


def test_backend_control_errors_preserve_status_detail_and_retry_after():
    control_error = SimpleNamespace(
        failure=SimpleNamespace(
            status_code=409,
            code="evaluation_request_busy",
            detail={"reason": "evaluation_request_busy"},
            retry_after="1",
        )
    )

    error = service.backend_control_http_exception(control_error)

    assert error.status_code == 409
    assert error.detail == {"reason": "evaluation_request_busy"}
    assert error.headers == {"Retry-After": "1"}


def _controlled_segment_stage(**overrides):
    # A stage is only in scope once it names a controlled model; these cases must
    # exercise the controlled segmentation root guards rather than be skipped.
    stage = {
        "model_id": f"trained:{uuid.uuid4()}",
        "task": "segment",
        "parent_stage": None,
    }
    stage.update(overrides)
    return stage


@pytest.mark.parametrize(
    "stage",
    [
        _controlled_segment_stage(write={"target": "attributes"}),
        _controlled_segment_stage(input={"mode": "parent_crop"}),
        _controlled_segment_stage(parent_stage=0),
        _controlled_segment_stage(roi={"x": 0}),
        _controlled_segment_stage(source={"kind": "scene"}),
        _controlled_segment_stage(source={"data_type": "video"}),
    ],
)
def test_controlled_scope_rejects_non_image_root_contracts(stage):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        service.assert_image_scope(
            SimpleNamespace(data_type="image"), pipeline_stages=[stage]
        )
    assert error.value.status_code == 422


def test_backend_download_is_absolute_with_a_relative_browser_proxy(monkeypatch):
    from urllib.parse import urlsplit
    from app.config import settings

    bucket = "retained-model-bucket"
    monkeypatch.setattr(settings, "minio_public_url", "/minio")
    monkeypatch.setattr(settings, "ml_backend_storage_host", "")
    url = service.download_url("owned/weights.pt", bucket=bucket)
    parsed = urlsplit(url)
    assert parsed.scheme in {"http", "https"} and parsed.netloc
    assert parsed.path.endswith("/owned/weights.pt")
    # The saved bucket travels with the signed request; the relative browser
    # proxy route never prefixes a backend-reachable URL.
    assert bucket in parsed.netloc or parsed.path.startswith(f"/{bucket}/")
    assert not parsed.path.startswith("/minio/")
    public_url = service.storage.generate_download_url("owned/input.png")
    assert public_url.startswith("/minio/")


async def test_frozen_model_revalidates_current_project_bindings(
    db_session,
    super_admin,
    httpx_client,
    controlled_artifacts,
):
    owner, token = super_admin
    project, run, attempt = await seed_source(db_session, owner)
    data, _ = await register(httpx_client, token, project, run, attempt)
    version = await db_session.get(TrainedModelVersion, uuid.UUID(data["id"]))
    _, deployment = await ready_deployment(db_session, project, version)
    ref = build_reference(version, deployment)
    port = service.bind_frozen_model_reference(db_session)
    assert ref["release_id"] is None and ref["release_generation"] is None
    request = ValidateFrozenModelReferenceRequest(project_id=project.id, reference=ref)
    assert await port.validate(request) == deployment.registry_id

    extra_field = {**ref, "future_identity": "preserve-and-reject"}
    extra_field["reference_digest"] = content_digest(
        {key: value for key, value in extra_field.items() if key != "reference_digest"}
    )
    # Unknown keys are not normalized away: the whole reference is invalid.
    with pytest.raises(FreezeModelReferenceError) as unknown:
        await port.validate(
            ValidateFrozenModelReferenceRequest(
                project_id=project.id, reference=extra_field
            )
        )
    assert unknown.value.code == "model_reference_invalid"

    absent_release_fields = {
        key: value
        for key, value in ref.items()
        if key not in {"release_id", "release_generation"}
    }
    absent_release_fields["reference_digest"] = content_digest(
        {
            key: value
            for key, value in absent_release_fields.items()
            if key != "reference_digest"
        }
    )
    # Absent pinned release fields are an invalid reference, not a shorthand.
    with pytest.raises(FreezeModelReferenceError) as missing:
        await port.validate(
            ValidateFrozenModelReferenceRequest(
                project_id=project.id, reference=absent_release_fields
            )
        )
    assert missing.value.code == "model_reference_invalid"

    project.tool_bindings = {"bbox": {"classes": [{"name": "different"}]}}
    with pytest.raises(FreezeModelReferenceError) as failure:
        await port.validate(request)
    assert failure.value.status_code == 422
    assert failure.value.code == "model_reference_invalid"
