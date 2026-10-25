"""Window mutations survive actual request-session closure and fresh readers."""

import uuid

import httpx

from app.core.security import create_access_token
from app.db.models.storage_maintenance import StorageMaintenanceGate as Gate
from app.db.models.storage_migration_window import StorageMigrationWindow as Window
from app.db.models.user import User
from app.deps import get_db
from app.main import app
from app.schemas.storage_migration_window import StorageMigrationWindowTransition
from app.services import storage_migration_windows as windows
from app.services.dataset_assets import LEGACY_LOCATION_ID
from tests.test_storage_migration_windows import (
    acquire,
    bound_reader,
    copy_worker_world as copy_worker_world,
    reader_world as reader_world,
)

PATH = "/api/v1/storage/migration-windows"


async def test_each_http_mutation_commits_before_the_next_independent_request(
    reader_world,
):
    world, sessions = reader_world, []
    async with world.maker() as db:
        actor = await db.get(User, world.actor_id)
        await windows.transition(
            db,
            world.window_id,
            actor,
            StorageMigrationWindowTransition(
                action="close", expected_revision=world.window_revision
            ),
        )
        await db.commit()

    # This dependency deliberately has the same no-auto-commit lifetime as
    # production get_db. Every request owns a new Session and connection lease.
    async def fresh_request():
        async with world.maker() as session:
            sessions.append(session)
            yield session

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = fresh_request
    token = create_access_token(subject=str(world.actor_id), role="super_admin")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": f"Bearer {token}"}
            opened = await client.post(
                PATH,
                json={"parent_copy_id": str(world.copy_id)},
                headers=headers,
            )
            assert opened.status_code == 201, opened.text
            value = opened.json()
            window_id = uuid.UUID(value["window_id"])
            async with world.maker() as db:
                durable = await db.get(Window, window_id)
                assert durable is not None and durable.state == "draining"
                assert (await db.get(Gate, LEGACY_LOCATION_ID)).state == "draining"
            detail = await client.get(f"{PATH}/{window_id}", headers=headers)
            assert detail.status_code == 200 and detail.json()["revision"] == 1
            stopped = await client.post(
                f"{PATH}/{window_id}/transitions",
                json={"action": "stop", "expected_revision": 1},
                headers=headers,
            )
            assert stopped.status_code == 200, stopped.text
            value = stopped.json()
            async with world.maker() as db:
                durable = await db.get(Window, window_id)
                gate = await db.get(Gate, LEGACY_LOCATION_ID)
                assert durable.state == gate.state == "stopped"
                assert durable.source_gate_revision == gate.revision
                world.window_id, world.window_revision = window_id, durable.revision
                world.revision, world.source = gate.revision, dict(durable.source)

            reader = await bound_reader(world, "inventory")
            permit = await acquire(world, reader)
            closing = await client.post(
                f"{PATH}/{window_id}/transitions",
                json={"action": "close", "expected_revision": value["revision"]},
                headers=headers,
            )
            assert closing.status_code == 200, closing.text
            assert closing.json()["state"] == "closing"
            async with world.maker() as db:
                assert (await db.get(Window, window_id)).state == "closing"
                assert (await db.get(Gate, LEGACY_LOCATION_ID)).state == "stopped"
            async with permit:
                pass
            closed = await client.post(
                f"{PATH}/{window_id}/transitions",
                json={
                    "action": "close",
                    "expected_revision": closing.json()["revision"],
                },
                headers=headers,
            )
            assert closed.status_code == 200, closed.text
            async with world.maker() as db:
                assert (await db.get(Window, window_id)).state == "closed"
                assert (await db.get(Gate, LEGACY_LOCATION_ID)).state == "active"
            detail = await client.get(f"{PATH}/{window_id}", headers=headers)
            assert detail.status_code == 200 and detail.json()["state"] == "closed"
            assert detail.json()["source_quiescence_proven"] is False
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
    assert len(sessions) == 6 and len({id(session) for session in sessions}) == 6
    assert all(not session.in_transaction() for session in sessions)
