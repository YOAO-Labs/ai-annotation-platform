"""Versioned API inputs, native receipts, executor qualification and legacy replay."""

import io
import uuid

import pytest
from fastapi import HTTPException
from training_contract.trainer_plugin import TRAINER_PROTOCOL_VERSION

from app.db.models.training_run import TrainingRun, TrainingAttempt
from app.schemas.training_run import ExecutorFinish, TrainingRunCreate
from app.services.training_datasets import bind_training_authority
from app.services import training_runs as service
from app.services import training_adapters as adapters
from app.services import training_claims as claims
from app.services.training_dataset_bridge import content_digest
from tests.test_training_runs import ready_dataset, executor_claim, fence
from tests.test_training_runs import training_config as training_config_fixture  # noqa: F401
from tests.trainer_plugin_fixtures import (
    ULTRALYTICS_VERSION,
    build_v3_receipt,
    ultralytics_schema,
)

pytestmark = pytest.mark.usefixtures("training_config_fixture")


def configuration(**values):
    schema = ultralytics_schema("yolo11n")
    return {
        "schema_id": schema["schema_id"],
        "schema_digest": schema["schema_digest"],
        "values": values,
    }


def segmentation_catalog(monkeypatch):
    schema = ultralytics_schema("yolo11n-seg")
    return {
        "schema_id": schema["schema_id"],
        "schema_digest": schema["schema_digest"],
        "values": {"epochs": 2, "batch": 1, "imgsz": 128},
    }


async def test_segmentation_requires_exact_dataset_base_schema_and_keeps_recipes_independent(
    db_session, super_admin, httpx_client, monkeypatch
):
    from training_contract.dataset import SEGMENTATION_CONTRACT
    from app.services import training_recipes

    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    configuration = segmentation_catalog(monkeypatch)
    path = f"/api/v1/projects/{project.id}/training-runs:preflight"
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "name": "Segmentation",
        "dataset_id": str(dataset.id),
        "base_model": "yolo11n-seg",
        "configuration": configuration,
        "request_id": str(uuid.uuid4()),
    }
    rejected = await httpx_client.post(path, headers=headers, json=body)
    assert rejected.status_code == 422 and "不匹配" in rejected.text
    dataset.manifest = {**dataset.manifest, **SEGMENTATION_CONTRACT}
    dataset.snapshot = {**SEGMENTATION_CONTRACT, "classes": dataset.manifest["classes"]}
    await db_session.flush()
    accepted = await httpx_client.post(path, headers=headers, json=body)
    assert accepted.status_code == 200, accepted.text
    config = accepted.json()["config"]
    assert config["data_contract"] == SEGMENTATION_CONTRACT
    assert config["configuration"]["effective_values"]["task"] == "segment"
    assert set(config["base_model"]) == {"id", "sha256", "filename", "family"}
    no_contract = await httpx_client.post(
        path,
        headers=headers,
        json={key: value for key, value in body.items() if key != "configuration"},
    )
    assert no_contract.status_code == 422 and "完整" in no_contract.text
    wrong_base = await httpx_client.post(
        path, headers=headers, json={**body, "base_model": "yolo11n"}
    )
    assert wrong_base.status_code == 422 and "不匹配" in wrong_base.text
    reusable = await training_recipes.snapshot(
        db_session, "ultralytics", "yolo11n-seg", configuration
    )
    assert (
        reusable["configuration"]["identity"]["task"] == SEGMENTATION_CONTRACT["task"]
    )
    assert await training_recipes.compatibility(db_session, reusable) == (True, None)
    with pytest.raises(HTTPException, match="任务"):
        await claims.fixed_policy_template(db_session, "ultralytics", "yolo11n-seg")
    model = next(
        base
        for item in (await adapters.resolve_catalog(db_session))["items"]
        for base in item["base_models"]
        if base["id"] == "yolo11n-seg"
    )
    assert (
        model["capabilities"]["artifact_download"]
        and model["capabilities"]["model_registration"]
    )
    study = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-studies:preview",
        headers=headers,
        json={
            "base": body,
            "grid": {"lr0": [0.01]},
            "objective": "metrics/mAP50-95",
            "direction": "maximize",
            "budgets": {
                "max_trials": 1,
                "max_planned_epochs": 2,
                "max_native_seconds": 10,
            },
        },
    )
    assert study.status_code == 422 and "仅通过图片检测" in study.text
    preview = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-previews",
        headers=headers,
        json={
            **{key: value for key, value in body.items() if key != "name"},
            "framework": "ultralytics",
        },
    )
    assert preview.status_code == 422 and "图片检测" in preview.text
    policy = await httpx_client.put(
        f"/api/v1/projects/{project.id}/learning-policy",
        headers=headers,
        json={
            "expected_revision": 0,
            "parent_id": str(dataset.id),
            "base_model": "yolo11n",
            "min_changed_samples": 1,
            "check_interval_minutes": 1,
            "cooldown_minutes": 0,
        },
    )
    assert policy.status_code == 422 and "任务与父数据版本不匹配" in policy.text


def test_runtime_compatibility_requires_v3_protocol_mode_gpu_and_base_asset():
    base_sha = "2" * 64
    config = {
        "framework": "ultralytics",
        "framework_version": ULTRALYTICS_VERSION,
        "adapter_revision": 3,
        "trainer_protocol": TRAINER_PROTOCOL_VERSION,
        "base_model": {"id": "yolo11n", "sha256": base_sha},
        "trainer_plugin": {
            "plugin_id": "yoao.trainer.ultralytics",
            "execution_mode": "container",
            "requires_gpu": True,
        },
    }
    runtime = {
        "executor_protocol": 3,
        "trainer_protocols": [TRAINER_PROTOCOL_VERSION],
        "execution_modes": ["container"],
        "gpu_uuid": "GPU-11111111-2222-3333-4444-555555555555",
        "base_asset_sha256": [base_sha],
    }
    assert service.runtime_compatible(config, runtime)
    for bad_runtime in (
        {**runtime, "executor_protocol": 2},
        {**runtime, "trainer_protocols": []},
        {**runtime, "execution_modes": ["local_process"]},
        {**runtime, "gpu_uuid": "cpu-0"},
        {**runtime, "base_asset_sha256": []},
    ):
        assert not service.runtime_compatible(config, bad_runtime)
    local_config = {
        **config,
        "trainer_plugin": {
            "plugin_id": "yoao.trainer.ultralytics",
            "execution_mode": "local_process",
            "requires_gpu": False,
        },
    }
    local_runtime = {
        "executor_protocol": 3,
        "trainer_protocols": [TRAINER_PROTOCOL_VERSION],
        "execution_modes": ["local_process"],
        "allowed_local_plugins": ["yoao.trainer.ultralytics"],
        "base_asset_sha256": [base_sha],
    }
    assert service.runtime_compatible(local_config, local_runtime)
    assert not service.runtime_compatible(
        local_config, {**local_runtime, "allowed_local_plugins": []}
    )


async def new_v2(client, token, project, dataset):
    path = f"/api/v1/projects/{project.id}/training-runs"
    body = {
        "name": "Contract v2",
        "dataset_id": str(dataset.id),
        "request_id": str(uuid.uuid4()),
        "configuration": configuration(
            epochs=2, optimizer="AdamW", mosaic=0.5, close_mosaic=0
        ),
    }
    headers = {"Authorization": f"Bearer {token}"}
    preflight = await client.post(path + ":preflight", headers=headers, json=body)
    assert preflight.status_code == 200, preflight.text
    body["preflight_digest"] = preflight.json()["config_digest"]
    created = await client.post(path, headers=headers, json=body)
    assert created.status_code == 202, created.text
    return created.json(), body


def test_legacy_request_serialization_keeps_exact_existing_shape():
    body = TrainingRunCreate(
        name="Old", dataset_id=uuid.uuid4(), request_id=uuid.uuid4()
    )
    legacy = {
        "name": body.name,
        "dataset_id": str(body.dataset_id),
        "framework": "ultralytics",
        "base_model": "yolo11n",
        "parameters": {
            "epochs": 50,
            "batch": 8,
            "imgsz": 640,
            "seed": 42,
            "lr0": 0.01,
            "patience": 50,
        },
        "request_id": str(body.request_id),
        "preflight_digest": None,
    }
    assert body.request_payload() == legacy
    assert content_digest(body.request_payload()) == content_digest(legacy)


async def test_parameter_schema_is_project_authorized_and_complete(
    db_session, super_admin, httpx_client, annotator
):
    owner, token = super_admin
    project, _ = await ready_dataset(db_session, owner)
    path = f"/api/v1/projects/{project.id}/training-runs/parameter-schema?framework=ultralytics&base_model=yolo11n"
    result = await httpx_client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert result.status_code == 200, result.text
    expected_schema = ultralytics_schema("yolo11n")
    assert len(result.json()["fields"]) == len(expected_schema["fields"])
    assert result.json()["schema_digest"] == expected_schema["schema_digest"]
    _, other_token = annotator
    forbidden = await httpx_client.get(
        path, headers={"Authorization": f"Bearer {other_token}"}
    )
    assert forbidden.status_code == 404


@pytest.mark.parametrize(
    "values",
    [
        {"device": 1},
        {"optimizer": "auto"},
        {"unknown": 1},
        {"epochs": True},
        {"epochs": 10**400},
        {"epochs": 0},
    ],
)
async def test_invalid_v2_values_fail_before_enqueue(
    db_session, super_admin, httpx_client, values
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    payload = {
        "name": "Invalid",
        "dataset_id": str(dataset.id),
        "request_id": str(uuid.uuid4()),
        "configuration": configuration(**values),
    }
    path = f"/api/v1/projects/{project.id}/training-runs:preflight"
    response = await httpx_client.post(
        path, headers={"Authorization": f"Bearer {token}"}, json=payload
    )
    assert response.status_code == 422, response.text
    response = await httpx_client.post(
        path,
        headers={"Authorization": f"Bearer {token}"},
        json={**payload, "parameters": {"epochs": 2}},
    )
    assert response.status_code == 422


async def test_old_executors_cannot_claim_v2_and_new_executor_must_advertise_exact_schema(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    data, payload = await new_v2(httpx_client, token, project, dataset)
    assert data["config"]["parameters"]["epochs"] == 2
    assert data["config"]["configuration"]["effective_values"]["optimizer"] == "AdamW"
    claim = executor_claim()
    claim.runtime["executor_protocol"] = 2
    assert (
        await service.claim(
            db_session, claim, training_authority=bind_training_authority(db_session)
        )
        is None
    )
    claim.runtime.update(executor_protocol=3, trainer_protocols=[])
    assert (
        await service.claim(
            db_session, claim, training_authority=bind_training_authority(db_session)
        )
        is None
    )
    claim.runtime.update(
        trainer_protocols=[TRAINER_PROTOCOL_VERSION], base_asset_sha256=[]
    )
    assert (
        await service.claim(
            db_session, claim, training_authority=bind_training_authority(db_session)
        )
        is None
    )
    claim = executor_claim()
    assignment = await service.claim(
        db_session, claim, training_authority=bind_training_authority(db_session)
    )
    assert assignment["run_id"] == data["id"]
    repeated = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-runs",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert repeated.status_code == 202 and repeated.json()["id"] == data["id"]


async def test_v2_success_requires_verified_native_observation_and_retains_lease_on_failure(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    data, _ = await new_v2(httpx_client, token, project, dataset)
    claim = executor_claim()
    assignment = await service.claim(
        db_session, claim, training_authority=bind_training_authority(db_session)
    )
    run = await db_session.get(TrainingRun, uuid.UUID(data["id"]))
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    base_inventory = {
        name: {"name": name, "role": role, "sha256": "a" * 64, "size_bytes": 10}
        for name, role in [
            ("best.pt", "weights"),
            ("checkpoint.pt", "checkpoint"),
            ("metrics.json", "metrics"),
            ("config.json", "config"),
            ("train.log", "log"),
        ]
    }
    attempt.upload_inventory = dict(base_inventory)
    monkeypatch.setattr(service, "verify_artifact", lambda *args, **kwargs: None)
    body = ExecutorFinish(
        **fence(claim, assignment), status="succeeded", process_exited=True
    )
    with pytest.raises(HTTPException) as error:
        await service.finish(
            db_session,
            attempt.id,
            body,
            training_authority=bind_training_authority(db_session),
        )
    assert error.value.status_code == 422
    assert (await service.lock_resource(db_session)).attempt_id == attempt.id
    bad_raw, bad_spec = build_v3_receipt(base_inventory, framework="other")
    attempt.upload_inventory = {**base_inventory, "result.json": bad_spec}
    monkeypatch.setattr(
        service.storage.client,
        "get_object",
        lambda **kwargs: {
            "Body": io.BytesIO(bad_raw),
            "ContentLength": len(bad_raw),
            "Metadata": {"attempt-id": str(attempt.id)},
        },
    )
    with pytest.raises(HTTPException):
        await service.finish(
            db_session,
            attempt.id,
            body,
            training_authority=bind_training_authority(db_session),
        )
    good_raw, good_spec = build_v3_receipt(base_inventory, framework="ultralytics")
    attempt.upload_inventory = {**base_inventory, "result.json": good_spec}
    monkeypatch.setattr(
        service.storage.client,
        "get_object",
        lambda **kwargs: {
            "Body": io.BytesIO(good_raw),
            "ContentLength": len(good_raw),
            "Metadata": {"attempt-id": str(attempt.id)},
        },
    )
    assert (
        await service.finish(
            db_session,
            attempt.id,
            body,
            training_authority=bind_training_authority(db_session),
        )
    ).status == "succeeded"


def test_training_request_task_aliases_conflicts_and_legacy_payload():
    from pydantic import ValidationError

    raw = {
        "name": "Explicit task",
        "dataset_id": uuid.uuid4(),
        "request_id": uuid.uuid4(),
    }
    old = TrainingRunCreate(**raw)
    assert "task" not in old.request_payload()
    canonical = TrainingRunCreate(**raw, task="segment", task_type="segmentation")
    assert (
        canonical.task == "segment" and canonical.request_payload()["task"] == "segment"
    )
    with pytest.raises(ValidationError):
        TrainingRunCreate(**raw, task="detect", task_type="segment")


async def test_explicit_training_task_must_match_frozen_dataset(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/training-runs:preflight",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "Wrong task",
            "task": "segment",
            "dataset_id": str(dataset.id),
            "request_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 422 and "任务" in response.text
