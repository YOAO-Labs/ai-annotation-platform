from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aap_identity.authenticator import Authenticator, VerifiedCredential
from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.directory_contract import (
    DirectoryFailure,
    decode_directory_request,
    decode_native_directory_reply,
    native_directory_command,
)
from aap_identity.directory_manager import (
    DirectoryManager,
    directory_request_fingerprint,
)
from aap_identity.directory_postgres import DirectoryPostgresData
from aap_identity.postgres import PostgresData
from aap_identity.security import create_access_token
from app.db.native_schema import NATIVE_MANAGED_TABLES
from tests.test_identity_auth_native import _cleanup, _seed


_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_CANONICAL_SCHEMA = (
    _ROOT / "apps/identity/src/aap_identity/native_directory_protocol.schema.json"
)
_SUPPORT = _SQL_DIR / "0243_identity_directory_support.sql"
_ARTIFACT = _SQL_DIR / "0243_identity_directory.sql"
_SCHEMA = _SQL_DIR / "0243_identity_directory.schema.json"
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0243_identity_directory.py"
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_ROLE_GRANTS = _ROOT / "infra/docker/identity-directory-grants.sql"
_LAN_GRANTS = _ROOT / "infra/docker/lan-production-grants.sql"


def _directory_dsn() -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity directory client acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_DIRECTORY_URL")
    if not value:
        pytest.skip("Identity directory client DSN is not configured")
    return (
        make_url(value)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


def _auth_dsn(test_engine) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity directory role acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_AUTH_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _audit_context() -> dict[str, str]:
    return {
        "method": "GET",
        "path": "/api/v1/users/export",
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }


def _principal(actor_id: uuid.UUID, email: str) -> Principal:
    return Principal(
        actor_id=actor_id,
        name="Directory fixture",
        email=email,
        role="super_admin",
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=0,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


async def _stored_email(test_engine, actor_id: uuid.UUID) -> str:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        return await session.scalar(
            text("SELECT email FROM public.users WHERE id=:actor_id"),
            {"actor_id": actor_id},
        )


class _Authenticator:
    def __init__(self, principal: Principal):
        self.verified = VerifiedCredential(principal=principal, key_hash=None)

    async def authenticate_bound(self, token: str) -> VerifiedCredential:
        assert token == "directory-test-token"
        return self.verified


def _request(operation: str, *, operation_id: uuid.UUID | None = None) -> dict:
    return {
        "schema": "aap-identity-directory/v1",
        "operation": operation,
        "operation_id": str(operation_id or uuid.uuid4()),
        "token": "directory-test-token",
        "audit_context": _audit_context(),
        "payload": {
            "status": "all",
            "project_id": None,
            "group_id": None,
            "role": None,
            "search": None,
            "format": "json",
        },
    }


def test_frozen_identity_directory_native_artifacts():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    canonical_schema = json.loads(_CANONICAL_SCHEMA.read_text(encoding="utf-8"))
    support = _SUPPORT.read_text(encoding="utf-8")
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    role_grants = _ROLE_GRANTS.read_text(encoding="utf-8")
    lan_grants = _LAN_GRANTS.read_text(encoding="utf-8")

    assert "identity_directory_export_receipts" in NATIVE_MANAGED_TABLES
    assert schema["$id"] == "aap-identity-directory-native/v1"
    assert schema == canonical_schema
    assert set(schema["$defs"])
    assert set(schema["$defs"]["DirectoryUserRecord"]["properties"]) == {
        "id",
        "email",
        "name",
        "role",
        "group_name",
        "group_id",
        "status",
        "is_active",
        "disabled_kind",
        "disabled_at",
        "disabled_by",
        "disabled_reason",
        "email_verified_at",
        "last_login_at",
        "last_seen_at",
        "password_admin_reset_at",
        "deactivation_requested_at",
        "deactivation_scheduled_at",
        "preferences",
        "avatar_ref",
        "created_at",
    }
    assert migration.index('"0243_identity_directory_support.sql"') < migration.index(
        '"0243_identity_directory.sql"'
    )
    assert "operation text NOT NULL CHECK (operation = 'export_users')" in support
    assert "FOR UPDATE" not in artifact
    assert "identity_directory_project_refs(uuid[])" in support
    assert "record_identity_directory_export(jsonb,jsonb,jsonb)" in "".join(
        module_grants.split()
    )
    assert "identity_directory_project_refs(uuid[])" in role_grants
    assert "aap_identity_directory_v1.execute_directory_command(jsonb)" in lan_grants
    assert "password_hash" not in artifact
    assert "preferences" not in support


@pytest.mark.asyncio
async def test_directory_reads_validate_filters_and_projection(test_engine):
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    commands = [
        (
            "list_users",
            {
                "role": None,
                "status": "all",
                "project_id": None,
            },
        ),
        (
            "query_users",
            {
                "page": 1,
                "page_size": 10,
                "status": "all",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": None,
            },
        ),
        (
            "user_stats",
            {
                "status": "all",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": None,
            },
        ),
    ]

    def command(operation, payload):
        return {
            "schema": "aap-identity-directory-native/v1",
            "operation": operation,
            "operation_id": str(uuid.uuid4()),
            "actor": {
                "actor_id": str(fixture["actor_id"]),
                "name": "stale name",
                "email": "stale@example.test",
                "role": "super_admin",
                "is_active": True,
                "credential_type": "jwt",
                "credential_id": str(uuid.uuid4()),
                "generation": 0,
                "scopes": None,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            },
            "expected_key_hash": None,
            "request_fingerprint": "a" * 64,
            "derivation_id": "b" * 64,
            "audit_context": _audit_context(),
            "payload": payload,
        }

    try:
        async with maker.begin() as session:
            for operation, payload in commands:
                raw = await session.scalar(
                    text(
                        "SELECT aap_identity_directory_v1.execute_directory_command("
                        "CAST(:request AS jsonb))"
                    ),
                    {"request": json.dumps(command(operation, payload))},
                )
                assert raw["ok"] is True
                assert raw["operation"] == operation
                assert raw["operation_id"]
                assert raw["principal"]["role"] == "super_admin"
                assert raw["replayed"] is False
                if operation == "list_users":
                    user = next(
                        item
                        for item in raw["data"]["users"]
                        if item["id"] == str(fixture["actor_id"])
                    )
                    assert set(user) == set(
                        schema["$defs"]["DirectoryUserRecord"]["properties"]
                    )
                    assert "password_hash" not in user
                elif operation == "query_users":
                    assert raw["data"]["page"] == 1
                    assert raw["data"]["page_size"] == 10
                    assert raw["data"]["pages"] >= 1
                    assert all(
                        item["is_managed"] and item["is_lifecycle_managed"]
                        for item in raw["data"]["items"]
                    )
                else:
                    assert raw["data"]["total"] >= 1
                    assert raw["data"]["online"] <= raw["data"]["total"]
                    assert raw["data"]["weekly_active"] <= raw["data"]["total"]

            invalid_role = command(
                "list_users",
                {"role": "annotator", "status": "all", "project_id": None},
            )
            rejected = await session.scalar(
                text(
                    "SELECT aap_identity_directory_v1.execute_directory_command("
                    "CAST(:request AS jsonb))"
                ),
                {"request": json.dumps(invalid_role)},
            )
            assert rejected == {
                "schema": "aap-identity-directory-native/v1",
                "ok": False,
                "error": {
                    "code": "invalid_platform_role",
                    "status_code": 400,
                    "detail": "非法平台角色: annotator",
                },
            }
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_native_command_rechecks_manager_role_after_jwt_admission(test_engine):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "directory-native-stale-manager-test-secret"
    token = create_access_token(
        str(fixture["actor_id"]),
        "super_admin",
        secret=secret,
        expire_minutes=15,
    )
    authenticator = Authenticator(PostgresData(_auth_dsn(test_engine)), secret)
    directory_data = DirectoryPostgresData(_directory_dsn())
    directory_manager = DirectoryManager(authenticator, directory_data, secret)
    request = decode_directory_request(
        {
            "schema": "aap-identity-directory/v1",
            "operation": "user_stats",
            "operation_id": str(uuid.uuid4()),
            "token": token,
            "audit_context": {
                "method": "GET",
                "path": "/api/v1/users/stats",
                "ip": "127.0.0.1",
                "request_id": str(uuid.uuid4()),
            },
            "payload": {
                "status": "all",
                "project_id": None,
                "group_id": None,
                "role": None,
                "search": None,
            },
        }
    )
    verified = await authenticator.authenticate_bound(token)
    assert verified.principal.role == "super_admin"
    generation = verified.principal.generation

    try:
        async with maker.begin() as session:
            stored_generation = await session.scalar(
                text("SELECT credential_generation FROM public.users WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )
            assert stored_generation == generation
            await session.execute(
                text("UPDATE public.users SET role='employee' WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )

        native = native_directory_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            request_fingerprint=directory_request_fingerprint(secret, request),
            derivation_id=directory_manager.derivation_id,
        )
        command_payload = native.model_dump(mode="json", by_alias=True)
        with pytest.raises(DirectoryFailure) as raised:
            async with directory_data.transaction() as transaction:
                reply = await transaction.execute_directory_command(command_payload)
                decode_native_directory_reply(native, reply)
        assert raised.value.code == "manager_required"
        assert raised.value.status_code == 403
        assert raised.value.detail == "需要角色权限: super_admin或project_admin"

        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT credential_generation FROM public.users WHERE id=:id"),
                    {"id": fixture["actor_id"]},
                )
                == generation
            )

        current = await authenticator.authenticate_bound(token)
        assert current.principal.role == "employee"
        assert current.principal.generation == generation
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_manager_rejects_corrupt_export_before_narrow_transaction_commits(
    test_engine,
):
    dsn = _directory_dsn()
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    principal = _principal(
        fixture["actor_id"], await _stored_email(test_engine, fixture["actor_id"])
    )
    request = decode_directory_request(
        _request("export_users", operation_id=operation_id)
    )

    class _CorruptReplyTransaction:
        def __init__(self, transaction):
            self.transaction = transaction

        async def execute_directory_command(self, payload):
            reply = await self.transaction.execute_directory_command(payload)
            reply["data"]["exported_by"] = "corrupt@example.test"
            return reply

    class _CorruptingData:
        def __init__(self):
            self.inner = DirectoryPostgresData(dsn)

        @asynccontextmanager
        async def transaction(self):
            async with self.inner.transaction() as transaction:
                yield _CorruptReplyTransaction(transaction)

    manager = DirectoryManager(
        _Authenticator(principal), _CorruptingData(), "test-secret"
    )
    try:
        with pytest.raises(IdentityUnavailable):
            await manager.execute(request)
        async with maker() as session:
            receipt_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_directory_export_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor_id "
                    "AND action='user.export' AND request_id=:request_id"
                ),
                {
                    "actor_id": fixture["actor_id"],
                    "request_id": request.audit_context.request_id,
                },
            )
            assert receipt_count == 0
            assert audit_count == 0
    finally:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM public.identity_directory_export_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_export_replay_returns_frozen_result_without_second_audit(test_engine):
    dsn = _directory_dsn()
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    original_email = await _stored_email(test_engine, fixture["actor_id"])
    principal = _principal(fixture["actor_id"], original_email)
    request = decode_directory_request(
        _request("export_users", operation_id=operation_id)
    )
    manager = DirectoryManager(
        _Authenticator(principal), DirectoryPostgresData(dsn), "test-secret"
    )
    try:
        first = await manager.execute(request)
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET email=:email WHERE id=:actor_id"),
                {
                    "email": f"renamed-{fixture['actor_id'].hex}@example.test",
                    "actor_id": fixture["actor_id"],
                },
            )
        second = await manager.execute(request)
        assert second.data == first.data
        async with maker() as session:
            receipt = await session.execute(
                text(
                    "SELECT operation, actor_id, result, audit_id "
                    "FROM public.identity_directory_export_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            row = receipt.mappings().one()
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor_id "
                    "AND action='user.export' AND request_id=:request_id"
                ),
                {
                    "actor_id": fixture["actor_id"],
                    "request_id": request.audit_context.request_id,
                },
            )
            assert row["operation"] == "export_users"
            assert row["actor_id"] == fixture["actor_id"]
            assert row["result"]["exported_by"] == original_email
            assert row["audit_id"] is not None
            assert audit_count == 1
    finally:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM public.identity_directory_export_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
        await _cleanup(test_engine, fixture)
