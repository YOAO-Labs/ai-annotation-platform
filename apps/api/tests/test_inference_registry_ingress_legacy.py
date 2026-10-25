"""Actual legacy env/seed ingress observations, captured before owner extraction."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import uuid

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.user import User
from app.services import ml_backend_env_sync as env
from app.services import screenshot_seed_backends as seed
from tests.labeling_class_query_helpers import assert_owned_test_database

ROOT = Path(__file__).resolve().parents[3]
BASE = "3951a84fc629e5be6a71b6bfb2751fec0a781bfa"
FIXTURE = (
    Path(__file__).parent / "fixtures/inference_registry_ingress_legacy/samples.json"
)
NOOP_FIXTURE = FIXTURE.with_name("noops.json")
SOURCES = (
    "apps/api/app/services/ml_backend_env_sync.py",
    "apps/api/app/services/screenshot_seed_backends.py",
    "apps/api/app/services/ml_backend.py",
)
ENV_CASES = (
    "new_ok",
    "new_failed",
    "existing_ok",
    "existing_failed",
    "manual_ok",
    "manual_invalid_setup",
    "stale",
    "empty",
    "invalid_setup",
    "huge_row",
    "bad_name",
)
SEED_CASES = (
    "live_ok",
    "live_failed",
    "live_plain_health",
    "live_setup_failed",
    "live_empty_setup",
    "live_huge_row",
    "live_bad_extra",
    "live_nan",
    "stub_new",
    "stub_existing",
    "stub_false",
    "stub_zero",
    "stub_empty_list",
    "stub_bad_extra",
)
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_REGISTRY_INGRESS_LEGACY_EXCLUSIVE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _normalise(value, origin):
    if isinstance(value, dict):
        return {k: _normalise(v, origin) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normalise(v, origin) for v in value]
    if isinstance(value, datetime):
        return "<time>"
    if isinstance(value, str):
        return value.replace(origin, "<backend>")
    if isinstance(value, float) and not math.isfinite(value):
        return "<nonfinite>"
    return value


def _memory(rows, origin):
    fields = (
        "name",
        "url",
        "source",
        "state",
        "is_interactive",
        "extra_params",
        "health_meta",
        "last_checked_at",
        "error_message",
    )
    return [
        _normalise({field: inspect(row).dict.get(field) for field in fields}, origin)
        for row in rows
    ]


@contextmanager
def _server(case, trace):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            trace.append("http:" + path)
            status = 200
            if path == "/health":
                if case == "live_failed":
                    status = 503
                body = (
                    b"ready"
                    if case == "live_plain_health"
                    else json.dumps(
                        {
                            "compute": {"configured_device": "cpu"},
                            **(
                                {"host": {"n": float("nan")}}
                                if case == "live_nan"
                                else {}
                            ),
                        }
                    ).encode()
                )
            else:
                if case in {"new_failed", "existing_failed", "live_setup_failed"}:
                    status = 503
                setup = {
                    "name": "mock-v2-backend"
                    if case.startswith("stub_")
                    else "Controlled",
                    "supported_prompts": ["bbox"],
                    "model_version": "one",
                }
                if case in {"invalid_setup", "manual_invalid_setup"}:
                    setup = ["bad"]
                if case == "bad_name":
                    setup["name"] = {"bad": "name"}
                if case == "live_empty_setup":
                    setup = None
                body = json.dumps(setup).encode()
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


def _phase(statement):
    s = " ".join(statement.lower().split())
    native = "aap_inference_registry_ingress_v1."
    for function, phase in (
        ("registry_by_url(", "registry_read"),
        ("registry_candidates(", "registry_read"),
        ("create_registry(", "registry_insert"),
        ("singleton_pool(", "pool_read"),
        ("create_singleton_pool(", ["pool_insert", "member_insert"]),
        ("apply_observation(", "registry_write"),
        ("disconnect_env_registry(", "registry_write"),
        ("tag_seed_registry(", "registry_write"),
    ):
        if native + function in s:
            return phase
    if s.startswith("insert into users"):
        return "unrelated_flush"
    for table, label in (
        ("ml_backend_registry", "registry"),
        ("ml_backend_service_pools", "pool"),
        ("ml_backend_pool_members", "member"),
    ):
        if s.startswith("insert into " + table):
            return label + "_insert"
        if s.startswith("update " + table):
            return label + "_write"
        if s.startswith("select ") and (
            " from " + table + " " in s or s.endswith(" from " + table)
        ):
            return label + "_read"
    return None


async def _snapshot(engine, origin):
    async with engine.connect() as db:
        rows = (
            (
                await db.execute(
                    text(
                        "SELECT name,url,source,state,is_interactive,extra_params::text,health_meta::text,last_checked_at,error_message FROM ml_backend_registry ORDER BY url"
                    )
                )
            )
            .mappings()
            .all()
        )
        result = []
        for row in rows:
            value = dict(row)
            for key in ("extra_params", "health_meta"):
                raw = value[key]
                value[key] = (
                    "<oversized-json>"
                    if raw and len(raw) > 4500
                    else (None if raw is None else json.loads(raw))
                )
            result.append(_normalise(value, origin))
        pools = (
            (
                await db.execute(
                    text(
                        "SELECT r.url,p.name,p.enabled,p.routing_generation,p.capability_fingerprint,p.capability_snapshot::text,m.traffic_state,m.weight FROM ml_backend_service_pools p JOIN ml_backend_pool_members m ON m.pool_id=p.id JOIN ml_backend_registry r ON r.id=m.registry_id ORDER BY r.url"
                    )
                )
            )
            .mappings()
            .all()
        )
        shaped = []
        for row in pools:
            value = dict(row)
            raw = value["capability_snapshot"]
            value["capability_snapshot"] = None if raw is None else json.loads(raw)
            shaped.append(_normalise(value, origin))
        users = await db.scalar(
            text(
                "SELECT count(*) FROM users WHERE email LIKE 'ingress-pending-%@example.test'"
            )
        )
    return {"registry": result, "pools": shaped, "pending_user_visible": users == 1}


async def _clear(engine):
    async with engine.begin() as db:
        await db.execute(text("DELETE FROM ml_backend_pool_members"))
        await db.execute(text("DELETE FROM ml_backend_service_pools"))
        await db.execute(text("DELETE FROM ml_backend_registry"))
        await db.execute(
            text("DELETE FROM users WHERE email LIKE 'ingress-pending-%@example.test'")
        )


async def _prepare(engine, origin, case):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    if case in {"new_ok", "new_failed", "invalid_setup", "bad_name", "stub_new"}:
        return
    async with maker.begin() as db:
        extra = {"keep": {"nested": True}}
        if case in {"live_bad_extra", "stub_bad_extra"}:
            extra = [1]
        if case == "stub_false":
            extra = False
        if case == "stub_zero":
            extra = 0
        if case == "stub_empty_list":
            extra = []
        if case == "stub_matching_marker":
            extra["seed"] = {
                "managed_by": seed.SEED_MANAGED_BY,
                "profile": "screenshots",
                "revision": seed.SEED_REVISION,
            }
        row = MLBackendRegistry(
            id=uuid.uuid4(),
            name="mock-v2-backend" if case.startswith("stub_") else "Before",
            url=origin,
            source="manual"
            if case.startswith("manual_")
            or case.startswith("stub_")
            or case.startswith("live_")
            else "env",
            state="disconnected" if case == "already_disconnected" else "connected",
            is_interactive=True,
            auth_method="none",
            extra_params=extra,
            health_meta={"old": "snapshot"},
            error_message="old error",
        )
        db.add(row)
        await db.flush()
        if case in {"huge_row", "live_huge_row"}:
            await db.execute(
                text(
                    "UPDATE ml_backend_registry SET health_meta=CAST(:raw AS jsonb) WHERE id=:id"
                ),
                {"raw": '{"n":' + "9" * 5000 + "}", "id": row.id},
            )


async def _observe(engine, monkeypatch, *, env_cases=ENV_CASES, seed_cases=SEED_CASES):
    await assert_owned_test_database(engine)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    observations = []
    for kind, cases in (("env", env_cases), ("seed", seed_cases)):
        for case in cases:
            for autoflush in (False, True):
                await _clear(engine)
                trace = []
                with _server(case, trace) as origin:
                    await _prepare(engine, origin, case)
                    pending = User(
                        id=uuid.uuid4(),
                        email=f"ingress-pending-{uuid.uuid4().hex}@example.test",
                        name="Pending",
                        password_hash="fixture",
                        role="employee",
                    )
                    error = None
                    flush_error = None
                    memory = []
                    caller_pending = None
                    unrelated_pending = None
                    original_limits = (
                        env.capability_limits_from_settings
                        if kind == "env"
                        else seed.capability_limits_from_settings
                    )

                    def limits():
                        trace.append("limits")
                        return original_limits()

                    class Clock(datetime):
                        @classmethod
                        def now(cls, tz=None):
                            trace.append("clock")
                            return datetime.now(tz)

                    def sql(conn, cursor, statement, parameters, context, executemany):
                        phase = _phase(statement)
                        if phase:
                            trace.extend(phase if isinstance(phase, list) else [phase])

                    with monkeypatch.context() as patch:
                        patch.setattr(env if kind == "env" else seed, "datetime", Clock)
                        patch.setattr(
                            env if kind == "env" else seed,
                            "capability_limits_from_settings",
                            limits,
                        )
                        event.listen(engine.sync_engine, "before_cursor_execute", sql)
                        try:
                            if kind == "env":

                                @asynccontextmanager
                                async def session():
                                    nonlocal caller_pending, unrelated_pending, memory
                                    async with maker(autoflush=autoflush) as db:
                                        db.add(pending)
                                        try:
                                            yield db
                                        finally:
                                            caller_pending = db.in_transaction()
                                            unrelated_pending = pending in db.new
                                            memory = _memory(
                                                [
                                                    r
                                                    for r in db.identity_map.values()
                                                    if isinstance(r, MLBackendRegistry)
                                                ],
                                                origin,
                                            )

                                patch.setattr(env, "async_session", session)
                                patch.setattr(
                                    env,
                                    "_observe_urls",
                                    lambda: (
                                        []
                                        if case
                                        in {"empty", "stale", "already_disconnected"}
                                        else [origin]
                                    ),
                                )
                                try:
                                    await env.sync_env_backends()
                                except Exception as exc:
                                    error = type(exc).__name__
                            else:
                                async with maker(autoflush=autoflush) as db:
                                    db.add(pending)
                                    try:
                                        rows = (
                                            await seed._stub_candidate(db, origin)
                                            if case.startswith("stub_")
                                            else await seed._live_candidates(db)
                                        )
                                        memory = _memory(rows, origin)
                                        caller_pending = db.in_transaction()
                                        unrelated_pending = pending in db.new
                                        try:
                                            await db.flush()
                                        except Exception as exc:
                                            flush_error = type(exc).__name__
                                    except Exception as exc:
                                        error = type(exc).__name__
                                        caller_pending = db.in_transaction()
                                        unrelated_pending = pending in db.new
                                    await db.rollback()
                        finally:
                            event.remove(
                                engine.sync_engine, "before_cursor_execute", sql
                            )
                    observations.append(
                        {
                            "kind": kind,
                            "case": case,
                            "autoflush": autoflush,
                            "error": error,
                            "flush_error": flush_error,
                            "trace": trace,
                            "caller_pending": caller_pending,
                            "unrelated_pending": unrelated_pending,
                            "memory": sorted(memory, key=lambda v: v.get("url") or ""),
                            "database": await _snapshot(engine, origin),
                        }
                    )
    await _clear(engine)
    return observations


@pytest.mark.skipif(
    os.environ.get("AAP_CAPTURE_REGISTRY_INGRESS_LEGACY") != "1",
    reason="capture is explicit and never overwrites a baseline",
)
async def test_capture_actual_registry_ingress_legacy(test_engine, monkeypatch):
    output = ROOT / ".worktree/inference-registry-ingress-legacy-samples.json"
    assert not FIXTURE.exists() and not output.exists()
    hashes = {}
    for name in SOURCES:
        expected = subprocess.check_output(
            ["rtk", "proxy", "git", "show", f"{BASE}:{name}"], cwd=ROOT
        )
        assert (ROOT / name).read_bytes() == expected
        hashes[name] = hashlib.sha256(expected).hexdigest()
    result = {
        "base": BASE,
        "source_sha256": hashes,
        "cases": await _observe(test_engine, monkeypatch),
    }
    with output.open("x") as stream:
        stream.write(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )


async def test_replay_actual_registry_ingress_legacy(test_engine, monkeypatch):
    if not FIXTURE.exists():
        pytest.skip("capture has not yet been promoted")
    expected = os.environ.get("AAP_REGISTRY_INGRESS_LEGACY_SHA256")
    assert expected, "Root must pin the captured registry ingress SHA256"
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == expected
    fixture = json.loads(raw)
    observed = await _observe(test_engine, monkeypatch)
    (ROOT / ".worktree" / f"ingress-replay-all-{uuid.uuid4().hex}.json").write_text(
        json.dumps(observed, indent=2)
    )
    assert len(observed) == len(fixture["cases"])
    for actual, old in zip(observed, fixture["cases"], strict=True):
        actual = _qualified_sample(actual, native=hasattr(env, "_sync_borrowed"))
        old = _qualified_sample(old, native=False)
        if actual != old:
            diagnostic = (
                ROOT / ".worktree" / f"ingress-replay-mismatch-{uuid.uuid4().hex}.json"
            )
            diagnostic.write_text(
                json.dumps({"actual": actual, "expected": old}, indent=2)
            )
        assert actual == old, (actual["kind"], actual["case"], actual["autoflush"])


def _qualified_sample(value, *, native):
    sample = deepcopy(value)
    if sample["kind"] == "env" and sample["case"] in {"new_failed", "existing_failed"}:
        # Failed setup uses this fixture's ephemeral host/port as the default name.
        for row in [
            *sample["memory"],
            *sample["database"]["registry"],
            *sample["database"]["pools"],
        ]:
            assert re.fullmatch(r"127\.0\.0\.1:[0-9]+", row["name"])
            row["name"] = "<backend-host>"
    if not native:
        return sample
    if sample["case"] == "bad_name" and not sample["autoflush"]:
        # Native failure precedes ORM failed-flush cleanup; caller close still rolls back.
        assert sample["unrelated_pending"] is True
        assert sample["database"] == {
            "registry": [],
            "pools": [],
            "pending_user_visible": False,
        }
        sample["unrelated_pending"] = False
    if (
        sample["kind"] == "seed"
        and not sample["autoflush"]
        and sample["case"]
        in {
            "stub_existing",
            "stub_false",
            "stub_zero",
            "stub_empty_list",
            "stub_matching_marker",
        }
    ):
        # The atomic Native Pool+Member macro follows the caller's pre-probe flush.
        start = sample["trace"].index("pool_read") + 1
        end = sample["trace"].index("http:/health")
        marker = [] if sample["case"] == "stub_matching_marker" else ["registry_write"]
        assert sample["trace"][start:end] == [
            *marker,
            "unrelated_flush",
            "pool_insert",
            "member_insert",
        ]
        sample["trace"][start:end] = [
            "pool_insert",
            *marker,
            "unrelated_flush",
            "member_insert",
        ]
    return sample


@pytest.mark.skipif(
    os.environ.get("AAP_CAPTURE_REGISTRY_INGRESS_NOOPS") != "1",
    reason="additional capture is explicit and never rewrites the original corpus",
)
async def test_capture_registry_ingress_noops(test_engine, monkeypatch):
    output = ROOT / ".worktree/inference-registry-ingress-noops.json"
    assert not NOOP_FIXTURE.exists() and not output.exists()
    for name in SOURCES:
        expected = subprocess.check_output(
            ["rtk", "proxy", "git", "show", f"{BASE}:{name}"], cwd=ROOT
        )
        assert (ROOT / name).read_bytes() == expected
    result = {
        "base": BASE,
        "cases": await _observe(
            test_engine,
            monkeypatch,
            env_cases=("already_disconnected",),
            seed_cases=("stub_matching_marker",),
        ),
    }
    with output.open("x") as stream:
        stream.write(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )


async def test_replay_registry_ingress_noops(test_engine, monkeypatch):
    if not NOOP_FIXTURE.exists():
        pytest.skip("additional capture has not yet been promoted")
    raw = NOOP_FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == os.environ.get(
        "AAP_REGISTRY_INGRESS_NOOPS_SHA256"
    )
    actual = await _observe(
        test_engine,
        monkeypatch,
        env_cases=("already_disconnected",),
        seed_cases=("stub_matching_marker",),
    )
    assert [
        _qualified_sample(row, native=hasattr(env, "_sync_borrowed")) for row in actual
    ] == json.loads(raw)["cases"]
