"""Frozen family, native head and version identities at API trust boundaries."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

import pytest
from yoao_inference_contract.execution import ModelTaskResult
from fastapi import HTTPException
from training_contract.trainer_plugin import canonical_json_digest as digest

from app.config import settings
from app.schemas.training_initialization import (
    FrozenTrainingInitialization,
    FrozenSegmentationTrainingInitialization,
    TrainingInitialization,
)
from app.services import (
    controlled_segmentation,
    managed_inference as managed,
    sample_evaluation_masks as masks,
    trained_models,
    training_adapters,
    training_initialization as initialization,
)
from tests.sample_evaluation_fixtures import profile
from tests.test_controlled_segmentation import example
from tests.test_segmentation_initialization_evidence import (
    LearningFactsSession,
    source_fixture,
)
from tests.trainer_plugin_fixtures import (
    ALL_ULTRALYTICS_MODEL_IDS,
    _parse_ultralytics_model,
    segmentation_identity,
    ultralytics_identity,
)


ULTRA_MODELS = list(ALL_ULTRALYTICS_MODEL_IDS)


def _spec(base_model: str):
    family, scale, task, end2end = _parse_ultralytics_model(base_model)
    return SimpleNamespace(
        id=base_model,
        family=family,
        scale=scale,
        task=task,
        end2end=end2end,
    )


@pytest.mark.parametrize("base_model", ULTRA_MODELS)
def test_receipt_freezes_exact_family_scale_and_head(base_model):
    spec = _spec(base_model)
    qualification = ultralytics_identity(base_model, "8.4.183")
    version = SimpleNamespace(
        id=uuid.uuid4(),
        artifact={"sha256": "a" * 64},
        classes=["shape"],
        framework="ultralytics",
        framework_version="8.4.183",
        provenance={
            "task": spec.task,
            "base_model": {"id": base_model},
            "model_return": qualification,
        },
    )
    receipt = trained_models.expected_receipt(version)
    assert receipt["model_family"] == spec.family
    assert receipt["model_size"] == spec.scale
    assert receipt["runtime_qualification"]["end2end"] is spec.end2end
    assert receipt["task"] == (
        "segmentation" if spec.task == "segment" else "detection"
    )


@pytest.mark.parametrize(
    "base_model", [item for item in ULTRA_MODELS if not item.endswith("-seg")]
)
async def test_source_pin_and_public_schema_resolve_source_version_and_family(
    monkeypatch, base_model
):
    spec = _spec(base_model)
    project, version, DB, _, _, _ = source_fixture(
        monkeypatch, base_model=base_model, framework_version="8.4.183"
    )
    db = DB()
    pin, _ = await initialization.source_pin(db, project, version.id)
    assert pin["policy_digest"] == digest(
        {
            "framework": "ultralytics",
            "framework_version": "8.4.183",
            "task": "detect",
            "model_family": spec.family,
            "base_model": base_model,
        }
    )
    assert pin["task"] == "detect"
    assert pin["model_family"] == spec.family
    assert pin["model_version_id"] == str(version.id)
    assert pin["framework_version"] == "8.4.183"
    db.storage_transport.client.get_object.assert_not_called()
    assert FrozenTrainingInitialization.model_validate(pin).base_model == base_model
    assert (
        initialization.compatible_pin(
            pin,
            "ultralytics",
            base_model,
            ["shape"],
            framework_version="8.4.183",
            expected_task="detect",
        )
        == []
    )
    other_family = "yolov8n" if pin["model_family"] != "yolov8" else "yolo11n"
    assert initialization.compatible_pin(
        pin,
        "ultralytics",
        other_family,
        ["shape"],
        framework_version="8.4.183",
        expected_task="detect",
    )


@pytest.mark.parametrize("base_model", ["yolov8s-seg", "yolo11x-seg", "yolo26m-seg"])
def test_mask_validation_keeps_version_and_native_head_bound_to_frozen_reference(
    base_model,
):
    result, ref, pixels = example()
    qualification = segmentation_identity(base_model, "8.4.183")
    ref.update(
        qualification,
        framework_version="8.4.183",
        provenance={"base_model": {"id": base_model}},
    )
    runtime = qualification["runtime_qualification"]
    with pytest.raises(ValueError, match="profile versions"):
        controlled_segmentation.runtime_for_reference(ref, "8.4.177")
    result.meta["segmentation"].update(
        runtime_qualification=runtime,
        decoder=runtime["decoder"],
        mask_processing=runtime["mask_processing"],
    )
    original = deepcopy(result)
    controlled_segmentation.validate_results((ModelTaskResult(**vars(result)),), ref)
    wire = original.meta
    predictions = masks.predictions_from_wire(
        original.result,
        [0],
        metadata=wire,
        width=5,
        height=3,
        runtime_qualification=controlled_segmentation.runtime_for_reference(ref),
    )
    assert predictions[0]["area_pixels"] == sum(pixels)
    with pytest.raises(ValueError):
        masks.predictions_from_wire(
            original.result, [0], metadata=wire, width=5, height=3
        )
    mismatched = deepcopy(ref)
    mismatched["provenance"]["base_model"]["id"] = (
        "yolo11n-seg" if base_model.startswith("yolo26") else "yolo26n-seg"
    )
    with pytest.raises(ValueError):
        controlled_segmentation.runtime_for_reference(mismatched)


def managed_case(base_model):
    native = _spec(base_model)
    qualification = ultralytics_identity(base_model, "8.4.183")
    ref = {
        "deployment_id": str(uuid.uuid4()),
        "model_version_id": str(uuid.uuid4()),
        "registry_id": str(uuid.uuid4()),
        "framework": "ultralytics",
        "framework_version": "8.4.183",
        "task": native.task,
        "runtime_qualification": qualification["runtime_qualification"],
        "provenance": {
            "base_model": {"id": base_model},
            "model_return": qualification,
        },
    }
    runtime = profile()
    runtime.update(
        revision=2,
        ultralytics_version="8.4.183",
        implementation="aap-ultralytics-instance-mask"
        if native.task == "segment"
        else "aap-ultralytics-detection",
    )
    if native.end2end:
        runtime["options"]["nms"] = False
    runtime["profile_digest"] = digest(
        {key: value for key, value in runtime.items() if key != "profile_digest"}
    )
    return native, ref, runtime


@pytest.mark.parametrize(
    "base_model",
    ["yolov8s", "yolo11m", "yolo26x", "yolov8s-seg", "yolo11l-seg", "yolo26m-seg"],
)
async def test_managed_profile_requests_exact_frozen_model_and_checks_native_branch(
    monkeypatch, base_model
):
    native, ref, runtime = managed_case(base_model)
    project = SimpleNamespace(id=uuid.uuid4(), tool_bindings={})
    model_id = uuid.UUID(ref["model_version_id"])
    deployment_id = uuid.UUID(ref["deployment_id"])
    registry_id = uuid.UUID(ref["registry_id"])
    freeze = AsyncMock(return_value=ref)
    monkeypatch.setattr(
        managed.trained_models,
        "bind_frozen_model_reference",
        lambda _db: SimpleNamespace(freeze=freeze),
    )
    monkeypatch.setattr(
        managed.trained_models,
        "enabled_backend",
        AsyncMock(return_value=SimpleNamespace()),
    )
    remote = AsyncMock(return_value=runtime)
    monkeypatch.setattr(
        "app.services.inference_backend_control.backend_control_service",
        lambda _db: SimpleNamespace(managed_evaluation_profile=remote),
    )
    monkeypatch.setattr(
        "app.services.inference_topology_data.InferenceTopologyData.registry_facts",
        AsyncMock(
            return_value=SimpleNamespace(
                id=registry_id,
                is_active=True,
                health_meta={
                    "capabilities": {
                        "managed_evaluation": True,
                        "managed_model_compatibility": [{"framework": "ultralytics"}],
                    }
                },
                extra_params={},
            )
        ),
    )
    db = LearningFactsSession(
        {
            ("model_deployment_detail", project.id, model_id, deployment_id): {
                "id": deployment_id,
                "model_version_id": model_id,
                "registry_id": registry_id,
                "check_id": uuid.uuid4(),
                "status": "ready",
                "receipt": {},
                "trial": {},
                "error_message": None,
                "checked_at": None,
                "created_at": datetime.now(UTC),
            }
        }
    )
    _, checked = await managed.qualification(db, project, model_id, deployment_id)
    request = freeze.await_args.args[0]
    assert request.project_id == project.id
    assert request.model_id == f"trained:{model_id}"
    assert request.registry_id == registry_id
    remote.assert_awaited_once_with(registry_id)
    assert checked["ultralytics_version"] == "8.4.183"
    if native.end2end:
        runtime["options"]["nms"] = True
        runtime["profile_digest"] = digest(
            {key: value for key, value in runtime.items() if key != "profile_digest"}
        )
        with pytest.raises(HTTPException) as error:
            await managed.qualification(db, project, model_id, deployment_id)
        assert error.value.detail["code"] == "runtime_unqualified"
        runtime["options"]["nms"] = False
    runtime["ultralytics_version"] = "8.4.177"
    runtime["profile_digest"] = digest(
        {key: value for key, value in runtime.items() if key != "profile_digest"}
    )
    with pytest.raises(HTTPException) as error:
        await managed.qualification(db, project, model_id, deployment_id)
    assert error.value.detail["code"] == "deployment_runtime_changed"


@pytest.mark.parametrize(
    "base_model", ["yolov8n", "yolo11n-seg", "yolo26n", "yolo26n-seg"]
)
def test_observed_end2end_cannot_change_from_the_frozen_family(base_model):
    native, ref, runtime = managed_case(base_model)
    spec = {
        "model_ref": ref,
        "profile": runtime,
        "samples": [{"width": 32, "height": 32}],
        "confidence": 0.25,
        "nms_iou": 0.7,
    }
    row = SimpleNamespace(
        ordinal=0, request={"evaluation": {"request_id": str(uuid.uuid4())}}
    )
    inference = {
        **row.request["evaluation"],
        "width": 32,
        "height": 32,
        "effective_device": runtime["effective_device"],
        "options": runtime["options"],
        "conf": 0.25,
        "nms_iou": 0.7,
        "end2end": native.end2end,
    }
    managed.verify_inference(spec, row, inference)
    with pytest.raises(ValueError, match="execution identity"):
        managed.verify_inference(
            spec, row, {**inference, "end2end": not native.end2end}
        )
    if native.end2end:
        runtime["options"]["nms"] = True
        with pytest.raises(ValueError, match="native head"):
            managed.verify_inference(spec, row, inference)


async def test_cross_version_initialization_fails_before_reading_source_storage(
    monkeypatch,
):
    monkeypatch.setattr(settings, "training_enabled", True)
    monkeypatch.setattr(settings, "training_executor_token", "a" * 32)
    monkeypatch.setattr(settings, "training_gpu_uuid", "GPU-test")
    project, version, DB, _, metrics, owner = source_fixture(monkeypatch)
    db = DB()
    pin, _ = await initialization.source_pin(db, project, version.id)
    assert pin["framework_version"] == "8.4.177"
    storage = db.storage_transport.client
    storage.get_object.reset_mock()
    monkeypatch.setattr(
        training_adapters,
        "plugin_template",
        AsyncMock(return_value={"framework_version": "8.4.183", "task": "segment"}),
    )
    source = TrainingInitialization(**initialization.reference(pin))
    with pytest.raises(HTTPException, match="跨版本"):
        await initialization.resolve(db, project, source, "ultralytics", "yolo11n-seg")
    storage.get_object.assert_called_once_with(Bucket=owner.bucket, Key=metrics["key"])
    storage.head_object.assert_not_called()
    eligibility = await initialization.eligibility(
        db, project, version.id, "ultralytics", "yolo11n-seg"
    )
    assert not eligibility["eligible"]
    assert any("跨版本" in reason for reason in eligibility["reasons"])
    assert eligibility["pin"] == pin
    storage.get_object.reset_mock()
    assert (
        await initialization.validate_accepted(
            db, project, pin, "ultralytics", "yolo11n-seg"
        )
        == pin
    )
    storage.get_object.assert_any_call(Bucket=owner.bucket, Key=version.artifact["key"])


@pytest.mark.parametrize(
    "base_model", [item for item in ULTRA_MODELS if item.endswith("-seg")]
)
async def test_segment_source_pin_resolves_the_new_family_policy_and_raw_report(
    monkeypatch, base_model
):
    spec = _spec(base_model)
    project, version, DB, report, metrics, owner = source_fixture(
        monkeypatch, base_model=base_model, framework_version="8.4.183"
    )
    db = DB()
    pin, frozen_metrics = await initialization.source_pin(db, project, version.id)
    db.storage_transport.client.get_object.assert_called_once_with(
        Bucket=owner.bucket, Key=metrics["key"]
    )
    assert pin["model_version_id"] == str(version.id)
    assert pin["framework_version"] == "8.4.183"
    assert pin["policy_digest"] == digest(
        {
            "framework": "ultralytics",
            "framework_version": "8.4.183",
            "task": "segment",
            "model_family": spec.family,
            "base_model": base_model,
        }
    )
    assert pin["model_family"] == spec.family
    assert pin["evaluation"]["report_digest"] == digest(report)
    assert pin["evaluation"]["normalized_model_digest"] == digest(version.evaluation)
    assert (
        FrozenSegmentationTrainingInitialization.model_validate(pin).base_model
        == base_model
    )
    assert frozen_metrics == metrics
