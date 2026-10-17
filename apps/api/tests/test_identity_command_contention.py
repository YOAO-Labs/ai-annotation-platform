"""Account authority fences must not partially revoke credentials or deadlock projections."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.audit_log import AuditLog
from app.db.models.group import Group
from app.db.models.user import User
from app.deps import get_db
from app.schemas.group import GroupUpdate
from app.services.account_groups import AccountGroupService
from tests.factory import create_user


@pytest.mark.parametrize("operation", ["rename", "delete"])
async def test_cross_group_managers_do_not_deadlock_account_projections(
    test_engine, monkeypatch, operation
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    key = uuid.uuid4().hex
    async with maker() as setup:
        first = await create_user(
            setup, "project_admin", f"first-{key}@groups.test", "First"
        )
        second = await create_user(
            setup, "project_admin", f"second-{key}@groups.test", "Second"
        )
        group_a, group_b = Group(name=f"a-{key}"), Group(name=f"b-{key}")
        setup.add_all([group_a, group_b])
        await setup.flush()
        first.group_id, first.group_name = group_a.id, group_a.name
        second.group_id, second.group_name = group_b.id, group_b.name
        user_ids, group_ids = [first.id, second.id], [group_a.id, group_b.id]
        await setup.commit()
    ready, arrived = asyncio.Event(), 0
    lock_group = AccountGroupService._group

    async def synchronized_group(service, group_id):
        nonlocal arrived
        group = await lock_group(service, group_id)
        arrived += 1
        if arrived == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 5)
        return group

    monkeypatch.setattr(AccountGroupService, "_group", synchronized_group)

    async def mutate(actor_id, group_id, name):
        async with maker() as db:
            owner = AccountGroupService(db)
            if operation == "rename":
                await owner.update(
                    group_id, actor_id=actor_id, payload=GroupUpdate(name=name)
                )
            else:
                await owner.delete(group_id, actor_id=actor_id)
            await db.commit()
            return "committed"

    try:
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                mutate(user_ids[0], group_ids[1], f"renamed-b-{key}"),
                mutate(user_ids[1], group_ids[0], f"renamed-a-{key}"),
                return_exceptions=True,
            ),
            15,
        )
        assert outcomes == ["committed", "committed"]
        async with maker() as check:
            first, second = (
                await check.get(User, user_ids[0]),
                await check.get(User, user_ids[1]),
            )
            if operation == "rename":
                assert (first.group_name, second.group_name) == (
                    f"renamed-a-{key}",
                    f"renamed-b-{key}",
                )
                assert (first.group_id, second.group_id) == tuple(group_ids)
            else:
                assert (
                    first.group_id,
                    first.group_name,
                    second.group_id,
                    second.group_name,
                ) == (None, None, None, None)
                assert (
                    await check.scalars(select(Group).where(Group.id.in_(group_ids)))
                ).all() == []
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
            await cleanup.execute(delete(Group).where(Group.id.in_(group_ids)))
            await cleanup.commit()


async def test_busy_logout_does_not_revoke_token_before_account_admission(
    test_engine, app_module
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as setup:
        user = await create_user(
            setup, "employee", f"logout-{uuid.uuid4()}@contention.test", "Logout"
        )
        user_id = user.id
        await setup.commit()
    token = create_access_token(str(user_id), "employee")
    headers = {"Authorization": f"Bearer {token}"}

    async def request_db():
        async with maker() as db:
            yield db

    previous = app_module.dependency_overrides.get(get_db)
    app_module.dependency_overrides[get_db] = request_db
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module),
            base_url="http://contention.test",
        ) as client:
            async with maker() as writer:
                await writer.execute(
                    select(User).where(User.id == user_id).with_for_update()
                )
                busy = await client.post("/api/v1/auth/logout", headers=headers)
                assert busy.status_code == 409, busy.text
                await writer.rollback()
            heartbeat = await client.post("/api/v1/auth/me/heartbeat", headers=headers)
            assert heartbeat.status_code == 204, heartbeat.text
            logged_out = await client.post("/api/v1/auth/logout", headers=headers)
            assert logged_out.status_code == 204, logged_out.text
            replay = await client.post("/api/v1/auth/me/heartbeat", headers=headers)
            assert replay.status_code == 401, replay.text
    finally:
        if previous is None:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous
        async with maker() as cleanup:
            await cleanup.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
            await cleanup.execute(delete(AuditLog).where(AuditLog.actor_id == user_id))
            await cleanup.execute(delete(User).where(User.id == user_id))
            await cleanup.commit()


async def test_group_authority_stays_fenced_until_commit_and_rechecks_disable(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as setup:
        actor = await create_user(
            setup, "project_admin", f"authority-{uuid.uuid4()}@groups.test", "Manager"
        )
        actor_id = actor.id
        await setup.commit()
    group_id = None
    try:
        async with maker() as writer:
            from app.schemas.group import GroupCreate

            _, group = await AccountGroupService(writer).create(
                actor_id=actor_id, payload=GroupCreate(name=f"fenced-{uuid.uuid4()}")
            )
            group_id, original_name = group.id, group.name
            async with maker() as revoker:
                with pytest.raises(DBAPIError) as busy:
                    await revoker.execute(
                        select(User)
                        .where(User.id == actor_id)
                        .with_for_update(nowait=True)
                    )
                assert getattr(busy.value.orig, "sqlstate", None) == "55P03"
                await revoker.rollback()
            await writer.commit()
            async with maker() as revoker:
                current = await revoker.scalar(
                    select(User).where(User.id == actor_id).with_for_update()
                )
                current.is_active = False
                await revoker.commit()
            with pytest.raises(HTTPException) as denied:
                await AccountGroupService(writer).update(
                    group_id, actor_id=actor_id, payload=GroupUpdate(name="not allowed")
                )
            assert denied.value.status_code == 403
        async with maker() as check:
            assert (await check.get(Group, group_id)).name == original_name
    finally:
        async with maker() as cleanup:
            if group_id is not None:
                await cleanup.execute(delete(Group).where(Group.id == group_id))
            await cleanup.execute(delete(User).where(User.id == actor_id))
            await cleanup.commit()
