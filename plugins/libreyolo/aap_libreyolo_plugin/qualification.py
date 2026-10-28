"""Validate administrator-approved GPU lifecycle evidence against this deployment.

The constrained operator and platform owner authenticate the approval and mount
its exact report read-only. This process verifies the report, not its own image.
"""
from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import re
import subprocess
from pathlib import Path

from aap_backend_runtime.lifecycle_evidence import validate_managed_lifecycle_evidence


def current_gpu(uuid):
    raw = subprocess.check_output(["nvidia-smi", "--query-gpu=uuid,driver_version,memory.total",
                                   "--format=csv,noheader,nounits"], text=True)
    matches = []
    for row in raw.splitlines():
        parts = [part.strip() for part in row.split(",")]
        if len(parts) != 3 or not parts[2].isdigit():
            raise ValueError("Cannot establish actual GPU driver and memory")
        if parts[0].lower() == uuid.lower():
            matches.append((parts[1], int(parts[2])))
    if len(matches) != 1:
        raise ValueError("Cannot uniquely identify approved GPU")
    return matches[0]


def verify_qualification(path, *, report_sha256, artifact, image_id, registry_id, gpu_resource_id,
                         gpu_uuid, runtime_qualification, expected_weight, gpu_facts=None):
    if not isinstance(report_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", report_sha256):
        raise ValueError("An administrator-approved report digest is required")
    with Path(path).open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != report_sha256:
        raise ValueError("Lifecycle evidence does not match the approved digest")
    if (not isinstance(artifact, str) or not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", artifact)
        or not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        or not registry_id or not gpu_resource_id or not gpu_uuid):
        raise ValueError("Lifecycle evidence requires a complete deployment binding")
    payload = json.loads(raw)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    if raw != canonical:
        raise ValueError("Lifecycle evidence must use canonical JSON bytes")
    report = validate_managed_lifecycle_evidence(payload)
    manifest = files("aap_libreyolo_plugin").joinpath("controlled-model-assets.json").read_bytes()
    driver, total_memory = gpu_facts or current_gpu(gpu_uuid)
    gpu = report["gpu"]
    runtime = report["deployment"]["runtime_versions"]
    expected_runtime = {key: str(value).lower() if isinstance(value, bool) else str(value)
                        for key, value in runtime_qualification.items()}
    expected_runtime.update(plugin_id="libreyolo", plugin_version="0.1.0", artifact=artifact,
                            asset_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                            driver_version=driver)
    if (not report["passed"] or report["backend_name"] != "libreyolo"
        or report["deployment"]["image_id"] != image_id
        or any(runtime.get(key) != value for key, value in expected_runtime.items())
        or report["deployment"]["pool_topology"] != {
            "registry_id": registry_id, "gpu_resource_id": gpu_resource_id,
            "pools": {"models": {"cap": 1}},
        }
        or gpu["uuid"] != gpu_uuid or gpu["driver_version"] != driver
        or gpu["total_memory_mb"] != total_memory or gpu["runtime_version"] != runtime_qualification["cuda_build"]
        or not any(item["kind"] == "weight" and item["sha256"] == expected_weight["sha256"]
                   and item["size_bytes"] == expected_weight["size_bytes"] for item in report["artifacts"])):
        raise ValueError("Lifecycle evidence does not match this GPU deployment")
    return True
