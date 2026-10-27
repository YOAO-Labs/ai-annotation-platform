"""Exercise the existing HTTP protocol and disposable MinIO with actual bytes."""

import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
from urllib.request import urlopen
from urllib.parse import parse_qs, urlsplit
import uuid

from aap_protocol_v2 import CocoRlePayload, native_mask_candidate_id
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.ml_backend_registry import ProjectMLBackendPool
from app.deps import get_prediction_input_session_factory
from app.services.raster_mask_storage import build_rle_reference
from app.services.storage import storage_service
from tests.conftest import create_registry_with_pool
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_references import reference_world as reference_world
from tests.test_interactive_native_mask_endpoint import _setup
from tests.test_reference_image_inference import accept_body, call
from tests.prediction_input_live_assertions import (
    assert_retired_input_versions,
    ensure_versioned_import_bucket,
    retire_known_inputs,
)


async def test_real_http_minio_native_create_refine_and_predict_test(
    db_session,
    test_engine,
    httpx_client,
    app_module,
    reference_world,
    super_admin,
    monkeypatch,
):
    _, project, task, content = reference_world
    await ensure_versioned_import_bucket()
    project.raster_mask_native_editing_enabled = True
    project.tool_bindings = {
        "region": {
            "enabled": True,
            "classes": [{"name": "car"}],
            "attribute_schema": {"fields": []},
        }
    }
    monkeypatch.setattr(settings, "raster_mask_create_enabled", True)
    monkeypatch.setattr(
        settings,
        "ml_backend_storage_host",
        urlsplit(storage_service.client.meta.endpoint_url).netloc,
    )
    received, keys, observed = [], [], []
    rles = [
        {"encoding": "coco_rle", "size": [32, 48], "counts": [13, 7, 1516]},
        {"encoding": "coco_rle", "size": [32, 48], "counts": [13, 9, 1514]},
    ]
    mask_keys = [build_rle_reference(rle)["object_key"] for rle in rles]
    preexisting = {
        key for key in mask_keys if storage_service.verify_upload(key) is not None
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, data):
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            assert self.path == "/setup"
            self.reply(_setup(["polygon", "mask"]))

        def do_POST(self):
            assert self.path == "/predict"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            tasks = body.get("tasks") or [body["task"]]
            for current in tasks:
                with urlopen(current["file_path"], timeout=5) as response:
                    raw = response.read()
                assert raw == content[1]
                received.append(hashlib.sha256(raw).hexdigest())
                parsed = urlsplit(current["file_path"])
                bucket, key = parsed.path.lstrip("/").split("/", 1)
                version = parse_qs(parsed.query)["versionId"][0]
                assert version not in {"", "null"}
                keys.append(key)
                observed.append((bucket, key, version))
            if "tasks" in body:
                self.reply(
                    {
                        "results": [
                            {"task_id": tasks[0]["id"], "result": [], "score": 0.8}
                        ]
                    }
                )
                return
            context = body["context"]
            is_refine = "mask_prompt" in context
            if is_refine:
                assert (
                    context["mask_prompt"]["source_digest"]
                    == build_rle_reference(rles[0])["sha256"]
                )
            rle = CocoRlePayload.model_validate(rles[int(is_refine)])
            self.reply(
                {
                    "result": [
                        {
                            "type": "mask",
                            "value": {
                                "rle": rle.model_dump(mode="json"),
                                "masklabels": ["car"],
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
                    "cache_hit": False,
                    "model_load_ms": 0,
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    backend, pool = await create_registry_with_pool(
        db_session,
        name=f"reference-live-{uuid.uuid4()}",
        url=f"http://127.0.0.1:{server.server_port}",
        enabled_pool=True,
    )
    db_session.add(
        ProjectMLBackendPool(project_id=project.id, pool_id=pool.id, enabled=True)
    )
    await db_session.commit()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    app_module.dependency_overrides[get_prediction_input_session_factory] = lambda: (
        factory
    )
    w = SimpleNamespace(
        project_id=project.id, task_id=task.id, backend_id=backend.id, mask=True
    )
    try:
        first = await call(httpx_client, w, headers(super_admin))
        assert first.status_code == 200, first.text
        accepted = await httpx_client.post(
            f"/api/v1/tasks/{w.task_id}/ai-mask-candidates/accept",
            json=accept_body(first.json()),
            headers=headers(super_admin),
        )
        assert accepted.status_code == 200, accepted.text
        annotation = accepted.json()["annotation"]
        refined = await call(
            httpx_client,
            w,
            headers(super_admin),
            context={
                "type": "point",
                "points": [[0.3, 0.4]],
                "labels": [1],
                "output_geometry": "mask",
                "mask_prompt_source": {
                    "annotation_id": annotation["id"],
                    "source_version": annotation["version"],
                },
            },
        )
        assert refined.status_code == 200, refined.text
        body = accept_body(refined.json())
        body["target"] = {
            "mode": "refine",
            "source_annotation_id": annotation["id"],
            "source_version": annotation["version"],
        }
        accepted_refine = await httpx_client.post(
            f"/api/v1/tasks/{w.task_id}/ai-mask-candidates/accept",
            json=body,
            headers={**headers(super_admin), "If-Match": str(annotation["version"])},
        )
        assert accepted_refine.status_code == 200, accepted_refine.text
        assert accepted_refine.json()["annotation"]["id"] == annotation["id"]
        assert accepted_refine.json()["result_version"] == annotation["version"] + 1
        tested = await call(httpx_client, w, headers(super_admin), "test")
        assert tested.status_code == 200, tested.text
        assert len(received) == 3 and len(set(received)) == 1
        await retire_known_inputs(factory, w.task_id)
        await assert_retired_input_versions(
            factory, w.task_id, storage_service, observed=observed
        )
    finally:
        app_module.dependency_overrides.pop(get_prediction_input_session_factory, None)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=5)
        for key in set(mask_keys) - preexisting:
            storage_service.delete_object(key)
