"""Real health I/O with the original pending Session and three owner bindings."""

from __future__ import annotations

from contextlib import AsyncExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.db.models.user import User
from app.services.ml_backend import MLBackendService
from tests.inference_health_observation_helpers import select_binding
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@contextmanager
def backend_server(
    *, setup_failed=False, plain=False, nonfinite=False, null_health=False
):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            setup = self.path.endswith("/setup")
            body = (
                b"null"
                if null_health and not setup
                else b"ready"
                if plain and not setup
                else json.dumps(
                    {"supported_prompts": ["bbox"]}
                    if setup
                    else {
                        "compute": {"configured_device": "cpu"},
                        **({"host": {"value": float("nan")}} if nonfinite else {}),
                    }
                ).encode()
            )
            self.send_response(503 if setup and setup_failed else 200)
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
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("binding", ["borrowed", "dsn", "http"])
@pytest.mark.parametrize("autoflush", [False, True])
async def test_dirty_identity_map_endpoint_is_observed_then_current_fence_applies(
    test_engine, monkeypatch, binding, autoflush
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine, binding)
        with backend_server() as (origin, calls):
            async with maker(autoflush=autoflush) as caller:
                row = MLBackendRegistry(
                    id=uuid.uuid4(),
                    name="Pending health",
                    url=origin + "/stored",
                    auth_method="token",
                    auth_token="stored-token",
                    extra_params={},
                )
                caller.add(row)
                await caller.flush()
                row.url = origin + "/pending"
                row.auth_token = "pending-token"
                pending = User(
                    id=uuid.uuid4(),
                    email=f"health-uow-{uuid.uuid4().hex}@example.test",
                    name="Pending",
                    password_hash="fixture",
                    role="employee",
                )
                caller.add(pending)
                result = await MLBackendService(caller).check_health(row.id)
                assert result is autoflush
                assert calls == [
                    ("/pending/health", "Bearer pending-token"),
                    ("/pending/setup", "Bearer pending-token"),
                ]
                assert await caller.get(MLBackendRegistry, row.id) is row
                assert row.url == origin + ("/pending" if autoflush else "/stored")
                assert row.auth_token == (
                    "pending-token" if autoflush else "stored-token"
                )
                assert (row.last_checked_at is not None) is autoflush
                assert (pending in caller.new) is (not autoflush)
                async with maker() as outside:
                    assert await outside.get(MLBackendRegistry, row.id) is None
                    assert await outside.get(User, pending.id) is None
                assert caller.in_transaction()
                await caller.rollback()


@pytest.mark.parametrize("binding", ["borrowed", "dsn", "http"])
@pytest.mark.parametrize("outcome", ["caps", "setup_failed", "plain"])
async def test_observation_does_not_commit_and_only_caps_flush_unrelated_uow(
    test_engine, monkeypatch, binding, outcome
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine, binding)
        with backend_server(
            setup_failed=outcome == "setup_failed", plain=outcome == "plain"
        ) as (origin, calls):
            async with maker(autoflush=False) as caller:
                row = MLBackendRegistry(
                    id=uuid.uuid4(),
                    name="Health transaction",
                    url=origin,
                    auth_method="none",
                    extra_params={},
                )
                caller.add(row)
                await caller.flush()
                pending = User(
                    id=uuid.uuid4(),
                    email=f"health-caps-{uuid.uuid4().hex}@example.test",
                    name="Pending",
                    password_hash="fixture",
                    role="employee",
                )
                caller.add(pending)
                assert await MLBackendService(caller).check_health(row.id) is True
                assert row.last_checked_at is not None
                assert (pending in caller.new) is (outcome != "caps")
                assert len(calls) == (1 if outcome == "plain" else 2)
                async with maker() as outside:
                    assert await outside.get(MLBackendRegistry, row.id) is None
                    assert await outside.get(User, pending.id) is None
                await caller.rollback()
                async with maker() as outside:
                    assert await outside.get(MLBackendRegistry, row.id) is None


@pytest.mark.parametrize("binding", ["borrowed", "dsn", "http"])
async def test_frozen_health_phase_corpus_matches_for_each_binding(
    test_engine, monkeypatch, binding
):
    from tests.test_inference_health_legacy import (
        test_replay_actual_legacy_health_phases,
    )

    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine, binding)
        await test_replay_actual_legacy_health_phases(test_engine, monkeypatch)


@pytest.mark.parametrize("binding", ["borrowed", "dsn", "http"])
async def test_nonfinite_backend_metadata_keeps_original_post_clock_write_failure(
    test_engine, monkeypatch, binding
):
    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError
    from tests.test_inference_health_legacy import _phase

    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine, binding)
        with backend_server(nonfinite=True) as (origin, calls):
            async with maker() as caller:
                row = MLBackendRegistry(id=uuid.uuid4(), name="Nonfinite", url=origin)
                caller.add(row)
                await caller.flush()
                trace = []

                def observe(conn, cursor, statement, parameters, context, many):
                    phase = _phase(statement)
                    if phase:
                        trace.extend(phase if isinstance(phase, list) else [phase])

                event.listen(test_engine.sync_engine, "before_cursor_execute", observe)
                try:
                    with pytest.raises(DBAPIError):
                        await MLBackendService(caller).check_health(row.id)
                finally:
                    event.remove(
                        test_engine.sync_engine, "before_cursor_execute", observe
                    )
                assert len(calls) == 2
                assert trace.index("clock") < trace.index("backend_write")
                await caller.rollback()


@pytest.mark.parametrize("binding", ["borrowed", "dsn", "http"])
async def test_loaded_registry_uuid_crosses_closed_wire_and_refreshes_same_instance(
    test_engine, monkeypatch, binding
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine, binding)
        with backend_server() as (origin, calls):
            registry_id = uuid.uuid4()
            async with maker.begin() as seed:
                seed.add(
                    MLBackendRegistry(
                        id=registry_id, name="Loaded registry", url=origin
                    )
                )
            try:
                async with maker() as caller:
                    row = await caller.get(MLBackendRegistry, registry_id)
                    assert await MLBackendService(caller).check_health(row.id) is True
                    assert await caller.get(MLBackendRegistry, registry_id) is row
                    assert row.last_checked_at is not None
                    assert len(calls) == 2
                    async with maker() as outside:
                        assert (
                            await outside.get(MLBackendRegistry, registry_id)
                        ).last_checked_at is None
                    await caller.rollback()
            finally:
                async with maker.begin() as cleanup:
                    await cleanup.execute(
                        text("DELETE FROM public.ml_backend_registry WHERE id=:id"),
                        {"id": registry_id},
                    )


@pytest.mark.parametrize("binding", ["borrowed", "dsn", "http"])
async def test_null_health_json_keeps_legacy_type_error_before_clock(
    test_engine, monkeypatch, binding
):
    from sqlalchemy import event
    from tests.test_inference_health_legacy import _phase

    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine, binding)
        with backend_server(null_health=True) as (origin, calls):
            async with maker() as caller:
                row = MLBackendRegistry(id=uuid.uuid4(), name="Null health", url=origin)
                caller.add(row)
                await caller.flush()
                trace = []

                def observe(conn, cursor, statement, parameters, context, many):
                    phase = _phase(statement)
                    if phase:
                        trace.extend(phase if isinstance(phase, list) else [phase])

                event.listen(test_engine.sync_engine, "before_cursor_execute", observe)
                try:
                    with pytest.raises(TypeError, match="NoneType"):
                        await MLBackendService(caller).check_health(row.id)
                finally:
                    event.remove(
                        test_engine.sync_engine, "before_cursor_execute", observe
                    )
                assert calls == [("/health", None)]
                assert "clock" not in trace and "backend_write" not in trace
                assert row.last_checked_at is None
                await caller.rollback()
