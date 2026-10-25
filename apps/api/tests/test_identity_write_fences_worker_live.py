"""A fresh owned Celery consumer executes account-fenced cross-frame jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import uuid

import pytest
from celery import Celery
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.identity_account_lock_data import IdentityAccountLockData
from tests.factory import create_user
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_cross_frame_jobs import _create_worker_job
from tests.test_identity_admission_live import ROOT
from tests.test_identity_sessions_live import _verify_redis
from tests.test_module_data_roles import _role_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_WRITE_FENCES_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned account-fence verifier",
    ),
]


async def test_fresh_celery_worker_uses_account_facts_and_current_actor_status(
    test_engine, tmp_path, record_property
):
    _verify_postgres(test_engine.url)
    _verify_redis()
    runtime_url = _role_url("AAP_MODULE_DATA_LEGACY_RUNTIME_URL", "legacy_runtime")
    runtime = make_url(runtime_url)
    assert (runtime.host, runtime.port, runtime.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = None
    jobs, project_ids, targets = [], [], []
    worker = None
    queue = f"account_fences_{uuid.uuid4().hex}"
    broker = os.environ["REDIS_URL"]
    sender = Celery("account-fences-acceptance", broker=broker, backend=broker)
    deliveries = []
    proof, trace = tmp_path / "worker.json", tmp_path / "capabilities.jsonl"
    log = (tmp_path / "worker.log").open("wb")
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db, "super_admin", f"{queue}@worker.test", "Account-fenced worker"
            )
            actor_id = actor.id
            for _ in range(3):
                job_id, tasks = await _create_worker_job(
                    db, owner_id=actor.id, target_statuses=["in_progress"]
                )
                jobs.append(job_id)
                targets.append(tasks[1].id)
                project_ids.append(tasks[0].project_id)
        runtime_engine = create_async_engine(runtime_url, hide_parameters=True)
        try:
            async with async_sessionmaker(runtime_engine)() as runtime_db:
                profile = await runtime_db.scalar(
                    text("SELECT aap_identity_v1.read_actor_context(:id, :role)"),
                    {"id": actor_id, "role": "super_admin"},
                )
                if isinstance(profile, str):
                    profile = json.loads(profile)
                assert profile["ok"] is True and profile["data"]["id"] == str(actor_id)
                for mode in ("share", "update", "key_share"):
                    facts = await IdentityAccountLockData(runtime_db).lock_accounts(
                        [actor_id], mode=mode
                    )
                    assert facts[actor_id].role == "super_admin"
                    await runtime_db.rollback()
        finally:
            await runtime_engine.dispose()
        environment = {
            **os.environ,
            "DATABASE_URL": runtime_url,
            "MIGRATION_DATABASE_URL": "",
            "TEST_DATABASE_URL": "",
            "REDIS_URL": broker,
            "CELERY_BROKER_URL": broker,
            "CELERY_RESULT_BACKEND": broker,
            "CELERY_BROKER_READ_URL": broker,
            "CELERY_BROKER_WRITE_URL": broker,
            "SECRET_KEY": secrets.token_urlsafe(32),
            "SENTRY_DSN": "",
            "AAP_ACCOUNT_FENCE_WORKER_PROOF": str(proof),
            "AAP_ACCOUNT_FENCE_WORKER_TRACE": str(trace),
        }
        task_name = "app.workers.cross_frame_job.run_cross_frame_job"
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "apps/api/tests/fixtures/identity_account_lock_worker_peer.py"),
            "worker",
            "--pool=solo",
            "--concurrency=1",
            f"--queues={queue}",
            f"--hostname={queue}@owned",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
            "--loglevel=WARNING",
            cwd=ROOT / "apps/api",
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 35
        while not proof.exists():
            assert worker.returncode is None, "Fresh account-fence worker exited"
            assert time.monotonic() < deadline, "Fresh worker did not become ready"
            await asyncio.sleep(0.1)
        loaded = json.loads(proof.read_text())
        assert loaded["pid"] == worker.pid and loaded["task"] == task_name
        assert all(
            hashlib.sha256((ROOT / "apps/api" / name).read_bytes()).hexdigest()
            == digest
            for name, digest in loaded["source_hashes"].items()
        )

        for index, job_id in enumerate(jobs):
            if index == 2:
                async with maker.begin() as db:
                    actor = await db.get(User, actor_id)
                    actor.is_active = False
            delivery = await asyncio.to_thread(
                sender.send_task, task_name, args=[str(job_id)], queue=queue
            )
            deliveries.append(delivery)
            if index == 2:
                with pytest.raises(ValueError, match="cross-frame job actor not found"):
                    await asyncio.to_thread(delivery.get, timeout=30)
            else:
                await asyncio.to_thread(delivery.get, timeout=30)
            async with maker() as db:
                job = await db.get(AsyncJob, job_id)
                assert job.status == ("failed" if index == 2 else "completed")
                assert await db.scalar(
                    select(func.count(Annotation.id)).where(
                        Annotation.task_id == targets[index]
                    )
                ) == (0 if index == 2 else 1)
                if index < 2:
                    assert job.result["success_count"] == 1
                    assert job.result["failed_count"] == 0
        calls = [
            json.loads(line)["capability"] for line in trace.read_text().splitlines()
        ]
        # Each dispatch checks its actor; each successful job also checks the
        # source and target scopes through Identity instead of User ORM reads.
        assert calls.count("read_actor_facts") == 7
        assert calls.count("lock_accounts_share") >= 2
        record_property(
            "fresh_account_fences_worker",
            "current-source-hashes,owned-broker,three-dispatches,separate-event-loops,current-inactive-actor",
        )
    finally:
        if worker is not None and worker.returncode is None:
            worker.terminate()
            try:
                await asyncio.wait_for(worker.wait(), timeout=15)
            except TimeoutError:
                worker.kill()
                await worker.wait()
        log.close()
        _verify_redis()
        for delivery in deliveries:
            await asyncio.to_thread(delivery.forget)
        with sender.connection_for_write() as connection:
            connection.default_channel.queue_delete(queue)
        sender.close()
        if actor_id is not None:
            async with maker.begin() as db:
                await db.execute(delete(AsyncJob).where(AsyncJob.id.in_(jobs)))
                await db.execute(
                    delete(Annotation).where(Annotation.project_id.in_(project_ids))
                )
                await db.execute(delete(Task).where(Task.project_id.in_(project_ids)))
                await db.execute(delete(Project).where(Project.id.in_(project_ids)))
                await db.execute(delete(Dataset).where(Dataset.created_by == actor_id))
                await db.execute(delete(User).where(User.id == actor_id))
