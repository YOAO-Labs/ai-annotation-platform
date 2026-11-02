"""Preference writes must not block account authority or weaken revocation fences."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_identity_accounts_native import (
    _apply,
    _apply_command,
    _command,
    _jwt_actor,
    _prepare,
)
from tests.test_identity_auth_native import _cleanup, _seed


@pytest.mark.asyncio
async def test_changed_preferences_coexist_with_authority_but_fence_security(
    test_engine,
):
    fixture = await _seed(test_engine, create_key=False)
    actor_id = fixture["actor_id"]
    request = _command("patch_preferences", uuid.uuid4(), _jwt_actor(actor_id))
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as writer:
            prepared = await _prepare(writer, request)
            assert prepared["ok"] is True
            initial = prepared["data"]["account"]["preferences"]
            updated = {"ui": {"theme": "dark"}}
            assert initial != updated
            applied = await _apply(
                writer,
                _apply_command(
                    request,
                    base_value=initial,
                    new_value=updated,
                    response_preferences=updated,
                ),
            )
            assert applied["ok"] is True
            async with maker() as reader:
                account = await reader.scalar(
                    text(
                        "SELECT aap_identity_v1.lock_accounts_share("
                        "CAST(:ids AS uuid[]), true)"
                    ),
                    {"ids": [actor_id]},
                )
                assert account["accounts"][0]["id"] == str(actor_id)
                await reader.rollback()
            async with maker() as security_writer:
                await security_writer.execute(text("SET LOCAL lock_timeout='100ms'"))
                with pytest.raises(DBAPIError) as blocked:
                    await security_writer.execute(
                        text("UPDATE public.users SET is_active=false WHERE id=:id"),
                        {"id": actor_id},
                    )
                assert blocked.value.orig.sqlstate == "55P03"
                await security_writer.rollback()
            await writer.commit()
        async with maker.begin() as reader:
            current = await _prepare(
                reader, _command("get_preferences", uuid.uuid4(), _jwt_actor(actor_id))
            )
            assert current["data"]["account"]["preferences"] == updated
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bootstrap_admin", "import_admin", "seed_persona"])
async def test_account_creation_initializes_separate_preferences(test_engine, mode):
    account_id = uuid.uuid4()
    common = {
        "account_id": str(account_id),
        "email": "admin" if mode == "import_admin" else f"{account_id}@example.test",
        "name": "超级管理员" if mode == "import_admin" else "Test account",
        "password_hash": "opaque",
    }
    if mode == "seed_persona":
        payload = {
            **common,
            "schema": "aap-identity-seed-persona/v1",
            "role": "employee",
            "group_name": None,
        }
        function = "create_seed_persona_account"
    else:
        payload = {
            **common,
            "schema": "aap-identity-provisioning/v1",
            "mode": mode,
            "now": "2026-01-01T00:00:00Z" if mode == "bootstrap_admin" else None,
            "request_id": "preference-proof" if mode == "bootstrap_admin" else None,
        }
        function = "create_provisioning_account"
    async with test_engine.connect() as connection:
        result = await connection.scalar(
            text(f"SELECT aap_identity_v1.{function}(CAST(:request AS jsonb))"),
            {"request": json.dumps(payload)},
        )
        assert result["account_id"] == str(account_id)
        assert (
            await connection.scalar(
                text(
                    "SELECT preferences FROM public.identity_account_preferences "
                    "WHERE user_id=:id"
                ),
                {"id": account_id},
            )
            == {}
        )
