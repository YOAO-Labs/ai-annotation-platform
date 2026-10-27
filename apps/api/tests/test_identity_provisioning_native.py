"""Native and function-only LOGIN checks for Identity provisioning."""

from __future__ import annotations

import asyncpg
import bcrypt
import json
import os
from datetime import UTC, datetime
from pathlib import Path
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aap_identity.provisioning import AccountProvisioningManager
from aap_identity.provisioning_contract import (
    PROVISIONING_PROTOCOL,
    ProvisioningCreateRequest,
    ProvisioningLookupRequest,
)
from app.config import settings
from app.db.models.user import User
from tests.test_asset_storage_cleanup_runtime import _verify_postgres

_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_SCHEMA = _SQL_DIR / "0252_identity_provisioning.schema.json"
_SUPPORT = _SQL_DIR / "0252_identity_provisioning_support.sql"
_ARTIFACT = _SQL_DIR / "0252_identity_provisioning.sql"
_MIGRATION = (
    Path(__file__).parents[1] / "alembic/versions/0252_identity_provisioning.py"
)
_CANONICAL_SCHEMA = (
    _ROOT / "apps/identity/src/aap_identity/provisioning_protocol.schema.json"
)
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_ROLE_GRANTS = _ROOT / "infra/docker/identity-provisioning-grants.sql"
_LAN_GRANTS = _ROOT / "infra/docker/lan-production-grants.sql"
_VERIFIER = _ROOT / "scripts/verify_module_data_boundary.py"


def _provisioning_dsn(test_engine) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity provisioning acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_PROVISIONING_URL")
    if not value:
        pytest.skip("Identity provisioning client DSN is not configured")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith("_test")
    assert parsed.username == "identity_provisioning_client"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    _verify_postgres(test_engine.url)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _owner_dsn(test_engine) -> str:
    parsed = make_url(settings.effective_migration_database_url)
    assert parsed.database and parsed.database.endswith("_test")
    assert parsed.username == "owner"
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    _verify_postgres(test_engine.url)
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _require_owned_acceptance() -> None:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity provisioning acceptance requires the owned verifier")


def _json_reply(raw):
    return json.loads(raw) if isinstance(raw, (str, bytes)) else raw


class _SessionProvisioningData:
    """Exercise the native functions through the caller's current transaction."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def lookup_account(self, request: ProvisioningLookupRequest) -> dict:
        result = await self.session.execute(
            text("SELECT aap_identity_v1.read_provisioning_account(:email)"),
            {"email": request.email},
        )
        return _json_reply(result.scalar_one())

    async def create_account(self, request: ProvisioningCreateRequest) -> dict:
        payload = request.model_dump(mode="json", by_alias=True)
        result = await self.session.execute(
            text(
                "SELECT aap_identity_v1.create_provisioning_account("
                "CAST(:payload AS jsonb))"
            ),
            {"payload": json.dumps(payload, ensure_ascii=False)},
        )
        return _json_reply(result.scalar_one())


def _create_payload(
    *,
    account_id: uuid.UUID,
    email: str,
    request_id: str | None = None,
    name: str = "Provisioning native test",
    password_hash: str = "test-only-provisioning-hash",
) -> dict:
    return {
        "schema": PROVISIONING_PROTOCOL,
        "mode": "bootstrap_admin",
        "account_id": str(account_id),
        "email": email,
        "name": name,
        "password_hash": password_hash,
        "now": datetime.now(UTC).isoformat(),
        "request_id": request_id,
    }


async def _cleanup(test_engine, account_ids, emails, request_ids) -> None:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        # Immutable Audit and its actor stay until the verifier destroys its DB.
        await session.execute(
            text(
                "DELETE FROM public.users AS account "
                "WHERE (id=ANY(CAST(:ids AS uuid[])) OR email=ANY(CAST(:emails AS text[]))) "
                "AND NOT EXISTS (SELECT 1 FROM public.audit_logs AS audit "
                "WHERE audit.actor_id=account.id)"
            ),
            {"ids": list(account_ids), "emails": list(emails)},
        )


class _AbortProvisioning(Exception):
    pass


def test_provisioning_schema_sql_and_function_allowlist_are_frozen():
    canonical = json.loads(_CANONICAL_SCHEMA.read_text(encoding="utf-8"))
    snapshot = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    support = _SUPPORT.read_text(encoding="utf-8")
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    role_grants = _ROLE_GRANTS.read_text(encoding="utf-8")
    lan_grants = _LAN_GRANTS.read_text(encoding="utf-8")
    verifier = _VERIFIER.read_text(encoding="utf-8")

    assert snapshot == {
        **canonical,
        "x-postgresql-functions": [
            "aap_audit_v1.record_identity_bootstrap_admin(uuid, text, text)",
            "aap_identity_v1.create_provisioning_account(jsonb)",
            "aap_identity_v1.read_provisioning_account(text)",
        ],
    }
    assert _SCHEMA.read_bytes() != _CANONICAL_SCHEMA.read_bytes()
    assert canonical.get("x-postgresql-functions") is None
    assert snapshot["$id"] == "aap-identity-provisioning/v1"

    assert migration.index(
        '"0252_identity_provisioning_support.sql"'
    ) < migration.index('"0252_identity_provisioning.sql"')
    assert "SECURITY DEFINER" in support + artifact
    assert "SET search_path = pg_catalog, pg_temp" in support + artifact
    assert "ON CONFLICT" not in artifact
    assert "EXCEPTION WHEN unique_violation" not in artifact
    assert "INSERT INTO public.users" in artifact
    assert "INSERT INTO public.audit_logs" in support
    assert "INSERT INTO public.audit_logs" not in artifact
    assert "preferences" in module_grants
    assert "identity_provisioning_client" in role_grants
    assert "aap_identity_v1.create_provisioning_account(jsonb)" in role_grants
    assert "aap_identity_v1.read_provisioning_account(text)" in role_grants
    assert "aap_identity_v1.create_provisioning_account(jsonb)" in lan_grants
    assert '"PROVISIONING": "identity_provisioning_client"' in verifier
    assert '"0252_identity_provisioning.sql"' in verifier
    assert '"0252_identity_provisioning.schema.json"' in verifier


@pytest.mark.asyncio
async def test_bootstrap_defaults_audit_atomicity_and_existing_email_noop(
    test_engine, monkeypatch
):
    _require_owned_acceptance()
    account_id = uuid.uuid4()
    email = f"bootstrap-{account_id.hex}@example.test"
    request_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with maker.begin() as session:
            manager = AccountProvisioningManager(_SessionProvisioningData(session))
            created_id = await manager.create_bootstrap_super_admin(
                email=email,
                name="Bootstrapped owner",
                password="operator supplied password",
                now=now,
                request_id=request_id,
            )
            assert isinstance(created_id, uuid.UUID)
            account_id = created_id

        async with maker() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT email,name,role,status,is_active,group_name,group_id,"
                            "email_verified_at,credential_generation,preferences::text,"
                            "password_hash,disabled_kind,disabled_at,disabled_by,"
                            "disabled_reason,last_login_at,last_seen_at,"
                            "password_admin_reset_at,deactivation_requested_at,"
                            "deactivation_reason,deactivation_scheduled_at,created_at "
                            "FROM public.users WHERE id=:id"
                        ),
                        {"id": account_id},
                    )
                )
                .mappings()
                .one()
            )
            assert row["email"] == email
            assert row["name"] == "Bootstrapped owner"
            assert row["role"] == "super_admin"
            assert row["status"] == "online"
            assert row["is_active"] is True
            assert row["group_name"] is None and row["group_id"] is None
            assert row["email_verified_at"] == now
            assert row["credential_generation"] == 0
            assert row["preferences"] == "{}"
            assert bcrypt.checkpw(
                b"operator supplied password", row["password_hash"].encode()
            )
            assert all(
                row[field] is None
                for field in (
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
                )
            )
            assert row["created_at"] is not None
            audit = (
                (
                    await session.execute(
                        text(
                            "SELECT action,actor_id,actor_email,actor_role,target_type,"
                            "target_id,method,path,status_code,ip,request_id,"
                            "detail_json::text AS detail_json "
                            "FROM public.audit_logs WHERE action='system.bootstrap_admin' "
                            "AND actor_id=:id AND request_id=:request_id"
                        ),
                        {"id": account_id, "request_id": request_id},
                    )
                )
                .mappings()
                .one()
            )
            assert audit["action"] == "system.bootstrap_admin"
            assert audit["actor_id"] == account_id
            assert audit["actor_email"] == email
            assert audit["actor_role"] == "super_admin"
            assert audit["target_type"] == "user"
            assert audit["target_id"] == str(account_id)
            assert audit["method"] is None and audit["path"] is None
            assert audit["status_code"] is None and audit["ip"] is None
            assert audit["request_id"] == request_id
            assert json.loads(audit["detail_json"]) == {"email": email}

        import aap_identity.provisioning as provisioning_module

        def fail_if_hashed(_password):
            pytest.fail("an existing bootstrap email must not be hashed again")

        monkeypatch.setattr(provisioning_module, "hash_password", fail_if_hashed)
        async with maker.begin() as session:
            manager = AccountProvisioningManager(_SessionProvisioningData(session))
            assert (
                await manager.create_bootstrap_super_admin(
                    email=email,
                    name="Ignored replacement",
                    password="must not be hashed",
                    now=datetime.now(UTC),
                    request_id=str(uuid.uuid4()),
                )
            ) is None
        async with maker() as session:
            unchanged = (
                (
                    await session.execute(
                        text(
                            "SELECT name,password_hash,email_verified_at "
                            "FROM public.users WHERE id=:id"
                        ),
                        {"id": account_id},
                    )
                )
                .mappings()
                .one()
            )
            assert unchanged["name"] == "Bootstrapped owner"
            assert unchanged["email_verified_at"] == now
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE action='system.bootstrap_admin' AND actor_id=:id"
                    ),
                    {"id": account_id},
                )
                == 1
            )

        rollback_id = uuid.uuid4()
        rollback_email = f"bootstrap-rollback-{rollback_id.hex}@example.test"
        rollback_request_id = str(uuid.uuid4())
        monkeypatch.undo()
        with pytest.raises(_AbortProvisioning):
            async with maker.begin() as session:
                manager = AccountProvisioningManager(_SessionProvisioningData(session))
                rollback_id = await manager.create_bootstrap_super_admin(
                    email=rollback_email,
                    name="Rollback owner",
                    password="rollback password",
                    now=datetime.now(UTC),
                    request_id=rollback_request_id,
                )
                raise _AbortProvisioning
        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM public.users WHERE id=:id"),
                    {"id": rollback_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE action='system.bootstrap_admin' AND request_id=:request_id"
                    ),
                    {"request_id": rollback_request_id},
                )
                == 0
            )
    finally:
        await _cleanup(
            test_engine,
            [account_id, rollback_id] if "rollback_id" in locals() else [account_id],
            [email, rollback_email] if "rollback_email" in locals() else [email],
            [request_id, rollback_request_id]
            if "rollback_request_id" in locals()
            else [request_id],
        )


@pytest.mark.asyncio
async def test_import_environment_guard_reuse_and_defaults(test_engine, monkeypatch):
    _require_owned_acceptance()
    import aap_identity.provisioning as provisioning_module

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    account_id = uuid.uuid4()
    async with maker() as session:
        if (
            await session.scalar(
                text("SELECT id FROM public.users WHERE email='admin' LIMIT 1")
            )
            is not None
        ):
            pytest.skip("the owned acceptance database already contains an admin row")
    try:
        async with maker.begin() as session:
            manager = AccountProvisioningManager(_SessionProvisioningData(session))
            with pytest.raises(RuntimeError) as error:
                await manager.ensure_import_admin(environment="production")
            assert str(error.value) == (
                "生产环境下未找到 admin 用户,拒绝自动创建弱口令 admin/123456。"
                "请先手动创建管理员账号再运行本导入脚本。"
            )
        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM public.users WHERE email='admin'")
                )
                == 0
            )

        async with maker.begin() as session:
            session.add(
                User(
                    id=account_id,
                    email="admin",
                    name="Historical row",
                    password_hash="unchanged-historical-hash",
                    role="viewer",
                    is_active=False,
                    status="offline",
                    disabled_kind="suspended",
                )
            )

        def fail_if_hashed(_password):
            pytest.fail("an existing import admin must be reused without hashing")

        monkeypatch.setattr(provisioning_module, "hash_password", fail_if_hashed)
        async with maker.begin() as session:
            manager = AccountProvisioningManager(_SessionProvisioningData(session))
            assert (
                await manager.ensure_import_admin(environment="production")
                == account_id
            )

        async with maker() as session:
            unchanged = (
                (
                    await session.execute(
                        text(
                            "SELECT name,password_hash,role,is_active "
                            "FROM public.users WHERE id=:id"
                        ),
                        {"id": account_id},
                    )
                )
                .mappings()
                .one()
            )
            assert unchanged == {
                "name": "Historical row",
                "password_hash": "unchanged-historical-hash",
                "role": "viewer",
                "is_active": False,
            }

        monkeypatch.undo()
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.users WHERE id=:id OR email='admin'"),
                {"id": account_id},
            )
        async with maker.begin() as session:
            manager = AccountProvisioningManager(_SessionProvisioningData(session))
            account_id = await manager.ensure_import_admin(environment="staging")

        async with maker() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT email,name,role,status,is_active,email_verified_at,"
                            "credential_generation,preferences::text,password_hash "
                            "FROM public.users WHERE id=:id"
                        ),
                        {"id": account_id},
                    )
                )
                .mappings()
                .one()
            )
            assert row["email"] == "admin"
            assert row["name"] == "超级管理员"
            assert row["role"] == "super_admin"
            assert row["status"] == "offline"
            assert row["is_active"] is True
            assert row["email_verified_at"] is None
            assert row["credential_generation"] == 0
            assert row["preferences"] == "{}"
            assert bcrypt.checkpw(b"123456", row["password_hash"].encode())
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE action='system.bootstrap_admin' AND actor_id=:id"
                    ),
                    {"id": account_id},
                )
                == 0
            )
    finally:
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.users WHERE id=:id"), {"id": account_id}
            )


@pytest.mark.asyncio
async def test_duplicate_email_keeps_raw_unique_and_aborted_transaction_errors(
    test_engine,
):
    _require_owned_acceptance()
    connection = await asyncpg.connect(_owner_dsn(test_engine))
    account_id = uuid.uuid4()
    duplicate_id = uuid.uuid4()
    email = f"provisioning-duplicate-{account_id.hex}@example.test"
    request_id = str(uuid.uuid4())
    transaction = connection.transaction()
    await transaction.start()
    try:
        first = await connection.fetchval(
            "SELECT aap_identity_v1.create_provisioning_account($1::jsonb)",
            json.dumps(
                _create_payload(
                    account_id=account_id, email=email, request_id=request_id
                )
            ),
        )
        assert json.loads(first)["account_id"] == str(account_id)

        with pytest.raises(asyncpg.UniqueViolationError) as duplicate:
            await connection.fetchval(
                "SELECT aap_identity_v1.create_provisioning_account($1::jsonb)",
                json.dumps(
                    _create_payload(
                        account_id=duplicate_id,
                        email=email,
                        request_id=str(uuid.uuid4()),
                    )
                ),
            )
        assert duplicate.value.sqlstate == "23505"
        with pytest.raises(asyncpg.InFailedSQLTransactionError) as aborted:
            await connection.fetchval("SELECT 1")
        assert aborted.value.sqlstate == "25P02"
    finally:
        await transaction.rollback()
        try:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.users WHERE id=$1 OR email=$2",
                    account_id,
                    email,
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE action='system.bootstrap_admin' AND request_id=$1",
                    request_id,
                )
                == 0
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_provisioning_login_has_only_two_functions_and_no_raw_access(test_engine):
    _require_owned_acceptance()
    connection = await asyncpg.connect(_provisioning_dsn(test_engine))
    email = f"provisioning-client-{uuid.uuid4().hex}@example.test"
    account_id = uuid.uuid4()
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "identity_provisioning_client"
        )
        rows = await connection.fetch(
            """
            SELECT namespace.nspname || '.' || function.proname || '(' ||
                   pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = function.pronamespace
            JOIN pg_catalog.pg_type AS return_type
              ON return_type.oid = function.prorettype
            WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND function.prokind IN ('f','p','a','w')
              AND NOT (
                  return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
                  AND return_type.typname IN ('trigger','event_trigger')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM pg_catalog.pg_depend AS dependency
                  WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
                    AND dependency.objid = function.oid
                    AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
                    AND dependency.deptype = 'e'
              )
              AND (
                  pg_catalog.left(namespace.nspname,4)='aap_'
                  OR function.prosecdef
                  OR EXISTS (
                      SELECT 1 FROM pg_catalog.aclexplode(COALESCE(
                          function.proacl,
                          pg_catalog.acldefault('f', function.proowner)
                      )) AS acl
                      WHERE acl.privilege_type='EXECUTE' AND acl.grantee<>0
                        AND pg_catalog.pg_has_role(
                            current_user, acl.grantee, 'USAGE'
                        )
                  )
              )
              AND pg_catalog.has_function_privilege(
                  current_user, function.oid, 'EXECUTE'
              )
            ORDER BY namespace.nspname, function.proname,
                     pg_catalog.oidvectortypes(function.proargtypes)
            """
        )
        assert [row["signature"] for row in rows] == [
            "aap_identity_v1.create_provisioning_account(jsonb)",
            "aap_identity_v1.create_seed_persona_account(jsonb)",
            "aap_identity_v1.read_provisioning_account(text)",
        ]
        assert await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_v1', 'USAGE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_v1', 'CREATE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, 'public', 'CREATE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_database_privilege(current_user, "
            "current_database(), 'CREATE,TEMP')"
        )

        extra_field = _create_payload(account_id=account_id, email=email)
        extra_field["unexpected"] = True
        invalid_time = _create_payload(account_id=account_id, email=email)
        invalid_time["now"] = "2026-10-21T00:00:00+99:00"
        malformed_payloads = (
            ("NULL::jsonb", None),
            ("'null'::jsonb", None),
            ("'[]'::jsonb", None),
            ("'{}'::jsonb", None),
            ("to_jsonb('scalar'::text)", None),
            ("$1::jsonb", extra_field),
            ("$1::jsonb", invalid_time),
        )
        for expression, payload in malformed_payloads:
            parameters = [] if payload is None else [json.dumps(payload)]
            with pytest.raises(asyncpg.PostgresError) as invalid:
                await connection.fetchval(
                    f"SELECT aap_identity_v1.create_provisioning_account({expression})",
                    *parameters,
                )
            assert invalid.value.sqlstate == "22023"

        transaction = connection.transaction()
        try:
            with pytest.raises(_AbortProvisioning):
                async with transaction:
                    created = await connection.fetchval(
                        "SELECT aap_identity_v1.create_provisioning_account($1::jsonb)",
                        json.dumps(_create_payload(account_id=account_id, email=email)),
                    )
                    assert set(json.loads(created)) == {
                        "schema",
                        "email",
                        "account_id",
                    }
                    assert json.loads(created)["account_id"] == str(account_id)
                    raise _AbortProvisioning
        except _AbortProvisioning:
            pass
        lookup = await connection.fetchval(
            "SELECT aap_identity_v1.read_provisioning_account($1)", email
        )
        assert json.loads(lookup) == {
            "schema": "aap-identity-provisioning/v1",
            "email": email,
            "account_id": None,
        }

        forbidden = (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.audit_logs LIMIT 1",
            "SELECT aap_identity_v1.read_actor_context(NULL::uuid,NULL::text)",
            "CREATE TEMP TABLE provisioning_forbidden(id integer)",
            "SET ROLE identity_code",
        )
        for statement in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()
