import hashlib
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aap_inference_contract.execution import freeze_json
from aap_inference_contract.plugin import PluginActivateRequest, PluginInstallation, PluginLifecycleQualification, PluginManifest, plugin_is_active


def manifest():
    return {
        "plugin_id": "libreyolo",
        "version": "1.0",
        "publisher": "example",
        "license": "MIT",
        "artifact": "registry.example/plugin@sha256:" + "a" * 64,
        "sdk_compatibility": ">=1",
        "entrypoint": ["python", "-m", "plugin"],
        "capabilities": ["inference", "detection", "managed_models"],
        "resource": {"device": "cuda", "memory_mb": 1024, "gpu_memory_mb": 2048},
        "asset_manifest_sha256": "b" * 64,
        "allowed_asset_sources": ["https://example.com/assets"],
        "managed_model_compatibility": [{"framework": "libreyolo", "format": "libreyolo", "runtime_device": "cuda"}],
    }


def installation(status="active"):
    return {
        "installation_id": str(uuid4()),
        "manifest": manifest(),
        "status": status,
        "revision": 1,
        "resource": {"gpu_resource_id": "node/gpu", "physical_gpu_uuid": "GPU-123", "logical_device": "cuda:0"},
    }


def test_manifest_requires_fixed_digest_and_bounded_capabilities():
    value = manifest()
    value["artifact"] = "registry.example/plugin:latest"
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(value)
    value = manifest()
    value["capabilities"].append("database")
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(value)
    value = manifest()
    value["managed_model_compatibility"] = []
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(value)


@pytest.mark.parametrize("reference", [
    "--mount=type=bind,src=/tmp,dst=/x@sha256:" + "a" * 64,
    "image,other@sha256:" + "a" * 64,
    "image=other@sha256:" + "a" * 64,
    "image name@sha256:" + "a" * 64,
    "-image@sha256:" + "a" * 64,
    "registry.example:5000/@sha256:" + "a" * 64,
])
def test_artifact_rejects_option_and_invalid_reference_forms(reference):
    value = manifest()
    value["artifact"] = reference
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(value)


@pytest.mark.parametrize("reference", [
    "registry.example:5000/team/model:stable@sha256:" + "a" * 64,
    "model@sha256:" + "a" * 64,
])
def test_artifact_accepts_digest_pinned_registry_and_image(reference):
    value = manifest()
    value["artifact"] = reference
    assert PluginManifest.model_validate(value).artifact == reference


def test_cuda_binding_requires_physical_device():
    value = installation()
    value["resource"]["physical_gpu_uuid"] = None
    with pytest.raises(ValidationError):
        PluginInstallation.model_validate(value)


def test_reserved_status_fails_closed_and_legacy_stays_usable():
    assert plugin_is_active(None)
    assert plugin_is_active({"max_concurrency": 4})
    assert plugin_is_active({"plugin": installation()})
    assert plugin_is_active(freeze_json({"plugin": installation()}))
    assert not plugin_is_active({"plugin": installation("disabled")})
    assert not plugin_is_active({"plugin": {"status": "active"}})
    assert not plugin_is_active({"plugin": None})
    assert not plugin_is_active({"plugin_lifecycle": None})
    assert not plugin_is_active({"plugin_lifecycle": {"proof": "forged"}})


def test_activation_requires_exact_administrator_inspection_identity():
    value = {
        "expected_revision": 1,
        "receipt": {
            "installation_id": str(uuid4()), "registry_id": str(uuid4()),
            "artifact": manifest()["artifact"], "image_id": "sha256:" + "c" * 64,
            "container_id": "d" * 64, "resource": installation()["resource"],
        },
    }
    assert PluginActivateRequest.model_validate(value).receipt.artifact == manifest()["artifact"]
    value["receipt"]["container_id"] = "short"
    with pytest.raises(ValidationError):
        PluginActivateRequest.model_validate(value)
    value["receipt"]["container_id"] = "d" * 64
    value["receipt"]["artifact"] = "--mount=evil@sha256:" + "a" * 64
    with pytest.raises(ValidationError):
        PluginActivateRequest.model_validate(value)
    value["receipt"]["artifact"] = manifest()["artifact"]
    value["receipt"]["unverified_claim"] = True
    with pytest.raises(ValidationError):
        PluginActivateRequest.model_validate(value)


def test_lifecycle_qualification_hashes_canonical_ascii_json():
    report = {"name": "检测", "cycle": 1}
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    assert PluginLifecycleQualification(report=report, sha256=digest).sha256 == digest
    with pytest.raises(ValidationError):
        PluginLifecycleQualification(report={**report, "cycle": 2}, sha256=digest)
    with pytest.raises(ValidationError):
        PluginLifecycleQualification(report={"cycle": float("nan")}, sha256=digest)
