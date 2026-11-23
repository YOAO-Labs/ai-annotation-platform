"""Isolate committed storage-migration ledgers in an owned, fully migrated database."""

import os
import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.managed_image_inference_fixtures import managed_database


@pytest.fixture
async def migration_engine(test_db_url):
    async with managed_database(test_db_url) as engine:
        yield engine


native_reclaim_only = pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the disposable module-data boundary verifier",
)


@pytest.fixture
async def native_reclaim_db(test_engine):
    from datetime import datetime, timezone

    from tests.factory import create_user
    from tests.test_asset_storage_cleanup_runtime import (
        _verified_resource_manifest,
        _verify_postgres,
    )

    url = test_engine.url
    name = url.database
    assert url.host == "127.0.0.1" and name and name.endswith("_test")
    _verify_postgres(url)
    _verified_resource_manifest(test_engine, url)
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as db:
        assert await db.scalar(text("SELECT current_database()")) == name
        empty = await db.scalar(
            text(
                """
                SELECT NOT (
                    EXISTS (SELECT 1 FROM public.managed_original_retirements)
                    OR EXISTS (SELECT 1 FROM public.managed_original_creations)
                    OR EXISTS (SELECT 1 FROM public.managed_original_reclaim_attempts)
                    OR EXISTS (SELECT 1 FROM public.managed_original_reclaim_claims)
                    OR EXISTS (SELECT 1 FROM public.managed_original_reference_reconciliation)
                    OR EXISTS (SELECT 1 FROM public.async_jobs)
                    OR EXISTS (SELECT 1 FROM public.storage_operations)
                    OR EXISTS (SELECT 1 FROM public.training_dataset_versions)
                    OR EXISTS (SELECT 1 FROM public.active_learning_rounds)
                )
                """
            )
        )
        if not empty:
            pytest.fail(
                "Native reclamation requires a fresh disposable database per case."
            )
        actor = await create_user(
            db, "super_admin", f"{uuid.uuid4()}@reclaim.test", "Reclaim operator"
        )
        actor.email_verified_at = datetime.now(timezone.utc)
        await db.commit()
        yield db, actor


@pytest.fixture
async def db_session(migration_engine):
    """SAVEPOINT-isolated session inside the owned schema."""
    connection = await migration_engine.connect()
    transaction = await connection.begin()
    maker = async_sessionmaker(connection, class_=AsyncSession, expire_on_commit=False)
    session = maker()
    await connection.begin_nested()
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        from app.services.system_settings_service import SystemSettingsService

        SystemSettingsService.invalidate()


@pytest.fixture
async def httpx_client(db_session):
    from app.deps import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _identity(db, role, email, name):
    from app.core.security import create_access_token
    from tests.factory import create_user

    user = await create_user(db, role, email, name)
    return user, create_access_token(subject=str(user.id), role=role)


@pytest.fixture
async def super_admin(db_session):
    return await _identity(db_session, "super_admin", "admin@test.local", "Admin")


@pytest.fixture
async def project_admin(db_session):
    return await _identity(db_session, "project_admin", "pm@test.local", "PM")


@pytest.fixture
async def annotator(db_session):
    return await _identity(db_session, "employee", "anno@test.local", "Annotator")


@pytest.fixture
async def reviewer(db_session):
    return await _identity(db_session, "employee", "qa@test.local", "Reviewer")
