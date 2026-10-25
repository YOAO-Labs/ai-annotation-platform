"""Separate CPU peer with actual route accounting and externally held work."""

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
import uvicorn

from aap_protocol_v2.routing_runtime import install_backend_routing_controller


app = FastAPI()
controller = install_backend_routing_controller(
    app,
    environ={
        "AAP_ROUTE_CONTROL_TOKEN": os.environ["AAP_FIXTURE_ROUTE_TOKEN"],
        "AAP_ROUTE_REGISTRY_ID": os.environ["AAP_FIXTURE_REGISTRY_ID"],
    },
)
gates = Path(os.environ["AAP_FIXTURE_GATES"])
entered: list[str] = []
finished: list[str] = []


async def held_work(label: str):
    while not (gates / label).exists():
        await asyncio.sleep(0.01)
    finished.append(label)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "incarnation": controller.backend_incarnation,
        "phase": controller.phase,
        "active_requests": controller.active_requests,
        "entered": entered,
        "finished": finished,
    }


@app.post("/predict")
async def predict(request: Request):
    if request.headers.get("authorization") != (
        "Bearer " + os.environ["AAP_FIXTURE_ROUTE_TOKEN"]
    ):
        raise HTTPException(status_code=401)
    payload = await request.json()
    label, mode = payload.get("label"), payload.get("mode", "handler")
    if label not in {"old", "other", "delayed", "new"} or mode not in {
        "handler",
        "background",
    }:
        raise HTTPException(status_code=400)
    entered.append(label)
    if mode == "background":
        future = asyncio.create_task(held_work(label))
        assert controller.track_background_future(future)
    else:
        await held_work(label)
    return {"label": label, "ok": True}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ["AAP_FIXTURE_PORT"]),
        log_level="critical",
        access_log=False,
    )
