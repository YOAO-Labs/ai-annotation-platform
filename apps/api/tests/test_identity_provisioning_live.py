from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import os
import secrets
import subprocess
import uuid

import httpx
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_identity.security import hash_password, verify_password
from app.db.models.audit_log import AuditLog
from app.db.models.user import User
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_admission_live import (
    ROOT,
    _start_api,
    _start_container_identity,
)
from tests.test_identity_sessions_live import _login_redis
from tests.test_module_data_roles import _role_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_PROVISIONING_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned provisioning verifier",
    ),
]

_PROTOCOL = "aap-identity-provisioning/v1"
_LIVE_FIXTURE = ROOT / "apps/api/tests/fixtures/identity_provisioning_legacy.json"


def _legacy_records() -> dict:
    if not _LIVE_FIXTURE.is_file():
        pytest.fail(
            "Root did not provide the frozen actual legacy provisioning fixture"
        )
    fixture = json.loads(_LIVE_FIXTURE.read_text(encoding="utf-8"))
    assert fixture.get("kind") == "actual-legacy-identity-provisioning"
    return fixture["records"]


def _provisioning_target(test_engine) -> tuple[str, str]:
    url = make_url(
        _role_url(
            "AAP_MODULE_DATA_IDENTITY_PROVISIONING_URL",
            "identity_provisioning_client",
        )
    )
    assert url.host == "127.0.0.1"
    assert url.port == test_engine.url.port
    assert url.database == test_engine.url.database
    assert url.database.endswith("_test")
    container_id = os.environ["AAP_MODULE_DATA_PG_CONTAINER_ID"]
    assert len(container_id) == 64 and all(
        char in "0123456789abcdef" for char in container_id
    )
    _verify_postgres(test_engine.url)
    return container_id, url.database


def _node_peer(*, container_id, database, requests, transaction):
    peer = ROOT / "apps/api/tests/fixtures/identity_provisioning_peer.mjs"
    payload = {
        "container_id": container_id,
        "database": database,
        "requests": requests,
        "transaction": transaction,
    }
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    result = subprocess.run(
        ["node", str(peer)],
        input=encoded,
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=45,
    )
    secret_values = [
        entry["request"]["password_hash"]
        for entry in requests
        if entry.get("operation") == "create"
        and isinstance(entry.get("request"), dict)
        and isinstance(entry["request"].get("password_hash"), str)
    ]
    if any(value in result.stdout + result.stderr for value in secret_values):
        pytest.fail("Identity provisioning peer echoed a password hash")
    assert result.returncode == 0, "Independent Identity provisioning peer failed"
    return json.loads(result.stdout)


async def _node_peer_async(**kwargs):
    return await asyncio.to_thread(_node_peer, **kwargs)


def _lookup(email: str) -> dict:
    return {
        "operation": "lookup",
        "request": {"schema": _PROTOCOL, "email": email},
    }


def _create(*, account_id, mode, email, name, password_hash, now, request_id) -> dict:
    return {
        "operation": "create",
        "request": {
            "schema": _PROTOCOL,
            "mode": mode,
            "account_id": str(account_id),
            "email": email,
            "name": name,
            "password_hash": password_hash,
            "now": now,
            "request_id": request_id,
        },
    }


def _profile(user: User, password: str) -> dict:
    fields = (
        "email",
        "name",
        "role",
        "status",
        "is_active",
        "group_name",
        "group_id",
        "credential_generation",
        "preferences",
        "disabled_kind",
        "disabled_at",
        "disabled_by",
        "disabled_reason",
        "last_login_at",
        "last_seen_at",
        "password_admin_reset_at",
        "deactivation_requested_at",
        "deactivation_reason",
        "deactivation_scheduled_at",
        "weekly_target_default",
        "avatar_ref",
    )
    result = {field: getattr(user, field) for field in fields}
    for field in ("group_id", "disabled_by"):
        if result[field] is not None:
            result[field] = str(result[field])
    result.update(
        email_verified=user.email_verified_at is not None,
        created_at_present=user.created_at is not None,
        id_is_uuid=isinstance(user.id, uuid.UUID),
        password_valid=verify_password(password, user.password_hash),
    )
    return result


def _bootstrap_audit(audit: AuditLog, user_id: uuid.UUID) -> dict:
    return {
        "actor_matches_account": audit.actor_id == user_id,
        "actor_email": audit.actor_email,
        "actor_role": audit.actor_role,
        "action": audit.action,
        "target_type": audit.target_type,
        "target_matches_account": audit.target_id == str(user_id),
        "detail_json": audit.detail_json,
        "method": audit.method,
        "path": audit.path,
        "status_code": audit.status_code,
        "ip": audit.ip,
        "request_id": audit.request_id,
    }


async def _count_audits(maker, *, actor_id=None, email=None) -> int:
    async with maker() as db:
        query = select(func.count(AuditLog.id))
        if actor_id is not None:
            query = query.where(AuditLog.actor_id == actor_id)
        if email is not None:
            query = query.where(AuditLog.actor_email == email)
        return await db.scalar(query)


async def _assert_missing_account(maker, *, email: str) -> bool:
    async with maker() as db:
        return await db.scalar(select(User.id).where(User.email == email)) is None


async def _remove_unlogged_import_admin(maker, *, user_id: uuid.UUID) -> None:
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        audit_count = await db.scalar(
            select(func.count(AuditLog.id)).where(AuditLog.actor_id == user_id)
        )
        if audit_count == 0:
            await db.execute(delete(User).where(User.id == user_id))


async def test_restricted_node_provisioning_client_matches_legacy_and_authenticates(
    test_engine, tmp_path, record_property
):
    legacy = _legacy_records()
    container_id, database = _provisioning_target(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)

    privilege_probe = await _node_peer_async(
        container_id=container_id,
        database=database,
        requests=[],
        transaction="probe_denied",
    )
    assert privilege_probe["current_user"] == "identity_provisioning_client"
    assert privilege_probe["session_user"] == "identity_provisioning_client"
    assert privilege_probe["raw_user_access"] is False
    assert privilege_probe["denied_capabilities"] == 11

    bootstrap_expected = legacy["bootstrap"]
    email = f"provisioning-peer-{uuid.uuid4().hex}@example.test"
    name = bootstrap_expected["profile"]["name"]
    password = "BootstrapBaseline123"
    password_hash = await asyncio.to_thread(hash_password, password)
    account_id = uuid.uuid4()
    now = datetime.now(UTC).isoformat()
    requests = [
        _lookup(email),
        _create(
            account_id=account_id,
            mode="bootstrap_admin",
            email=email,
            name=name,
            password_hash=password_hash,
            now=now,
            request_id="provisioning-baseline",
        ),
    ]
    assert await _assert_missing_account(maker, email=email)
    created = await _node_peer_async(
        container_id=container_id,
        database=database,
        requests=requests,
        transaction="commit",
    )
    assert (
        created["current_user"]
        == created["session_user"]
        == "identity_provisioning_client"
    )
    assert created["raw_user_access"] is False
    assert created["results"][0]["account_id"] is None
    assert created["results"][1]["account_id"] == str(account_id)
    assert bootstrap_expected["created"] is True

    async with maker() as db:
        user = await db.scalar(select(User).where(User.id == account_id))
        assert user is not None
        expected_profile = {**bootstrap_expected["profile"], "email": email}
        assert _profile(user, password) == expected_profile
        audit = await db.scalar(
            select(AuditLog).where(
                AuditLog.actor_id == account_id,
                AuditLog.action == "system.bootstrap_admin",
            )
        )
        assert audit is not None
        expected_audit = {
            **bootstrap_expected["audit"],
            "actor_email": email,
            "detail_json": {"email": email},
        }
        assert _bootstrap_audit(audit, account_id) == expected_audit
        assert (
            await db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.actor_id == account_id,
                    AuditLog.action == "system.bootstrap_admin",
                )
            )
            == bootstrap_expected["audit_count"]
        )

    replayed = await _node_peer_async(
        container_id=container_id,
        database=database,
        requests=[_lookup(email)],
        transaction="rollback",
    )
    assert replayed["results"][0]["account_id"] == str(account_id)
    async with maker() as db:
        user = await db.scalar(select(User).where(User.id == account_id))
        assert user is not None
        assert _profile(user, password) == expected_profile
    assert legacy["bootstrap"]["replayed_created"] is False
    assert legacy["bootstrap"]["profile_unchanged"] is True
    assert (
        await _count_audits(maker, actor_id=account_id)
        == bootstrap_expected["audit_count"]
    )

    second_expected = legacy["multiple_bootstrap"]
    second_email = f"provisioning-peer-second-{uuid.uuid4().hex}@example.test"
    second_id = uuid.uuid4()
    second_password = "SecondBaseline123"
    second_create = await _node_peer_async(
        container_id=container_id,
        database=database,
        requests=[
            _lookup(second_email),
            _create(
                account_id=second_id,
                mode="bootstrap_admin",
                email=second_email,
                name="Second bootstrap",
                password_hash=await asyncio.to_thread(hash_password, second_password),
                now=datetime.now(UTC).isoformat(),
                request_id=None,
            ),
        ],
        transaction="commit",
    )
    assert second_create["results"][0]["account_id"] is None
    assert second_create["results"][1]["account_id"] == str(second_id)
    async with maker() as db:
        super_admin_count = await db.scalar(
            select(func.count(User.id)).where(
                User.email.in_([email, second_email]), User.role == "super_admin"
            )
        )
    assert {
        "created": second_create["results"][1]["account_id"] == str(second_id),
        "super_admin_count": super_admin_count,
    } == second_expected

    async with _login_redis() as (counter_url, redis_owner):
        await redis_owner.delete("login_failed:127.0.0.1")
        secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        identity = api = None
        try:
            identity = await _start_container_identity(
                test_engine,
                tmp_path,
                secret,
                control,
                login_redis_url=counter_url,
                enable_accounts=True,
                enable_presence=False,
            )
            api = _start_api(
                tmp_path,
                secret,
                control,
                identity.origin,
                database_url=os.environ["AAP_MODULE_DATA_IDENTITY_AUTH_URL"],
                configuration={"TURNSTILE_ENABLED": "false"},
            )
            async with httpx.AsyncClient(
                base_url=api.origin, trust_env=False, timeout=30
            ) as client:
                login = await client.post(
                    "/api/v1/auth/login",
                    json={"email": second_email, "password": second_password},
                )
                assert login.status_code == 200
                token = login.json()["access_token"]
                me = await client.get(
                    "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
                )
                assert me.status_code == 200
                assert me.json()["id"] == str(second_id)
                assert me.json()["email"] == second_email
                assert me.json()["role"] == "super_admin"
        finally:
            if api is not None:
                api.stop()
            if identity is not None:
                identity.stop()
            await redis_owner.delete("login_failed:127.0.0.1")

    import_expected = legacy["staging_import"]
    assert legacy["production_missing"]["account_missing"] is True
    assert await _assert_missing_account(maker, email="admin")
    import_password = "123456"
    import_request = _create(
        account_id=uuid.uuid4(),
        mode="import_admin",
        email="admin",
        name="超级管理员",
        password_hash=await asyncio.to_thread(hash_password, import_password),
        now=None,
        request_id=None,
    )
    rolled_back_import = await _node_peer_async(
        container_id=container_id,
        database=database,
        requests=[_lookup("admin"), import_request],
        transaction="rollback",
    )
    assert rolled_back_import["results"][0]["account_id"] is None
    assert (
        rolled_back_import["results"][1]["account_id"]
        == import_request["request"]["account_id"]
    )
    rolled_back_import_id = uuid.UUID(import_request["request"]["account_id"])
    try:
        assert await _assert_missing_account(maker, email="admin")
        assert await _count_audits(maker, actor_id=rolled_back_import_id) == 0
    finally:
        await _remove_unlogged_import_admin(maker, user_id=rolled_back_import_id)

    import_id = uuid.uuid4()
    try:
        committed_import = await _node_peer_async(
            container_id=container_id,
            database=database,
            requests=[
                _lookup("admin"),
                _create(
                    account_id=import_id,
                    mode="import_admin",
                    email="admin",
                    name="超级管理员",
                    password_hash=await asyncio.to_thread(
                        hash_password, import_password
                    ),
                    now=None,
                    request_id=None,
                ),
            ],
            transaction="commit",
        )
        assert committed_import["results"][0]["account_id"] is None
        assert committed_import["results"][1]["account_id"] == str(import_id)
        async with maker() as db:
            admin = await db.scalar(select(User).where(User.id == import_id))
            assert admin is not None
            import_profile = _profile(admin, import_password)
            import_audit_count = await db.scalar(
                select(func.count(AuditLog.id)).where(AuditLog.actor_id == import_id)
            )
        replay_import = await _node_peer_async(
            container_id=container_id,
            database=database,
            requests=[_lookup("admin")],
            transaction="rollback",
        )
        assert {
            "profile": import_profile,
            "audit_count": import_audit_count,
        } == {
            "profile": import_expected["profile"],
            "audit_count": import_expected["audit_count"],
        }
        assert replay_import["results"][0]["account_id"] == str(import_id)

        async with maker.begin() as db:
            admin = await db.scalar(
                select(User).where(User.id == import_id).with_for_update()
            )
            assert admin is not None
            admin.role = "reviewer"
            admin.is_active = False
            admin.name = "  Existing historical admin 😀 "
            admin.password_hash = await asyncio.to_thread(
                hash_password, "ExistingPreset123"
            )
            await db.flush()
            existing_before = _profile(admin, "ExistingPreset123")
        existing_expected = legacy["existing_import"]
        existing = await _node_peer_async(
            container_id=container_id,
            database=database,
            requests=[_lookup("admin")],
            transaction="rollback",
        )
        assert existing["results"][0]["account_id"] == str(import_id)
        async with maker() as db:
            admin = await db.scalar(select(User).where(User.id == import_id))
            assert admin is not None
            existing_actual = {
                "reused_same_id": existing["results"][0]["account_id"]
                == str(import_id),
                "profile_unchanged": _profile(admin, "ExistingPreset123")
                == existing_before,
                "role": admin.role,
                "is_active": admin.is_active,
                "audit_count": await db.scalar(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.actor_id == import_id
                    )
                ),
            }
            assert existing_actual == existing_expected
    finally:
        await _remove_unlogged_import_admin(maker, user_id=import_id)

    rollback_expected = legacy["bootstrap_rollback"]
    rollback_email = f"provisioning-peer-rollback-{uuid.uuid4().hex}@example.test"
    rollback_create = await _node_peer_async(
        container_id=container_id,
        database=database,
        requests=[
            _lookup(rollback_email),
            _create(
                account_id=uuid.uuid4(),
                mode="bootstrap_admin",
                email=rollback_email,
                name="Rollback",
                password_hash=await asyncio.to_thread(
                    hash_password, "RollbackBaseline123"
                ),
                now=datetime.now(UTC).isoformat(),
                request_id=None,
            ),
        ],
        transaction="rollback",
    )
    assert rollback_create["results"][0]["account_id"] is None
    assert rollback_create["results"][1]["account_id"] is not None
    missing_after_rollback = await _assert_missing_account(maker, email=rollback_email)
    audit_missing_after_rollback = await _count_audits(maker, email=rollback_email) == 0
    assert {
        "account_missing": missing_after_rollback,
        "audit_missing": audit_missing_after_rollback,
    } == rollback_expected

    duplicate_expected = legacy["duplicate_create"]
    duplicate_email = f"provisioning-peer-race-{uuid.uuid4().hex}@example.test"
    assert await _assert_missing_account(maker, email=duplicate_email)
    lookups = await asyncio.gather(
        *(
            _node_peer_async(
                container_id=container_id,
                database=database,
                requests=[_lookup(duplicate_email)],
                transaction="rollback",
            )
            for _ in range(2)
        )
    )
    assert all(item["results"][0]["account_id"] is None for item in lookups)
    competing_requests = [
        _create(
            account_id=uuid.uuid4(),
            mode="bootstrap_admin",
            email=duplicate_email,
            name="Concurrent bootstrap",
            password_hash=await asyncio.to_thread(
                hash_password, "ConcurrentBaseline123"
            ),
            now=datetime.now(UTC).isoformat(),
            request_id=None,
        )
        for _ in range(2)
    ]
    contenders = await asyncio.gather(
        *(
            _node_peer_async(
                container_id=container_id,
                database=database,
                requests=[request],
                transaction="create_or_duplicate",
            )
            for request in competing_requests
        )
    )
    assert sorted(item["outcome"] for item in contenders) == [
        "created",
        "duplicate_aborted",
    ]
    duplicate_result = next(
        item for item in contenders if item["outcome"] == "duplicate_aborted"
    )
    assert duplicate_result["sqlstates"] == ["23505", "25P02"]
    async with maker() as db:
        account_count = await db.scalar(
            select(func.count(User.id)).where(User.email == duplicate_email)
        )
    audit_count = await _count_audits(maker, email=duplicate_email)
    actual_duplicate = {
        "created_count": sum(item["outcome"] == "created" for item in contenders),
        "sqlstate": duplicate_result["sqlstates"][0],
        "requires_rollback": "25P02" in duplicate_result["sqlstates"],
        "account_count": account_count,
        "audit_count": audit_count,
    }
    assert actual_duplicate == duplicate_expected

    record_property(
        "identity_provisioning_peer",
        "Node provisioning LOGIN, same-transaction lookup/create, legacy parity, atomic Audit/import rollback, duplicate race and HTTP login",
    )
