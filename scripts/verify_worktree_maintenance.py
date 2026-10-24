"""Explicit live acceptance: disposable PostgreSQL plus owned Redis/MinIO resources.

Run with apps/api/.venv/bin/python scripts/verify_worktree_maintenance.py.
The shared PostgreSQL database and its roles are never changed.
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))


async def probe_database():
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import create_async_engine

    runtime = create_async_engine(os.environ["DATABASE_URL"])
    owner = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    try:
        async with runtime.connect() as connection:
            assert await connection.scalar(text("SELECT current_user")) == "runtime"
            assert not await connection.scalar(
                text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            )
            for sql in (
                "CREATE TABLE forbidden_probe (id int)",
                "REFRESH MATERIALIZED VIEW CONCURRENTLY mv_user_perf_daily",
            ):
                try:
                    await connection.execute(text(sql))
                except DBAPIError:
                    await connection.rollback()
                else:
                    raise AssertionError(
                        "The ordinary runtime role unexpectedly has maintenance privileges"
                    )
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE public.audit_logs_y2001m01 PARTITION OF public.audit_logs "
                    "FOR VALUES FROM ('2001-01-01 00:00:00+00') TO ('2001-02-01 00:00:00+00')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO public.audit_logs_y2001m01 (id,action,created_at) VALUES "
                    "(2001001,'maintenance.archive.first','2001-01-02 00:00:00+00'),"
                    "(2001001,'maintenance.archive.second','2001-01-03 00:00:00+00')"
                )
            )
    finally:
        await runtime.dispose()
        await owner.dispose()


async def verify_archive(result):
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.db.models.async_job import AsyncJob
    from app.db.models.audit_archive_output import AuditArchiveOutput
    from app.services import audit_archive_storage

    engine = create_async_engine(os.environ["MIGRATION_DATABASE_URL"])
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            row = await db.scalar(
                select(AuditArchiveOutput).where(
                    AuditArchiveOutput.year == 2001,
                    AuditArchiveOutput.month == 1,
                    AuditArchiveOutput.state == "ready",
                )
            )
            assert row is not None and row.row_count == 2
            assert str(row.id) in result["output_ids"]
            job = await db.get(AsyncJob, row.job_id)
            assert job is not None and job.status == "completed"
            assert str(job.id) == result["job_id"]
            assert job.result["archived_partitions"] == result["archived_partitions"]
            assert (
                await db.scalar(
                    text("SELECT to_regclass('public.audit_logs_y2001m01')")
                )
                is None
            )
            page = await asyncio.to_thread(audit_archive_storage.read, row)
            assert page["total"] == 3
            assert [item["action"] for item in page["items"][:-1]] == [
                "maintenance.archive.first",
                "maintenance.archive.second",
            ]
            assert page["items"][-1] == audit_archive_storage.metadata(row)
    finally:
        await engine.dispose()


def probe_tasks():
    from app.workers.celery_app import celery_app

    asyncio.run(probe_database())
    try:
        for task in (
            "app.workers.cleanup.refresh_user_perf_mv",
            "app.workers.cleanup.refresh_audit_bi_mv",
        ):
            result = celery_app.send_task(task).get(timeout=60)
            assert result.get("refreshed") is True, result
        for task in (
            "app.workers.audit_partition.ensure_future_audit_partitions",
            "app.workers.prediction_partition.ensure_future_prediction_partitions",
        ):
            result = celery_app.send_task(task, kwargs={"months_ahead": 6}).get(
                timeout=60
            )
            assert result["created"], result
        result = celery_app.send_task(
            "app.workers.audit_partition.archive_old_audit_partitions"
        ).get(timeout=60)
        assert "audit_logs_y2001m01" in result["archived_partitions"], result
        asyncio.run(verify_archive(result))
        print(
            "PASS: runtime DDL denied; all 5 routed maintenance tasks completed, including real partition creation/drop and view refresh.",
            flush=True,
        )
    finally:
        celery_app.close()


def main():
    from app.config import settings
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from worktree_env import load_identity, topology
    from worktree_resources import LocalResources, docker
    from worktree_runtime import run_processes, parse_arguments

    container = None
    backend = None
    with tempfile.TemporaryDirectory(prefix="aap-maintenance-acceptance-") as temporary:
        try:
            name = f"aap-maintenance-test-{uuid4().hex[:12]}"
            container = docker(
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "org.aap.acceptance=maintenance",
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_USER=owner",
                "--env",
                "POSTGRES_PASSWORD=fixture",
                "postgres:16-alpine",
            )
            deadline = time.monotonic() + 30
            while subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "pg_isready",
                    "-h",
                    "127.0.0.1",
                    "-U",
                    "owner",
                ],
                capture_output=True,
            ).returncode:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Disposable PostgreSQL failed readiness")
                time.sleep(0.2)
            info = json.loads(docker("inspect", container))[0]
            assert info["Config"]["Labels"]["org.aap.acceptance"] == "maintenance"
            port = info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
            owner_url = f"postgresql+asyncpg://owner:fixture@127.0.0.1:{port}/postgres"

            async def create_runtime_role():
                engine = create_async_engine(owner_url)
                try:
                    async with engine.begin() as connection:
                        await connection.execute(
                            text(
                                "CREATE ROLE runtime LOGIN PASSWORD 'fixture' NOSUPERUSER NOCREATEDB NOCREATEROLE"
                            )
                        )
                finally:
                    await engine.dispose()

            asyncio.run(create_runtime_role())
            root = Path(temporary)
            backend = LocalResources(
                topology(root, load_identity(root, create=True), "test"),
                settings.model_copy(
                    update={
                        "database_url": owner_url.replace("owner:", "runtime:"),
                        "migration_database_url": owner_url,
                    }
                ),
                code_root=ROOT,
            )
            options = parse_arguments(
                [
                    "exec",
                    "--mode",
                    "test",
                    "--with-worker",
                    "--",
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--probe",
                ]
            )
            assert run_processes(backend, options) == 0
            assert not (root / ".worktree/test/run.json").exists()
            assert asyncio.run(backend.database())["sessions"] == 0
            print(
                "PASS: both workers and the command shut down; no database sessions remain.",
                flush=True,
            )
        finally:
            try:
                if backend and backend.touched:
                    backend.destroy(backend.resources["confirmation"])
            finally:
                if container:
                    docker("rm", "-f", "-v", container)


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        probe_tasks()
    else:
        main()
