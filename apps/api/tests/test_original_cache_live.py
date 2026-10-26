"""Actual MinIO bytes and an independent Celery cleanup consumer."""

import asyncio
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
import uuid

from sqlalchemy import select

from app.db.models.original_cache import OriginalCacheEntry as Entry
from app.services import original_cache as cache
from app.workers.celery_app import celery_app
from tests.test_original_cache import cache_world as cache_world, fill, record

_STORAGE = cache.make_storage


async def test_actual_minio_copy_read_and_worker_recovery(
    cache_world, monkeypatch, tmp_path
):
    world = cache_world
    monkeypatch.setattr(cache, "make_storage", _STORAGE)
    queue = f"original-cache-test-{uuid.uuid4().hex}"
    node = f"{queue}@local"
    worker = None
    result = None
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
                "-m",
                "celery",
                "-A",
                "app.workers.celery_app",
                "worker",
                "-P",
                "solo",
                "--loglevel=WARNING",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "-Q",
                queue,
                "-n",
                node,
            ],
            cwd=Path(__file__).parents[1],
            env=os.environ.copy(),
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        # The result deadline covers only the task, never a cold worker import.
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            registered = await asyncio.to_thread(
                lambda: (
                    celery_app.control.inspect(
                        destination=[node], timeout=1
                    ).registered()
                    or {}
                )
            )
            if "app.workers.original_cache.maintain_original_cache" in registered.get(
                node, []
            ):
                break
            assert worker.poll() is None, "Owned cache worker exited at startup"
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Owned cache worker did not register")
        result = celery_app.send_task(
            "app.workers.original_cache.maintain_original_cache", queue=queue
        )
        response = await asyncio.to_thread(
            result.get, timeout=40, disable_sync_subtasks=False
        )
        assert response["removed"] >= 1 and await record(world) is None
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
        if result:
            await asyncio.to_thread(result.forget)
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
