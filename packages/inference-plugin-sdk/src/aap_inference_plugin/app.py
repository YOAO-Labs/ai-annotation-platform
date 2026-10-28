"""HTTP composition for detection providers; model code never handles protocol envelopes."""
from __future__ import annotations

import asyncio
import json
import hashlib
from contextlib import asynccontextmanager
from typing import Any
import httpx

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from aap_backend_runtime.backend_auth import require_external_bearer
from aap_backend_runtime.gpu_lifecycle import ManagedGpuLifecycle
from aap_backend_runtime.managed_pool import BuildArtifact, ManagedLruPool
from aap_backend_runtime.versions import versions_payload
from aap_protocol_v2 import BatchPredictResponse, PredictionResult, TaskItem
from aap_protocol_v2.lifecycle import (
    AdmissionScope, GenerationTransitionRequest, LifecycleModeRequest, LifecycleResetRequest,
    ManagedLifecycleCapabilities, GPU_GENERATION_HEADER, GPU_ADMISSION_TOKEN_HEADER,
    GPU_HEALTH_CHALLENGE_HEADER, GPU_HEALTH_CHALLENGE_QUERY_PARAM,
    match_gpu_health_challenge, parse_gpu_admission_header_values,
    parse_gpu_control_token_header_values,
)
from aap_protocol_v2.routing_runtime import install_backend_routing_controller
from aap_protocol_v2.version import PROTOCOL_VERSION, COMPAT_PROTOCOL_VERSIONS

from .conversion import rectangles
from .image import fetch_image
from .types import DetectionConfig, DetectionProvider


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conf: float = Field(default=0.25, ge=0, le=1, allow_inf_nan=False)
    iou: float = Field(default=0.7, ge=0, le=1, allow_inf_nan=False)
    max_det: int = Field(default=300, ge=1, le=1000)


class TerminalDrainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    registry_id: str = Field(min_length=1, max_length=128)
    boot_id: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=256)


def receipt_digest(receipt: dict) -> str:
    return hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def create_detection_app(provider: DetectionProvider, config: DetectionConfig) -> FastAPI:
    """Compose one process, one pool, and one lifecycle around a provider."""
    def build(key: str) -> BuildArtifact:
        identity = json.loads(key)
        if identity["kind"] == "builtin":
            name = identity["name"]
            model = provider.load(name, config.device, config.catalog[name])
        else:
            ref = identity["ref"]
            path, receipt = config.managed_store.resolve(ref["model_version_id"], ref["sha256"])
            actual = config.runtime_receipt(receipt)
            if any(actual.get(k) != ref[k] for k in ("model_version_id", "sha256", "classes_digest")):
                raise ValueError("installed model identity changed")
            runtime = actual.get("inference_runtime")
            if runtime != identity["runtime"] or receipt_digest(actual) != identity["receipt_digest"]:
                raise ValueError("installed model runtime changed")
            model = provider.load("trained:" + ref["model_version_id"], config.device,
                                  {"receipt": receipt, "path": path, "frozen_model_ref": ref})
        return BuildArtifact(resource=model, attachments=(model,))

    def close_models(items: tuple[Any, ...]) -> None:
        for item in items:
            item.close()

    pool = ManagedLruPool(config.pool_capacity, build, lambda key: key,
                          lambda: None, cleanup_attachments=close_models,
                          device_of=lambda _: config.device, pool_name="models")
    lifecycle = ManagedGpuLifecycle(pool, verify_keyring=config.verify_keyring,
                                    evictable_verified=config.evictable_verified,
                                    expected_registry_id=config.expected_registry_id,
                                    expected_gpu_resource_id=config.expected_gpu_resource_id,
                                    terminal_cpu_enabled=config.device == "cpu")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await lifecycle.shutdown()

    app = FastAPI(title="AAP detection plugin", lifespan=lifespan)

    @app.middleware("http")
    async def authentication(request: Request, call_next):
        if request.url.path != "/health":
            try:
                require_external_bearer(request, config.token)
            except HTTPException as exc:
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)
    controller = install_backend_routing_controller(app, registry_id=config.expected_registry_id)

    def track(operation, future):
        operation.track_future(future)
        if future is not None:
            controller.track_background_future(future)

    def headers(request: Request):
        try:
            return parse_gpu_admission_header_values(request.headers.getlist(GPU_GENERATION_HEADER),
                                                     request.headers.getlist(GPU_ADMISSION_TOKEN_HEADER)) or (None, None)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Invalid GPU admission headers") from exc

    def control_token(request: Request):
        try:
            return parse_gpu_control_token_header_values(request.headers.getlist(GPU_GENERATION_HEADER),
                                                         request.headers.getlist(GPU_ADMISSION_TOKEN_HEADER))
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="Invalid GPU control headers") from exc

    async def begin(request: Request, scope: AdmissionScope):
        generation, token = headers(request)
        return await lifecycle.begin_workload(scope, generation_header=generation, token=token)

    async def identity(context: dict) -> tuple[str, dict | None]:
        if not isinstance(context, dict):
            raise HTTPException(422, "Context must be an object")
        if context.get("task", "detect") not in ("detect", "detection") or context.get("type") not in (None, "none"):
            raise HTTPException(422, "Only non-interactive detection is supported")
        ref = context.get("frozen_model_ref")
        variants = context.get("model_variants") or context.get("variants") or {}
        if not isinstance(variants, dict):
            raise HTTPException(422, "Model variants must be an object")
        if ref is None:
            if context.get("model_id") not in (None, "detect"):
                raise HTTPException(422, "Unknown model id")
            name = variants.get("size", config.default_model)
            if name not in config.catalog or variants.get("series", config.series) != config.series or any(k not in {"series", "size"} for k in variants):
                raise HTTPException(422, "Unsupported model variant")
            return json.dumps({"kind": "builtin", "name": name}, sort_keys=True), None
        if not isinstance(ref, dict) or variants or config.managed_store is None:
            raise HTTPException(422, "Managed model reference requires an installed model and empty variants")
        try:
            from uuid import UUID
            version = str(UUID(str(ref["model_version_id"])))
            if context.get("model_id") != "trained:" + version:
                raise ValueError("model_id does not match reference")
            receipt = await asyncio.to_thread(config.managed_store.receipt, version)
            actual = config.runtime_receipt(receipt)
            if any(actual.get(k) != ref.get(k) for k in ("model_version_id", "sha256", "classes_digest")):
                raise ValueError("reference differs from installed receipt")
            runtime = actual["inference_runtime"]
            pinned = ref.get("inference_runtime")
            if (pinned is None and runtime.get("qualification", {}).get("device") != "cpu") or (pinned is not None and pinned != runtime):
                raise ValueError("reference runtime differs from installed runtime")
            key = json.dumps({"kind": "managed", "ref": ref, "runtime": runtime,
                              "receipt_digest": receipt_digest(actual), "device": config.device},
                             sort_keys=True, separators=(",", ":"))
            return key, {"trained_model": {k: actual[k] for k in ("model_version_id", "sha256", "classes_digest")}, "inference_runtime": runtime}
        except (KeyError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc

    def runtime_facts():
        return dict(config.runtime_info() if callable(config.runtime_info) else config.runtime_info)

    @app.get("/health")
    async def health(request: Request, response: Response):
        challenge = match_gpu_health_challenge(request.headers.getlist(GPU_HEALTH_CHALLENGE_HEADER),
                                               request.query_params.getlist(GPU_HEALTH_CHALLENGE_QUERY_PARAM))
        if challenge is not None:
            response.headers[GPU_HEALTH_CHALLENGE_HEADER] = challenge
            response.headers["Cache-Control"] = "no-store"
        snapshot, residency = await lifecycle.snapshot_and_residency()
        facts = runtime_facts()
        return {"status": "ok", "compute": {"configured_device": config.device,
                "effective_device": config.device if snapshot["current_size"] else None,
                "cpu_fallback_supported": False}, "pool": snapshot,
                "gpu_info": {"physical_gpu_uuid": config.physical_gpu_uuid,
                             "driver_version": facts.get("driver_version"),
                             "total_memory_mb": facts.get("total_memory_mb")},
                "residency": residency.model_dump(mode="json")}

    @app.get("/setup")
    async def setup():
        model = {"id": "detect", "display_name": "Detection", "task": "detect", "is_interactive": False,
                 "composition": "atom", "output_attribute_types": ["class"],
                 "resource_profile": {"device": "gpu" if config.device.startswith("cuda:") else "cpu", "batchable": False},
                 "default_variants": {"series": config.series, "size": config.default_model},
                 "supported_prompts": ["none"], "supported_inputs": ["full_image", "crop"],
                 "supported_geometric_outputs": ["bbox"], "supported_variants": [
                     {"key": "series", "title": "Series", "variants": [{"value": config.series, "label": config.series}]},
                     {"key": "size", "title": "Size", "variants": [{"value": size, "label": size, "recommended": size == config.default_model} for size in config.catalog]}],
                 "params": Params.model_json_schema()}
        result = {"protocol_version": PROTOCOL_VERSION, "compat_protocol_versions": COMPAT_PROTOCOL_VERSIONS,
                  "task_contract_revision": 1, "name": "detection-plugin", "is_interactive": False,
                  "supported_prompts": ["none"], "supported_geometric_outputs": ["bbox"], "models": [model],
                  "warmup_endpoint": True, "params": Params.model_json_schema(),
                  "managed_model_compatibility": list(config.managed_model_compatibility),
                  "plugin_identity": dict(config.plugin_identity),
                  "runtime_info": runtime_facts(),
                  "physical_gpu_uuid": config.physical_gpu_uuid}
        if config.evictable_verified:
            result["managed_lifecycle"] = ManagedLifecycleCapabilities().model_dump(mode="json")
            if config.device.startswith("cuda:"):
                result["terminal_lifecycle"] = {"terminal_drain_signed_endpoint": "/lifecycle/terminal-drain-signed"}
        if config.device == "cpu" and config.expected_registry_id and not config.expected_gpu_resource_id:
            result["terminal_lifecycle"] = {"terminal_drain_endpoint": "/lifecycle/terminal-drain"}
        return result

    @app.get("/versions")
    async def versions():
        return versions_payload(config.versions.get("model_version", config.default_model),
                                config.versions.get("backend_version", "0.1.0"),
                                **{k: v for k, v in config.versions.items() if k not in {"model_version", "backend_version"}})

    @app.post("/managed-models")
    async def install(request: Request):
        if config.managed_store is None:
            raise HTTPException(404, "Managed models are not configured")
        try:
            body = config.install_request_model.model_validate(await request.json())
            return config.runtime_receipt(await config.managed_store.install(body))
        except ValidationError as exc:
            raise HTTPException(422, detail=exc.errors(include_url=False, include_input=False, include_context=False)) from exc

    @app.get("/managed-models/{version_id}")
    async def receipt(version_id: str):
        if config.managed_store is None:
            raise HTTPException(404, "Managed models are not configured")
        try:
            return config.runtime_receipt(await asyncio.to_thread(config.managed_store.receipt, version_id))
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "Managed model unavailable") from exc

    @app.post("/predict")
    async def predict(request: Request):
        operation = await begin(request, AdmissionScope.PREDICT)
        key = None
        try:
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("context", {}), dict):
                raise HTTPException(422, "Invalid prediction envelope")
            single = "task" in body and "tasks" not in body
            raw_tasks = [body["task"]] if single else body.get("tasks")
            if not isinstance(raw_tasks, list) or not raw_tasks:
                raise HTTPException(422, "At least one task is required")
            try:
                tasks = [TaskItem.model_validate(item) for item in raw_tasks]
                context = body.get("context", {})
                params = Params.model_validate(context.get("params") or {}).model_dump()
            except ValidationError as exc:
                raise HTTPException(422, detail=exc.errors(include_url=False, include_input=False, include_context=False)) from exc
            key, meta = await identity(context)
            output = []
            for task in tasks:
                try:
                    image = await asyncio.to_thread(fetch_image, task.file_path, config.allowed_image_hosts)
                except (ValueError, OSError, httpx.HTTPError) as exc:
                    raise HTTPException(422, "Image input is invalid or unavailable; check the allowed host and image limits") from exc
                async with pool.borrow(key) as lease:
                    future = asyncio.ensure_future(asyncio.to_thread(lease.resource.predict, image, params))
                    track(operation, future)
                    try:
                        detected = await asyncio.shield(future)
                    except asyncio.CancelledError:
                        while not future.done():
                            try:
                                await asyncio.shield(future)
                            except asyncio.CancelledError:
                                pass
                            except Exception:
                                break
                        raise
                    items = rectangles(detected, lease.resource.classes, image.width, image.height)
                    output.append(PredictionResult(task=task.id, result=items, meta=meta,
                                  model_version=context.get("model_id") if meta else json.loads(key)["name"],
                                  cache_hit=lease.cache_hit, model_load_ms=lease.load_ms))
            if single:
                return output[0].model_dump(exclude_none=True)
            return BatchPredictResponse(results=output).model_dump(exclude_none=True)
        finally:
            if key is not None:
                track(operation, pool.builder_for_now(key))
            await operation.close()

    @app.post("/warmup")
    async def warmup(request: Request):
        operation = await begin(request, AdmissionScope.WARMUP)
        key = None
        try:
            body = await request.json()
            key, _ = await identity(body)
            hit, duration, evicted = await pool.warmup(key)
            return {"ok": True, "cache_hit": hit, "model_load_ms": duration, "evicted": evicted}
        finally:
            if key is not None:
                track(operation, pool.builder_for_now(key))
            await operation.close()

    @app.post("/unload")
    async def unload(request: Request):
        generation, token = headers(request)
        body = await request.body()
        if not body.strip():
            if generation is not None:
                raise HTTPException(403, "Generation requires a signed transition")
            return await lifecycle.legacy_unload()
        transition = GenerationTransitionRequest.model_validate_json(body)
        return (await lifecycle.managed_unload(transition.generation, generation_header=generation, token=token)).model_dump(mode="json")

    if config.device == "cpu" and config.expected_registry_id and not config.expected_gpu_resource_id:
        @app.post("/lifecycle/terminal-drain")
        async def terminal_drain(body: TerminalDrainRequest):
            return await lifecycle.terminal_drain(
                registry_id=body.registry_id, boot_id=body.boot_id, operation_id=body.operation_id
            )

    if config.device.startswith("cuda:"):
        @app.post("/lifecycle/terminal-drain-signed")
        async def terminal_drain_signed(body: GenerationTransitionRequest, request: Request):
            generation, token = headers(request)
            return await lifecycle.terminal_drain_signed(
                body.generation, generation_header=generation, token=token
            )

    @app.post("/drain")
    @app.post("/drain/cancel")
    async def drain(request: Request):
        generation, token = headers(request)
        transition = GenerationTransitionRequest.model_validate(await request.json())
        method = lifecycle.cancel_drain if request.url.path.endswith("/cancel") else lifecycle.drain
        return (await method(transition.generation, generation_header=generation, token=token)).model_dump(mode="json")

    @app.post("/lifecycle/mode")
    async def mode(request: Request):
        return (await lifecycle.set_mode(LifecycleModeRequest.model_validate(await request.json()), token=control_token(request))).model_dump(mode="json")

    @app.post("/lifecycle/reset")
    async def reset(request: Request):
        return (await lifecycle.reset(LifecycleResetRequest.model_validate(await request.json()), token=control_token(request))).model_dump(mode="json")

    return app
