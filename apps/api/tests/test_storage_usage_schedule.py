"""Explicit periodic authority never borrows another administrator identity."""

from datetime import timedelta

from sqlalchemy import select

from app.db.models.async_job import AsyncJob
from app.db.models.storage_usage import StorageUsageSchedule as Schedule
from app.services import storage_usage as svc
from app.workers import storage_usage as worker
from tests.test_storage_usage import ready as ready


async def test_schedule_defaults_off_and_requires_explicit_authorized_put(
    ready, httpx_client, auth_headers
):
    db, _ = ready
    data = (
        await httpx_client.get("/api/v1/storage/buckets", headers=auth_headers)
    ).json()
    assert data["usage_schedule"]["status"] == "off"
    assert data["accounting"]["coverage"] == "partial"
    assert data["accounting"]["projected_total_bytes"] is None
    response = await httpx_client.put(
        "/api/v1/storage/usage-schedule", headers=auth_headers, json={"enabled": True}
    )
    assert response.status_code == 200, response.text
    assert response.json()["interval_hours"] == 24 and response.json()["next_due_at"]
    assert await db.scalar(select(AsyncJob)) is None
    for value in (0, 169):
        response = await httpx_client.put(
            "/api/v1/storage/usage-schedule",
            headers=auth_headers,
            json={"enabled": True, "interval_hours": value},
        )
        assert response.status_code == 422


async def test_due_tick_reuses_single_inventory_and_disable_does_not_cancel(ready):
    db, actor = ready
    await svc.configure_schedule(db, actor, True, 1)
    await db.commit()
    await worker.recover(db)
    jobs = list(await db.scalars(select(AsyncJob)))
    assert len(jobs) == 1
    row = await db.get(Schedule, "platform", populate_existing=True)
    row.next_due_at = svc.now() - timedelta(seconds=1)
    await db.commit()
    await worker.recover(db)
    assert len(list(await db.scalars(select(AsyncJob)))) == 1
    await svc.configure_schedule(db, actor, False, 24)
    await db.commit()
    await db.refresh(jobs[0])
    assert jobs[0].status == "pending"
    assert (await db.get(Schedule, "platform")).status == "off"


async def test_revocation_pauses_even_before_due_and_requires_reauthorization(ready):
    db, actor = ready
    await svc.configure_schedule(db, actor, True, 24)
    row = await db.get(Schedule, "platform")
    row.next_due_at = svc.now() + timedelta(days=1)
    actor.is_active = False
    await db.commit()
    await worker.recover(db)
    await db.refresh(row)
    assert row.status == "paused" and row.pause_reason == "access_revoked"
    actor.is_active = True
    await db.commit()
    await worker.recover(db)
    await db.refresh(row)
    assert row.status == "paused"
    await svc.configure_schedule(db, actor, True, 24)
    assert row.status == "active"


async def test_configuration_change_pauses_without_starting_a_job(ready, monkeypatch):
    db, actor = ready
    await svc.configure_schedule(db, actor, True, 24)
    await db.commit()
    monkeypatch.setattr(svc.storage_service, "import_bucket", "changed-target")
    await worker.recover(db)
    row = await db.get(Schedule, "platform", populate_existing=True)
    assert row.status == "paused" and row.pause_reason == "configuration_changed"
    assert await db.scalar(select(AsyncJob)) is None
