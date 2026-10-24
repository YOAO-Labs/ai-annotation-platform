"""Actual HTTP/MinIO delivery and native candidate acceptance for one repair."""

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
import uuid

from aap_protocol_v2 import CocoRlePayload, native_mask_candidate_id

from app.config import settings
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services import raster_mask_storage, prediction_inputs
from app.services.ml_client import MLBackendClient
from app.services.ml_capabilities import extract_capabilities
from app.services.storage import storage_service
from app.workers import mask_repair as worker
from tests.test_interactive_native_mask_endpoint import _setup
from tests.test_prediction_inputs import input_world as input_world
from tests.prediction_input_live_assertions import (
    assert_retired_input_versions,
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

_SETUP = MLBackendClient.setup
_PREDICT = MLBackendClient.predict_interactive
_STORE = worker._store_result_rle
_PREPARE = raster_mask_storage.prepare_mask_payload_for_write
_INPUT_STORAGE = prediction_inputs.make_storage


async def test_real_http_minio_repair_candidate_and_acceptance(
    reference_repair, repair_client, monkeypatch
):
    w = reference_repair
    for target, value in (
        ("app.services.ml_client.MLBackendClient.setup", _SETUP),
        ("app.services.ml_client.MLBackendClient.predict_interactive", _PREDICT),
        ("app.workers.mask_repair._store_result_rle", _STORE),
        ("app.services.prediction_inputs.make_storage", _INPUT_STORAGE),
        ("app.services.raster_mask_storage.prepare_mask_payload_for_write", _PREPARE),
        ("app.services.prediction.prepare_mask_payload_for_write", _PREPARE),
        ("app.services.annotation.prepare_mask_payload_for_write", _PREPARE),
    ):
        monkeypatch.setattr(target, value)
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
    mask_key = reference["object_key"]
    preexisting = storage_service.verify_upload(mask_key) is not None
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
        if not preexisting:
            storage_service.client.delete_object(
                Bucket=storage_service.bucket, Key=mask_key
            )
