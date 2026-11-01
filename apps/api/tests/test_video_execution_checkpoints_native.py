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


@pytest.mark.parametrize(
    "open_artifact", ["none", "route", "journal", "attempt", "capture"]
)
async def test_cancelled_replay_requires_all_owner_artifacts_closed(
    db_session, open_artifact
):
    job, input_id, claim = await _claimed_running_job(db_session)
    route = await _install_ready_tracker_scope(db_session, job.id, claim)
    job.status = "cancelled"
    await db_session.flush()
    if open_artifact != "route":
        await db_session.execute(
            text(
                "UPDATE aap_inference_routing_v1.scopes SET state='reconciled',"
                "closed=true WHERE id=:scope"
            ),
            {"scope": route["scope_id"]},
        )
        await db_session.execute(
            text(
                "INSERT INTO aap_inference_routing_v1.scope_retirements "
                "(retirement_id,scope_id,registry_id,authority_epoch,"
                "backend_incarnation,challenge,fence_until,admission_expires_at,"
                "transport_identity_sha256,config_revision,realm,phase) "
                "SELECT :retirement,id,registry_id,authority_epoch,:incarnation,"
                ":digest,0,pg_catalog.clock_timestamp(),:digest,config_revision,"
                "realm,'completed' FROM aap_inference_routing_v1.scopes WHERE id=:scope"
            ),
            {
                "retirement": uuid.uuid4(),
                "scope": route["scope_id"],
                "incarnation": uuid.uuid4(),
                "digest": "a" * 64,
            },
        )
    if open_artifact != "journal":
        await db_session.execute(
            text(
                "UPDATE aap_jobs_v1.video_execution_journals SET writer_terminal=true,"
                "writer_terminal_outcome='cancelled',execution_retired=true WHERE job_id=:job"
            ),
            {"job": job.id},
        )
    if open_artifact in {"attempt", "capture"}:
        capture_id = uuid.uuid4()
        params = {
            "job": job.id,
            "run": uuid.UUID(claim["run_id"]),
            "generation": claim["generation"],
            "capture": capture_id,
            "digest": "b" * 64,
        }
        await db_session.execute(
            text(
                "INSERT INTO aap_jobs_v1.video_execution_window_attempts "
                "(job_id,run_id,generation,window_ordinal,window_digest,mode,state,capture_id) "
                "VALUES (:job,:run,:generation,0,:digest,'execute',:state,:capture)"
            ),
            {**params, "state": "capturing" if open_artifact == "capture" else "started"},
        )
        if open_artifact == "capture":
            await db_session.execute(
                text(
                    "INSERT INTO aap_jobs_v1.video_execution_captures "
                    "(capture_id,job_id,run_id,generation,window_ordinal,"
                    "manifest_digest,manifest,state) VALUES "
                    "(:capture,:job,:run,:generation,0,:digest,'{}'::jsonb,'receiving')"
                ),
                params,
            )
    request = {
        "job_id": str(job.id),
        "worker_id": str(uuid.uuid4()),
        "lease_seconds": 30,
    }
    # An extant lease always wins even over otherwise complete retirement.
    busy = await _step(db_session, "claim", request)
    assert busy["reason"] == "execution_busy"
    await db_session.execute(
        text(
            "UPDATE aap_jobs_v1.video_executions SET "
            "lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE job_id=:job"
        ),
        {"job": job.id},
    )
    response = await _step(db_session, "claim", request)
    if open_artifact == "none":
        assert response["settled"] == {"job_id": str(job.id)}
        assert (
            await db_session.scalar(
                text("SELECT aap_jobs_v1.video_execution_input_retained(:input)"),
                {"input": input_id},
            )
            is False
        )
        stale = await _step(
            db_session,
            "release",
            {key: claim[key] for key in (
                "job_id", "run_id", "worker_id", "generation", "lease_token"
            )},
        )
        assert stale["reason"] == "lease_lost"
        assert (await _step(db_session, "claim", request))["settled"] == {
            "job_id": str(job.id)
        }
    else:
        if open_artifact == "journal":
            assert (
                await db_session.scalar(
                    text("SELECT aap_jobs_v1.video_execution_input_retained(:input)"),
                    {"input": input_id},
                )
                is True
            )
        assert response["execution"]["state"] == "recovery_required"
