"""Measure indexed dataset reads in an owned, disposable worktree environment.

Run seed, then measure through ``dev-worktree.mjs exec --mode e2e``. The seed
requires an empty business database and never resets existing data. Forty real
directory images per tier exercise preview storage; all other rows are explicitly
unverified synthetic indexes. This is not a source-scan or production benchmark.
The launcher owns cleanup of the whole namespace, including the source files.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

from worktree_env import WorktreeError, atomic_json, load_identity, topology  # noqa: E402
from worktree_resources import LocalResources, migration_graph  # noqa: E402
from worktree_runtime import (  # noqa: E402
    require_owner,
    validate_inventory,
    validate_revisions,
)


def require(value, message):
    if not value:
        raise WorktreeError(message)


def verified_environment():
    from app.config import settings
    from sqlalchemy.engine import make_url

    require(__debug__, "Benchmark requires Python assertions")
    require(os.environ.get("AAP_WORKTREE_MODE") == "e2e", "Owned e2e mode required")
    resources = topology(ROOT, load_identity(ROOT), "e2e")
    backend = LocalResources(resources, settings, code_root=ROOT)
    try:
        observed = backend.inspect()
        validate_inventory(resources, observed)
        for name, row in (
            (resources["database"], observed["database"]),
            (resources["redis_name"], observed["redis"]),
            *observed["buckets"].items(),
        ):
            require(require_owner(name, row, resources["owner"]), "Resource not ready")
        require(observed["redis"]["running"], "Owned Redis is stopped")
        expected = backend.environment()
        for key in (
            "DATABASE_URL",
            "MIGRATION_DATABASE_URL",
            "TEST_DATABASE_URL",
            "REDIS_URL",
            "CELERY_BROKER_URL",
            "CELERY_BROKER_READ_URL",
            "CELERY_BROKER_WRITE_URL",
            "CELERY_RESULT_BACKEND",
            "MINIO_ENDPOINT",
            "TMPDIR",
            *resources["buckets"],
        ):
            require(os.environ.get(key) == expected[key], "Environment target mismatch")
        require(
            make_url(settings.database_url).database == resources["database"],
            "Application database mismatch",
        )
        validate_revisions(
            *migration_graph(ROOT), asyncio.run(backend.revisions()), skip=True
        )
        return backend, resources
    except BaseException:
        backend.s3.close()
        raise


def write_report(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)


def summary(values):
    ordered = sorted(values)
    return {
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
    }


async def seed(resources, path):
    from PIL import Image
    from pytest import MonkeyPatch
    from sqlalchemy import func, insert, select

    from app.db.base import async_session, engine
    from app.db.models.annotation import Annotation
    from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
    from app.db.models.dataset_source import DatasetSourcePreview
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.user import User
    from tests.factory import create_project, create_user
    from tests.test_dataset_content import create_content_world
    from tests.test_dataset_source_changes import accept_all_files, rescan

    nonce = uuid.uuid4().hex[:10]
    result = {"schema_version": 1, "owner": resources["owner"], "tiers": []}
    try:
        async with async_session() as db:
            for model in (User, Dataset, Project, Task):
                require(
                    await db.scalar(select(func.count()).select_from(model)) == 0,
                    "Seed requires an empty business database; it never resets data",
                )
            actor = await create_user(
                db, "super_admin", f"e08-{nonce}@example.test", "规模测量"
            )
            await db.commit()
            result["user_id"], result["email"] = str(actor.id), actor.email
            for size in (1000, 10000):
                directory = (
                    Path(os.environ["TMPDIR"]) / f"dataset-benchmark-{nonce}-{size}"
                )
                directory.mkdir()
                with MonkeyPatch.context() as patch:
                    world = await create_content_world(db, actor, directory, patch)
                    root, _, _, dataset, _, source, _, _ = world
                    dataset.name = f"规模测量 · {size:,} 条索引"
                    source.cache_policy = "preview"
                    for index in range(1, 40):
                        Image.new("RGB", (48, 32), (index * 6, index * 3, index)).save(
                            root / f"real-{index:03}.png", format="PNG"
                        )
                    await db.commit()
                    world = await rescan(db, world)
                    await accept_all_files(db, world)
                real = list(
                    await db.scalars(
                        select(DatasetItem)
                        .where(DatasetItem.dataset_id == dataset.id)
                        .order_by(DatasetItem.file_name)
                    )
                )
                require(len(real) == 40, "Real reference registration count mismatch")
                now = datetime.now(timezone.utc)
                for index, item in enumerate(real):
                    item.created_at = now + timedelta(seconds=index)
                synthetic = [
                    {
                        "id": uuid.uuid4(),
                        "dataset_id": dataset.id,
                        "file_name": f"synthetic-{index:05}.png",
                        "file_path": f"benchmark-unverified/{nonce}/{size}/{index}.png",
                        "file_type": "image",
                        "width": 48,
                        "height": 32,
                        "metadata_": {"benchmark_synthetic": True},
                        "created_at": now - timedelta(days=1, seconds=index),
                    }
                    for index in range(size - 40)
                ]
                for offset in range(0, len(synthetic), 500):
                    await db.execute(
                        insert(DatasetItem), synthetic[offset : offset + 500]
                    )
                dataset.file_count = size
                await db.flush()
                projects = []
                item_ids = [item.id for item in real] + [row["id"] for row in synthetic]
                for project_index in range(2):
                    project = await create_project(
                        db,
                        owner_id=actor.id,
                        name=f"索引 {size:,} · 项目 {project_index + 1}",
                    )
                    db.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
                    tasks = [
                        {
                            "id": uuid.uuid4(),
                            "project_id": project.id,
                            "dataset_item_id": item_id,
                            "display_id": f"BM-{nonce}-{size}-{project_index}-{index}",
                            "file_name": f"index-{index}.png",
                            "file_type": "image",
                            "file_path": f"benchmark-task/{nonce}/{size}/{index}.png",
                            "status": "pending",
                            "is_labeled": index % 10 == 0,
                        }
                        for index, item_id in enumerate(item_ids)
                    ]
                    for offset in range(0, len(tasks), 500):
                        await db.execute(insert(Task), tasks[offset : offset + 500])
                    annotations = [
                        {
                            "task_id": task["id"],
                            "project_id": project.id,
                            "user_id": actor.id,
                            "source": "manual",
                            "annotation_type": "bbox",
                            "tool_unit_id": "bbox",
                            "class_name": "car" if project_index == 0 else "person",
                            "geometry": {
                                "type": "bbox",
                                "x": 0.1,
                                "y": 0.1,
                                "w": 0.2,
                                "h": 0.2,
                            },
                        }
                        for index, task in enumerate(tasks)
                        if index % 10 == 0
                    ]
                    for offset in range(0, len(annotations), 500):
                        await db.execute(
                            insert(Annotation), annotations[offset : offset + 500]
                        )
                    projects.append(
                        {
                            "id": str(project.id),
                            "tasks": size,
                            "annotations": len(annotations),
                        }
                    )
                await db.commit()
                previews = list(
                    await db.scalars(
                        select(DatasetSourcePreview).where(
                            DatasetSourcePreview.source_id == source.id
                        )
                    )
                )
                require(
                    len(previews) == 40
                    and all(row.state == "ready" for row in previews),
                    "Real previews must all be ready",
                )
                source_bytes = [file.stat().st_size for file in root.iterdir()]
                result["tiers"].append(
                    {
                        "size": size,
                        "dataset_id": str(dataset.id),
                        "source_id": str(source.id),
                        "real_references": 40,
                        "unverified_synthetic_indexes": size - 40,
                        "projects": projects,
                        "preview_ids": [str(item.id) for item in reversed(real)],
                        "preview_sha256": sorted(row.sha256 for row in previews),
                        "source_bytes": {
                            "mean": statistics.mean(source_bytes),
                            "max": max(source_bytes),
                        },
                        "preview_bytes": {
                            "mean": statistics.mean(row.size_bytes for row in previews),
                            "max": max(row.size_bytes for row in previews),
                        },
                    }
                )
                write_report(path, result)
                print(
                    json.dumps({"seeded_index_rows": size, "real_previews": 40}),
                    flush=True,
                )
    finally:
        await engine.dispose()


async def measure(resources, fixture_path, output, rounds):
    import httpx
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.security import create_access_token
    from app.db.base import engine
    from app.main import app
    from app.services.storage import storage_service

    logging.getLogger("httpx").setLevel(logging.WARNING)
    fixture = json.loads(fixture_path.read_text())
    require(fixture["owner"] == resources["owner"], "Fixture owner mismatch")
    require(
        sorted(tier["size"] for tier in fixture["tiers"]) == [1000, 10000],
        "Fixture is incomplete; both seeded tiers are required",
    )

    # Runtime roles do not own migrated tables: their ANALYZE may only warn and
    # skip. Prepare all join statistics with the already verified namespace owner.
    owner_engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    try:
        async with owner_engine.begin() as db:
            identity = (
                (
                    await db.execute(
                        text(
                            "SELECT current_database() AS name, shobj_description(oid, 'pg_database') AS owner "
                            "FROM pg_database WHERE datname = current_database()"
                        )
                    )
                )
                .mappings()
                .one()
            )
            require(
                identity["name"] == resources["database"]
                and identity["owner"] == resources["owner"],
                "Planner preparation database owner mismatch",
            )
            await db.execute(text("ANALYZE"))
            planner_statistics = dict(
                (
                    await db.execute(
                        text(
                            "SELECT relname, reltuples::bigint FROM pg_class "
                            "WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p') "
                            "ORDER BY relname"
                        )
                    )
                ).all()
            )
            for table in (
                "datasets",
                "dataset_items",
                "dataset_sources",
                "dataset_assets",
                "dataset_references",
                "storage_locations",
                "tasks",
            ):
                require(
                    planner_statistics.get(table, -1) >= 0,
                    "Missing fixture planner statistics",
                )
    finally:
        await owner_engine.dispose()
    active = None

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        if active is not None:
            context._benchmark_started = time.perf_counter()
            active["sql_count"] += 1

    def after_sql(conn, cursor, statement, parameters, context, executemany):
        if active is not None:
            active["sql_ms"] += (
                time.perf_counter() - context._benchmark_started
            ) * 1000

    def provider_call(model, **kwargs):
        if active is not None:
            active["storage_calls"][model.name] += 1

    async with engine.connect() as db:
        database_version = await db.scalar(text("select version()"))
    result = {
        "schema_version": 1,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "logical_cpus": os.cpu_count(),
            "database": database_version,
            "transport": "in-process ASGI + real PostgreSQL/Redis/MinIO on loopback",
        },
        "method": {
            "concurrency": 1,
            "warmup": 2,
            "samples_per_case": rounds,
            "page_size": 20,
            "p95": "nearest rank",
            "cache": "warm DB after ANALYZE; application caches unchanged",
            "planner_statistics": planner_statistics,
            "latency_slo": None,
            "qualification": "local synthetic index baseline",
        },
        "fixture": {key: value for key, value in fixture.items() if key != "owner"},
        "cases": [],
    }
    event.listen(engine.sync_engine, "before_cursor_execute", before_sql)
    event.listen(engine.sync_engine, "after_cursor_execute", after_sql)
    storage_service.client.meta.events.register("before-call.s3", provider_call)
    token = create_access_token(fixture["user_id"], "super_admin")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://benchmark",
            headers={"Authorization": "Bearer " + token},
        ) as client:
            cases = [
                ("dataset-list", "GET", "/api/v1/datasets", {"limit": 20}, None, None)
            ]
            for tier in fixture["tiers"]:
                base = f"/api/v1/datasets/{tier['dataset_id']}"
                cases.extend(
                    [
                        (
                            "first-page",
                            "GET",
                            base + "/items",
                            {"limit": 20},
                            None,
                            tier,
                        ),
                        (
                            "second-page",
                            "GET",
                            base + "/items",
                            {"limit": 20, "offset": 20},
                            None,
                            tier,
                        ),
                        (
                            "last-page",
                            "GET",
                            base + "/items",
                            {"limit": 20, "offset": tier["size"] - 20},
                            None,
                            tier,
                        ),
                        (
                            "combined-filter",
                            "GET",
                            base + "/items",
                            {
                                "limit": 20,
                                "search": "synthetic-",
                                "extension": "png",
                                "dimension_state": "known",
                                "min_width": 48,
                            },
                            None,
                            tier,
                        ),
                        (
                            "source-filter",
                            "GET",
                            base + "/items",
                            {"limit": 20, "source_id": tier["source_id"]},
                            None,
                            tier,
                        ),
                        ("workbench", "GET", base + "/workbench", {}, None, tier),
                        (
                            "inspector",
                            "GET",
                            base + "/items/" + tier["preview_ids"][0],
                            {},
                            None,
                            tier,
                        ),
                    ]
                )
                for project in tier["projects"]:
                    cases.append(
                        (
                            "project-summary",
                            "POST",
                            f"/api/v1/projects/{project['id']}/data-manager/summary",
                            {},
                            {
                                "filter_json": {
                                    "field": "dataset.dataset_id",
                                    "op": "eq",
                                    "value": tier["dataset_id"],
                                }
                            },
                            tier,
                        )
                    )
            for name, method, route, params, body, tier in cases:
                samples = []
                for index in range(rounds + 2):
                    active = {"sql_count": 0, "sql_ms": 0, "storage_calls": Counter()}
                    started = time.perf_counter()
                    response = await client.request(
                        method, route, params=params, json=body
                    )
                    active["elapsed_ms"] = (time.perf_counter() - started) * 1000
                    sample, active = active, None
                    require(
                        response.status_code == 200,
                        f"{name} returned {response.status_code}",
                    )
                    payload = response.json()
                    sample.update(
                        bytes=len(response.content), status=response.status_code
                    )
                    if "items" in payload:
                        sample.update(
                            rows=len(payload["items"]), total=payload["total"]
                        )
                        require(sample["rows"] <= 20, "Unbounded page response")
                    if name.endswith("page"):
                        require(payload["total"] == tier["size"], "Page total mismatch")
                    if name in {"first-page", "second-page"}:
                        offset = params.get("offset", 0)
                        require(
                            [row["id"] for row in payload["items"]]
                            == tier["preview_ids"][offset : offset + 20],
                            "Page ordering or real-preview fixture mismatch",
                        )
                    if name == "combined-filter":
                        require(
                            payload["total"] == tier["size"] - 40,
                            "Combined filter mismatch",
                        )
                        require(
                            all(
                                row["availability"]["state"] == "unverified"
                                for row in payload["items"]
                            ),
                            "Synthetic indexes must not be marked verified",
                        )
                    if name == "source-filter":
                        require(payload["total"] == 40, "Source filter mismatch")
                    if name == "project-summary":
                        require(
                            payload["scope"]["matched_task_total"] == tier["size"],
                            "Project task count mismatch",
                        )
                        require(
                            payload["annotations"]["total"] == tier["size"] // 10,
                            "Annotation count mismatch",
                        )
                    require(
                        not sample["storage_calls"],
                        "Metadata read unexpectedly called object storage",
                    )
                    if index >= 2:
                        samples.append(sample)
                entry = {
                    "name": name,
                    "index_rows": tier["size"] if tier else None,
                    "method": method,
                    "route": route,
                    "params": params,
                    "samples": samples,
                    **{
                        key: summary([row[key] for row in samples])
                        for key in ("elapsed_ms", "sql_ms", "sql_count", "bytes")
                    },
                }
                result["cases"].append(entry)
                write_report(output, result)
                print(
                    json.dumps(
                        {
                            "case": name,
                            "index_rows": entry["index_rows"],
                            "elapsed_ms": entry["elapsed_ms"],
                            "sql_count": entry["sql_count"],
                        }
                    ),
                    flush=True,
                )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_sql)
        event.remove(engine.sync_engine, "after_cursor_execute", after_sql)
        storage_service.client.meta.events.unregister("before-call.s3", provider_call)
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("seed", "measure"))
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rounds", type=int, default=10, choices=range(5, 51))
    args = parser.parse_args()
    require(
        args.phase == "seed" or args.output is not None, "Measure requires --output"
    )
    backend, resources = verified_environment()
    try:
        if args.phase == "seed":
            asyncio.run(seed(resources, args.fixture))
        else:
            asyncio.run(measure(resources, args.fixture, args.output, args.rounds))
    finally:
        backend.s3.close()


if __name__ == "__main__":
    main()
