"""Real owned worker acceptance; all committed resources are removed in finally."""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from celery.utils.nodenames import worker_direct
from sqlalchemy import delete, select, func
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_asset import StorageLocation
from app.db.models.dataset_source import DatasetSource, DatasetScanRun, DatasetScanEntry
from app.db.models.notification import Notification
from app.db.models.user import User
from app.schemas.dataset_source import DatasetSourceCreate
from app.schemas.storage_location import FilesystemLocationCreate
from app.services import dataset_scans as svc, storage_location as locations
from app.workers.celery_app import celery_app
from app.api.v1.storage_locations import _probe_worker
from app.workers.dataset_scan import dispatch


async def test_real_worker_scans_only_metadata_and_rejects_duplicate_delivery(
    test_engine, tmp_path, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    (root / "new dataset").mkdir()
    (root / "new dataset" / "中文.jpg").write_bytes(b"metadata only")
    monkeypatch.setattr(settings, "storage_filesystem_roots", {"test": str(root)})
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    node = f"inventory-{uuid.uuid4().hex[:12]}@local"
    queue = f"inventory-{uuid.uuid4().hex}"
    env = dict(
        os.environ,
        STORAGE_FILESYSTEM_ROOTS=json.dumps({"test": str(root)}),
        PYTHONDONTWRITEBYTECODE="1",
    )
    process = None
    actor_id = dataset_id = location_id = source_id = run_id = job_id = None
    with (tmp_path / "worker.log").open("wb") as log:
        try:
            async with maker() as db:
                actor = User(
                    id=uuid.uuid4(),
                    email=f"scan-{uuid.uuid4().hex}@example.test",
                    name="Inventory",
                    role="super_admin",
                    is_active=True,
                    password_hash="test-only",
                    email_verified_at=datetime.now(timezone.utc),
                )
                db.add(actor)
                await db.flush()
                dataset = Dataset(
                    display_id=f"ds-{uuid.uuid4().hex[:12]}",
                    name="Inventory",
                    created_by=actor.id,
                )
                db.add(dataset)
                await db.flush()
                location = await locations.create_filesystem(
                    db,
                    FilesystemLocationCreate(
                        name=f"inventory-{uuid.uuid4().hex}", root_handle="test"
                    ),
                )
                actor_id, dataset_id, location_id = actor.id, dataset.id, location.id
                await db.commit()
            process = subprocess.Popen(
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
                    queue,
                    "--hostname",
                    node,
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                registered = await asyncio.to_thread(
                    lambda: (
                        celery_app.control.inspect(
                            destination=[node], timeout=1
                        ).registered()
                        or {}
                    )
                )
                if "app.workers.dataset_scan.run_dataset_scan" in registered.get(
                    node, []
                ):
                    break
                assert process.poll() is None, (
                    "Owned inventory worker stopped during startup"
                )
            else:
                raise AssertionError("Owned inventory worker did not register")
            result = await asyncio.to_thread(
                _probe_worker, location_id, 1, actor_id, node, None, None, None
            )
            assert result["ok"], result["code"]
            async with maker() as db:
                location = await db.get(StorageLocation, location_id)
                actor = await db.get(User, actor_id)
                await locations.store_probe(
                    db, location, f"worker:{node}", result, datetime.now(timezone.utc)
                )
                source = await svc.create_source(
                    db,
                    DatasetSourceCreate(
                        dataset_id=dataset_id,
                        location_id=location_id,
                        name="Inventory",
                        node_id=node,
                    ),
                    actor,
                )
                source_id = source.id
                run, _ = await svc.start_scan(db, source.id, actor)
                run_id, job_id = run.id, run.async_job_id
                await db.commit()
                await dispatch(db, run.id)
                await dispatch(db, run.id)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                async with maker() as db:
                    run = await db.get(DatasetScanRun, run_id)
                    if run.status not in svc.ACTIVE:
                        assert run.complete and run.counts["file_count"] == 1, (
                            run.status,
                            run.error_code,
                        )
                        assert (
                            await db.scalar(
                                select(func.count())
                                .select_from(DatasetScanEntry)
                                .where(DatasetScanEntry.run_id == run.id)
                            )
                            == 3
                        )
                        assert (
                            await db.scalar(
                                select(func.count())
                                .select_from(DatasetItem)
                                .where(DatasetItem.dataset_id == dataset_id)
                            )
                            == 0
                        )
                        break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("Owned inventory run did not finish")
            assert (root / "new dataset" / "中文.jpg").read_bytes() == b"metadata only"
        finally:
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            with celery_app.connection_for_write() as connection:
                with connection.channel() as channel:
                    channel.queue_delete(worker_direct(node).name)
                    channel.queue_delete(f"{node}.celery.pidbox")
                    channel.queue_delete(queue)
            async with maker() as db:
                if run_id:
                    await db.execute(
                        delete(DatasetScanRun).where(DatasetScanRun.id == run_id)
                    )
                if source_id:
                    await db.execute(
                        delete(DatasetSource).where(DatasetSource.id == source_id)
                    )
                if job_id:
                    await db.execute(
                        delete(Notification).where(Notification.target_id == job_id)
                    )
                    await db.execute(delete(AsyncJob).where(AsyncJob.id == job_id))
                if dataset_id:
                    await db.execute(delete(Dataset).where(Dataset.id == dataset_id))
                if location_id:
                    await db.execute(
                        delete(StorageLocation).where(StorageLocation.id == location_id)
                    )
                if actor_id:
                    await db.execute(delete(User).where(User.id == actor_id))
                await db.commit()
