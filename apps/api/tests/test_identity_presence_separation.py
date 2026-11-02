"""Presence must coexist with authority reads without relaxing security fences."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_identity_accounts_native import (
    _api_key_actor,
    _apply,
    _apply_command,
    _command,
    _jwt_actor,
    _prepare,
)
from tests.test_identity_auth_native import _cleanup, _seed


@pytest.mark.asyncio
async def test_heartbeat_and_authority_share_coexist_and_fence_security(test_engine):
    fixture = await _seed(test_engine, create_key=False)
    actor_id = fixture["actor_id"]
    jti = uuid.uuid4()
    actor = _jwt_actor(actor_id, jti=jti)
    command = _command("heartbeat", uuid.uuid4(), actor)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as writer:
            await writer.begin()
            await writer.execute(
                text("SELECT id FROM public.users WHERE id=:id FOR UPDATE"),
                {"id": actor_id},
            )
            async with maker.begin() as heartbeat:
                assert (await _prepare(heartbeat, command))["error"][
                    "code"
                ] == "account_busy"
            await writer.rollback()
        async with maker() as reader:
            await reader.begin()
            await reader.execute(
                text(
                    "SELECT aap_identity_v1.lock_accounts_share(CAST(:ids AS uuid[]), true)"
                ),
                {"ids": [actor_id]},
            )
            async with maker.begin() as heartbeat:
                assert (await _prepare(heartbeat, command))["ok"] is True
                assert (
                    await _apply(
                        heartbeat,
                        _apply_command(command, base_value=None, new_value=None),
                    )
                )["ok"] is True
            async with maker() as observer:
                status, seen = (
                    await observer.execute(
                        text(
                            "SELECT status,last_seen_at FROM public.identity_account_presence "
                            "WHERE user_id=:id"
                        ),
                        {"id": actor_id},
                    )
                ).one()
                assert status == "online" and seen is not None
            async with maker() as writer:
                await writer.begin()
                await writer.execute(text("SET LOCAL lock_timeout='100ms'"))
                with pytest.raises(DBAPIError) as error:
                    await writer.execute(
                        text("UPDATE public.users SET is_active=false WHERE id=:id"),
                        {"id": actor_id},
                    )
                assert error.value.orig.sqlstate == "55P03"
                await writer.rollback()
            await reader.rollback()

        async with maker() as heartbeat:
            await heartbeat.begin()
            assert (await _prepare(heartbeat, command))["ok"] is True
            assert (
                await _apply(
                    heartbeat, _apply_command(command, base_value=None, new_value=None)
                )
            )["ok"] is True
            async with maker.begin() as reader:
                reply = await reader.scalar(
                    text(
                        "SELECT aap_identity_v1.lock_accounts_share(CAST(:ids AS uuid[]), true)"
                    ),
                    {"ids": [actor_id]},
                )
                assert reply["accounts"][0]["id"] == str(actor_id)
            for change in (
                "is_active=false",
                "role='viewer'",
                "password_hash='changed'",
                "credential_generation=credential_generation+1",
            ):
                async with maker() as writer:
                    await writer.begin()
                    await writer.execute(text("SET LOCAL lock_timeout='100ms'"))
                    with pytest.raises(DBAPIError) as error:
                        await writer.execute(
                            text(f"UPDATE public.users SET {change} WHERE id=:id"),
                            {"id": actor_id},
                        )
                    assert error.value.orig.sqlstate == "55P03"
                    await writer.rollback()
            async with maker() as revoker:
                await revoker.begin()
                await revoker.execute(text("SET LOCAL lock_timeout='100ms'"))
                with pytest.raises(DBAPIError) as error:
                    await revoker.scalar(
                        text("SELECT aap_identity_v1.revoke_jti(:jti,:expires)"),
                        {"jti": jti, "expires": datetime.now(UTC) + timedelta(hours=1)},
                    )
                assert error.value.orig.sqlstate == "55P03"
                await revoker.rollback()
            await heartbeat.rollback()
        async with maker.begin() as revoker:
            await revoker.scalar(
                text("SELECT aap_identity_v1.revoke_jti(:jti,:expires)"),
                {"jti": jti, "expires": datetime.now(UTC) + timedelta(hours=1)},
            )
        async with maker.begin() as heartbeat:
            assert (await _prepare(heartbeat, command))["error"][
                "code"
            ] == "credential_revoked"
    finally:
        await _cleanup(test_engine, fixture, (jti,))


@pytest.mark.asyncio
async def test_heartbeat_keeps_api_key_revocation_fenced(test_engine):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            key_hash = await session.scalar(
                text("SELECT key_hash FROM public.api_keys WHERE id=:id"),
                {"id": fixture["key_id"]},
            )
        actor = _api_key_actor(fixture["actor_id"], fixture["key_id"])
        command = _command("heartbeat", uuid.uuid4(), actor, expected_key_hash=key_hash)
        async with maker() as heartbeat:
            await heartbeat.begin()
            assert (await _prepare(heartbeat, command))["ok"] is True
            assert (
                await _apply(
                    heartbeat, _apply_command(command, base_value=None, new_value=None)
                )
            )["ok"] is True
            for change in ("revoked_at=clock_timestamp()", "key_hash='changed'"):
                async with maker() as writer:
                    await writer.begin()
                    await writer.execute(text("SET LOCAL lock_timeout='100ms'"))
                    with pytest.raises(DBAPIError) as error:
                        await writer.execute(
                            text(f"UPDATE public.api_keys SET {change} WHERE id=:id"),
                            {"id": fixture["key_id"]},
                        )
                    assert error.value.orig.sqlstate == "55P03"
                    await writer.rollback()
            await heartbeat.rollback()
        async with maker.begin() as writer:
            await writer.execute(
                text(
                    "UPDATE public.api_keys SET revoked_at=clock_timestamp() WHERE id=:id"
                ),
                {"id": fixture["key_id"]},
            )
        async with maker.begin() as heartbeat:
            assert (await _prepare(heartbeat, command))["error"][
                "code"
            ] == "credential_revoked"
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_presence_creation_and_account_delete_share_transaction(test_engine):
    fixture = await _seed(test_engine, create_key=False)
    actor_id = fixture["actor_id"]
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            await session.begin()
            row = (
                await session.execute(
                    text(
                        "SELECT status,last_seen_at FROM identity_account_presence WHERE user_id=:id"
                    ),
                    {"id": actor_id},
                )
            ).one()
            assert tuple(row) == ("offline", None)
            await session.execute(
                text("DELETE FROM users WHERE id=:id"), {"id": actor_id}
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM identity_account_presence WHERE user_id=:id"
                    ),
                    {"id": actor_id},
                )
                == 0
            )
            await session.rollback()
        async with maker() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT status FROM identity_account_presence WHERE user_id=:id"
                    ),
                    {"id": actor_id},
                )
                == "offline"
            )
    finally:
        await _cleanup(test_engine, fixture)
