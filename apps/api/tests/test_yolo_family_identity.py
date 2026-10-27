"""Frozen family, native head and version identities at API trust boundaries."""

from copy import deepcopy
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

import pytest
from aap_inference_contract.execution import ModelTaskResult
from fastapi import HTTPException
from training_contract.catalog import digest
from training_contract.initialization import policy_for_model
from training_contract.model_return import segmentation_identity, ultralytics_identity
from training_contract.models import model_spec, models_for_framework

from app.schemas.training_initialization import (
    FrozenTrainingInitialization,
    FrozenSegmentationTrainingInitialization,
)
from app.services import (
    controlled_segmentation,
    managed_inference as managed,
    sample_evaluation_masks as masks,
    trained_models,
    training_initialization as initialization,
)
from tests.sample_evaluation_fixtures import profile
from tests.test_controlled_segmentation import example
from tests.test_segmentation_initialization_evidence import source_fixture


ULTRA_MODELS = [item.id for item in models_for_framework("ultralytics")]


@pytest.mark.parametrize("base_model", ULTRA_MODELS)
def test_receipt_freezes_exact_family_scale_and_head(base_model):
    spec = model_spec("ultralytics", base_model)
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
    version.provenance["model_return"] = {
        **qualification,
        "model_size": "m" if spec.scale != "m" else "s",
    }
    with pytest.raises(HTTPException):
        trained_models.expected_receipt(version)


@pytest.mark.parametrize(
    "base_model", [item for item in ULTRA_MODELS if not item.endswith("-seg")]
)
async def test_source_pin_and_public_schema_resolve_source_version_and_family(
    monkeypatch, base_model
):
    project, version, DB, _, _, _ = source_fixture(monkeypatch)
    _, run, dataset = list(DB().rows)
    config = run.config
    config.update(
        task="detect", framework_version="8.4.183", base_model={"id": base_model}
    )
    run.config_digest = digest(config)
    version.framework_version = "8.4.183"
    version.provenance.update(
        config_digest=run.config_digest, base_model=config["base_model"]
    )
    rows = iter([version, run, dataset])
    db = SimpleNamespace(scalar=AsyncMock(side_effect=lambda _: next(rows)))
    # Reuse the complete source attempt, not a serving installation.
    attempt = await DB().get(None, None)
    db.get = AsyncMock(return_value=attempt)
    pin, _ = await initialization.source_pin(db, project, version.id)
    policy = policy_for_model(base_model, "8.4.183")
    assert pin["policy_digest"] == digest(policy)
    assert pin["task"] == "detect"
    assert pin["model_family"] == model_spec("ultralytics", base_model).family
    assert FrozenTrainingInitialization.model_validate(pin).base_model == base_model
    assert (
        initialization.compatible_pin(pin, "ultralytics", base_model, ["shape"]) == []
    )
    other_family = "yolov8n" if pin["model_family"] != "yolov8" else "yolo11n"
    assert initialization.compatible_pin(pin, "ultralytics", other_family, ["shape"])


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
    native = model_spec("ultralytics", base_model)
    ref = {
        "deployment_id": str(uuid.uuid4()),
        "model_version_id": str(uuid.uuid4()),
        "registry_id": str(uuid.uuid4()),
        "framework": "ultralytics",
        "framework_version": "8.4.183",
        "task": native.task,
        "provenance": {"base_model": {"id": base_model}},
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
    monkeypatch.setattr(
        managed.trained_models,
        "bind_frozen_model_reference",
        lambda _db: SimpleNamespace(freeze=AsyncMock(return_value=ref)),
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
    db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                model_version_id=ref["model_version_id"],
                registry_id=uuid.UUID(ref["registry_id"]),
            )
        )
    )
    _, checked = await managed.qualification(
        db,
        SimpleNamespace(id=uuid.uuid4(), tool_bindings={}),
        ref["model_version_id"],
        uuid.UUID(ref["deployment_id"]),
    )
    remote.assert_awaited_once_with(uuid.UUID(ref["registry_id"]))
    assert checked["ultralytics_version"] == "8.4.183"
    runtime["ultralytics_version"] = "8.4.177"
    runtime["profile_digest"] = digest(
        {key: value for key, value in runtime.items() if key != "profile_digest"}
    )
    with pytest.raises(HTTPException) as error:
        await managed.qualification(
            db,
            SimpleNamespace(id=uuid.uuid4(), tool_bindings={}),
            ref["model_version_id"],
            uuid.UUID(ref["deployment_id"]),
        )
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
    from app.schemas.training_initialization import TrainingInitialization
    from app.services import training_runs

    project, version, DB, _, _, _ = source_fixture(monkeypatch)
    pin, metrics = await initialization.source_pin(DB(), project, version.id)
    monkeypatch.setattr(
        initialization, "source_pin", AsyncMock(return_value=(pin, metrics))
    )
    monkeypatch.setattr(
        training_runs,
        "controlled_template",
        lambda *_: {"framework_version": "8.4.183"},
    )
    read_storage = AsyncMock()
    monkeypatch.setattr(initialization, "read_storage", read_storage)
    source = TrainingInitialization(**initialization.reference(pin))
    with pytest.raises(HTTPException, match="跨版本"):
        await initialization.resolve(
            DB(), project, source, "ultralytics", "yolo11n-seg"
        )
    read_storage.assert_not_awaited()
    eligibility = await initialization.eligibility(
        DB(), project, version.id, "ultralytics", "yolo11n-seg"
    )
    assert not eligibility["eligible"]
    assert any("跨版本" in reason for reason in eligibility["reasons"])
    # An accepted historical pin validates against its own immutable runtime.
    assert (
        await initialization.validate_accepted(
            DB(), project, pin, "ultralytics", "yolo11n-seg"
        )
        == pin
    )


@pytest.mark.parametrize(
    "base_model", [item for item in ULTRA_MODELS if item.endswith("-seg")]
)
async def test_segment_source_pin_resolves_the_new_family_policy_and_raw_report(
    monkeypatch, base_model
):
    project, version, DB, report, metrics, _ = source_fixture(
        monkeypatch, base_model=base_model, framework_version="8.4.183"
    )
    pin, frozen_metrics = await initialization.source_pin(DB(), project, version.id)
    assert pin["policy_digest"] == digest(policy_for_model(base_model, "8.4.183"))
    assert pin["model_family"] == model_spec("ultralytics", base_model).family
    assert pin["evaluation"]["report_digest"] == digest(report)
    assert pin["evaluation"]["normalized_model_digest"] == digest(version.evaluation)
    assert (
        FrozenSegmentationTrainingInitialization.model_validate(pin).base_model
        == base_model
    )
    assert frozen_metrics == metrics
