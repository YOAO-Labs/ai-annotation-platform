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


def libre_version():
    return SimpleNamespace(
        id="version-1", framework="libreyolo", framework_version="1.6.0",
        classes=["cat", "dog"], artifact={"sha256": "a" * 64, "size_bytes": 123},
        provenance={"task": "detect", "base_model": {"id": "libreyolo9t"},
                    "model_return": libre_identity("libreyolo9t")},
    )


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
        trained_models.check_trial_runtime(libre_version(), pinned, {"inference_runtime": cuda})
    legacy = SimpleNamespace(receipt={"sha256": "a"}, trial={})
    with pytest.raises(trained_models.PinnedRuntimeConflict):
        trained_models.check_install_runtime(version, legacy, {"inference_runtime": cuda})
    trained_models.check_install_runtime(version, legacy, {"inference_runtime": cpu})


def test_declared_capability_requires_both_approval_and_observation():
    from aap_inference.capabilities import extract_capabilities
    from aap_inference.pool_capability import capability_fingerprint, diff_capabilities

    version = libre_version()
    cpu = {"framework": "libreyolo", "format": libre_identity("libreyolo9t")["artifact_format"], "runtime_device": "cpu"}
    cuda = {**cpu, "runtime_device": "cuda"}
    setup = {"name": "libreyolo", "managed_model_compatibility": [cpu]}
    caps = extract_capabilities(setup)
    backend = SimpleNamespace(
        extra_params={"plugin": {"manifest": {"managed_model_compatibility": [cpu, cuda]}}},
        health_meta={"capabilities": caps},
    )
    trained_models.check_declared_compatibility(backend, version, {"qualification": {"device": "cpu"}})
    with pytest.raises(HTTPException):
        trained_models.check_declared_compatibility(backend, version, {"qualification": {"device": "cuda"}})
    backend.extra_params["plugin"]["manifest"]["managed_model_compatibility"] = [cuda]
    with pytest.raises(HTTPException):
        trained_models.check_declared_compatibility(backend, version, {"qualification": {"device": "cpu"}})
    other_caps = extract_capabilities({**setup, "managed_model_compatibility": [cuda]})
    assert capability_fingerprint(caps) != capability_fingerprint(other_caps)
    assert "managed_model_compatibility" in diff_capabilities(caps, other_caps).differing_fields
    malformed = extract_capabilities({**setup, "managed_model_compatibility": [cpu, {**cpu, "runtime_device": "gpu"}]})
    assert malformed["managed_model_compatibility"] == []
    assert any(w["field"] == "managed_model_compatibility" for w in malformed["warnings"])
    backend.health_meta["capabilities"] = malformed
    backend.extra_params["plugin"]["manifest"]["managed_model_compatibility"] = [cpu]
    with pytest.raises(HTTPException):
        trained_models.check_declared_compatibility(backend, version, {"qualification": {"device": "cpu"}})


def test_legacy_trial_publication_only_accepts_original_cpu_identity():
    version = libre_version()
    receipt = trained_models.expected_receipt(version)
    cpu = inference_runtime(receipt, "1.6.0", device="cpu")
    cuda = inference_runtime(receipt, "1.6.0", device="cuda")
    legacy = SimpleNamespace(trial={"sample_count": 1}, receipt=receipt)
    trained_models.check_trial_runtime(version, legacy, {"inference_runtime": cpu})
    trained_models.check_trial_runtime(version, legacy, {})
    for runtime in (cuda, {**cpu, "version": "1.6.1"}, {**cpu, "qualification": {**cpu["qualification"], "torch": "other"}}):
        with pytest.raises(HTTPException):
            trained_models.check_trial_runtime(version, legacy, {"inference_runtime": runtime})
    assert legacy.trial == {"sample_count": 1}
    assert "inference_runtime" not in legacy.receipt
