"""Qualify existing best-effort HTTP metadata with owned PG, Redis and workers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace
import uuid

from celery import Celery
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.middleware.audit import AuditMiddleware
from app.middleware.request_id import RequestIDMiddleware
from tests.factory import create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_module_assembly_live import _restored_database
from tests.test_identity_recovery import _postgres_tool
from tests.test_identity_sessions_live import _verify_redis
from tests.test_module_data_roles import _role_url
from tests.test_workspace_worker_assembly_live import _broker_state, _delete_owned_queue

ROOT = Path(__file__).resolve().parents[3]
TASK = "app.workers.audit.persist_audit_entry"
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_AUDIT_METADATA_DELIVERY_ACCEPTANCE") != "1",
        reason="requires explicitly selected verifier-owned Audit metadata resources",
    ),
]


@asynccontextmanager
async def _worker(directory, database_url, sender):
    _verify_redis()
    directory.mkdir()
    hostname = "audit-metadata-" + uuid.uuid4().hex + "@owned"
    proof = directory / "worker-proof.json"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "ENVIRONMENT": "development",
        "AAP_WORKTREE_MODE": "test",
        "DATABASE_URL": database_url,
        "MIGRATION_DATABASE_URL": "",
        "TEST_DATABASE_URL": "",
        "PYTEST_DATABASE_URL": "",
        "PLAYWRIGHT_E2E_DATABASE_URL": "",
        "SECRET_KEY": secrets.token_urlsafe(32),
        "SENTRY_DSN": "",
        "REDIS_URL": os.environ["REDIS_URL"],
        "CELERY_BROKER_URL": os.environ["REDIS_URL"],
        "CELERY_RESULT_BACKEND": os.environ["REDIS_URL"],
        "CELERY_BROKER_READ_URL": os.environ["REDIS_URL"],
        "CELERY_BROKER_WRITE_URL": os.environ["REDIS_URL"],
    }
    program = """
import hashlib, inspect, json, pathlib, sys
from app.workers.celery_app import celery_app
from app.workers.audit import persist_audit_entry
assert not persist_audit_entry.acks_late
source = pathlib.Path(inspect.getsourcefile(persist_audit_entry.run))
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    'task': persist_audit_entry.name, 'acks_late': bool(persist_audit_entry.acks_late),
    'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
}))
# Load the existing task without booting unrelated domain consumers.
celery_app.conf.include = ['app.workers.audit']
celery_app.worker_main(sys.argv[2:])
"""
    with (directory / "worker.log").open("wb") as log:
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            str(proof),
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            "--queues=audit",
            f"--hostname={hostname}",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=WARNING",
            cwd=ROOT / "apps/api",
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(35):
                while True:
                    assert worker.returncode is None, "Owned Audit worker exited"
                    inspector = sender.control.inspect(
                        destination=[hostname], timeout=1
                    )
                    registered = await asyncio.to_thread(inspector.registered)
                    if registered and TASK in registered.get(hostname, []):
                        break
                    await asyncio.sleep(0.1)
            evidence = json.loads(proof.read_text())
            assert evidence == {
                "task": TASK,
                "acks_late": False,
                "source_sha256": hashlib.sha256(
                    (ROOT / "apps/api/app/workers/audit.py").read_bytes()
                ).hexdigest(),
            }
            yield SimpleNamespace(process=worker, proof=evidence, node_name=hostname)
        finally:
            try:
                if worker.returncode is None:
                    worker.terminate()
                    await asyncio.wait_for(worker.wait(), 15)
            finally:
                with suppress(ProcessLookupError):
                    os.killpg(worker.pid, signal.SIGKILL)
                await worker.wait()


async def _rows(engine, request_id):
    async with engine.connect() as db:
        return (
            (
                await db.execute(
                    text(
                        "SELECT actor_id,actor_role,request_id,path,status_code,"
                        "jsonb_typeof(detail_json) AS detail_kind "
                        "FROM aap_audit_v1.audit_events WHERE request_id=:request"
                    ),
                    {"request": request_id},
                )
            )
            .mappings()
            .all()
        )


async def test_metadata_best_effort_failure_restart_and_database_restore(
    test_engine, tmp_path, monkeypatch, caplog, record_property
):
    from app.middleware import audit as middleware
    from app.workers.audit import persist_audit_entry

    _verify_postgres(test_engine.url)
    _verify_redis()
    database_url = _role_url("AAP_MODULE_DATA_AUDIT_APPEND_URL", "audit_append_client")
    engine = create_async_engine(database_url, hide_parameters=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    broker = os.environ["REDIS_URL"]
    sender = Celery("audit-metadata-acceptance", broker=broker, backend=broker)
    sender.conf.update(accept_content=["json"], task_serializer="json")
    assert make_url(persist_audit_entry.app.conf.broker_url) == make_url(broker)
    deliveries = []
    monkeypatch.setattr(middleware, "async_session", maker)
    monkeypatch.setattr(middleware.settings, "audit_async", True)
    # Alembic disables preexisting loggers; retain the best-effort failure cause.
    monkeypatch.setattr(middleware.logger, "disabled", False)
    async with async_sessionmaker(test_engine, expire_on_commit=False).begin() as db:
        actor = await create_user(
            db,
            "employee",
            f"audit-metadata-{uuid.uuid4().hex}@example.test",
            "Audit actor",
        )

    async def endpoint(request):
        return JSONResponse({"accepted": True}, status_code=201)

    app = Starlette(routes=[Route("/api/v1/audit-fixture", endpoint, methods=["POST"])])
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestIDMiddleware)

    async def request(client, request_id):
        response = await client.post(
            "/api/v1/audit-fixture", headers={"X-Request-ID": request_id}
        )
        assert response.status_code == 201 and response.json() == {"accepted": True}
        assert response.headers["X-Request-ID"] == request_id

    async def send(payload):
        delivery = await asyncio.to_thread(
            sender.send_task, TASK, args=[payload], queue="audit"
        )
        deliveries.append(delivery)
        assert await asyncio.to_thread(delivery.get, timeout=15) is None

    payload = {
        "actor_id": str(actor.id),
        "actor_role": "employee",
        "action": "http.post",
        "method": "POST",
        "path": "/api/v1/audit-fixture",
        "status_code": 201,
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }
    trigger = "audit_metadata_pause_" + uuid.uuid4().hex[:12]
    advisory_key = uuid.uuid4().int % (1 << 62)
    trigger_created = False
    try:
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT current_user")) == "audit_append_client"
            assert (
                await db.scalar(
                    text(
                        "SELECT has_table_privilege(current_user,'public.audit_logs','INSERT')"
                    )
                )
                is False
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # A refused connection to an owned bound socket exercises real enqueue failure.
            with socket.socket() as unavailable:
                unavailable.bind(("127.0.0.1", 0))
                refused_broker = f"redis://127.0.0.1:{unavailable.getsockname()[1]}/0"
                denied = Celery(
                    "audit-refused-broker",
                    broker=refused_broker,
                )
                try:
                    with monkeypatch.context() as patch:
                        # Celery's environment selector overrides its constructor broker.
                        patch.setenv("CELERY_BROKER_WRITE_URL", refused_broker)
                        patch.setattr(
                            persist_audit_entry,
                            "delay",
                            lambda body: denied.send_task(
                                TASK, args=[body], queue="audit", retry=False
                            ),
                        )
                        refused_id = str(uuid.uuid4())
                        await request(client, refused_id)
                        assert len(await _rows(test_engine, refused_id)) == 1, (
                            caplog.text
                        )
                        assert "audit task enqueue failed" in caplog.text
                finally:
                    denied.close()

            class CommitReplyLost(AsyncSession):
                async def commit(self):
                    await super().commit()
                    raise ConnectionError("fixture lost the successful commit reply")

            with monkeypatch.context() as patch:
                patch.setattr(middleware.settings, "audit_async", False)
                patch.setattr(
                    middleware,
                    "async_session",
                    async_sessionmaker(engine, class_=CommitReplyLost),
                )
                ambiguous_id = str(uuid.uuid4())
                await request(client, ambiguous_id)
                assert len(await _rows(test_engine, ambiguous_id)) == 1
            with monkeypatch.context() as patch:
                patch.setattr(middleware.settings, "audit_async", False)
                await request(client, ambiguous_id)
            assert len(await _rows(test_engine, ambiguous_id)) == 2

            async with _worker(tmp_path / "first", database_url, sender) as worker:
                await send(payload)
                assert (await _rows(test_engine, payload["request_id"]))[0][
                    "actor_id"
                ] == actor.id
                original_delay = persist_audit_entry.delay

                def publish_reply_lost(body):
                    deliveries.append(original_delay(body))
                    raise ConnectionError("fixture lost the accepted publish reply")

                with monkeypatch.context() as patch:
                    patch.setattr(persist_audit_entry, "delay", publish_reply_lost)
                    duplicate_id = str(uuid.uuid4())
                    await request(client, duplicate_id)
                assert await asyncio.to_thread(deliveries[-1].get, timeout=15) is None
                assert len(await _rows(test_engine, duplicate_id)) == 2

                async with test_engine.begin() as db:
                    await db.execute(
                        text(f"""
                        CREATE FUNCTION public.{trigger}() RETURNS trigger
                        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp
                        AS $pause$ BEGIN
                            IF NEW.request_id = '{payload["request_id"]}' THEN
                                PERFORM pg_advisory_xact_lock({advisory_key});
                            END IF;
                            RETURN NEW;
                        END $pause$
                    """)
                    )
                    await db.execute(
                        text(f"REVOKE ALL ON FUNCTION public.{trigger}() FROM PUBLIC")
                    )
                    await db.execute(
                        text(
                            f"CREATE TRIGGER {trigger} BEFORE INSERT ON public.audit_logs "
                            f"FOR EACH ROW EXECUTE FUNCTION public.{trigger}()"
                        )
                    )
                trigger_created = True
                async with test_engine.connect() as control:
                    await control.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": advisory_key},
                    )
                    lost = await asyncio.to_thread(
                        sender.send_task, TASK, args=[payload], queue="audit"
                    )
                    deliveries.append(lost)
                    async with asyncio.timeout(20):
                        while not await control.scalar(
                            text(
                                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                                "AND NOT granted AND classid=:high AND objid=:low"
                            ),
                            {
                                "high": advisory_key >> 32,
                                "low": advisory_key & 0xFFFFFFFF,
                            },
                        ):
                            await asyncio.sleep(0.1)
                    assert await asyncio.to_thread(_broker_state, sender, "audit") == {
                        "queue_size": 0,
                        "unacknowledged": 0,
                    }
                    inspector = sender.control.inspect(
                        destination=[worker.node_name], timeout=2
                    )
                    active = await asyncio.to_thread(inspector.active)
                    running = [
                        task
                        for task in active[worker.node_name]
                        if task["id"] == lost.id
                    ]
                    assert len(running) == 1
                    child_pid = running[0]["worker_pid"]
                    assert child_pid != worker.process.pid
                    assert os.getpgid(child_pid) == worker.process.pid
                    os.kill(child_pid, signal.SIGKILL)
                    await control.rollback()
                # The first committed row survives; the interrupted second insert rolls back.
                assert len(await _rows(test_engine, payload["request_id"])) == 1
            async with test_engine.begin() as db:
                await db.execute(text(f"DROP TRIGGER {trigger} ON public.audit_logs"))
                await db.execute(text(f"DROP FUNCTION public.{trigger}()"))
            trigger_created = False
            async with _worker(tmp_path / "restart", database_url, sender):
                await send({**payload, "request_id": str(uuid.uuid4())})
                assert len(await _rows(test_engine, payload["request_id"])) == 1
                assert await asyncio.to_thread(_broker_state, sender, "audit") == {
                    "queue_size": 0,
                    "unacknowledged": 0,
                }
                # Explicit replay can recover the missing attempt; it is not automatic redelivery.
                await send(payload)
                assert len(await _rows(test_engine, payload["request_id"])) == 2

        snapshot = tmp_path / "metadata.dump"
        snapshot.write_bytes(
            _postgres_tool(
                test_engine,
                [
                    "pg_dump",
                    "-U",
                    "owner",
                    "-Fc",
                    "--dbname",
                    test_engine.url.database,
                ],
            )
        )
        snapshot.chmod(0o600)
        async with _restored_database(test_engine, snapshot) as clone:
            clone_url = make_url(database_url).set(database=clone)
            clone_engine = create_async_engine(test_engine.url.set(database=clone))
            try:
                assert len(await _rows(clone_engine, payload["request_id"])) == 2
                async with _worker(
                    tmp_path / "restored",
                    clone_url.render_as_string(hide_password=False),
                    sender,
                ):
                    await send(payload)
                    assert len(await _rows(clone_engine, payload["request_id"])) == 3
                    stale_id = str(uuid.uuid4())
                    await send(
                        {
                            **payload,
                            "actor_id": str(uuid.uuid4()),
                            "request_id": stale_id,
                        }
                    )
                    rows = await _rows(clone_engine, stale_id)
                    assert len(rows) == 1 and rows[0]["actor_id"] is None
                    assert (
                        rows[0]["actor_role"] == "employee"
                        and rows[0]["detail_kind"] == "null"
                    )
            finally:
                await clone_engine.dispose()
        record_property(
            "audit_metadata_delivery",
            json.dumps(
                {
                    "existing_best_effort_semantics": True,
                    "broker_refusal_sync_fallback": True,
                    "publish_reply_loss_duplicates": True,
                    "commit_reply_loss_injected_after_real_commit": True,
                    "early_ack_process_loss_not_redelivered": True,
                    "prefork_child_sigkill": True,
                    "explicit_replay_after_restart": True,
                    "full_database_restore_and_writer_resume": True,
                    "stale_actor_anonymous_retry": True,
                    "restricted_append_login": True,
                    "worker_proof": worker.proof,
                    "dump_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                    "cold_image": False,
                }
            ),
        )
    finally:
        try:
            if trigger_created:
                async with test_engine.begin() as db:
                    await db.execute(
                        text(f"DROP TRIGGER {trigger} ON public.audit_logs")
                    )
                    await db.execute(text(f"DROP FUNCTION public.{trigger}()"))
        finally:
            try:
                for delivery in deliveries:
                    await asyncio.to_thread(delivery.forget)
                _verify_redis()
                await asyncio.to_thread(_delete_owned_queue, sender, "audit")
            finally:
                sender.close()
                await engine.dispose()
