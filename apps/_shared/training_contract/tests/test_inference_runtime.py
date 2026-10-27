"""Training provenance never changes when a qualified inference runtime changes."""

import copy

import pytest

from training_contract.inference_runtime import (
    inference_runtime,
    validate_inference_runtime,
)
from training_contract.model_return import segmentation_identity


def test_legacy_detection_is_explicit_and_artifact_stays_unchanged():
    receipt = {
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "task": "detection",
    }
    original = copy.deepcopy(receipt)
    runtime = inference_runtime(receipt, "8.4.183")
    assert receipt == original
    assert runtime["training_version"] == "8.4.177"
    assert runtime["version"] == "8.4.183"
    assert runtime["qualification"]["source_sha256"]
    assert validate_inference_runtime(receipt, runtime) == runtime
    runtime["qualification"]["torch"] = "unknown"
    with pytest.raises(ValueError):
        validate_inference_runtime(receipt, runtime)


@pytest.mark.parametrize(
    "trained,actual",
    [("8.4.177", "8.4.184"), ("8.4.183", "8.4.177"), ("8.4.61", "8.4.183")],
)
def test_unqualified_pairs_are_rejected(trained, actual):
    with pytest.raises(ValueError):
        inference_runtime(
            {"framework": "ultralytics", "framework_version": trained}, actual
        )


def test_legacy_segmentation_uses_actual_decoder_sources():
    receipt = {
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "task": "segmentation",
        **segmentation_identity("yolo11n-seg", "8.4.177"),
    }
    runtime = inference_runtime(receipt, "8.4.183")
    assert runtime["qualification"]["ultralytics"] == "8.4.183"
    assert (
        runtime["qualification"]["source_sha256"]
        != receipt["runtime_qualification"]["source_sha256"]
    )
    receipt["model_size"] = "x"
    with pytest.raises(ValueError):
        inference_runtime(receipt, "8.4.183")
