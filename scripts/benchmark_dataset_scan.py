"""Measure real directory inventories inside an owned, disposable e2e environment.

Run through dev:worktree exec --mode e2e, using the current API virtualenv.
The launcher owns infrastructure cleanup; this tool owns its Worker and file tree.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
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


def tool_digests():
    return {
        name: hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
        for name in ["benchmark_dataset_scan.py", "dataset_scan_benchmark_worker.py"]
    }


def environment_contract(root, environ):
    from sqlalchemy.engine import make_url
    from worktree_env import load_identity, read_state, state_path, topology

    if environ.get("AAP_WORKTREE_MODE") != "e2e":
        raise ValueError("Use dev:worktree exec --mode e2e")
    resources = topology(root, load_identity(root), "e2e")
    manifest = read_state(state_path(root, "e2e", "resources.json"))
    if manifest["resources"] != resources:
        raise ValueError("Resource manifest does not match this checkout")
    if any(environ.get(key) != value for key, value in resources["buckets"].items()):
        raise ValueError("All nine owned storage overrides are required")
    database = make_url(environ.get("DATABASE_URL", ""))
    if (
        database.drivername != "postgresql+asyncpg"
        or database.host not in {"localhost", "127.0.0.1", "::1"}
        or database.database != resources["database"]
        or database.host != manifest["postgres"]["host"]
        or (database.port or 5432) != manifest["postgres"]["port"]
    ):
        raise ValueError("Use the launcher's exact local database target")
    return resources


def check_database_identity(resources, database, owner):
    if database != resources["database"] or owner != resources["owner"]:
        raise ValueError("Database identity or ownership mismatch")


def check_worker_environment(observed, expected):
    from worktree_env import BUCKETS

    keys = [
        "DATABASE_URL",
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "CELERY_BROKER_READ_URL",
        "CELERY_BROKER_WRITE_URL",
        "CELERY_RESULT_BACKEND",
        "MINIO_ENDPOINT",
        "TMPDIR",
        *BUCKETS,
    ]
    if any(observed.get(key) != expected[key] for key in keys):
        raise ValueError("Worker environment does not match owned resources")
    if any(
        observed.get(key)
        for key in [
            "MIGRATION_DATABASE_URL",
            "TEST_DATABASE_URL",
            "PLAYWRIGHT_E2E_DATABASE_URL",
        ]
    ):
        raise ValueError("Ordinary Worker must not receive owner/test credentials")


def verify_worker_environment():
    from app.config import settings
    from worktree_resources import LocalResources
    from worktree_runtime import validate_inventory, require_owner

    resources = environment_contract(ROOT, os.environ)
    backend = LocalResources(resources, settings, code_root=ROOT)
    try:
        observed = backend.inspect()
        validate_inventory(resources, observed)
        for name, row in [
            (resources["database"], observed["database"]),
            (resources["redis_name"], observed["redis"]),
            *observed["buckets"].items(),
        ]:
            if not require_owner(name, row, resources["owner"]):
                raise ValueError("Owned Worker resource is missing")
        check_worker_environment(os.environ, backend.environment())
    finally:
        backend.s3.close()


def build_tree(root: Path, count: int, *, flat=False):
    from PIL import Image

    root.mkdir()
    output = io.BytesIO()
    Image.new("RGB", (4, 3), "navy").save(output, format="PNG")
    for index in range(count):
        group, ordinal = divmod(index, 1000)
        parent = (
            root if flat else root / f"组-{group:03}" / Path(*(["深层"] * (group % 3)))
        )
        parent.mkdir(parents=True, exist_ok=True)
        # Equal basenames in distinct directories are intentional.
        suffix = ".tmp" if index % 100 == 0 else ".png"
        payload = b"damaged image" if index % 101 == 0 else output.getvalue()
        (parent / f"同名-{index if flat else ordinal:04}{suffix}").write_bytes(payload)
    return tree_fingerprint(root)


def tree_fingerprint(root):
    entries, digest = {}, hashlib.sha256()
    for directory, folders, files in os.walk(root):
        folders.sort()
        files.sort()
        relative = Path(directory).relative_to(root)
        if relative.parts:
            entries[relative.as_posix()] = "directory"
        for name in files:
            path = Path(directory) / name
            key = path.relative_to(root).as_posix()
            info = path.stat()
            digest.update(json.dumps([key, info.st_size, info.st_mtime_ns]).encode())
            digest.update(path.read_bytes())
            entries[key] = "file"
    return entries, digest.hexdigest()


def process_observation(pid, root):
    status = Path(f"/proc/{pid}/status").read_text()
    values = {
        line.split(":", 1)[0]: int(line.split()[1])
        for line in status.splitlines()
        if line.startswith(("VmRSS:", "VmHWM:"))
    }
    descriptors = 0
    for path in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(path)
        except FileNotFoundError:
            continue
        if target == str(root) or target.startswith(str(root) + "/"):
            descriptors += 1
    return {
        "rss_kib": values["VmRSS"],
        "hwm_kib": values["VmHWM"],
        "source_fds": descriptors,
    }


async def wait_attempt(
    maker, run_id, task_id, worker, root, metrics, *, started, cancel_at=None
):
    from sqlalchemy import select, func
    from app.db.models.async_job import AsyncJob
    from app.db.models.dataset_source import DatasetScanRun, DatasetScanEntry
    from app.db.models.user import User
    from app.services import dataset_scans as scans

    peak, cancelled, checkpoint, old_fence = 0, None, None, None
    requested, captured = None, None
    while time.monotonic() - started < 210:
        if worker.poll() is not None:
            raise RuntimeError("Owned scan Worker stopped")
        observation = process_observation(worker.pid, root)
        peak = max(peak, observation["rss_kib"])
        async with maker() as db:
            run = await db.get(DatasetScanRun, run_id)
            if (
                cancel_at
                and cancelled is None
                and run.counts.get("file_count", 0) >= cancel_at
                and run.status == "running"
            ):
                old_fence = (run.generation, run.lease_token)
                job = await db.get(AsyncJob, run.async_job_id)
                actor = await db.get(User, job.user_id)
                requested = time.monotonic()
                await scans.cancel_job(db, job.id, actor)
                await db.commit()
                cancelled = time.monotonic() - requested
                checkpoint = await db.scalar(
                    select(func.count())
                    .select_from(DatasetScanEntry)
                    .where(DatasetScanEntry.run_id == run_id)
                )
                captured = await db.scalar(
                    select(func.count())
                    .select_from(DatasetScanEntry)
                    .where(
                        DatasetScanEntry.run_id == run_id,
                        DatasetScanEntry.kind == "directory",
                        DatasetScanEntry.state == "captured",
                    )
                )
            terminal = run.status not in scans.ACTIVE
            result = {
                "status": run.status,
                "complete": run.complete,
                "counts": dict(run.counts),
                "error_code": run.error_code,
            }
        metric_file = metrics / f"{task_id}.json"
        if terminal and metric_file.exists():
            observation = process_observation(worker.pid, root)
            if observation["source_fds"]:
                raise AssertionError(
                    "Worker retained source descriptors after task settlement"
                )
            result.update(json.loads(metric_file.read_text()))
            result.update(
                wall_seconds=time.monotonic() - started,
                sampled_peak_rss_kib=peak,
                worker_hwm_kib=observation["hwm_kib"],
                source_fds_after=0,
            )
            if cancel_at:
                if (
                    cancelled is None
                    or result["status"] != "cancelled"
                    or result["complete"]
                ):
                    raise AssertionError(
                        "Cancellation did not interrupt an active scan"
                    )
                result.update(
                    cancel_commit_seconds=cancelled,
                    cancel_settlement_observed_seconds=time.monotonic() - requested,
                    cancelled_checkpoint_rows=checkpoint,
                    captured_directories_at_cancel=captured,
                )
            return result, old_fence
        await asyncio.sleep(0.05)
    raise TimeoutError("Owned scan attempt exceeded benchmark deadline")


async def run_benchmark(directory, sizes):
    from sqlalchemy import event, func, select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.config import settings
    from app.db.models.async_job import AsyncJob
    from app.db.models.dataset import Dataset, DatasetItem
    from app.db.models.dataset_asset import StorageLocation
    from app.db.models.dataset_source import (
        DatasetSource,
        DatasetScanEntry,
        DatasetScanRun,
    )
    from app.db.models.user import User
    from app.schemas.dataset_source import DatasetSourceCreate
    from app.schemas.storage_location import FilesystemLocationCreate
    from app.services import dataset_scans as scans, storage_location as locations
    from app.workers.celery_app import celery_app
    from app.workers.dataset_scan import dispatch
    from app.api.v1.storage_locations import _probe_worker

    resources = environment_contract(ROOT, os.environ)
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    worker = None
    node = f"scan-bench-{uuid.uuid4().hex[:12]}@local"
    queue = f"scan-bench-{uuid.uuid4().hex}"
    root, metrics = directory / "sources", directory / "metrics"
    root.mkdir()
    metrics.mkdir()
    settings.storage_filesystem_roots = {"benchmark": str(root)}
    profiles = [(f"files-{size}", size, False, False) for size in sizes]
    profiles += [
        ("cancel-resume", 10000, False, True),
        ("directory-budget", 5001, True, False),
        ("scan-budget", 50001, False, False),
    ]
    report = {
        "schema": 1,
        "tool_sha256": tool_digests(),
        "code_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "platform": platform.platform(),
        "logical_cpus": os.cpu_count(),
        "cpu_model": next(
            (
                line.split(":", 1)[1].strip()
                for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")
            ),
            "unknown",
        ),
        "memory_total_kib": int(
            next(
                line.split()[1]
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemTotal:")
            )
        ),
        "fixture_filesystem": subprocess.check_output(
            ["findmnt", "-n", "-o", "FSTYPE", "--target", str(directory)], text=True
        ).strip(),
        "cache_state": "newly written fixtures; OS caches not flushed; one warm Worker reused",
        "rss_sampling_ms": 50,
        "instrumentation": "Worker-only SQL and filesystem counters; per-directory timings include instrumentation",
        "limits": {"per_directory": 5000, **scans.BUDGET},
        "profiles": [],
    }
    try:
        async with maker() as db:
            current = await db.execute(
                text(
                    "SELECT current_database(), shobj_description((SELECT oid FROM pg_database WHERE datname=current_database()), 'pg_database')"
                )
            )
            check_database_identity(resources, *current.one())
            for model in [User, Dataset, DatasetSource, DatasetScanRun, AsyncJob]:
                if await db.scalar(select(func.count()).select_from(model)):
                    raise ValueError(
                        "Benchmark requires an empty disposable environment"
                    )
            actor = User(
                id=uuid.uuid4(),
                email=f"scan-{uuid.uuid4().hex}@example.test",
                name="Scan benchmark",
                role="super_admin",
                is_active=True,
                password_hash="benchmark-not-a-login",
                email_verified_at=datetime.now(timezone.utc),
            )
            db.add(actor)
            await db.flush()
            dataset = Dataset(
                display_id=f"scan-{uuid.uuid4().hex[:10]}",
                name="Scan benchmark",
                created_by=actor.id,
            )
            db.add(dataset)
            await db.flush()
            location = await locations.create_filesystem(
                db,
                FilesystemLocationCreate(
                    name="Scan benchmark", root_handle="benchmark"
                ),
            )
            actor_id, dataset_id, location_id = actor.id, dataset.id, location.id
            await db.commit()
        from worktree_env import application_environment

        env = dict(
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
                ],
                cwd=ROOT / "apps/api",
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + 60
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
                if worker.poll() is not None:
                    raise RuntimeError("Benchmark Worker did not start")
                await asyncio.sleep(0.1)
            else:
                raise TimeoutError("Benchmark Worker did not register")
            probe = await asyncio.to_thread(
                _probe_worker, location_id, 1, actor_id, node, None, None, None
            )
            if not probe["ok"]:
                raise RuntimeError("Owned source root probe failed")
            async with maker() as db:
                location = await db.get(StorageLocation, location_id)
                await locations.store_probe(
                    db, location, f"worker:{node}", probe, datetime.now(timezone.utc)
                )
                await db.commit()
            report["worker_baseline"] = process_observation(worker.pid, root)
            for name, count, flat, cancel in profiles:
                expected, fingerprint = await asyncio.to_thread(
                    build_tree, root / name, count, flat=flat
                )
                async with maker() as db:
                    actor = await db.get(User, actor_id)
                    source = await scans.create_source(
                        db,
                        DatasetSourceCreate(
                            dataset_id=dataset_id,
                            location_id=location_id,
                            name=name,
                            relative_path=name,
                            node_id=node,
                            exclude_globs=["*.tmp"],
                        ),
                        actor,
                    )
                    run, _ = await scans.start_scan(db, source.id, actor)
                    run_id, job_id = run.id, run.async_job_id
                    task_id = (await db.get(AsyncJob, job_id)).celery_task_id
                    await db.commit()
                    dispatched_at = time.monotonic()
                    await dispatch(db, run_id)
                result, old_fence = await wait_attempt(
                    maker,
                    run_id,
                    task_id,
                    worker,
                    root,
                    metrics,
                    started=dispatched_at,
                    cancel_at=2000 if cancel else None,
                )
                result.update(
                    name=name,
                    fixture_files=count,
                    fixture_directories=sum(
                        x == "directory" for x in expected.values()
                    ),
                )
                if cancel:
                    async with maker() as db:
                        before = await db.scalar(
                            select(func.count())
                            .select_from(DatasetScanEntry)
                            .where(DatasetScanEntry.run_id == run_id)
                        )
                        if before != result["cancelled_checkpoint_rows"]:
                            raise AssertionError(
                                "Late Worker committed after cancellation"
                            )
                        actor = await db.get(User, actor_id)
                        run = await scans.resume_scan(db, run_id, actor)
                        new_task = (
                            await db.get(AsyncJob, run.async_job_id)
                        ).celery_task_id
                        await db.commit()
                        try:
                            await scans.fenced_context(db, run_id, task_id, old_fence)
                        except scans.LostScanLease:
                            await db.rollback()
                        else:
                            raise AssertionError(
                                "Old attempt remained writable after resume"
                            )
                        dispatched_at = time.monotonic()
                        await dispatch(db, run_id)
                    resumed, _ = await wait_attempt(
                        maker,
                        run_id,
                        new_task,
                        worker,
                        root,
                        metrics,
                        started=dispatched_at,
                    )
                    result["resume"] = resumed
                final = result.get("resume", result)
                expected_error = (
                    "directory_too_large"
                    if flat
                    else "scan_budget_exceeded"
                    if name == "scan-budget"
                    else None
                )
                if expected_error:
                    if (
                        final["status"] != "failed"
                        or final["complete"]
                        or final["error_code"] != expected_error
                    ):
                        raise AssertionError(
                            "Budget rejection did not match the production contract"
                        )
                else:
                    if final["status"] != "completed" or not final["complete"]:
                        raise AssertionError(
                            "Real directory inventory did not complete"
                        )
                    async with maker() as db:
                        rows = (
                            await db.execute(
                                select(
                                    DatasetScanEntry.relative_path,
                                    DatasetScanEntry.kind,
                                    DatasetScanEntry.state,
                                ).where(DatasetScanEntry.run_id == run_id)
                            )
                        ).all()
                        actual = {path: kind for path, kind, _ in rows if path}
                        if actual != expected or len(rows) != len(expected) + 1:
                            raise AssertionError(
                                "Inventory lost or duplicated fixture members"
                            )
                        skipped = sum(
                            path.endswith(".tmp")
                            for path, kind in expected.items()
                            if kind == "file"
                        )
                        if (
                            final["counts"]["file_count"] != count - skipped
                            or final["counts"]["skipped_count"] != skipped
                        ):
                            raise AssertionError(
                                "Selected and excluded files did not match"
                            )
                        if await db.scalar(
                            select(func.count())
                            .select_from(DatasetItem)
                            .where(DatasetItem.dataset_id == dataset_id)
                        ):
                            raise AssertionError(
                                "Metadata scan unexpectedly registered samples"
                            )
                    final["exact_inventory_verified"] = True
                if await asyncio.to_thread(tree_fingerprint, root / name) != (
                    expected,
                    fingerprint,
                ):
                    raise AssertionError("Worker modified fixture files")
                if any(
                    attempt["filesystem"]["regular_file_opens"]
                    or attempt["source_fds_after"]
                    for attempt in [result, result.get("resume", result)]
                ):
                    raise AssertionError(
                        "Metadata Worker read source content or retained descriptors"
                    )
                # Exercise the actual list handler with an observer that rejects any physical walk.
                from app.api.v1.dataset_sources import list_entries
                from app.services import filesystem_storage as fs

                queries = []

                def query_seen(*args):
                    queries.append(1)

                event.listen(engine.sync_engine, "before_cursor_execute", query_seen)
                original, original_open = fs.scan_directory, fs._open_beneath

                def forbidden(*args, **kwargs):
                    raise AssertionError("Indexed listing traversed physical storage")

                fs.scan_directory = forbidden
                fs._open_beneath = forbidden
                listed_at = time.monotonic()
                try:
                    async with maker() as db:
                        page = await list_entries(
                            run_id,
                            limit=25,
                            offset=0,
                            db=db,
                            actor=await db.get(User, actor_id),
                        )
                        if len(page["items"]) > 25:
                            raise AssertionError("Indexed page exceeded its limit")
                finally:
                    fs.scan_directory = original
                    fs._open_beneath = original_open
                    event.remove(
                        engine.sync_engine, "before_cursor_execute", query_seen
                    )
                result["indexed_page"] = {
                    "rows": len(page["items"]),
                    "sql_statements": len(queries),
                    "seconds": time.monotonic() - listed_at,
                    "physical_walks": 0,
                }
                report["profiles"].append(result)
                print(
                    json.dumps(
                        {
                            "profile": name,
                            "status": final["status"],
                            "wall_seconds": round(final["wall_seconds"], 3),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
        if report["tool_sha256"] != tool_digests():
            raise RuntimeError("Measurement tools changed during the run")
        return report
    finally:
        if worker and worker.poll() is None:
            worker.send_signal(signal.SIGTERM)
            try:
                await asyncio.to_thread(worker.wait, timeout=15)
            except subprocess.TimeoutExpired:
                worker.kill()
                await asyncio.to_thread(worker.wait, timeout=5)
        await engine.dispose()


def main():
    from benchmark_dataset_workbench import verified_environment

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--sizes",
        type=int,
        nargs="+",
        default=[1000, 10000, 40000],
        choices=[1000, 10000, 40000],
    )
    arguments = parser.parse_args()
    if arguments.output.exists():
        parser.error("Output already exists; preserve prior results")
    backend, _ = verified_environment()
    try:
        with tempfile.TemporaryDirectory(prefix="dataset-scan-benchmark-") as temporary:
            try:
                result = asyncio.run(run_benchmark(Path(temporary), arguments.sizes))
            except Exception:
                log = Path(temporary) / "worker.log"
                if log.exists():
                    with arguments.output.with_suffix(".worker.log").open(
                        "xb"
                    ) as stream:
                        stream.write(log.read_bytes()[-1048576:])
                raise
    finally:
        backend.s3.close()
    with arguments.output.open("x") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
