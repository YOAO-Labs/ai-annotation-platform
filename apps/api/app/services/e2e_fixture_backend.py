"""Run-scoped test execution provider using the real backend route controller."""

from __future__ import annotations

import asyncio
import builtins
import hmac
import json
import os
import secrets
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
import uvicorn
from yoao_protocol_v2.routing_runtime import install_backend_routing_controller


class FixtureBackendRuntime:
    """A deterministic test provider, not a qualified model or GPU runtime."""

    def __init__(self, run_id: UUID):
        self.run_id = run_id
        self.registry_id: UUID | None = None
        self.token = secrets.token_urlsafe(32)
        self.requests: list[dict] = []
        self.attached = False
        self.failures = 0
        self.fail_all = False
        self.gate: asyncio.Event | None = None
        self.server = None
        self.task = None
        self.listener = None
        self.url = ""
        app = FastAPI()
        self.controller = install_backend_routing_controller(
            app, environ={"AAP_ROUTE_CONTROL_TOKEN": self.token}
        )

        def authorize(request: Request) -> None:
            if not hmac.compare_digest(
                request.headers.get("authorization", ""), "Bearer " + self.token
            ):
                raise HTTPException(401)

        @app.get("/health")
        async def health(request: Request):
            authorize(request)
            return {"status": "ok", "model_version": "e2-ai-request"}

        @app.get("/setup")
        async def setup(request: Request):
            authorize(request)
            model = {
                "id": "e2-detect",
                "display_name": "E2 detector",
                "task": "detection",
                "model_family": "yolo",
                "infra": "onnx",
                "composition": "atom",
                "is_interactive": False,
                "supported_prompts": ["none"],
                "supported_inputs": ["full_image"],
                "default_input_type": "full_image",
                "supported_geometric_outputs": ["bbox"],
                "output_attribute_types": ["select"],
                "output_attribute_schema": [
                    {
                        "key": "color",
                        "label": "颜色",
                        "type": "select",
                        "options": ["blue", "red"],
                    }
                ],
                "resource_profile": {"device": "cpu", "batchable": True},
                "supported_variants": [
                    {
                        "key": "size",
                        "title": "档位",
                        "variants": [
                            {"value": "small", "label": "小"},
                            {"value": "large", "label": "大"},
                        ],
                    }
                ],
                "default_variants": {"size": "small"},
                "default_thresholds": {"confidence": 0.25},
                "params": {
                    "type": "object",
                    "properties": {
                        "confidence": {
                            "type": "number",
                            "title": "置信度阈值",
                            "minimum": 0,
                            "maximum": 1,
                            "default": 0.25,
                            "x-platform-role": "confidence",
                        }
                    },
                },
                "classes": [{"index": 0, "name": "car"}],
            }
            secondary = {
                **model,
                "id": "e2-secondary",
                "display_name": "E2 secondary detector",
                "supported_prompts": ["text"],
                "supported_inputs": ["crop"],
                "default_input_type": "crop",
            }
            interactive = {
                "id": "e2-interactive",
                "display_name": "E2 interactive fixture",
                "task": "segmentation",
                "is_interactive": True,
                "supported_prompts": ["bbox", "point"],
                "supported_inputs": ["full_image"],
                "supported_geometric_outputs": ["mask"],
            }
            return {
                "name": "E2 AI request fixture",
                "version": "1",
                "protocol_version": "2.1",
                "compat_protocol_versions": ["2.0"],
                "model_version": "e2-ai-request",
                "is_interactive": True,
                "infra": "onnx",
                "models": [model, secondary, interactive],
            }

        @app.get("/classes")
        async def classes(request: Request):
            authorize(request)
            return {"classes": [{"index": 0, "name": "car"}]}

        @app.post("/predict")
        async def predict(request: Request):
            authorize(request)
            body = await request.json()
            tasks = body.get("tasks")
            single = not isinstance(tasks, list) or not tasks
            tasks = [body.get("task")] if single else tasks
            if not all(
                isinstance(task, dict) and isinstance(task.get("id"), str)
                for task in tasks
            ):
                raise HTTPException(400, "Prediction requires tasks with IDs")
            entry = {
                "tasks": tasks,
                "context": body.get("context") or {},
                "receivedAt": datetime.now(timezone.utc).timestamp() * 1000,
            }
            self.requests.append(entry)
            gate, fail = self.gate, self.fail_all or self.failures > 0
            if self.failures:
                self.failures -= 1
            if gate is not None:
                await gate.wait()
            if fail:
                entry["responseStatus"] = 503
                raise HTTPException(
                    503,
                    {
                        "error_code": "model_unavailable",
                        "message": "E2 injected model failure",
                    },
                )
            entry["responseStatus"] = 200
            result = {
                "result": [
                    {
                        "type": "rectanglelabels",
                        "value": {
                            "x": 12,
                            "y": 15,
                            "width": 25,
                            "height": 30,
                            "rectanglelabels": ["car"],
                        },
                        "attributes": {"color": "blue"},
                        "score": 0.9,
                    }
                ],
                "score": 0.9,
                "model_version": "e2-ai-request",
                "inference_time_ms": 5,
            }
            return (
                result
                if single
                else {"results": [{"task": task["id"], **result} for task in tasks]}
            )

        self.app = app

    async def start(self):
        host = os.environ.get("PLAYWRIGHT_ML_FIXTURE_HOST", "").strip()
        if not host:
            addresses = socket.getaddrinfo(
                socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
            )
            host = next(
                (row[4][0] for row in addresses if not row[4][0].startswith("127.")), ""
            )
        if (
            not host
            or any(c in host for c in "/:@?# \t\r\n")
            or host in {"localhost", "0.0.0.0"}
            or host.startswith("127.")
        ):
            raise RuntimeError(
                "PLAYWRIGHT_ML_FIXTURE_HOST must be reachable from API and workers"
            )
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("0.0.0.0", 0))
        self.listener.listen(128)
        self.listener.setblocking(False)
        port = self.listener.getsockname()[1]
        self.url = f"http://{host}:{port}"
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="0.0.0.0",
                port=port,
                lifespan="off",
                access_log=False,
                log_level="critical",
            )
        )
        self.task = asyncio.create_task(self.server.serve(sockets=[self.listener]))
        async with asyncio.timeout(10):
            while not self.server.started:
                if self.task.done():
                    await self.task
                    raise RuntimeError("Fixture provider exited before startup")
                await asyncio.sleep(0.01)
        return self

    async def close(self):
        if self.gate is not None:
            self.gate.set()
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            await self.task
        if self.listener is not None:
            self.listener.close()

    def control(self, action: str, value):
        if action == "registry":
            registry_id = str(UUID(value))
            if (
                self.controller.registry_id is not None
                or self.controller.phase != "unbound"
            ):
                raise HTTPException(409, "Fixture provider registry already bound")
            self.controller.registry_id = registry_id
        elif action == "attach":
            if self.attached:
                raise HTTPException(409, "Fixture provider already attached")
            self.attached = True
        elif action == "detach":
            if self.controller.active_requests:
                raise HTTPException(409, "Fixture provider still executing")
            self.attached = False
        elif action == "hold":
            if self.gate is None:
                self.gate = asyncio.Event()
        elif action == "release":
            if self.gate is not None:
                self.gate.set()
                self.gate = None
        elif action == "fail_all" and isinstance(value, bool):
            self.fail_all = value
        elif action == "fail_next" and type(value) is int and value > 0:
            self.failures += value
        elif action != "requests":
            raise HTTPException(422, "Invalid fixture provider operation")
        return {"requests": self.requests}


class FixtureBackendProcess:
    def __init__(self, run_id: UUID):
        self.run_id = run_id
        self.registry_id: UUID | None = None
        self.process = None
        self.lock = asyncio.Lock()
        self.url = ""
        self.token = ""

    async def start(self):
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "PLAYWRIGHT_ML_FIXTURE_HOST": os.environ.get(
                "PLAYWRIGHT_ML_FIXTURE_HOST", ""
            ),
        }
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.services.e2e_fixture_backend",
            str(self.run_id),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
        )
        line = await self.process.stdout.readline()
        if not line:
            raise RuntimeError("Fixture provider failed to start")
        ready = json.loads(line)
        self.url, self.token = ready["url"], ready["token"]
        return self

    async def control(self, action: str, value):
        async with self.lock:
            if self.process is None or self.process.returncode is not None:
                raise HTTPException(
                    409, detail={"code": "fixture_backend_owner_unavailable"}
                )
            self.process.stdin.write(
                (json.dumps({"action": action, "value": value}) + "\n").encode()
            )
            await self.process.stdin.drain()
            line = await self.process.stdout.readline()
            if not line:
                raise HTTPException(
                    409, detail={"code": "fixture_backend_owner_unavailable"}
                )
            result = json.loads(line)
            if "error" in result:
                raise HTTPException(result["error"], result["detail"])
            return result

    async def close(self):
        if self.process is not None:
            if self.process.returncode is None:
                self.process.stdin.close()
            await self.process.wait()
            if self.process.returncode:
                raise RuntimeError("Fixture provider exited unsuccessfully")


class FixtureBackends:
    def __init__(self):
        self.runs: dict[UUID, FixtureBackendProcess] = {}

    async def start(self, run_id: UUID):
        if run_id in self.runs:
            raise RuntimeError("Fixture run already has a provider")
        provider = FixtureBackendProcess(run_id)
        self.runs[run_id] = provider
        await provider.start()
        return provider

    async def retire(self, run_id: UUID):
        provider = self.runs.get(run_id)
        if provider is None:
            return
        await provider.close()
        del self.runs[run_id]

    async def close(self):
        errors = []
        for provider in self.runs.values():
            try:
                await provider.close()
            except BaseException as error:
                errors.append(error)
        if errors:
            raise builtins.BaseExceptionGroup(
                "Fixture providers failed to close", errors
            )


async def _serve_provider(run_id: UUID):
    provider = await FixtureBackendRuntime(run_id).start()
    print(json.dumps({"url": provider.url, "token": provider.token}), flush=True)
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            try:
                result = provider.control(command["action"], command["value"])
            except HTTPException as error:
                result = {"error": error.status_code, "detail": error.detail}
            print(json.dumps(result), flush=True)
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(_serve_provider(UUID(sys.argv[1])))
