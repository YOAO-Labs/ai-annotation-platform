"""Deletion must preserve canonical Jobs until their execution is retired."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
