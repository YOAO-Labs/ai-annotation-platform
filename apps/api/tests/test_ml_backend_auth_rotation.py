from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.gpu_backend_fence import GPUBackendFence
from app.db.models.gpu_backend_membership import GPUBackendMembership
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.ml_backend import MLBackendService


async def _backend(db: AsyncSession, gpu: bool) -> MLBackendRegistry:
    backend = MLBackendRegistry(
        id=uuid.uuid4(),
        name="auth-rotation-test",
        url=f"http://auth-{uuid.uuid4()}.test",
        gpu_resource_id="node-auth/GPU-test" if gpu else None,
        vram_budget_mb=2048 if gpu else None,
        state="connected",
        health_meta={"previous": True},
        last_checked_at=datetime.now(UTC),
        error_message="previous failure",
    )
    db.add(backend)
    await db.flush()
    if gpu:
        fence = await db.get(GPUBackendFence, backend.id)
        fence.runtime_epoch_high_water = 1
        await db.flush()
    return backend


@pytest.mark.parametrize("gpu", [False, True])
async def test_raw_auth_rotation_clears_health_and_keeps_membership(db_session, gpu):
    backend = await _backend(db_session, gpu)
    result = await db_session.execute(
        text(
            "UPDATE ml_backend_registry SET auth_method='token', auth_token='new', "
            "url=url, state='connected', health_meta='{}'::jsonb, "
            "last_checked_at=clock_timestamp(), error_message='forged' "
            "WHERE id=:id RETURNING auth_revision, state, health_meta, "
            "last_checked_at, error_message"
        ),
        {"id": backend.id},
    )
    assert tuple(result.one()) == (1, "disconnected", None, None, None)
    if gpu:
        membership = await db_session.get(
            GPUBackendMembership, (backend.id, backend.gpu_resource_id)
        )
        assert (membership.membership_epoch, membership.state) == (1, "pending")
    await db_session.execute(
        text("UPDATE ml_backend_registry SET auth_token='new' WHERE id=:id"),
        {"id": backend.id},
    )
    assert (
        await db_session.scalar(
            text("SELECT auth_revision FROM ml_backend_registry WHERE id=:id"),
            {"id": backend.id},
        )
        == 1
    )


@pytest.mark.parametrize("field", ["url", "vram_budget_mb", "extra_params"])
async def test_mixed_auth_runtime_mutation_rolls_back(db_session, field):
    backend = await _backend(db_session, True)
    expression = {
        "url": "'http://replacement.test'",
        "vram_budget_mb": "4096",
        "extra_params": "'{\"max_concurrency\": 7}'::jsonb",
    }[field]
    with pytest.raises(IntegrityError, match="requires retirement"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "UPDATE ml_backend_registry SET auth_method='token', "
                    f"auth_token='new', {field}={expression} WHERE id=:id"
                ),
                {"id": backend.id},
            )
    result = await db_session.execute(
        text(
            "SELECT auth_revision, auth_method, auth_token, state, health_meta "
            "FROM ml_backend_registry WHERE id=:id"
        ),
        {"id": backend.id},
    )
    assert tuple(result.one()) == (0, "none", None, "connected", {"previous": True})


@pytest.mark.parametrize("also_rotate", [False, True])
async def test_auth_revision_cannot_be_forged(db_session, also_rotate):
    backend = await _backend(db_session, False)
    extra = ", auth_method='token', auth_token='new'" if also_rotate else ""
    with pytest.raises(IntegrityError, match="revision is database managed"):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    f"UPDATE ml_backend_registry SET auth_revision=4{extra} "
                    "WHERE id=:id"
                ),
                {"id": backend.id},
            )


async def test_service_returns_database_generated_auth_revision(db_session):
    backend = await _backend(db_session, False)
    updated = await MLBackendService(db_session).update(
        backend.id, auth_method="token", auth_token="new"
    )
    assert updated is backend
    assert updated.auth_revision == 1
    assert updated.health_meta is None


@pytest.mark.parametrize("gpu", [False, True])
@pytest.mark.parametrize("restore_original", [False, True])
async def test_inflight_health_cannot_overwrite_rotated_auth(
    db_session, monkeypatch, gpu, restore_original
):
    backend = await _backend(db_session, gpu)

    class Client:
        async def health_meta(self, **_kwargs):
            await db_session.execute(
                text(
                    "UPDATE ml_backend_registry SET auth_method='token', auth_token='new' "
                    "WHERE id=:id"
                ),
                {"id": backend.id},
            )
            if restore_original:
                await db_session.execute(
                    text(
                        "UPDATE ml_backend_registry SET auth_method='none', auth_token=NULL "
                        "WHERE id=:id"
                    ),
                    {"id": backend.id},
                )
            return True, None

        async def setup(self):
            raise AssertionError("metadata-free health must not probe setup")

    monkeypatch.setattr(
        "app.services.inference_backend_control.health_observation_client_factory",
        lambda _db: lambda _backend_row: Client(),
    )
    assert await MLBackendService(db_session).check_health(backend.id) is False
    await db_session.refresh(backend)
    assert backend.auth_revision == (2 if restore_original else 1)
    assert backend.state == "disconnected"
    assert backend.health_meta is None
    assert backend.last_checked_at is None


async def test_auth_rotation_waits_for_membership_proof_commit(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory.begin() as db:
        backend = await _backend(db, True)
    backend_id = backend.id
    try:
        async with factory.begin() as proof_db:
            await proof_db.execute(
                text(
                    "SELECT 1 FROM gpu_backend_memberships "
                    "WHERE backend_registry_id=:id FOR UPDATE"
                ),
                {"id": backend_id},
            )
            async with factory() as edit_db:
                await edit_db.execute(text("SET LOCAL lock_timeout='100ms'"))
                with pytest.raises(DBAPIError, match="lock timeout"):
                    await edit_db.execute(
                        text(
                            "UPDATE ml_backend_registry SET auth_method='token', "
                            "auth_token='new' WHERE id=:id"
                        ),
                        {"id": backend_id},
                    )
                await edit_db.rollback()
        async with factory.begin() as db:
            await db.execute(
                text(
                    "UPDATE ml_backend_registry SET auth_method='token', "
                    "auth_token='new' WHERE id=:id"
                ),
                {"id": backend_id},
            )
            backend = await db.get(MLBackendRegistry, backend_id)
            assert backend.auth_revision == 1
    finally:
        # Only this committed fixture needs explicit cleanup; the other tests use
        # the shared rollback fixture. Trigger changes are transaction-local DDL.
        async with factory.begin() as db:
            triggers = (
                ("ml_backend_registry", "trg_sync_gpu_backend_membership"),
                ("gpu_backend_memberships", "trg_validate_gpu_backend_membership"),
                ("gpu_backend_fences", "trg_validate_gpu_backend_fence_delete"),
            )
            for table, trigger in triggers:
                await db.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
            for table, key in (
                ("gpu_backend_memberships", "backend_registry_id"),
                ("gpu_backend_fences", "backend_registry_id"),
                ("ml_backend_registry", "id"),
            ):
                await db.execute(
                    text(f"DELETE FROM {table} WHERE {key}=:id"), {"id": backend_id}
                )
            for table, trigger in reversed(triggers):
                await db.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
