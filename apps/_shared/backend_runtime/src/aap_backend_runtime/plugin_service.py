"""Compose native backend services with the managed plugin HTTP boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from aap_backend_runtime.backend_auth import require_external_bearer
from aap_protocol_v2.errors import LifecycleErrorCode, LifecycleHTTPError
from aap_protocol_v2.lifecycle import (
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    GenerationTransitionRequest,
    TerminalDrainRequest,
    parse_gpu_admission_header_values,
)
from aap_protocol_v2.routing_runtime import install_backend_routing_controller


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
        if request.url.path != "/health":
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
