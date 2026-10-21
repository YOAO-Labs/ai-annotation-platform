"""Opt-in, disposable-resource capture of actual pre-authority routing behavior."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlparse
import uuid

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.ml_backend_registry import MLBackendRegistry, ProjectMLBackendPool
from app.db.models.project import Project
from app.db.models.user import User
from app.services.ml_backend import MLBackendService
from app.services.ml_routing.client import RoutedMLBackendClient
from app.services.ml_routing.contracts import RouterMode
from app.services.ml_routing.ledger import RoutingLedger
from app.services.ml_routing.router import MLBackendRouter, route_context
from tests.factory import create_project, create_user
from tests.labeling_class_query_helpers import assert_owned_test_database

BASE = "bc719bf9648a81d3a5a167091ee94e5d7b4dc15f"
ROOT = Path(__file__).resolve().parents[3]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_CAPTURE_ROUTING_LEGACY") != "1",
        reason="capture requires explicit opt-in and verified disposable resources",
    ),
]


@contextmanager
def _backend():
    accepted = threading.Event()
    release = threading.Event()
    state = {"block": False, "active": 0, "calls": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/predict"
            with lock:
                state["active"] += 1
                state["calls"] += 1
            try:
                if state["block"]:
                    accepted.set()
                    release.wait(10)
                result = {
                    "results": [{"task": t["id"], "result": []} for t in body["tasks"]]
                }
                raw = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    state["active"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state, accepted, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _source_identity():
    paths = [
        "apps/api/app/services/ml_routing/router.py",
        "apps/api/app/services/ml_routing/ledger.py",
        "apps/api/app/services/ml_routing/client.py",
        "apps/inference/src/aap_inference/backend_transport.py",
        "apps/api/app/services/ml_client.py",
    ]
    import hashlib

    identities = {}
    for path in paths:
        original = subprocess.check_output(["git", "show", f"{BASE}:{path}"], cwd=ROOT)
        assert (ROOT / path).read_bytes() == original, path
        identities[path] = hashlib.sha256(original).hexdigest()
    return identities


async def test_capture_actual_routing_legacy(test_engine, monkeypatch):
    await assert_owned_test_database(test_engine)
    source = _source_identity()
    output = Path(os.environ["AAP_ROUTING_LEGACY_OUTPUT"])
    assert output.resolve().is_relative_to(ROOT / ".worktree")
    assert not output.exists(), "never overwrite a baseline"
    # The verifier owns this Redis instance; no shared defaults or FLUSHDB.
    assert os.environ.get("AAP_MODULE_DATA_REDIS_CONTAINER_NAME")
    assert urlparse(settings.redis_url).port not in (None, 6379)
    ns = f"aap-routing-capture:{uuid.uuid4()}"
    ledger = RoutingLedger.from_url(settings.redis_url, namespace=ns)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    ids = {}
    observation = {"source_commit": BASE, "source_sha256": source, "races": {}}
    try:
        with _backend() as (url, state, accepted, release):
            async with maker.begin() as db:
                actor = await create_user(
                    db,
                    "super_admin",
                    f"route-{uuid.uuid4()}@test.local",
                    "Route capture",
                )
                project = await create_project(db, owner_id=actor.id)
                registry = MLBackendRegistry(
                    name="Route capture",
                    url=url,
                    source="manual",
                    state="connected",
                    last_checked_at=datetime.now(UTC),
                    health_meta={"capabilities": {"model_ids": ["cpu"]}},
                    extra_params={"device": "cpu", "max_concurrency": 8},
                )
                db.add(registry)
                await db.flush()
                from aap_inference.pool_capability import capability_fingerprint

                pool = MLBackendServicePool(
                    name=f"Route-{uuid.uuid4()}",
                    enabled=True,
                    legacy_instance_id=registry.id,
                    capability_fingerprint=capability_fingerprint(
                        {"model_ids": ["cpu"]}
                    ),
                    routing_generation=1,
                )
                db.add(pool)
                await db.flush()
                db.add(
                    MLBackendPoolMember(
                        pool_id=pool.id,
                        registry_id=registry.id,
                        traffic_state="active",
                        weight=1,
                    )
                )
                db.add(
                    ProjectMLBackendPool(
                        project_id=project.id, pool_id=pool.id, enabled=True
                    )
                )
                ids.update(
                    actor=actor.id,
                    project=project.id,
                    registry=registry.id,
                    pool=pool.id,
                )

            async def predict(index):
                async with maker() as db:
                    client = RoutedMLBackendClient(
                        db,
                        ids["registry"],
                        project_id=ids["project"],
                        owner="legacy-capture",
                        operation="predict",
                        mode=RouterMode.ENFORCE,
                        ledger_factory=lambda: RoutingLedger.from_url(
                            settings.redis_url, namespace=ns
                        ),
                    )
                    start = time.perf_counter()
                    result = await client.predict([{"id": str(index), "data": {}}])
                    assert len(result) == 1 and result[0].task_id == str(index)
                    return time.perf_counter() - start

            durations = []
            for start in range(0, 16, 4):
                durations.extend(
                    await asyncio.gather(*(predict(i) for i in range(start, start + 4)))
                )
            ordered = sorted(durations)
            observation["sample"] = {
                "requests": 16,
                "concurrency": 4,
                "durations_seconds": durations,
                "p50_seconds": ordered[7],
                "p95_seconds": ordered[15],
                "backend_calls": state["calls"],
            }

            # Pause after real DB candidates, then commit a conflicting writer.
            for race in ("drain", "project_disable"):
                ready, proceed = asyncio.Event(), asyncio.Event()
                async with maker() as db:
                    router = MLBackendRouter(db, ledger=ledger, mode=RouterMode.ENFORCE)
                    original = router._build_candidates

                    async def paused(*args, **kwargs):
                        result = await original(*args, **kwargs)
                        ready.set()
                        await proceed.wait()
                        return result

                    monkeypatch.setattr(router, "_build_candidates", paused)
                    pending = asyncio.create_task(
                        router.acquire(
                            ids["pool"],
                            owner="race",
                            operation="predict",
                            project_id=ids["project"],
                        )
                    )
                    try:
                        await asyncio.wait_for(ready.wait(), 5)
                        async with maker.begin() as writer:
                            if race == "drain":
                                await MLBackendService(writer).drain_pool_member(
                                    ids["pool"], ids["registry"]
                                )
                            else:
                                await writer.execute(
                                    update(ProjectMLBackendPool)
                                    .where(ProjectMLBackendPool.pool_id == ids["pool"])
                                    .values(enabled=False)
                                )
                        proceed.set()
                        selected = await asyncio.wait_for(pending, 5)
                        assert selected.lease is not None
                        observation["races"][race] = {
                            "stale_admitted": True,
                            "lease_generation": selected.lease.generation,
                        }
                        await router.cancel(selected.lease)
                    finally:
                        proceed.set()
                        if not pending.done():
                            pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
                async with maker.begin() as writer:
                    await writer.execute(
                        update(MLBackendPoolMember)
                        .where(MLBackendPoolMember.pool_id == ids["pool"])
                        .values(traffic_state="active")
                    )
                    await writer.execute(
                        update(ProjectMLBackendPool)
                        .where(ProjectMLBackendPool.pool_id == ids["pool"])
                        .values(enabled=True)
                    )

            # Actual server work outlives the client timeout; old cancel removes its lease.
            state["block"] = True
            async with maker() as db:
                router = MLBackendRouter(db, ledger=ledger, mode=RouterMode.ENFORCE)
                from app.services.ml_client import MLBackendClient

                backend = await db.get(MLBackendRegistry, ids["registry"])
                timed_out = False
                try:
                    async with route_context(
                        router,
                        ids["pool"],
                        owner="timeout",
                        operation="predict",
                        project_id=ids["project"],
                    ):
                        async with asyncio.timeout(0.3):
                            await MLBackendClient(backend).predict(
                                [{"id": "timeout", "data": {}}]
                            )
                except TimeoutError:
                    timed_out = True
                assert timed_out and accepted.is_set() and state["active"] == 1
                inflight = await ledger.member_inflight(
                    str(ids["pool"]), str(ids["registry"])
                )
                assert inflight == 0
                observation["races"]["timeout"] = {
                    "actual_backend_active": state["active"],
                    "redis_inflight": inflight,
                }
            release.set()

            async with maker() as db:
                router = MLBackendRouter(db, ledger=ledger, mode=RouterMode.ENFORCE)
                selected = await router.acquire(
                    ids["pool"],
                    owner="reset",
                    operation="predict",
                    project_id=ids["project"],
                )
                assert selected.lease
                before = await ledger.member_inflight(
                    str(ids["pool"]), str(ids["registry"])
                )
                keys = [key async for key in ledger._redis.scan_iter(match=f"{ns}:*")]
                assert keys
                await ledger._redis.delete(*keys)
                after = await ledger.member_inflight(
                    str(ids["pool"]), str(ids["registry"])
                )
                assert before == 1 and after == 0
                observation["races"]["redis_reset"] = {"before": before, "after": after}
            output.write_text(json.dumps(observation, indent=2, sort_keys=True) + "\n")
    finally:
        try:
            keys = [key async for key in ledger._redis.scan_iter(match=f"{ns}:*")]
            if keys:
                await ledger._redis.delete(*keys)
        finally:
            await ledger.aclose()
        if ids:
            async with maker.begin() as db:
                await db.execute(delete(Project).where(Project.id == ids["project"]))
                await db.execute(
                    delete(MLBackendServicePool).where(
                        MLBackendServicePool.id == ids["pool"]
                    )
                )
                await db.execute(
                    delete(MLBackendRegistry).where(
                        MLBackendRegistry.id == ids["registry"]
                    )
                )
                await db.execute(delete(User).where(User.id == ids["actor"]))
