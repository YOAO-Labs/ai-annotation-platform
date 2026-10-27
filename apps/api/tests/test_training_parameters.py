"""Versioned API inputs, native receipts, executor qualification and legacy replay."""

import io
import json
import uuid
from copy import deepcopy

import pytest
from fastapi import HTTPException
from training_contract import definition, schema_identity
from training_contract.catalog import supported_identities

from app.config import settings
from app.db.models.training_run import TrainingRun, TrainingAttempt
from app.schemas.training_run import ExecutorFinish, TrainingRunCreate
from app.services.training_datasets import bind_training_authority
from app.services import training_runs as service
from app.services import training_adapters as adapters
from app.services import training_claims as claims
from app.services.training_dataset_bridge import content_digest
from tests.test_training_runs import ready_dataset, executor_claim, fence
from tests.test_training_runs import training_config as training_config_fixture  # noqa: F401

pytestmark = pytest.mark.usefixtures("training_config_fixture")


def configuration(**values):
    schema = definition("ultralytics", version="8.4.177")
    return {
        "schema_id": schema["schema_id"],
        "schema_digest": schema["schema_digest"],
        "values": values,
    }


def segmentation_catalog(monkeypatch):
    catalog = deepcopy(settings.training_catalog_json)
    catalog["ultralytics"]["base_models"]["yolo11n-seg"] = {
        "sha256": "55ed65c56c91713d23e8402371c6c49a6fd84f257f7dce452e8d70e41dcbe152",
        "filename": "yolo11n-seg.pt",
    }
    monkeypatch.setattr(settings, "training_catalog_json", catalog)
    schema = definition("ultralytics", "yolo11n-seg", version="8.4.177")
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
    assert set(config["base_model"]) == {"id", "sha256", "filename"}
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
    reusable = training_recipes.snapshot("ultralytics", "yolo11n-seg", configuration)
    assert (
        reusable["configuration"]["identity"]["task"] == SEGMENTATION_CONTRACT["task"]
    )
    assert training_recipes.compatibility(reusable) == (True, None)
    with pytest.raises(HTTPException, match="任务"):
        claims.fixed_policy_template("ultralytics", "yolo11n-seg")
    model = next(
        base
        for item in adapters.catalog()["items"]
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


def test_runtime_image_upgrade_requires_full_qualified_schema_and_source_image(
    monkeypatch,
):
    old, new = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    identity = schema_identity(definition("ultralytics", "yolo11n", version="8.4.177"))
    config = {
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "adapter_revision": 2,
        "image_digest": old,
        "base_model": {"id": "yolo11n"},
        "configuration": {"identity": identity},
    }
    runtime = {
        "frameworks": {"ultralytics": "8.4.177"},
        "image_digest": new,
        "executor_protocol": 2,
        "supported_configs": supported_identities(),
    }
    entry = {"version": "8.4.177", "image_digest": new, "legacy_images": [old]}
    monkeypatch.setattr(settings, "training_catalog_json", {"ultralytics": entry})
    assert not service.runtime_compatible(config, runtime)
    entry["compatible_config_images"] = {old: [identity]}
    assert service.runtime_compatible(config, runtime)
    for changed in [
        {"base_model": {"id": "yolo11n-seg"}},
        {"image_digest": "sha256:" + "c" * 64},
        {"configuration": {"identity": {**identity, "schema_digest": "0" * 64}}},
    ]:
        assert not service.runtime_compatible({**config, **changed}, runtime)
    seg_identity = schema_identity(
        definition("ultralytics", "yolo11n-seg", version="8.4.177")
    )
    segmentation = {
        **config,
        "image_digest": new,
        "base_model": {"id": "yolo11n-seg"},
        "configuration": {"identity": seg_identity},
    }
    old_runtime = {
        **runtime,
        "supported_configs": [
            item for item in supported_identities() if item["task"] == "image-detection"
        ],
    }
    assert not service.runtime_compatible(segmentation, old_runtime)
    assert service.runtime_compatible(segmentation, runtime)
    entry["compatible_config_images"][old].append(seg_identity)
    assert service.runtime_compatible({**segmentation, "image_digest": old}, runtime)


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
    assert len(result.json()["fields"]) == 114
    assert (
        result.json()["schema_digest"]
        == definition("ultralytics", version="8.4.177")["schema_digest"]
    )
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
        {"rect": True, "mosaic": 0.5},
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
    assert (
        await service.claim(
            db_session, claim, training_authority=bind_training_authority(db_session)
        )
        is None
    )
    claim.runtime.update(executor_protocol=2, supported_configs=[])
    assert (
        await service.claim(
            db_session, claim, training_authority=bind_training_authority(db_session)
        )
        is None
    )
    wrong = schema_identity(definition("ultralytics", version="8.4.177"))
    wrong["schema_digest"] = "0" * 64
    claim.runtime["supported_configs"] = [wrong]
    assert (
        await service.claim(
            db_session, claim, training_authority=bind_training_authority(db_session)
        )
        is None
    )
    claim.runtime["supported_configs"] = supported_identities()
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


def test_legacy_image_mapping_is_explicit_one_way_and_requires_v1_support(monkeypatch):
    old, new = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    config = {
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "adapter_revision": 1,
        "image_digest": old,
        "base_model": {"id": "yolo11n"},
    }
    runtime = {
        "frameworks": {"ultralytics": "8.4.177"},
        "image_digest": new,
        "executor_protocol": 2,
        "supported_configs": supported_identities(),
    }
    monkeypatch.setattr(
        settings,
        "training_catalog_json",
        {
            "ultralytics": {
                "version": "8.4.177",
                "image_digest": new,
                "legacy_images": [old],
            }
        },
    )
    assert service.runtime_compatible(config, runtime)
    for changed in (
        {"executor_protocol": 1},
        {"supported_configs": []},
        {"image_digest": old},
        {"frameworks": {"ultralytics": "other"}},
    ):
        candidate = {**runtime, **changed}
        if changed == {"image_digest": old}:
            # Exact frozen image remains valid; the allowlist is unnecessary.
            assert service.runtime_compatible(config, candidate)
        else:
            assert not service.runtime_compatible(config, candidate)
    monkeypatch.setattr(
        settings,
        "training_catalog_json",
        {
            "ultralytics": {
                "version": "8.4.177",
                "image_digest": old,
                "legacy_images": [new],
            }
        },
    )
    assert not service.runtime_compatible(config, runtime)


async def test_v2_success_requires_verified_native_observation_and_retains_lease_on_failure(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    data, _ = await new_v2(httpx_client, token, project, dataset)
    claim = executor_claim()
    claim.runtime.update(executor_protocol=2, supported_configs=supported_identities())
    assignment = await service.claim(
        db_session, claim, training_authority=bind_training_authority(db_session)
    )
    run = await db_session.get(TrainingRun, uuid.UUID(data["id"]))
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    attempt.upload_inventory = {
        name: {"name": name, "role": role, "sha256": "a" * 64, "size_bytes": 10}
        for name, role in [
            ("best.pt", "weights"),
            ("checkpoint.pt", "checkpoint"),
            ("metrics.json", "metrics"),
            ("config.json", "config"),
            ("train.log", "log"),
        ]
    }
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
    options = {
        "data": "/state/data.yaml",
        "model": "/models/yolo11n.pt",
        "project": "/state/attempt",
        "name": "framework",
        "exist_ok": False,
        "resume": False,
    }
    observed = {**run.config["configuration"]["effective_values"], **options}
    blob = {
        "config_digest": run.config_digest,
        "observed": observed,
        "runtime_options": options,
        "verification": service.verify_native(run.config, observed, options),
    }
    attempt.upload_inventory = {
        **attempt.upload_inventory,
        "native-config.json": {
            "name": "native-config.json",
            "role": "config",
            "sha256": "a" * 64,
            "size_bytes": 100,
        },
    }
    monkeypatch.setattr(
        service.storage.client,
        "get_object",
        lambda **kwargs: {"Body": io.BytesIO(json.dumps(blob).encode())},
    )
    blob["observed"]["optimizer"] = "SGD"
    with pytest.raises(HTTPException):
        await service.finish(
            db_session,
            attempt.id,
            body,
            training_authority=bind_training_authority(db_session),
        )
    blob["observed"]["optimizer"] = "AdamW"
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
