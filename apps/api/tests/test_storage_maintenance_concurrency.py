"""Real committed control transitions serialize independent readers/publication."""

from contextlib import asynccontextmanager
import uuid
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.dataset_asset import StorageLocation
from app.db.models.storage_maintenance import (
    StorageMaintenanceGate as Gate,
    StorageOperation,
)
from app.db.models.user import User
from app.schemas.storage_maintenance import (
    StorageMaintenanceRequest,
    StorageMaintenanceApply,
)
from app.services import storage_access as access, storage_maintenance as maintenance
from app.services.content_snapshot import ContentError
from app.services.audit import AuditService


@asynccontextmanager
async def world(test_engine, monkeypatch):
    # These tests isolate admission/transaction concurrency. The API suite tests
    # real audit writes. Avoid producing immutable audit facts in a world whose
    # independently committed actor must be removed during precise teardown.
    monkeypatch.setattr(AuditService, "log", AsyncMock())
    actor_id, location_id = uuid.uuid4(), uuid.uuid4()
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    try:
        async with maker() as db:
            db.add(
                User(
                    id=actor_id,
                    email=f"maintenance-{actor_id.hex}@example.test",
                    name="Maintenance concurrency",
                    role="super_admin",
                    password_hash="test-only",
                    is_active=True,
                )
            )
            db.add(
                StorageLocation(
                    id=location_id,
                    name=f"maintenance-{location_id.hex}",
                    kind="filesystem",
                    config={"root_handle": "offline", "relative_path": ""},
                    enabled=True,
                )
            )
            await db.commit()
        yield maker, actor_id, location_id
    finally:
        async with test_engine.begin() as connection:
            await connection.execute(
                delete(StorageOperation).where(
                    StorageOperation.location_id == location_id
                )
            )
            await connection.execute(
                delete(Gate).where(Gate.location_id == location_id)
            )
            await connection.execute(
                delete(StorageLocation).where(StorageLocation.id == location_id)
            )
            await connection.execute(delete(User).where(User.id == actor_id))


async def transition(maker, actor_id, location_id, operation, revision):
    async with maker() as db:
        reviewed = await maintenance.preview(
            db,
            location_id,
            StorageMaintenanceRequest(operation=operation, expected_revision=revision),
            await db.get(User, actor_id),
        )
        await db.commit()
    async with maker() as db:
        result = await maintenance.apply(
            db,
            location_id,
            StorageMaintenanceApply(
                operation=operation,
                expected_revision=revision,
                impact_token=reviewed["impact_token"],
            ),
            await db.get(User, actor_id),
        )
        await db.commit()
        return result


async def test_committed_drain_stop_resume_fences_independent_admission(
    test_engine, monkeypatch
):
    monkeypatch.setattr(settings, "storage_maintenance_protocol_enabled", True)
    async with world(test_engine, monkeypatch) as (maker, actor_id, location_id):
        async with maker() as reader:
            permit = await access.acquire(reader, location_id, "pre_drain")
        result = await transition(maker, actor_id, location_id, "drain", 1)
        assert result["revision"] == 2 and result["open_operations"] == 1
        async with maker() as reader:
            with pytest.raises(ContentError, match="^storage_maintenance$"):
                await access.acquire(reader, location_id, "during_drain")
            with pytest.raises(ContentError, match="storage_maintenance_changed"):
                await access.check(reader, location_id, expected_revision=1, lock=True)
        async with maker() as control:
            with pytest.raises(HTTPException) as rejected:
                await maintenance.preview(
                    control,
                    location_id,
                    StorageMaintenanceRequest(operation="stop", expected_revision=2),
                    await control.get(User, actor_id),
                )
            assert rejected.value.detail["code"] == "storage_maintenance_unsettled"
        await permit.__aexit__(None, None, None)
        result = await transition(maker, actor_id, location_id, "stop", 2)
        assert result["state"] == "stopped" and result["open_operations"] == 0
        result = await transition(maker, actor_id, location_id, "resume", 3)
        assert result["state"] == "active" and result["revision"] == 4
        async with maker() as reader:
            with pytest.raises(ContentError, match="storage_maintenance_changed"):
                await access.acquire(
                    reader, location_id, "stale_after_resume", expected_revision=1
                )
            async with await access.acquire(
                reader, location_id, "fresh_after_resume", expected_revision=4
            ):
                assert (await maintenance.snapshot(reader, location_id))[
                    "open_operations"
                ] == 1


async def test_final_publication_gate_share_rejects_concurrent_control(
    test_engine, monkeypatch
):
    monkeypatch.setattr(settings, "storage_maintenance_protocol_enabled", True)
    async with world(test_engine, monkeypatch) as (maker, actor_id, location_id):
        async with maker() as publication:
            await access.check(publication, location_id, expected_revision=1, lock=True)
            async with maker() as control:
                with pytest.raises(HTTPException) as rejected:
                    await maintenance.preview(
                        control,
                        location_id,
                        StorageMaintenanceRequest(
                            operation="drain", expected_revision=1
                        ),
                        await control.get(User, actor_id),
                    )
                assert rejected.value.detail["code"] == "storage_maintenance_busy"
            await publication.commit()
        assert (await transition(maker, actor_id, location_id, "drain", 1))[
            "state"
        ] == "draining"
