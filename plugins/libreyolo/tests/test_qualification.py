import hashlib
import json

import pytest
from aap_backend_runtime.lifecycle_evidence import (
    REQUIRED_CONTRACT_CHECKS, build_managed_lifecycle_evidence, memory_cycle_evidence,
)
from aap_libreyolo_plugin import qualification


def test_report_requires_exact_digest_and_deployment_identity(tmp_path, monkeypatch):
    manifest = b'{"assets":[]}'
    (tmp_path / "controlled-model-assets.json").write_bytes(manifest)
    monkeypatch.setattr(qualification, "files", lambda _: tmp_path)
    profile = {"revision": 1, "libreyolo": "1.6.0", "torch": "2.14.1+cu132",
               "torchvision": "0.29.1+cu132", "device": "cuda", "cuda_build": "13.2",
               "precision": "fp32", "half": False, "amp": False, "tf32": False}
    artifact = "registry.example/libre@sha256:" + "a" * 64
    image_id = "sha256:" + "b" * 64
    gpu_uuid = "GPU-00000000-0000-0000-0000-000000000001"
    versions = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in profile.items()}
    versions.update(plugin_id="libreyolo", plugin_version="0.1.0", artifact=artifact,
                    asset_manifest_sha256=hashlib.sha256(manifest).hexdigest(), driver_version="595.99")
    weight = {"sha256": "c" * 64, "size_bytes": 12}
    cycles = [memory_cycle_evidence(cycle=i, generation=str(i),
              context_samples_mb=[100] * 5, loaded_samples_mb=[1100] * 5,
              unloaded_samples_mb=[120] * 5) for i in (1, 2)]
    evidence = build_managed_lifecycle_evidence(
        backend_name="libreyolo", deployment={"git_commit": "d" * 40, "image_id": image_id,
          "runtime_versions": versions, "pool_topology": {"registry_id": "registry", "gpu_resource_id": "resource",
                                                           "pools": {"models": {"cap": 1}}}},
        artifacts=[{"kind": "weight", "name": "weights.pt", "size_bytes": 12,
                    "sha256": weight["sha256"], "approval_ref": "approved"},
                   {"kind": "fixture", "name": "image.jpg", "size_bytes": 10,
                    "sha256": "e" * 64, "approval_ref": "approved"}],
        gpu={"uuid": gpu_uuid, "driver_version": "595.99", "runtime_version": "13.2",
             "total_memory_mb": 24576, "visible_device_count": 1}, cycles=cycles,
        contract_checks={name: True for name in REQUIRED_CONTRACT_CHECKS},
        final_residency={"state": "unloaded", "gpu_loaded": False, "evictable": False,
                         "active_requests": 0, "builders": 0, "borrowers": 0,
                         "pools": {"models": {"resident": False}}}, runtime_ephemera_clean=True)
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    path = tmp_path / "evidence.json"
    path.write_bytes(raw)
    args = dict(report_sha256=hashlib.sha256(raw).hexdigest(), artifact=artifact, image_id=image_id,
                registry_id="registry", gpu_resource_id="resource", gpu_uuid=gpu_uuid,
                runtime_qualification=profile, expected_weight=weight, gpu_facts=("595.99", 24576))
    assert qualification.verify_qualification(path, **args)
    for changed in ({"registry_id": "other"}, {"artifact": "registry.example/libre@sha256:" + "f" * 64},
                    {"gpu_facts": ("different", 24576)}, {"report_sha256": "0" * 64}):
        with pytest.raises(ValueError):
            qualification.verify_qualification(path, **{**args, **changed})
    path.write_bytes(raw + b"\n")
    with pytest.raises(ValueError):
        qualification.verify_qualification(path, **args)
