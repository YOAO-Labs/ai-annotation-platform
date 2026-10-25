from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import bcrypt
import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aap_identity
from aap_identity.admin_password_contract import (
    AdminPasswordFailure,
    decode_admin_password_request,
)
from aap_identity.admin_password_manager import AdminPasswordManager
from aap_identity.admin_password_postgres import AdminPasswordPostgresData
from aap_identity.authenticator import Authenticator, VerifiedCredential
from aap_identity.contract import IdentityUnavailable, Principal
from aap_identity.postgres import PostgresData
from aap_identity.security import create_access_token
from app.db.models.api_key import ApiKey
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from app.db.native_schema import NATIVE_MANAGED_TABLES
from tests.test_identity_auth_native import _cleanup, _seed


_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_CANONICAL_SCHEMA = (
    _ROOT / "apps/identity/src/aap_identity/native_admin_password_protocol.schema.json"
)
if not _CANONICAL_SCHEMA.is_file():
    _CANONICAL_SCHEMA = (
        Path(aap_identity.__file__).resolve().parent
        / "native_admin_password_protocol.schema.json"
    )
_SUPPORT = _SQL_DIR / "0244_identity_admin_passwords_support.sql"
_ARTIFACT = _SQL_DIR / "0244_identity_admin_passwords.sql"
_SCHEMA = _SQL_DIR / "0244_identity_admin_passwords.schema.json"
_MIGRATION = (
    Path(__file__).parents[1] / "alembic/versions/0244_identity_admin_passwords.py"
)
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_ROLE_GRANTS = _ROOT / "infra/docker/identity-admin-passwords-grants.sql"
_LAN_GRANTS = _ROOT / "infra/docker/lan-production-grants.sql"
_SECRET = "admin-password-native-test-secret"


def _admin_password_dsn() -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip(
            "Identity administrator-password acceptance requires the owned verifier"
        )
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_ADMIN_PASSWORD_URL")
    if not value:
        pytest.skip("Identity administrator-password client DSN is not configured")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_admin_password_client"
    assert os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _auth_dsn(test_engine) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip(
            "Identity administrator-password JWT checks require the owned verifier"
        )
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_AUTH_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _context() -> dict[str, str]:
    return {
        "method": "POST",
        "path": "/api/v1/users/admin-reset-password",
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }


def _request(target_id: uuid.UUID, *, operation_id: uuid.UUID | None = None):
    return decode_admin_password_request(
        {
            "schema": "aap-identity-admin-passwords/v1",
            "operation": "admin_reset_password",
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": "native-admin-password-test-token",
            "audit_context": _context(),
            "payload": {"target_user_id": str(target_id)},
        }
    )


def _principal(actor_id: uuid.UUID, email: str, *, role: str = "super_admin"):
    return Principal(
        actor_id=actor_id,
        name="Admin password fixture",
        email=email,
        role=role,
        is_active=True,
        credential_type="jwt",
        credential_id=uuid.uuid4(),
        generation=0,
        scopes=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )


class _CapturedAuthenticator:
    def __init__(self, verified: VerifiedCredential):
        self.verified = verified

    async def authenticate_bound(self, token: str) -> VerifiedCredential:
        assert token == "native-admin-password-test-token"
        return self.verified


async def _add_target(test_engine, *, role: str = "viewer") -> tuple[uuid.UUID, str]:
    target_id = uuid.uuid4()
    email = f"admin-password-target-{target_id.hex}@example.test"
    key_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(
            User(
                id=target_id,
                email=email,
                name="Reset target",
                password_hash="old-target-password-hash",
                credential_generation=0,
                role=role,
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            ApiKey(
                id=key_id,
                user_id=target_id,
                name="Retained target key",
                key_prefix=f"ak_{key_id.hex[:9]}",
                key_hash="retained-target-key-hash",
                scopes=["annotations:read"],
            )
        )
    return target_id, email


async def _cleanup_targets(test_engine, actor_id: uuid.UUID, target_ids, operation_ids):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await session.execute(
            text(
                "DELETE FROM public.identity_admin_password_reset_receipts "
                "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": list(operation_ids)},
        )
        await session.execute(
            text("DELETE FROM public.audit_logs WHERE actor_id=:actor_id"),
            {"actor_id": actor_id},
        )
        await session.execute(delete(ApiKey).where(ApiKey.user_id.in_(target_ids)))
        await session.execute(delete(User).where(User.id.in_(target_ids)))


def test_frozen_identity_admin_password_artifacts_and_scope_lock_order():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    canonical_schema = json.loads(_CANONICAL_SCHEMA.read_text(encoding="utf-8"))
    support = _SUPPORT.read_text(encoding="utf-8")
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    role_grants = _ROLE_GRANTS.read_text(encoding="utf-8")
    lan_grants = _LAN_GRANTS.read_text(encoding="utf-8")

    assert "identity_admin_password_reset_receipts" in NATIVE_MANAGED_TABLES
    assert schema == canonical_schema
    assert schema["$id"] == "aap-identity-admin-passwords-native/v1"
    assert migration.index(
        '"0244_identity_admin_passwords_support.sql"'
    ) < migration.index('"0244_identity_admin_passwords.sql"')
    assert "password_state_fingerprint" in support
    assert "FOREIGN KEY" not in support
    assert "password_hash" not in support
    assert "new_password_hash" not in support
    assert "FOR SHARE OF project NOWAIT" in support
    assert "FOR SHARE OF member NOWAIT" in support
    assert "member.project_id = ANY(v_locked_projects)" in support
    assert "FOR UPDATE OF account NOWAIT" in artifact
    assert "admin_password_target_in_owned_project" in artifact
    assert artifact.index("FOR UPDATE OF account NOWAIT") < artifact.index(
        "_lock_current_actor("
    )
    assert "pg_try_advisory_xact_lock" in artifact
    assert "public.digest(" in artifact
    assert "password_admin_reset_at = v_reset_at" in artifact
    assert "credential_generation" not in artifact
    assert "record_identity_admin_password_reset" in artifact
    assert "ON public.identity_admin_password_reset_receipts" in module_grants
    assert "admin_password_target_in_owned_project" in role_grants
    assert "record_identity_admin_password_reset" in role_grants
    assert "identity_admin_password_client" in role_grants
    assert "aap_identity_admin_passwords_v1.execute_admin_password_command(jsonb)" in (
        lan_grants
    )


@pytest.mark.asyncio
async def test_manager_replay_is_frozen_and_corrupt_native_reply_rolls_back(
    test_engine,
):
    dsn = _admin_password_dsn()
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id, target_email = await _add_target(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    request = _request(target_id, operation_id=operation_id)
    actor_email = await _stored_email(test_engine, fixture["actor_id"])
    principal = _principal(fixture["actor_id"], actor_email)
    authenticator = _CapturedAuthenticator(VerifiedCredential(principal, key_hash=None))
    manager = AdminPasswordManager(
        authenticator, AdminPasswordPostgresData(dsn), _SECRET
    )

    class _CorruptReplyTransaction:
        def __init__(self, transaction):
            self.transaction = transaction

        async def execute_admin_password_command(self, payload):
            reply = await self.transaction.execute_admin_password_command(payload)
            reply["operation_id"] = str(uuid.uuid4())
            return reply

    class _CorruptingData:
        def __init__(self):
            self.inner = AdminPasswordPostgresData(dsn)

        @asynccontextmanager
        async def transaction(self):
            async with self.inner.transaction() as transaction:
                yield _CorruptReplyTransaction(transaction)

    corrupt_request = _request(target_id)
    corrupt_manager = AdminPasswordManager(authenticator, _CorruptingData(), _SECRET)
    operation_ids = [operation_id, corrupt_request.operation_id]
    try:
        first = await manager.execute(request)
        assert first.data.target_user_id == target_id
        assert first.data.target_email == target_email
        async with maker() as session:
            before = await session.execute(
                text(
                    "SELECT password_hash, credential_generation, "
                    "password_admin_reset_at FROM public.users WHERE id=:id"
                ),
                {"id": target_id},
            )
            first_state = before.mappings().one()
            assert bcrypt.checkpw(
                first.data.temp_password.encode("utf-8"),
                first_state["password_hash"].encode("utf-8"),
            )
            assert first_state["credential_generation"] == 0
            assert (
                first_state["password_admin_reset_at"]
                == first.data.password_admin_reset_at
            )
            key_before = await session.execute(
                text(
                    "SELECT key_hash, revoked_at FROM public.api_keys WHERE user_id=:id"
                ),
                {"id": target_id},
            )
            retained_key = key_before.mappings().one()

        replay = await manager.execute(request)
        assert replay.data == first.data

        rotated_manager = AdminPasswordManager(
            authenticator, AdminPasswordPostgresData(dsn), f"{_SECRET}-rotated"
        )
        with pytest.raises(AdminPasswordFailure) as rotated:
            await rotated_manager.execute(request)
        assert rotated.value.code == "operation_conflict"
        assert rotated.value.status_code == 409

        with pytest.raises(IdentityUnavailable):
            await corrupt_manager.execute(corrupt_request)

        async with maker() as session:
            after = await session.execute(
                text(
                    "SELECT password_hash, credential_generation, "
                    "password_admin_reset_at FROM public.users WHERE id=:id"
                ),
                {"id": target_id},
            )
            final_state = after.mappings().one()
            key_after = await session.execute(
                text(
                    "SELECT key_hash, revoked_at FROM public.api_keys WHERE user_id=:id"
                ),
                {"id": target_id},
            )
            final_key = key_after.mappings().one()
            receipt_rows = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_admin_password_reset_receipts "
                    "WHERE target_user_id=:target_id"
                ),
                {"target_id": target_id},
            )
            audit_rows = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor_id "
                    "AND action='user.password_admin_reset'"
                ),
                {"actor_id": fixture["actor_id"]},
            )
            assert final_state["password_hash"] == first_state["password_hash"]
            assert final_state["credential_generation"] == 0
            assert (
                final_state["password_admin_reset_at"]
                == first_state["password_admin_reset_at"]
            )
            assert final_key == retained_key
            assert receipt_rows == 1
            assert audit_rows == 1
    finally:
        await _cleanup_targets(
            test_engine, fixture["actor_id"], [target_id], operation_ids
        )
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_same_timestamp_password_replacement_invalidates_replay(test_engine):
    dsn = _admin_password_dsn()
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id, _ = await _add_target(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    request = _request(target_id, operation_id=operation_id)
    actor_email = await _stored_email(test_engine, fixture["actor_id"])
    manager = AdminPasswordManager(
        _CapturedAuthenticator(
            VerifiedCredential(_principal(fixture["actor_id"], actor_email), None)
        ),
        AdminPasswordPostgresData(dsn),
        _SECRET,
    )
    try:
        first = await manager.execute(request)
        replacement = bcrypt.hashpw(
            b"superseding-password", bcrypt.gensalt(rounds=4)
        ).decode("ascii")
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET password_hash=:password_hash, "
                    "password_admin_reset_at=:reset_at WHERE id=:id"
                ),
                {
                    "password_hash": replacement,
                    "reset_at": first.data.password_admin_reset_at,
                    "id": target_id,
                },
            )

        with pytest.raises(AdminPasswordFailure) as raised:
            await manager.execute(request)
        assert raised.value.code == "operation_conflict"
        assert raised.value.status_code == 409

        async with maker() as session:
            target = await session.execute(
                text(
                    "SELECT password_hash, password_admin_reset_at "
                    "FROM public.users WHERE id=:id"
                ),
                {"id": target_id},
            )
            state = target.mappings().one()
            assert state["password_hash"] == replacement
            assert (
                state["password_admin_reset_at"] == first.data.password_admin_reset_at
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor_id "
                        "AND action='user.password_admin_reset'"
                    ),
                    {"actor_id": fixture["actor_id"]},
                )
                == 1
            )
    finally:
        await _cleanup_targets(
            test_engine, fixture["actor_id"], [target_id], [operation_id]
        )
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_native_fence_rejects_stale_manager_role_without_revoking_jwt(
    test_engine,
):
    dsn = _admin_password_dsn()
    auth_dsn = _auth_dsn(test_engine)
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id, _ = await _add_target(test_engine, role="employee")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "admin-password-stale-role-test-secret"
    token = create_access_token(
        str(fixture["actor_id"]), "super_admin", secret=secret, expire_minutes=15
    )
    authenticator = Authenticator(PostgresData(auth_dsn), secret)
    admitted = await authenticator.authenticate_bound(token)
    assert admitted.principal.role == "super_admin"
    generation = admitted.principal.generation
    request = _request(target_id)
    manager = AdminPasswordManager(
        _CapturedAuthenticator(admitted), AdminPasswordPostgresData(dsn), secret
    )
    try:
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET role='employee' WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )

        with pytest.raises(AdminPasswordFailure) as raised:
            await manager.execute(request)
        assert raised.value.code == "manager_required"
        assert raised.value.status_code == 403
        assert raised.value.detail == "当前账号已无管理权限"

        current = await authenticator.authenticate_bound(token)
        assert current.principal.role == "employee"
        assert current.principal.generation == generation
        async with maker() as session:
            target = await session.execute(
                text(
                    "SELECT password_hash, password_admin_reset_at "
                    "FROM public.users WHERE id=:id"
                ),
                {"id": target_id},
            )
            target_state = target.mappings().one()
            assert target_state["password_hash"] == "old-target-password-hash"
            assert target_state["password_admin_reset_at"] is None
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_admin_password_reset_receipts "
                        "WHERE operation_id=:operation_id"
                    ),
                    {"operation_id": request.operation_id},
                )
                == 0
            )
    finally:
        await _cleanup_targets(
            test_engine, fixture["actor_id"], [target_id], [request.operation_id]
        )
        await _cleanup(test_engine, fixture, jtis=[admitted.principal.credential_id])


@pytest.mark.asyncio
async def test_project_admin_preserves_employee_exception_and_owned_member_scope(
    test_engine,
):
    dsn = _admin_password_dsn()
    fixture = await _seed(test_engine, role="project_admin", create_key=False)
    employee_id, _ = await _add_target(test_engine, role="employee")
    reviewer_id, reviewer_email = await _add_target(test_engine, role="reviewer")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    project_display_id = f"pw-{project_id.hex[:17]}"
    employee_operation_id = uuid.uuid4()
    reviewer_operation_id = uuid.uuid4()
    rejected_request = _request(reviewer_id)
    actor_email = await _stored_email(test_engine, fixture["actor_id"])
    manager = AdminPasswordManager(
        _CapturedAuthenticator(
            VerifiedCredential(
                _principal(fixture["actor_id"], actor_email, role="project_admin"),
                None,
            )
        ),
        AdminPasswordPostgresData(dsn),
        _SECRET,
    )
    project = Project(
        id=project_id,
        display_id=project_display_id,
        name="Archived membership still authorizes reset",
        type_label="image",
        type_key="detection",
        owner_id=fixture["actor_id"],
        status="archived",
    )
    try:
        async with maker.begin() as session:
            session.add(project)
            await session.flush()
            session.add(
                ProjectMember(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    user_id=reviewer_id,
                    role="reviewer",
                    assigned_by=fixture["actor_id"],
                )
            )

        employee = await manager.execute(
            _request(employee_id, operation_id=employee_operation_id)
        )
        assert employee.data.target_user_id == employee_id
        reviewer = await manager.execute(
            _request(reviewer_id, operation_id=reviewer_operation_id)
        )
        assert reviewer.data.target_email == reviewer_email

        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.project_members WHERE project_id=:id"),
                {"id": project_id},
            )
        with pytest.raises(AdminPasswordFailure) as raised:
            await manager.execute(rejected_request)
        assert raised.value.code == "target_out_of_scope"
        assert raised.value.status_code == 403
        assert raised.value.detail == "该用户不在你管理的项目内"
    finally:
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.projects WHERE id=:id"),
                {"id": project_id},
            )
        await _cleanup_targets(
            test_engine,
            fixture["actor_id"],
            [employee_id, reviewer_id],
            [
                employee_operation_id,
                reviewer_operation_id,
                rejected_request.operation_id,
            ],
        )
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_native_user_lock_contention_returns_account_busy_without_write(
    test_engine,
):
    dsn = _admin_password_dsn()
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id, _ = await _add_target(test_engine)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    request = _request(target_id, operation_id=operation_id)
    actor_email = await _stored_email(test_engine, fixture["actor_id"])
    manager = AdminPasswordManager(
        _CapturedAuthenticator(
            VerifiedCredential(_principal(fixture["actor_id"], actor_email), None)
        ),
        AdminPasswordPostgresData(dsn),
        _SECRET,
    )
    try:
        async with maker() as blocker:
            async with blocker.begin():
                await blocker.execute(
                    text("SELECT id FROM public.users WHERE id=:id FOR UPDATE"),
                    {"id": target_id},
                )
                with pytest.raises(AdminPasswordFailure) as raised:
                    await manager.execute(request)
                assert raised.value.code == "account_busy"
                assert raised.value.status_code == 409
                assert raised.value.detail == "账号正在更新，请刷新后重试"

        async with maker() as session:
            state = await session.execute(
                text(
                    "SELECT password_hash, password_admin_reset_at "
                    "FROM public.users WHERE id=:id"
                ),
                {"id": target_id},
            )
            target = state.mappings().one()
            assert target["password_hash"] == "old-target-password-hash"
            assert target["password_admin_reset_at"] is None
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_admin_password_reset_receipts "
                        "WHERE operation_id=:operation_id"
                    ),
                    {"operation_id": operation_id},
                )
                == 0
            )
    finally:
        await _cleanup_targets(
            test_engine, fixture["actor_id"], [target_id], [operation_id]
        )
        await _cleanup(test_engine, fixture)


async def _stored_email(test_engine, actor_id: uuid.UUID) -> str:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        return await session.scalar(
            text("SELECT email FROM public.users WHERE id=:actor_id"),
            {"actor_id": actor_id},
        )
