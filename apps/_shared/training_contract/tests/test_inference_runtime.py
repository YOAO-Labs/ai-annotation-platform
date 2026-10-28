"""Training provenance never changes when a qualified inference runtime changes."""

import copy

import pytest

from training_contract.inference_runtime import (
    inference_runtime,
    validate_inference_runtime,
)
from training_contract.model_return import segmentation_identity
from training_contract.model_return import libre_identity


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


def libre_receipt():
    return {
        "framework": "libreyolo",
        "framework_version": "1.6.0",
        "task": "detect",
        **libre_identity("libreyolo9t"),
    }


def test_historical_libre_receipt_and_cpu_runtime_are_unchanged():
    receipt = libre_receipt()
    original = copy.deepcopy(receipt)
    cpu = {
        "revision": 1,
        "libreyolo": "1.6.0",
        "torch": "2.14.1+cu132",
        "torchvision": "0.29.1+cu132",
        "device": "cpu",
    }
    historical = {
        "revision": 1,
        "framework": "libreyolo",
        "training_version": "1.6.0",
        "version": "1.6.0",
        "task": "detect",
        "compatibility": "same-version",
        "qualification": cpu,
    }
    assert receipt["runtime_qualification"] == cpu
    assert inference_runtime(receipt, "1.6.0") == historical
    assert inference_runtime(receipt, "1.6.0", device="cpu") == historical
    assert validate_inference_runtime(receipt, historical) == historical
    gpu = inference_runtime(receipt, "1.6.0", device="cuda")
    assert validate_inference_runtime(receipt, gpu) == gpu
    assert gpu["qualification"] == {
        **cpu, "device": "cuda", "cuda_build": "13.2", "precision": "fp32",
        "half": False, "amp": False, "tf32": False,
    }
    gpu["qualification"]["precision"] = "fp16"
    assert receipt == original
    assert inference_runtime(receipt, "1.6.0") == historical
    assert inference_runtime(receipt, "1.6.0", device="cuda")["qualification"]["precision"] == "fp32"


@pytest.mark.parametrize(
    "field,value",
    [
        ("device", "cpu"), ("device", "cuda:0"), ("device", None),
        ("cuda_build", "12.8"), ("precision", "fp16"),
        ("half", True), ("amp", True), ("tf32", True),
        ("half", 0), ("torch", "2.14.1+cpu"), ("revision", 2),
        ("unknown", "extra"),
    ],
)
def test_cuda_runtime_rejects_tampering(field, value):
    receipt = libre_receipt()
    runtime = inference_runtime(receipt, "1.6.0", device="cuda")
    runtime["qualification"][field] = value
    with pytest.raises(ValueError):
        validate_inference_runtime(receipt, runtime)


@pytest.mark.parametrize(
    "field", ["device", "cuda_build", "precision", "half", "amp", "tf32"]
)
def test_cuda_runtime_requires_each_profile_field(field):
    receipt = libre_receipt()
    runtime = inference_runtime(receipt, "1.6.0", device="cuda")
    del runtime["qualification"][field]
    with pytest.raises(ValueError):
        validate_inference_runtime(receipt, runtime)


@pytest.mark.parametrize("device", [None, "", "gpu", "cuda:0", 0])
def test_device_kind_must_be_explicit_and_known(device):
    with pytest.raises(ValueError):
        inference_runtime(libre_receipt(), "1.6.0", device=device)


def test_cuda_profile_does_not_qualify_other_frameworks_or_tasks():
    receipt = {"framework": "ultralytics", "framework_version": "8.4.183"}
    old = inference_runtime(receipt, "8.4.183")
    assert validate_inference_runtime(receipt, old) == old
    with pytest.raises(ValueError):
        inference_runtime(receipt, "8.4.183", device="cuda")
    with pytest.raises(ValueError):
        validate_inference_runtime(receipt, inference_runtime(libre_receipt(), "1.6.0", device="cuda"))
    with pytest.raises(ValueError):
        inference_runtime({**libre_receipt(), "task": "segment"}, "1.6.0", device="cuda")
    with pytest.raises(ValueError):
        inference_runtime(libre_receipt(), "1.5.0", device="cuda")
