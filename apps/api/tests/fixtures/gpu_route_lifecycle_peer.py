"""Real YOLO lifecycle and route controllers with an in-memory model, no GPU."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "apps/yolo-backend"))

from cryptography.hazmat.primitives.serialization import load_pem_public_key
from fastapi import FastAPI, HTTPException, Request
import uvicorn

from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    GenerationTransitionRequest,
    GPU_ADMISSION_TOKEN_HEADER,
    GPU_GENERATION_HEADER,
    LifecycleModeRequest,
)
from aap_protocol_v2.routing_runtime import install_backend_routing_controller
from aap_backend_runtime.gpu_lifecycle import ManagedGpuLifecycle
from model_pool import ModelPool


class Model:
    names = {0: "fixture"}
    device = "cuda:0"


pool = ModelPool(2, lambda *_args: Model(), lambda: None, build_timeout=1)
lifecycle = ManagedGpuLifecycle(
    pool,
    verify_keyring={
        "fixture": load_pem_public_key(os.environ["AAP_FIXTURE_VERIFY_PEM"].encode())
    },
    evictable_verified=True,
    boot_id=os.environ["AAP_FIXTURE_BOOT_ID"],
    expected_registry_id=os.environ["AAP_FIXTURE_REGISTRY_ID"],
    expected_gpu_resource_id="fixture/GPU-1",
)
unloads = 0


@asynccontextmanager
async def lifespan(_app):
    try:
        yield
    finally:
        await lifecycle.shutdown()


app = FastAPI(lifespan=lifespan)
install_backend_routing_controller(
    app,
    environ={
        "AAP_ROUTE_CONTROL_TOKEN": os.environ["AAP_FIXTURE_ROUTE_TOKEN"],
        "AAP_ROUTE_REGISTRY_ID": os.environ["AAP_FIXTURE_REGISTRY_ID"],
    },
)


def credentials(request):
    if (
        request.headers.get("authorization")
        != "Bearer " + os.environ["AAP_FIXTURE_ROUTE_TOKEN"]
    ):
        raise HTTPException(status_code=401)
    return {
        "generation_header": request.headers.get(GPU_GENERATION_HEADER),
        "token": request.headers.get(GPU_ADMISSION_TOKEN_HEADER),
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "residency": (await lifecycle.residency()).model_dump(mode="json"),
        "successful_unloads": unloads,
    }


@app.post("/lifecycle/mode")
async def mode(request: Request):
    auth = credentials(request)
    return (
        await lifecycle.set_mode(
            LifecycleModeRequest.model_validate(await request.json()),
            token=auth["token"],
        )
    ).model_dump(mode="json")


@app.post("/warmup")
async def warmup(request: Request):
    operation = await lifecycle.begin_workload(
        AdmissionScope.WARMUP, **credentials(request)
    )
    try:
        async with pool.borrow("detect", "fixture", "small"):
            await asyncio.sleep(0)
        return {"ok": True}
    finally:
        await operation.close()


@app.post("/drain")
async def drain(request: Request):
    body = GenerationTransitionRequest.model_validate(await request.json())
    return (await lifecycle.drain(body.generation, **credentials(request))).model_dump(
        mode="json"
    )


@app.post("/unload")
async def unload(request: Request):
    global unloads
    body = GenerationTransitionRequest.model_validate(await request.json())
    result = await lifecycle.managed_unload(body.generation, **credentials(request))
    unloads += 1
    return result.model_dump(mode="json")


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ["AAP_FIXTURE_PORT"]),
        log_level="critical",
        access_log=False,
    )
