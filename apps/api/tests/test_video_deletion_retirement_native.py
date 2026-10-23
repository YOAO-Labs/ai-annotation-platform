"""Deletion must preserve canonical Jobs until their execution is retired."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_assets_video_inputs import execution as tracker_input_execution
from aap_assets_video_inputs_runtime.garbage import VideoGarbageCollector
from app.db.models.annotation import Annotation
from app.db.models.dataset import ProjectDataset
from app.db.models.video_tracker_job import VideoTrackerJob
from tests.factory import create_project, create_task
from tests.test_video_execution_checkpoints_native import _claimed_running_job
from tests.test_video_execution_native import (
    _install_ready_tracker_scope,
    _proof,
    _step,
)
from tests.test_video_tracker_input_lifecycle import (
    _capture_input,
    _finish_request,
    _native_call as _tracker_native_call,
    _plan_for_claim,
    _prepare_and_claim,
    _stage_times,
)
from tests.test_video_tracker_input_native import _capture, _seed_tracker_job

pytestmark = pytest.mark.asyncio


async def _seed_unretired(
    db, *, bound=True, journal=True, status="cancelled", claimed=True
):
    if claimed and journal:
        job, input_id, claim = await _claimed_running_job(db)
    else:
        actor, _project, _item, _task, job = await _seed_tracker_job(db)
        await _capture(db, actor, job.id)
        input_id = await db.scalar(
            text(
                "SELECT input_id FROM video_tracker_input_scopes WHERE tracker_job_id=:job"
            ),
            {"job": job.id},
        )
        claim = None
        if claimed:
            result = await _step(
                db,
                "claim",
                {
                    "job_id": str(job.id),
                    "worker_id": str(uuid.uuid4()),
                    "lease_seconds": 30,
                },
            )
            assert result["status"] == "ok", result
            claim = result["execution"]
    if bound and claimed:
        await _install_ready_tracker_scope(db, job.id, claim)
    row = (
        await db.execute(
            text(
                "SELECT task.project_id,item.dataset_id FROM tasks AS task "
                "JOIN dataset_items AS item ON item.id=task.dataset_item_id "
                "WHERE task.id=:id"
            ),
            {"id": job.task_id},
        )
    ).one()
    db.add(ProjectDataset(project_id=row.project_id, dataset_id=row.dataset_id))
    job.status = status
    await db.flush()
    return {
        "actor_id": job.created_by,
        "project_id": row.project_id,
        "dataset_id": row.dataset_id,
        "task_id": job.task_id,
        "job_id": job.id,
        "input_id": input_id,
        "claim": claim,
    }


async def _retire(db, fixture):
    raw = await db.scalar(
        text("SELECT aap_jobs_v1.video_execution_checkpoint_step('retire',:proof)"),
        {"proof": json.dumps(_proof(fixture["job_id"], fixture["claim"]))},
    )
    reply = json.loads(raw) if isinstance(raw, str) else raw
    assert reply["status"] == "ok" and reply["retired"] is True, reply
    return reply


def _blocked(kind):
    return {
        "schema": "aap-project-delete/v1"
        if kind == "project"
        else "aap-dataset-unlink/v1",
        "ok": False,
        "error": {"code": "video_execution_not_retired"},
    }


async def _no_deletion_receipts(db, operation_id):
    for table in (
        "project_deletion_operations",
        "dataset_unlink_operations",
        "asset_storage_deletions",
    ):
        assert (
            await db.scalar(
                text(f"SELECT count(*) FROM {table} WHERE operation_id=:id"),
                {"id": operation_id},
            )
            == 0
        )
    assert (
        await db.scalar(
            text(
                "SELECT count(*) FROM audit_logs WHERE action IN ('project.delete','dataset.unlink') AND detail_json->>'operation_id'=:id"
            ),
            {"id": str(operation_id)},
        )
        == 0
    )


async def _assert_task_job_present(db, fixture):
    for table, identity in (
        ("tasks", fixture["task_id"]),
        ("video_tracker_jobs", fixture["job_id"]),
    ):
        assert await db.scalar(
            text(f"SELECT EXISTS(SELECT 1 FROM {table} WHERE id=:id)"), {"id": identity}
        )


async def _delete(db, kind, fixture, *, operation_id=None):
    params = {**fixture, "operation_id": operation_id or uuid.uuid4()}
    if kind == "project":
        sql = (
            "SELECT aap_workspace_v1.delete_project("
            "CAST(:operation_id AS uuid),CAST(:actor_id AS uuid),CAST(:project_id AS uuid),NULL)"
        )
    else:
        sql = (
            "SELECT aap_workspace_v1.unlink_dataset("
            "CAST(:operation_id AS uuid),CAST(:actor_id AS uuid),CAST(:dataset_id AS uuid),"
            "CAST(:project_id AS uuid),'video-delete-test',NULL)"
        )
    value = await db.scalar(text(sql), params)
    return json.loads(value) if isinstance(value, str) else value


async def _seed_tracker_input(db, *, ready=True):
    actor, _, _, task_id, job = await _seed_tracker_job(db)
    _, captured = await _capture_input(db, actor, job)
    claim = await _prepare_and_claim(db, actor, task_id, captured)
    if ready:
        plan, entries = _plan_for_claim(claim, frame_count=6)
        await _stage_times(db, claim, entries)
        result = await _tracker_native_call(
            db, "finish_video_tracker_input", _finish_request(claim, plan)
        )
        assert result.status == "ready"
    row = (
        await db.execute(
            text(
                "SELECT task.project_id,item.dataset_id FROM tasks AS task "
                "JOIN dataset_items AS item ON item.id=task.dataset_item_id "
                "WHERE task.id=:id"
            ),
            {"id": task_id},
        )
    ).one()
    db.add(ProjectDataset(project_id=row.project_id, dataset_id=row.dataset_id))
    await db.flush()
    return {
        "actor_id": actor,
        "project_id": row.project_id,
        "dataset_id": row.dataset_id,
        "task_id": task_id,
        "job_id": job.id,
        "input_id": captured.input_id,
        "job": job,
        "claim": claim,
    }


async def _queue_historical_tracker_input_root(db, fixture):
    now = await db.scalar(text("SELECT pg_catalog.clock_timestamp()"))
    prefix = await db.scalar(
        text(
            "SELECT prefix FROM public.asset_accepted_video_input_attempts "
            "WHERE input_id=:input AND generation=:generation"
        ),
        {"input": fixture["input_id"], "generation": fixture["claim"].generation},
    )
    await db.execute(
        text(
            "SELECT aap_assets_v1._video_input_queue_prefix("
            ":prefix,'input_expired',pg_catalog.clock_timestamp())"
        ),
        {"prefix": prefix},
    )
    await db.execute(
        text(
            "UPDATE public.asset_accepted_video_inputs "
            "SET status='expired',retire_after=:now,retention_checked_at=:now "
            "WHERE input_id=:input"
        ),
        {"now": now, "input": fixture["input_id"]},
    )
    await db.execute(
        text(
            "UPDATE public.asset_accepted_video_input_attempts "
            "SET status='expired',completed_at=:now "
            "WHERE input_id=:input AND generation=:generation"
        ),
        {
            "now": now,
            "input": fixture["input_id"],
            "generation": fixture["claim"].generation,
        },
    )
    await db.execute(
        text(
            "UPDATE public.asset_accepted_input_garbage "
            "SET not_before=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE prefix=:prefix"
        ),
        {"prefix": prefix},
    )
    frozen_source_key = await db.scalar(
        text(
            "SELECT plan #>> '{input,blob,key}' "
            "FROM public.asset_accepted_video_inputs WHERE input_id=:input"
        ),
        {"input": fixture["input_id"]},
    )
    assert frozen_source_key == prefix + "source"
    return prefix


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_unretired_cancelled_execution_blocks_cascade(db_session, kind):
    fixture = await _seed_unretired(db_session)
    reply = await _delete(db_session, kind, fixture)
    observed = {
        "reply": reply,
        "job_exists": await db_session.scalar(
            text("SELECT EXISTS(SELECT 1 FROM video_tracker_jobs WHERE id=:id)"),
            {"id": fixture["job_id"]},
        ),
        "task_exists": await db_session.scalar(
            text("SELECT EXISTS(SELECT 1 FROM tasks WHERE id=:id)"),
            {"id": fixture["task_id"]},
        ),
        "execution_retired": await db_session.scalar(
            text(
                "SELECT execution_retired FROM aap_jobs_v1.video_execution_journals WHERE job_id=:id"
            ),
            {"id": fixture["job_id"]},
        ),
    }
    assert observed == {
        "reply": {
            "schema": "aap-project-delete/v1"
            if kind == "project"
            else "aap-dataset-unlink/v1",
            "ok": False,
            "error": {"code": "video_execution_not_retired"},
        },
        "job_exists": True,
        "task_exists": True,
        "execution_retired": False,
    }


@pytest.mark.parametrize("kind", ["project", "unlink"])
@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "running",
        "completed",
        "failed",
        "pending_review",
        "partially_reviewed",
        "accepted",
        "discarded",
    ],
)
async def test_job_status_does_not_replace_retirement_proof(db_session, kind, status):
    fixture = await _seed_unretired(db_session, status=status)
    operation = uuid.uuid4()
    assert await _delete(db_session, kind, fixture, operation_id=operation) == _blocked(
        kind
    )
    await _assert_task_job_present(db_session, fixture)
    await _no_deletion_receipts(db_session, operation)
    assert (
        await db_session.scalar(
            text("SELECT aap_jobs_v1.video_execution_input_retained(:id)"),
            {"id": fixture["input_id"]},
        )
        is True
    )


@pytest.mark.parametrize("kind", ["project", "unlink"])
@pytest.mark.parametrize("bound", [False, True])
async def test_claim_before_first_journal_cannot_be_deleted(db_session, kind, bound):
    fixture = await _seed_unretired(db_session, journal=False, bound=bound)
    assert await _delete(db_session, kind, fixture) == _blocked(kind)
    await _assert_task_job_present(db_session, fixture)


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_current_run_must_match_even_a_retired_old_journal(db_session, kind):
    fixture = await _seed_unretired(db_session, journal=False, bound=False)
    await db_session.execute(
        text(
            "INSERT INTO aap_jobs_v1.video_execution_journals(job_id,run_id,writer_terminal,execution_retired) VALUES(:job,:old_run,true,true)"
        ),
        {"job": fixture["job_id"], "old_run": uuid.uuid4()},
    )
    assert await _delete(db_session, kind, fixture) == _blocked(kind)
    await _assert_task_job_present(db_session, fixture)


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_live_job_with_inconsistent_retired_journal_is_rejected(db_session, kind):
    fixture = await _seed_unretired(
        db_session, journal=False, bound=False, status="running"
    )
    await db_session.execute(
        text(
            "INSERT INTO aap_jobs_v1.video_execution_journals(job_id,run_id,writer_terminal,execution_retired) VALUES(:job,:run,true,true)"
        ),
        {"job": fixture["job_id"], "run": uuid.UUID(fixture["claim"]["run_id"])},
    )
    assert await _delete(db_session, kind, fixture) == _blocked(kind)


@pytest.mark.parametrize("kind", ["project", "unlink"])
@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "running",
        "cancelled",
        "completed",
        "failed",
        "pending_review",
        "partially_reviewed",
        "accepted",
        "discarded",
    ],
)
async def test_started_legacy_job_without_ledger_requires_explicit_drain(
    db_session, kind, status
):
    fixture = await _seed_unretired(db_session, claimed=False, status=status)
    await db_session.execute(
        text("UPDATE video_tracker_jobs SET started_at=:started WHERE id=:id"),
        {"id": fixture["job_id"], "started": datetime.now(timezone.utc)},
    )
    assert await _delete(db_session, kind, fixture) == _blocked(kind)
    await _assert_task_job_present(db_session, fixture)


@pytest.mark.parametrize(
    "case",
    [
        "started_legacy",
        "input_scope_orphan",
        "missing_scope_started_legacy",
        "current_run_before_journal",
        "stale_retired_journal",
    ],
)
async def test_deletion_block_also_retains_unproven_video_input(db_session, case):
    fixture = await _seed_tracker_input(db_session)
    job = fixture["job"]
    now = await db_session.scalar(text("SELECT pg_catalog.clock_timestamp()"))

    if case in {"current_run_before_journal", "stale_retired_journal"}:
        result = await _step(
            db_session,
            "claim",
            {
                "job_id": str(job.id),
                "worker_id": str(uuid.uuid4()),
                "lease_seconds": 30,
            },
        )
        assert result["status"] == "ok", result
        if case == "stale_retired_journal":
            await db_session.execute(
                text(
                    "INSERT INTO aap_jobs_v1.video_execution_journals "
                    "(job_id,run_id,input_id,writer_terminal,execution_retired) "
                    "VALUES(:job,:run,:input,true,true)"
                ),
                {
                    "job": job.id,
                    "run": uuid.uuid4(),
                    "input": fixture["input_id"],
                },
            )
        job.status = "cancelled"
        job.cancel_requested_at = now
    else:
        job.status = "completed"
        job.started_at = now
        job.completed_at = now
        if case == "input_scope_orphan":
            await db_session.flush()
            await db_session.execute(
                text("DELETE FROM public.video_tracker_jobs WHERE id=:id"),
                {"id": job.id},
            )
        elif case == "missing_scope_started_legacy":
            await db_session.flush()
            await db_session.execute(
                text(
                    "DELETE FROM public.video_tracker_input_scopes WHERE input_id=:id"
                ),
                {"id": fixture["input_id"]},
            )
    await db_session.flush()

    deletion = await _delete(db_session, "project", fixture)
    retention_raw = await db_session.scalar(
        text("SELECT aap_labeling_v1.video_tracker_input_referenced(:id)"),
        {"id": fixture["input_id"]},
    )
    retention = (
        json.loads(retention_raw) if isinstance(retention_raw, str) else retention_raw
    )
    sweep = await _tracker_native_call(
        db_session,
        "sweep_video_tracker_inputs",
        tracker_input_execution.SweepVideoInputsRequest(limit=10),
    )
    input_status = await db_session.scalar(
        text(
            "SELECT status FROM public.asset_accepted_video_inputs WHERE input_id=:id"
        ),
        {"id": fixture["input_id"]},
    )
    queued_prefixes = await db_session.scalar(
        text(
            "SELECT count(*) FROM public.asset_accepted_video_input_attempts AS attempt "
            "JOIN public.asset_accepted_input_garbage AS garbage USING (prefix) "
            "WHERE attempt.input_id=:id AND garbage.reason='input_expired'"
        ),
        {"id": fixture["input_id"]},
    )

    observed = {
        "deletion": deletion,
        "referenced": retention.get("data", {}).get("referenced"),
        "sweep_expired": sweep.expired,
        "input_status": input_status,
        "queued_prefixes": queued_prefixes,
    }
    assert retention.get("data", {}).get("referenced") is True, retention
    assert observed == {
        "deletion": _blocked("project"),
        "referenced": True,
        "sweep_expired": 0,
        "input_status": "ready",
        "queued_prefixes": 0,
    }, observed


async def _retire_current_tracker_run_without_routes(db, fixture):
    job = fixture["job"]
    result = await _step(
        db,
        "claim",
        {
            "job_id": str(job.id),
            "worker_id": str(uuid.uuid4()),
            "lease_seconds": 30,
        },
    )
    assert result["status"] == "ok", result
    claim = result["execution"]
    project_id = await db.scalar(
        text("SELECT project_id FROM public.tasks WHERE id=:task"),
        {"task": fixture["task_id"]},
    )
    await db.execute(
        text(
            "INSERT INTO aap_jobs_v1.video_execution_journals "
            "(job_id,run_id,input_id,task_id,project_id,source_item_id) "
            "VALUES(:job,:run,:input,:task,:project,:source)"
        ),
        {
            "job": job.id,
            "run": uuid.UUID(claim["run_id"]),
            "input": fixture["input_id"],
            "task": fixture["task_id"],
            "project": project_id,
            "source": job.dataset_item_id,
        },
    )
    job.status = "failed"
    job.completed_at = await db.scalar(text("SELECT pg_catalog.clock_timestamp()"))
    await db.flush()
    journal = (
        await db.execute(
            text(
                "SELECT run_id,writer_terminal,execution_retired "
                "FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
            ),
            {"job": job.id},
        )
    ).one()
    assert journal == (uuid.UUID(claim["run_id"]), True, True)


@pytest.mark.parametrize(
    "case",
    [
        "never_started_terminal",
        "retired_current_run",
        "never_started_terminal_without_scope",
        "retired_current_run_without_scope",
        "review_grace",
    ],
)
async def test_proven_safe_inputs_keep_existing_retention_rules(db_session, case):
    fixture = await _seed_tracker_input(db_session)
    job = fixture["job"]
    now = await db_session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
    if case in {"never_started_terminal", "never_started_terminal_without_scope"}:
        job.status = "completed"
        job.completed_at = now
    else:
        await _retire_current_tracker_run_without_routes(db_session, fixture)
        if case == "review_grace":
            job.status = "pending_review"
            job.staged_result = {"frames": []}
            job.completed_at = now
    if case in {
        "never_started_terminal_without_scope",
        "retired_current_run_without_scope",
    }:
        await db_session.flush()
        await db_session.execute(
            text("DELETE FROM public.video_tracker_input_scopes WHERE input_id=:id"),
            {"id": fixture["input_id"]},
        )
    await db_session.flush()

    retention_raw = await db_session.scalar(
        text("SELECT aap_labeling_v1.video_tracker_input_referenced(:id)"),
        {"id": fixture["input_id"]},
    )
    retention = (
        json.loads(retention_raw) if isinstance(retention_raw, str) else retention_raw
    )
    sweep = await _tracker_native_call(
        db_session,
        "sweep_video_tracker_inputs",
        tracker_input_execution.SweepVideoInputsRequest(limit=10),
    )
    input_status = await db_session.scalar(
        text(
            "SELECT status FROM public.asset_accepted_video_inputs WHERE input_id=:id"
        ),
        {"id": fixture["input_id"]},
    )
    queued_prefixes = await db_session.scalar(
        text(
            "SELECT count(*) FROM public.asset_accepted_video_input_attempts AS attempt "
            "JOIN public.asset_accepted_input_garbage AS garbage USING (prefix) "
            "WHERE attempt.input_id=:id AND garbage.reason='input_expired'"
        ),
        {"id": fixture["input_id"]},
    )

    if case == "review_grace":
        assert retention["data"]["referenced"] is True
        assert sweep.expired == 0
        assert input_status == "ready"
        assert queued_prefixes == 0
    else:
        assert retention["data"]["referenced"] is False
        assert sweep.expired == 1
        assert input_status == "expired"
        assert queued_prefixes == 1


async def test_missing_scope_nonready_input_fails_closed_and_gc_skips(db_session):
    fixture = await _seed_tracker_input(db_session, ready=False)
    now = await db_session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
    fixture["job"].status = "completed"
    fixture["job"].started_at = now
    fixture["job"].completed_at = now
    await db_session.flush()
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_inputs "
            "SET lease_expires_at=:expired,retention_checked_at=NULL "
            "WHERE input_id=:id"
        ),
        {"expired": now - timedelta(seconds=1), "id": fixture["input_id"]},
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_input_attempts "
            "SET lease_expires_at=:expired WHERE input_id=:id AND generation=:generation"
        ),
        {
            "expired": now - timedelta(seconds=1),
            "id": fixture["input_id"],
            "generation": fixture["claim"].generation,
        },
    )
    await db_session.execute(
        text("DELETE FROM public.video_tracker_input_scopes WHERE input_id=:id"),
        {"id": fixture["input_id"]},
    )

    retention_raw = await db_session.scalar(
        text("SELECT aap_labeling_v1.video_tracker_input_referenced(:id)"),
        {"id": fixture["input_id"]},
    )
    retention = (
        json.loads(retention_raw) if isinstance(retention_raw, str) else retention_raw
    )
    sweep = await _tracker_native_call(
        db_session,
        "sweep_video_tracker_inputs",
        tracker_input_execution.SweepVideoInputsRequest(limit=10),
    )
    observed = {
        "retention": retention,
        "sweep_expired": sweep.expired,
        "input_status": await db_session.scalar(
            text(
                "SELECT status FROM public.asset_accepted_video_inputs WHERE input_id=:id"
            ),
            {"id": fixture["input_id"]},
        ),
        "queued_prefixes": await db_session.scalar(
            text(
                "SELECT count(*) FROM public.asset_accepted_video_input_attempts AS attempt "
                "JOIN public.asset_accepted_input_garbage AS garbage USING (prefix) "
                "WHERE attempt.input_id=:id"
            ),
            {"id": fixture["input_id"]},
        ),
    }
    assert observed == {
        "retention": {
            "schema": "aap-labeling-video-tracker-inputs/v1",
            "ok": False,
            "error": {"code": "invalid_response"},
        },
        "sweep_expired": 0,
        "input_status": "materializing",
        "queued_prefixes": 0,
    }, observed


async def test_tracker_input_retention_lock_contention_fails_closed_and_gc_skips(
    test_engine,
):
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner.begin() as db:
        fixture = await _seed_tracker_input(db)
        now = await db.scalar(text("SELECT pg_catalog.clock_timestamp()"))
        fixture["job"].status = "completed"
        fixture["job"].started_at = now
        fixture["job"].completed_at = now
    async with owner() as locker, owner() as caller:
        await locker.execute(
            text("SELECT id FROM public.video_tracker_jobs WHERE id=:id FOR UPDATE"),
            {"id": fixture["job_id"]},
        )
        retention_raw = await asyncio.wait_for(
            caller.scalar(
                text("SELECT aap_labeling_v1.video_tracker_input_referenced(:id)"),
                {"id": fixture["input_id"]},
            ),
            5,
        )
        retention = (
            json.loads(retention_raw)
            if isinstance(retention_raw, str)
            else retention_raw
        )
        sweep = await asyncio.wait_for(
            _tracker_native_call(
                caller,
                "sweep_video_tracker_inputs",
                tracker_input_execution.SweepVideoInputsRequest(limit=10),
            ),
            5,
        )
        assert retention == {
            "schema": "aap-labeling-video-tracker-inputs/v1",
            "ok": False,
            "error": {"code": "invalid_response"},
        }
        assert sweep.expired == 0
        assert (
            await caller.scalar(
                text(
                    "SELECT status FROM public.asset_accepted_video_inputs WHERE input_id=:id"
                ),
                {"id": fixture["input_id"]},
            )
            == "ready"
        )
        await caller.rollback()
        await locker.rollback()


class _NativeVideoGarbageDatabase:
    def __init__(self, db):
        self.db = db

    async def call(self, operation, request):
        if operation == "claim_accepted_video_input_garbage":
            statement = (
                "SELECT aap_assets_v1.claim_accepted_video_input_garbage("
                "CAST(:worker AS uuid),CAST(:lease AS integer),CAST(:page AS integer))"
            )
            parameters = {
                "worker": request.worker_id,
                "lease": request.lease_seconds,
                "page": request.page_size,
            }
        elif operation == "complete_accepted_video_input_garbage":
            statement = (
                "SELECT aap_assets_v1.complete_accepted_video_input_garbage("
                "CAST(:garbage AS uuid),CAST(:token AS uuid))"
            )
            parameters = {
                "garbage": request.garbage_id,
                "token": request.lease_token,
            }
        else:
            raise AssertionError(f"unexpected garbage operation: {operation}")
        raw = await self.db.scalar(text(statement), parameters)
        return tracker_input_execution.parse_native(operation, request, raw)


class _RecordingVideoStorage:
    def __init__(self):
        self.deleted_prefixes = []

    async def delete_attempt_versions(self, prefix):
        self.deleted_prefixes.append(prefix)
        return 1


async def test_due_historical_tracker_tombstone_is_rechecked_before_gc_delete(
    db_session,
):
    fixture = await _seed_tracker_input(db_session)
    job = fixture["job"]
    now = await db_session.scalar(text("SELECT pg_catalog.clock_timestamp()"))
    job.status = "completed"
    job.started_at = now
    job.completed_at = now
    await db_session.flush()
    # Recreate the persisted result of the pre-0312 tracker sweep with a real
    # accepted input, attempt prefix, and owner-created garbage tombstone.
    prefix = await _queue_historical_tracker_input_root(db_session, fixture)

    deletion = await _delete(db_session, "project", fixture)
    retention_raw = await db_session.scalar(
        text("SELECT aap_labeling_v1.video_tracker_input_referenced(:id)"),
        {"id": fixture["input_id"]},
    )
    retention = (
        json.loads(retention_raw) if isinstance(retention_raw, str) else retention_raw
    )
    storage = _RecordingVideoStorage()
    collector = VideoGarbageCollector(
        _NativeVideoGarbageDatabase(db_session), storage, worker_id=uuid.uuid4()
    )
    completed = await collector.collect_once()
    tombstone = (
        await db_session.execute(
            text(
                "SELECT lease_token,completed_at,not_before "
                "FROM public.asset_accepted_input_garbage WHERE prefix=:prefix"
            ),
            {"prefix": prefix},
        )
    ).one()
    after_claim = await db_session.scalar(text("SELECT pg_catalog.clock_timestamp()"))

    observed = {
        "deletion_blocked": deletion == _blocked("project"),
        "input_referenced": retention.get("data", {}).get("referenced"),
        "deleted_prefixes": storage.deleted_prefixes,
        "completed": completed,
        "tombstone_deferred": (
            tombstone.lease_token is None
            and tombstone.completed_at is None
            and tombstone.not_before > after_claim
        ),
    }
    assert observed == {
        "deletion_blocked": True,
        "input_referenced": True,
        "deleted_prefixes": [],
        "completed": 0,
        "tombstone_deferred": True,
    }, observed


@pytest.mark.parametrize("status", ["running", "unknown_future_state"])
async def test_unstarted_job_status_outside_deletion_safe_set_retains_input(
    db_session, status
):
    fixture = await _seed_tracker_input(db_session)
    await db_session.execute(
        text(
            "UPDATE public.video_tracker_jobs SET status=:status,started_at=NULL "
            "WHERE id=:id"
        ),
        {"status": status, "id": fixture["job_id"]},
    )
    await db_session.flush()
    deletion = await _delete(db_session, "project", fixture)
    jobs_reply_raw = await db_session.scalar(
        text("SELECT aap_jobs_v1.video_tracker_input_retention_facts(:input,:job)"),
        {"input": fixture["input_id"], "job": fixture["job_id"]},
    )
    jobs_reply = (
        json.loads(jobs_reply_raw)
        if isinstance(jobs_reply_raw, str)
        else jobs_reply_raw
    )
    retention_raw = await db_session.scalar(
        text("SELECT aap_labeling_v1.video_tracker_input_referenced(:id)"),
        {"id": fixture["input_id"]},
    )
    retention = (
        json.loads(retention_raw) if isinstance(retention_raw, str) else retention_raw
    )
    sweep = await _tracker_native_call(
        db_session,
        "sweep_video_tracker_inputs",
        tracker_input_execution.SweepVideoInputsRequest(limit=10),
    )
    observed = {
        "deletion_blocked": deletion == _blocked("project"),
        "jobs_execution_retained": jobs_reply.get("data", {}).get("execution_retained"),
        "input_referenced": retention.get("data", {}).get("referenced"),
        "sweep_expired": sweep.expired,
        "input_status": await db_session.scalar(
            text(
                "SELECT status FROM public.asset_accepted_video_inputs "
                "WHERE input_id=:id"
            ),
            {"id": fixture["input_id"]},
        ),
    }
    assert observed == {
        "deletion_blocked": True,
        "jobs_execution_retained": True,
        "input_referenced": True,
        "sweep_expired": 0,
        "input_status": "ready",
    }, observed


@pytest.mark.parametrize("status", ["running", "unknown_future_state"])
async def test_retired_run_with_nonterminal_status_still_retains_input(
    db_session, status
):
    fixture = await _seed_tracker_input(db_session)
    await _retire_current_tracker_run_without_routes(db_session, fixture)
    await db_session.execute(
        text("UPDATE public.video_tracker_jobs SET status=:status WHERE id=:id"),
        {"status": status, "id": fixture["job_id"]},
    )
    await db_session.flush()

    deletion = await _delete(db_session, "project", fixture)
    jobs_reply_raw = await db_session.scalar(
        text("SELECT aap_jobs_v1.video_tracker_input_retention_facts(:input,:job)"),
        {"input": fixture["input_id"], "job": fixture["job_id"]},
    )
    jobs_reply = (
        json.loads(jobs_reply_raw)
        if isinstance(jobs_reply_raw, str)
        else jobs_reply_raw
    )
    retention_raw = await db_session.scalar(
        text("SELECT aap_labeling_v1.video_tracker_input_referenced(:id)"),
        {"id": fixture["input_id"]},
    )
    retention = (
        json.loads(retention_raw) if isinstance(retention_raw, str) else retention_raw
    )
    sweep = await _tracker_native_call(
        db_session,
        "sweep_video_tracker_inputs",
        tracker_input_execution.SweepVideoInputsRequest(limit=10),
    )
    assert deletion == _blocked("project")
    assert jobs_reply["data"]["execution_retained"] is True
    assert retention["data"]["referenced"] is True
    assert sweep.expired == 0


@pytest.mark.parametrize("state", ["never_started", "current_retired"])
async def test_safe_historical_tracker_tombstones_still_reach_video_gc(
    db_session, state
):
    fixture = await _seed_tracker_input(db_session)
    if state == "never_started":
        fixture["job"].status = "completed"
        fixture["job"].started_at = None
        fixture["job"].completed_at = await db_session.scalar(
            text("SELECT pg_catalog.clock_timestamp()")
        )
    else:
        await _retire_current_tracker_run_without_routes(db_session, fixture)
    await db_session.flush()
    prefix = await _queue_historical_tracker_input_root(db_session, fixture)
    storage = _RecordingVideoStorage()
    collector = VideoGarbageCollector(
        _NativeVideoGarbageDatabase(db_session), storage, worker_id=uuid.uuid4()
    )

    completed = await collector.collect_once()

    assert storage.deleted_prefixes == [prefix]
    assert completed == 1
    assert await db_session.scalar(
        text(
            "SELECT completed_at IS NOT NULL "
            "FROM public.asset_accepted_input_garbage WHERE prefix=:prefix"
        ),
        {"prefix": prefix},
    )


async def test_retained_first_page_does_not_starve_superseded_attempt_cleanup(
    db_session,
):
    fixture = await _seed_tracker_input(db_session, ready=False)
    first_claim = fixture["claim"]
    expired = await db_session.scalar(
        text("SELECT pg_catalog.clock_timestamp()-interval '1 second'")
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_inputs SET lease_expires_at=:expired "
            "WHERE input_id=:input"
        ),
        {"expired": expired, "input": fixture["input_id"]},
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_video_input_attempts SET lease_expires_at=:expired "
            "WHERE input_id=:input AND generation=:generation"
        ),
        {
            "expired": expired,
            "input": fixture["input_id"],
            "generation": first_claim.generation,
        },
    )
    second = await _tracker_native_call(
        db_session,
        "claim_video_tracker_input",
        tracker_input_execution.ClaimVideoInputRequest(
            worker_id=uuid.uuid4(), input_id=fixture["input_id"], lease_seconds=60
        ),
    )
    assert second.claim is not None
    assert second.claim.generation == first_claim.generation + 1
    plan, entries = _plan_for_claim(second.claim, frame_count=6)
    await _stage_times(db_session, second.claim, entries)
    ready = await _tracker_native_call(
        db_session,
        "finish_video_tracker_input",
        _finish_request(second.claim, plan),
    )
    assert ready.status == "ready"
    fixture["claim"] = second.claim
    current_run = await _step(
        db_session,
        "claim",
        {
            "job_id": str(fixture["job_id"]),
            "worker_id": str(uuid.uuid4()),
            "lease_seconds": 30,
        },
    )
    assert current_run["status"] == "ok", current_run
    # A stale unretired journal can retain the input ID, but the immutable
    # ready plan now points only at generation 2, never the abandoned root.
    await db_session.execute(
        text(
            "INSERT INTO aap_jobs_v1.video_execution_journals "
            "(job_id,run_id,input_id,writer_terminal,execution_retired) "
            "VALUES(:job,:run,:input,false,false)"
        ),
        {
            "job": fixture["job_id"],
            "run": uuid.uuid4(),
            "input": fixture["input_id"],
        },
    )
    await db_session.flush()
    assert await _delete(db_session, "project", fixture) == _blocked("project")

    older_prefix = await db_session.scalar(
        text(
            "SELECT prefix FROM public.asset_accepted_video_input_attempts "
            "WHERE input_id=:input AND generation=:generation"
        ),
        {"input": fixture["input_id"], "generation": first_claim.generation},
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_input_garbage "
            "SET not_before=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE prefix=:prefix"
        ),
        {"prefix": older_prefix},
    )
    current_prefix = await _queue_historical_tracker_input_root(db_session, fixture)
    assert current_prefix != older_prefix
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_input_garbage "
            "SET not_before=pg_catalog.clock_timestamp()-interval '2 seconds' "
            "WHERE prefix=:prefix"
        ),
        {"prefix": current_prefix},
    )
    await db_session.execute(
        text(
            "UPDATE public.asset_accepted_input_garbage "
            "SET not_before=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE prefix=:prefix"
        ),
        {"prefix": older_prefix},
    )

    database = _NativeVideoGarbageDatabase(db_session)
    request = tracker_input_execution.ClaimVideoGarbageRequest(
        worker_id=uuid.uuid4(), lease_seconds=60, page_size=1
    )
    first_page = await database.call("claim_accepted_video_input_garbage", request)
    current_row = (
        await db_session.execute(
            text(
                "SELECT lease_token,not_before FROM public.asset_accepted_input_garbage "
                "WHERE prefix=:prefix"
            ),
            {"prefix": current_prefix},
        )
    ).one()
    assert first_page.items == ()
    assert current_row.lease_token is None
    assert current_row.not_before > await db_session.scalar(
        text("SELECT pg_catalog.clock_timestamp()")
    )

    second_page = await database.call("claim_accepted_video_input_garbage", request)
    assert [item.prefix for item in second_page.items] == [older_prefix]
    storage = _RecordingVideoStorage()
    await storage.delete_attempt_versions(second_page.items[0].prefix)
    ack = await database.call(
        "complete_accepted_video_input_garbage",
        tracker_input_execution.CompleteVideoGarbageRequest(
            garbage_id=second_page.items[0].garbage_id,
            lease_token=second_page.items[0].lease_token,
        ),
    )
    assert ack.completed is True
    assert storage.deleted_prefixes == [older_prefix]


@pytest.mark.parametrize("kind", ["project", "unlink"])
@pytest.mark.parametrize("status", ["queued", "cancelled", "completed", "failed"])
async def test_never_started_no_execution_job_can_be_deleted(db_session, kind, status):
    fixture = await _seed_unretired(db_session, claimed=False, status=status)
    assert (await _delete(db_session, kind, fixture))["ok"] is True
    assert (
        await db_session.scalar(
            text("SELECT EXISTS(SELECT 1 FROM video_tracker_jobs WHERE id=:id)"),
            {"id": fixture["job_id"]},
        )
        is False
    )


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_cancel_and_native_retire_allow_deletion_with_a_live_lease(
    db_session, kind
):
    fixture = await _seed_unretired(db_session, bound=False)
    assert await _delete(db_session, kind, fixture) == _blocked(kind)
    await _retire(db_session, fixture)
    assert (
        await db_session.scalar(
            text(
                "SELECT lease_expires_at>clock_timestamp() FROM aap_jobs_v1.video_executions WHERE job_id=:id"
            ),
            {"id": fixture["job_id"]},
        )
        is True
    )
    operation = uuid.uuid4()
    first = await _delete(db_session, kind, fixture, operation_id=operation)
    assert first["ok"] is True, first
    assert await _delete(db_session, kind, fixture, operation_id=operation) == first
    assert (
        await db_session.scalar(
            text(
                "SELECT execution_retired FROM aap_jobs_v1.video_execution_journals WHERE job_id=:id"
            ),
            {"id": fixture["job_id"]},
        )
        is True
    )
    assert (
        await db_session.scalar(
            text("SELECT aap_jobs_v1.video_execution_input_retained(:id)"),
            {"id": fixture["input_id"]},
        )
        is False
    )


@pytest.mark.parametrize("kind", ["project", "unlink"])
@pytest.mark.parametrize("claim_exists", [False, True])
async def test_existing_orphan_scope_does_not_hide_unretired_or_unknown_execution(
    db_session, kind, claim_exists
):
    fixture = await _seed_unretired(db_session, claimed=claim_exists)
    await db_session.execute(
        text("DELETE FROM video_tracker_jobs WHERE id=:id"), {"id": fixture["job_id"]}
    )
    assert await _delete(db_session, kind, fixture) == _blocked(kind)
    assert (
        await db_session.scalar(
            text("SELECT EXISTS(SELECT 1 FROM tasks WHERE id=:id)"),
            {"id": fixture["task_id"]},
        )
        is True
    )


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_orphan_scope_after_old_task_cascade_is_still_guarded(db_session, kind):
    fixture = await _seed_unretired(db_session)
    await db_session.execute(
        text("DELETE FROM tasks WHERE id=:id"), {"id": fixture["task_id"]}
    )
    assert await _delete(db_session, kind, fixture) == _blocked(kind)


@pytest.mark.parametrize("kind", ["project", "unlink"])
@pytest.mark.parametrize("edge", ["annotation_fk", "source_sidecar"])
async def test_cross_task_annotation_edges_are_in_the_guard_footprint(
    db_session, kind, edge
):
    fixture = await _seed_unretired(db_session)
    project = await create_project(db_session, owner_id=fixture["actor_id"])
    target = await create_task(db_session, project_id=project.id)
    annotation = Annotation(
        task_id=target.id,
        project_id=target.project_id,
        user_id=fixture["actor_id"],
        class_name="target",
        geometry={"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1},
    )
    db_session.add(annotation)
    await db_session.flush()
    if edge == "annotation_fk":
        await db_session.execute(
            text(
                "UPDATE video_tracker_jobs SET annotation_id=:annotation WHERE id=:job"
            ),
            {"annotation": annotation.id, "job": fixture["job_id"]},
        )
    else:
        await db_session.execute(
            text(
                "INSERT INTO annotation_video_tracker_input_sources(annotation_id,tracker_job_id,input_id,task_id,project_id,request_digest,prompt_digest,plan_digest,timebase_digest,source_version_id,source_sha256,from_frame,to_frame,direction,grid_step) VALUES(:annotation,:job,:input,:task,:project,:digest,:digest,:digest,:digest,'test',:digest,0,5,'forward',1)"
            ),
            {
                "annotation": annotation.id,
                "job": fixture["job_id"],
                "input": fixture["input_id"],
                "task": fixture["task_id"],
                "project": fixture["project_id"],
                "digest": "a" * 64,
            },
        )
    item = await db_session.scalar(
        text("SELECT dataset_item_id FROM tasks WHERE id=:id"),
        {"id": fixture["task_id"]},
    )
    await db_session.execute(
        text("UPDATE tasks SET dataset_item_id=:item WHERE id=:id"),
        {"item": item, "id": target.id},
    )
    db_session.add(
        ProjectDataset(project_id=project.id, dataset_id=fixture["dataset_id"])
    )
    await db_session.flush()
    target_scope = {**fixture, "project_id": project.id, "task_id": target.id}
    assert await _delete(db_session, kind, target_scope) == _blocked(kind)
    assert (
        await db_session.scalar(
            text("SELECT EXISTS(SELECT 1 FROM annotations WHERE id=:id)"),
            {"id": annotation.id},
        )
        is True
    )
    await _assert_task_job_present(db_session, fixture)


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_unrelated_current_job_is_not_in_deletion_scope(db_session, kind):
    fixture = await _seed_unretired(db_session)
    project = await create_project(db_session, owner_id=fixture["actor_id"])
    db_session.add(
        ProjectDataset(project_id=project.id, dataset_id=fixture["dataset_id"])
    )
    await db_session.flush()
    assert (await _delete(db_session, kind, {**fixture, "project_id": project.id}))[
        "ok"
    ] is True
    await _assert_task_job_present(db_session, fixture)


@pytest.mark.parametrize("kind", ["project", "unlink"])
@pytest.mark.parametrize("locked", ["task", "job", "execution", "journal"])
async def test_guard_lock_contention_is_busy_and_preserves_the_graph(
    test_engine, kind, locked
):
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner.begin() as db:
        fixture = await _seed_unretired(db)
    table, column, identity = {
        "task": ("public.tasks", "id", fixture["task_id"]),
        "job": ("public.video_tracker_jobs", "id", fixture["job_id"]),
        "execution": ("aap_jobs_v1.video_executions", "job_id", fixture["job_id"]),
        "journal": (
            "aap_jobs_v1.video_execution_journals",
            "job_id",
            fixture["job_id"],
        ),
    }[locked]
    async with owner() as locker, owner() as caller:
        await locker.execute(
            text(f"SELECT {column} FROM {table} WHERE {column}=:id FOR UPDATE"),
            {"id": identity},
        )
        operation = uuid.uuid4()
        try:
            reply = await asyncio.wait_for(
                _delete(caller, kind, fixture, operation_id=operation), 5
            )
            assert reply == {
                "schema": _blocked(kind)["schema"],
                "ok": False,
                "error": {"code": "project_busy"},
            }
            await _assert_task_job_present(caller, fixture)
            await _no_deletion_receipts(caller, operation)
        finally:
            await caller.rollback()
            await locker.rollback()


@pytest.mark.parametrize("kind", ["project", "unlink"])
@pytest.mark.parametrize("finish", ["commit", "rollback"])
async def test_fk_admission_waits_for_deletion_and_cannot_escape_the_fence(
    test_engine, kind, finish
):
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner.begin() as db:
        fixture = await _seed_unretired(db, bound=False)
        await _retire(db, fixture)
        item = await db.scalar(
            text("SELECT dataset_item_id FROM tasks WHERE id=:id"),
            {"id": fixture["task_id"]},
        )
    ready = asyncio.Event()
    state = {}

    async def admit():
        async with owner() as db:
            state["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
            ready.set()
            db.add(
                VideoTrackerJob(
                    task_id=fixture["task_id"],
                    dataset_item_id=item,
                    created_by=fixture["actor_id"],
                    status="queued",
                    model_key="mock_bbox",
                    direction="forward",
                    from_frame=0,
                    to_frame=1,
                    prompt={},
                    event_channel="delete-fence-test",
                )
            )
            try:
                await db.commit()
                return "committed"
            except DBAPIError as exc:
                await db.rollback()
                return exc.orig.sqlstate

    async with owner() as deleting:
        assert (await _delete(deleting, kind, fixture))["ok"] is True
        pending = asyncio.create_task(admit())
        try:
            await asyncio.wait_for(ready.wait(), 5)
            async with owner() as inspect:
                async with asyncio.timeout(5):
                    while not await inspect.scalar(
                        text(
                            "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"
                        ),
                        {"pid": state["pid"]},
                    ):
                        assert not pending.done()
                        await asyncio.sleep(0.01)
            if finish == "commit":
                await deleting.commit()
            else:
                await deleting.rollback()
            assert await asyncio.wait_for(pending, 5) == (
                "23503" if finish == "commit" else "committed"
            )
        finally:
            await deleting.rollback()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("kind", ["project", "unlink"])
@pytest.mark.parametrize("finish", ["commit", "rollback"])
async def test_existing_queued_claim_is_fenced_until_deletion_finishes(
    test_engine, kind, finish
):
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner.begin() as db:
        fixture = await _seed_unretired(db, claimed=False, status="queued")
    async with owner() as deleting, owner() as claimant:
        assert (await _delete(deleting, kind, fixture))["ok"] is True
        command = {
            "job_id": str(fixture["job_id"]),
            "worker_id": str(uuid.uuid4()),
            "lease_seconds": 30,
        }
        first = await _step(claimant, "claim", command)
        assert first["status"] == "rejected" and first["reason"] == "execution_busy"
        await claimant.rollback()
        if finish == "commit":
            await deleting.commit()
        else:
            await deleting.rollback()
        second = await _step(claimant, "claim", command)
        if finish == "commit":
            assert second["status"] == "rejected" and second["reason"] == "job_missing"
        else:
            assert second["status"] == "ok" and second["execution"]["generation"] == 1
        await claimant.rollback()


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_proven_retired_orphan_execution_allows_deletion(db_session, kind):
    fixture = await _seed_unretired(db_session, bound=False)
    await _retire(db_session, fixture)
    await db_session.execute(
        text("DELETE FROM video_tracker_jobs WHERE id=:id"), {"id": fixture["job_id"]}
    )
    assert (await _delete(db_session, kind, fixture))["ok"] is True
    assert (
        await db_session.scalar(
            text(
                "SELECT execution_retired FROM aap_jobs_v1.video_execution_journals WHERE job_id=:id"
            ),
            {"id": fixture["job_id"]},
        )
        is True
    )


async def test_guard_functions_have_exact_owners_and_only_internal_execution(
    test_engine,
):
    async with async_sessionmaker(test_engine)() as db:
        for signature, owner in (
            (
                "aap_jobs_v1.guard_video_execution_deletion(uuid[],uuid[],uuid)",
                "jobs_code",
            ),
            ("aap_labeling_v1.guard_project_video_deletion(uuid)", "labeling_code"),
            (
                "aap_labeling_v1.guard_dataset_video_deletion(uuid,uuid)",
                "labeling_code",
            ),
        ):
            row = (
                await db.execute(
                    text(
                        "SELECT pg_get_userbyid(proowner),prosecdef,provolatile::text,proparallel::text,proconfig FROM pg_proc WHERE oid=CAST(:signature AS regprocedure)"
                    ),
                    {"signature": signature},
                )
            ).one()
            assert tuple(row[:4]) == (owner, True, "v", "u")
            assert row[4] == ["search_path=pg_catalog, pg_temp"]
            assert not await db.scalar(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_proc p,LATERAL aclexplode(p.proacl) a WHERE p.oid=CAST(:signature AS regprocedure) AND a.grantee=0 AND a.privilege_type='EXECUTE')"
                ),
                {"signature": signature},
            )
            for role in (
                "workspace_project_deletion_client",
                "workspace_dataset_unlink_client",
            ):
                assert not await db.scalar(
                    text(
                        "SELECT has_function_privilege(:role,CAST(:signature AS regprocedure),'EXECUTE')"
                    ),
                    {"role": role, "signature": signature},
                )
            allowed = "labeling_code" if owner == "jobs_code" else "workspace_code"
            assert (
                await db.scalar(
                    text(
                        "SELECT has_function_privilege(:role,CAST(:signature AS regprocedure),'EXECUTE')"
                    ),
                    {"role": allowed, "signature": signature},
                )
                is True
            )
        assert (
            await db.scalar(
                text(
                    "SELECT has_column_privilege('jobs_code','public.video_tracker_jobs','started_at','SELECT')"
                )
            )
            is True
        )
        assert (
            await db.scalar(
                text(
                    "SELECT has_column_privilege('jobs_code','public.video_tracker_jobs','task_id','SELECT')"
                )
            )
            is False
        )


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_never_started_deletion_does_not_poison_later_deletion(db_session, kind):
    fixture = await _seed_unretired(db_session, claimed=False, status="queued")
    assert (await _delete(db_session, "unlink", fixture))["ok"] is True
    assert (
        await db_session.scalar(
            text(
                "SELECT EXISTS(SELECT 1 FROM video_tracker_input_scopes WHERE tracker_job_id=:id)"
            ),
            {"id": fixture["job_id"]},
        )
        is True
    )
    if kind == "unlink":
        db_session.add(
            ProjectDataset(
                project_id=fixture["project_id"], dataset_id=fixture["dataset_id"]
            )
        )
        await db_session.flush()
    second = await _delete(db_session, kind, fixture)
    assert second["ok"] is True, second


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_blocked_group_does_not_write_partial_never_started_proofs(
    db_session, kind
):
    fixture = await _seed_unretired(db_session, claimed=False, status="queued")
    item = await db_session.scalar(
        text("SELECT dataset_item_id FROM tasks WHERE id=:id"),
        {"id": fixture["task_id"]},
    )
    other = VideoTrackerJob(
        task_id=fixture["task_id"],
        dataset_item_id=item,
        created_by=fixture["actor_id"],
        status="failed",
        started_at=datetime.now(timezone.utc),
        model_key="mock_bbox",
        direction="forward",
        from_frame=0,
        to_frame=1,
        prompt={},
        event_channel="delete-proof-test",
    )
    db_session.add(other)
    await db_session.flush()
    assert await _delete(db_session, kind, fixture) == _blocked(kind)
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM aap_jobs_v1.video_deletion_clearances WHERE job_id=ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [fixture["job_id"], other.id]},
        )
        == 0
    )


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_outer_rollback_restores_graph_and_removes_clearance(db_session, kind):
    fixture = await _seed_unretired(db_session, claimed=False, status="queued")
    operation = uuid.uuid4()
    transaction = await db_session.begin_nested()
    assert (await _delete(db_session, kind, fixture, operation_id=operation))[
        "ok"
    ] is True
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM aap_jobs_v1.video_deletion_clearances WHERE job_id=:job"
            ),
            {"job": fixture["job_id"]},
        )
        == 1
    )
    await transaction.rollback()
    await _assert_task_job_present(db_session, fixture)
    await _no_deletion_receipts(db_session, operation)
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM aap_jobs_v1.video_deletion_clearances WHERE job_id=:job"
            ),
            {"job": fixture["job_id"]},
        )
        == 0
    )


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_stale_never_started_clearance_cannot_override_execution(
    db_session, kind
):
    fixture = await _seed_unretired(db_session, claimed=True, bound=False)
    await db_session.execute(
        text("INSERT INTO aap_jobs_v1.video_deletion_clearances(job_id) VALUES(:id)"),
        {"id": fixture["job_id"]},
    )
    assert await _delete(db_session, kind, fixture) == _blocked(kind)
    await _assert_task_job_present(db_session, fixture)


async def test_downgrade_refuses_to_drop_committed_clearance_evidence(test_engine):
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner.begin() as db:
        fixture = await _seed_unretired(db, claimed=False, status="queued")
        assert (await _delete(db, "unlink", fixture))["ok"] is True
    path = (
        Path(__file__).parents[1] / "alembic/versions/0300_video_deletion_retirement.py"
    )
    spec = importlib.util.spec_from_file_location(
        "owned_video_deletion_downgrade", path
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def downgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()

    with pytest.raises(DBAPIError) as rejected:
        async with test_engine.begin() as connection:
            await connection.run_sync(downgrade)
    assert rejected.value.orig.sqlstate == "55000"
    async with owner() as db:
        assert (
            await db.scalar(
                text(
                    "SELECT count(*) FROM aap_jobs_v1.video_deletion_clearances WHERE job_id=:id"
                ),
                {"id": fixture["job_id"]},
            )
            == 1
        )
        assert (
            await db.scalar(
                text(
                    "SELECT to_regprocedure('aap_jobs_v1.guard_video_execution_deletion(uuid[],uuid[],uuid)') IS NOT NULL"
                )
            )
            is True
        )


def _function_url(test_engine, kind):
    key = (
        "WORKSPACE_PROJECT_DELETION"
        if kind == "project"
        else "WORKSPACE_DATASET_UNLINK"
    )
    role = (
        "workspace_project_deletion_client"
        if kind == "project"
        else "workspace_dataset_unlink_client"
    )
    url = make_url(os.environ[f"AAP_MODULE_DATA_{key}_URL"])
    assert url.username == role
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return role, url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_portable_function_only_client_preserves_retirement_failure(
    test_engine, kind
):
    from aap_workspace.dataset_unlink import DatasetUnlinkManager
    from aap_workspace.dataset_unlink_contract import (
        DatasetUnlinkCommand,
        DatasetUnlinkFailure,
    )
    from aap_workspace.dataset_unlink_postgres import DatasetUnlinkPostgresData
    from aap_workspace.project_deletion import ProjectDeletionManager
    from aap_workspace.project_deletion_contract import (
        ProjectDeletionCommand,
        ProjectDeletionFailure,
    )
    from aap_workspace.project_deletion_postgres import ProjectDeletionPostgresData

    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner.begin() as db:
        fixture = await _seed_unretired(db)
    _, url = _function_url(test_engine, kind)
    connection = await asyncpg.connect(url, timeout=5, command_timeout=10)
    operation = uuid.uuid4()
    common = {
        "operation_id": operation,
        "actor_id": fixture["actor_id"],
        "project_id": fixture["project_id"],
        "audit_context": None,
    }
    try:
        async with connection.transaction():
            if kind == "project":
                with pytest.raises(ProjectDeletionFailure) as blocked:
                    await ProjectDeletionManager(
                        ProjectDeletionPostgresData(connection)
                    ).execute(ProjectDeletionCommand(**common))
            else:
                with pytest.raises(DatasetUnlinkFailure) as blocked:
                    await DatasetUnlinkManager(
                        DatasetUnlinkPostgresData(connection)
                    ).execute(
                        DatasetUnlinkCommand(
                            **common,
                            dataset_id=fixture["dataset_id"],
                            cleanup_bucket="video-delete-test",
                        )
                    )
            assert blocked.value.code == "video_execution_not_retired"
    finally:
        await connection.close()
    async with owner() as db:
        await _assert_task_job_present(db, fixture)
        await _no_deletion_receipts(db, operation)


@pytest.mark.parametrize("kind", ["project", "unlink"])
async def test_node_function_only_caller_can_commit_rejection_without_partial_deletion(
    test_engine, kind
):
    owner = async_sessionmaker(test_engine, expire_on_commit=False)
    async with owner.begin() as db:
        fixture = await _seed_unretired(db)
    role, url = _function_url(test_engine, kind)
    operation = uuid.uuid4()
    command = {
        name: str(fixture[name]) for name in ("actor_id", "project_id", "dataset_id")
    }
    command["operation_id"] = str(operation)
    process = await asyncio.create_subprocess_exec(
        "node",
        str(Path(__file__).parent / "fixtures/video_deletion_retirement_peer.mjs"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            process.communicate(
                json.dumps(
                    {
                        "database_url": url,
                        "role": role,
                        "kind": kind,
                        "command": command,
                    }
                ).encode()
            ),
            20,
        )
        assert process.returncode == 0, err.decode()
        assert json.loads(out) == _blocked(kind)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    async with owner() as db:
        await _assert_task_job_present(db, fixture)
        await _no_deletion_receipts(db, operation)
