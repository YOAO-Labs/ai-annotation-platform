"""Native claim generations and conservative video route recovery fences."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_video_tracker_input_native import _capture, _seed_tracker_job

pytestmark = pytest.mark.asyncio

_STEP = text("SELECT aap_jobs_v1.video_execution_step(:action, :arguments)")
_ROUTING_STEP = text(
    "SELECT aap_inference_routing_v1.routing_step(:action, :arguments)"
)


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


async def _step(db, action: str, arguments: dict):
    raw = await db.scalar(
        _STEP,
        {
            "action": action,
            "arguments": json.dumps(arguments, separators=(",", ":")),
        },
    )
    return _decode(raw)


async def _routing_step(db, action: str, arguments: dict):
    raw = await db.scalar(
        _ROUTING_STEP,
        {
            "action": action,
            "arguments": json.dumps(arguments, separators=(",", ":")),
        },
    )
    return _decode(raw)


def _proof(job_id, claim):
    return {
        "job_id": str(job_id),
        "run_id": claim["run_id"],
        "worker_id": claim["worker_id"],
        "generation": claim["generation"],
        "lease_token": claim["lease_token"],
    }


async def _install_bound_route_intent(db, job_id, claim):
    request_id = uuid.uuid4()
    scope_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO aap_jobs_v1.video_execution_route_intents ("
            "request_id,job_id,run_id,generation,command_sha256,command_ciphertext,"
            "scope_id,state,realm) VALUES ("
            ":request,:job,:run,:generation,:digest,"
            "public.pgp_sym_encrypt('{}',pg_catalog.encode("
            "aap_jobs_v1._video_execution_secret(),'hex'),'cipher-algo=aes256'),"
            ":scope,'bound',aap_jobs_v1._video_execution_realm())"
        ),
        {
            "request": request_id,
            "job": job_id,
            "run": uuid.UUID(claim["run_id"]),
            "generation": claim["generation"],
            "digest": uuid.uuid4().hex * 2,
            "scope": scope_id,
        },
    )
    await db.execute(
        text(
            "UPDATE aap_jobs_v1.video_executions SET route_request_id=:request, "
            "lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE job_id=:job"
        ),
        {"request": request_id, "job": job_id},
    )
    return request_id, scope_id


async def _install_ready_tracker_scope(db, job_id, claim):
    request_id, scope_id = await _install_bound_route_intent(db, job_id, claim)
    digest = await db.scalar(
        text(
            "SELECT command_sha256 FROM aap_jobs_v1.video_execution_route_intents "
            "WHERE request_id=:request"
        ),
        {"request": request_id},
    )
    realm = await db.scalar(text("SELECT aap_inference_routing_v1._routing_realm()"))
    await db.execute(
        text(
            "INSERT INTO aap_inference_routing_v1.scopes ("
            "id,command_sha256,registry_id,pool_id,project_id,mode,owner,operation,"
            "generation,config_revision,eligibility_revision,authority_epoch,realm,"
            "state,closed,heartbeat_deadline) VALUES ("
            ":id,:digest,:registry,NULL,NULL,'direct','probe','probe',NULL,1,1,"
            ":epoch,:realm,'active',false,"
            "pg_catalog.clock_timestamp()+interval '1 minute')"
        ),
        {
            "id": scope_id,
            "digest": digest,
            "registry": uuid.uuid4(),
            "epoch": uuid.uuid4(),
            "realm": realm,
        },
    )
    await db.execute(
        text(
            "UPDATE aap_inference_routing_v1.scopes "
            "SET owner=:owner,operation='tracker' WHERE id=:scope"
        ),
        {"owner": f"tracker:{job_id}", "scope": scope_id},
    )
    await db.execute(
        text(
            "UPDATE aap_jobs_v1.video_executions "
            "SET lease_expires_at=pg_catalog.clock_timestamp()+interval '1 minute' "
            "WHERE job_id=:job"
        ),
        {"job": job_id},
    )
    capability = await db.scalar(
        text("SELECT aap_inference_routing_v1._routing_scope_capability(:scope)"),
        {"scope": scope_id},
    )
    assert isinstance(capability, str) and len(capability) == 64
    return {"request_id": request_id, "scope_id": scope_id, "capability": capability}


async def _seed_claimable_input(db, actor_id, project_id, item_id, task_id, job_id):
    request_digest = "a" * 64
    prompt_digest = "b" * 64
    scope = {
        "tracker_job_id": str(job_id),
        "origin_actor_id": str(actor_id),
        "request_digest": request_digest,
        "prompt_digest": prompt_digest,
        "task": {
            "task_id": str(task_id),
            "project_id": str(project_id),
            "dataset_item_id": str(item_id),
        },
    }
    await db.execute(
        text(
            "INSERT INTO public.video_tracker_input_scopes ("
            "tracker_job_id,input_id,task_id,project_id,origin_actor_id,"
            "request_digest,prompt_digest,scope) VALUES ("
            ":job,:input,:task,:project,:actor,:request,:prompt,CAST(:scope AS jsonb))"
        ),
        {
            "job": job_id,
            "input": uuid.uuid4(),
            "task": task_id,
            "project": project_id,
            "actor": actor_id,
            "request": request_digest,
            "prompt": prompt_digest,
            "scope": json.dumps(scope, separators=(",", ":")),
        },
    )


async def _cleanup_claimed_scope(
    test_engine, *, actor_id, project_id, dataset_id, item_id, task_id, job_id, scope_id
):
    async with test_engine.begin() as cleanup:
        await cleanup.execute(
            text("DELETE FROM aap_inference_routing_v1.attempts WHERE scope_id=:scope"),
            {"scope": scope_id},
        )
        await cleanup.execute(
            text("DELETE FROM aap_inference_routing_v1.scopes WHERE id=:scope"),
            {"scope": scope_id},
        )
        await cleanup.execute(
            text(
                "DELETE FROM aap_jobs_v1.video_execution_route_intents "
                "WHERE job_id=:job"
            ),
            {"job": job_id},
        )
        await cleanup.execute(
            text("DELETE FROM aap_jobs_v1.video_executions WHERE job_id=:job"),
            {"job": job_id},
        )
        await cleanup.execute(
            text(
                "DELETE FROM public.video_tracker_input_scopes "
                "WHERE tracker_job_id=:job"
            ),
            {"job": job_id},
        )
        await cleanup.execute(
            text("DELETE FROM public.video_tracker_jobs WHERE id=:job"),
            {"job": job_id},
        )
        await cleanup.execute(
            text("DELETE FROM public.tasks WHERE id=:task"), {"task": task_id}
        )
        await cleanup.execute(
            text("DELETE FROM public.dataset_items WHERE id=:item"), {"item": item_id}
        )
        await cleanup.execute(
            text("DELETE FROM public.datasets WHERE id=:dataset"),
            {"dataset": dataset_id},
        )
        await cleanup.execute(
            text("DELETE FROM public.projects WHERE id=:project"),
            {"project": project_id},
        )
        await cleanup.execute(
            text("DELETE FROM public.users WHERE id=:actor"), {"actor": actor_id}
        )


async def test_video_execution_takeover_keeps_logical_run_and_fences_old_claim(
    db_session,
):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    await _capture(db_session, actor, job.id)
    first = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    assert first["status"] == "ok"
    first_claim = first["execution"]
    assert first_claim["generation"] == 1
    assert first_claim["state"] == "ready"

    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await db_session.flush()
    await db_session.execute(
        text(
            "UPDATE aap_jobs_v1.video_executions "
            "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE job_id=:job"
        ),
        {"job": job.id},
    )

    second = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    assert second["status"] == "ok"
    second_claim = second["execution"]
    assert second_claim["generation"] == 2
    assert second_claim["run_id"] == first_claim["run_id"]
    assert second_claim["lease_token"] != first_claim["lease_token"]
    assert second_claim["state"] == "ready"

    stale = await _step(
        db_session, "guard", {**_proof(job.id, first_claim), "purpose": "stage"}
    )
    assert stale == {
        "schema": "aap-video-execution-data/v1",
        "status": "rejected",
        "reason": "lease_lost",
    }

    released = await _step(db_session, "release", _proof(job.id, second_claim))
    assert released == {
        "schema": "aap-video-execution-data/v1",
        "status": "ok",
        "released": True,
    }


async def test_video_execution_recovery_claim_cannot_resume_result_writes(db_session):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    await _capture(db_session, actor, job.id)
    first = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    first_claim = first["execution"]
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await db_session.flush()
    await _install_bound_route_intent(db_session, job.id, first_claim)

    recovered = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    assert recovered["status"] == "ok"
    claim = recovered["execution"]
    assert claim["run_id"] == first_claim["run_id"]
    assert claim["generation"] == first_claim["generation"] + 1
    assert claim["state"] == "recovery_required"

    job.status = "running"
    await db_session.flush()
    for purpose in ("start", "progress", "mask_root", "stage"):
        response = await _step(
            db_session, "guard", {**_proof(job.id, claim), "purpose": purpose}
        )
        assert response["status"] == "rejected"
        assert response["reason"] == "recovery_required"

    cleanup = await _step(
        db_session, "guard", {**_proof(job.id, claim), "purpose": "cancel"}
    )
    assert cleanup["status"] == "ok"
    assert cleanup["guarded"] is True


async def test_video_execution_claim_rejects_legacy_running_job(db_session):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    await _capture(db_session, actor, job.id)
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await db_session.flush()

    response = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    assert response == {
        "schema": "aap-video-execution-data/v1",
        "status": "rejected",
        "reason": "legacy_execution_unbound",
    }


async def test_cancelled_current_generation_can_finish_partial_stage(db_session):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    await _capture(db_session, actor, job.id)
    claimed = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    claim = claimed["execution"]
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await db_session.flush()
    job.cancel_requested_at = datetime.now(timezone.utc)
    job.status = "cancelled"
    await db_session.flush()

    renewed = await _step(
        db_session,
        "renew",
        {**_proof(job.id, claim), "lease_seconds": 30},
    )
    assert renewed["status"] == "ok"
    for purpose in ("progress", "stage", "cancel", "fail"):
        response = await _step(
            db_session, "guard", {**_proof(job.id, claim), "purpose": purpose}
        )
        assert response["status"] == "ok"
        assert response["guarded"] is True

    mask_root = await _step(
        db_session, "guard", {**_proof(job.id, claim), "purpose": "mask_root"}
    )
    assert mask_root["status"] == "rejected"
    assert mask_root["reason"] == "job_cancelled"


async def test_cancelled_current_scope_allows_heartbeat_but_denies_new_attempt(
    db_session,
):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    claimable_input = await db_session.execute(
        text(
            "SELECT task.project_id,task.dataset_item_id "
            "FROM public.tasks AS task WHERE task.id=:task"
        ),
        {"task": job.task_id},
    )
    project_id, item_id = claimable_input.one()
    await _seed_claimable_input(
        db_session, actor, project_id, item_id, job.task_id, job.id
    )
    claimed = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    claim = claimed["execution"]
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await db_session.flush()
    route = await _install_ready_tracker_scope(db_session, job.id, claim)
    attempt_id = uuid.uuid4()
    identity_hash = "c" * 64
    await db_session.execute(
        text(
            "INSERT INTO aap_inference_routing_v1.attempts ("
            "id,scope_id,method,path,transport_identity_sha256,state) "
            "VALUES (:id,:scope,'POST','/predict',:identity,'active')"
        ),
        {"id": attempt_id, "scope": route["scope_id"], "identity": identity_hash},
    )
    job.cancel_requested_at = datetime.now(timezone.utc)
    job.status = "cancelled"
    await db_session.flush()

    assert (
        await db_session.scalar(
            text("SELECT aap_jobs_v1._video_execution_scope_is_current(:scope,true)"),
            {"scope": route["scope_id"]},
        )
        is True
    )
    assert (
        await db_session.scalar(
            text("SELECT aap_jobs_v1._video_execution_scope_is_current(:scope,false)"),
            {"scope": route["scope_id"]},
        )
        is False
    )
    heartbeat = await _routing_step(
        db_session,
        "heartbeat_scope",
        {
            "scope_id": str(route["scope_id"]),
            "capability": route["capability"],
            "ttl_ms": 30_000,
        },
    )
    assert heartbeat["status"] == "ok"
    begin_replay = await _routing_step(
        db_session,
        "begin_attempt",
        {
            "scope_id": str(route["scope_id"]),
            "capability": route["capability"],
            "attempt_id": str(attempt_id),
            "method": "POST",
            "path": "/predict",
            "transport_identity": {"identity_sha256": identity_hash},
        },
    )
    assert begin_replay["status"] == "rejected"
    assert begin_replay["reason"] == "scope_unavailable"

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "INSERT INTO aap_inference_routing_v1.attempts ("
                    "id,scope_id,method,path,transport_identity_sha256,state) "
                    "VALUES (:id,:scope,'POST','/predict',:identity,'active')"
                ),
                {
                    "id": uuid.uuid4(),
                    "scope": route["scope_id"],
                    "identity": "c" * 64,
                },
            )


async def test_video_execution_lock_contention_returns_management_busy(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id = project_id = dataset_id = item_id = task_id = job_id = scope_id = None
    route = None
    blocker = None
    blocker_transaction = None
    try:
        async with maker.begin() as setup:
            actor_id, project, item_id, task_id, job = await _seed_tracker_job(setup)
            project_id = project.id
            dataset_id = await setup.scalar(
                text("SELECT dataset_id FROM public.dataset_items WHERE id=:item"),
                {"item": item_id},
            )
            job_id = job.id
            await _seed_claimable_input(
                setup, actor_id, project.id, item_id, task_id, job_id
            )
            claimed = await _step(
                setup,
                "claim",
                {
                    "job_id": str(job_id),
                    "worker_id": str(uuid.uuid4()),
                    "lease_seconds": 30,
                },
            )
            job.status = "running"
            job.started_at = datetime.now(timezone.utc)
            await setup.flush()
            route = await _install_ready_tracker_scope(
                setup, job_id, claimed["execution"]
            )
            scope_id = route["scope_id"]

        blocker = await test_engine.connect()
        blocker_transaction = await blocker.begin()
        await blocker.execute(
            text(
                "SELECT job_id FROM aap_jobs_v1.video_executions "
                "WHERE job_id=:job FOR UPDATE"
            ),
            {"job": job_id},
        )
        async with test_engine.connect() as caller:
            response = await _routing_step(
                caller,
                "heartbeat_scope",
                {
                    "scope_id": str(scope_id),
                    "capability": route["capability"],
                    "ttl_ms": 30_000,
                },
            )
        assert response["status"] == "rejected"
        assert response["reason"] == "management_busy"
    finally:
        if blocker_transaction is not None and blocker_transaction.is_active:
            await blocker_transaction.rollback()
        if blocker is not None:
            await blocker.close()
        if all(
            value is not None
            for value in (
                actor_id,
                project_id,
                dataset_id,
                item_id,
                task_id,
                job_id,
                scope_id,
            )
        ):
            await _cleanup_claimed_scope(
                test_engine,
                actor_id=actor_id,
                project_id=project_id,
                dataset_id=dataset_id,
                item_id=item_id,
                task_id=task_id,
                job_id=job_id,
                scope_id=scope_id,
            )


async def test_deleted_job_can_only_recover_its_retained_route_intent(db_session):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    await _capture(db_session, actor, job.id)
    claimed = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    claim = claimed["execution"]
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await db_session.flush()
    route_request_id, route_scope_id = await _install_bound_route_intent(
        db_session, job.id, claim
    )
    await db_session.delete(job)
    await db_session.flush()

    cleanup_claim = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    assert cleanup_claim["status"] == "ok"
    cleanup_proof = _proof(job.id, cleanup_claim["execution"])
    assert cleanup_claim["execution"]["state"] == "recovery_required"
    renewed = await _step(
        db_session,
        "renew",
        {**cleanup_proof, "lease_seconds": 30},
    )
    assert renewed["status"] == "ok"
    assert renewed["execution"]["state"] == "recovery_required"
    route_status = _decode(
        await db_session.scalar(
            text("SELECT aap_jobs_v1._video_execution_route_step('status',:arguments)"),
            {"arguments": json.dumps(cleanup_proof, separators=(",", ":"))},
        )
    )
    assert route_status["ok"] is True
    assert route_status["intent"]["scope_id"] == str(route_scope_id)

    write_guard = await _step(
        db_session, "guard", {**cleanup_proof, "purpose": "stage"}
    )
    assert write_guard["status"] == "rejected"
    assert write_guard["reason"] == "job_missing"


async def test_failed_job_can_claim_only_bound_route_cleanup(db_session):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    await _capture(db_session, actor, job.id)
    initial = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    initial_claim = initial["execution"]
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await db_session.flush()
    await _install_bound_route_intent(db_session, job.id, initial_claim)
    job.status = "failed"
    job.completed_at = datetime.now(timezone.utc)
    await db_session.flush()

    cleanup = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    assert cleanup["status"] == "ok"
    cleanup_claim = cleanup["execution"]
    assert cleanup_claim["state"] == "recovery_required"
    assert cleanup_claim["generation"] == initial_claim["generation"] + 1
    for purpose in ("start", "progress", "mask_root", "stage"):
        denied = await _step(
            db_session,
            "guard",
            {**_proof(job.id, cleanup_claim), "purpose": purpose},
        )
        assert denied["status"] == "rejected"


async def test_pending_review_job_can_claim_only_bound_route_cleanup(db_session):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    await _capture(db_session, actor, job.id)
    initial = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    initial_claim = initial["execution"]
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await db_session.flush()
    await _install_bound_route_intent(db_session, job.id, initial_claim)
    job.status = "pending_review"
    job.completed_at = datetime.now(timezone.utc)
    await db_session.flush()

    cleanup = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    assert cleanup["status"] == "ok"
    cleanup_claim = cleanup["execution"]
    assert cleanup_claim["state"] == "recovery_required"
    assert cleanup_claim["generation"] == initial_claim["generation"] + 1
    renewed = await _step(
        db_session,
        "renew",
        {**_proof(job.id, cleanup_claim), "lease_seconds": 30},
    )
    assert renewed["status"] == "ok"
    assert renewed["execution"]["state"] == "recovery_required"


async def test_terminal_job_without_bound_intent_cannot_be_reclaimed(db_session):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    await _capture(db_session, actor, job.id)
    initial = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    await db_session.flush()
    job.status = "failed"
    job.completed_at = datetime.now(timezone.utc)
    await db_session.flush()
    await db_session.execute(
        text(
            "UPDATE aap_jobs_v1.video_executions "
            "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE job_id=:job"
        ),
        {"job": job.id},
    )

    rejected = await _step(
        db_session,
        "claim",
        {"job_id": str(job.id), "worker_id": str(uuid.uuid4()), "lease_seconds": 30},
    )
    assert rejected["status"] == "rejected"
    assert rejected["reason"] == "job_not_active"
    assert initial["execution"]["state"] == "ready"


@pytest.mark.parametrize("outcome", ("unknown", "rejected", "scope_exists"))
async def test_release_distinguishes_proven_no_scope_rejection_from_uncertainty(
    db_session, outcome
):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    job_id = job.id
    await _capture(db_session, actor, job_id)
    claim = (
        await _step(
            db_session,
            "claim",
            {
                "job_id": str(job_id),
                "worker_id": str(uuid.uuid4()),
                "lease_seconds": 120,
            },
        )
    )["execution"]
    if outcome == "scope_exists":
        route = await _install_ready_tracker_scope(db_session, job_id, claim)
        request_id, scope_id = route["request_id"], route["scope_id"]
    else:
        request_id, scope_id = await _install_bound_route_intent(
            db_session, job_id, claim
        )
    await db_session.execute(
        text(
            "UPDATE aap_jobs_v1.video_execution_route_intents SET state='prepared' "
            "WHERE request_id=:request"
        ),
        {"request": request_id},
    )
    if outcome != "unknown":
        await db_session.execute(
            text(
                "INSERT INTO aap_inference_routing_v1.command_receipts "
                "(id,command_sha256,operation,result,creating_xid,realm) "
                "SELECT request_id,command_sha256,'video_acquire',"
                '\'{"reason":"pool_not_enabled"}\'::jsonb,'
                "pg_catalog.pg_current_xact_id(),aap_inference_routing_v1._routing_realm() "
                "FROM aap_jobs_v1.video_execution_route_intents WHERE request_id=:request"
            ),
            {"request": request_id},
        )
    reply = await _step(db_session, "release", _proof(job_id, claim))
    if outcome == "rejected":
        assert reply == {
            "schema": "aap-video-execution-data/v1",
            "status": "ok",
            "released": True,
        }
        assert (
            await db_session.scalar(
                text(
                    "SELECT lease_expires_at <= pg_catalog.clock_timestamp() "
                    "FROM aap_jobs_v1.video_executions WHERE job_id=:job"
                ),
                {"job": job_id},
            )
            is True
        )
    else:
        assert reply["reason"] == "recovery_required"
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM aap_jobs_v1.video_execution_route_intents WHERE request_id=:request"
            ),
            {"request": request_id},
        )
        == 1
    )


async def test_successful_acquire_receipt_cannot_recover_a_superseded_generation(
    db_session,
):
    actor, _, _, _, job = await _seed_tracker_job(db_session)
    job_id = job.id
    await _capture(db_session, actor, job_id)
    claim = (
        await _step(
            db_session,
            "claim",
            {
                "job_id": str(job_id),
                "worker_id": str(uuid.uuid4()),
                "lease_seconds": 120,
            },
        )
    )["execution"]
    route = await _install_ready_tracker_scope(db_session, job_id, claim)
    await db_session.execute(
        text("UPDATE public.video_tracker_jobs SET status='running' WHERE id=:job"),
        {"job": job_id},
    )
    await db_session.execute(
        text(
            "INSERT INTO aap_inference_routing_v1.command_receipts "
            "(id,command_sha256,operation,result,creating_xid,realm) "
            "SELECT request_id,command_sha256,'video_acquire',"
            "aap_inference_routing_v1._routing_scope_view(scope_id,true),"
            "pg_catalog.pg_current_xact_id(),aap_inference_routing_v1._routing_realm() "
            "FROM aap_jobs_v1.video_execution_route_intents WHERE request_id=:request"
        ),
        {"request": route["request_id"]},
    )
    digest = await db_session.scalar(
        text(
            "SELECT command_sha256 FROM aap_jobs_v1.video_execution_route_intents WHERE request_id=:request"
        ),
        {"request": route["request_id"]},
    )
    arguments = {"request_id": str(route["request_id"]), "command_sha256": digest}
    assert (await _routing_step(db_session, "command_status", arguments))[
        "status"
    ] == "ok"
    await db_session.execute(
        text(
            "UPDATE aap_inference_routing_v1.scopes SET state='reserved', "
            "heartbeat_deadline=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE id=:scope"
        ),
        {"scope": route["scope_id"]},
    )
    expired = await _routing_step(db_session, "command_status", arguments)
    assert expired["status"] == "rejected" and expired["reason"] == "scope_unavailable"
    await db_session.execute(
        text(
            "UPDATE aap_inference_routing_v1.scopes SET state='active' WHERE id=:scope"
        ),
        {"scope": route["scope_id"]},
    )
    # Active work does not disappear when its ordinary heartbeat TTL elapses.
    assert (await _routing_step(db_session, "command_status", arguments))[
        "status"
    ] == "ok"
    await db_session.execute(
        text(
            "UPDATE aap_jobs_v1.video_executions SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE job_id=:job"
        ),
        {"job": job_id},
    )
    replacement = (
        await _step(
            db_session,
            "claim",
            {
                "job_id": str(job_id),
                "worker_id": str(uuid.uuid4()),
                "lease_seconds": 120,
            },
        )
    )["execution"]
    assert replacement["generation"] == 2
    assert replacement["state"] == "recovery_required"
    stale = await _routing_step(db_session, "command_status", arguments)
    assert stale["status"] == "rejected" and stale["reason"] == "scope_unavailable"
