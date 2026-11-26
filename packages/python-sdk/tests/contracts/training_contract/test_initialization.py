"""Framework-agnostic qualification pins and native reset evidence checks."""

import copy
import uuid

import pytest

from training_contract.dataset import INSTANCE_RLE_CONTRACT
from training_contract.initialization import (
    saved_prefix_compatible,
    validate_pin,
    verify_evidence,
)
from training_contract.trainer_plugin import canonical_json_digest as digest

POLICY_DIGEST = "a" * 64
SEGMENTATION_POLICY_DIGEST = "b" * 64


def pin(
    *,
    framework="custom-trainer",
    task="detect",
    model_family="custom",
    base_model="custom-small",
):
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
        "policy_digest": SEGMENTATION_POLICY_DIGEST
        if task == "segment"
        else POLICY_DIGEST,
        **identifiers,
        "model_name": "Frozen candidate",
        "framework": framework,
        "framework_version": "1.0.0",
        "task": task,
        "model_family": model_family,
        "base_model": base_model,
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
            "image_digest": "sha256:" + "5" * 64,
        },
        "evaluation": {
            "report_sha256": "6" * 64,
            "report_digest": "7" * 64,
            "validation_digest": "4" * 64,
        },
    }
    if task == "segment":
        value["data_contract"] = dict(INSTANCE_RLE_CONTRACT)
        value["evaluation"].update(
            normalized_model_digest="8" * 64,
            descriptor_digest="9" * 64,
            weights_sha256=value["artifact"]["sha256"],
        )
    value["source_digest"] = digest(value)
    return value


def fresh(value):
    return {
        "policy_digest": value["policy_digest"],
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


def test_generic_pin_accepts_arbitrary_trainer_frameworks_and_managed_prefix():
    value = pin(
        framework="my_trainer",
        task="detect",
        model_family="resnet",
        base_model="resnet50",
    )
    assert validate_pin(value) == value
    assert saved_prefix_compatible({"initialization": value}, {})

    prefixed = copy.deepcopy(value)
    prefixed["artifact"]["key"] = (
        "tenant/_aap_managed/training/" + prefixed["artifact"]["key"]
    )
    prefixed["source_digest"] = digest(
        {key: item for key, item in prefixed.items() if key != "source_digest"}
    )
    assert validate_pin(prefixed) == prefixed
    assert not saved_prefix_compatible(
        {"initialization": prefixed}, {"training_saved_prefix_schema": 1}
    )
    assert saved_prefix_compatible(
        {"initialization": prefixed}, {"training_saved_prefix_schema": 2}
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(framework=""),
        lambda p: p.update(policy_digest="not-a-sha"),
        lambda p: p["artifact"].update(size_bytes=2 * 1024**3 + 1),
        lambda p: p["artifact"].update(key="https://external.invalid/weights.pt"),
        lambda p: p["runtime"].update(image_digest="invalid"),
        lambda p: p.update(classes=list(reversed(p["classes"]))),
        lambda p: p.update(classes=[{}]),
        lambda p: p.update(binding_digest="0" * 64),
    ],
)
def test_tampered_or_invalid_source_is_rejected(mutate):
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
        "policy_digest": value["policy_digest"],
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


def test_segmentation_fresh_receipt_includes_exact_mask_head_state():
    value = pin(task="segment")
    assert validate_pin(value) == value
    receipt = {
        **fresh(value),
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
