"""Runtime qualification must not change immutable training or trial identities."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from training_contract.inference_runtime import inference_runtime
from training_contract.model_return import libre_identity

from app.services import trained_models


def libre_receipt():
    return {
        "framework": "libreyolo",
        "framework_version": "1.6.0",
        "task": "detect",
        **libre_identity("libreyolo9t"),
    }


def test_legacy_reference_rejects_same_version_cuda_result():
    ref = libre_receipt()
    cpu = inference_runtime(ref, "1.6.0", device="cpu")
    cuda = inference_runtime(ref, "1.6.0", device="cuda")
    trained_models.validate_result_runtime({}, ref)
    trained_models.validate_result_runtime({"inference_runtime": cpu}, ref)
    with pytest.raises(ValueError, match="Legacy frozen"):
        trained_models.validate_result_runtime({"inference_runtime": cuda}, ref)
    trained_models.validate_result_runtime({"inference_runtime": cuda}, {**ref, "inference_runtime": cuda})
    with pytest.raises(ValueError, match="different inference runtime"):
        trained_models.validate_result_runtime({"inference_runtime": cpu}, {**ref, "inference_runtime": cuda})


def test_install_and_publish_keep_trial_runtime_pinned():
    ref = libre_receipt()
    cpu = inference_runtime(ref, "1.6.0", device="cpu")
    cuda = inference_runtime(ref, "1.6.0", device="cuda")
    version = SimpleNamespace(framework="libreyolo")
    fresh = SimpleNamespace(receipt={}, trial={})
    with pytest.raises(HTTPException, match="运行时"):
        trained_models.check_install_runtime(version, fresh, {})
    trained_models.check_install_runtime(version, fresh, {"inference_runtime": cuda})
    pinned = SimpleNamespace(receipt={"inference_runtime": cpu}, trial={"inference_runtime": cpu})
    with pytest.raises(trained_models.PinnedRuntimeConflict):
        trained_models.check_install_runtime(version, pinned, {"inference_runtime": cuda})
    with pytest.raises(HTTPException):
        trained_models.check_trial_runtime(pinned, {"inference_runtime": cuda})
    legacy = SimpleNamespace(receipt={"sha256": "a"}, trial={})
    with pytest.raises(trained_models.PinnedRuntimeConflict):
        trained_models.check_install_runtime(version, legacy, {"inference_runtime": cuda})
    trained_models.check_install_runtime(version, legacy, {"inference_runtime": cpu})


def test_declared_capability_requires_both_approval_and_observation(monkeypatch):
    version = SimpleNamespace(framework="libreyolo")
    monkeypatch.setattr(trained_models, "expected_receipt", lambda _: {"artifact_format": "native"})
    cpu = {"framework": "libreyolo", "format": "native", "runtime_device": "cpu"}
    cuda = {**cpu, "runtime_device": "cuda"}
    backend = SimpleNamespace(
        extra_params={"plugin": {"manifest": {"managed_model_compatibility": [cpu, cuda]}}},
        health_meta={"capabilities": {"managed_model_compatibility": [cpu]}},
    )
    trained_models.check_declared_compatibility(backend, version, {"qualification": {"device": "cpu"}})
    with pytest.raises(HTTPException):
        trained_models.check_declared_compatibility(backend, version, {"qualification": {"device": "cuda"}})
