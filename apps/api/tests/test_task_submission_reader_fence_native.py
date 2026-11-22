from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.task_submission import TaskSubmissionWorkflow
from app.application.task_workflow_errors import TaskWorkflowError
from app.db.models.async_job import AsyncJob
from app.db.models.audit_log import AuditLog
from app.db.models.mask_qc import MaskQCRun
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.deps import get_quality_job_dispatcher
from app.services.audit import AuditAction
from app.services.audit_origin_contract import AuditOriginContext
from app.services.batch import BatchService
from app.services.labeling_core_read_relations import (
    lock_batch_rows_for_core_read,
    lock_task_for_core_read,
)
from app.services.labeling_tasks import LabelingTaskService
from app.services.original_content_access import visible_task
from app.services.project_access import resolve_project_access
from tests.factory import (
    create_batch,
    create_membership,
    create_project,
    create_task,
    create_user,
)

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class SubmissionRows:
    sessions: async_sessionmaker[AsyncSession]
    owner_id: uuid.UUID
    actor_id: uuid.UUID
    member_id: uuid.UUID
    project_id: uuid.UUID
    batch_id: uuid.UUID
    image_id: uuid.UUID
    video_id: uuid.UUID


@pytest.fixture
async def submission_rows(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        owner = await create_user(
            db, "project_admin", f"submit-owner-{uuid.uuid4()}@test.local", "Owner"
        )
        actor = await create_user(
            db, "employee", f"submit-actor-{uuid.uuid4()}@test.local", "Annotator"
        )
        project = await create_project(db, owner_id=owner.id)
        member = await create_membership(
            db, project_id=project.id, user_id=actor.id, role="annotator"
        )
        batch = await create_batch(db, project_id=project.id, status="annotating")
        batch.annotator_id = actor.id
        image = await create_task(db, project_id=project.id, status="in_progress")
        video = await create_task(db, project_id=project.id, status="pending")
        video.file_type = "video"
        video.file_name = "clip.mp4"
        video.file_path = "media/clip.mp4"
        for task in (image, video):
            task.batch_id = batch.id
            task.assignee_id = actor.id
        await db.flush()
        await BatchService(db).recalculate_counters(batch.id)
        rows = SubmissionRows(
            sessions,
            owner.id,
            actor.id,
            member.id,
            project.id,
            batch.id,
            image.id,
            video.id,
        )
    try:
        yield rows
    finally:
        async with sessions.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
            await db.execute(
                delete(AuditLog).where(AuditLog.target_id == str(rows.image_id))
            )
            await db.execute(
                delete(MaskQCRun).where(MaskQCRun.project_id == rows.project_id)
            )
            await db.execute(
                delete(AsyncJob).where(AsyncJob.project_id == rows.project_id)
            )
            await db.execute(delete(Task).where(Task.project_id == rows.project_id))
            await db.execute(
                delete(TaskBatch).where(TaskBatch.project_id == rows.project_id)
            )
            await db.execute(delete(Project).where(Project.id == rows.project_id))
            await db.execute(
                delete(User).where(User.id.in_([rows.owner_id, rows.actor_id]))
            )


async def _access(db, rows, actor_id):
    project = await db.get(Project, rows.project_id)
    return await resolve_project_access(db, actor_id=actor_id, project=project)


async def _read_video(db, rows):
    actor = await db.get(User, rows.actor_id)
    task = await visible_task(db, rows.video_id, actor, lock=True, wait_for_task=True)
    assert task.batch_id == rows.batch_id
    assert task.file_type == "video"


async def _submit(db, rows, *, access=None):
    if access is None:
        access = await _access(db, rows, rows.actor_id)
    return await TaskSubmissionWorkflow(db).submit_task(
        rows.image_id,
        actor_id=rows.actor_id,
        access=access,
        origin=AuditOriginContext(
            "POST", f"/api/v1/tasks/{rows.image_id}/submit", None
        ),
        quality_dispatcher=get_quality_job_dispatcher(),
    )


@asynccontextmanager
async def _blocked_submit(observer, reader, writer, operation):
    reader_pid = await reader.scalar(text("SELECT pg_backend_pid()"))
    writer_pid = await writer.scalar(text("SELECT pg_backend_pid()"))
    pending = asyncio.create_task(operation)
    try:
        async with asyncio.timeout(5):
            while not pending.done():
                if await observer.scalar(
                    text("SELECT :reader = ANY(pg_blocking_pids(:writer))"),
                    {"reader": reader_pid, "writer": writer_pid},
                ):
                    break
            else:
                pending.result()
                pytest.fail("Submit returned without waiting on the sibling reader")
        yield pending
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def _audit_rows(db, rows):
    return (
        await db.scalars(
            select(AuditLog).where(AuditLog.target_id == str(rows.image_id))
        )
    ).all()


async def _assert_no_qc(db, rows):
    assert (
        await db.scalar(
            select(MaskQCRun.id).where(MaskQCRun.project_id == rows.project_id)
        )
        is None
    )
    assert (
        await db.scalar(
            select(AsyncJob.id).where(AsyncJob.project_id == rows.project_id)
        )
        is None
    )


async def _assert_not_submitted(rows, *, batch_id=None, round_id=None):
    async with rows.sessions() as db:
        task = await db.get(Task, rows.image_id)
        assert task.batch_id == (batch_id or rows.batch_id)
        assert task.status == "in_progress"
        assert task.submitted_at is None
        assert task.review_round_id == round_id
        assert task.review_submitter_id is None
        batch = await db.get(TaskBatch, task.batch_id)
        assert batch.status == "annotating" and batch.review_tasks == 0
        assert (await db.get(Project, rows.project_id)).review_tasks == 0
        assert not await _audit_rows(db, rows)
        await _assert_no_qc(db, rows)


async def test_submit_drains_video_sibling_reader_and_commits_once(
    test_engine, submission_rows
):
    rows = submission_rows
    async with rows.sessions() as reader, rows.sessions() as observer:
        await _read_video(reader, rows)
        async with test_engine.begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout = '2s'"))
            async with async_sessionmaker(
                connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )() as writer:
                async with _blocked_submit(
                    observer, reader, writer, _submit(writer, rows)
                ) as pending:
                    async with rows.sessions() as contender:
                        with pytest.raises(TaskWorkflowError) as busy:
                            await _submit(contender, rows)
                        assert busy.value.status_code == 409
                        assert busy.value.detail == {"reason": "batch_busy"}
                    await reader.commit()
                    result = await asyncio.wait_for(pending, 5)
            assert await connection.scalar(text("SHOW lock_timeout")) == "2s"
            assert result == {
                "status": "submitted",
                "task_id": str(rows.image_id),
                "mask_qc_run_id": None,
                "mask_qc_status": None,
            }
            assert (await observer.get(Task, rows.image_id)).status == "in_progress"
            async with rows.sessions() as competing_reader:
                with pytest.raises(DBAPIError) as busy:
                    await lock_batch_rows_for_core_read(
                        competing_reader,
                        [rows.batch_id],
                        project_id=rows.project_id,
                        lock_mode="share",
                        nowait=True,
                    )
                assert busy.value.orig.sqlstate == "55P03"

    async with rows.sessions() as repeat:
        with pytest.raises(TaskWorkflowError) as refused:
            await _submit(repeat, rows)
        assert refused.value.status_code == 409
        assert refused.value.detail == {
            "reason": "task_not_submittable",
            "status": "review",
        }

    async with rows.sessions() as db:
        task = await db.get(Task, rows.image_id)
        assert task.status == "review" and task.submitted_at is not None
        assert task.review_round_id is not None
        assert task.review_submitter_id == rows.actor_id
        assert (await db.get(Task, rows.video_id)).status == "pending"
        batch = await db.get(TaskBatch, rows.batch_id)
        assert (
            batch.status,
            batch.total_tasks,
            batch.completed_tasks,
            batch.review_tasks,
        ) == ("annotating", 2, 0, 1)
        project = await db.get(Project, rows.project_id)
        assert (
            project.total_tasks,
            project.completed_tasks,
            project.review_tasks,
            project.in_progress_tasks,
        ) == (2, 0, 1, 0)
        (audit,) = await _audit_rows(db, rows)
        assert (
            audit.action == AuditAction.TASK_SUBMIT and audit.actor_id == rows.actor_id
        )
        assert audit.detail_json["review_round_id"] == str(task.review_round_id)
        await _assert_no_qc(db, rows)


@pytest.mark.parametrize("reason", ["batch_busy", "task_busy"])
async def test_submit_rejects_exclusive_editor_without_waiting(submission_rows, reason):
    rows = submission_rows
    async with rows.sessions() as editor, rows.sessions() as writer:
        if reason == "batch_busy":
            assert await lock_batch_rows_for_core_read(
                editor, [rows.batch_id], nowait=True
            )
        else:
            assert await lock_task_for_core_read(editor, rows.image_id, nowait=True)
        await writer.execute(text("SET LOCAL lock_timeout = '2s'"))
        await writer.execute(text("SET LOCAL statement_timeout = '750ms'"))
        with pytest.raises(TaskWorkflowError) as busy:
            await _submit(writer, rows)
        assert (busy.value.status_code, busy.value.detail) == (409, {"reason": reason})
    await _assert_not_submitted(rows)


@pytest.mark.parametrize(
    ("caller_timeout", "statement_limit"),
    [("100ms", "750ms"), ("2s", "1500ms")],
    ids=["shorter", "capped"],
)
async def test_submit_reader_timeout_preserves_caller_budget(
    submission_rows, caller_timeout, statement_limit
):
    rows = submission_rows
    async with rows.sessions() as reader, rows.sessions() as writer:
        await _read_video(reader, rows)
        await writer.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": caller_timeout},
        )
        await writer.execute(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": statement_limit},
        )
        with pytest.raises(TaskWorkflowError) as busy:
            await _submit(writer, rows)
        assert busy.value.status_code == 409
        assert busy.value.detail == {"reason": "batch_busy"}
        assert await writer.scalar(text("SHOW lock_timeout")) == caller_timeout
        await reader.commit()
        async with rows.sessions() as editor:
            assert await lock_batch_rows_for_core_read(
                editor, [rows.batch_id], project_id=rows.project_id, nowait=True
            )
    await _assert_not_submitted(rows)


@pytest.mark.parametrize("denial", ["revoked_membership", "rebound_actor"])
async def test_submit_rechecks_bound_actor_and_current_membership(
    submission_rows, denial
):
    rows = submission_rows
    async with rows.sessions() as writer:
        access = await _access(
            writer,
            rows,
            rows.actor_id if denial == "revoked_membership" else rows.owner_id,
        )
        if denial == "revoked_membership":
            async with rows.sessions.begin() as revoker:
                await revoker.execute(
                    delete(ProjectMember).where(ProjectMember.id == rows.member_id)
                )
        with pytest.raises(HTTPException) as refused:
            await _submit(writer, rows, access=access)
        if denial == "revoked_membership":
            assert (refused.value.status_code, refused.value.detail) == (
                404,
                "项目不存在",
            )
        else:
            assert refused.value.status_code == 409
            assert refused.value.detail == {"reason": "access_context_mismatch"}
    await _assert_not_submitted(rows)


async def test_submit_rechecks_rebound_task_after_reader_drains(submission_rows):
    rows = submission_rows
    async with rows.sessions.begin() as db:
        destination = await create_batch(
            db, project_id=rows.project_id, status="annotating"
        )
        destination.annotator_id = rows.actor_id
        destination_id = destination.id
    async with (
        rows.sessions() as reader,
        rows.sessions() as observer,
        rows.sessions() as writer,
    ):
        await _read_video(reader, rows)
        async with _blocked_submit(
            observer, reader, writer, _submit(writer, rows)
        ) as pending:
            async with rows.sessions.begin() as mover:
                assert (
                    await BatchService(mover).assign_tasks_to_batch(
                        destination_id, [rows.image_id]
                    )
                    == 1
                )
            await reader.commit()
            with pytest.raises(TaskWorkflowError) as conflict:
                await asyncio.wait_for(pending, 5)
            assert conflict.value.status_code == 409
            assert conflict.value.detail == {"reason": "task_target_changed"}
    await _assert_not_submitted(rows, batch_id=destination_id)


async def test_submit_native_owner_refuses_stale_round_with_matching_status(
    submission_rows,
):
    rows = submission_rows
    now = datetime.now(timezone.utc)
    async with rows.sessions() as writer:
        stale_task = await writer.get(Task, rows.image_id)
        assert stale_task.review_round_id is None and stale_task.status == "in_progress"
        async with rows.sessions.begin() as prior:
            labeling = LabelingTaskService(prior)
            task = await prior.get(Task, rows.image_id)
            round_id = await labeling.submit_task(task, actor_id=rows.actor_id, now=now)
            await labeling.withdraw_task(task, actor_id=rows.actor_id)
        with pytest.raises(TaskWorkflowError) as conflict:
            await LabelingTaskService(writer).submit_task(
                stale_task, actor_id=rows.actor_id, now=now
            )
        assert (conflict.value.status_code, conflict.value.detail) == (
            409,
            {"status": "in_progress"},
        )
    await _assert_not_submitted(rows, round_id=round_id)
