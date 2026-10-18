"""A real Celery consumer executes creation using only the scoped LOGIN role."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from celery import Celery
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.dataset import DatasetService
from app.services.labeling_tasks import LabelingTaskService
from app.services.task_creation_jobs import TaskCreationJobs
from app.services.task_creation_contract import TaskCreationFailure
from tests.test_task_creation_handoff import (
    _append_target,
    _cleanup,
    _seed,
    pytestmark as pytestmark,
)


@pytest.mark.parametrize("initial_status", ["pending", "running", "completed"])
async def test_old_unscoped_creation_is_terminalized_without_adopting_payload_scope(
    test_engine, monkeypatch, initial_status
):
    from app.config import settings
    from app.db.models.async_job import AsyncJob
    from app.workers.create_tasks import _run_create_tasks

    _append_target()
    fixture = await _seed(test_engine, item_count=1)
    operation_id = uuid4()
    retained_result = {"created_tasks": 1, "total": 2}
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        async with maker.begin() as owner:
            # Represents a pre-migration SET NULL row, not a new accepted operation.
            owner.add(
                AsyncJob(
                    id=operation_id,
                    kind="create_tasks",
                    project_id=None,
                    user_id=fixture["actor_id"],
                    status=initial_status,
                    result=retained_result,
                    payload={
                        "schema": "aap-create-tasks/v1",
                        "operation_id": str(operation_id),
                        "actor_id": str(fixture["actor_id"]),
                        "dataset_id": str(fixture["dataset_id"]),
                        "project_id": str(fixture["project_id"]),
                        "association_id": str(fixture["association_id"]),
                        "mode": "link",
                    },
                )
            )
        monkeypatch.setattr(
            settings, "database_url", os.environ["AAP_MODULE_DATA_APPEND_URL"]
        )
        for _ in range(2):
            with pytest.raises(TaskCreationFailure) as rejected:
                await _run_create_tasks(
                    job_id=str(operation_id), celery_task_id=str(uuid4())
                )
            assert rejected.value.code == "job_scope_mismatch"
        async with maker() as owner:
            job = await owner.get(AsyncJob, operation_id)
            assert job.status == (
                "completed" if initial_status == "completed" else "failed"
            )
            assert job.project_id is job.retained_project_id is None
            assert job.result == retained_result
            if initial_status != "completed":
                assert job.error_message == "task_creation:job_scope_mismatch"
                assert job.completed_at is not None
            assert (
                await owner.scalar(
                    text("SELECT count(*) FROM public.tasks WHERE project_id=:id"),
                    {"id": fixture["project_id"]},
                )
                == 0
            )
    finally:
        await _cleanup(test_engine, fixture, [operation_id])


async def test_real_celery_creation_uses_public_capabilities_and_replays_terminal_job(
    test_engine,
):
    assert os.environ.get("AAP_WORKTREE_MODE") == "test"
    _append_target()
    role_dsn = os.environ["AAP_MODULE_DATA_APPEND_URL"]
    broker = os.environ["CELERY_BROKER_URL"]
    engine = create_async_engine(role_dsn, hide_parameters=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    fixture = await _seed(test_engine, item_count=5)
    operation_id, cancelled_id, failed_id = uuid4(), uuid4(), uuid4()
    worker = None
    sender = Celery("creation-acceptance", broker=broker, backend=broker)
    result = duplicate = None
    try:
        # Exercise association INSERT through the public client, not owner SQL.
        async with test_engine.begin() as owner:
            await owner.execute(
                text("DELETE FROM public.project_datasets WHERE id = :id"),
                {"id": fixture["association_id"]},
            )
        async with maker.begin() as db:
            association = await DatasetService(db).associate_project(
                fixture["dataset_id"],
                fixture["project_id"],
                actor_id=fixture["actor_id"],
            )
            assert not association.existing
            fixture["association_id"] = association.link_id
            jobs = TaskCreationJobs(db)
            labeling = LabelingTaskService(db)
            for op in (operation_id, cancelled_id, failed_id):
                await jobs.create(
                    operation_id=op,
                    actor_id=fixture["actor_id"],
                    dataset_id=fixture["dataset_id"],
                    project_id=fixture["project_id"],
                    association_id=association.link_id,
                )
                await labeling.accept_dataset_task_creation(
                    operation_id=op,
                    actor_id=fixture["actor_id"],
                    dataset_id=fixture["dataset_id"],
                    project_id=fixture["project_id"],
                    association_id=association.link_id,
                    job_id=op,
                    mode="generic",
                    chunk_size=2,
                )
            found = await jobs.find(
                actor_id=fixture["actor_id"],
                dataset_id=fixture["dataset_id"],
                project_id=fixture["project_id"],
                association_id=association.link_id,
            )
            assert found.job_id in {operation_id, cancelled_id, failed_id}
            assert (
                await jobs.cancel(fixture["actor_id"], cancelled_id)
            ).status == "cancelled"
            assert (
                await jobs.fail(failed_id, failed_id, "source_changed")
            ).status == "failed"

        async with test_engine.begin() as owner:
            await owner.execute(
                text("UPDATE public.users SET is_active = false WHERE id = :id"),
                {"id": fixture["actor_id"]},
            )

        queue = f"creation_acceptance_{uuid4().hex}"
        env = {
            **os.environ,
            "DATABASE_URL": role_dsn,
            "MIGRATION_DATABASE_URL": "",
            "TEST_DATABASE_URL": "",
        }
        with tempfile.TemporaryFile() as output:
            worker = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.workers.celery_app",
                "worker",
                "--pool=solo",
                "--concurrency=1",
                f"--queues={queue}",
                f"--hostname={queue}@%h",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
                "--loglevel=WARNING",
                cwd=Path(__file__).parents[1],
                env=env,
                stdout=output,
                stderr=output,
            )
            execution_id = str(uuid4())
            result = await asyncio.to_thread(
                sender.send_task,
                "app.workers.create_tasks.run_create_tasks",
                args=[str(operation_id)],
                queue=queue,
                task_id=execution_id,
            )
            await asyncio.to_thread(result.get, timeout=30)
            duplicate = await asyncio.to_thread(
                sender.send_task,
                "app.workers.create_tasks.run_create_tasks",
                args=[str(operation_id)],
                queue=queue,
            )
            await asyncio.to_thread(duplicate.get, timeout=15)
            assert worker.returncode is None
            async with maker() as db:
                operation = await LabelingTaskService(db).read_dataset_task_creation(
                    operation_id
                )
                assert operation.state == "completed"
                assert operation.created == operation.processed == operation.total == 5
                assert operation.next_chunk == 3
                assert (
                    await TaskCreationJobs(db).finish(operation_id, operation_id)
                ).result == {
                    "created_tasks": 5,
                    "total": 5,
                }
            async with test_engine.connect() as owner:
                row = (
                    await owner.execute(
                        text(
                            "SELECT status, celery_task_id FROM public.async_jobs WHERE id = :id"
                        ),
                        {"id": operation_id},
                    )
                ).one()
                assert row.status == "completed" and row.celery_task_id == execution_id
                assert (
                    await owner.scalar(
                        text(
                            "SELECT count(*) FROM public.tasks WHERE project_id = :id"
                        ),
                        {"id": fixture["project_id"]},
                    )
                    == 5
                )
    finally:
        if worker is not None and worker.returncode is None:
            worker.terminate()
            try:
                await asyncio.wait_for(worker.wait(), 10)
            except TimeoutError:
                worker.kill()
                await worker.wait()
        for delivery in (result, duplicate):
            if delivery is not None:
                await asyncio.to_thread(delivery.forget)
        sender.close()
        await engine.dispose()
        await _cleanup(test_engine, fixture, [operation_id, cancelled_id, failed_id])
