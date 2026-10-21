"""Frozen real HTTP/SQL phases for the complete legacy health coordinator."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import uuid

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.ml_backend_pool import MLBackendPoolMember, MLBackendServicePool
from app.db.models.user import User
from app.services.ml_backend import MLBackendService
from tests.labeling_class_query_helpers import assert_owned_test_database

ROOT = Path(__file__).resolve().parents[3]
BASE = "6590de1b273dac2c6fa048968b472fc4e6b7189f"
FIXTURE = Path(__file__).parent / "fixtures/inference_health_legacy/samples.json"
FIXTURE_SHA = "7a9cfd4808e8e2b97d7b84c43621f9e724b6b2b82f0bf6dfcf74d263f7f83418"
CASES = (
    "cpu_healthy",
    "cpu_plain",
    "cpu_failed",
    "cpu_setup_failed",
    "cpu_drift",
    "gpu_echo",
    "gpu_no_echo",
    "gpu_legacy_fallback",
    "gpu_failed",
    "cpu_challenge",
    "bad_challenge",
    "huge_health_json",
)
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _phase(statement):
    s = " ".join(statement.lower().split())
    if "aap_inference_health_data_v1." in s:
        for function, phase in (
            ("observation_clock()", "clock"),
            ("lock_resource(", "resource_lock"),
            ("try_promotion()", "promotion_try"),
            ("write_backend(", "backend_write"),
            ("member_pool(", "member_read"),
            ("pool_row(", "pool_lock"),
            ("member_row(", "member_lock"),
        ):
            if function in s:
                return phase
        for function, label in (
            ("backend_row(", "backend"),
            ("membership_row(", "membership"),
        ):
            if function in s:
                return label + ("_lock" if ",true)" in s else "_read")
        if "apply_reconciliation(" in s:
            return ["member_write", "pool_write"] if "'disable'" in s else "pool_write"
    if "clock_timestamp()" in s:
        return "clock"
    if "aap:gpu-resource:" in s:
        return "resource_lock"
    if "aap:gpu-membership-promotion" in s:
        return "promotion_try"
    if s.startswith("insert into users"):
        return "unrelated_flush"
    for table, label in (
        ("ml_backend_registry", "backend"),
        ("gpu_backend_memberships", "membership"),
        ("ml_backend_pool_members", "member"),
        ("ml_backend_service_pools", "pool"),
    ):
        if s.startswith("update " + table):
            return label + "_write"
        if s.startswith("select ") and (
            " from " + table + " " in s
            or " from " + table == s[-len(" from " + table) :]
        ):
            return label + ("_lock" if "for update" in s else "_read")
    return None


@contextmanager
def _backend(case, trace):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = self.path.split("?", 1)[0]
            trace.append("http:" + route.strip("/"))
            challenge = self.headers.get("X-AAP-GPU-Health-Challenge")
            if route == "/health":
                if case == "gpu_legacy_fallback" and challenge:
                    self.send_response(422)
                    self.end_headers()
                    return
                if case in ("cpu_failed", "gpu_failed"):
                    self.send_response(503)
                    self.end_headers()
                    return
                if case == "cpu_plain":
                    body = b"ready"
                else:
                    body = json.dumps(
                        {
                            "compute": {"configured_device": "cpu"},
                            "residency": {
                                "state": "unloaded",
                                "gpu_loaded": False,
                                "active_requests": 0,
                            },
                        }
                    ).encode()
                self.send_response(200)
                if case == "gpu_echo" and challenge:
                    self.send_header("X-AAP-GPU-Health-Challenge", challenge)
            else:
                if case == "cpu_setup_failed":
                    self.send_response(503)
                    self.end_headers()
                    return
                body = json.dumps(
                    {
                        "name": "controlled",
                        "model_version": "next" if case == "cpu_drift" else "one",
                        "supported_prompts": ["bbox"],
                        "supported_trackers": ["sam2_video"],
                    }
                ).encode()
                self.send_response(200)
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


def _normalise(value, backend_id, resource):
    if isinstance(value, dict):
        return {
            k: (
                "<time>"
                if k in {"observed_at", "probe_started_at"}
                else _normalise(v, backend_id, resource)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_normalise(v, backend_id, resource) for v in value]
    if isinstance(value, str):
        return value.replace(str(backend_id), "$backend").replace(resource, "$resource")
    return value


async def _observe(test_engine, monkeypatch):
    await assert_owned_test_database(test_engine)
    monkeypatch.setattr(settings, "ml_health_timeout", 2)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    observations = []
    for case in CASES:
        for autoflush in (False, True):
            trace = []
            with _backend(case, trace) as url:
                async with maker(autoflush=autoflush) as db:
                    registry_id = uuid.uuid4()
                    resource = f"node-health/GPU-{uuid.uuid4().hex}"
                    backend = MLBackendRegistry(
                        id=registry_id,
                        name="Frozen health",
                        url=url,
                        state="disconnected",
                        is_interactive=False,
                        auth_method="token",
                        auth_token="synthetic-health-token",
                        health_meta={"old": "snapshot"},
                        gpu_resource_id=resource if case.startswith("gpu_") else None,
                        vram_budget_mb=1024 if case.startswith("gpu_") else None,
                    )
                    db.add(backend)
                    await db.flush()
                    pool = await MLBackendService(db)._create_singleton_pool(backend)
                    if case == "cpu_drift":
                        await MLBackendService(db)._reconcile_pool_capability(
                            registry_id,
                            {"model_version": "old", "supported_prompts": ["bbox"]},
                        )
                        pool.enabled = True
                        await db.flush()
                    if case == "huge_health_json":
                        await db.execute(
                            text(
                                "UPDATE public.ml_backend_registry SET health_meta=CAST(:raw AS jsonb) WHERE id=:id"
                            ),
                            {"raw": '{"n":' + "9" * 5000 + "}", "id": registry_id},
                        )
                    pending = User(
                        id=uuid.uuid4(),
                        email=f"health-pending-{uuid.uuid4().hex}@example.test",
                        name="Pending",
                        password_hash="fixture",
                        role="employee",
                    )
                    db.add(pending)
                    await db.execute(text("SET LOCAL ROLE legacy_runtime"))

                    def observe(
                        conn, cursor, statement, parameters, context, executemany
                    ):
                        phase = _phase(statement)
                        if phase:
                            trace.extend(phase if isinstance(phase, list) else [phase])

                    event.listen(
                        test_engine.sync_engine, "before_cursor_execute", observe
                    )
                    result = None
                    error = None
                    challenge = (
                        "b" * 64
                        if case.startswith("gpu_") or case == "cpu_challenge"
                        else ("INVALID" if case == "bad_challenge" else None)
                    )
                    try:
                        result = await MLBackendService(db).check_health(
                            registry_id, gpu_health_challenge=challenge
                        )
                    except Exception as exc:
                        error = {"type": type(exc).__name__, "message": str(exc)}
                    finally:
                        event.remove(
                            test_engine.sync_engine, "before_cursor_execute", observe
                        )
                    async with maker() as observer:
                        visible = await observer.scalar(
                            select(MLBackendRegistry.id).where(
                                MLBackendRegistry.id == registry_id
                            )
                        )
                        assert visible is None
                    with db.no_autoflush:
                        row = (
                            await db.execute(
                                text(
                                    "SELECT state,is_interactive,last_checked_at,health_meta::text FROM public.ml_backend_registry WHERE id=:id"
                                ),
                                {"id": registry_id},
                            )
                        ).one()
                        member = await db.scalar(
                            select(MLBackendPoolMember).where(
                                MLBackendPoolMember.registry_id == registry_id
                            )
                        )
                        current_pool = await db.scalar(
                            select(MLBackendServicePool)
                            .where(MLBackendServicePool.id == pool.id)
                            .execution_options(populate_existing=True)
                        )
                    raw = row.health_meta
                    meta = (
                        None
                        if raw is None
                        else (
                            "<oversized-json>"
                            if case == "huge_health_json"
                            else json.loads(raw)
                        )
                    )
                    proof = (
                        (meta or {}).get("gpu_arbiter_probe")
                        if isinstance(meta, dict)
                        else None
                    )
                    observations.append(
                        {
                            "case": case,
                            "autoflush": autoflush,
                            "result": result,
                            "error": error,
                            "trace": trace,
                            "caller_pending": db.in_transaction(),
                            "unrelated_pending": pending in db.new,
                            "observer_sees_backend": visible is not None,
                            "state": row.state,
                            "is_interactive": row.is_interactive,
                            "checked": row.last_checked_at is not None,
                            "health_meta": _normalise(meta, registry_id, resource),
                            "proof_time_matches_row": proof is not None
                            and proof["observed_at"]
                            == row.last_checked_at.isoformat().replace("+00:00", "Z"),
                            "pool": {
                                "enabled": current_pool.enabled,
                                "generation": current_pool.routing_generation,
                                "fingerprint": current_pool.capability_fingerprint,
                                "snapshot": current_pool.capability_snapshot,
                                "member_state": member.traffic_state,
                            },
                        }
                    )
                    await db.rollback()
    assert len(observations) == len(CASES) * 2
    return observations


@pytest.mark.skipif(
    os.environ.get("AAP_CAPTURE_INFERENCE_HEALTH_LEGACY") != "1",
    reason="capture is explicit and never overwrites the frozen baseline",
)
async def test_capture_actual_legacy_health_phases(test_engine, monkeypatch):
    output = ROOT / ".worktree/inference-health-legacy-samples.json"
    assert not output.exists() and not FIXTURE.exists()
    source = {}
    for name in (
        "apps/api/app/services/ml_backend.py",
        "apps/inference/src/aap_inference/backend_transport.py",
    ):
        expected = subprocess.check_output(
            ["rtk", "proxy", "git", "show", f"{BASE}:{name}"], cwd=ROOT
        )
        assert (ROOT / name).read_bytes() == expected
        source[name] = hashlib.sha256(expected).hexdigest()
    data = {
        "base": BASE,
        "source_sha256": source,
        "cases": await _observe(test_engine, monkeypatch),
    }
    with output.open("x") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


async def test_replay_actual_legacy_health_phases(test_engine, monkeypatch):
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA
    expected = json.loads(raw)
    observed = await _observe(test_engine, monkeypatch)
    for current, previous in zip(observed, expected["cases"], strict=True):
        trace = current["trace"]
        normalised = []
        index = 0
        while index < len(trace):
            if trace[index : index + 3] == ["member_read", "pool_lock", "member_lock"]:
                normalised.extend(["member_lock", "pool_lock"])
                index += 3
            else:
                normalised.append(trace[index])
                index += 1
        assert {**current, "trace": normalised} == previous, current["case"]
