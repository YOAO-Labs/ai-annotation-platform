from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.video_tracker_job import VideoTrackerJob
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.factory import create_project, create_task, create_user

pytestmark = pytest.mark.asyncio

_PREPARE = text("SELECT aap_jobs_v1.prepare_dispatch(CAST(:request AS jsonb))")
_CLAIM = text("SELECT aap_jobs_v1.claim_dispatches(CAST(:request AS jsonb))")
_FINISH = text("SELECT aap_jobs_v1.finish_dispatch(CAST(:request AS jsonb))")
_STATUS = text("SELECT aap_jobs_v1.dispatch_status(CAST(:intent_id AS uuid))")


def _dispatch(execution_id: str) -> dict:
    return {
        "schema": "aap-jobs-queue/v2",
        "operation": "dispatch",
        "task_name": "app.workers.tasks.batch_predict",
        "execution_id": execution_id,
        "args": ["project-id", "backend-id", ["task-id"]],
        "kwargs": {"manifest": {"items": [False, None, "雪"]}},
        "options": {"queue": "cpu"},
    }


def _canvas(execution_id: str) -> dict:
    callback = _dispatch(execution_id)
    child = _dispatch(str(uuid.uuid4()))
    return {
        "schema": "aap-jobs-queue/v2",
        "operation": "canvas",
        "execution_id": execution_id,
        "group_id": str(uuid.uuid4()),
        "tasks": [
            {
                key: value
                for key, value in child.items()
                if key not in {"schema", "operation"}
            }
        ],
        "callback": {
            key: value
            for key, value in callback.items()
            if key not in {"schema", "operation"}
        },
    }


def _job(*, status: str = "pending", kind: str = "batch_predict") -> AsyncJob:
    return AsyncJob(
        id=uuid.uuid4(),
        kind=kind,
        status=status,
        progress_pct=0,
        payload={},
        result={},
    )


async def _prepare(db, job_id: uuid.UUID, envelope: dict, *, purpose="execution"):
    request = {
        "intent_id": None,
        "job_id": str(job_id),
        "purpose": purpose,
        "envelope": envelope,
    }
    return await db.scalar(
        _PREPARE,
        {"request": json.dumps(request, ensure_ascii=False)},
    )


async def _claim(db, *, limit: int = 10, lease_seconds: int = 30):
    return await db.scalar(
        _CLAIM,
        {"request": json.dumps({"limit": limit, "lease_seconds": lease_seconds})},
    )


async def _finish(db, lease: dict, *, accepted: bool):
    request = {
        "intent_id": lease["intent_id"],
        "lease_token": lease["lease_token"],
        "generation": lease["generation"],
        "accepted": accepted,
    }
    return await db.scalar(_FINISH, {"request": json.dumps(request)})


def _data(reply: dict) -> dict | list | None:
    assert reply["schema"] == "aap-jobs-dispatch-ledger/v1"
    return reply["data"]


def _refused(reply: dict, code: str) -> None:
    assert reply == {
        "schema": "aap-jobs-dispatch-ledger/v1",
        "ok": False,
        "data": None,
        "error": {"code": code},
    }


async def test_prepare_is_rollback_safe_and_rejects_immutable_identity_rewrites(
    db_session,
):
    job = _job()
    db_session.add(job)
    await db_session.flush()
    envelope = _dispatch(str(uuid.uuid4()))

    savepoint = await db_session.begin_nested()
    rolled_back = _data(await _prepare(db_session, job.id, envelope))
    assert rolled_back["envelope"] == envelope
    await savepoint.rollback()
    status = await db_session.scalar(_STATUS, {"intent_id": rolled_back["intent_id"]})
    assert _data(status) is None

    first = _data(await _prepare(db_session, job.id, envelope))
    replay = _data(await _prepare(db_session, job.id, envelope))
    assert replay == first
    assert replay["lease_token"] == first["lease_token"]

    changed = {**envelope, "kwargs": {"manifest": {"changed": True}}}
    conflict = await _prepare(db_session, job.id, changed)
    _refused(conflict, "identity_mismatch")
    stored = await db_session.scalar(
        text(
            "SELECT envelope FROM aap_jobs_v1.dispatch_intents "
            "WHERE intent_id=CAST(:intent_id AS uuid)"
        ),
        {"intent_id": first["intent_id"]},
    )
    assert stored == envelope
    count = await db_session.scalar(
        text(
            "SELECT pg_catalog.count(*) FROM aap_jobs_v1.dispatch_intents "
            "WHERE job_id=CAST(:job_id AS uuid)"
        ),
        {"job_id": job.id},
    )
    assert count == 1


async def test_unknown_publication_reclaims_same_wire_ids_and_fences_old_acknowledgements(
    db_session,
):
    job = _job()
    db_session.add(job)
    await db_session.flush()
    envelope = _dispatch(str(uuid.uuid4()))
    inline = _data(await _prepare(db_session, job.id, envelope))

    await db_session.execute(
        text(
            "UPDATE aap_jobs_v1.dispatch_intents "
            "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE intent_id=CAST(:intent_id AS uuid)"
        ),
        {"intent_id": inline["intent_id"]},
    )
    first_claim = _data(await _claim(db_session, limit=1))
    assert len(first_claim) == 1
    claimed = first_claim[0]
    assert claimed["intent_id"] == inline["intent_id"]
    assert claimed["execution_id"] == inline["execution_id"]
    assert claimed["envelope"] == inline["envelope"] == envelope
    assert claimed["generation"] == inline["generation"] + 1
    assert claimed["lease_token"] != inline["lease_token"]

    _refused(await _finish(db_session, inline, accepted=True), "stale_lease")
    pending = _data(await _finish(db_session, claimed, accepted=False))
    assert pending["state"] == "pending"

    backoff = _data(await _prepare(db_session, job.id, envelope))
    assert backoff["state"] == "pending"
    assert backoff["lease_token"] is None

    await db_session.execute(
        text(
            "UPDATE aap_jobs_v1.dispatch_intents "
            "SET next_attempt_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE intent_id=CAST(:intent_id AS uuid)"
        ),
        {"intent_id": inline["intent_id"]},
    )
    retry_claim = _data(await _claim(db_session, limit=1))
    assert len(retry_claim) == 1
    retry = retry_claim[0]
    assert retry["intent_id"] == inline["intent_id"]
    assert retry["envelope"] == envelope
    assert retry["generation"] == claimed["generation"] + 1
    submitted = _data(await _finish(db_session, retry, accepted=True))
    assert submitted["state"] == "submitted"
    status = await db_session.scalar(_STATUS, {"intent_id": inline["intent_id"]})
    assert _data(status)["state"] == "submitted"
    assert _data(await _claim(db_session)) == []


async def test_concurrent_dispatch_claims_are_single_winner_and_skip_locked(
    test_engine,
):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    job = _job()
    envelope = _dispatch(str(uuid.uuid4()))
    try:
        async with sessions() as setup:
            setup.add(job)
            await setup.flush()
            inline = _data(await _prepare(setup, job.id, envelope))
            await setup.commit()
        async with sessions() as setup:
            await setup.execute(
                text(
                    "UPDATE aap_jobs_v1.dispatch_intents "
                    "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
                    "WHERE intent_id=CAST(:intent_id AS uuid)"
                ),
                {"intent_id": inline["intent_id"]},
            )
            await setup.commit()

        async def compete():
            async with sessions() as contender:
                async with contender.begin():
                    return await _claim(contender, limit=1)

        replies = await asyncio.gather(compete(), compete())
        batches = [_data(reply) for reply in replies]
        assert all(isinstance(batch, list) for batch in batches)
        leases = [lease for batch in batches for lease in batch]
        assert len(leases) == 1
        assert leases[0]["intent_id"] == inline["intent_id"]
        assert leases[0]["envelope"] == envelope
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                text("DELETE FROM public.async_jobs WHERE id=:job_id"),
                {"job_id": job.id},
            )
            await cleanup.commit()


@pytest.mark.parametrize(
    ("job_kind", "summary_kind"),
    [("tracking", "video_tracker"), ("correction", "video_correction")],
)
async def test_frame_canvas_requires_running_and_cancelled_tracker_retirement_is_scoped(
    db_session,
    job_kind,
    summary_kind,
):
    pending_job = _job()
    failed_job = _job(status="failed")
    db_session.add_all([pending_job, failed_job])
    await db_session.flush()

    canvas = _canvas(str(uuid.uuid4()))
    _refused(
        await _prepare(db_session, pending_job.id, canvas, purpose="frame_canvas"),
        "job_not_dispatchable",
    )
    pending_job.status = "running"
    await db_session.flush()
    running_canvas = _data(
        await _prepare(db_session, pending_job.id, canvas, purpose="frame_canvas")
    )
    assert running_canvas["state"] == "leased"

    ordinary = _dispatch(str(uuid.uuid4()))
    _refused(
        await _prepare(db_session, failed_job.id, ordinary), "job_not_dispatchable"
    )
    _refused(
        await _prepare(
            db_session,
            failed_job.id,
            ordinary,
            purpose="tracker_retirement",
        ),
        "tracker_retirement_not_allowed",
    )
    recoverable_job = _job()
    db_session.add(recoverable_job)
    await db_session.flush()
    recoverable_envelope = _dispatch(str(uuid.uuid4()))
    stale_dispatch = _data(
        await _prepare(db_session, recoverable_job.id, recoverable_envelope)
    )
    recoverable_job.status = "failed"
    await db_session.flush()
    await db_session.execute(
        text(
            "UPDATE aap_jobs_v1.dispatch_intents "
            "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE intent_id=CAST(:intent_id AS uuid)"
        ),
        {"intent_id": stale_dispatch["intent_id"]},
    )

    suffix = uuid.uuid4().hex[:12]
    actor = await create_user(
        db_session,
        "project_admin",
        f"dispatch-retirement-{suffix}@example.test",
        "Dispatch retirement",
    )
    project = await create_project(db_session, owner_id=actor.id)
    task = await create_task(db_session, project_id=project.id)
    dataset = Dataset(
        id=uuid.uuid4(),
        display_id=f"DR-{suffix[:8]}",
        name=f"Dispatch retirement {suffix}",
        data_type="video",
        created_by=actor.id,
    )
    db_session.add(dataset)
    await db_session.flush()
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="retirement.mp4",
        file_path=f"dispatch-retirement/{suffix}.mp4",
        file_type="video",
    )
    db_session.add(item)
    await db_session.flush()

    source_id = None
    if job_kind == "correction":
        from app.db.models.annotation import Annotation

        source = Annotation(
            task_id=task.id,
            project_id=project.id,
            user_id=actor.id,
            annotation_type="bbox",
            class_name="object",
            tool_unit_id="bbox",
            geometry={"type": "bbox", "x": 0, "y": 0, "w": 1, "h": 1},
        )
        db_session.add(source)
        await db_session.flush()
        source_id = source.id
    tracker_id = uuid.uuid4()
    tracker = VideoTrackerJob(
        id=tracker_id,
        task_id=task.id,
        dataset_item_id=item.id,
        created_by=actor.id,
        status="cancelled",
        job_kind=job_kind,
        annotation_id=source_id,
        track_id_snapshot="dispatch-retirement" if source_id else None,
        correction_frame=0 if source_id else None,
        model_key="fixture-model",
        direction="forward",
        from_frame=0,
        to_frame=1,
        event_channel=f"dispatch-retirement:{suffix}",
    )
    tracker_job = AsyncJob(
        id=tracker_id,
        kind=summary_kind,
        status="cancelled",
        progress_pct=0,
        payload={},
        result={},
    )
    db_session.add_all([tracker, tracker_job])
    await db_session.flush()

    retirement = _dispatch(str(uuid.uuid4()))
    retirement["task_name"] = "app.workers.video_tracker.run_video_tracker_job"
    retirement["args"] = [str(tracker_id)]
    retirement["kwargs"] = {}
    wrong_task = {**retirement, "task_name": "app.workers.tasks.batch_predict"}
    _refused(
        await _prepare(
            db_session, tracker_id, wrong_task, purpose="tracker_retirement"
        ),
        "tracker_retirement_not_allowed",
    )
    wrong_target = {**retirement, "args": [str(uuid.uuid4())]}
    _refused(
        await _prepare(
            db_session, tracker_id, wrong_target, purpose="tracker_retirement"
        ),
        "tracker_retirement_not_allowed",
    )
    terminal_lease = _data(
        await _prepare(
            db_session,
            tracker_id,
            retirement,
            purpose="tracker_retirement",
        )
    )
    assert terminal_lease["state"] == "leased"
    await db_session.execute(
        text(
            "UPDATE aap_jobs_v1.dispatch_intents "
            "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE intent_id=CAST(:intent_id AS uuid)"
        ),
        {"intent_id": terminal_lease["intent_id"]},
    )
    redrive = _data(await _claim(db_session, limit=1))
    assert len(redrive) == 1
    assert redrive[0]["purpose"] == "tracker_retirement"
    assert redrive[0]["envelope"] == retirement
    retired = await db_session.scalar(
        _STATUS, {"intent_id": stale_dispatch["intent_id"]}
    )
    assert _data(retired)["state"] == "retired"


@pytest.mark.parametrize("invalid", ["empty", "duplicate", "group", "callback"])
async def test_native_rejects_unpublishable_canvas_without_saving_an_intent(
    db_session, invalid
):
    job = _job(status="running")
    db_session.add(job)
    await db_session.flush()
    envelope = _canvas(str(uuid.uuid4()))
    if invalid == "empty":
        envelope["tasks"] = []
    elif invalid == "duplicate":
        envelope["tasks"].append(dict(envelope["tasks"][0]))
    elif invalid == "group":
        envelope["tasks"][0]["execution_id"] = envelope["group_id"]
    else:
        envelope["tasks"][0]["execution_id"] = envelope["execution_id"]
    _refused(
        await _prepare(db_session, job.id, envelope, purpose="frame_canvas"),
        "invalid_request",
    )
    assert not await db_session.scalar(
        text(
            "SELECT EXISTS(SELECT FROM aap_jobs_v1.dispatch_intents "
            "WHERE job_id=:job_id)"
        ),
        {"job_id": job.id},
    )


async def test_relay_claims_just_before_publish_and_survives_a_competing_ack(
    db_session,
):
    from aap_jobs.dispatch_relay import JobsDispatchRelay
    from app.services.jobs_dispatch import jobs_dispatch_data

    jobs = [_job() for _ in range(3)]
    db_session.add_all(jobs)
    await db_session.flush()
    intents = []
    for job in jobs:
        lease = _data(await _prepare(db_session, job.id, _dispatch(str(uuid.uuid4()))))
        await _finish(db_session, lease, accepted=False)
        intents.append(lease["intent_id"])
    await db_session.execute(
        text(
            "UPDATE aap_jobs_v1.dispatch_intents "
            "SET next_attempt_at=pg_catalog.clock_timestamp()-interval '1 second' "
            "WHERE intent_id=ANY(CAST(:ids AS uuid[]))"
        ),
        {"ids": intents},
    )
    published = []

    class Publisher:
        async def publish_envelope(self, envelope):
            active = await db_session.scalar(
                text(
                    "SELECT pg_catalog.count(*) FROM aap_jobs_v1.dispatch_intents "
                    "WHERE intent_id=ANY(CAST(:ids AS uuid[])) AND state='leased'"
                ),
                {"ids": intents},
            )
            assert active == 1
            if not published:
                await db_session.execute(
                    text(
                        "UPDATE aap_jobs_v1.dispatch_intents "
                        "SET next_attempt_at=pg_catalog.clock_timestamp()+interval '1 hour' "
                        "WHERE state='pending' AND intent_id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": intents},
                )
                await db_session.execute(
                    text(
                        "UPDATE aap_jobs_v1.dispatch_intents "
                        "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
                        "WHERE execution_id=:execution_id"
                    ),
                    {"execution_id": envelope["execution_id"]},
                )
                competitor = _data(await _claim(db_session, limit=1))[0]
                assert competitor["execution_id"] == envelope["execution_id"]
                await _finish(db_session, competitor, accepted=True)
                await db_session.execute(
                    text(
                        "UPDATE aap_jobs_v1.dispatch_intents "
                        "SET next_attempt_at=pg_catalog.clock_timestamp()-interval '1 second' "
                        "WHERE state='pending' AND intent_id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": intents},
                )
            published.append(envelope["execution_id"])
            return envelope["execution_id"]

    relay = JobsDispatchRelay(jobs_dispatch_data(db_session), Publisher(), batch_size=3)
    assert await relay.run_once() == 2
    assert len(set(published)) == 3
    for intent_id in intents:
        status = _data(await db_session.scalar(_STATUS, {"intent_id": intent_id}))
        assert status["state"] == "submitted"


async def test_locked_terminal_intent_does_not_block_live_dispatch(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    terminal, live = _job(), _job()
    try:
        async with sessions() as setup:
            setup.add_all([terminal, live])
            await setup.flush()
            terminal_lease = _data(
                await _prepare(setup, terminal.id, _dispatch(str(uuid.uuid4())))
            )
            await _prepare(setup, live.id, _dispatch(str(uuid.uuid4())))
            terminal.status = "failed"
            await setup.flush()
            await setup.execute(
                text(
                    "UPDATE aap_jobs_v1.dispatch_intents "
                    "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
                    "WHERE job_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [terminal.id, live.id]},
            )
            await setup.commit()
        async with sessions() as blocker:
            await blocker.execute(
                text(
                    "SELECT FROM aap_jobs_v1.dispatch_intents "
                    "WHERE intent_id=CAST(:intent_id AS uuid) FOR UPDATE"
                ),
                {"intent_id": terminal_lease["intent_id"]},
            )
            async with sessions() as claimant:
                claims = _data(await asyncio.wait_for(_claim(claimant, limit=1), 2))
                assert [row["job_id"] for row in claims] == [str(live.id)]
                await claimant.commit()
            await blocker.rollback()
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                text(
                    "DELETE FROM public.async_jobs WHERE id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [terminal.id, live.id]},
            )
            await cleanup.commit()


@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires verifier-owned API and dispatch-relay LOGINs",
)
async def test_api_accepts_and_relay_recovers_without_private_ledger_privileges(
    test_engine,
):
    import asyncpg
    from sqlalchemy.engine import make_url

    from aap_jobs.dispatch_postgres import JobsDispatchPostgres
    from app.config import settings
    from tests.test_asset_storage_cleanup_runtime import _verify_postgres

    _verify_postgres(test_engine.url)
    api_url = make_url(settings.database_url).set(drivername="postgresql")
    relay_url = make_url(os.environ["AAP_MODULE_DATA_JOBS_DISPATCH_RELAY_URL"])
    expected = (test_engine.url.host, test_engine.url.port, test_engine.url.database)
    assert (api_url.host, api_url.port, api_url.database) == expected
    assert (relay_url.host, relay_url.port, relay_url.database) == expected
    assert len({api_url.username, relay_url.username, test_engine.url.username}) == 3
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    job = _job()
    try:
        async with sessions() as setup:
            setup.add(job)
            await setup.commit()
        async with await JobsDispatchPostgres.connect(
            api_url.render_as_string(hide_password=False)
        ) as api:
            accepted = await api.client.prepare(job.id, _dispatch(str(uuid.uuid4())))
        async with sessions() as owner:
            await owner.execute(
                text(
                    "UPDATE aap_jobs_v1.dispatch_intents "
                    "SET lease_expires_at=pg_catalog.clock_timestamp()-interval '1 second' "
                    "WHERE intent_id=:intent_id"
                ),
                {"intent_id": accepted.intent_id},
            )
            await owner.commit()
        async with await JobsDispatchPostgres.connect(
            relay_url.render_as_string(hide_password=False)
        ) as relay:
            lease = (await relay.client.claim(limit=1))[0]
            assert lease.envelope == accepted.envelope
            submitted = await relay.client.finish(lease, accepted=True)
            assert submitted.state == "submitted"
            assert (await relay.client.status(lease.intent_id)).state == "submitted"

        for url, denied in (
            (
                relay_url,
                (
                    "SELECT * FROM aap_jobs_v1.dispatch_intents",
                    "SELECT * FROM public.async_jobs",
                    "SELECT aap_jobs_v1.prepare_dispatch('{}'::jsonb)",
                    "CREATE TABLE public.jobs_relay_escape(id integer)",
                ),
            ),
            (
                api_url,
                (
                    "SELECT * FROM aap_jobs_v1.dispatch_intents",
                    "SELECT aap_jobs_v1.claim_dispatches('{}'::jsonb)",
                ),
            ),
        ):
            connection = await asyncpg.connect(
                url.render_as_string(hide_password=False)
            )
            try:
                for statement in denied:
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await connection.execute(statement)
            finally:
                await connection.close()
    finally:
        async with sessions() as cleanup:
            await cleanup.execute(
                text("DELETE FROM public.async_jobs WHERE id=:job_id"),
                {"job_id": job.id},
            )
            await cleanup.commit()
