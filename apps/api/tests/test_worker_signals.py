"""Direct regression tests for the Celery signal fallback in ``app.workers.signals``.

The signal fallback covers the path nothing else exercises: a worker process
crash or a revoked task that never reached the job's own ``mark_*`` call.  These
tests pin the contract the P0 ledger recorded as a protection gap (C6):

* a crash flips ``async_jobs`` to ``failed`` and writes the terminal
  notification exactly once;
* an already-terminal job is never overwritten by a late signal;
* unknown task ids and non-whitelisted kinds stay silent instead of crashing;
* the domain ledger keeps its specific terminal value (``partial``,
  ``rollback_failed``) instead of being flattened by a generic handler.

The signal functions open their own engine on ``settings.database_url`` and use
``asyncio.run``.  A SAVEPOINT-scoped fixture session cannot prove cross-connection
visibility, so this module seeds *committed* rows through a dedicated engine and
points the signal module at the same disposable test database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.async_job import AsyncJob, AsyncJobStatus
from app.db.models.mask_format_import import MaskFormatImport
from app.db.models.mask_repair_batch import MaskRepairBatch
from app.db.models.notification import Notification
from app.db.models.project import Project
from app.db.models.user import User
from app.workers import signals
from tests.factory import create_project, create_user

pytestmark = pytest.mark.asyncio

_HEX64 = "a" * 64


class _SignalWorld:
    """Committed rows for signal fallback tests, with deterministic cleanup."""

    def __init__(self, engine):
        self.engine = engine
        self.maker = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        self.user_ids: set[uuid.UUID] = set()
        self.project_ids: set[uuid.UUID] = set()
        self.job_ids: set[uuid.UUID] = set()
        self.repair_ids: set[uuid.UUID] = set()
        self.format_ids: set[uuid.UUID] = set()

    async def user(self, role: str = "employee"):
        async with self.maker() as db:
            created = await create_user(
                db, role, f"signal-{uuid.uuid4()}@test.local", "Signal"
            )
            self.user_ids.add(created.id)
            await db.commit()
            return created.id

    async def project(self, owner_id: uuid.UUID):
        async with self.maker() as db:
            created = await create_project(db, owner_id=owner_id, name="Signal")
            self.project_ids.add(created.id)
            await db.commit()
            return created.id

    async def job(
        self,
        *,
        kind: str,
        user_id: uuid.UUID,
        project_id: uuid.UUID | None = None,
        status: str = AsyncJobStatus.RUNNING.value,
        celery_task_id: str | None = None,
        result: dict | None = None,
    ) -> tuple[uuid.UUID, str]:
        task_id = celery_task_id or f"celery-{uuid.uuid4()}"
        async with self.maker() as db:
            created = AsyncJob(
                kind=kind,
                user_id=user_id,
                project_id=project_id,
                status=status,
                payload={},
                result=result or {},
                celery_task_id=task_id,
            )
            db.add(created)
            await db.commit()
            self.job_ids.add(created.id)
            return created.id, task_id

    async def mask_repair(
        self,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        async_job_id: uuid.UUID,
        rollback: bool = False,
        status: str = "running",
        result: dict | None = None,
    ) -> uuid.UUID:
        async with self.maker() as db:
            batch = MaskRepairBatch(
                project_id=project_id,
                requested_by_id=user_id,
                async_job_id=None if rollback else async_job_id,
                rollback_async_job_id=async_job_id if rollback else None,
                token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                status=status,
                plan_digest=_HEX64,
                request_json={},
                plan_json={},
                result_json=result or {},
                receipt_expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
            db.add(batch)
            await db.commit()
            self.repair_ids.add(batch.id)
            return batch.id

    async def mask_format(
        self,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        async_job_id: uuid.UUID,
        status: str = "running",
        result: dict | None = None,
    ) -> uuid.UUID:
        async with self.maker() as db:
            batch = MaskFormatImport(
                project_id=project_id,
                requested_by_id=user_id,
                async_job_id=async_job_id,
                format_id="coco-rle",
                adapter_version="1.0",
                manifest_version="1.0",
                staged_object_key=f"staged/{uuid.uuid4()}.json",
                staged_sha256=_HEX64,
                mapping_json={},
                options_json={},
                mapping_digest=_HEX64,
                options_digest=_HEX64,
                plan_json={},
                plan_digest=_HEX64,
                token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                receipt_expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                status=status,
                result_json=result or {},
            )
            db.add(batch)
            await db.commit()
            self.format_ids.add(batch.id)
            return batch.id

    async def scalar_job(self, job_id: uuid.UUID) -> AsyncJob | None:
        async with self.maker() as db:
            return await db.get(AsyncJob, job_id)

    async def scalar_repair(self, batch_id: uuid.UUID) -> MaskRepairBatch | None:
        async with self.maker() as db:
            return await db.get(MaskRepairBatch, batch_id)

    async def scalar_format(self, batch_id: uuid.UUID) -> MaskFormatImport | None:
        async with self.maker() as db:
            return await db.get(MaskFormatImport, batch_id)

    async def notification_types(self, user_id: uuid.UUID) -> list[str]:
        async with self.maker() as db:
            rows = (
                await db.execute(
                    select(Notification.type)
                    .where(Notification.user_id == user_id)
                    .order_by(Notification.created_at.asc())
                )
            ).scalars()
            return list(rows)

    async def cleanup(self) -> None:
        async with self.maker() as db:
            if self.user_ids:
                await db.execute(
                    delete(Notification).where(Notification.user_id.in_(self.user_ids))
                )
            if self.format_ids:
                await db.execute(
                    delete(MaskFormatImport).where(
                        MaskFormatImport.id.in_(self.format_ids)
                    )
                )
            if self.repair_ids:
                await db.execute(
                    delete(MaskRepairBatch).where(
                        MaskRepairBatch.id.in_(self.repair_ids)
                    )
                )
            if self.job_ids:
                await db.execute(delete(AsyncJob).where(AsyncJob.id.in_(self.job_ids)))
            if self.project_ids:
                await db.execute(
                    delete(Project).where(Project.id.in_(self.project_ids))
                )
            if self.user_ids:
                await db.execute(delete(User).where(User.id.in_(self.user_ids)))
            await db.commit()


@pytest.fixture
async def signal_world(test_db_url, apply_migrations, monkeypatch):
    engine = create_async_engine(test_db_url, echo=False)
    monkeypatch.setattr(signals, "settings", SimpleNamespace(database_url=test_db_url))
    world = _SignalWorld(engine)
    try:
        yield world
    finally:
        await world.cleanup()
        await engine.dispose()


async def test_mark_failed_marks_job_and_notifies_once(signal_world):
    user_id = await signal_world.user()
    project_id = await signal_world.project(user_id)
    job_id, task_id = await signal_world.job(
        kind="batch_predict", user_id=user_id, project_id=project_id
    )

    await signals._mark_failed(task_id, "RuntimeError: boom")

    job = await signal_world.scalar_job(job_id)
    assert job.status == AsyncJobStatus.FAILED.value
    assert "RuntimeError: boom" in (job.error_message or "")
    assert job.completed_at is not None
    assert await signal_world.notification_types(user_id) == ["job.failed"]

    # A second late signal for the same task id is a no-op, not a duplicate.
    await signals._mark_failed(task_id, "RuntimeError: boom")
    assert await signal_world.notification_types(user_id) == ["job.failed"]


async def test_mark_failed_never_overwrites_terminal_job(signal_world):
    user_id = await signal_world.user()
    job_id, task_id = await signal_world.job(
        kind="batch_predict",
        user_id=user_id,
        status=AsyncJobStatus.COMPLETED.value,
        result={"success_count": 1},
    )

    await signals._mark_failed(task_id, "late crash")

    job = await signal_world.scalar_job(job_id)
    assert job.status == AsyncJobStatus.COMPLETED.value
    assert job.error_message is None
    assert job.result == {"success_count": 1}
    assert await signal_world.notification_types(user_id) == []


async def test_mark_failed_unknown_task_id_is_silent(signal_world):
    # No matching async_job is the common case for ml_health/cleanup tasks; the
    # fallback must not raise and must not fabricate a notification.
    await signals._mark_failed("does-not-exist", "boom")


async def test_mark_failed_unknown_kind_marks_generic_without_notification(
    signal_world,
):
    user_id = await signal_world.user()
    job_id, task_id = await signal_world.job(kind="create_tasks", user_id=user_id)

    await signals._mark_failed(task_id, "boom")

    job = await signal_world.scalar_job(job_id)
    assert job.status == AsyncJobStatus.FAILED.value
    # create_tasks is not a user-visible terminal kind: state changes, no notice.
    assert await signal_world.notification_types(user_id) == []


async def test_mark_cancelled_sets_cancelled_and_notifies(signal_world):
    user_id = await signal_world.user()
    job_id, task_id = await signal_world.job(kind="video_tracker", user_id=user_id)

    await signals._mark_cancelled(task_id)

    job = await signal_world.scalar_job(job_id)
    assert job.status == AsyncJobStatus.CANCELLED.value
    assert job.completed_at is not None
    assert await signal_world.notification_types(user_id) == ["job.cancelled"]


async def test_mark_cancelled_mask_format_import_keeps_partial(signal_world):
    user_id = await signal_world.user()
    project_id = await signal_world.project(user_id)
    committed_job, committed_task = await signal_world.job(
        kind="mask_format_import", user_id=user_id, project_id=project_id
    )
    committed_batch = await signal_world.mask_format(
        project_id=project_id,
        user_id=user_id,
        async_job_id=committed_job,
        result={"items": {"a": {"status": "committed"}, "b": {"status": "skipped"}}},
    )
    empty_job, empty_task = await signal_world.job(
        kind="mask_format_import", user_id=user_id, project_id=project_id
    )
    empty_batch = await signal_world.mask_format(
        project_id=project_id, user_id=user_id, async_job_id=empty_job
    )

    await signals._mark_cancelled(committed_task)
    await signals._mark_cancelled(empty_task)

    assert (await signal_world.scalar_format(committed_batch)).status == "partial"
    assert (await signal_world.scalar_format(empty_batch)).status == "cancelled"
    assert await signal_world.notification_types(user_id) == [
        "job.cancelled",
        "job.cancelled",
    ]


async def test_mark_failed_mask_repair_rollback_keeps_rollback_failed(signal_world):
    user_id = await signal_world.user()
    project_id = await signal_world.project(user_id)
    rollback_job, rollback_task = await signal_world.job(
        kind="mask_repair_rollback", user_id=user_id, project_id=project_id
    )
    rollback_batch = await signal_world.mask_repair(
        project_id=project_id,
        user_id=user_id,
        async_job_id=rollback_job,
        rollback=True,
    )
    repair_job, repair_task = await signal_world.job(
        kind="mask_repair", user_id=user_id, project_id=project_id
    )
    repair_batch = await signal_world.mask_repair(
        project_id=project_id, user_id=user_id, async_job_id=repair_job
    )

    await signals._mark_failed(rollback_task, "rollback crashed")
    await signals._mark_failed(repair_task, "repair crashed")

    assert (
        await signal_world.scalar_repair(rollback_batch)
    ).status == "rollback_failed"
    assert (await signal_world.scalar_repair(repair_batch)).status == "failed"


async def test_signal_handlers_ignore_missing_task_id(signal_world):
    # Neither handler may raise or touch the database when Celery supplies no id.
    async with signal_world.maker() as db:
        before = await db.scalar(select(func.count()).select_from(Notification))

    signals._on_task_failure(sender=None, task_id=None, exception=RuntimeError("x"))
    signals._on_task_revoked(sender=None, request=None)

    async with signal_world.maker() as db:
        after = await db.scalar(select(func.count()).select_from(Notification))
    assert after == before
