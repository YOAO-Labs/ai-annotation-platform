"""Native, readonly actor-context capability and its SQL snapshot."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aap_identity
from tests.test_identity_auth_native import _call, _cleanup, _seed

_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_SQL = _SQL_DIR / "0250_identity_actor_context.sql"
_SCHEMA = _SQL_DIR / "0250_identity_actor_context.schema.json"
_MIGRATION = (
    Path(__file__).parents[1] / "alembic/versions/0250_identity_actor_context.py"
)
_CANONICAL_SCHEMA = _ROOT / "apps/identity/src/aap_identity/actor_protocol.schema.json"
if not _CANONICAL_SCHEMA.is_file():
    _CANONICAL_SCHEMA = (
        Path(aap_identity.__file__).resolve().parent / "actor_protocol.schema.json"
    )


def test_actor_context_artifact_preserves_the_frozen_admission_contract():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    canonical = json.loads(_CANONICAL_SCHEMA.read_text(encoding="utf-8"))
    sql = _SQL.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")

    assert schema["$id"] == "aap-identity-actor/v1"
    assert schema["$defs"] == canonical["$defs"]
    assert schema["x-postgresql-functions"] == [
        "aap_identity_v1.read_actor_context(uuid, text)"
    ]
    assert 'revision = "0250"' in migration
    assert 'down_revision = "0249"' in migration
    assert "CREATE FUNCTION aap_identity_v1.read_actor_context" in sql
    assert "RETURNS jsonb" in sql
    assert "STABLE" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, pg_temp" in sql
    assert "v_is_active IS DISTINCT FROM TRUE" in sql
    assert "v_role IS DISTINCT FROM p_admitted_role" in sql
    assert "weekly_target_default" in sql
    assert "FOR UPDATE" not in sql
    assert "FOR SHARE" not in sql
    assert "last_used_at" not in sql
    assert "credential_generation" not in sql
    assert "password_hash" not in sql
    assert "audit_logs" not in sql
    assert "INSERT INTO" not in sql
    assert "UPDATE public." not in sql


@pytest.mark.asyncio
async def test_actor_context_preserves_zero_and_null_and_rejects_stale_accounts(
    test_engine,
):
    fixture = await _seed(test_engine, role="super_admin")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    actor_id = fixture["actor_id"]
    key_id = fixture["key_id"]
    try:
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET weekly_target_default=0 WHERE id=:id"),
                {"id": actor_id},
            )

        async with maker() as session:
            actor_email = await session.scalar(
                text("SELECT email FROM public.users WHERE id=:id"),
                {"id": actor_id},
            )
            zero = await _call(
                session,
                "SELECT aap_identity_v1.read_actor_context(:id, :role)",
                id=actor_id,
                role="super_admin",
            )
            assert zero == {
                "schema": "aap-identity-actor/v1",
                "ok": True,
                "data": {
                    "id": str(actor_id),
                    "name": "Identity admission fixture",
                    "email": actor_email,
                    "role": "super_admin",
                    "is_active": True,
                    "weekly_target_default": 0,
                },
            }

        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET weekly_target_default=NULL WHERE id=:id"),
                {"id": actor_id},
            )
        async with maker() as session:
            null_target = await _call(
                session,
                "SELECT aap_identity_v1.read_actor_context(:id, :role)",
                id=actor_id,
                role="super_admin",
            )
            assert null_target["data"]["weekly_target_default"] is None

            absent = await _call(
                session,
                "SELECT aap_identity_v1.read_actor_context(:id, :role)",
                id=uuid.uuid4(),
                role="super_admin",
            )
            invalid_id = await _call(
                session,
                "SELECT aap_identity_v1.read_actor_context(NULL::uuid, 'super_admin')",
            )
            invalid_role = await _call(
                session,
                "SELECT aap_identity_v1.read_actor_context(:id, NULL::text)",
                id=actor_id,
            )
            unsupported_role = await _call(
                session,
                "SELECT aap_identity_v1.read_actor_context(:id, 'historic_role')",
                id=actor_id,
            )
            assert absent["error"]["code"] == "credential_invalid"
            assert invalid_id["error"]["code"] == "invalid_request"
            assert invalid_role["error"]["code"] == "invalid_request"
            assert unsupported_role["error"]["code"] == "invalid_request"

        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET role='employee' WHERE id=:id"),
                {"id": actor_id},
            )
        async with maker() as session:
            stale_role = await _call(
                session,
                "SELECT aap_identity_v1.read_actor_context(:id, 'super_admin')",
                id=actor_id,
            )
            assert stale_role["error"]["code"] == "credential_invalid"

        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET role='super_admin', is_active=false WHERE id=:id"
                ),
                {"id": actor_id},
            )
        async with maker() as session:
            inactive = await _call(
                session,
                "SELECT aap_identity_v1.read_actor_context(:id, 'super_admin')",
                id=actor_id,
            )
            assert inactive["error"]["code"] == "credential_invalid"
            assert (
                await session.scalar(
                    text("SELECT last_used_at FROM public.api_keys WHERE id=:id"),
                    {"id": key_id},
                )
                is None
            )
            assert await session.scalar(
                text("SELECT scopes FROM public.api_keys WHERE id=:id"),
                {"id": key_id},
            ) == ["annotations:read", "datasets:read"]
            assert (
                await session.scalar(
                    text("SELECT credential_generation FROM public.users WHERE id=:id"),
                    {"id": actor_id},
                )
                == 0
            )
    finally:
        await _cleanup(test_engine, fixture)
