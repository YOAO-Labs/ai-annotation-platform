"""Run the application Jobs worker and Beat on a verifier-owned broker/database."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import subprocess
import sys
import uuid

from celery import Celery
from celery.utils.nodenames import worker_direct
from sqlalchemy import text

from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_sessions_live import _verify_redis

RECOVERY_TASK = "app.workers.jobs_dispatch.recover_job_dispatches"
API_ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def jobs_child(command, *, environment, directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{name}.log").open("xb") as log:
        process = subprocess.Popen(
            [sys.executable, *command],
            cwd=API_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)


def jobs_secret(directory, name, value):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value)
    return str(path)


def jobs_broker_env(broker_url):
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "REDIS_URL": broker_url,
        "CELERY_BROKER_URL": broker_url,
    }


@asynccontextmanager
async def denied_first_publication(test_engine):
    """Refuse before entry, without inferring no-send from a sender exception."""
    _verify_postgres(test_engine.url)
    function = "yoao_jobs_v1.begin_dispatch_publication(uuid,uuid,text,bigint,uuid)"
    role = "yoao_jobs_publication_client"
    check = text(
        "SELECT has_function_privilege(CAST(:role AS name), CAST(:name AS text), 'EXECUTE')"
    )
    arguments = {"role": role, "name": function}
    async with test_engine.begin() as db:
        assert await db.scalar(check, arguments) is True
        await db.exec_driver_sql(f"REVOKE EXECUTE ON FUNCTION {function} FROM {role}")
        assert await db.scalar(check, arguments) is False
    try:
        yield
    finally:
        async with test_engine.begin() as db:
            await db.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {function} TO {role}")


@asynccontextmanager
async def jobs_recovery_worker(*, directory, dispatch_url, publication_url):
    broker_url = _verify_redis().render_as_string(hide_password=False)
    environment = jobs_broker_env(broker_url)
    worker_environment = {
        **environment,
        "JOBS_DISPATCH_DATABASE_URL_FILE": jobs_secret(
            directory, "dispatch-dsn", dispatch_url
        ),
        "JOBS_PUBLICATION_DATABASE_URL_FILE": jobs_secret(
            directory, "publication-dsn", publication_url
        ),
    }
    async with jobs_child(
        ["-m", "app.workers.jobs_dispatch"],
        environment=worker_environment,
        directory=directory,
        name="credentials",
    ) as preflight:
        assert await asyncio.to_thread(preflight.wait, timeout=30) == 0, (
            "Jobs recovery credential preflight failed"
        )
    node = f"jobs-recovery-{uuid.uuid4().hex}@local"
    sender = Celery("jobs_recovery_readiness", broker=broker_url)
    try:
        async with jobs_child(
            [
                "-m",
                "celery",
                "-A",
                "app.workers.celery_app",
                "worker",
                "--pool=solo",
                "--concurrency=1",
                "--prefetch-multiplier=1",
                "--without-mingle",
                "--without-gossip",
                "--loglevel=INFO",
                "--queues=jobs.recovery",
                f"--hostname={node}",
            ],
            environment=worker_environment,
            directory=directory,
            name="worker",
        ) as worker:
            async with asyncio.timeout(60):
                while True:
                    assert worker.poll() is None, "Jobs recovery worker exited"
                    inspector = sender.control.inspect(destination=[node], timeout=1)
                    registered = await asyncio.to_thread(inspector.registered) or {}
                    if RECOVERY_TASK not in registered.get(node, ()):
                        continue
                    active = await asyncio.to_thread(inspector.active_queues) or {}
                    queues = {queue["name"] for queue in active.get(node, ())}
                    assert queues in (
                        {"jobs.recovery"},
                        {"jobs.recovery", worker_direct(node).name},
                    )
                    break
            beat_command = (
                "from app.workers.celery_app import celery_app; "
                "key = 'recover-job-dispatches'; "
                "celery_app.conf.beat_schedule = {key: celery_app.conf.beat_schedule[key]}; "
                "celery_app.start(['beat', '--loglevel=INFO', "
                f"'--schedule={directory / 'schedule'}'])"
            )
            async with jobs_child(
                ["-c", beat_command],
                environment=environment,
                directory=directory,
                name="beat",
            ) as beat:
                yield worker, beat
    finally:
        sender.close()
