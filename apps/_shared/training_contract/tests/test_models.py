"""Family expansion preserves historical contracts and rejects invented models."""

from copy import deepcopy

import pytest

from training_contract import definition, resolve, verify_frozen
from training_contract.catalog import supported_identities
from training_contract.dataset import SEGMENTATION, INSTANCE_RLE_CONTRACT
from training_contract.initialization import policy_for_model, supported_policy_digests
from training_contract.model_return import (
    registration_eligibility,
    segmentation_identity,
)
from training_contract.models import MODEL_SPECS, model_spec
from training_contract.validation import CANONICAL, configuration_for_data_contract


def test_registry_is_exact_native_matrix_without_libre_e():
    expected = {
        f"{family}{scale}{suffix}"
        for family in ("yolov8", "yolo11", "yolo26")
        for scale in "nsmlx"
        for suffix in ("", "-seg")
    } | {f"libreyolo9{scale}" for scale in "tsmc"}
    assert len(MODEL_SPECS) == 34
    assert {spec.id for spec in MODEL_SPECS} == expected
    assert model_spec("libreyolo", "LibreYOLO9c").scale == "c"
    for framework, name in [
        ("libreyolo", "libreyolo9e"),
        ("ultralytics", "yolov8n-pose"),
        ("ultralytics", "yolo26z"),
        ("ultralytics", "other.yaml"),
    ]:
        with pytest.raises(ValueError):
            model_spec(framework, name)
        with pytest.raises(ValueError):
            definition(framework, name)


@pytest.mark.parametrize("spec", MODEL_SPECS, ids=lambda spec: spec.id)
def test_each_model_freezes_real_family_task_and_exact_runtime(spec):
    schema = definition(spec.framework, spec.id)
    assert (schema["model_family"], schema["task"]) == (spec.family, spec.task)
    configuration = resolve(
        spec.framework,
        spec.id,
        {
            "schema_id": schema["schema_id"],
            "schema_digest": schema["schema_digest"],
            "values": {"epochs": 2, "batch": 1, "imgsz": 128},
        },
    )
    config = {
        "framework": spec.framework,
        "framework_version": schema["framework_version"],
        "adapter_revision": 2,
        "base_model": {"id": spec.id},
        "classes": ["car"],
        "configuration": configuration,
        "parameters": {
            key: configuration["effective_values"][key] for key in CANONICAL
        },
    }
    if spec.task == SEGMENTATION:
        config["data_contract"] = INSTANCE_RLE_CONTRACT
        config["configuration"] = configuration_for_data_contract(
            configuration, INSTANCE_RLE_CONTRACT
        )
    verify_frozen(config)
    qualified = registration_eligibility(config)
    assert qualified["qualified"]
    if spec.framework == "ultralytics":
        assert schema["framework_version"] == "8.4.183"
        policy = policy_for_model(spec.id, "8.4.183")
        assert policy["model_family"] == spec.family
        assert spec.id in policy["qualified_models"]
        runtime = qualified["qualification"]["runtime_qualification"]
        assert runtime["ultralytics"] == "8.4.183"
        assert runtime["end2end"] is spec.end2end
        assert configuration["effective_values"]["nms"] is (
            False if spec.end2end else None
        )
        if spec.end2end:
            dfl = next(field for field in schema["fields"] if field["key"] == "dfl")
            assert dfl["status"] == "inapplicable"
            assert "dfl" not in configuration["effective_values"]
    broken = deepcopy(config)
    broken["framework_version"] = "0.0.0"
    with pytest.raises(ValueError):
        verify_frozen(broken)


def test_native_advertisement_does_not_grant_cross_version_resume():
    current = supported_identities({"ultralytics": "8.4.183"})
    historical = supported_identities({"ultralytics": "8.4.177"})
    assert {item["framework_version"] for item in current} == {"8.4.183"}
    assert {item["framework_version"] for item in historical} == {"8.4.177"}
    assert {item["model_family"] for item in current} == {"yolov8", "yolo11", "yolo26"}
    assert not set(supported_policy_digests("8.4.183")) & set(
        supported_policy_digests("8.4.177")
    )
    assert (
        segmentation_identity("yolo11n-seg")["runtime_qualification"]["ultralytics"]
        == "8.4.177"
    )
    with pytest.raises(ValueError):
        segmentation_identity("yolo26n-seg", "8.4.177")


def test_semantic_target_budget_fails_before_native_work():
    schema = definition("ultralytics", "yolo26x-seg")
    assert (
        next(field for field in schema["fields"] if field["key"] == "batch")["default"]
        == 1
    )
    configuration = resolve(
        "ultralytics",
        "yolo26x-seg",
        {
            "schema_id": schema["schema_id"],
            "schema_digest": schema["schema_digest"],
            "values": {"batch": 2, "imgsz": 1280},
        },
    )
    config = {
        "framework": "ultralytics",
        "framework_version": "8.4.183",
        "adapter_revision": 2,
        "base_model": {"id": "yolo26x-seg"},
        "classes": [str(index) for index in range(80)],
        "configuration": configuration,
        "parameters": {
            key: configuration["effective_values"][key] for key in CANONICAL
        },
    }
    with pytest.raises(ValueError, match="64000000"):
        verify_frozen(config)
