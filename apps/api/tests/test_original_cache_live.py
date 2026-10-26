"""Actual MinIO bytes and an independent Celery cleanup consumer."""

import asyncio
from datetime import timedelta
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

from celery import Celery, states
from redis import Redis
from sqlalchemy import select, text

from app.config import settings
from app.db.models.original_cache import OriginalCacheEntry as Entry
from app.services import original_cache as cache
from tests.test_original_cache import cache_world as cache_world, fill, record
from tests.storage_migration_isolation import migration_engine

# The real global sweep must not consume another test's committed due entries.
test_engine = migration_engine

_STORAGE = cache.make_storage
TASK = "app.workers.original_cache.maintain_original_cache"
_CREDENTIALS = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]*://)[^\s/@]+@"
    r"|[a-z_]*(?:secret|password|token|access_key)[a-z_]*\s*[=:]\s*\S+"
)
_WORKER = """
import sys
from functools import partial
from app.workers import original_cache
from app.workers.celery_app import celery_app
original_cache.create_async_engine = partial(
    original_cache.create_async_engine,
    connect_args={"server_settings": {"search_path": sys.argv.pop(1)}},
)
celery_app.worker_main(sys.argv[1:])
"""


def _owned_app():
    """Producer and result client built from the canonical transport settings.

    ``celery_app`` is one process-wide object whose backend, producer pool and
    result consumer are shared with every other test in the shard, so a tick
    that borrows them inherits whatever transport state an earlier test left
    behind. This client names the same broker and result backend the worker
    reads, and readiness is read from the durable meta rather than from the
    backend's fire-and-forget pub/sub delivery.
    """
    return Celery(
        "annotation_worker",
        broker=settings.effective_celery_broker,
        backend=settings.effective_celery_broker,
    )


def _redacted(line):
    return _CREDENTIALS.sub("***", line)


def _tail(lines, *, limit=60, size=8000):
    body = "\n".join(lines[-limit:])
    return body[-size:]


def _await_result(app, task_id, *, timeout):
    """Poll the worker's durable meta; a pub/sub message is not evidence."""
    backend = app.backend
    deadline = time.monotonic() + timeout
    while True:
        meta = backend.get_task_meta(task_id)
        if meta.get("status") in states.READY_STATES:
            if meta["status"] != states.SUCCESS:
                raise AssertionError(
                    _redacted(
                        "%s: %s" % (meta["status"], meta.get("traceback") or meta)
                    )
                )
            return meta["result"]
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no durable result within {timeout}s")
        time.sleep(0.1)


def _diagnostics(node, app, worker, task_id, log_path, queue):
    """Bounded, credential-safe facts that name the timed-out phase."""
    facts = [f"task={task_id}"]
    try:
        target = urlsplit(settings.redis_url)
        facts.append(f"redis_target={target.hostname}:{target.port}{target.path}")
    except Exception as exc:
        facts.append(f"redis_target={type(exc).__name__}")
    try:
        facts.append(
            "worker="
            + ("alive" if worker.poll() is None else f"exited {worker.returncode}")
        )
    except Exception as exc:
        facts.append(f"worker={type(exc).__name__}")
    try:
        facts.append(f"meta={app.backend.get_task_meta(task_id).get('status')}")
    except Exception as exc:
        facts.append(f"meta={type(exc).__name__}")
    try:
        with Redis.from_url(settings.redis_url) as client:
            facts.append(f"queued={client.llen(queue)}")
            facts.append(
                f"stored={client.exists(app.backend.get_key_for_task(task_id))}"
            )
    except Exception as exc:
        facts.append(f"redis={type(exc).__name__}")
    try:
        queues = (
            app.control.inspect(destination=[node], timeout=2).active_queues() or {}
        )
        facts.append(
            "consumes=" + repr([item["name"] for item in queues.get(node, [])])
        )
        active = app.control.inspect(destination=[node], timeout=2).active() or {}
        facts.append(f"active={[item['id'] for item in active.get(node, [])]}")
    except Exception as exc:
        facts.append(f"inspect={type(exc).__name__}")
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError as exc:
        lines = [f"log unavailable: {type(exc).__name__}"]
    return "\n".join([*facts, _tail([_redacted(line) for line in lines])])


async def test_actual_minio_copy_read_and_worker_recovery(
    cache_world, test_engine, monkeypatch, tmp_path
):
    world = cache_world
    monkeypatch.setattr(cache, "make_storage", _STORAGE)
    async with test_engine.connect() as connection:
        schema = await connection.scalar(text("SELECT current_schema()"))
    assert schema.startswith("storage_migration_test_")
    queue = f"original-cache-test-{uuid.uuid4().hex}"
    node = f"{queue}@local"
    app = _owned_app()
    task = None
    worker = None
    log_path = tmp_path / "worker.log"
    output = log_path.open("wb")
    try:
        assert await fill(world) == "stored"
        entry = await record(world)
        content = await cache.read(world.factory, world.source_id, world.asset_id)
        try:
            assert content.sha256 == world.sha and content.file.read() == world.data
        finally:
            content.close()
        async with world.factory() as db:
            current = await db.get(Entry, entry.id)
            current.expires_at = cache.now() - timedelta(seconds=1)
            current.next_check_at = current.expires_at
            await db.commit()
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WORKER,
                schema + ",public",
                "worker",
                "-P",
                "solo",
                "--loglevel=INFO",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "-Q",
                queue,
                "-n",
                node,
            ],
            cwd=Path(__file__).parents[1],
            env={
                **os.environ,
                "DATABASE_URL": test_engine.url.render_as_string(hide_password=False),
            },
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        # The result deadline covers only the task, never a cold worker import.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            registered = await asyncio.to_thread(
                lambda: (
                    app.control.inspect(destination=[node], timeout=1).registered()
                    or {}
                )
            )
            if TASK in registered.get(node, []):
                break
            assert worker.poll() is None, "Owned cache worker exited at startup"
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Owned cache worker did not register")
        task = app.send_task(TASK, queue=queue)
        try:
            response = await asyncio.to_thread(_await_result, app, task.id, timeout=40)
            assert response["removed"] >= 1 and await record(world) is None
        except Exception as exc:
            raise AssertionError(
                _diagnostics(node, app, worker, task.id, log_path, queue)
            ) from exc
        storage = _STORAGE()
        try:
            found = await asyncio.to_thread(
                storage.list_objects,
                f"original-cache/v1/{world.asset_id}/",
                bucket=world.bucket,
            )
            assert not found
        finally:
            storage.client.close()
    finally:
        if worker:
            worker.terminate()
            try:
                await asyncio.to_thread(worker.wait, timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
                await asyncio.to_thread(worker.wait, timeout=5)
        if task:
            await asyncio.to_thread(task.forget)
        app.close()
        output.close()
        log_path.unlink(missing_ok=True)
        # Failure paths also remove only this test's durable exact-key intents.
        storage = _STORAGE()
        try:
            async with world.factory() as db:
                entries = list(
                    await db.scalars(
                        select(Entry).where(
                            Entry.scope_location_id == world.location_id
                        )
                    )
                )
            for owned in entries:
                await asyncio.to_thread(
                    storage.client.delete_object,
                    Bucket=owned.bucket,
                    Key=cache.object_key(owned),
                )
        finally:
            storage.client.close()
