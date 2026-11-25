"""Qualification pins and native reset evidence cannot silently degrade."""

import copy
import uuid

import pytest

from training_contract.catalog import digest
from training_contract.initialization import (
    POLICY_DIGEST,
    compatible_runtime,
    validate_pin,
    verify_evidence,
)


def pin():
    identifiers = {
        key: str(uuid.uuid4())
        for key in (
            "project_id",
            "model_version_id",
            "run_id",
            "attempt_id",
            "dataset_id",
        )
    }
    classes, bindings = ["car", "person"], ["car", "person"]
    value = {
        "kind": "trained_model",
        "policy_digest": POLICY_DIGEST,
        **identifiers,
        "model_name": "Frozen candidate",
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "task": "image-detection",
        "model_family": "yolo11",
        "base_model": "yolo11n",
        "artifact": {
            "key": f"projects/{identifiers['project_id']}/runs/{identifiers['run_id']}/attempts/{identifiers['attempt_id']}/best.pt",
            "role": "weights",
            "sha256": "1" * 64,
            "size_bytes": 123,
        },
        "classes": classes,
        "classes_digest": digest(classes),
        "bindings": bindings,
        "binding_digest": digest({"classes": classes, "bindings": bindings}),
        "config_digest": "2" * 64,
        "dataset_digest": "3" * 64,
        "validation_digest": "4" * 64,
        "runtime": {
            "python": "3.12.3",
            "torch": "2.14.1+cu132",
            "torchvision": "0.29.1+cu132",
            "cuda": "13.2",
            "image_digest": "sha256:" + "5" * 64,
        },
        "evaluation": {
            "report_sha256": "6" * 64,
            "report_digest": "7" * 64,
            "validation_digest": "4" * 64,
        },
    }
    value["source_digest"] = digest(value)
    return value


def fresh(value):
    return {
        "policy_digest": POLICY_DIGEST,
        "source_digest": value["source_digest"],
        "source_sha256": value["artifact"]["sha256"],
        "mode": "weights_only",
        "source_tensor_digest": "8" * 64,
        "loaded_tensor_digest": "8" * 64,
        "tensor_count": 499,
        "start_epoch": 0,
        "optimizer_state_count": 0,
        "scheduler_last_epoch": -1,
        "scaler_state": {},
        "ema_updates": 0,
        "best_fitness": 0.0,
        "best_weights_present": False,
    }


@pytest.mark.parametrize("family", ["yolov8", "yolo11", "yolo26"])
@pytest.mark.parametrize("suffix", ["", "-seg"])
def test_current_initialization_policy_binds_exact_family_and_task(family, suffix):
    from training_contract.dataset import INSTANCE_RLE_CONTRACT
    from training_contract.initialization import policy_for_model

    value = pin()
    base = f"{family}x{suffix}"
    policy = policy_for_model(base, "8.4.183")
    value.update(
        policy_digest=digest(policy),
        base_model=base,
        model_family=family,
        framework_version="8.4.183",
        task=policy["task"],
    )
    if suffix:
        value["data_contract"] = INSTANCE_RLE_CONTRACT
        value["evaluation"].update(
            normalized_model_digest="a" * 64,
            descriptor_digest="b" * 64,
            weights_sha256=value["artifact"]["sha256"],
        )
    value["source_digest"] = digest(
        {key: item for key, item in value.items() if key != "source_digest"}
    )
    assert validate_pin(value) == value
    changed = copy.deepcopy(value)
    changed["base_model"] = "yolo11n" if family != "yolo11" else "yolov8n"
    with pytest.raises(ValueError):
        validate_pin(changed)


def test_pin_is_exact_and_requires_qualified_execution_capability():
    value = pin()
    assert validate_pin(value) == value
    config = {
        "initialization": value,
        "adapter_revision": 2,
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "base_model": {"id": "yolo11n"},
        "classes": value["classes"],
    }
    runtime = {
        "torch": "2.14.1+cu132",
        "torchvision": "0.29.1+cu132",
        "python": "3.12.3",
        "cuda": "13.2",
    }
    assert not compatible_runtime(config, runtime)
    assert compatible_runtime(
        config, {**runtime, "initialization_policy_digest": POLICY_DIGEST}
    )
    assert not compatible_runtime(
        {**config, "classes": list(reversed(value["classes"]))},
        {**runtime, "initialization_policy_digest": POLICY_DIGEST},
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(model_family="yolo26"),
        lambda p: p.update(base_model="yolo11-custom"),
        lambda p: p["artifact"].update(size_bytes=2 * 1024**3 + 1),
        lambda p: p["artifact"].update(key="https://external.invalid/weights.pt"),
        lambda p: p["runtime"].update(torch="2.13.0"),
        lambda p: p["runtime"].update(python="unknown"),
        lambda p: p["runtime"].update(cuda="12.8"),
        lambda p: p.update(classes=list(reversed(p["classes"]))),
        lambda p: p.update(classes=[{}]),
        lambda p: p.update(binding_digest="0" * 64),
    ],
)
def test_unqualified_or_tampered_source_is_rejected(mutate):
    value = pin()
    mutate(value)
    value["source_digest"] = digest(
        {key: item for key, item in value.items() if key != "source_digest"}
    )
    with pytest.raises(ValueError):
        validate_pin(value)


@pytest.mark.parametrize(
    "change",
    [
        {"mode": "checkpoint_resume"},
        {"source_sha256": "0" * 64},
        {"loaded_tensor_digest": "0" * 64},
        {"start_epoch": 1},
        {"start_epoch": False},
        {"optimizer_state_count": 1},
        {"scheduler_last_epoch": 7},
        {"scaler_state": {"scale": 65536.0}},
        {"ema_updates": 3},
        {"best_fitness": 0.5},
        {"best_weights_present": True},
    ],
)
def test_fresh_training_must_reset_state_and_fully_transfer_weights(change):
    value = pin()
    evidence = fresh(value)
    verify_evidence({"initialization": value}, evidence, resumed=False)
    with pytest.raises(ValueError):
        verify_evidence(
            {"initialization": value}, {**evidence, **change}, resumed=False
        )


def test_resume_is_bound_to_its_original_pin_but_keeps_its_own_training_epoch():
    value = pin()
    evidence = {
        "policy_digest": POLICY_DIGEST,
        "source_digest": value["source_digest"],
        "source_sha256": value["artifact"]["sha256"],
        "mode": "checkpoint_resume",
        "start_epoch": 2,
    }
    verify_evidence({"initialization": value}, evidence, resumed=True)
    wrong = copy.deepcopy(evidence)
    wrong["source_digest"] = "0" * 64
    with pytest.raises(ValueError):
        verify_evidence({"initialization": value}, wrong, resumed=True)


def segmentation_pin():
    from training_contract.initialization import SEGMENTATION_POLICY_DIGEST
    from training_contract.dataset import INSTANCE_RLE_CONTRACT

    value = pin()
    value.update(
        policy_digest=SEGMENTATION_POLICY_DIGEST,
        task="segment",
        base_model="yolo11n-seg",
        data_contract=dict(INSTANCE_RLE_CONTRACT),
    )
    value["evaluation"].update(
        normalized_model_digest="8" * 64,
        descriptor_digest="9" * 64,
        weights_sha256=value["artifact"]["sha256"],
    )
    value["source_digest"] = digest(
        {key: item for key, item in value.items() if key != "source_digest"}
    )
    return value


def test_segmentation_policy_is_separate_and_freezes_source_bridge_and_evaluator():
    from training_contract.initialization import SEGMENTATION_POLICY_DIGEST
    from training_contract.dataset import INSTANCE_RLE_CONTRACT, SEGMENTATION_CONTRACT

    value = segmentation_pin()
    assert validate_pin(value) == value
    assert value["policy_digest"] != POLICY_DIGEST
    config = {
        "initialization": value,
        "adapter_revision": 2,
        "task": "segment",
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "base_model": {"id": "yolo11n-seg"},
        "classes": value["classes"],
        "data_contract": dict(SEGMENTATION_CONTRACT),
    }
    runtime = {**value["runtime"], "initialization_policy_digest": POLICY_DIGEST}
    assert not compatible_runtime(config, runtime)
    assert compatible_runtime(
        config,
        {
            **runtime,
            "initialization_policy_digests": [
                POLICY_DIGEST,
                SEGMENTATION_POLICY_DIGEST,
            ],
        },
    )
    assert value["data_contract"] == INSTANCE_RLE_CONTRACT
    wrong = copy.deepcopy(value)
    wrong["data_contract"]["bridge"] = "ultralytics-simple-polygon-1"
    wrong["source_digest"] = digest(
        {key: item for key, item in wrong.items() if key != "source_digest"}
    )
    with pytest.raises(ValueError):
        validate_pin(wrong)


def test_segmentation_fresh_receipt_includes_exact_mask_head_state():
    value = segmentation_pin()
    receipt = {
        **fresh(value),
        "policy_digest": value["policy_digest"],
        "mask_head_tensor_digest": "9" * 64,
        "loaded_mask_head_tensor_digest": "9" * 64,
        "mask_head_tensor_count": 93,
    }
    verify_evidence({"initialization": value}, receipt, resumed=False)
    for change in (
        {"loaded_mask_head_tensor_digest": "0" * 64},
        {"mask_head_tensor_count": 0},
        {"optimizer_state_count": 4},
    ):
        with pytest.raises(ValueError):
            verify_evidence(
                {"initialization": value}, {**receipt, **change}, resumed=False
            )
