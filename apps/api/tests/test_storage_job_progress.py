"""Actual checkpoint observations are fenced, transactional and safe to expose."""

import copy
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.db.models.async_job import AsyncJob
from app.schemas.async_job import AsyncJobOut
from app.services import async_job as jobs
from app.services import storage_job_progress as progress


async def running(db, *, kind="storage_usage"):
    job = await jobs.create_job(db, kind=kind, celery_task_id=str(uuid.uuid4()))
    await jobs.mark_running(db, job.id)
    await db.flush()
    return job


async def observe(db, job, **kwargs):
    return await progress.record(
        db,
        job,
        task_id=kwargs.pop("task_id", job.celery_task_id),
        stage=kwargs.pop("stage", "enumerating"),
        unit=kwargs.pop("unit", "pages"),
        completed=kwargs.pop("completed", 1),
        marker=kwargs.pop("marker", "page-1"),
        **kwargs,
    )


async def test_checkpoint_preserves_pending_payload_result_and_repeated_percent(
    db_session,
):
    job = await running(db_session)
    job.payload, job.result = {"owner_pending": True}, {"page": 1}
    assert await observe(db_session, job)
    first = job.last_progress_at
    assert job.progress_pct == 0 and job.progress_observation["sequence"] == 1
    assert job.payload == {"owner_pending": True} and job.result == {"page": 1}
    await db_session.flush()
    await db_session.refresh(job)
    assert job.payload == {"owner_pending": True} and job.result == {"page": 1}
    assert await observe(db_session, job, marker="page-2", completed=2)
    assert job.last_progress_at >= first and job.progress_observation["sequence"] == 2
    assert job.progress_pct == 0


async def test_duplicate_and_older_native_sequence_do_not_refresh(db_session):
    job = await running(db_session)
    assert await observe(db_session, job, sequence=4)
    first = (job.last_progress_at, copy.deepcopy(job.progress_observation))
    for values in (
        {"sequence": 5},
        {"marker": "older", "sequence": 3},
        {"marker": "equal", "sequence": 4},
    ):
        assert not await observe(db_session, job, **values)
        assert (job.last_progress_at, job.progress_observation) == first
    assert await observe(db_session, job, marker="new", sequence=5)


@pytest.mark.parametrize(
    "mutate",
    [
        "caller_terminal",
        "db_terminal",
        "caller_task",
        "db_task",
        "unsupported",
        "bad_pair",
        "negative",
        "bool",
        "bad_sequence",
    ],
)
async def test_current_caller_and_locked_database_are_both_required(db_session, mutate):
    job = await running(db_session)
    task, kwargs = job.celery_task_id, {}
    if mutate == "caller_terminal":
        job.status = "completed"
    elif mutate == "db_terminal":
        await db_session.execute(
            update(AsyncJob)
            .where(AsyncJob.id == job.id)
            .values(status="cancelled")
            .execution_options(synchronize_session=False)
        )
    elif mutate == "caller_task":
        job.celery_task_id = "other"
    elif mutate == "db_task":
        await db_session.execute(
            update(AsyncJob)
            .where(AsyncJob.id == job.id)
            .values(celery_task_id="other")
            .execution_options(synchronize_session=False)
        )
    elif mutate == "unsupported":
        job.kind = "training_run"
    elif mutate == "bad_pair":
        kwargs["stage"] = "copying"
    elif mutate == "negative":
        kwargs["completed"] = -1
    elif mutate == "bool":
        kwargs["completed"] = True
    else:
        kwargs["sequence"] = 0
    assert not await observe(db_session, job, task_id=task, **kwargs)
    assert job.last_progress_at is None and job.progress_observation is None


async def test_transaction_rollback_reverts_observation_with_checkpoint(db_session):
    job = await running(db_session)
    await db_session.flush()
    savepoint = await db_session.begin_nested()
    job.result = {"checkpoint": 2}
    assert await observe(db_session, job)
    await db_session.flush()
    await savepoint.rollback()
    await db_session.refresh(job)
    assert (
        job.result == {}
        and job.last_progress_at is None
        and job.progress_observation is None
    )


async def test_generic_percentage_running_and_lease_payload_are_not_progress(
    db_session,
):
    job = await running(db_session)
    await jobs.update_progress(
        db_session, job.id, 75, extra_payload={"lease": "changed"}
    )
    await jobs.mark_running(db_session, job.id)
    await db_session.flush()
    await db_session.refresh(job)
    assert job.last_progress_at is None and job.progress_observation is None


async def test_backwards_database_clock_never_replaces_future_fact(db_session):
    job = await running(db_session)
    assert await observe(db_session, job)
    job.last_progress_at = datetime.now(timezone.utc) + timedelta(days=1)
    await db_session.flush()
    old = (job.last_progress_at, copy.deepcopy(job.progress_observation))
    assert not await observe(db_session, job, marker="later")
    assert (job.last_progress_at, job.progress_observation) == old


def public_job():
    now = datetime.now(timezone.utc)
    job_id, task = uuid.uuid4(), str(uuid.uuid4())
    value = {
        "schema": 1,
        "execution_id": str(progress.execution_id(job_id, task)),
        "stage": "enumerating",
        "unit": "pages",
        "completed": 2,
        "sequence": 2,
        "source": {"task_id": task, "marker_sha256": "a" * 64},
        "observed_at": "1900-01-01",
        "idle_seconds": 999999,
        "attention_after_seconds": 1,
        "secret": "must-not-leak",
    }
    return SimpleNamespace(
        id=job_id,
        kind="storage_usage",
        project_id=None,
        user_id=None,
        status="running",
        progress_pct=0,
        payload={},
        result={},
        error_message=None,
        celery_task_id=task,
        started_at=now - timedelta(hours=1),
        completed_at=None,
        created_at=now - timedelta(hours=1),
        updated_at=now,
        last_progress_at=now - timedelta(seconds=950),
        progress_observation=value,
    )


@pytest.mark.parametrize(
    "status", ["pending", "running", "completed", "cancelled", "failed"]
)
def test_public_projection_uses_server_time_and_only_safe_fields(status):
    job = public_job()
    job.status = status
    before = datetime.now(timezone.utc)
    out = AsyncJobOut.model_validate(job).model_dump(mode="json", by_alias=True)
    obs = out["progress_observation"]
    assert 950 <= obs["idle_seconds"] < 960
    assert obs["attention_after_seconds"] == 900
    assert datetime.fromisoformat(obs["observed_at"]) >= before
    assert set(obs) == {
        "schema",
        "execution_id",
        "stage",
        "unit",
        "completed",
        "sequence",
        "observed_at",
        "idle_seconds",
        "attention_after_seconds",
    }
    assert "must-not-leak" not in str(out)


@pytest.mark.parametrize(
    "mutation",
    [
        "null",
        "other_execution",
        "other_task",
        "unsupported",
        "negative",
        "boolean",
        "stage",
        "marker",
        "missing_time",
        "naive_time",
        "shape",
    ],
)
def test_invalid_or_old_observations_clear_both_public_fields(mutation):
    job = public_job()
    if mutation == "null":
        job.progress_observation = None
    elif mutation == "other_execution":
        job.progress_observation["execution_id"] = str(uuid.uuid4())
    elif mutation == "other_task":
        job.celery_task_id = "new"
    elif mutation == "unsupported":
        job.kind = "training_run"
    elif mutation == "negative":
        job.progress_observation["completed"] = -1
    elif mutation == "boolean":
        job.progress_observation["sequence"] = True
    elif mutation == "stage":
        job.progress_observation["stage"] = ["enumerating"]
    elif mutation == "marker":
        job.progress_observation["source"]["marker_sha256"] = "invalid"
    elif mutation == "missing_time":
        job.last_progress_at = None
    elif mutation == "naive_time":
        job.last_progress_at = datetime.now()
    else:
        job.progress_observation = []
    value = AsyncJobOut.model_validate(job)
    assert value.last_progress_at is None and value.progress_observation is None


def test_future_clock_is_unknown_instead_of_negative_idle():
    job = public_job()
    job.last_progress_at = datetime.now(timezone.utc) + timedelta(days=1)
    out = AsyncJobOut.model_validate(job)
    assert out.progress_observation.idle_seconds is None


async def test_get_and_list_only_project_without_updating_stored_facts(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    job = await running(db_session)
    job.user_id = user.id
    await db_session.flush()
    assert await observe(db_session, job)
    await db_session.flush()
    await db_session.refresh(job)
    original = (
        job.last_progress_at,
        copy.deepcopy(job.progress_observation),
        job.updated_at,
    )
    headers = {"Authorization": f"Bearer {token}"}
    for path in (f"/api/v1/async-jobs/{job.id}", "/api/v1/async-jobs"):
        response = await httpx_client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        item = (
            body
            if "items" not in body
            else next(v for v in body["items"] if v["id"] == str(job.id))
        )
        assert item["progress_observation"]["completed"] == 1
        assert "source" not in item["progress_observation"]
    current = (
        await db_session.execute(
            select(
                AsyncJob.last_progress_at,
                AsyncJob.progress_observation,
                AsyncJob.updated_at,
            ).where(AsyncJob.id == job.id)
        )
    ).one()
    assert tuple(current) == original
