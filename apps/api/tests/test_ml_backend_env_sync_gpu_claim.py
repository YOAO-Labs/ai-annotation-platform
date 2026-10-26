"""Env backend reconciliation must never guess or overwrite GPU claims."""

from __future__ import annotations

from contextlib import AsyncExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services import ml_backend_env_sync as env
from tests.inference_registry_ingress_helpers import select_binding
from tests.labeling_class_query_helpers import assert_owned_test_database
from tests.test_inference_registry_ingress_native import (
    _cleanup_registries,
    _connect,
)


@contextmanager
def _setup_server(trace):
    """Serve valid setup for env observation and a failure for ``/failed``."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            trace.append(path)
            if path.startswith("/failed/"):
                status = 503
                body = b""
            else:
                status = 200
                body = json.dumps(
                    {
                        "name": "Controlled",
                        "supported_prompts": ["bbox"],
                        "model_version": "one",
                    }
                ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


async def test_env_sync_uses_safe_defaults_and_preserves_existing_claims(
    test_engine,
    monkeypatch,
) -> None:
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    owner = await _connect(test_engine)
    ids = [uuid.uuid4() for _ in range(4)]
    trace = []
    origin = None
    try:
        with _setup_server(trace) as origin:
            existing_env = MLBackendRegistry(
                id=ids[0],
                name="existing-env",
                url=origin + "/existing-env",
                source="env",
                state="connected",
                gpu_resource_id="node-a/GPU-env",
                vram_budget_mb=8192,
                eviction_priority=3,
            )
            existing_manual = MLBackendRegistry(
                id=ids[1],
                name="existing-manual",
                url=origin + "/existing-manual",
                source="manual",
                state="connected",
                gpu_resource_id="node-a/GPU-manual",
                vram_budget_mb=12288,
                eviction_priority=9,
            )
            stale_env = MLBackendRegistry(
                id=ids[2],
                name="stale-env",
                url=f"http://stale-{ids[2]}.example.test",
                source="env",
                state="connected",
                gpu_resource_id="node-b/GPU-stale",
                vram_budget_mb=4096,
                eviction_priority=-1,
            )
            failed_env = MLBackendRegistry(
                id=ids[3],
                name="failed-env",
                url=origin + "/failed",
                source="env",
                state="connected",
                health_meta={"compute": {"configured_device": "cpu"}},
                gpu_resource_id="node-b/GPU-failed",
                vram_budget_mb=6144,
                eviction_priority=2,
            )
            async with maker.begin() as db:
                db.add_all((existing_env, existing_manual, stale_env, failed_env))

            new_url = origin + "/new-env"
            urls = [
                new_url,
                existing_env.url,
                existing_manual.url,
                failed_env.url,
            ]
            monkeypatch.setattr(env, "_observe_urls", lambda: urls)
            async with AsyncExitStack() as stack:
                await select_binding(stack, monkeypatch, test_engine, "http")
                await env.sync_env_backends()

            async with maker() as db:
                rows = {
                    row.url: row
                    for row in (
                        await db.execute(
                            select(MLBackendRegistry).where(
                                MLBackendRegistry.url.in_([*urls, stale_env.url])
                            )
                        )
                    ).scalars()
                }
            created = rows[new_url]
            assert created.state == "connected"
            assert created.gpu_resource_id is None
            assert created.vram_budget_mb is None
            assert created.eviction_priority == 0

            refreshed_env = rows[existing_env.url]
            assert refreshed_env.state == "connected"
            assert refreshed_env.gpu_resource_id == "node-a/GPU-env"
            assert refreshed_env.vram_budget_mb == 8192
            assert refreshed_env.eviction_priority == 3

            untouched_manual = rows[existing_manual.url]
            assert untouched_manual.name == "existing-manual"
            assert untouched_manual.gpu_resource_id == "node-a/GPU-manual"
            assert untouched_manual.vram_budget_mb == 12288
            assert untouched_manual.eviction_priority == 9

            refreshed_stale = rows[stale_env.url]
            assert refreshed_stale.state == "disconnected"
            assert refreshed_stale.gpu_resource_id == "node-b/GPU-stale"
            assert refreshed_stale.vram_budget_mb == 4096
            assert refreshed_stale.eviction_priority == -1

            refreshed_failed = rows[failed_env.url]
            assert refreshed_failed.state == "error"
            assert refreshed_failed.health_meta is None
            assert refreshed_failed.gpu_resource_id == "node-b/GPU-failed"
            assert refreshed_failed.vram_budget_mb == 6144
            assert refreshed_failed.eviction_priority == 2
    finally:
        try:
            if origin is not None:
                rows = await owner.fetch(
                    "SELECT id FROM ml_backend_registry WHERE url LIKE $1",
                    origin + "%",
                )
                ids.extend(row["id"] for row in rows)
            await _cleanup_registries(owner, list(set(ids)))
        finally:
            await owner.close()
