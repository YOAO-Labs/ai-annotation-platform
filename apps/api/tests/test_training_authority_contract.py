"""The shared training authority keeps its row fences in the caller transaction."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.api_key import ApiKey
from app.db.models.project import Project
from app.db.models.user import User
from app.services.training_dataset_bridge import TrainingDatasetError
from app.services.training_datasets import bind_training_authority
from tests.factory import create_project, create_user


async def seed_authority_rows(test_engine, *, revoked=False):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    async with maker() as setup:
        owner = await create_user(
            setup, "project_admin", f"authority-{suffix}@training.test", "Authority"
        )
        project = await create_project(setup, owner_id=owner.id)
        key = ApiKey(
            user_id=owner.id,
            name="Training authority test",
            key_prefix="ak_test",
            key_hash="test-hash",
            scopes=["datasets:read", "annotations:read"],
            revoked_at=datetime.now(timezone.utc) if revoked else None,
        )
        setup.add(key)
        await setup.flush()
        ids = (owner.id, project.id, key.id)
        await setup.commit()
    return maker, ids


async def cleanup_authority_rows(maker, ids):
    owner_id, project_id, key_id = ids
    async with maker() as cleanup:
        await cleanup.execute(
            Project.__table__.delete().where(Project.id == project_id)
        )
        await cleanup.execute(ApiKey.__table__.delete().where(ApiKey.id == key_id))
        await cleanup.execute(User.__table__.delete().where(User.id == owner_id))
        await cleanup.commit()


async def assert_session_can_lock(maker, ids):
    owner_id, project_id, key_id = ids
    async with maker() as db:
        for model, identity in (
            (User, owner_id),
            (Project, project_id),
            (ApiKey, key_id),
        ):
            row = await db.scalar(
                select(model).where(model.id == identity).with_for_update(nowait=True)
            )
            assert row is not None
        await db.rollback()


@pytest.mark.parametrize("release", ["commit", "rollback"])
async def test_authority_locks_remain_with_caller_until_commit_or_rollback(
    test_engine, release
):
    maker, ids = await seed_authority_rows(test_engine)
    owner_id, project_id, key_id = ids
    try:
        async with maker() as caller:
            await bind_training_authority(caller).assert_authority(
                project_id=project_id,
                actor_id=owner_id,
                api_key_id=str(key_id),
            )
            async with maker() as competitor:
                for model, identity in (
                    (User, owner_id),
                    (Project, project_id),
                    (ApiKey, key_id),
                ):
                    with pytest.raises(DBAPIError) as busy:
                        await competitor.scalar(
                            select(model)
                            .where(model.id == identity)
                            .with_for_update(nowait=True)
                        )
                    assert getattr(busy.value.orig, "sqlstate", None) == "55P03"
                    await competitor.rollback()
            if release == "commit":
                await caller.commit()
            else:
                await caller.rollback()
        await assert_session_can_lock(maker, ids)
    finally:
        await cleanup_authority_rows(maker, ids)


async def test_revoked_api_key_remains_a_dataset_authority_rejection(test_engine):
    maker, ids = await seed_authority_rows(test_engine, revoked=True)
    owner_id, project_id, key_id = ids
    try:
        async with maker() as caller:
            with pytest.raises(TrainingDatasetError) as rejected:
                await bind_training_authority(caller).assert_authority(
                    project_id=project_id,
                    actor_id=owner_id,
                    api_key_id=str(key_id),
                )
            assert rejected.value.code == "api_key_revoked"
            assert rejected.value.status_code == 403
            await caller.rollback()
    finally:
        await cleanup_authority_rows(maker, ids)


async def test_authority_lock_contention_remains_a_retryable_project_busy_409(
    test_engine,
):
    maker, ids = await seed_authority_rows(test_engine)
    owner_id, project_id, key_id = ids
    try:
        async with maker() as lock_owner:
            await lock_owner.scalar(
                select(User).where(User.id == owner_id).with_for_update()
            )
            async with maker() as caller:
                with pytest.raises(HTTPException) as busy:
                    await bind_training_authority(caller).assert_authority(
                        project_id=project_id,
                        actor_id=owner_id,
                        api_key_id=str(key_id),
                    )
                assert busy.value.status_code == 409
                assert busy.value.detail == {"reason": "project_busy"}
                await caller.rollback()
            await lock_owner.rollback()
    finally:
        await cleanup_authority_rows(maker, ids)
