"""Account authority fences must not partially revoke credentials or deadlock projections."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
import uuid

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.security import create_access_token
from app.db.models.audit_log import AuditLog
from app.db.models.group import Group
from app.db.models.user import User
from app.deps import get_db
from tests.factory import create_user


async def _group_command(db, actor_id, operation, payload, *, operation_id=None):
    from aap_identity.contract import Principal
    from aap_identity.group_contract import (
        decode_native_group_command,
        decode_native_group_reply,
    )

    actor = await db.get(User, actor_id)
    command = decode_native_group_command(
        {
            "schema": "aap-identity-groups-native/v1",
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "actor": Principal(
                actor_id=actor.id,
                name=actor.name,
                email=actor.email,
                role=actor.role,
                is_active=True,
                credential_type="jwt",
                credential_id=uuid.uuid4(),
                generation=actor.credential_generation,
                scopes=None,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ).model_dump(mode="json"),
            "expected_key_hash": None,
            "request_fingerprint": hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest(),
            "derivation_id": "a" * 64,
            "audit_context": {
                "method": None,
                "path": None,
                "ip": None,
                "request_id": None,
            },
            "payload": payload,
        }
    )
    raw = await db.scalar(
        text(
            "SELECT aap_identity_groups_v1.execute_group_command(CAST(:command AS jsonb))"
        ),
        {"command": command.model_dump_json(by_alias=True)},
    )
    raw = json.loads(raw) if isinstance(raw, str) else raw
    return decode_native_group_reply(command, raw)


async def _cleanup_groups(maker, user_ids, group_ids):
    async with maker.begin() as db:
        await db.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
        await db.execute(
            text(
                "DELETE FROM public.identity_group_operation_receipts WHERE actor_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": user_ids},
        )
        await db.execute(delete(AuditLog).where(AuditLog.actor_id.in_(user_ids)))
        await db.execute(delete(User).where(User.id.in_(user_ids)))
        await db.execute(delete(Group).where(Group.id.in_(group_ids)))


@pytest.mark.parametrize("operation", ["rename", "delete"])
async def test_cross_group_managers_do_not_deadlock_account_projections(
    test_engine, operation
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    key = uuid.uuid4().hex
    async with maker.begin() as setup:
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
    ready, arrived = asyncio.Event(), 0

    async def mutate(actor_id, group_id, name):
        nonlocal arrived
        async with maker() as db:
            await db.execute(
                select(Group.id).where(Group.id == group_id).with_for_update()
            )
            arrived += 1
            if arrived == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 5)
            payload = {"group_id": str(group_id)}
            if operation == "rename":
                payload.update(name=name, description=None)
            await _group_command(
                db,
                actor_id,
                "update_group" if operation == "rename" else "delete_group",
                payload,
            )
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
        await _cleanup_groups(maker, user_ids, group_ids)


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
    from aap_identity.group_contract import GroupFailure

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as setup:
        actor = await create_user(
            setup, "project_admin", f"authority-{uuid.uuid4()}@groups.test", "Manager"
        )
        actor_id = actor.id
    group_id = uuid.uuid4()
    original_name = f"fenced-{uuid.uuid4()}"
    try:
        async with maker() as writer:
            reply = await _group_command(
                writer,
                actor_id,
                "create_group",
                {"name": original_name, "description": None},
                operation_id=group_id,
            )
            assert reply.data.group.id == group_id
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
            async with maker.begin() as revoker:
                current = await revoker.scalar(
                    select(User).where(User.id == actor_id).with_for_update()
                )
                current.is_active = False
            with pytest.raises(GroupFailure) as denied:
                await _group_command(
                    writer,
                    actor_id,
                    "update_group",
                    {
                        "group_id": str(group_id),
                        "name": "not allowed",
                        "description": None,
                    },
                )
            assert denied.value.code in {"credential_invalid", "user_inactive"}
        async with maker() as check:
            assert (await check.get(Group, group_id)).name == original_name
    finally:
        await _cleanup_groups(maker, [actor_id], [group_id])
