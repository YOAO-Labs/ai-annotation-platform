import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from celery.utils.nodenames import worker_direct
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db.models.dataset_asset import StorageLocation
from app.db.models.user import User
from app.schemas.storage_location import FilesystemLocationCreate
from app.services.storage_location import create_filesystem
from app.api.v1.storage_locations import _probe_worker
from app.workers.celery_app import celery_app
from app.workers.storage_probe import probe_location


def test_worker_rejects_wrong_actual_hostname():
    probe_location.push_request(hostname="actual@host")
    try:
        result = probe_location.run(
            str(uuid.uuid4()), 1, str(uuid.uuid4()), "other@host", None
        )
    finally:
        probe_location.pop_request()
    assert result["code"] == "wrong_execution_node"


@pytest.mark.parametrize("publish_fails", [False, True])
def test_probe_transport_failures_are_sanitized(monkeypatch, publish_fails):
    def fail(*args, **kwargs):
        raise OSError("private broker details")

    task = SimpleNamespace(get=lambda **kwargs: {"ok": True, "code": "ok"}, forget=fail)
    monkeypatch.setattr(
        probe_location.app, "connection_for_write", lambda **kwargs: nullcontext(None)
    )
    monkeypatch.setattr(
        probe_location, "apply_async", fail if publish_fails else lambda **kwargs: task
    )
    result = _probe_worker(uuid.uuid4(), 1, uuid.uuid4(), "test@node", None)
    assert result["code"] == ("node_unavailable" if publish_fails else "ok")
    assert "private" not in str(result)


async def test_real_direct_workers_distinguish_missing_mount(
    test_engine, tmp_path, monkeypatch
):
    root = tmp_path / "controlled"
    root.mkdir()
    (root / "sample.bin").write_bytes(b"sample content")
    monkeypatch.setattr(settings, "storage_filesystem_roots", {"test": str(root)})
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as db:
        user = User(
            email=f"probe-{uuid.uuid4().hex}@example.test",
            name="Probe",
            password_hash="test-only",
            role="super_admin",
            is_active=True,
            email_verified_at=datetime.now(timezone.utc),
        )
        db.add(user)
        await db.flush()
        location = await create_filesystem(
            db,
            FilesystemLocationCreate(
                name=f"probe-{uuid.uuid4().hex}", root_handle="test"
            ),
        )
        await db.commit()
        actor_id, location_id = user.id, location.id
    nodes = [f"storage-probe-{uuid.uuid4().hex[:10]}@local" for _ in range(2)]
    offline_node = f"storage-offline-{uuid.uuid4().hex[:10]}@local"
    shared_queue = f"storage-probe-{uuid.uuid4().hex}"
    processes, logs = [], []
    try:
        for index, node in enumerate(nodes):
            env = dict(
                os.environ,
                STORAGE_FILESYSTEM_ROOTS=json.dumps(
                    {"test": str(root)} if index == 0 else {}
                ),
                PYTHONDONTWRITEBYTECODE="1",
            )
            log = (tmp_path / f"worker-{index}.log").open("wb")
            logs.append(log)
            processes.append(
                subprocess.Popen(
                    [
                        str(Path.cwd() / ".venv/bin/celery"),
                        "-A",
                        "app.workers.celery_app",
                        "worker",
                        "--pool=solo",
                        "--concurrency=1",
                        "--loglevel=warning",
                        "--without-gossip",
                        "--without-mingle",
                        "--without-heartbeat",
                        "-Q",
                        shared_queue,
                        "--hostname",
                        node,
                    ],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            )
        deadline = time.monotonic() + 30
        registered = {}
        while time.monotonic() < deadline:
            registered = await run_in_threadpool(
                lambda: (
                    celery_app.control.inspect(
                        destination=nodes, timeout=1
                    ).registered()
                    or {}
                )
            )
            if all(
                "app.workers.storage_probe.probe_location" in registered.get(node, [])
                for node in nodes
            ):
                break
            assert all(process.poll() is None for process in processes), (
                "Owned probe worker exited during startup"
            )
            await asyncio.sleep(0.1)
        assert set(registered) == set(nodes), (
            "Owned workers did not register before deadline"
        )
        queues = await run_in_threadpool(
            lambda: celery_app.control.inspect(
                destination=nodes, timeout=1
            ).active_queues()
        )
        assert all(
            any(queue["name"] == worker_direct(node).name for queue in queues[node])
            for node in nodes
        )
        readable = await run_in_threadpool(
            _probe_worker, location_id, 1, actor_id, nodes[0], "sample.bin"
        )
        missing = await run_in_threadpool(
            _probe_worker, location_id, 1, actor_id, nodes[1], "sample.bin"
        )
        assert readable["ok"] and readable["capabilities"]["read"] == "verified"
        assert not missing["ok"] and missing["code"] == "root_not_configured"
        offline = await run_in_threadpool(
            _probe_worker, location_id, 1, actor_id, offline_node, None
        )
        assert not offline["ok"] and offline["code"] == "node_timeout"
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for log in logs:
            log.close()
        with celery_app.connection_for_write() as connection:
            with connection.channel() as channel:
                for node in [*nodes, offline_node]:
                    channel.queue_delete(worker_direct(node).name)
                    channel.queue_delete(f"{node}.celery.pidbox")
                channel.queue_delete(shared_queue)
        async with maker() as db:
            await db.execute(
                delete(StorageLocation).where(StorageLocation.id == location_id)
            )
            await db.execute(delete(User).where(User.id == actor_id))
            await db.commit()
