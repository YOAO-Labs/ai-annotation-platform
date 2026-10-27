"""Actual HTTP/MinIO delivery and native candidate acceptance for one repair."""

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
import uuid

import httpx

from aap_inference import extract_capabilities
from aap_inference_contract.backend_control import BackendControlSetupResult
from aap_inference_contract.execution import ModelTaskResult, thaw_json
from aap_protocol_v2 import CocoRlePayload, native_mask_candidate_id

from app.config import settings
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services import raster_mask_storage, prediction_inputs
from app.services.storage import storage_service
from tests.inference_execution_test_ports import FakeModelExecutionPort
from tests.test_interactive_native_mask_endpoint import _setup
from tests.test_prediction_inputs import input_world as input_world
from tests.prediction_input_live_assertions import (
    assert_retired_input_versions,
    ensure_versioned_import_bucket,
    retire_known_inputs,
)
from tests.test_reference_mask_repair import (
    reference_repair as reference_repair,
    repair_client as repair_client,
    plan,
    execute,
    forward,
    state,
)

_INPUT_STORAGE = prediction_inputs.make_storage


async def test_real_http_minio_repair_candidate_and_acceptance(
    reference_repair, repair_client, monkeypatch
):
    w = reference_repair
    await ensure_versioned_import_bucket()
    monkeypatch.setattr("app.services.prediction_inputs.make_storage", _INPUT_STORAGE)
    monkeypatch.setattr(
        settings,
        "ml_backend_storage_host",
        urlsplit(storage_service.client.meta.endpoint_url).netloc,
    )
    offset = 1 + uuid.uuid4().int % 1400
    rle = CocoRlePayload(
        encoding="coco_rle", size=[32, 48], counts=[offset, 8, 1536 - offset - 8]
    )
    reference = raster_mask_storage.build_rle_reference(rle.model_dump(mode="json"))
    received, input_keys, observed = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body):
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            assert self.path == "/setup"
            self.reply(_setup(["mask", "polygon"]))

        def do_POST(self):
            assert self.path == "/predict"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            task, context = body["task"], body["context"]
            with urlopen(task["file_path"], timeout=5) as response:
                raw = response.read()
            assert raw == w.content[1]
            received.append(hashlib.sha256(raw).hexdigest())
            parsed = urlsplit(task["file_path"])
            bucket, key = parsed.path.lstrip("/").split("/", 1)
            version = parse_qs(parsed.query)["versionId"][0]
            assert version not in {"", "null"}
            input_keys.append(key)
            observed.append((bucket, key, version))
            assert context["model_id"] == "native-interactive"
            self.reply(
                {
                    "result": [
                        {
                            "type": "mask",
                            "value": {
                                "rle": rle.model_dump(mode="json"),
                                "masklabels": ["object"],
                            },
                            "score": 0.9,
                            "candidate_id": native_mask_candidate_id(
                                rle,
                                prompt_revision=context["prompt_revision"],
                                candidate_index=0,
                            ),
                        }
                    ],
                    "score": 0.9,
                    "inference_time_ms": 12,
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with w.factory() as db:
            (
                await db.get(MLBackendRegistry, w.backend_id)
            ).url = f"http://127.0.0.1:{server.server_port}"
            await db.commit()
            backend = await db.get(
                MLBackendRegistry, w.backend_id, populate_existing=True
            )
            backend.health_meta = {
                "capabilities": extract_capabilities(_setup(["mask", "polygon"]))
            }
            await db.commit()

        base = f"http://127.0.0.1:{server.server_port}"

        async def respond(operation, task_input):
            task = thaw_json(task_input)
            context = thaw_json(operation.context)
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    base + "/predict",
                    json={"task": task, "context": context},
                )
                response.raise_for_status()
                data = response.json()
            return ModelTaskResult(
                task_id=task["id"],
                result=data["result"],
                score=data.get("score"),
                inference_time_ms=data.get("inference_time_ms"),
            )

        class Control:
            async def setup(self, registry_id):
                async with httpx.AsyncClient() as client:
                    response = await client.get(base + "/setup")
                    response.raise_for_status()
                    return BackendControlSetupResult(
                        registry_id=uuid.UUID(str(registry_id)),
                        setup=response.json(),
                    )

        control = Control()
        monkeypatch.setattr(
            "app.application.mask_repair_execution.backend_control_service",
            lambda _db: control,
        )

        class Execution:
            async def execute(self, operation, *, before_dispatch=None):
                setup = await control.setup(operation.requested_registry_id)
                return await FakeModelExecutionPort(
                    respond,
                    pool_id=w.pool_id,
                    capabilities=extract_capabilities(setup.setup),
                ).execute(operation, before_dispatch=before_dispatch)

        monkeypatch.setattr(
            "app.application.mask_repair_execution.model_execution_port",
            lambda _db: Execution(),
        )
        await plan(w, sam=True)
        await execute(w)
        await forward(w)
        batch, job, _, predictions = await state(w)
        assert (batch.status, job.status, len(predictions)) == (
            "completed",
            "completed",
            1,
        ), batch.result_json
        assert received == [hashlib.sha256(w.content[1]).hexdigest()]
        assert input_keys and all(
            key.startswith("prediction-inputs/") for key in input_keys
        )
        preview = await repair_client.get(
            f"/api/v1/tasks/{w.task_id}/predictions/{predictions[0].id}/mask-content/0"
        )
        assert preview.status_code == 200, preview.text
        assert preview.json() == rle.model_dump(mode="json")
        response = await repair_client.post(
            f"/api/v1/tasks/{w.task_id}/predictions/{predictions[0].id}/accept"
        )
        assert response.status_code == 200, response.text
        assert response.json()[0]["geometry"]["mask"]["sha256"] == reference["sha256"]
        await retire_known_inputs(w.factory, w.task_id)
        await assert_retired_input_versions(
            w.factory, w.task_id, storage_service, observed=observed
        )
        assert await raster_mask_storage.load_coco_rle(reference) == rle.model_dump(
            mode="json"
        )
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)
