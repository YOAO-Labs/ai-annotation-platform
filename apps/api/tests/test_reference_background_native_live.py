"""Reference background qualification through actual HTTP and MinIO transports."""

import asyncio
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import urlsplit
from urllib.request import urlopen

import httpx

from sqlalchemy import select

from yoao_inference_contract.execution import ModelTaskResult
from app.config import settings
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.services import (
    prediction_inputs,
    raster_mask_storage as masks,
    trained_models,
)
from app.services.storage import storage_service
from tests.inference_execution_test_ports import PredictionClientExecutionPort
from tests.prediction_input_live_assertions import ensure_versioned_import_bucket
from tests.test_dataset_content import headers
from tests.test_prediction_inputs import input_world as input_world
from tests.test_prediction_reference_delivery import (
    committed_reference_delivery as committed_reference_delivery,
    frame_publication_login as frame_publication_login,
    image_executor_logins as image_executor_logins,
    managed_worker_world as managed_worker_world,
    run_dispatched,
)
from tests.test_reference_background_native import (
    enqueue,
    committed_native_worker as committed_native_worker,
    response_for,
)

_STORE, _LOAD = masks.store_coco_rle, masks.load_coco_rle
_INPUT_STORAGE = prediction_inputs.make_storage


async def test_http_minio_native_reference_generation_content_and_acceptance(
    committed_native_worker, monkeypatch
):
    w, httpx_client, super_admin = committed_native_worker
    await ensure_versioned_import_bucket()
    monkeypatch.setattr(masks, "store_coco_rle", _STORE)
    monkeypatch.setattr(masks, "load_coco_rle", _LOAD)
    monkeypatch.setattr(prediction_inputs, "make_storage", _INPUT_STORAGE)
    monkeypatch.setattr(
        settings,
        "ml_backend_storage_host",
        urlsplit(storage_service.client.meta.endpoint_url).netloc,
    )
    expected = response_for(w)
    project_id, task_id = w.project.id, w.task.id
    seen, inputs = [], []
    key = w.mask["object_key"]
    preexisting = storage_service.verify_upload(key) is not None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == "/predict"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["context"]["task"] == "segment"
            assert body["context"]["frozen_model_ref"]["model_version_id"] == str(
                w.version.id
            )
            results = []
            for task in body["tasks"]:
                with urlopen(task["file_path"], timeout=5) as response:
                    content = response.read()
                assert content == w.content[1]
                seen.append(hashlib.sha256(content).hexdigest())
                inputs.append(
                    urlsplit(task["file_path"]).path.lstrip("/").split("/", 1)[1]
                )
                results.append(
                    {
                        "task": task["id"],
                        "result": expected.result,
                        "meta": expected.meta,
                        "score": expected.score,
                        "inference_time_ms": 12,
                    }
                )
            raw = json.dumps({"results": results}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        w.backend.url = f"http://127.0.0.1:{server.server_port}"
        await w.db.commit()
        await w.db.refresh(w.backend)
        w.backend.health_meta = {
            "capabilities": {"task_contract_revision": 1},
            "compute": {"effective_device": "cpu"},
        }
        w.backend.last_checked_at = datetime.now(timezone.utc)
        w.backend.state = "connected"
        await w.db.commit()
        base = f"http://127.0.0.1:{server.server_port}"

        class Client:
            async def predict(self, tasks, context=None):
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        base + "/predict",
                        json={"tasks": tasks, "context": context},
                    )
                    response.raise_for_status()
                    data = response.json()
                return trained_models.validate_and_map_results(
                    tuple(
                        ModelTaskResult(
                            task_id=item["task"],
                            result=item["result"],
                            meta=item.get("meta"),
                            score=item.get("score"),
                            inference_time_ms=item.get("inference_time_ms"),
                        )
                        for item in data["results"]
                    ),
                    w.ref,
                )

        pool_id = await w.db.scalar(
            select(ProjectMLBackendPool.pool_id).where(
                ProjectMLBackendPool.project_id == project_id
            )
        )
        monkeypatch.setattr(
            "app.application.preannotation_execution._model_execution_port",
            lambda *_args, **_kwargs: PredictionClientExecutionPort(
                Client(), instance_id=w.backend.id, pool_id=pool_id
            ),
        )
        job, dispatch = await enqueue(w, httpx_client, super_admin, monkeypatch)
        await run_dispatched(dispatch)
        await w.db.refresh(job)
        failures = list(
            await w.db.scalars(
                select(FailedPrediction.message).where(
                    FailedPrediction.project_id == project_id
                )
            )
        )
        assert job.status == "completed", failures
        prediction = await w.db.scalar(
            select(Prediction).where(Prediction.task_id == task_id)
        )
        meta = await w.db.scalar(
            select(PredictionMeta).where(PredictionMeta.prediction_id == prediction.id)
        )
        assert meta.extra["reference_native"]["reference_input"]["sha256"] == seen[0]
        response = await httpx_client.get(
            f"/api/v1/tasks/{task_id}/predictions/{prediction.id}/mask-content/0",
            headers=headers(super_admin),
        )
        assert response.status_code == 200, response.text
        assert response.json() == w.rle
        response = await httpx_client.post(
            f"/api/v1/tasks/{task_id}/predictions/{prediction.id}/accept",
            headers=headers(super_admin),
        )
        assert response.status_code == 200, response.text
        assert response.json()[0]["geometry"]["mask"]["sha256"] == w.mask["sha256"]
        assert seen == [hashlib.sha256(w.content[1]).hexdigest()]
        assert inputs and all(
            storage_service.verify_upload(value, bucket=storage_service.import_bucket)
            is None
            for value in inputs
        )
        assert await _LOAD(w.mask) == w.rle
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)
        if not preexisting:
            storage_service.client.delete_object(Bucket=storage_service.bucket, Key=key)
