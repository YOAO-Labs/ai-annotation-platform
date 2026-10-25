"""Measure complete incremental sync cycles in an empty, owned e2e namespace.

The real ordinary Worker runs all domain tasks unchanged. Only fixture due times
are advanced explicitly between settled stages; real cadence is not measured.
The launcher owns infrastructure; this tool owns its child and source tree.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

from benchmark_dataset_scan import (  # noqa: E402
    check_database_identity,
    environment_contract,
    tree_fingerprint,
)
from benchmark_dataset_read import process_sample  # noqa: E402
from benchmark_dataset_workbench import verified_environment  # noqa: E402
from worktree_env import application_environment, atomic_json  # noqa: E402


def tool_digests():
    return {
        name: hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
        for name in (
            "benchmark_dataset_sync.py",
            "dataset_scan_benchmark_worker.py",
            "benchmark_dataset_scan.py",
            "benchmark_dataset_read.py",
            "dataset_read_benchmark_api.py",
            "benchmark_dataset_workbench.py",
        )
    }


def build_files(root, count, *, additions=False):
    from PIL import Image

    root.mkdir(parents=True, exist_ok=True)
    payloads = []
    for size, color in [
        ((32, 24), "navy"),
        ((256, 192), "green"),
        ((1024, 768), "red"),
    ]:
        body = io.BytesIO()
        with Image.new("RGB", size, color) as image:
            image.save(body, format="PNG")
        payloads.append(body.getvalue())
    expected = {}
    for index in range(count):
        group = index // 1000
        parent = root / f"group-{group:03}" / Path(*(["深层"] * (group % 3)))
        parent.mkdir(parents=True, exist_ok=True)
        path = parent / f"{'new' if additions else 'base'}-{index:06}.png"
        payload = payloads[index % len(payloads)]
        with path.open("xb") as output:
            output.write(payload)
        expected[path.relative_to(root).as_posix()] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    return expected


def aggregate_metrics(rows, wall_seconds):
    if not rows:
        raise ValueError("No settled Worker observations")
    counts, sql, tasks = Counter(), Counter(), Counter()
    execution = 0
    identities = set()
    for identifier, row in rows:
        if identifier in identities:
            raise ValueError("Repeated task observation")
        identities.add(identifier)
        duration = row["settled_monotonic"] - row["started_monotonic"]
        if duration < 0:
            raise ValueError("Invalid task clock")
        execution += duration
        counts.update(row["filesystem"])
        sql.update(row["sql"])
        tasks[row["task_name"]] += 1
    if execution > wall_seconds + 0.05:
        raise ValueError("Task timings exceed the solo Worker cycle")
    return {
        "task_count": len(rows),
        "tasks_by_name": dict(tasks),
        "worker_execution_seconds": execution,
        "outside_task_seconds": max(0, wall_seconds - execution),
        "filesystem": dict(counts),
        "sql": dict(sql),
    }


async def identities(maker, source_id):
    from sqlalchemy import select
    from app.db.models.dataset_source import DatasetSourceItem

    async with maker() as db:
        rows = await db.execute(
            select(
                DatasetSourceItem.path_digest,
                DatasetSourceItem.dataset_item_id,
                DatasetSourceItem.asset_id,
                DatasetSourceItem.current_revision_id,
                DatasetSourceItem.content_sha256,
                DatasetSourceItem.ownership_mode,
            ).where(DatasetSourceItem.source_id == source_id)
        )
        return {row[0]: tuple(str(value) for value in row[1:]) for row in rows}


async def verify_members(maker, source_id, expected, before=None):
    from sqlalchemy import select
    from app.db.models.dataset import DatasetItem
    from app.db.models.dataset_asset import DatasetAsset
    from app.db.models.dataset_source import DatasetSourceItem
    from app.services.dataset_scans import path_digest

    actual = await identities(maker, source_id)
    if set(actual) != {path_digest(path) for path in expected}:
        raise AssertionError("Accepted members differ from real source files")
    if before and any(actual.get(key) != value for key, value in before.items()):
        raise AssertionError("An old Item, Asset or accepted revision changed")
    async with maker() as db:
        rows = (
            await db.execute(
                select(
                    DatasetSourceItem.path_digest,
                    DatasetItem.file_size,
                    DatasetAsset.checksum_value,
                    DatasetAsset.size_bytes,
                )
                .join(DatasetItem, DatasetItem.id == DatasetSourceItem.dataset_item_id)
                .join(DatasetAsset, DatasetAsset.id == DatasetSourceItem.asset_id)
                .where(DatasetSourceItem.source_id == source_id)
            )
        ).all()
    expected_by_digest = {
        path_digest(path): receipt for path, receipt in expected.items()
    }
    for digest, item_size, sha, asset_size in rows:
        receipt = expected_by_digest[digest]
        if (
            sha != receipt["sha256"]
            or item_size != receipt["size"]
            or asset_size != item_size
        ):
            raise AssertionError("Accepted content proof differs from real bytes")
    if len(rows) != len(expected):
        raise AssertionError("Accepted Item/Asset relation missing")
    return actual


async def drive_cycle(maker, source_id, worker, root, metrics, report, output, label):
    from app.db.models.dataset_source import (
        DatasetSourceSyncSchedule as Schedule,
        DatasetScanRun,
        DatasetRegistrationRun,
    )
    from app.services import dataset_source_sync as sync, dataset_scans as scans
    from app.workers.dataset_source_sync import _publish

    seen = {path.stem for path in metrics.glob("*.json")}
    baseline = process_sample(worker.pid, root, Path(os.environ["TMPDIR"]))
    peaks = {key: value for key, value in baseline.items() if key != "cpu_seconds"}
    async with maker() as db:
        row = await db.get(Schedule, source_id)
        initial_scan = row.scan_id
    started, last_report, last_token = time.monotonic(), 0, None
    while True:
        if worker.poll() is not None:
            raise RuntimeError("Owned sync Worker stopped")
        sample = process_sample(worker.pid, root, Path(os.environ["TMPDIR"]))
        for key in peaks:
            peaks[key] = max(peaks[key], sample[key])
        async with maker() as db:
            row = await db.get(Schedule, source_id)
            if row.last_error or not row.enabled or row.state == "awaiting_review":
                raise RuntimeError("Sync did not satisfy the pure-additions fixture")
            settled = (
                row.state == "idle"
                and row.scan_id != initial_scan
                and row.dispatch_token is None
                and last_token is not None
                and (metrics / f"{last_token}.json").exists()
            )
            if settled:
                break
            ready = row.state == "idle" and row.scan_id == initial_scan
            run = None
            if row.state == "scanning":
                run = await db.get(DatasetScanRun, row.scan_id)
            elif row.registration_id and row.state in {"preflighting", "registering"}:
                run = await db.get(DatasetRegistrationRun, row.registration_id)
            if run:
                ready = run.status not in scans.ACTIVE
            if ready and row.dispatch_token is None:
                stamp = await scans.now(db) - timedelta(seconds=1)
                row.next_check_at = stamp
                if row.state == "idle":
                    row.next_run_at = stamp
                await db.commit()
                work = await sync.claim_due(db, source_id)
                await db.commit()
                if not work:
                    raise AssertionError("Explicit fixture wake was not claimable")
                last_token = str(work[2])
                await asyncio.to_thread(_publish, *work)
            state = {"schedule": row.state, "run_status": run.status if run else None}
            if isinstance(run, DatasetRegistrationRun):
                state["counts"] = dict(run.counts)
        elapsed = time.monotonic() - started
        if elapsed > 3600:
            raise TimeoutError("Sync observation incomplete; no restart attempted")
        if elapsed - last_report >= 10:
            last_report = elapsed
            report["current"] = {"label": label, "elapsed_seconds": elapsed, **state}
            atomic_json(output, report)
            print(json.dumps(report["current"]), flush=True)
        await asyncio.sleep(0.1)
    elapsed = time.monotonic() - started
    after = process_sample(worker.pid, root, Path(os.environ["TMPDIR"]))
    if any(
        after[key]
        for key in (
            "source_regular_fds",
            "source_directory_fds",
            "temporary_fds",
            "temporary_bytes",
        )
    ):
        raise AssertionError("Source or anonymous snapshot descriptors remain")
    rows = [
        (path.stem, json.loads(path.read_text()))
        for path in sorted(metrics.glob("*.json"))
        if path.stem not in seen
    ]
    return {
        "label": label,
        "wall_seconds": elapsed,
        "cpu_seconds": after["cpu_seconds"] - baseline["cpu_seconds"],
        "sampled_peak": peaks,
        "after": after,
        **aggregate_metrics(rows, elapsed),
    }


async def run(directory, sizes, report, output):
    from sqlalchemy import func, select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.config import settings
    from app.db.models.dataset import Dataset
    from app.db.models.dataset_asset import StorageLocation
    from app.db.models.dataset_source import DatasetSource, DatasetSourceSyncSchedule
    from app.db.models.user import User
    from app.schemas.dataset_source import DatasetSourceCreate, SourceSyncScheduleUpdate
    from app.schemas.storage_location import FilesystemLocationCreate
    from app.services import (
        dataset_scans as scans,
        storage_location as locations,
        dataset_source_sync as sync,
    )
    from app.api.v1.storage_locations import _probe_worker
    from app.workers.celery_app import celery_app

    resources = environment_contract(ROOT, os.environ)
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    worker = None
    root, metrics = directory / "sources", directory / "metrics"
    root.mkdir()
    metrics.mkdir()
    node, queue = (
        f"sync-bench-{uuid.uuid4().hex[:12]}@local",
        f"sync-bench-{uuid.uuid4().hex}",
    )
    settings.storage_filesystem_roots = {"benchmark": str(root)}
    try:
        async with maker() as db:
            current = await db.execute(
                text(
                    "SELECT current_database(), shobj_description((SELECT oid FROM pg_database WHERE datname=current_database()), 'pg_database')"
                )
            )
            check_database_identity(resources, *current.one())
            for model in (User, Dataset, DatasetSource):
                if await db.scalar(select(func.count()).select_from(model)):
                    raise ValueError("Benchmark requires an empty business database")
            actor = User(
                id=uuid.uuid4(),
                email=f"sync-{uuid.uuid4().hex}@example.test",
                name="Sync benchmark",
                role="super_admin",
                is_active=True,
                password_hash="benchmark-not-a-login",
                email_verified_at=datetime.now(timezone.utc),
            )
            db.add(actor)
            await db.flush()
            dataset = Dataset(
                display_id=f"sync-{uuid.uuid4().hex[:10]}",
                name="Sync benchmark",
                created_by=actor.id,
            )
            db.add(dataset)
            location = await locations.create_filesystem(
                db,
                FilesystemLocationCreate(
                    name="Sync benchmark", root_handle="benchmark"
                ),
            )
            await db.flush()
            actor_id, dataset_id, location_id = actor.id, dataset.id, location.id
            await db.commit()
        environment = dict(
            application_environment(os.environ),
            STORAGE_FILESYSTEM_ROOTS=json.dumps({"benchmark": str(root)}),
            DEBUG="false",
            LOG_LEVEL="WARNING",
            PYTHONDONTWRITEBYTECODE="1",
        )
        with (directory / "worker.log").open("wb") as log:
            worker = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "scripts/dataset_scan_benchmark_worker.py"),
                    str(metrics),
                    node,
                    queue,
                    "sync",
                ],
                cwd=ROOT / "apps/api",
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 60
            while True:
                if worker.poll() is not None:
                    raise RuntimeError("Observed Worker exited before registration")
                registered = await asyncio.to_thread(
                    lambda: (
                        celery_app.control.inspect(
                            destination=[node], timeout=1
                        ).registered()
                        or {}
                    )
                )
                if (
                    "app.workers.dataset_source_sync.advance_source_sync"
                    in registered.get(node, [])
                ):
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Worker registration incomplete")
                await asyncio.sleep(0.1)
            probe = await asyncio.to_thread(
                _probe_worker, location_id, 1, actor_id, node, None, None, None
            )
            if not probe["ok"]:
                raise RuntimeError("Source root probe failed")
            async with maker() as db:
                location = await db.get(StorageLocation, location_id)
                await locations.store_probe(
                    db, location, f"worker:{node}", probe, datetime.now(timezone.utc)
                )
                await db.commit()
            for count in sizes:
                source_root = root / f"files-{count}"
                expected = await asyncio.to_thread(build_files, source_root, count)
                async with maker() as db:
                    actor = await db.get(User, actor_id)
                    source = await scans.create_source(
                        db,
                        DatasetSourceCreate(
                            dataset_id=dataset_id,
                            location_id=location_id,
                            name=f"files-{count}",
                            relative_path=source_root.name,
                            node_id=node,
                            cache_policy="none",
                        ),
                        actor,
                    )
                    source_id = source.id
                    await sync.configure(
                        db,
                        source_id,
                        SourceSyncScheduleUpdate(
                            expected_revision=0,
                            enabled=True,
                            interval_minutes=15,
                            new_file_policy="pure_additions",
                        ),
                        actor,
                    )
                    await db.commit()
                setup = await drive_cycle(
                    maker,
                    source_id,
                    worker,
                    root,
                    metrics,
                    report,
                    output,
                    f"seed-{count}",
                )
                before = await verify_members(maker, source_id, expected)
                fingerprint = await asyncio.to_thread(tree_fingerprint, source_root)
                item = {
                    "baseline_files": count,
                    "baseline_bytes": sum(value["size"] for value in expected.values()),
                    "setup_seconds": setup["wall_seconds"],
                    "cycles": [],
                }
                report["profiles"].append(item)
                result = await drive_cycle(
                    maker,
                    source_id,
                    worker,
                    root,
                    metrics,
                    report,
                    output,
                    f"unchanged-{count}",
                )
                await verify_members(maker, source_id, expected, before)
                if (
                    await asyncio.to_thread(tree_fingerprint, source_root)
                    != fingerprint
                ):
                    raise AssertionError("Sync modified original files")
                result["identity_and_bytes_verified"] = True
                item["cycles"].append(result)
                added_count = max(1, count // 100)
                added = await asyncio.to_thread(
                    build_files, source_root, added_count, additions=True
                )
                expected.update(added)
                fingerprint = await asyncio.to_thread(tree_fingerprint, source_root)
                result = await drive_cycle(
                    maker,
                    source_id,
                    worker,
                    root,
                    metrics,
                    report,
                    output,
                    f"additions-{count}",
                )
                await verify_members(maker, source_id, expected, before)
                if (
                    await asyncio.to_thread(tree_fingerprint, source_root)
                    != fingerprint
                ):
                    raise AssertionError("Sync modified original files")
                result.update(
                    identity_and_bytes_verified=True,
                    added_files=added_count,
                    added_bytes=sum(value["size"] for value in added.values()),
                )
                item["cycles"].append(result)
                async with maker() as db:
                    # No Beat is running; disable this completed fixture explicitly.
                    row = await db.get(DatasetSourceSyncSchedule, source_id)
                    row.enabled = False
                    await db.commit()
                atomic_json(output, report)
                print(
                    json.dumps(
                        {"completed_files": count, "cycles": len(item["cycles"])}
                    ),
                    flush=True,
                )
        if report["tool_sha256"] != tool_digests():
            raise RuntimeError("Measurement tools changed during execution")
    finally:
        if worker is not None and worker.poll() is None:
            worker.send_signal(signal.SIGTERM)
            try:
                await asyncio.to_thread(worker.wait, timeout=20)
            except subprocess.TimeoutExpired:
                worker.kill()
                await asyncio.to_thread(worker.wait, timeout=5)
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--sizes", nargs="+", type=int, choices=[1000, 10000], default=[1000, 10000]
    )
    options = parser.parse_args()
    descriptor = os.open(options.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    report = {
        "schema": 1,
        "status": "running",
        "tool_sha256": tool_digests(),
        "code_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "scope": "local warm filesystem reference sync; manually advanced stage wakeups; production cadence excluded",
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "logical_cpus": os.cpu_count(),
        "cpu_model": next(
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        "rss_sampling_seconds": 0.1,
        "profiles": [],
    }
    backend = None
    try:
        backend, _ = verified_environment()
        with tempfile.TemporaryDirectory(prefix="dataset-sync-benchmark-") as temporary:
            report["fixture_filesystem"] = subprocess.check_output(
                ["findmnt", "-n", "-o", "FSTYPE", "--target", temporary], text=True
            ).strip()
            try:
                asyncio.run(run(Path(temporary), options.sizes, report, options.output))
                report["status"] = "passed"
            except BaseException as exc:
                report["status"] = "failed"
                report["failure_type"] = type(exc).__name__
                log = Path(temporary) / "worker.log"
                if log.exists():
                    target = options.output.with_suffix(".worker.log")
                    with os.fdopen(
                        os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                        "wb",
                    ) as output:
                        output.write(log.read_bytes()[-1048576:])
                raise
    except BaseException as exc:
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        if backend is not None:
            backend.s3.close()
        atomic_json(options.output, report)


if __name__ == "__main__":
    main()
