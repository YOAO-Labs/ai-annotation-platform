from __future__ import annotations

import hashlib
import json

import pytest

from aap_backend_runtime.plugin_service import plugin_verified_lifecycle
from aap_lifecycle_contract import REQUIRED_CONTRACT_CHECKS, memory_cycle_evidence


@pytest.fixture
def qualification(tmp_path, monkeypatch):
    registry = "12345678-1234-4234-8234-123456789abc"
    resource = "node/GPU-12345678-1234-4234-8234-123456789abc"
    uuid = "GPU-12345678-1234-4234-8234-123456789abc"
    image = "sha256:" + "a" * 64
    artifact = "127.0.0.1:51041/ocr@sha256:" + "b" * 64
    asset = "c" * 64
    weights = [{"sha256": "d" * 64, "size_bytes": 31}]
    gpu = {"device_uuid": uuid, "total_memory_mb": 24576, "driver_version": "550.54"}
    runtime = {"python": "3.10.18", "onnxruntime": "1.23.2", "cuda": "12.8.0"}
    topology = {"engines": {"cap": 1}}
    counts = {"engines": 3}
    cycles = []
    for index in (1, 2):
        cycle = memory_cycle_evidence(
            cycle=index,
            generation=str(index),
            context_samples_mb=[100] * 5,
            loaded_samples_mb=[1100] * 5,
            unloaded_samples_mb=[100] * 5,
        )
        cycle["session_observations"] = {"engines": [["CUDAExecutionProvider"]] * 3}
        cycles.append(cycle)
    report = {
        "schema_version": "1",
        "backend_name": "rapidocr",
        "generated_at": "2026-10-29T12:00:00Z",
        "passed": True,
        "blockers": [],
        "deployment": {
            "git_commit": "e" * 40,
            "image_id": image,
            "runtime_versions": {
                **runtime,
                "plugin_id": "rapidocr",
                "plugin_version": "0.1.0",
                "artifact": artifact,
                "asset_manifest_sha256": asset,
                "registry_id": registry,
                "gpu_resource_id": resource,
                "driver_version": gpu["driver_version"],
            },
            "pool_topology": {
                "registry_id": registry,
                "gpu_resource_id": resource,
                "pools": topology,
                "session_counts": counts,
            },
        },
        "artifacts": [
            {
                "kind": "weight",
                "name": "model.onnx",
                **weights[0],
                "approval_ref": "internal",
            },
            {
                "kind": "fixture",
                "name": "fixture.png",
                "sha256": "f" * 64,
                "size_bytes": 10,
                "approval_ref": "internal",
            },
        ],
        "gpu": {
            "uuid": uuid,
            "total_memory_mb": gpu["total_memory_mb"],
            "driver_version": gpu["driver_version"],
            "runtime_version": runtime["cuda"],
            "visible_device_count": 1,
        },
        "cycles": cycles,
        "contract_checks": {name: True for name in REQUIRED_CONTRACT_CHECKS},
        "final_residency": {
            "state": "unloaded",
            "gpu_loaded": False,
            "evictable": False,
            "active_requests": 0,
            "builders": 0,
            "borrowers": 0,
            "pools": {"engines": {"resident": False}},
            "identity": {"backend_registry_id": registry, "gpu_resource_id": resource},
        },
        "runtime_ephemera_clean": True,
    }

    def save(value):
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()
        path = tmp_path / "qualification.json"
        path.write_bytes(raw)
        monkeypatch.setenv("AAP_PLUGIN_QUALIFICATION_FILE", str(path))
        monkeypatch.setenv(
            "AAP_PLUGIN_QUALIFICATION_SHA256", hashlib.sha256(raw).hexdigest()
        )

    for key, value in {
        "AAP_PLUGIN_TOKEN_FILE": "/private/token",
        "AAP_PLUGIN_DEVICE": "cuda:0",
        "AAP_PLUGIN_REGISTRY_ID": registry,
        "AAP_ROUTE_REGISTRY_ID": registry,
        "AAP_PLUGIN_GPU_RESOURCE_ID": resource,
        "AAP_PLUGIN_GPU_UUID": uuid,
        "AAP_PLUGIN_ID": "rapidocr",
        "AAP_PLUGIN_VERSION": "0.1.0",
        "AAP_PLUGIN_OCI_ARTIFACT": artifact,
        "AAP_PLUGIN_IMAGE_ID": image,
        "AAP_PLUGIN_ASSET_MANIFEST_SHA256": asset,
    }.items():
        monkeypatch.setenv(key, value)
    save(report)
    return (
        report,
        save,
        {
            "pool_topology": topology,
            "runtime_versions": runtime,
            "gpu_facts": gpu,
            "expected_weights": weights,
            "session_counts": counts,
        },
    )


def test_qualified_native_plugin_requires_matching_measured_deployment(qualification):
    report, save, args = qualification
    assert plugin_verified_lifecycle(**args) is True
    report["gpu"]["driver_version"] = "different"
    save(report)
    with pytest.raises(ValueError, match="measured"):
        plugin_verified_lifecycle(**args)


def test_qualification_rejects_unapproved_weights_and_incomplete_session_evidence(
    qualification,
):
    report, save, args = qualification
    args["expected_weights"] = [{"sha256": "0" * 64, "size_bytes": 31}]
    with pytest.raises(ValueError, match="measured"):
        plugin_verified_lifecycle(**args)
    args["expected_weights"] = [{"sha256": "d" * 64, "size_bytes": 31}]
    report["cycles"][0]["session_observations"]["engines"].pop()
    save(report)
    with pytest.raises(ValueError, match="session observations"):
        plugin_verified_lifecycle(**args)


def test_missing_or_changed_approved_report_never_qualifies(qualification, monkeypatch):
    report, save, args = qualification
    monkeypatch.delenv("AAP_PLUGIN_QUALIFICATION_FILE")
    with pytest.raises(ValueError, match="together"):
        plugin_verified_lifecycle(**args)
    monkeypatch.delenv("AAP_PLUGIN_QUALIFICATION_SHA256")
    assert plugin_verified_lifecycle(**args) is False
    save(report)
    monkeypatch.setenv("AAP_PLUGIN_QUALIFICATION_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="digest"):
        plugin_verified_lifecycle(**args)
