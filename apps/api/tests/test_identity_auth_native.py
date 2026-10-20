from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aap_identity.recovery_crypto import token_hash
from app.db.models.api_key import ApiKey
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.user import User


_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_ARTIFACT = _SQL_DIR / "0235_identity_admission.sql"
_SCHEMA = _SQL_DIR / "0235_identity_admission.schema.json"


def _uuid(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


async def _seed(
    test_engine,
    *,
    active: bool = True,
    role: str = "employee",
    create_key: bool = True,
):
    suffix = uuid.uuid4().hex
    actor_id = uuid.uuid4()
    key_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(
            User(
                id=actor_id,
                email=f"identity-admission-{suffix}@example.test",
                name="Identity admission fixture",
                password_hash="test-only",
                role=role,
                is_active=active,
            )
        )
        await session.flush()
        if create_key:
            session.add(
                ApiKey(
                    id=key_id,
                    user_id=actor_id,
                    name="Identity fixture",
                    key_prefix="ak_123456789",
                    key_hash=f"hash-{suffix}",
                    scopes=["annotations:read", "datasets:read"],
                )
            )
    return {"actor_id": actor_id, "key_id": key_id}


async def _cleanup(test_engine, fixture: dict[str, uuid.UUID], jtis=()) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await session.execute(
            text("DELETE FROM public.audit_logs WHERE actor_id=:id"),
            {"id": fixture["actor_id"]},
        )
        await session.execute(
            delete(PasswordResetToken).where(
                PasswordResetToken.user_id == fixture["actor_id"]
            )
        )
        await session.execute(
            delete(ApiKey).where(ApiKey.user_id == fixture["actor_id"])
        )
        if jtis:
            await session.execute(
                text(
                    "DELETE FROM public.identity_revoked_jtis "
                    "WHERE jti = ANY(CAST(:jtis AS uuid[]))"
                ),
                {"jtis": list(jtis)},
            )
        await session.execute(delete(User).where(User.id == fixture["actor_id"]))


async def _call(session: AsyncSession, sql: str, **params):
    return (await session.execute(text(sql), params)).scalar_one()


def test_frozen_identity_admission_protocol_and_ownership_artifacts():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    functions = {
        "aap_identity_v1.authentication_policy()",
        "aap_identity_v1.read_jwt_principal(uuid, uuid, bigint, timestamptz, boolean)",
        "aap_identity_v1.read_api_key_candidates(text, uuid, integer)",
        "aap_identity_v1.confirm_api_key_principal(uuid, text)",
        "aap_identity_v1.get_credential_generation(uuid)",
        "aap_identity_v1.advance_credential_generation(uuid)",
        "aap_identity_v1.revoke_jti(uuid, timestamptz)",
        "aap_identity_v1.is_jti_revoked(uuid)",
        "aap_identity_v1.retire_account_credentials(uuid)",
        "aap_identity_v1.lock_current_principal(uuid, text, uuid, bigint, timestamptz, text[])",
        "aap_identity_v1.purge_expired_revocations(integer)",
    }
    assert set(schema["x-postgresql-functions"]) == functions
    assert "identity_revoked_jtis" in artifact
    assert "credential_generation bigint NOT NULL DEFAULT 0" in artifact
    assert artifact.count("CREATE FUNCTION") == 12  # public operations + private helper
    assert artifact.count("REVOKE ALL ON FUNCTION") == 12
    assert artifact.count("SET search_path = pg_catalog, pg_temp") == 12
    assert "identity_auth_role=identity_auth_client" in Path(__file__).parents[
        3
    ].joinpath("infra/docker/identity-auth-grants.sql").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_jwt_principal_checks_current_generation_expiry_and_jti_revocation(
    test_engine,
):
    fixture = await _seed(test_engine)
    jti = uuid.uuid4()
    old_generation_jti = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    expiry = datetime.now(UTC) + timedelta(minutes=10)
    try:
        async with maker() as session:
            policy = await _call(
                session, "SELECT aap_identity_v1.authentication_policy()"
            )
            assert policy == {
                "schema": "aap-identity-policy/v1",
                "issuer": "aap-identity/v1",
                "refresh_grace_seconds": 604800,
            }
            reply = await _call(
                session,
                "SELECT aap_identity_v1.read_jwt_principal("
                "CAST(:actor AS uuid), CAST(:jti AS uuid), 0, :expires, false)",
                actor=_uuid(fixture["actor_id"]),
                jti=_uuid(jti),
                expires=expiry,
            )
            assert reply["ok"] is True
            assert reply["data"]["credential_id"] == str(jti)
            assert reply["data"]["generation"] == 0
            refreshable = await _call(
                session,
                "SELECT aap_identity_v1.read_jwt_principal("
                "CAST(:actor AS uuid), CAST(:jti AS uuid), 0, :expires, true)",
                actor=_uuid(fixture["actor_id"]),
                jti=_uuid(uuid.uuid4()),
                expires=datetime.now(UTC) - timedelta(hours=1),
            )
            refresh_expired = await _call(
                session,
                "SELECT aap_identity_v1.read_jwt_principal("
                "CAST(:actor AS uuid), CAST(:jti AS uuid), 0, :expires, true)",
                actor=_uuid(fixture["actor_id"]),
                jti=_uuid(uuid.uuid4()),
                expires=datetime.now(UTC) - timedelta(days=8),
            )
            assert refreshable["ok"] is True
            assert refresh_expired["error"]["code"] == "token_expired"
            await session.commit()

        async with maker.begin() as session:
            await _call(
                session,
                "SELECT aap_identity_v1.revoke_jti(CAST(:jti AS uuid), :retain_until)",
                jti=_uuid(jti),
                retain_until=expiry + timedelta(days=7),
            )
            await _call(
                session,
                "SELECT aap_identity_v1.revoke_jti(CAST(:jti AS uuid), :retain_until)",
                jti=_uuid(jti),
                retain_until=expiry,
            )
            retained_until = await session.scalar(
                text(
                    "SELECT expires_at FROM public.identity_revoked_jtis WHERE jti=:jti"
                ),
                {"jti": jti},
            )
            assert retained_until == expiry + timedelta(days=7)
            revoked = await _call(
                session,
                "SELECT aap_identity_v1.read_jwt_principal("
                "CAST(:actor AS uuid), CAST(:jti AS uuid), 0, :expires, false)",
                actor=_uuid(fixture["actor_id"]),
                jti=_uuid(jti),
                expires=expiry,
            )
            stale = await _call(
                session,
                "SELECT aap_identity_v1.read_jwt_principal("
                "CAST(:actor AS uuid), CAST(:jti AS uuid), 1, :expires, false)",
                actor=_uuid(fixture["actor_id"]),
                jti=_uuid(old_generation_jti),
                expires=expiry,
            )
            expired = await _call(
                session,
                "SELECT aap_identity_v1.read_jwt_principal("
                "CAST(:actor AS uuid), CAST(:jti AS uuid), 0, :expires, false)",
                actor=_uuid(fixture["actor_id"]),
                jti=_uuid(uuid.uuid4()),
                expires=datetime.now(UTC) - timedelta(seconds=1),
            )
            assert revoked["error"]["code"] == "credential_revoked"
            assert stale["error"]["code"] == "generation_outdated"
            assert expired["error"]["code"] == "token_expired"
    finally:
        await _cleanup(test_engine, fixture, (jti,))


@pytest.mark.asyncio
async def test_api_key_candidates_are_ordered_and_confirmation_updates_last_used(
    test_engine,
):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    key_ids = [uuid.UUID(int=10_001), uuid.UUID(int=10_002), uuid.UUID(int=10_003)]
    prefix = "ak_123456789"
    try:
        async with maker.begin() as session:
            for key_id in key_ids:
                session.add(
                    ApiKey(
                        id=key_id,
                        user_id=fixture["actor_id"],
                        name=f"candidate-{key_id.int}",
                        key_prefix=prefix,
                        key_hash=f"hash-{key_id.int}",
                        scopes=["annotations:read", "datasets:read"],
                    )
                )
            await session.flush()
            await session.execute(
                text("UPDATE public.api_keys SET revoked_at=now() WHERE id=:key_id"),
                {"key_id": key_ids[1]},
            )

        async with maker() as session:
            first = await _call(
                session,
                "SELECT aap_identity_v1.read_api_key_candidates(:prefix, NULL, 1)",
                prefix=prefix,
            )
            assert first["schema"] == "aap-identity-candidates/v1"
            assert [item["id"] for item in first["items"]] == [str(key_ids[0])]
            assert first["next_after"] == str(key_ids[0])
            second = await _call(
                session,
                "SELECT aap_identity_v1.read_api_key_candidates("
                ":prefix, CAST(:after_id AS uuid), 10)",
                prefix=prefix,
                after_id=str(key_ids[0]),
            )
            assert [item["id"] for item in second["items"]] == [str(key_ids[2])]
            assert second["next_after"] is None

            reply = await _call(
                session,
                "SELECT aap_identity_v1.confirm_api_key_principal("
                "CAST(:key_id AS uuid), :expected_hash)",
                key_id=str(key_ids[2]),
                expected_hash=f"hash-{key_ids[2].int}",
            )
            assert reply["ok"] is True
            assert reply["data"]["credential_type"] == "api_key"
            assert reply["data"]["credential_id"] == str(key_ids[2])
            assert reply["data"]["scopes"] == ["annotations:read", "datasets:read"]
            await session.commit()

        async with maker() as session:
            last_used = await session.scalar(
                text("SELECT last_used_at FROM public.api_keys WHERE id=:key_id"),
                {"key_id": key_ids[2]},
            )
            assert last_used is not None
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_generation_retirement_and_revocations_share_the_caller_transaction(
    test_engine,
):
    fixture = await _seed(test_engine)
    token_id = uuid.uuid4()
    jti = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            email = await session.scalar(
                select(User.email).where(User.id == fixture["actor_id"])
            )
            session.add(
                PasswordResetToken(
                    id=token_id,
                    user_id=fixture["actor_id"],
                    token_hash=token_hash(uuid.uuid4().hex),
                    issued_email=email,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )

        async with maker() as session:
            transaction = await session.begin()
            generation = await _call(
                session,
                "SELECT aap_identity_v1.advance_credential_generation("
                "CAST(:actor AS uuid))",
                actor=_uuid(fixture["actor_id"]),
            )
            assert generation == 1
            await transaction.rollback()

        async with maker() as session:
            assert (
                await _call(
                    session,
                    "SELECT aap_identity_v1.get_credential_generation(CAST(:actor AS uuid))",
                    actor=_uuid(fixture["actor_id"]),
                )
                == 0
            )
            await session.commit()

        async with maker() as session:
            transaction = await session.begin()
            await _call(
                session,
                "SELECT aap_identity_v1.retire_account_credentials("
                "CAST(:actor AS uuid))",
                actor=_uuid(fixture["actor_id"]),
            )
            await transaction.rollback()
        async with maker() as session:
            assert (
                await _call(
                    session,
                    "SELECT aap_identity_v1.get_credential_generation(CAST(:actor AS uuid))",
                    actor=_uuid(fixture["actor_id"]),
                )
                == 0
            )
            assert (
                await session.scalar(
                    text("SELECT revoked_at FROM public.api_keys WHERE id=:key_id"),
                    {"key_id": fixture["key_id"]},
                )
                is None
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT used_at FROM public.password_reset_tokens WHERE id=:token_id"
                    ),
                    {"token_id": token_id},
                )
                is None
            )
            await session.commit()

        async with maker.begin() as session:
            result = await _call(
                session,
                "SELECT aap_identity_v1.retire_account_credentials("
                "CAST(:actor AS uuid))",
                actor=_uuid(fixture["actor_id"]),
            )
            assert result["schema"] == "aap-identity-retirement/v1"
            assert result["generation"] == 1
            assert str(fixture["key_id"]) in result["revoked_key_ids"]
            assert result["revoked_at"]

        async with maker.begin() as session:
            await _call(
                session,
                "SELECT aap_identity_v1.revoke_jti(CAST(:jti AS uuid), :retain_until)",
                jti=_uuid(jti),
                retain_until=datetime.now(UTC) + timedelta(days=8),
            )
            assert (
                await _call(
                    session,
                    "SELECT aap_identity_v1.is_jti_revoked(CAST(:jti AS uuid))",
                    jti=_uuid(jti),
                )
                is True
            )

        async with maker() as session:
            assert (
                await _call(
                    session,
                    "SELECT aap_identity_v1.get_credential_generation(CAST(:actor AS uuid))",
                    actor=_uuid(fixture["actor_id"]),
                )
                == 1
            )
            key_revoked = await session.scalar(
                text("SELECT revoked_at FROM public.api_keys WHERE id=:key_id"),
                {"key_id": fixture["key_id"]},
            )
            token_used = await session.scalar(
                text(
                    "SELECT used_at FROM public.password_reset_tokens WHERE id=:token_id"
                ),
                {"token_id": token_id},
            )
            assert key_revoked is not None
            assert token_used is not None
            await session.commit()
    finally:
        await _cleanup(test_engine, fixture, (jti,))


@pytest.mark.asyncio
async def test_final_principal_fence_requires_current_scopes_and_holds_jti_lock(
    test_engine,
):
    fixture = await _seed(test_engine)
    jti = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    expiry = datetime.now(UTC) + timedelta(minutes=10)
    try:
        async with maker() as session:
            async with session.begin():
                jwt = await _call(
                    session,
                    "SELECT aap_identity_v1.lock_current_principal("
                    "CAST(:actor AS uuid), 'jwt', CAST(:credential_id AS uuid), "
                    "0, :expires, NULL::text[])",
                    actor=_uuid(fixture["actor_id"]),
                    credential_id=_uuid(jti),
                    expires=expiry,
                )
                assert jwt["ok"] is True
                async with maker() as observer:
                    held = await observer.scalar(
                        text(
                            "SELECT pg_catalog.pg_try_advisory_xact_lock("
                            "pg_catalog.hashtextextended('aap-identity-jti:' || :jti, 0))"
                        ),
                        {"jti": str(jti)},
                    )
                assert held is False

        async with maker.begin() as session:
            api_key = await _call(
                session,
                "SELECT aap_identity_v1.lock_current_principal("
                "CAST(:actor AS uuid), 'api_key', CAST(:credential_id AS uuid), "
                "NULL, NULL, CAST(:required AS text[]))",
                actor=_uuid(fixture["actor_id"]),
                credential_id=_uuid(fixture["key_id"]),
                required=["annotations:read"],
            )
            assert api_key["ok"] is True
            denied = await _call(
                session,
                "SELECT aap_identity_v1.lock_current_principal("
                "CAST(:actor AS uuid), 'api_key', CAST(:credential_id AS uuid), "
                "NULL, NULL, CAST(:required AS text[]))",
                actor=_uuid(fixture["actor_id"]),
                credential_id=_uuid(fixture["key_id"]),
                required=["annotations:write"],
            )
            assert denied["ok"] is False
    finally:
        await _cleanup(test_engine, fixture)
