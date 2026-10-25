"""Only the new private global kind gains the required notification fences."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import uuid

import pytest
from sqlalchemy import delete, select

from app.core import security
from app.db.models.api_key import ApiKey
from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.services import async_job as jobs
from app.services.notification import NotificationService

KIND = "storage_migration_inventory"


@pytest.fixture(
    autouse=True, params=["storage_migration_inventory", "storage_migration_copy"]
)
def private_kind(request, monkeypatch):
    monkeypatch.setitem(globals(), "KIND", request.param)


async def notices(db, actor):
    job = await jobs.create_job(db, kind=KIND, user_id=actor.id)
    protected = Notification(
        user_id=actor.id,
        type="job.completed",
        target_type="async_job",
        target_id=job.id,
        payload={"kind": KIND, "status": "completed"},
    )
    read = Notification(
        user_id=actor.id,
        type="job.completed",
        target_type="async_job",
        target_id=job.id,
        payload={"kind": KIND},
        read_at=datetime.now(timezone.utc),
    )
    unrelated = Notification(
        user_id=actor.id,
        type="system.announcement",
        target_type="system",
        target_id=uuid.uuid4(),
        payload={"message": "global"},
    )
    db.add_all([protected, read, unrelated])
    await db.flush()
    return protected, read, unrelated


@pytest.mark.parametrize("principal", ["full", "narrow", "downgraded"])
@pytest.mark.parametrize("job_retained", [True, False])
async def test_rest_notification_reads_counts_and_mutations_use_current_authority(
    db_session, httpx_client, super_admin, principal, job_retained
):
    actor, token = super_admin
    protected, read, unrelated = await notices(db_session, actor)
    if not job_retained:
        await db_session.execute(
            delete(AsyncJob).where(AsyncJob.id == protected.target_id)
        )

    if principal == "narrow":
        token = "ak_" + uuid.uuid4().hex
        db_session.add(
            ApiKey(
                user_id=actor.id,
                name="Notification inventory",
                key_prefix=token[:12],
                key_hash=security.hash_password(token),
                scopes=["datasets:read"],
            )
        )
    elif principal == "downgraded":
        actor.role = "project_admin"
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}
    allowed = principal == "full"
    response = await httpx_client.get("/api/v1/notifications", headers=headers)
    assert response.status_code == 200
    assert response.json()["total"] == (3 if allowed else 1)
    assert response.json()["unread"] == (2 if allowed else 1)
    count = await httpx_client.get(
        "/api/v1/notifications/unread-count", headers=headers
    )
    assert count.json()["unread"] == (2 if allowed else 1)
    single = await httpx_client.post(
        f"/api/v1/notifications/{protected.id}/read", headers=headers
    )
    assert single.status_code == (200 if allowed else 404)
    single = await httpx_client.delete(
        f"/api/v1/notifications/{read.id}", headers=headers
    )
    assert single.status_code == (200 if allowed else 404)
    batch = await httpx_client.post(
        "/api/v1/notifications/mark-all-read", headers=headers
    )
    assert batch.json()["updated"] == 1
    cleared = await httpx_client.post(
        "/api/v1/notifications/clear-read", headers=headers
    )
    assert cleared.json()["deleted"] == (2 if allowed else 1)
    remaining = list(
        await db_session.scalars(
            select(Notification.id).where(Notification.user_id == actor.id)
        )
    )
    assert len(remaining) == (0 if allowed else 2)


async def test_creation_and_late_websocket_delivery_recheck_current_role_and_key(
    db_session, super_admin, monkeypatch
):
    from app.api.v1 import ws
    from app.db import base
    from app.services import api_key_service

    actor, token = super_admin
    protected, _, _ = await notices(db_session, actor)
    svc = NotificationService(db_session)
    assert await svc.notification_deliverable(protected.id, actor.id)
    assert not await svc.notification_deliverable(
        protected.id, actor.id, include_migration_inventory=False
    )
    actor.role = "project_admin"
    await db_session.flush()
    assert not await svc.notification_deliverable(protected.id, actor.id)
    scopes = await svc._resolve_delivery_scopes([protected])
    assert not await svc._allowed_delivery_indices([protected], scopes)
    actor.role = "super_admin"
    await db_session.flush()

    @asynccontextmanager
    async def session():
        yield db_session

    async def authenticate(_token):
        return actor

    current_scopes = ["*"]

    async def resolve(db, raw):
        assert db is db_session and raw == "ak_fixture"
        from types import SimpleNamespace

        return SimpleNamespace(scopes=current_scopes), actor

    monkeypatch.setattr(base, "async_session", session)
    monkeypatch.setattr(ws, "_authenticate_socket_token", authenticate)
    monkeypatch.setattr(api_key_service, "resolve_token", resolve)
    message = json.dumps({"id": str(protected.id), "type": "job.completed"})
    assert await ws._notification_message_allowed("ak_fixture", message)
    current_scopes[:] = ["datasets:read"]
    assert not await ws._notification_message_allowed("ak_fixture", message)
    assert await ws._notification_message_allowed(token, message)
    await db_session.execute(delete(AsyncJob).where(AsyncJob.id == protected.target_id))
    assert await ws._notification_message_allowed(token, message)
    assert not await ws._notification_message_allowed("ak_fixture", message)
    actor.role = "project_admin"
    await db_session.flush()
    assert not await ws._notification_message_allowed(token, message)


async def test_actual_job_kind_wins_over_stale_notification_payload(
    db_session, super_admin
):
    actor, _ = super_admin
    protected, _, _ = await notices(db_session, actor)
    job = await db_session.get(AsyncJob, protected.target_id)
    job.kind = "unrelated_global"
    actor.role = "project_admin"
    await db_session.flush()
    svc = NotificationService(db_session)
    items, total, unread = await svc.list_for_user(
        actor.id, include_migration_inventory=False
    )
    assert total == 3 and unread == 2 and protected in items
    assert await svc.notification_deliverable(
        protected.id, actor.id, include_migration_inventory=False
    )
