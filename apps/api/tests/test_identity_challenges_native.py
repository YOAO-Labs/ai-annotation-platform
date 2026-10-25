from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.email_verification_token import EmailVerificationToken
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.user import User
from tests.test_identity_auth_native import _seed


_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_ARTIFACT = _SQL_DIR / "0239_identity_recovery.sql"
_SCHEMA = _SQL_DIR / "0239_identity_recovery.schema.json"
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0239_identity_recovery.py"
_NATIVE_SCHEMA = Path(__file__).parents[1] / "app/db/native_schema.py"
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_RECOVERY_GRANTS = _ROOT / "infra/docker/identity-recovery-grants.sql"

_CONFIG = {
    "allow_open_registration": True,
    "email_verification_required": False,
    "frontend_base_url": "https://app.example.test",
    "expire_minutes": 60,
}
_PASSWORD_HASH = "$2b$12$" + "a" * 53


def _audit_context() -> dict:
    return {
        "method": "POST",
        "path": "/api/v1/auth/recovery",
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }


def _command(
    operation: str,
    operation_id: uuid.UUID,
    args: dict,
    *,
    config: dict | None = None,
    fingerprint: str = "f" * 64,
    derivation_id: str = "d" * 64,
) -> dict:
    return {
        "schema": "aap-identity-recovery-native/v1",
        "operation": operation,
        "operation_id": str(operation_id),
        "request_fingerprint": fingerprint,
        "derivation_id": derivation_id,
        "audit_context": _audit_context(),
        "config": config or _CONFIG,
        "args": args,
    }


def _apply_command(
    command: dict,
    *,
    password_hash: str | None = None,
    token_hash: str | None = None,
    token_ciphertext: str | None = None,
    encryption_id: str | None = None,
) -> dict:
    if command["operation"] in {"consume_password_reset", "consume_email_verification"}:
        token_hash = command["args"]["token_hash"]
    return {
        **command,
        "password_hash": password_hash,
        "token_hash": token_hash,
        "token_ciphertext": token_ciphertext,
        "encryption_id": encryption_id,
    }


async def _prepare(session: AsyncSession, request: dict) -> dict:
    result = await session.execute(
        text(
            "SELECT aap_identity_recovery_v1.prepare_recovery_command("
            "CAST(:request AS jsonb))"
        ),
        {"request": json.dumps(request)},
    )
    return result.scalar_one()


async def _apply(session: AsyncSession, request: dict) -> dict:
    result = await session.execute(
        text(
            "SELECT aap_identity_recovery_v1.apply_recovery_command("
            "CAST(:request AS jsonb))"
        ),
        {"request": json.dumps(request)},
    )
    return result.scalar_one()


async def _cleanup_recovery(
    test_engine,
    fixture: dict[str, uuid.UUID],
    operation_ids: list[uuid.UUID],
) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await session.execute(
            text(
                "DELETE FROM public.identity_recovery_operation_receipts "
                "WHERE operation_id = ANY(CAST(:operation_ids AS uuid[]))"
            ),
            {"operation_ids": operation_ids},
        )
        await session.execute(
            text("DELETE FROM public.audit_logs WHERE actor_id=:actor_id"),
            {"actor_id": fixture["actor_id"]},
        )
        await session.execute(
            text(
                "DELETE FROM public.identity_credential_mail_outbox WHERE actor_id=:actor_id"
            ),
            {"actor_id": fixture["actor_id"]},
        )
        await session.execute(
            text(
                "DELETE FROM public.email_verification_tokens WHERE user_id=:actor_id"
            ),
            {"actor_id": fixture["actor_id"]},
        )
        await session.execute(
            text("DELETE FROM public.password_reset_tokens WHERE user_id=:actor_id"),
            {"actor_id": fixture["actor_id"]},
        )
        await session.execute(
            text("DELETE FROM public.api_keys WHERE user_id=:actor_id"),
            {"actor_id": fixture["actor_id"]},
        )
        await session.execute(
            text("DELETE FROM public.users WHERE id=:actor_id"),
            {"actor_id": fixture["actor_id"]},
        )


def test_frozen_identity_recovery_artifacts_and_narrow_role_grants():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    grants = _RECOVERY_GRANTS.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    native_schema = _NATIVE_SCHEMA.read_text(encoding="utf-8")
    statements = [
        statement.strip()
        for statement in artifact.split("-- aap-statement-break")
        if statement.strip()
    ]

    assert len(statements) > 40
    assert all(
        not sa.text(statement.replace(":", r"\:")).compile().params
        for statement in statements
    )
    assert set(schema["x-postgresql-functions"]) == {
        "aap_identity_recovery_v1.recovery_policy()",
        "aap_identity_recovery_v1.prepare_recovery_command(jsonb)",
        "aap_identity_recovery_v1.apply_recovery_command(jsonb)",
        "aap_identity_recovery_v1.mail_policy()",
        "aap_identity_recovery_v1.claim_mail(jsonb)",
        "aap_identity_recovery_v1.renew_mail(jsonb)",
        "aap_identity_recovery_v1.finish_mail(jsonb)",
    }
    assert artifact.count("CREATE FUNCTION") == 14
    assert artifact.count("REVOKE ALL ON FUNCTION") == 14
    assert artifact.count("SECURITY DEFINER") == 14
    assert artifact.count("SET search_path = pg_catalog, pg_temp") == 14
    assert "jsonb_object_length" not in artifact
    assert "CREATE EXTENSION" not in artifact
    assert "pgcrypto" not in artifact
    assert (
        "pg_catalog.sha256(pg_catalog.convert_to(token.token_hash, 'UTF8'))" in artifact
    )
    assert "issued_email = account.email" in artifact
    assert "ON DELETE CASCADE" in artifact
    assert "identity_recovery_operation_receipts" in native_schema
    assert "identity_credential_mail_outbox" in native_schema
    assert (
        "ALTER TABLE public.password_reset_tokens RENAME COLUMN token TO token_hash"
        in artifact
    )
    assert (
        "ALTER TABLE public.email_verification_tokens RENAME COLUMN token TO token_hash"
        in artifact
    )
    assert "aap_identity_v1.retire_account_credentials" not in artifact
    assert 'revision = "0239"' in migration and 'down_revision = "0238"' in migration
    assert (
        "password_reset_tokens" in migration
        and "email_verification_tokens" in migration
    )
    assert "receipts" in migration and "outbox" in migration
    assert "identity_recovery_policy(jsonb)" in module_grants
    assert "record_identity_recovery_event" in module_grants
    assert "token_hash, issued_email" in module_grants
    assert "identity_mail_settings_client" in grants
    assert "aap_settings_v1.mail_delivery_policy(jsonb)" in grants
    assert "pg_advisory_xact_lock" in artifact
    assert "FOR UPDATE" in artifact


@pytest.mark.asyncio
async def test_open_registration_atomically_creates_verification_mail_and_receipt(
    test_engine,
):
    operation_id = uuid.uuid4()
    email = f"recovery-open-{uuid.uuid4().hex}@example.test"
    request = _command(
        "register_open",
        operation_id,
        {"email": email, "name": "Recovery test", "captcha_verified": True},
        config={**_CONFIG, "email_verification_required": True},
    )
    apply = _apply_command(
        request,
        password_hash=_PASSWORD_HASH,
        token_hash="a" * 64,
        token_ciphertext="gAAAAABnative-test-ciphertext",
        encryption_id="e" * 64,
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        transaction = await session.begin()
        try:
            await session.execute(
                text(
                    "INSERT INTO public.system_settings(key,value_type,value_json) "
                    "VALUES ('allow_open_registration','bool','true'::jsonb) "
                    "ON CONFLICT (key) DO UPDATE SET value_type='bool',value_json='true'::jsonb"
                )
            )
            await session.execute(
                text(
                    "INSERT INTO public.system_settings(key,value_type,value_json) "
                    "VALUES ('frontend_base_url','str',to_jsonb('https://recovery.example.test'::text)) "
                    "ON CONFLICT (key) DO UPDATE SET value_type='str',"
                    "value_json=to_jsonb('https://recovery.example.test'::text)"
                )
            )
            prepared = await _prepare(session, request)
            assert prepared["ok"] is True
            assert prepared["data"]["eligible"] is True
            assert prepared["data"]["actor"] is None
            assert prepared["data"]["token"] is None
            assert prepared["data"]["policy"]["email_verification_required"] is True
            assert (
                prepared["data"]["policy"]["frontend_base_url"]
                == "https://recovery.example.test"
            )

            applied = await _apply(session, apply)
            assert applied["ok"] is True
            result = applied["data"]["result"]
            assert result["account"]["id"] == str(operation_id)
            assert result["account"]["email"] == email
            assert result["account"]["role"] == "viewer"
            assert result["account"]["email_verified_at"] is None
            assert result["claims"] is None
            assert result["email_verification_required"] is True
            assert "password_hash" not in result["account"]
            assert applied["data"]["actor"]["is_active"] is True
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.users WHERE id=:id AND status='offline'"
                    ),
                    {"id": operation_id},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.email_verification_tokens "
                        "WHERE id=:id AND user_id=:id AND issued_email=:email AND token_hash=:hash"
                    ),
                    {"id": operation_id, "email": email, "hash": "a" * 64},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_credential_mail_outbox "
                        "WHERE id=:id AND actor_id=:id AND purpose='email_verification' "
                        "AND email=:email AND frontend_base_url='https://recovery.example.test'"
                    ),
                    {"id": operation_id, "email": email},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_recovery_operation_receipts "
                        "WHERE operation_id=:id AND operation='register_open' AND actor_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE actor_id=:id AND action='user.register'"
                    ),
                    {"id": operation_id},
                )
                == 1
            )
            frozen = await session.scalar(
                text(
                    "SELECT result FROM public.identity_recovery_operation_receipts "
                    "WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            assert "email" not in frozen["account"]
            assert "name" not in frozen["account"]
            replay_prepared = await _prepare(session, request)
            assert replay_prepared["ok"] is True
            assert replay_prepared["data"]["replay"] == result
            assert replay_prepared["data"]["password_hash"] is None
            replay = await _apply(session, _apply_command(request))
            assert replay["ok"] is True
            assert replay["data"]["replayed"] is True
            assert replay["data"]["result"] == result
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE actor_id=:id AND action='user.register'"
                    ),
                    {"id": operation_id},
                )
                == 1
            )
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_open_registration_without_verification_issues_frozen_viewer_claims(
    test_engine,
):
    operation_id = uuid.uuid4()
    email = f"recovery-viewer-{uuid.uuid4().hex}@example.test"
    request = _command(
        "register_open",
        operation_id,
        {"email": email, "name": "Viewer registration", "captcha_verified": True},
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        transaction = await session.begin()
        try:
            await session.execute(
                text(
                    "INSERT INTO public.system_settings(key,value_type,value_json) "
                    "VALUES ('allow_open_registration','bool','true'::jsonb) "
                    "ON CONFLICT (key) DO UPDATE SET value_type='bool',value_json='true'::jsonb"
                )
            )
            prepared = await _prepare(session, request)
            assert prepared["ok"] is True
            applied = await _apply(
                session,
                _apply_command(request, password_hash=_PASSWORD_HASH),
            )
            assert applied["ok"] is True
            result = applied["data"]["result"]
            claims = result["claims"]
            assert result["account"]["id"] == str(operation_id)
            assert result["account"]["role"] == "viewer"
            assert result["account"]["status"] == "online"
            assert result["account"]["email_verified_at"] is not None
            assert claims["sub"] == str(operation_id)
            assert claims["jti"] == str(operation_id)
            assert claims["role"] == "viewer"
            assert claims["gen"] == 0
            assert claims["iss"] == "aap-identity/v1"
            assert result["email_verification_required"] is False
            assert result["completed"] is None
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.users WHERE id=:id AND last_login_at IS NULL"
                    ),
                    {"id": operation_id},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.email_verification_tokens WHERE user_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_credential_mail_outbox WHERE actor_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 0
            )

            replay_prepared = await _prepare(session, request)
            assert replay_prepared["ok"] is True
            assert replay_prepared["data"]["password_hash"] == _PASSWORD_HASH
            replay = await _apply(
                session,
                _apply_command(request, password_hash=_PASSWORD_HASH),
            )
            assert replay["ok"] is True
            assert replay["data"]["replayed"] is True
            assert replay["data"]["result"] == result
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE actor_id=:id AND action='user.register'"
                    ),
                    {"id": operation_id},
                )
                == 1
            )
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_no_target_reset_receipt_cannot_retarget_a_later_account(test_engine):
    operation_id = uuid.uuid4()
    email = f"recovery-retarget-{uuid.uuid4().hex}@example.test"
    request = _command(
        "request_password_reset",
        operation_id,
        {"email": email, "captcha_verified": True},
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        transaction = await session.begin()
        try:
            prepared = await _prepare(session, request)
            assert prepared["ok"] is True
            assert prepared["data"]["eligible"] is False
            assert prepared["data"]["actor"] is None
            first = await _apply(session, _apply_command(request))
            assert first["ok"] is True
            assert first["data"]["result"] == {
                "account": None,
                "claims": None,
                "email_verification_required": None,
                "completed": None,
            }
            session.add(
                User(
                    id=uuid.uuid4(),
                    email=email,
                    name="Created after receipt",
                    password_hash=_PASSWORD_HASH,
                    role="employee",
                )
            )
            await session.flush()
            replay_prepare = await _prepare(session, request)
            assert replay_prepare["ok"] is True
            assert replay_prepare["data"]["replay"] == first["data"]["result"]
            replay = await _apply(session, _apply_command(request))
            assert replay["ok"] is True
            assert replay["data"]["replayed"] is True
            assert replay["data"]["result"] == first["data"]["result"]
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.password_reset_tokens WHERE issued_email=:email"
                    ),
                    {"email": email},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_credential_mail_outbox WHERE email=:email"
                    ),
                    {"email": email},
                )
                == 0
            )
        finally:
            await transaction.rollback()


@pytest.mark.asyncio
async def test_reset_and_email_verification_are_one_time_and_preserve_inactive_state(
    test_engine,
):
    fixture = await _seed(test_engine, active=False, create_key=False)
    actor_id = fixture["actor_id"]
    operation_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    reset_hash = "b" * 64
    verification_hashes = ["c" * 64, "d" * 64]
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as session:
            async with session.begin():
                user = await session.get(User, actor_id)
                original_hash = user.password_hash
                original_generation = user.credential_generation
                expires_at = datetime.now(UTC) + timedelta(hours=1)
                session.add(
                    PasswordResetToken(
                        id=uuid.uuid4(),
                        user_id=actor_id,
                        token_hash=reset_hash,
                        issued_email=user.email,
                        expires_at=expires_at,
                    )
                )
                for token_hash in verification_hashes:
                    session.add(
                        EmailVerificationToken(
                            id=uuid.uuid4(),
                            user_id=actor_id,
                            token_hash=token_hash,
                            issued_email=user.email,
                            expires_at=expires_at,
                        )
                    )

        async with maker() as session:
            transaction = await session.begin()
            reset = _command(
                "consume_password_reset",
                operation_ids[0],
                {"token_hash": reset_hash},
            )
            prepared = await _prepare(session, reset)
            assert prepared["ok"] is True
            assert prepared["data"]["eligible"] is False
            assert prepared["data"]["actor"]["is_active"] is False
            assert prepared["data"]["token"]["token_hash"] == reset_hash
            reset_reply = await _apply(session, _apply_command(reset))
            assert reset_reply["ok"] is True
            assert reset_reply["data"]["result"]["completed"] is False
            assert reset_reply["data"]["password_changed"] is False

            verify_replies = []
            for operation_id, token_hash in zip(
                operation_ids[1:], verification_hashes, strict=True
            ):
                verify = _command(
                    "consume_email_verification",
                    operation_id,
                    {"token_hash": token_hash},
                )
                verify_reply = await _apply(session, _apply_command(verify))
                assert verify_reply["ok"] is True
                assert verify_reply["data"]["result"]["completed"] is True
                verify_replies.append(verify_reply)

            current = await session.get(User, actor_id)
            assert current.is_active is False
            assert current.email_verified_at is not None
            assert current.credential_generation == original_generation
            assert current.password_hash == original_hash
            assert reset_reply["data"]["actor"]["is_active"] is False
            assert verify_replies[0]["data"]["email_verified_changed"] is True
            assert verify_replies[1]["data"]["email_verified_changed"] is False
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.password_reset_tokens "
                        "WHERE user_id=:id AND used_at IS NOT NULL"
                    ),
                    {"id": actor_id},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.email_verification_tokens "
                        "WHERE user_id=:id AND used_at IS NOT NULL"
                    ),
                    {"id": actor_id},
                )
                == 2
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:id AND action='user.register'"
                    ),
                    {"id": actor_id},
                )
                == 2
            )
            await transaction.rollback()
    finally:
        await _cleanup_recovery(test_engine, fixture, operation_ids)


@pytest.mark.asyncio
async def test_concurrent_reset_consumers_cannot_both_use_one_challenge(test_engine):
    fixture = await _seed(test_engine, create_key=False)
    actor_id = fixture["actor_id"]
    token_hash = "e" * 64
    operation_ids = [uuid.uuid4(), uuid.uuid4()]
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            account = await session.get(User, actor_id)
            session.add(
                PasswordResetToken(
                    id=uuid.uuid4(),
                    user_id=actor_id,
                    token_hash=token_hash,
                    issued_email=account.email,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )

        first_request = _command(
            "consume_password_reset", operation_ids[0], {"token_hash": token_hash}
        )
        second_request = _command(
            "consume_password_reset", operation_ids[1], {"token_hash": token_hash}
        )
        first = maker()
        second = maker()
        first_transaction = await first.begin()
        second_transaction = await second.begin()
        try:
            first_prepared = await _prepare(first, first_request)
            assert first_prepared["ok"] is True
            second_task = asyncio.create_task(_prepare(second, second_request))
            await asyncio.sleep(0.05)
            assert not second_task.done()
            first_reply = await _apply(
                first,
                _apply_command(first_request, password_hash="$2b$12$" + "f" * 53),
            )
            assert first_reply["ok"] is True
            await first_transaction.commit()
            second_prepared = await asyncio.wait_for(second_task, timeout=5)
            assert second_prepared["ok"] is False
            assert second_prepared["error"]["code"] == "invalid_request"
            await second_transaction.rollback()
        finally:
            if first_transaction.is_active:
                await first_transaction.rollback()
            if second_transaction.is_active:
                await second_transaction.rollback()
            await first.close()
            await second.close()
    finally:
        await _cleanup_recovery(test_engine, fixture, operation_ids)
