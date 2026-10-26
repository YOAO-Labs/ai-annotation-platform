"""Reference background qualification through actual HTTP and MinIO transports."""

import asyncio
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import urlsplit
from urllib.request import urlopen

from sqlalchemy import select

from app.config import settings
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.services import prediction_inputs, raster_mask_storage as masks
from app.services.storage import storage_service
from app.workers import tasks as worker
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_prediction_inputs import input_world as input_world
from tests.test_prediction_jobs_worker import _passthrough_engine_and_factory
from tests.test_reference_background_native import (
    enqueue,
    native_world as native_world,
    response_for,
)

_STORE, _LOAD = masks.store_coco_rle, masks.load_coco_rle
_INPUT_STORAGE = prediction_inputs.make_storage


async def test_http_minio_native_reference_generation_content_and_acceptance(
    native_world, httpx_client, super_admin, monkeypatch
):
    w = native_world
    monkeypatch.setattr(masks, "store_coco_rle", _STORE)
    monkeypatch.setattr(masks, "load_coco_rle", _LOAD)
    monkeypatch.setattr(prediction_inputs, "make_storage", _INPUT_STORAGE)
    monkeypatch.setattr(
        settings,
        "ml_backend_storage_host",
        urlsplit(storage_service.client.meta.endpoint_url).netloc,
    )
    expected = response_for(w)
    # Worker writes expire caller instances; retain only immutable identifiers.
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
        # Updating the endpoint deliberately invalidates the previous health record.
        await w.db.refresh(w.backend)
        w.backend.health_meta = {
            "capabilities": {"task_contract_revision": 1},
            "compute": {"effective_device": "cpu"},
        }
        w.backend.last_checked_at = datetime.now(timezone.utc)
        w.backend.state = "connected"
        await w.db.commit()
        job, dispatch = await enqueue(w, httpx_client, super_admin, monkeypatch)
        engine, factory = _passthrough_engine_and_factory(w.db)
        monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", engine)
        monkeypatch.setattr("sqlalchemy.ext.asyncio.async_sessionmaker", factory)
        monkeypatch.setattr(worker, "_publish_progress", lambda *a, **kw: None)
        await worker._run_batch(
            *dispatch.call_args.kwargs["args"],
            **dispatch.call_args.kwargs["kwargs"],
            celery_task_id=str(job.id),
            worker_node_id="worker:content@test",
        )
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
