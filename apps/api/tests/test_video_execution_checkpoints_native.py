"""Native terminal retention rules for durable video window journals."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from tests.test_video_execution_native import _install_ready_tracker_scope, _step
from tests.test_video_tracker_input_native import _capture, _seed_tracker_job

pytestmark = pytest.mark.asyncio


async def _claimed_running_job(db):
    actor, _, _, task_id, job = await _seed_tracker_job(db)
    await _capture(db, actor, job.id)
    response = await _step(
        db,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    assert response["status"] == "ok"
    claim = response["execution"]
    job.status = "running"
    await db.flush()
    input_id = await db.scalar(
        text(
            "SELECT input_id FROM public.video_tracker_input_scopes "
            "WHERE tracker_job_id=:job"
        ),
        {"job": job.id},
    )
    project_id = await db.scalar(
        text("SELECT project_id FROM public.tasks WHERE id=:task"),
        {"task": task_id},
    )
    await db.execute(
        text(
            "INSERT INTO aap_jobs_v1.video_execution_journals "
            "(job_id,run_id,input_id,task_id,project_id,source_item_id) VALUES "
            "(:job,:run,:input,:task,:project,:source)"
        ),
        {
            "job": job.id,
            "run": uuid.UUID(claim["run_id"]),
            "input": input_id,
            "task": task_id,
            "project": project_id,
            "source": job.dataset_item_id,
        },
    )
    return job, input_id, claim


async def test_cancelled_job_with_unretired_route_preserves_checkpoint_pins(db_session):
    job, input_id, claim = await _claimed_running_job(db_session)
    await _install_ready_tracker_scope(db_session, job.id, claim)

    job.cancel_requested_at = await db_session.scalar(
        text("SELECT pg_catalog.clock_timestamp()")
    )
    job.status = "cancelled"
    await db_session.flush()

    journal = (
        await db_session.execute(
            text(
                "SELECT writer_terminal,execution_retired "
                "FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
            ),
            {"job": job.id},
        )
    ).one()
    assert journal == (False, False)
    assert (
        await db_session.scalar(
            text("SELECT aap_jobs_v1.video_execution_input_retained(:input)"),
            {"input": input_id},
        )
        is True
    )
    assert (
        await db_session.scalar(
            text("SELECT aap_jobs_v1.video_execution_running_mask_roots_live(:job)"),
            {"job": job.id},
        )
        is True
    )


async def test_failed_job_without_route_intents_retires_checkpoint_pins(db_session):
    job, input_id, _ = await _claimed_running_job(db_session)
    job.status = "failed"
    await db_session.flush()

    journal = (
        await db_session.execute(
            text(
                "SELECT writer_terminal,writer_terminal_outcome,execution_retired "
                "FROM aap_jobs_v1.video_execution_journals WHERE job_id=:job"
            ),
            {"job": job.id},
        )
    ).one()
    assert journal == (True, "failed", True)
    assert (
        await db_session.scalar(
            text("SELECT aap_jobs_v1.video_execution_input_retained(:input)"),
            {"input": input_id},
        )
        is False
    )


@pytest.mark.parametrize(
    "root_kind", ["video_tracker_running_outputs", "video_tracker_input_scopes"]
)
async def test_unknown_legacy_tracker_roots_preserve_fail_closed_policy(
    db_session, root_kind
):
    request = {"kind": root_kind, "job": str(uuid.uuid4())}
    for fail_closed in (True, False):
        assert (
            await db_session.scalar(
                text(
                    "SELECT aap_labeling_v1._mask_gc_root_entry_live("
                    ":kind,:job,NULL,:fail_closed)"
                ),
                {**request, "fail_closed": fail_closed},
            )
            is fail_closed
        )
