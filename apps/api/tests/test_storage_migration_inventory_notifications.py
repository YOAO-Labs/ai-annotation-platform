"""Only the new private global kind gains the required notification fences."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import uuid

import pytest
from sqlalchemy import delete, select
from yoao_notifications.websocket import notification_message_allowed

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
    if not allowed:
        assert protected.read_at is None
    single = await httpx_client.delete(
        f"/api/v1/notifications/{read.id}", headers=headers
    )
    assert single.status_code == (200 if allowed else 404)
    if not allowed:
        assert read in db_session
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


async def test_creation_and_late_websocket_delivery_recheck_current_role(
    db_session, super_admin, monkeypatch
):
    from app.api.v1 import ws
    from app.db import base

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

    async def authenticate(_raw, *, allow_api_key=False):
        from types import SimpleNamespace

        assert not allow_api_key
        return SimpleNamespace(actor_id=actor.id)

    monkeypatch.setattr(base, "async_session", session)
    monkeypatch.setattr(ws, "_authenticate_socket_token", authenticate)
    message = json.dumps({"id": str(protected.id), "type": "job.completed"})

    async def allowed(credential):
        return await notification_message_allowed(
            credential,
            message,
            authenticate=ws._notification_authenticate,
            open_delivery=ws._open_notification_delivery,
        )

    assert not await allowed("ak_fixture")
    assert await allowed(token)
    await db_session.execute(delete(AsyncJob).where(AsyncJob.id == protected.target_id))
    assert await allowed(token)
    actor.role = "project_admin"
    await db_session.flush()
    assert not await allowed(token)


async def test_actual_job_kind_wins_over_stale_notification_payload(
    db_session, super_admin
):
    actor, _ = super_admin
    protected, _, _ = await notices(db_session, actor)
    job = await db_session.get(AsyncJob, protected.target_id)
    job.kind = "dataset_import"
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


async def test_inactive_super_keeps_unrelated_inbox_but_not_private_migration(
    db_session, super_admin
):
    actor, _ = super_admin
    protected, _, unrelated = await notices(db_session, actor)
    actor.is_active = False
    await db_session.flush()
    items, total, unread = await NotificationService(db_session).list_for_user(actor.id)
    assert (total, unread) == (1, 1)
    assert [item.id for item in items] == [unrelated.id]
    assert not await NotificationService(db_session).notification_deliverable(
        protected.id, actor.id
    )


@pytest.mark.parametrize("recipient", ["ordinary", "super"])
@pytest.mark.parametrize("job_state", ["unknown", "missing"])
async def test_unresolved_job_is_hidden_from_rest_mutations_and_socket(
    db_session, httpx_client, annotator, super_admin, monkeypatch, recipient, job_state
):
    from app.api.v1 import ws
    from app.db import base
    from types import SimpleNamespace

    actor, token = super_admin if recipient == "super" else annotator
    job = await jobs.create_job(db_session, kind="dataset_import", user_id=actor.id)
    target_id = job.id
    if job_state == "unknown":
        job.kind = "unrelated_global"
    else:
        await db_session.execute(delete(AsyncJob).where(AsyncJob.id == target_id))
    hidden_unread = Notification(
        user_id=actor.id,
        type="job.completed",
        target_type="async_job",
        target_id=target_id,
        payload={},
    )
    hidden_read = Notification(
        user_id=actor.id,
        type="job.completed",
        target_type="async_job",
        target_id=target_id,
        payload={},
        read_at=datetime.now(timezone.utc),
    )
    visible = Notification(
        user_id=actor.id,
        type="system.announcement",
        target_type="system",
        target_id=uuid.uuid4(),
        payload={},
    )
    db_session.add_all([hidden_unread, hidden_read, visible])
    await db_session.flush()
    hidden_ids = {hidden_unread.id, hidden_read.id}
    headers = {"Authorization": f"Bearer {token}"}
    page = await httpx_client.get("/api/v1/notifications", headers=headers)
    assert page.status_code == 200
    assert page.json()["total"] == 1
    assert page.json()["unread"] == 1
    assert (
        await httpx_client.get("/api/v1/notifications/unread-count", headers=headers)
    ).json()["unread"] == 1

    service = NotificationService(db_session)
    assert not await service.notification_deliverable(hidden_unread.id, actor.id)
    with db_session.no_autoflush:
        assert not await service.mark_read(actor.id, hidden_unread.id)
        assert hidden_unread.read_at is None
        assert not await service.delete_for_user(actor.id, hidden_read.id)
        assert hidden_read in db_session

    @asynccontextmanager
    async def session():
        yield db_session

    async def authenticate(_raw, *, allow_api_key=True):
        return SimpleNamespace(actor_id=actor.id, scopes=None)

    monkeypatch.setattr(base, "async_session", session)
    monkeypatch.setattr(ws, "_authenticate_socket_token", authenticate)
    message = json.dumps({"id": str(hidden_unread.id), "type": "job.completed"})
    assert not await notification_message_allowed(
        token,
        message,
        authenticate=ws._notification_authenticate,
        open_delivery=ws._open_notification_delivery,
    )
    assert (
        await httpx_client.post(
            f"/api/v1/notifications/{hidden_unread.id}/read", headers=headers
        )
    ).status_code == 404
    assert (
        await httpx_client.delete(
            f"/api/v1/notifications/{hidden_read.id}", headers=headers
        )
    ).status_code == 404
    assert (
        await httpx_client.post("/api/v1/notifications/mark-all-read", headers=headers)
    ).json()["updated"] == 1
    assert (
        await httpx_client.post("/api/v1/notifications/clear-read", headers=headers)
    ).json()["deleted"] == 1
    remaining = set(
        await db_session.scalars(
            select(Notification.id).where(Notification.user_id == actor.id)
        )
    )
    assert remaining == hidden_ids
    with db_session.no_autoflush:
        assert await service.mark_all_read(actor.id) == 0
        assert hidden_unread.read_at is None
        assert await service.clear_read(actor.id) == 0
        assert hidden_read in db_session
