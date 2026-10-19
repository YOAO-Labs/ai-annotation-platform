from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.api_key import ApiKey
from app.db.models.user import User
from tests.test_identity_auth_native import _cleanup, _seed


_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_ARTIFACT = _SQL_DIR / "0236_identity_api_keys.sql"
_SCHEMA = _SQL_DIR / "0236_identity_api_keys.schema.json"
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0236_identity_api_keys.py"
_NATIVE_SCHEMA = Path(__file__).parents[1] / "app/db/native_schema.py"
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_KEY_GRANTS = _ROOT / "infra/docker/identity-keys-grants.sql"


def _jwt_actor(actor_id: uuid.UUID, *, jti: uuid.UUID | None = None) -> dict:
    return {
        "actor_id": str(actor_id),
        "name": "stale caller name",
        "email": "stale-caller@example.test",
        "role": "employee",
        "is_active": True,
        "credential_type": "jwt",
        "credential_id": str(jti or uuid.uuid4()),
        "generation": 0,
        "scopes": None,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }


def _api_key_actor(
    actor_id: uuid.UUID, key_id: uuid.UUID, *, expires_at: str | None = None
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
        "scopes": ["annotations:read"],
        "expires_at": expires_at,
    }


def _request(
    operation: str,
    operation_id: uuid.UUID,
    actor: dict,
    payload: dict,
    *,
    expected_key_hash: str | None = None,
    new_key: dict | None = None,
) -> dict:
    return {
        "schema": "aap-identity-keys-native/v1",
        "operation": operation,
        "operation_id": str(operation_id),
        "actor": actor,
        "expected_key_hash": expected_key_hash,
        "payload": payload,
        "new_key": new_key,
    }


def _new_key(key_id: uuid.UUID, *, salt_char: str = "a") -> dict:
    return {
        "key_id": str(key_id),
        "key_prefix": "ak_123456789",
        "key_hash": "$2b$12$" + salt_char * 53,
        "derivation_id": "d" * 64,
    }


async def _execute(session: AsyncSession, request: dict) -> dict:
    result = await session.execute(
        text(
            "SELECT aap_identity_keys_v1.execute_key_command(CAST(:request AS jsonb))"
        ),
        {"request": json.dumps(request)},
    )
    return result.scalar_one()


def test_frozen_native_key_artifacts_and_two_function_owner_surface():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    key_grants = _KEY_GRANTS.read_text(encoding="utf-8")
    native_schema = _NATIVE_SCHEMA.read_text(encoding="utf-8")
    statements = [
        statement.strip()
        for statement in artifact.split("-- aap-statement-break")
        if statement.strip()
    ]

    assert len(statements) == 9
    assert all(
        not sa.text(statement.replace(":", r"\:")).compile().params
        for statement in statements
    )
    assert set(schema["x-postgresql-functions"]) == {
        "aap_identity_keys_v1.key_policy()",
        "aap_identity_keys_v1.execute_key_command(jsonb)",
    }
    api_expiry = schema["$defs"]["Principal"]["allOf"][0]["else"]["properties"][
        "expires_at"
    ]["anyOf"]
    assert {item.get("type") for item in api_expiry} == {"string", "null"}
    assert artifact.count("CREATE FUNCTION") == 2
    assert artifact.count("REVOKE ALL ON FUNCTION") == 2
    assert artifact.count("SECURITY DEFINER") == 2
    assert artifact.count("SET search_path = pg_catalog, pg_temp") == 2
    assert "ON DELETE CASCADE" in artifact
    assert "public.identity_key_operation_receipts" in artifact
    assert "FOR UPDATE NOWAIT" in artifact
    assert "pg_try_advisory_xact_lock" in artifact
    assert "aap_identity_v1.lock_current_principal" in artifact
    assert "jsonb_object_length" not in artifact
    assert "pg_catalog.jsonb_object_keys" in artifact
    assert (
        "key_hash"
        not in artifact.split("INSERT INTO public.identity_key_operation_receipts", 1)[
            1
        ]
    )
    assert "plaintext" not in artifact
    assert 'revision = "0236"' in migration
    assert 'down_revision = "0235"' in migration
    assert "Cannot remove Identity API-key operation receipts" in migration
    assert "identity_key_operation_receipts" in native_schema
    assert 'identity_key_operation_receipts TO :"identity_owner"' in module_grants
    assert "identity_key_role=identity_key_client" in key_grants
    assert key_grants.count("aap_identity_keys_v1.key_policy()") == 2
    assert key_grants.count("aap_identity_keys_v1.execute_key_command(jsonb)") == 2
    assert "identity_auth_role" not in key_grants
    assert "identity_command_role" not in key_grants


@pytest.mark.asyncio
async def test_create_receipt_rolls_back_replays_frozen_result_and_cascades(
    test_engine,
):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    actor = _jwt_actor(fixture["actor_id"])
    request = _request(
        "create_api_key",
        operation_id,
        actor,
        {"name": "automation", "scopes": ["datasets:read"], "expires_in_days": 30},
        new_key=_new_key(operation_id),
    )
    try:
        async with maker() as session:
            transaction = await session.begin()
            first = await _execute(session, request)
            assert first["ok"] is True
            assert first["data"]["key"]["id"] == str(operation_id)
            await transaction.rollback()

        async with maker.begin() as session:
            key_count = await session.scalar(
                text("SELECT count(*) FROM public.api_keys WHERE id=:key_id"),
                {"key_id": operation_id},
            )
            receipt_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_key_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            assert key_count == 0
            assert receipt_count == 0

        async with maker.begin() as session:
            created = await _execute(session, request)
            assert created["ok"] is True
        frozen = created["data"]

        async with maker.begin() as session:
            stored = await session.execute(
                text(
                    "SELECT payload::text, result::text "
                    "FROM public.identity_key_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            payload_text, result_text = stored.one()
            assert "$2b$12$" not in payload_text + result_text
            assert "key_hash" not in payload_text + result_text

            await session.execute(
                text("UPDATE public.users SET name='Current owner' WHERE id=:actor_id"),
                {"actor_id": fixture["actor_id"]},
            )

        replay_request = json.loads(json.dumps(request))
        replay_request["actor"] = _jwt_actor(fixture["actor_id"], jti=uuid.uuid4())
        replay_request["new_key"]["key_hash"] = "$2b$12$" + "b" * 53
        async with maker.begin() as session:
            replay = await _execute(session, replay_request)
            assert replay["ok"] is True
            assert replay["data"] == frozen
            assert replay["principal"]["name"] == "Current owner"

        revoke_id = uuid.uuid4()
        async with maker.begin() as session:
            revoked = await _execute(
                session,
                _request(
                    "revoke_api_key",
                    revoke_id,
                    _jwt_actor(fixture["actor_id"]),
                    {"key_id": str(operation_id)},
                ),
            )
            assert revoked["data"] == {"key_id": str(operation_id), "revoked": True}

        async with maker.begin() as session:
            replay_after_revoke = await _execute(session, replay_request)
            assert replay_after_revoke["data"] == frozen
            stored_revoked_at = await session.scalar(
                text("SELECT revoked_at FROM public.api_keys WHERE id=:key_id"),
                {"key_id": operation_id},
            )
            assert stored_revoked_at is not None

            conflict = json.loads(json.dumps(replay_request))
            conflict["payload"]["name"] = "different scope"
            conflicted = await _execute(session, conflict)
            assert conflicted["error"]["code"] == "operation_conflict"

        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.users WHERE id=:actor_id"),
                {"actor_id": fixture["actor_id"]},
            )
            remaining = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_key_operation_receipts "
                    "WHERE actor_id=:actor_id"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            assert remaining == 0
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_patch_semantics_expired_targets_and_owner_isolation(test_engine):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    other_id = uuid.uuid4()
    other_key_id = uuid.uuid4()
    suffix = uuid.uuid4().hex
    try:
        async with maker.begin() as session:
            session.add(
                User(
                    id=other_id,
                    email=f"identity-keys-other-{suffix}@example.test",
                    name="Other key owner",
                    password_hash="test-only",
                    role="employee",
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                ApiKey(
                    id=other_key_id,
                    user_id=other_id,
                    name="foreign key",
                    key_prefix="ak_987654321",
                    key_hash="test-only",
                    scopes=[],
                )
            )
            await session.execute(
                text("UPDATE public.api_keys SET expires_at=:expired WHERE id=:key_id"),
                {
                    "expired": datetime.now(UTC) - timedelta(days=1),
                    "key_id": fixture["key_id"],
                },
            )

        async with maker.begin() as session:
            empty_patch = await _execute(
                session,
                _request(
                    "update_api_key",
                    uuid.uuid4(),
                    _jwt_actor(fixture["actor_id"]),
                    {"key_id": str(fixture["key_id"]), "patch": {}},
                ),
            )
            assert empty_patch["ok"] is True
            assert empty_patch["data"]["key"]["expires_at"] is not None

            cleared = await _execute(
                session,
                _request(
                    "update_api_key",
                    uuid.uuid4(),
                    _jwt_actor(fixture["actor_id"]),
                    {
                        "key_id": str(fixture["key_id"]),
                        "patch": {"expires_in_days": None},
                    },
                ),
            )
            assert cleared["ok"] is True
            assert cleared["data"]["key"]["expires_at"] is None

            null_name = await _execute(
                session,
                _request(
                    "update_api_key",
                    uuid.uuid4(),
                    _jwt_actor(fixture["actor_id"]),
                    {
                        "key_id": str(fixture["key_id"]),
                        "patch": {"name": None},
                    },
                ),
            )
            assert null_name["error"]["code"] == "invalid_request"

            foreign = await _execute(
                session,
                _request(
                    "update_api_key",
                    uuid.uuid4(),
                    _jwt_actor(fixture["actor_id"]),
                    {"key_id": str(other_key_id), "patch": {}},
                ),
            )
            assert foreign["error"]["code"] == "key_not_found"

            revoked = await _execute(
                session,
                _request(
                    "revoke_api_key",
                    uuid.uuid4(),
                    _jwt_actor(fixture["actor_id"]),
                    {"key_id": str(fixture["key_id"])},
                ),
            )
            assert revoked["ok"] is True

            after_revoke = await _execute(
                session,
                _request(
                    "update_api_key",
                    uuid.uuid4(),
                    _jwt_actor(fixture["actor_id"]),
                    {"key_id": str(fixture["key_id"]), "patch": {}},
                ),
            )
            assert after_revoke["error"]["code"] == "key_not_found"
    finally:
        async with maker.begin() as session:
            await session.execute(delete(ApiKey).where(ApiKey.user_id == other_id))
            await session.execute(delete(User).where(User.id == other_id))
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_api_key_hash_fence_and_current_principal_facts(test_engine):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    key_expiry = datetime.now(UTC) + timedelta(hours=1)
    async with maker.begin() as session:
        await session.execute(
            text("UPDATE public.api_keys SET expires_at=:expires WHERE id=:key_id"),
            {"expires": key_expiry, "key_id": fixture["key_id"]},
        )
        expected_hash = await session.scalar(
            text("SELECT key_hash FROM public.api_keys WHERE id=:key_id"),
            {"key_id": fixture["key_id"]},
        )
    actor = _api_key_actor(
        fixture["actor_id"], fixture["key_id"], expires_at=key_expiry.isoformat()
    )
    try:
        async with maker.begin() as session:
            bad = await _execute(
                session,
                _request(
                    "update_api_key",
                    uuid.uuid4(),
                    actor,
                    {
                        "key_id": str(fixture["key_id"]),
                        "patch": {"name": "should not apply"},
                    },
                    expected_key_hash="wrong-verified-hash",
                ),
            )
            assert bad["error"]["code"] == "credential_invalid"

        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET name='Current owner', "
                    "email='current-owner@example.test', role='viewer' WHERE id=:actor_id"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            valid = await _execute(
                session,
                _request(
                    "update_api_key",
                    uuid.uuid4(),
                    actor,
                    {
                        "key_id": str(fixture["key_id"]),
                        "patch": {"name": "updated by its own key"},
                    },
                    expected_key_hash=expected_hash,
                ),
            )
            assert valid["ok"] is True
            assert valid["principal"]["name"] == "Current owner"
            assert valid["principal"]["email"] == "current-owner@example.test"
            assert valid["principal"]["role"] == "viewer"
            assert valid["principal"]["expires_at"] is not None
            assert valid["data"]["key"]["name"] == "updated by its own key"
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_malformed_json_shapes_return_invalid_request_without_sql_errors(
    test_engine,
):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    actor = _jwt_actor(fixture["actor_id"])
    valid_list = _request("list_api_keys", uuid.uuid4(), actor, {})
    extra_field = json.loads(json.dumps(valid_list))
    extra_field["unexpected"] = True
    requests = [
        [],
        extra_field,
        _request("list_api_keys", uuid.uuid4(), [], {}),
        _request("list_api_keys", uuid.uuid4(), actor, []),
        _request("list_api_keys", uuid.uuid4(), actor, {"extra": True}),
        _request(
            "update_api_key",
            uuid.uuid4(),
            actor,
            {"key_id": str(uuid.uuid4()), "patch": []},
        ),
        _request(
            "create_api_key",
            uuid.uuid4(),
            actor,
            {"name": "invalid"},
            new_key=[],
        ),
    ]
    try:
        async with maker.begin() as session:
            for request in requests:
                reply = await _execute(session, request)
                assert reply["schema"] == "aap-identity-keys-native/v1"
                assert reply["error"]["code"] == "invalid_request"
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_jwt_generation_expiry_and_revocation_are_fenced(test_engine):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    revoked_jti = uuid.uuid4()
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "SELECT aap_identity_v1.revoke_jti("
                    "CAST(:jti AS uuid), :retain_until)"
                ),
                {
                    "jti": revoked_jti,
                    "retain_until": datetime.now(UTC) + timedelta(days=10),
                },
            )

        stale_generation = _jwt_actor(fixture["actor_id"])
        stale_generation["generation"] = 1
        expired = _jwt_actor(fixture["actor_id"])
        expired["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        revoked = _jwt_actor(fixture["actor_id"], jti=revoked_jti)
        async with maker.begin() as session:
            stale = await _execute(
                session,
                _request("list_api_keys", uuid.uuid4(), stale_generation, {}),
            )
            assert stale["error"]["code"] == "generation_outdated"

            expired_reply = await _execute(
                session,
                _request("list_api_keys", uuid.uuid4(), expired, {}),
            )
            assert expired_reply["error"]["code"] == "token_expired"

            revoked_reply = await _execute(
                session,
                _request("list_api_keys", uuid.uuid4(), revoked, {}),
            )
            assert revoked_reply["error"]["code"] == "credential_revoked"
    finally:
        await _cleanup(test_engine, fixture, (revoked_jti,))


@pytest.mark.asyncio
async def test_list_includes_revoked_keys_and_orders_timestamp_ties_by_id(test_engine):
    fixture = await _seed(test_engine, create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    older_id = uuid.uuid4()
    tied_low_id, tied_high_id = sorted(
        (uuid.uuid4(), uuid.uuid4()), key=lambda value: value.int
    )
    tied_at = datetime(2025, 1, 1, tzinfo=UTC)
    try:
        async with maker.begin() as session:
            session.add_all(
                [
                    ApiKey(
                        id=older_id,
                        user_id=fixture["actor_id"],
                        name="older",
                        key_prefix="ak_100000001",
                        key_hash="test-only",
                        scopes=[],
                        created_at=tied_at - timedelta(seconds=1),
                    ),
                    ApiKey(
                        id=tied_low_id,
                        user_id=fixture["actor_id"],
                        name="tied-low",
                        key_prefix="ak_100000002",
                        key_hash="test-only",
                        scopes=[],
                        created_at=tied_at,
                    ),
                    ApiKey(
                        id=tied_high_id,
                        user_id=fixture["actor_id"],
                        name="tied-high",
                        key_prefix="ak_100000003",
                        key_hash="test-only",
                        scopes=[],
                        revoked_at=tied_at,
                        created_at=tied_at,
                    ),
                ]
            )

        async with maker.begin() as session:
            reply = await _execute(
                session,
                _request(
                    "list_api_keys",
                    uuid.uuid4(),
                    _jwt_actor(fixture["actor_id"]),
                    {},
                ),
            )
            items = reply["data"]["items"]
            assert [item["id"] for item in items] == [
                str(tied_high_id),
                str(tied_low_id),
                str(older_id),
            ]
            assert items[0]["revoked_at"] is not None
            assert all("key_hash" not in item for item in items)
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_target_and_operation_contention_return_key_busy_without_waiting(
    test_engine,
):
    fixture = await _seed(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        target_operation = uuid.uuid4()
        target_request = _request(
            "update_api_key",
            target_operation,
            _jwt_actor(fixture["actor_id"]),
            {"key_id": str(fixture["key_id"]), "patch": {}},
        )
        async with maker.begin() as holder:
            await holder.execute(
                text("SELECT id FROM public.api_keys WHERE id=:key_id FOR UPDATE"),
                {"key_id": fixture["key_id"]},
            )
            async with maker.begin() as caller:
                busy_target = await _execute(caller, target_request)
                assert busy_target["error"]["code"] == "key_busy"

        operation_id = uuid.uuid4()
        operation_request = _request(
            "update_api_key",
            operation_id,
            _jwt_actor(fixture["actor_id"]),
            {"key_id": str(fixture["key_id"]), "patch": {}},
        )
        async with maker.begin() as holder:
            await holder.execute(
                text(
                    "SELECT pg_catalog.pg_advisory_xact_lock("
                    "pg_catalog.hashtextextended("
                    "'aap-identity-key-operation/' || :operation_id, 0))"
                ),
                {"operation_id": str(operation_id)},
            )
            async with maker.begin() as caller:
                busy_operation = await _execute(caller, operation_request)
                assert busy_operation["error"]["code"] == "key_busy"
    finally:
        await _cleanup(test_engine, fixture)
