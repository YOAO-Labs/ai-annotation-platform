from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.user import User
from tests.test_identity_auth_native import _cleanup, _seed


_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_ARTIFACT = _SQL_DIR / "0238_identity_accounts.sql"
_SCHEMA = _SQL_DIR / "0238_identity_accounts.schema.json"
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0238_identity_accounts.py"
_NATIVE_SCHEMA = Path(__file__).parents[1] / "app/db/native_schema.py"
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_ACCOUNT_GRANTS = _ROOT / "infra/docker/identity-accounts-grants.sql"


def _audit_context() -> dict:
    return {
        "method": "PATCH",
        "path": "/api/v1/auth/me",
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }


def _jwt_actor(
    actor_id: uuid.UUID,
    *,
    jti: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    generation: int = 0,
) -> dict:
    return {
        "actor_id": str(actor_id),
        "name": "stale caller name",
        "email": "stale-caller@example.test",
        "role": "employee",
        "is_active": True,
        "credential_type": "jwt",
        "credential_id": str(jti or uuid.uuid4()),
        "generation": generation,
        "scopes": None,
        "expires_at": (
            expires_at or datetime.now(UTC) + timedelta(minutes=10)
        ).isoformat(),
    }


def _api_key_actor(
    actor_id: uuid.UUID,
    key_id: uuid.UUID,
    *,
    expires_at: datetime | None = None,
) -> dict:
    return {
        "actor_id": str(actor_id),
        "name": "stale caller name",
        "email": "stale-caller@example.test",
        "role": "employee",
        "is_active": True,
        "credential_type": "api_key",
        "credential_id": str(key_id),
        "generation": None,
        "scopes": ["annotations:read", "datasets:read"],
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


def _command(
    operation: str,
    operation_id: uuid.UUID,
    actor: dict,
    *,
    expected_key_hash: str | None = None,
    fingerprint: str = "f" * 64,
    derivation_id: str = "d" * 64,
    audit_context: dict | None = None,
) -> dict:
    return {
        "schema": "aap-identity-accounts-native/v1",
        "operation": operation,
        "operation_id": str(operation_id),
        "actor": actor,
        "expected_key_hash": expected_key_hash,
        "request_fingerprint": fingerprint,
        "derivation_id": derivation_id,
        "audit_context": audit_context or _audit_context(),
    }


def _apply_command(
    command: dict,
    *,
    base_value,
    new_value,
    response_preferences=None,
) -> dict:
    return {
        **command,
        "base_value": base_value,
        "new_value": new_value,
        "response_preferences": response_preferences,
    }


async def _prepare(session: AsyncSession, request: dict | list | str | None) -> dict:
    result = await session.execute(
        text(
            "SELECT aap_identity_accounts_v1.prepare_account_command("
            "CAST(:request AS jsonb))"
        ),
        {"request": json.dumps(request)},
    )
    return result.scalar_one()


async def _apply(session: AsyncSession, request: dict | list | str | None) -> dict:
    result = await session.execute(
        text(
            "SELECT aap_identity_accounts_v1.apply_account_command("
            "CAST(:request AS jsonb))"
        ),
        {"request": json.dumps(request)},
    )
    return result.scalar_one()


def test_frozen_native_account_artifacts_and_function_owners():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    account_grants = _ACCOUNT_GRANTS.read_text(encoding="utf-8")
    native_schema = _NATIVE_SCHEMA.read_text(encoding="utf-8")
    statements = [
        statement.strip()
        for statement in artifact.split("-- aap-statement-break")
        if statement.strip()
    ]

    assert len(statements) == 21
    assert all(
        not sa.text(statement.replace(":", r"\:")).compile().params
        for statement in statements
    )
    assert set(schema["x-postgresql-functions"]) == {
        "aap_identity_accounts_v1.account_policy()",
        "aap_identity_accounts_v1.prepare_account_command(jsonb)",
        "aap_identity_accounts_v1.apply_account_command(jsonb)",
        "aap_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)",
    }
    assert artifact.count("CREATE FUNCTION") == 8
    assert artifact.count("REVOKE ALL ON FUNCTION") == 8
    assert artifact.count("SECURITY DEFINER") == 8
    assert artifact.count("SET search_path = pg_catalog, pg_temp") == 8
    assert "jsonb_object_length" not in artifact
    assert "ON DELETE CASCADE" in artifact
    assert "FOR UPDATE NOWAIT" in artifact
    assert "aap_identity_v1.lock_current_principal" in artifact
    assert "aap_audit_v1.record_identity_account_event" in artifact
    assert "last_seen_at IS NULL OR account.last_seen_at < p_cutoff" in artifact
    receipt_insert = artifact.split(
        "INSERT INTO public.identity_account_operation_receipts", 1
    )[1].split(";", 1)[0]
    assert "password_hash" not in receipt_insert
    assert "new_value" not in receipt_insert
    assert 'revision = "0238"' in migration
    assert 'down_revision = "0237"' in migration
    assert "Cannot remove Identity account operation receipts" in migration
    assert "identity_account_operation_receipts" in native_schema
    assert "request_scope, result, audit_id" in module_grants
    assert "record_identity_account_event" in module_grants
    assert "identity_account_role=identity_account_client" in account_grants
    assert "identity_presence_role=identity_presence_client" in account_grants
    assert account_grants.count("aap_identity_accounts_v1.account_policy()") == 2
    assert (
        account_grants.count("aap_identity_accounts_v1.prepare_account_command(jsonb)")
        == 2
    )
    assert (
        account_grants.count("aap_identity_accounts_v1.apply_account_command(jsonb)")
        == 2
    )
    assert (
        account_grants.count(
            "aap_identity_accounts_v1.mark_inactive_accounts_offline(timestamptz)"
        )
        == 2
    )


@pytest.mark.asyncio
async def test_name_apply_is_atomic_and_replay_never_rewinds_account(test_engine):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    actor = _jwt_actor(fixture["actor_id"])
    request = _command("update_name", operation_id, actor)
    try:
        async with maker.begin() as session:
            old_name = await session.scalar(
                text("SELECT name FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )

        async with maker() as session:
            transaction = await session.begin()
            prepared = await _prepare(session, request)
            assert prepared["ok"] is True
            assert prepared["data"]["account"]["name"] == old_name
            assert prepared["data"]["password_hash"] is None
            assert prepared["data"]["replay"] is None
            applied = await _apply(
                session,
                _apply_command(
                    request,
                    base_value=old_name,
                    new_value="Updated name",
                ),
            )
            assert applied["ok"] is True
            assert applied["data"]["account"]["name"] == "Updated name"
            assert applied["data"]["changed"] is True
            assert "password_hash" not in applied["data"]["account"]
            await transaction.rollback()

        async with maker.begin() as session:
            current_name = await session.scalar(
                text("SELECT name FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            receipt_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_account_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor_id AND action='user.profile_update'"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            assert current_name == old_name
            assert receipt_count == 0
            assert audit_count == 0

        async with maker.begin() as session:
            prepared = await _prepare(session, request)
            accepted = await _apply(
                session,
                _apply_command(
                    request,
                    base_value=old_name,
                    new_value="Updated name",
                ),
            )
            assert accepted["ok"] is True
            frozen = accepted["data"]
            assert prepared["data"]["replay"] is None
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor_id AND action='user.profile_update'"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            assert audit_count == 1

        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET name='Later profile', role='viewer' WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
        replay_request = _command(
            "update_name",
            operation_id,
            _jwt_actor(fixture["actor_id"], jti=uuid.uuid4()),
            audit_context=_audit_context(),
        )
        async with maker.begin() as session:
            replay = await _prepare(session, replay_request)
            assert replay["ok"] is True
            assert replay["principal"]["role"] == "viewer"
            assert replay["data"]["account"]["name"] == "Later profile"
            assert replay["data"]["replay"] == frozen
            replay_apply = await _apply(
                session,
                _apply_command(
                    replay_request,
                    base_value=old_name,
                    new_value="Updated name",
                ),
            )
            assert replay_apply["ok"] is True
            assert replay_apply["data"] == frozen
            conflict_request = _command(
                "update_name",
                operation_id,
                _jwt_actor(fixture["actor_id"], jti=uuid.uuid4()),
                fingerprint="e" * 64,
            )
            conflict = await _prepare(session, conflict_request)
            assert conflict["error"]["code"] == "operation_conflict"
            current_name = await session.scalar(
                text("SELECT name FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            replay_audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor_id AND action='user.profile_update'"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            assert current_name == "Later profile"
            assert replay_audit_count == audit_count

        async with maker.begin() as session:
            await session.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await session.execute(
                text("DELETE FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            remaining = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_account_operation_receipts "
                    "WHERE actor_id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            assert remaining == 0
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_password_change_rechecks_api_key_hash_and_keeps_other_credentials(
    test_engine,
):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    old_hash = "$2b$04$" + "a" * 53
    new_hash = "$2b$12$" + "b" * 53
    key_expiry = datetime.now(UTC) + timedelta(hours=1)
    operation_id = uuid.uuid4()
    reset_at = datetime(2024, 1, 1, tzinfo=UTC)
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET password_hash=:hash, "
                    "password_admin_reset_at=:reset_at WHERE id=:id"
                ),
                {"hash": old_hash, "reset_at": reset_at, "id": fixture["actor_id"]},
            )
            await session.execute(
                text("UPDATE public.api_keys SET expires_at=:expires WHERE id=:id"),
                {"expires": key_expiry, "id": fixture["key_id"]},
            )
            user_email = await session.scalar(
                text("SELECT email FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            key_hash = await session.scalar(
                text("SELECT key_hash FROM public.api_keys WHERE id=:id"),
                {"id": fixture["key_id"]},
            )
        actor = _api_key_actor(
            fixture["actor_id"], fixture["key_id"], expires_at=key_expiry
        )
        request = _command(
            "change_password", operation_id, actor, expected_key_hash=key_hash
        )
        wrong_hash = _command(
            "change_password",
            operation_id,
            actor,
            expected_key_hash="incorrect-current-api-key-hash",
        )
        async with maker.begin() as session:
            rejected = await _prepare(session, wrong_hash)
            assert rejected["error"]["code"] == "credential_invalid"
            prepared = await _prepare(session, request)
            assert prepared["ok"] is True
            assert prepared["data"]["password_hash"] == old_hash
            accepted = await _apply(
                session,
                _apply_command(
                    request,
                    base_value=old_hash,
                    new_value=new_hash,
                ),
            )
            assert accepted["ok"] is True
            assert accepted["data"] == {
                "account": None,
                "preferences": None,
                "previous_avatar_ref": None,
                "changed": None,
            }
            user = await session.execute(
                text(
                    "SELECT password_hash, password_admin_reset_at, credential_generation "
                    "FROM public.users WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            assert tuple(user.one()) == (new_hash, None, 0)
            key = await session.execute(
                text("SELECT key_hash, revoked_at FROM public.api_keys WHERE id=:id"),
                {"id": fixture["key_id"]},
            )
            assert tuple(key.one()) == (key_hash, None)
            receipt = await session.execute(
                text(
                    "SELECT request_scope, result FROM public.identity_account_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            request_scope, result = receipt.one()
            assert result == accepted["data"]
            assert old_hash not in json.dumps(request_scope) + json.dumps(result)
            assert new_hash not in json.dumps(request_scope) + json.dumps(result)
            audit = await session.execute(
                text(
                    "SELECT detail_json FROM public.audit_logs "
                    "WHERE actor_id=:actor_id AND action='user.password_change'"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            audit_detail = audit.scalar_one()
            assert audit_detail == {"email": user_email}
            assert old_hash not in json.dumps(audit_detail)
            assert new_hash not in json.dumps(audit_detail)
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_preferences_keep_raw_base_and_avatar_noop_has_no_cleanup_target(
    test_engine,
):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    raw_preferences = {
        "opaque": {"legacy": [1, 2]},
        "workbench": {"image": {"scale": 1}},
    }
    response_preferences = {"workbench": {"image": {"scale": 2}}}
    merged_preferences = {
        "opaque": {"legacy": [1, 2]},
        "workbench": {"image": {"scale": 2}},
    }
    actor = _jwt_actor(fixture["actor_id"])
    preferences_id = uuid.uuid4()
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET preferences=CAST(:preferences AS jsonb) "
                    "WHERE id=:id"
                ),
                {
                    "preferences": json.dumps(raw_preferences),
                    "id": fixture["actor_id"],
                },
            )
        preferences_request = _command("patch_preferences", preferences_id, actor)
        async with maker.begin() as session:
            prepared = await _prepare(session, preferences_request)
            assert prepared["data"]["account"]["preferences"] == raw_preferences
            accepted = await _apply(
                session,
                _apply_command(
                    preferences_request,
                    base_value=raw_preferences,
                    new_value=merged_preferences,
                    response_preferences=response_preferences,
                ),
            )
            assert accepted["ok"] is True
            assert accepted["data"] == {
                "account": None,
                "preferences": response_preferences,
                "previous_avatar_ref": None,
                "changed": None,
            }
            stored = await session.scalar(
                text("SELECT preferences FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            assert stored == merged_preferences
            audit_count = await session.scalar(
                text("SELECT count(*) FROM public.audit_logs WHERE actor_id=:id"),
                {"id": fixture["actor_id"]},
            )
            assert audit_count == 0

        conflict_id = uuid.uuid4()
        conflict_request = _command("patch_preferences", conflict_id, actor)
        async with maker() as session:
            transaction = await session.begin()
            prepared = await _prepare(session, conflict_request)
            locked_snapshot = prepared["data"]["account"]["preferences"]
            await session.execute(
                text(
                    "UPDATE public.users SET preferences=CAST(:preferences AS jsonb) "
                    "WHERE id=:id"
                ),
                {
                    "preferences": json.dumps({"changed_by": "same transaction"}),
                    "id": fixture["actor_id"],
                },
            )
            stale_apply = await _apply(
                session,
                _apply_command(
                    conflict_request,
                    base_value=locked_snapshot,
                    new_value={"rejected": True},
                    response_preferences={"rejected": True},
                ),
            )
            assert stale_apply["error"]["code"] == "account_busy"
            await transaction.rollback()

        avatar_id = uuid.uuid4()
        await_avatar = _command("set_avatar_ref", avatar_id, actor)
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET avatar_ref='preset:old' WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            prepared = await _prepare(session, await_avatar)
            assert prepared["data"]["account"]["avatar_ref"] == "preset:old"
            changed = await _apply(
                session,
                _apply_command(
                    await_avatar,
                    base_value="preset:old",
                    new_value={
                        "avatar_ref": "preset:new",
                        "kind": "preset_or_clear",
                        "bytes": None,
                    },
                ),
            )
            assert changed["data"]["previous_avatar_ref"] == "preset:old"
            assert changed["data"]["changed"] is True
        noop_id = uuid.uuid4()
        noop_request = _command("set_avatar_ref", noop_id, actor)
        async with maker.begin() as session:
            await _prepare(session, noop_request)
            noop = await _apply(
                session,
                _apply_command(
                    noop_request,
                    base_value="preset:new",
                    new_value={
                        "avatar_ref": "preset:new",
                        "kind": "preset_or_clear",
                        "bytes": None,
                    },
                ),
            )
            assert noop["data"]["changed"] is False
            assert noop["data"]["previous_avatar_ref"] is None
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:id AND action='user.profile_update'"
                ),
                {"id": fixture["actor_id"]},
            )
            assert audit_count == 1
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_account_user_and_operation_lock_contention_fails_fast(test_engine):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    actor = _jwt_actor(fixture["actor_id"])
    request = _command("update_name", operation_id, actor)
    try:
        async with maker() as holder:
            user_transaction = await holder.begin()
            await holder.execute(
                text("SELECT id FROM public.users WHERE id=:id FOR UPDATE"),
                {"id": fixture["actor_id"]},
            )
            async with maker.begin() as session:
                busy = await _prepare(session, request)
                assert busy["error"]["code"] == "account_busy"
            await user_transaction.rollback()

            operation_transaction = await holder.begin()
            acquired = await holder.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended("
                    "'aap-identity-account-operation/' || "
                    "CAST(CAST(:operation_id AS uuid) AS text), 0))"
                ),
                {"operation_id": operation_id},
            )
            assert acquired is True
            async with maker.begin() as session:
                busy = await _prepare(session, request)
                assert busy["error"]["code"] == "credential_busy"
            await operation_transaction.rollback()

        async with maker.begin() as session:
            receipt_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_account_operation_receipts "
                    "WHERE actor_id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            audit_count = await session.scalar(
                text("SELECT count(*) FROM public.audit_logs WHERE actor_id=:id"),
                {"id": fixture["actor_id"]},
            )
            assert receipt_count == 0
            assert audit_count == 0
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_heartbeat_writes_presence_without_a_receipt_or_business_audit(
    test_engine,
):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    actor = _jwt_actor(fixture["actor_id"])
    operation_id = uuid.uuid4()
    request = _command("heartbeat", operation_id, actor)
    try:
        async with maker.begin() as session:
            prepared = await _prepare(session, request)
            assert prepared["ok"] is True
            applied = await _apply(
                session,
                _apply_command(
                    request,
                    base_value=None,
                    new_value=None,
                    response_preferences=None,
                ),
            )
            assert applied["ok"] is True
            assert applied["data"] == {
                "account": None,
                "preferences": None,
                "previous_avatar_ref": None,
                "changed": None,
            }
            state = await session.execute(
                text("SELECT status, last_seen_at FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            status_value, last_seen = state.one()
            assert status_value == "online"
            assert last_seen is not None
            receipt_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_account_operation_receipts "
                    "WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:id AND action IN ('user.profile_update','user.password_change')"
                ),
                {"id": fixture["actor_id"]},
            )
            assert receipt_count == 0
            assert audit_count == 0
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_presence_operator_preserves_cutoff_and_null_timestamp_rules(test_engine):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=5)
    suffix = uuid.uuid4().hex
    inactive_id = uuid.uuid4()
    null_seen_id = uuid.uuid4()
    boundary_id = uuid.uuid4()
    offline_id = uuid.uuid4()
    ids = (inactive_id, null_seen_id, boundary_id, offline_id)
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET status='online', last_seen_at=:stale "
                    "WHERE id=:id"
                ),
                {"stale": now - timedelta(minutes=10), "id": fixture["actor_id"]},
            )
            session.add_all(
                [
                    User(
                        id=inactive_id,
                        email=f"identity-account-inactive-{suffix}@example.test",
                        name="Inactive Presence",
                        password_hash="test-only",
                        role="employee",
                        is_active=False,
                        status="online",
                        last_seen_at=now - timedelta(minutes=10),
                    ),
                    User(
                        id=null_seen_id,
                        email=f"identity-account-null-{suffix}@example.test",
                        name="Null Presence",
                        password_hash="test-only",
                        role="employee",
                        is_active=True,
                        status="online",
                        last_seen_at=None,
                    ),
                    User(
                        id=boundary_id,
                        email=f"identity-account-boundary-{suffix}@example.test",
                        name="Boundary Presence",
                        password_hash="test-only",
                        role="employee",
                        is_active=True,
                        status="online",
                        last_seen_at=cutoff,
                    ),
                    User(
                        id=offline_id,
                        email=f"identity-account-offline-{suffix}@example.test",
                        name="Already Offline",
                        password_hash="test-only",
                        role="employee",
                        is_active=True,
                        status="offline",
                        last_seen_at=now - timedelta(minutes=10),
                    ),
                ]
            )
            await session.flush()
            affected = await session.scalar(
                text(
                    "SELECT aap_identity_accounts_v1.mark_inactive_accounts_offline(:cutoff)"
                ),
                {"cutoff": cutoff},
            )
            assert affected == 3
            states = await session.execute(
                text(
                    "SELECT id, status FROM public.users "
                    "WHERE id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [fixture["actor_id"], *ids]},
            )
            assert {row.id: row.status for row in states} == {
                fixture["actor_id"]: "offline",
                inactive_id: "offline",
                null_seen_id: "offline",
                boundary_id: "online",
                offline_id: "offline",
            }
    finally:
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.users WHERE id=ANY(CAST(:ids AS uuid[]))"),
                {"ids": list(ids)},
            )
        await _cleanup(test_engine, fixture)
