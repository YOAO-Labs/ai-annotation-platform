"""Current platform authority applies to every verification job read/cancel path."""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core import security
from app.db.models.api_key import ApiKey
from app.db.models.notification import Notification
from app.services import async_job as jobs
from app.services.async_job_notify import notify_job_terminal

KIND = "managed_original_verification"


@pytest.mark.parametrize("identity", ["super_admin", "project_admin", "annotator"])
async def test_verification_jobs_require_current_platform_administrator(
    db_session, httpx_client, super_admin, project_admin, annotator, identity
):
    actor, token = {
        "super_admin": super_admin,
        "project_admin": project_admin,
        "annotator": annotator,
    }[identity]
    job = await jobs.create_job(db_session, kind=KIND, user_id=actor.id)
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}
    expected = 200 if identity == "super_admin" else 403
    detail = await httpx_client.get(f"/api/v1/async-jobs/{job.id}", headers=headers)
    assert detail.status_code == expected
    listing = await httpx_client.get(f"/api/v1/async-jobs?kind={KIND}", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["total"] == (1 if identity == "super_admin" else 0)
    if identity != "super_admin":
        response = await httpx_client.post(
            f"/api/v1/async-jobs/{job.id}/cancel", headers=headers
        )
        assert response.status_code == 403


async def test_downgraded_original_job_owner_loses_all_verification_access(
    db_session, httpx_client, super_admin
):
    actor, token = super_admin
    job = await jobs.create_job(db_session, kind=KIND, user_id=actor.id)
    actor.role = "project_admin"
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}
    assert (
        await httpx_client.get(f"/api/v1/async-jobs/{job.id}", headers=headers)
    ).status_code == 403
    assert (
        await httpx_client.post(f"/api/v1/async-jobs/{job.id}/cancel", headers=headers)
    ).status_code == 403
    assert (
        await httpx_client.get(f"/api/v1/async-jobs?kind={KIND}", headers=headers)
    ).json()["total"] == 0


@pytest.mark.parametrize("scopes", [[], ["datasets:read", "datasets:write"], ["*"]])
async def test_verification_job_key_scope_and_running_cancel_request(
    db_session, httpx_client, super_admin, monkeypatch, scopes
):
    from app.services import managed_original_verification as verification

    actor, _ = super_admin
    secret = f"ak_{uuid.uuid4().hex}"
    db_session.add(
        ApiKey(
            user_id=actor.id,
            name="Verification job test",
            key_prefix=secret[:12],
            key_hash=security.hash_password(secret),
            scopes=scopes,
        )
    )
    job = await jobs.create_job(db_session, kind=KIND, user_id=actor.id)
    job.status = "running"
    await db_session.flush()
    await db_session.refresh(job)
    called = []

    async def cancel(db, job_id):
        assert db is db_session and job_id == job.id
        called.append(job_id)
        return "cancel_requested"

    monkeypatch.setattr(verification, "cancel", cancel)
    headers = {"Authorization": f"Bearer {secret}"}
    allowed = scopes == ["*"]
    response = await httpx_client.get(f"/api/v1/async-jobs/{job.id}", headers=headers)
    assert response.status_code == (200 if allowed else 403)
    response = await httpx_client.get(
        f"/api/v1/async-jobs?kind={KIND}", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["total"] == (1 if allowed else 0)
    response = await httpx_client.post(
        f"/api/v1/async-jobs/{job.id}/cancel", headers=headers
    )
    assert response.status_code == (200 if allowed else 403)
    assert len(called) == int(allowed)
    if allowed:
        assert response.json() == {"status": "cancel_requested", "id": str(job.id)}
        assert job.status == "running"


async def test_verification_completion_notification_contains_only_safe_summary(
    db_session, super_admin
):
    actor, _ = super_admin
    job = await jobs.create_job(
        db_session,
        kind=KIND,
        user_id=actor.id,
        payload={
            "scope": "registered_retirement_assertion_only",
            "private_pin": "opaque",
        },
    )
    await jobs.mark_complete(
        db_session,
        job.id,
        result={"outcome": "missing", "private_observation": {"key": "private"}},
    )
    await notify_job_terminal(db_session, job_id=job.id)
    await notify_job_terminal(db_session, job_id=job.id)
    rows = list(
        await db_session.scalars(
            select(Notification).where(Notification.target_id == job.id)
        )
    )
    assert len(rows) == 1
    assert rows[0].type == "job.completed"
    assert rows[0].payload == {
        "kind": KIND,
        "status": "completed",
        "scope": "registered_retirement_assertion_only",
    }


async def test_pending_verification_api_cancel_notifies_once(
    db_session, httpx_client, super_admin
):
    from app.services import managed_original_verification as verification
    from tests.test_managed_original_reclamation import assertion

    actor, token = super_admin
    anchor = await assertion(db_session)
    _, job, _ = await verification.start(db_session, anchor.id, actor)
    job_id = job.id
    response = await httpx_client.post(
        f"/api/v1/async-jobs/{job_id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    await notify_job_terminal(db_session, job_id=job_id)
    rows = list(
        await db_session.scalars(
            select(Notification).where(Notification.target_id == job_id)
        )
    )
    assert len(rows) == 1 and rows[0].type == "job.cancelled"


@pytest.mark.parametrize(
    "running,cancelled", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_verification_signal_boundary_notifies_without_raw_error(
    db_session, super_admin, monkeypatch, running, cancelled
):
    from app.services import managed_original_verification as verification
    from app.workers import signals
    from tests.test_managed_original_reclamation import assertion

    actor, _ = super_admin
    anchor = await assertion(db_session)
    row, job, _ = await verification.start(db_session, anchor.id, actor)
    job_id, task_id = job.id, job.celery_task_id
    if running:
        assert await verification.claim(db_session, job_id, task_id)

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(
        signals,
        "create_async_engine",
        lambda *args, **kwargs: SimpleNamespace(dispose=AsyncMock()),
    )
    monkeypatch.setattr(signals, "async_sessionmaker", lambda *args, **kwargs: session)
    await signals._apply_terminal(
        task_id, cancelled=cancelled, error="private endpoint / secret"
    )
    await signals._apply_terminal(
        task_id, cancelled=cancelled, error="late private error"
    )
    await db_session.refresh(row)
    expected = "cancelled" if cancelled and not running else "failed"
    assert row.status == expected
    rows = list(
        await db_session.scalars(
            select(Notification).where(Notification.target_id == job_id)
        )
    )
    assert len(rows) == 1 and rows[0].type == "job." + expected
    assert "private" not in str(rows[0].payload)
    assert row.error_code == (
        None if expected == "cancelled" else "verification_interrupted"
    )
