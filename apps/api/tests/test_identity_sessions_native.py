from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_identity_auth_native import _cleanup, _seed


_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_ARTIFACT = _SQL_DIR / "0237_identity_sessions.sql"
_SCHEMA = _SQL_DIR / "0237_identity_sessions.schema.json"
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0237_identity_sessions.py"
_NATIVE_SCHEMA = Path(__file__).parents[1] / "app/db/native_schema.py"
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_SESSION_GRANTS = _ROOT / "infra/docker/identity-sessions-grants.sql"

_POLICY = {
    "expire_minutes": 60,
    "email_verification_required": False,
    "derivation_id": "d" * 64,
}


def _audit_context() -> dict:
    return {
        "method": "POST",
        "path": "/v1/identity/sessions/execute",
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


def _request(
    operation: str,
    operation_id: uuid.UUID,
    actor: dict | None,
    *,
    expected_key_hash: str | None = None,
    login: dict | None = None,
    request_fingerprint: str | None = None,
    policy: dict | None = None,
    audit_context: dict | None = None,
    user_agent: str = "identity-session-native-test",
) -> dict:
    return {
        "schema": "aap-identity-sessions-native/v1",
        "operation": operation,
        "operation_id": str(operation_id),
        "actor": actor,
        "expected_key_hash": expected_key_hash,
        "login": login,
        "policy": policy or _POLICY,
        "request_fingerprint": request_fingerprint,
        "audit_context": audit_context or _audit_context(),
        "user_agent": user_agent,
    }


async def _execute(session: AsyncSession, request: dict | list | str | None) -> dict:
    result = await session.execute(
        text(
            "SELECT aap_identity_sessions_v1.execute_session_command(CAST(:request AS jsonb))"
        ),
        {"request": json.dumps(request)},
    )
    return result.scalar_one()


def test_frozen_native_session_artifacts_and_receipt_owner_grants():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    session_grants = _SESSION_GRANTS.read_text(encoding="utf-8")
    native_schema = _NATIVE_SCHEMA.read_text(encoding="utf-8")
    statements = [
        statement.strip()
        for statement in artifact.split("-- aap-statement-break")
        if statement.strip()
    ]

    assert len(statements) == 15
    assert all(
        not sa.text(statement.replace(":", r"\:")).compile().params
        for statement in statements
    )
    assert set(schema["x-postgresql-functions"]) == {
        "aap_identity_sessions_v1.session_policy()",
        "aap_identity_sessions_v1.read_login_candidate(text)",
        "aap_identity_sessions_v1.execute_session_command(jsonb)",
    }
    assert "account_busy" in schema["x-native-errors"]
    assert set(schema["$defs"]["LoginInput"]["required"]) == {
        "email",
        "actor_id",
        "expected_password_hash",
        "password_matches",
        "failed_count",
    }
    assert set(schema["$defs"]["LoginInput"]["properties"]) == set(
        schema["$defs"]["LoginInput"]["required"]
    )
    assert artifact.count("CREATE FUNCTION") == 5
    assert artifact.count("REVOKE ALL ON FUNCTION") == 5
    assert artifact.count("SECURITY DEFINER") == 5
    assert artifact.count("SET search_path = pg_catalog, pg_temp") == 5
    assert "ON DELETE CASCADE" in artifact
    assert "FOR UPDATE NOWAIT" in artifact
    assert "pg_try_advisory_xact_lock" in artifact
    assert "aap_identity_v1.read_jwt_principal" in artifact
    assert "aap_identity_v1.lock_current_principal" in artifact
    assert "jsonb_object_length" not in artifact
    assert (
        "password_hash"
        not in artifact.split(
            "INSERT INTO public.identity_session_operation_receipts", 1
        )[1]
    )
    assert 'revision = "0237"' in migration
    assert 'down_revision = "0236"' in migration
    assert "Cannot remove Identity session operation receipts" in migration
    assert "identity_session_operation_receipts" in native_schema
    assert "request_scope, result, audit_id" in module_grants
    assert "record_identity_session_event" in module_grants
    assert "identity_session_role=identity_session_client" in session_grants
    assert session_grants.count("aap_identity_sessions_v1.session_policy()") == 2
    assert (
        session_grants.count("aap_identity_sessions_v1.read_login_candidate(text)") == 2
    )
    assert (
        session_grants.count("aap_identity_sessions_v1.execute_session_command(jsonb)")
        == 2
    )
    assert "identity_auth_role" not in session_grants
    assert "identity_key_role" not in session_grants


@pytest.mark.asyncio
async def test_login_receipt_rollback_and_replay_never_rewinds_user_or_audit(
    test_engine,
):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    request_fingerprint = "f" * 64
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET password_hash='candidate-hash' WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            email = await session.scalar(
                text("SELECT email FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            candidate = await session.scalar(
                text("SELECT aap_identity_sessions_v1.read_login_candidate(:email)"),
                {"email": email},
            )
            assert candidate["item"] == {
                "actor_id": str(fixture["actor_id"]),
                "password_hash": "candidate-hash",
            }
        login = {
            "email": email,
            "actor_id": str(fixture["actor_id"]),
            "expected_password_hash": "candidate-hash",
            "password_matches": True,
            "failed_count": 0,
        }
        request = _request(
            "login",
            operation_id,
            None,
            login=login,
            request_fingerprint=request_fingerprint,
        )

        async with maker() as session:
            transaction = await session.begin()
            stale_hash = json.loads(json.dumps(request))
            stale_hash["login"]["expected_password_hash"] = "stale-candidate-hash"
            stale_email = json.loads(json.dumps(request))
            stale_email["login"]["email"] = "stale-candidate@example.test"
            for stale_candidate in (stale_hash, stale_email):
                drift = await _execute(session, stale_candidate)
                assert drift["error"]["code"] == "account_busy"
            rolled_back = await _execute(session, request)
            assert rolled_back["ok"] is True
            await transaction.rollback()

        async with maker.begin() as session:
            state = await session.execute(
                text(
                    "SELECT last_login_at, last_seen_at, status "
                    "FROM public.users WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            last_login, last_seen, status = state.one()
            receipt_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_session_operation_receipts "
                    "WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor_id AND action='auth.login'"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            assert (last_login, last_seen, status) == (None, None, "offline")
            assert receipt_count == 0
            assert audit_count == 0

        async with maker.begin() as session:
            accepted = await _execute(session, request)
            assert accepted["ok"] is True
            frozen = accepted["data"]
            initial_audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor_id AND action='auth.login'"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            receipt = await session.execute(
                text(
                    "SELECT request_scope, result, audit_id "
                    "FROM public.identity_session_operation_receipts "
                    "WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            stored_scope, stored_result, audit_id = receipt.one()
            assert stored_result == frozen
            assert audit_id is not None
            assert initial_audit_count == 1
            assert "candidate-hash" not in json.dumps(stored_scope) + json.dumps(
                stored_result
            )

            sentinel = datetime(2001, 1, 1, tzinfo=UTC)
            await session.execute(
                text(
                    "UPDATE public.users SET name='Current account', role='viewer', "
                    "credential_generation=9, status='offline', last_login_at=:sentinel, "
                    "last_seen_at=:sentinel WHERE id=:id"
                ),
                {"sentinel": sentinel, "id": fixture["actor_id"]},
            )

        replay_request = json.loads(json.dumps(request))
        replay_request["audit_context"] = _audit_context()
        replay_request["user_agent"] = "changed metadata is outside the replay scope"
        async with maker.begin() as session:
            conflict_request = json.loads(json.dumps(replay_request))
            conflict_request["request_fingerprint"] = "e" * 64
            conflict = await _execute(session, conflict_request)
            assert conflict["error"]["code"] == "operation_conflict"
            replay = await _execute(session, replay_request)
            assert replay["ok"] is True
            assert replay["data"] == frozen
            assert replay["principal"]["name"] == "Current account"
            assert replay["principal"]["role"] == "viewer"
            assert replay["data"]["claims"]["role"] == "employee"
            assert replay["principal"]["generation"] == frozen["claims"]["gen"]
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor_id AND action='auth.login'"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            state = await session.execute(
                text(
                    "SELECT credential_generation, status, last_login_at, last_seen_at "
                    "FROM public.users WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            generation, current_status, last_login, last_seen = state.one()
            assert audit_count == initial_audit_count
            assert (generation, current_status, last_login, last_seen) == (
                9,
                "offline",
                sentinel,
                sentinel,
            )
            assert stored_scope["actor_id"] == str(fixture["actor_id"])

        async with maker.begin() as session:
            await session.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await session.execute(
                text("DELETE FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            remaining = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_session_operation_receipts "
                    "WHERE actor_id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            assert remaining == 0
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_api_key_with_finite_actor_expiry_replays_only_after_current_hash_fence(
    test_engine,
):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    key_expiry = datetime.now(UTC) + timedelta(hours=1)
    operation_id = uuid.uuid4()
    try:
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.api_keys SET expires_at=:expires WHERE id=:id"),
                {"expires": key_expiry, "id": fixture["key_id"]},
            )
            key_hash = await session.scalar(
                text("SELECT key_hash FROM public.api_keys WHERE id=:id"),
                {"id": fixture["key_id"]},
            )
        actor = _api_key_actor(
            fixture["actor_id"], fixture["key_id"], expires_at=key_expiry
        )
        request = _request(
            "logout_all",
            operation_id,
            actor,
            expected_key_hash=key_hash,
        )
        async with maker.begin() as session:
            wrong_hash = _request(
                "logout_all",
                operation_id,
                actor,
                expected_key_hash="different-current-key-hash",
            )
            rejected = await _execute(session, wrong_hash)
            assert rejected["error"]["code"] == "credential_invalid"
            accepted = await _execute(session, request)
            assert accepted["ok"] is True
            frozen = accepted["data"]
            assert frozen["claims"]["gen"] == 1
            initial_audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor_id AND action='auth.logout_all'"
                ),
                {"actor_id": fixture["actor_id"]},
            )

        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET name='Updated owner', role='viewer', "
                    "credential_generation=11, status='online' WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
        replay_actor = _api_key_actor(fixture["actor_id"], fixture["key_id"])
        replay_request = _request(
            "logout_all",
            operation_id,
            replay_actor,
            expected_key_hash=key_hash,
        )
        async with maker.begin() as session:
            replay = await _execute(session, replay_request)
            assert replay["ok"] is True
            assert replay["data"] == frozen
            assert replay["principal"]["name"] == "Updated owner"
            assert replay["principal"]["role"] == "viewer"
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor_id AND action='auth.logout_all'"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            state = await session.execute(
                text(
                    "SELECT credential_generation, status FROM public.users WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            assert state.one() == (11, "online")
            assert audit_count == initial_audit_count

        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.api_keys SET expires_at=:expires WHERE id=:id"),
                {
                    "expires": datetime.now(UTC) - timedelta(seconds=1),
                    "id": fixture["key_id"],
                },
            )
            stale_replay = await _execute(session, replay_request)
            assert stale_replay["error"]["code"] == "credential_revoked"
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_logout_all_generation_exhaustion_leaves_no_audit_or_receipt(test_engine):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET credential_generation=9223372036854775807 "
                    "WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            key_hash = await session.scalar(
                text("SELECT key_hash FROM public.api_keys WHERE id=:id"),
                {"id": fixture["key_id"]},
            )
        operation_id = uuid.uuid4()
        request = _request(
            "logout_all",
            operation_id,
            _api_key_actor(fixture["actor_id"], fixture["key_id"]),
            expected_key_hash=key_hash,
        )
        with pytest.raises(sa.exc.DBAPIError):
            async with maker.begin() as session:
                await _execute(session, request)

        async with maker.begin() as session:
            state = await session.execute(
                text(
                    "SELECT credential_generation, status "
                    "FROM public.users WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            assert tuple(state.one()) == (9223372036854775807, "offline")
            receipt_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_session_operation_receipts "
                    "WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor_id AND action='auth.logout_all'"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            assert receipt_count == 0
            assert audit_count == 0
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_account_and_credential_lock_contention_fail_fast(test_engine):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    actor_jti = uuid.uuid4()
    actor = _jwt_actor(fixture["actor_id"], jti=actor_jti)
    user_lock_id = uuid.uuid4()
    jti_lock_id = uuid.uuid4()
    operation_lock_id = uuid.uuid4()
    try:
        async with maker() as holder:
            user_transaction = await holder.begin()
            await holder.execute(
                text("SELECT id FROM public.users WHERE id=:id FOR UPDATE"),
                {"id": fixture["actor_id"]},
            )
            async with maker.begin() as session:
                busy = await _execute(
                    session,
                    _request("logout", user_lock_id, actor),
                )
                assert busy["error"]["code"] == "account_busy"
            await user_transaction.rollback()

            jti_transaction = await holder.begin()
            acquired = await holder.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock_shared(hashtextextended("
                    "'aap-identity-jti:' || CAST(CAST(:jti AS uuid) AS text), 0))"
                ),
                {"jti": actor_jti},
            )
            assert acquired is True
            async with maker.begin() as session:
                busy = await _execute(
                    session,
                    _request("logout", jti_lock_id, actor),
                )
                assert busy["error"]["code"] == "credential_busy"
            await jti_transaction.rollback()

            operation_transaction = await holder.begin()
            acquired = await holder.scalar(
                text(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended("
                    "'aap-identity-session-operation/' || "
                    "CAST(CAST(:operation_id AS uuid) AS text), 0))"
                ),
                {"operation_id": operation_lock_id},
            )
            assert acquired is True
            async with maker.begin() as session:
                busy = await _execute(
                    session,
                    _request("refresh", operation_lock_id, actor),
                )
                assert busy["error"]["code"] == "credential_busy"
            await operation_transaction.rollback()

        async with maker.begin() as session:
            state = await session.execute(
                text("SELECT status, last_seen_at FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            assert tuple(state.one()) == ("offline", None)
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor_id AND action IN ('auth.logout','auth.token_refresh')"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            receipt_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_session_operation_receipts "
                    "WHERE actor_id=:actor_id"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            assert audit_count == 0
            assert receipt_count == 0
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_refresh_grace_logout_revocation_and_malformed_inputs(test_engine):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    old_expiry = datetime.now(UTC) - timedelta(hours=1)
    outside_grace_expiry = datetime.now(UTC) - timedelta(days=8)
    refresh_jti = uuid.uuid4()
    logout_jti = uuid.uuid4()
    logout_expiry = datetime.now(UTC) + timedelta(minutes=10)
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET password_hash='native-test' WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            outside_grace = await _execute(
                session,
                _request(
                    "refresh",
                    uuid.uuid4(),
                    _jwt_actor(
                        fixture["actor_id"],
                        jti=uuid.uuid4(),
                        expires_at=outside_grace_expiry,
                    ),
                ),
            )
            assert outside_grace["error"]["code"] == "token_expired"
            refreshed = await _execute(
                session,
                _request(
                    "refresh",
                    uuid.uuid4(),
                    _jwt_actor(
                        fixture["actor_id"], jti=refresh_jti, expires_at=old_expiry
                    ),
                ),
            )
            assert refreshed["ok"] is True
            assert refreshed["data"]["claims"]["jti"] != str(refresh_jti)
            assert (
                datetime.fromisoformat(refreshed["principal"]["expires_at"])
                == old_expiry
            )

            logout_id = uuid.uuid4()
            logout_request = _request(
                "logout",
                logout_id,
                _jwt_actor(
                    fixture["actor_id"], jti=logout_jti, expires_at=logout_expiry
                ),
            )
            logged_out = await _execute(session, logout_request)
            assert logged_out["ok"] is True
            assert logged_out["data"]["claims"] is None
            revoked_until = await session.scalar(
                text(
                    "SELECT expires_at FROM public.identity_revoked_jtis WHERE jti=:jti"
                ),
                {"jti": logout_jti},
            )
            assert revoked_until == logout_expiry + timedelta(days=7)
        async with maker.begin() as session:
            retry = await _execute(session, logout_request)
            assert retry["error"]["code"] == "credential_revoked"

            valid_actor = _jwt_actor(fixture["actor_id"])
            malformed = [
                [],
                "not-an-object",
                None,
                _request("logout_all", uuid.uuid4(), [], login=None),
                _request(
                    "logout_all",
                    uuid.uuid4(),
                    {**valid_actor, "scopes": "not-an-array"},
                ),
                _request(
                    "logout_all",
                    uuid.uuid4(),
                    {**valid_actor, "scopes": [{}]},
                ),
            ]
            invalid_api_actor = _api_key_actor(fixture["actor_id"], uuid.uuid4())
            malformed.extend(
                [
                    _request(
                        "logout_all",
                        uuid.uuid4(),
                        {**invalid_api_actor, "scopes": "not-an-array"},
                        expected_key_hash="verified-hash",
                    ),
                    _request(
                        "logout_all",
                        uuid.uuid4(),
                        {**invalid_api_actor, "scopes": [{}]},
                        expected_key_hash="verified-hash",
                    ),
                ]
            )
            login_with_extra_field = _request(
                "login",
                uuid.uuid4(),
                None,
                login={
                    "email": "nobody@example.test",
                    "actor_id": None,
                    "expected_password_hash": None,
                    "password_matches": False,
                    "failed_count": 0,
                    "unexpected": "extra native login field",
                },
                request_fingerprint="f" * 64,
            )
            malformed.append(login_with_extra_field)
            for request in malformed:
                reply = await _execute(session, request)
                assert reply["schema"] == "aap-identity-sessions-native/v1"
                assert reply["error"]["code"] == "invalid_request"
    finally:
        await _cleanup(test_engine, fixture, (logout_jti,))


@pytest.mark.asyncio
async def test_login_checks_password_before_inactive_and_verification_gates(
    test_engine,
):
    fixture = await _seed(test_engine, active=False, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET password_hash='candidate-hash' WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            email = await session.scalar(
                text("SELECT email FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            candidate = await session.scalar(
                text("SELECT aap_identity_sessions_v1.read_login_candidate(:email)"),
                {"email": email},
            )
            assert candidate["item"]["actor_id"] == str(fixture["actor_id"])
            assert candidate["item"]["password_hash"] == "candidate-hash"
        login = {
            "email": email,
            "actor_id": str(fixture["actor_id"]),
            "expected_password_hash": "candidate-hash",
            "password_matches": False,
            "failed_count": 3,
        }
        bad_password = _request(
            "login", uuid.uuid4(), None, login=login, request_fingerprint="a" * 64
        )
        inactive_login = json.loads(json.dumps(bad_password))
        inactive_login["operation_id"] = str(uuid.uuid4())
        inactive_login["login"]["password_matches"] = True
        async with maker.begin() as session:
            bad_reply = await _execute(session, bad_password)
            inactive_reply = await _execute(session, inactive_login)
            assert bad_reply["error"] == {
                "code": "invalid_credentials",
                "failed_count": 3,
            }
            assert inactive_reply["error"]["code"] == "user_inactive"

        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET is_active=true WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
        unverified_login = json.loads(json.dumps(inactive_login))
        unverified_login["operation_id"] = str(uuid.uuid4())
        unverified_login["policy"]["email_verification_required"] = True
        unverified = await _execute_with_maker(maker, unverified_login)
        assert unverified["error"]["code"] == "email_not_verified"

        async with maker.begin() as session:
            audits = await session.execute(
                text(
                    "SELECT status_code, detail_json->>'result' "
                    "FROM public.audit_logs WHERE action='auth.login' "
                    "AND (actor_id=:actor_id OR detail_json->>'email'=:email) "
                    "ORDER BY id"
                ),
                {"actor_id": fixture["actor_id"], "email": email},
            )
            assert [tuple(row) for row in audits.all()] == [
                (401, "invalid_credentials"),
                (403, "deactivated"),
                (400, "email_not_verified"),
            ]
            state = await session.execute(
                text(
                    "SELECT status, last_login_at, last_seen_at "
                    "FROM public.users WHERE id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            assert tuple(state.one()) == ("offline", None, None)
            receipts = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_session_operation_receipts "
                    "WHERE actor_id=:actor_id"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            assert receipts == 0
    finally:
        await _cleanup(test_engine, fixture)


async def _execute_with_maker(maker, request: dict) -> dict:
    async with maker.begin() as session:
        return await _execute(session, request)
