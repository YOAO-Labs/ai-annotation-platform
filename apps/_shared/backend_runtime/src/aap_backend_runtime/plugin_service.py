"""Compose native backend services with the managed plugin HTTP boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from aap_lifecycle_contract import validate_managed_lifecycle_evidence
from aap_backend_runtime.backend_auth import require_external_bearer
from aap_protocol_v2.errors import LifecycleErrorCode, LifecycleHTTPError
from aap_protocol_v2.lifecycle import (
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    GenerationTransitionRequest,
    TerminalDrainRequest,
    parse_gpu_admission_header_values,
)
from aap_protocol_v2.routing import ROUTE_CONTROL_PATH, ROUTE_STATUS_PATH
from aap_protocol_v2.routing_runtime import install_backend_routing_controller
from aap_protocol_v2.scope_retirement import SCOPE_RETIREMENT_PATH


def plugin_lifecycle_options() -> dict[str, Any]:
    """Return native lifecycle options only for an explicitly configured plugin."""
    if not os.environ.get("AAP_PLUGIN_TOKEN_FILE"):
        return {}
    registry = os.environ.get("AAP_PLUGIN_REGISTRY_ID", "")
    device = os.environ.get("AAP_PLUGIN_DEVICE", "")
    resource = os.environ.get("AAP_PLUGIN_GPU_RESOURCE_ID") or None
    gpu_uuid = os.environ.get("AAP_PLUGIN_GPU_UUID") or None
    if not registry or device not in {"cpu", "cuda:0"}:
        raise RuntimeError("Invalid plugin registry or device configuration")
    if (device == "cpu" and (resource or gpu_uuid)) or (
        device == "cuda:0" and not (resource and gpu_uuid)
    ):
        raise RuntimeError("Plugin device and GPU identity differ")
    if os.environ.get("AAP_ROUTE_REGISTRY_ID") != registry:
        raise RuntimeError("Plugin registry and route identity differ")
    return {
        "expected_registry_id": registry,
        "expected_gpu_resource_id": resource,
        "terminal_cpu_enabled": device == "cpu",
        "allow_legacy_workloads": device == "cpu",
    }


def plugin_setup_fields(
    pool_topology: Mapping[str, Mapping[str, int]],
    runtime_versions: Mapping[str, str],
) -> dict[str, Any]:
    """Return plugin-only identity and measured runtime facts for /setup."""
    options = plugin_lifecycle_options()
    if not options:
        return {}
    plugin_id = os.environ.get("AAP_PLUGIN_ID", "")
    version = os.environ.get("AAP_PLUGIN_VERSION", "")
    if not plugin_id or not version:
        raise RuntimeError("Plugin identity is unavailable")
    compatibility = json.loads(
        os.environ.get("AAP_PLUGIN_MANAGED_MODEL_COMPATIBILITY", "[]")
    )
    device = os.environ["AAP_PLUGIN_DEVICE"]
    terminal = (
        {"terminal_drain_endpoint": "/lifecycle/terminal-drain"}
        if device == "cpu"
        else {"terminal_drain_signed_endpoint": "/lifecycle/terminal-drain-signed"}
    )
    return {
        "plugin_identity": {"plugin_id": plugin_id, "version": version},
        "registry_id": options["expected_registry_id"],
        "gpu_resource_id": options["expected_gpu_resource_id"],
        "physical_gpu_uuid": os.environ.get("AAP_PLUGIN_GPU_UUID") or None,
        "managed_model_compatibility": compatibility,
        "pool_topology": dict(pool_topology),
        "runtime_info": {
            "device": "cpu" if device == "cpu" else "cuda",
            "runtime_versions": dict(runtime_versions),
        },
        "terminal_lifecycle": terminal,
    }


def plugin_verified_lifecycle(
    *,
    pool_topology: Mapping[str, Mapping[str, int]],
    runtime_versions: Mapping[str, str],
    gpu_facts: Mapping[str, Any],
    expected_weights: Sequence[Mapping[str, Any]],
    session_counts: Mapping[str, int],
) -> bool:
    """Bind approved GPU lifecycle evidence to the measured native deployment."""
    options = plugin_lifecycle_options()
    if not options:
        return False
    path = os.environ.get("AAP_PLUGIN_QUALIFICATION_FILE")
    digest = os.environ.get("AAP_PLUGIN_QUALIFICATION_SHA256")
    if bool(path) != bool(digest):
        raise ValueError(
            "Lifecycle report and approved digest must be supplied together"
        )
    if options["terminal_cpu_enabled"]:
        if path:
            raise ValueError("CPU plugins cannot use GPU lifecycle evidence")
        return False
    if not path:
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("An approved lifecycle report digest is required")
    with Path(path).open("rb") as source:
        raw = source.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Lifecycle report differs from the approved digest")
    report = json.loads(raw)
    canonical = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    if raw != canonical:
        raise ValueError("Lifecycle report must use canonical JSON")
    report = validate_managed_lifecycle_evidence(report)
    artifact = os.environ.get("AAP_PLUGIN_OCI_ARTIFACT", "")
    image_id = os.environ.get("AAP_PLUGIN_IMAGE_ID", "")
    asset_digest = os.environ.get("AAP_PLUGIN_ASSET_MANIFEST_SHA256", "")
    plugin_id = os.environ.get("AAP_PLUGIN_ID", "")
    plugin_version = os.environ.get("AAP_PLUGIN_VERSION", "")
    if (
        not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", artifact)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
        or not re.fullmatch(r"[0-9a-f]{64}", asset_digest)
        or not plugin_id
        or not plugin_version
    ):
        raise ValueError("Lifecycle report requires a complete installation binding")
    registry = options["expected_registry_id"]
    resource = options["expected_gpu_resource_id"]
    uuid = os.environ["AAP_PLUGIN_GPU_UUID"]
    versions = {
        **runtime_versions,
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "artifact": artifact,
        "asset_manifest_sha256": asset_digest,
        "registry_id": registry,
        "gpu_resource_id": resource,
        "driver_version": gpu_facts.get("driver_version"),
    }
    topology = {
        "registry_id": registry,
        "gpu_resource_id": resource,
        "pools": dict(pool_topology),
        "session_counts": dict(session_counts),
    }
    weight_set = {
        (item.get("sha256"), item.get("size_bytes")) for item in expected_weights
    }
    report_weights = {
        (item["sha256"], item["size_bytes"])
        for item in report["artifacts"]
        if item["kind"] == "weight"
    }
    gpu = report["gpu"]
    final_identity = (report["final_residency"] or {}).get("identity") or {}
    if (
        not report["passed"]
        or report["backend_name"] != plugin_id
        or report["deployment"]["image_id"] != image_id
        or report["deployment"]["pool_topology"] != topology
        or any(
            report["deployment"]["runtime_versions"].get(k) != v
            for k, v in versions.items()
        )
        or gpu_facts.get("device_uuid") != uuid
        or gpu["uuid"] != uuid
        or gpu["driver_version"] != gpu_facts.get("driver_version")
        or gpu["total_memory_mb"] != gpu_facts.get("total_memory_mb")
        or gpu["runtime_version"] != runtime_versions.get("cuda")
        or not weight_set
        or weight_set != report_weights
        or final_identity.get("backend_registry_id") != registry
        or final_identity.get("gpu_resource_id") != resource
    ):
        raise ValueError("Lifecycle report differs from the measured plugin deployment")
    return True


def install_plugin_service(app: FastAPI, lifecycle_getter: Callable[[], Any]) -> bool:
    """Install plugin authentication and terminal routes; leave first-party mode alone."""
    options = plugin_lifecycle_options()
    if not options:
        return False
    token = (
        Path(os.environ["AAP_PLUGIN_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    )
    if len(token) < 32:
        raise RuntimeError("Plugin credential is unavailable or invalid")
    install_backend_routing_controller(app)

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        if request.url.path not in {
            "/health",
            ROUTE_CONTROL_PATH,
            ROUTE_STATUS_PATH,
            SCOPE_RETIREMENT_PATH,
        }:
            try:
                require_external_bearer(request, token)
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code, content={"detail": exc.detail}
                )
        return await call_next(request)

    def lifecycle():
        current = lifecycle_getter()
        if current is None:
            raise HTTPException(status_code=503, detail="backend not ready")
        return current

    if options["terminal_cpu_enabled"]:

        @app.post("/lifecycle/terminal-drain")
        async def terminal_drain(body: TerminalDrainRequest):
            return await lifecycle().terminal_drain(
                registry_id=body.registry_id,
                boot_id=body.boot_id,
                operation_id=body.operation_id,
            )
    else:

        @app.post("/lifecycle/terminal-drain-signed")
        async def terminal_drain_signed(
            body: GenerationTransitionRequest, request: Request
        ):
            try:
                values = parse_gpu_admission_header_values(
                    request.headers.getlist(GPU_GENERATION_HEADER),
                    request.headers.getlist(GPU_ADMISSION_TOKEN_HEADER),
                )
            except ValueError as exc:
                raise LifecycleHTTPError(LifecycleErrorCode.ADMISSION_DENIED) from exc
            generation, admission_token = values or (None, None)
            return await lifecycle().terminal_drain_signed(
                body.generation, generation_header=generation, token=admission_token
            )

    return True
