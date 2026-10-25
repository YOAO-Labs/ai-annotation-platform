from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aap_identity
from aap_identity.authenticator import Authenticator, VerifiedCredential
from aap_identity.contract import IdentityUnavailable
from aap_identity.postgres import PostgresData
from aap_identity.security import create_access_token
from aap_identity.suspension_contract import (
    SuspensionFailure,
    decode_suspension_request,
)
from aap_identity.suspension_manager import SuspensionManager
from aap_identity.suspension_postgres import SuspensionPostgresData
from app.db.models.api_key import ApiKey
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.db.native_schema import NATIVE_MANAGED_TABLES
from tests.factory import create_project
from tests.test_identity_auth_native import _seed

_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_SCHEMA = _SQL_DIR / "0246_identity_suspensions.schema.json"
_SUPPORT = _SQL_DIR / "0246_identity_suspensions_support.sql"
_ARTIFACT = _SQL_DIR / "0246_identity_suspensions.sql"
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0246_identity_suspensions.py"
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_ROLE_GRANTS = _ROOT / "infra/docker/identity-suspensions-grants.sql"
_LAN_GRANTS = _ROOT / "infra/docker/lan-production-grants.sql"
_CANONICAL_SCHEMA = (
    _ROOT / "apps/identity/src/aap_identity/native_suspension_protocol.schema.json"
)
if not _CANONICAL_SCHEMA.is_file():
    _CANONICAL_SCHEMA = (
        Path(aap_identity.__file__).resolve().parent
        / "native_suspension_protocol.schema.json"
    )


def _suspension_dsn(test_engine=None) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity suspension acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_SUSPENSION_URL")
    if not value:
        pytest.skip("Identity suspension client DSN is not configured")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_suspension_client"
    assert os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    if test_engine is not None:
        assert (parsed.host, parsed.port, parsed.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _auth_dsn(test_engine) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity suspension JWT checks require the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_AUTH_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _context(operation: str, target_id: uuid.UUID) -> dict[str, str]:
    suffix = "deactivate" if operation == "suspend_account" else "reactivate"
    return {
        "method": "POST",
        "path": f"/api/v1/users/{target_id}/{suffix}",
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }


def _request(
    operation: str,
    target_id: uuid.UUID,
    token: str,
    *,
    operation_id: uuid.UUID | None = None,
    reason: str | None = None,
):
    payload = {"target_user_id": str(target_id)}
    if operation == "reactivate_account":
        payload["reason"] = reason
    return decode_suspension_request(
        {
            "schema": "aap-identity-suspensions/v1",
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": token,
            "audit_context": _context(operation, target_id),
            "payload": payload,
        }
    )


def _token(actor_id: uuid.UUID, role: str, secret: str) -> str:
    return create_access_token(str(actor_id), role, secret=secret, expire_minutes=15)


def _manager(test_engine, secret: str) -> SuspensionManager:
    return SuspensionManager(
        Authenticator(PostgresData(_auth_dsn(test_engine)), secret),
        SuspensionPostgresData(_suspension_dsn(test_engine)),
        secret,
    )


async def _add_user(test_engine, role: str, *, active: bool = True):
    user_id = uuid.uuid4()
    email = f"suspension-{user_id.hex}@example.test"
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(
            User(
                id=user_id,
                email=email,
                name=f"Suspension {role}",
                password_hash="test-only-suspension-hash",
                credential_generation=0,
                role=role,
                is_active=active,
                status="online" if active else "offline",
                disabled_kind=None if active else "suspended",
            )
        )
        await session.flush()
    return user_id, email


async def _add_key(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    key_id = uuid.uuid4()
    session.add(
        ApiKey(
            id=key_id,
            user_id=user_id,
            name=f"Suspension key {key_id.hex[:8]}",
            key_prefix=f"ak_{key_id.hex[:9]}",
            key_hash=f"non-authenticating-hash-{key_id.hex}",
            scopes=["annotations:read"],
        )
    )
    await session.flush()
    return key_id


async def _add_reset_token(
    session: AsyncSession, user_id: uuid.UUID, email: str
) -> uuid.UUID:
    token_id = uuid.uuid4()
    session.add(
        PasswordResetToken(
            id=token_id,
            user_id=user_id,
            token_hash=hashlib.sha256(token_id.bytes).hexdigest(),
            issued_email=email,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await session.flush()
    return token_id


async def _add_project(test_engine, owner_id: uuid.UUID, name: str) -> uuid.UUID:
    project_id = None
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        project = await create_project(session, owner_id=owner_id, name=name)
        project_id = project.id
    return project_id


async def _add_member(
    test_engine,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    assigned_by: uuid.UUID,
) -> uuid.UUID:
    member_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(
            ProjectMember(
                id=member_id,
                project_id=project_id,
                user_id=user_id,
                role="annotator",
                assigned_by=assigned_by,
            )
        )
        await session.flush()
    return member_id


async def _add_batch(
    test_engine, project_id: uuid.UUID, target_id: uuid.UUID, actor_id: uuid.UUID
) -> uuid.UUID:
    batch_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(
            TaskBatch(
                id=batch_id,
                project_id=project_id,
                display_id=f"S-{batch_id.hex[:10]}",
                name="Suspension scope",
                status="active",
                annotator_id=target_id,
                created_by=actor_id,
            )
        )
        await session.flush()
    return batch_id


async def _add_task_and_lock(
    test_engine, project_id: uuid.UUID, target_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    task_id = uuid.uuid4()
    lock_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(
            Task(
                id=task_id,
                project_id=project_id,
                display_id=f"ST-{task_id.hex[:10]}",
                file_name="suspension-scope.jpg",
                file_path=f"suspension/{task_id.hex}.jpg",
                status="pending",
                assignee_id=target_id,
            )
        )
        await session.flush()
        session.add(
            TaskLock(
                id=lock_id,
                task_id=task_id,
                user_id=target_id,
                expire_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await session.flush()
    return task_id, lock_id


async def _cleanup_world(
    test_engine,
    *,
    actor_ids=(),
    target_ids=(),
    operation_ids=(),
    project_ids=(),
    batch_ids=(),
    task_ids=(),
):
    user_ids = list({*actor_ids, *target_ids})
    project_ids = list(project_ids)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await session.execute(
            text(
                "DELETE FROM public.identity_suspension_operation_receipts "
                "WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": list(operation_ids)},
        )
        await session.execute(
            text(
                "DELETE FROM public.audit_logs WHERE actor_id=ANY(CAST(:actors AS uuid[])) "
                "OR target_id=ANY(CAST(:targets AS text[]))"
            ),
            {
                "actors": list(actor_ids),
                "targets": [str(user_id) for user_id in target_ids],
            },
        )
        await session.execute(
            text(
                "DELETE FROM public.task_locks WHERE task_id=ANY(CAST(:tasks AS uuid[])) "
                "OR user_id=ANY(CAST(:users AS uuid[]))"
            ),
            {"tasks": list(task_ids), "users": user_ids},
        )
        await session.execute(
            text(
                "DELETE FROM public.tasks WHERE id=ANY(CAST(:tasks AS uuid[])) "
                "OR project_id=ANY(CAST(:projects AS uuid[]))"
            ),
            {"tasks": list(task_ids), "projects": project_ids},
        )
        await session.execute(
            text(
                "DELETE FROM public.task_batches WHERE id=ANY(CAST(:batches AS uuid[])) "
                "OR project_id=ANY(CAST(:projects AS uuid[]))"
            ),
            {"batches": list(batch_ids), "projects": project_ids},
        )
        await session.execute(
            text(
                "DELETE FROM public.project_members "
                "WHERE project_id=ANY(CAST(:projects AS uuid[])) "
                "OR user_id=ANY(CAST(:users AS uuid[]))"
            ),
            {"projects": project_ids, "users": user_ids},
        )
        await session.execute(
            text("DELETE FROM public.projects WHERE id=ANY(CAST(:ids AS uuid[]))"),
            {"ids": project_ids},
        )
        await session.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id.in_(user_ids))
        )
        await session.execute(delete(ApiKey).where(ApiKey.user_id.in_(user_ids)))
        await session.execute(
            text(
                "DELETE FROM public.email_verification_tokens "
                "WHERE user_id=ANY(CAST(:users AS uuid[]))"
            ),
            {"users": user_ids},
        )
        await session.execute(delete(User).where(User.id.in_(user_ids)))


class _CapturedAuthenticator:
    def __init__(self, credential: VerifiedCredential):
        self.credential = credential

    async def authenticate_bound(self, token: str) -> VerifiedCredential:
        return self.credential


class _CorruptReplyTransaction:
    def __init__(self, transaction):
        self.transaction = transaction

    async def execute_suspension_command(self, payload):
        reply = await self.transaction.execute_suspension_command(payload)
        if reply.get("ok") is True:
            reply["operation_id"] = str(uuid.uuid4())
        return reply


class _CorruptingData:
    def __init__(self, dsn: str):
        self.inner = SuspensionPostgresData(dsn)

    @asynccontextmanager
    async def transaction(self):
        async with self.inner.transaction() as transaction:
            yield _CorruptReplyTransaction(transaction)


def test_frozen_suspension_artifacts_and_native_lock_contract():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    canonical = json.loads(_CANONICAL_SCHEMA.read_text(encoding="utf-8"))
    support = _SUPPORT.read_text(encoding="utf-8")
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    role_grants = _ROLE_GRANTS.read_text(encoding="utf-8")
    lan_grants = _LAN_GRANTS.read_text(encoding="utf-8")
    verifier = (_ROOT / "scripts/verify_module_data_boundary.py").read_text(
        encoding="utf-8"
    )

    assert "identity_suspension_operation_receipts" in NATIVE_MANAGED_TABLES
    assert schema == canonical
    assert _SCHEMA.read_bytes() == _CANONICAL_SCHEMA.read_bytes()
    assert schema["$id"] == "aap-identity-suspensions-native/v1"
    assert migration.index('"0246_identity_suspensions_support.sql"') < migration.index(
        '"0246_identity_suspensions.sql"'
    )
    assert "FOREIGN KEY" not in support
    assert "target_generation bigint NOT NULL" in support
    assert "target_is_active boolean NOT NULL" in support
    assert "lock_identity_suspension_scope(uuid,uuid,text)" in migration
    assert "RETURNS TABLE(has_projects boolean, all_projects_owned boolean)" in support
    assert "identity_directory_project_refs" in support
    assert "FOR KEY SHARE OF project NOWAIT" in support
    assert "FOR SHARE OF project NOWAIT" in support
    assert "FOR UPDATE OF account NOWAIT" in artifact
    assert artifact.index("lock_super_admin_invariant()") < artifact.index(
        "FOR UPDATE OF account NOWAIT"
    )
    assert artifact.index("FOR UPDATE OF account NOWAIT") < artifact.index(
        "_lock_current_actor("
    )
    assert "retire_account_credentials(v_target_user_id)" in artifact
    assert "_strip_unicode_space(v_reason)" not in artifact
    assert "ARRAY['target_user_id', 'reason_present']" in artifact
    assert "pg_catalog.jsonb_typeof(v_payload->'reason_present')" in artifact
    assert (
        artifact.count("INTO STRICT v_scope_has_projects, v_scope_all_projects_owned")
        == 2
    )
    assert artifact.count("v_scope_has_projects IS NULL") == 2
    receipt_binding = artifact.index(
        "v_receipt.request_fingerprint IS DISTINCT FROM v_request_fingerprint"
    )
    receipt_state_fence = artifact.index(
        "v_receipt.target_generation IS DISTINCT FROM v_target_generation"
    )
    suspension_policy = artifact.index(
        "IF v_operation = 'suspend_account' THEN", receipt_binding
    )
    assert receipt_binding < receipt_state_fence < suspension_policy
    assert (
        artifact.count(
            "v_receipt.target_generation IS DISTINCT FROM v_target_generation"
        )
        == 1
    )
    assert "deactivation_requested_at =" not in artifact
    assert "deactivation_scheduled_at =" not in artifact
    assert "disabled_reason = '管理员停用账号'" in artifact
    assert "record_identity_suspension_event" in artifact
    assert "identity_suspension_operation_receipts" in module_grants
    assert "identity_suspension_role=identity_suspension_client" in role_grants
    expected_functions = role_grants.split("expected_functions text[] := ARRAY[", 1)[
        1
    ].split("];", 1)[0]
    assert expected_functions.index(
        "execute_suspension_command"
    ) < expected_functions.index("suspension_policy")
    assert "aap_identity_suspensions_v1.execute_suspension_command(jsonb)" in (
        lan_grants
    )
    assert '"SUSPENSION": "identity_suspension_client"' in verifier


@pytest.mark.asyncio
async def test_suspend_reactivate_receipts_and_new_credentials_survive_replay(
    test_engine,
):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    actor_id = fixture["actor_id"]
    target_id, target_email = await _add_user(test_engine, "employee")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "identity-suspension-receipt-test-secret"
    token = _token(actor_id, "super_admin", secret)
    manager = _manager(test_engine, secret)
    suspend_request_id = uuid.uuid4()
    reactivate_request_id = uuid.uuid4()
    suspend_request = _request(
        "suspend_account",
        target_id,
        token,
        operation_id=suspend_request_id,
    )
    reactivate_request = _request(
        "reactivate_account",
        target_id,
        token,
        operation_id=reactivate_request_id,
        reason="\u2003\u3000",
    )
    key_id = uuid.uuid4()
    reset_id = uuid.uuid4()
    new_key_id = uuid.uuid4()
    new_reset_id = uuid.uuid4()
    now = datetime.now(UTC)
    requested_at = now - timedelta(minutes=2)
    scheduled_at = now + timedelta(days=6)
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET deactivation_requested_at=:requested, "
                    "deactivation_scheduled_at=:scheduled, deactivation_reason=:reason "
                    "WHERE id=:target"
                ),
                {
                    "requested": requested_at,
                    "scheduled": scheduled_at,
                    "reason": "Accepted self-deactivation",
                    "target": target_id,
                },
            )
            await session.flush()
            session.add(
                ApiKey(
                    id=key_id,
                    user_id=target_id,
                    name="Old integration key",
                    key_prefix=f"ak_{key_id.hex[:9]}",
                    key_hash=f"old-unrelated-hash-{key_id.hex}",
                    scopes=["annotations:read"],
                )
            )
            session.add(
                PasswordResetToken(
                    id=reset_id,
                    user_id=target_id,
                    token_hash=hashlib.sha256(reset_id.bytes).hexdigest(),
                    issued_email=target_email,
                    expires_at=now + timedelta(days=1),
                )
            )

        suspended = await manager.execute(suspend_request)
        assert suspended.data.user.id == target_id
        assert suspended.data.user.is_active is False
        assert suspended.data.user.status == "offline"
        assert suspended.data.user.disabled_kind == "suspended"
        assert suspended.data.user.disabled_reason == "管理员停用账号"
        assert suspended.data.user.deactivation_requested_at == requested_at
        assert suspended.data.user.deactivation_scheduled_at == scheduled_at

        async with maker() as session:
            state = (
                (
                    await session.execute(
                        text(
                            "SELECT credential_generation, is_active, disabled_kind, "
                            "deactivation_requested_at, deactivation_scheduled_at, "
                            "deactivation_reason FROM public.users WHERE id=:id"
                        ),
                        {"id": target_id},
                    )
                )
                .mappings()
                .one()
            )
            assert state["credential_generation"] == 1
            assert state["is_active"] is False
            assert state["disabled_kind"] == "suspended"
            assert state["deactivation_requested_at"] == requested_at
            assert state["deactivation_scheduled_at"] == scheduled_at
            assert state["deactivation_reason"] == "Accepted self-deactivation"
            assert (
                await session.scalar(
                    text("SELECT revoked_at FROM public.api_keys WHERE id=:id"),
                    {"id": key_id},
                )
            ) is not None
            assert (
                await session.scalar(
                    text(
                        "SELECT used_at FROM public.password_reset_tokens WHERE id=:id"
                    ),
                    {"id": reset_id},
                )
            ) is not None
            receipt = (
                (
                    await session.execute(
                        text(
                            "SELECT target_generation, target_is_active, result, audit_id "
                            "FROM public.identity_suspension_operation_receipts "
                            "WHERE operation_id=:id"
                        ),
                        {"id": suspend_request_id},
                    )
                )
                .mappings()
                .one()
            )
            assert receipt["target_generation"] == 1
            assert receipt["target_is_active"] is False
            assert receipt["result"]["user"]["is_active"] is False
            assert receipt["audit_id"] is not None
            assert await session.scalar(
                text(
                    "SELECT detail_json FROM public.audit_logs "
                    "WHERE action='user.deactivate' AND target_id=:id"
                ),
                {"id": str(target_id)},
            ) == {"email": target_email}

        replayed_suspend = await manager.execute(suspend_request)
        assert replayed_suspend.data == suspended.data
        reactivated = await manager.execute(reactivate_request)
        assert reactivated.data.user.is_active is True
        assert reactivated.data.user.disabled_kind is None
        assert reactivated.data.user.disabled_at is None
        assert reactivated.data.user.disabled_by is None
        assert reactivated.data.user.disabled_reason is None
        assert reactivated.data.user.deactivation_requested_at == requested_at
        assert reactivated.data.user.deactivation_scheduled_at == scheduled_at

        async with maker.begin() as session:
            session.add(
                ApiKey(
                    id=new_key_id,
                    user_id=target_id,
                    name="Issued after reactivation",
                    key_prefix=f"ak_{new_key_id.hex[:9]}",
                    key_hash=f"new-unrelated-hash-{new_key_id.hex}",
                    scopes=["annotations:read"],
                )
            )
            session.add(
                PasswordResetToken(
                    id=new_reset_id,
                    user_id=target_id,
                    token_hash=hashlib.sha256(new_reset_id.bytes).hexdigest(),
                    issued_email=target_email,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )

        replayed_reactivation = await manager.execute(reactivate_request)
        assert replayed_reactivation.data == reactivated.data
        async with maker() as session:
            final = (
                (
                    await session.execute(
                        text(
                            "SELECT credential_generation, is_active, deactivation_requested_at, "
                            "deactivation_scheduled_at, deactivation_reason "
                            "FROM public.users WHERE id=:id"
                        ),
                        {"id": target_id},
                    )
                )
                .mappings()
                .one()
            )
            assert final["credential_generation"] == 2
            assert final["is_active"] is True
            assert final["deactivation_requested_at"] == requested_at
            assert final["deactivation_scheduled_at"] == scheduled_at
            assert final["deactivation_reason"] == "Accepted self-deactivation"
            assert (
                await session.scalar(
                    text("SELECT revoked_at FROM public.api_keys WHERE id=:id"),
                    {"id": new_key_id},
                )
            ) is None
            assert (
                await session.scalar(
                    text(
                        "SELECT used_at FROM public.password_reset_tokens WHERE id=:id"
                    ),
                    {"id": new_reset_id},
                )
            ) is None
            assert await session.scalar(
                text(
                    "SELECT detail_json FROM public.audit_logs "
                    "WHERE action='user.reactivate' AND target_id=:id"
                ),
                {"id": str(target_id)},
            ) == {"reason_present": False}
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE target_id=:id "
                        "AND action IN ('user.deactivate','user.reactivate')"
                    ),
                    {"id": str(target_id)},
                )
                == 2
            )
    finally:
        await _cleanup_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[suspend_request_id, reactivate_request_id],
        )


@pytest.mark.asyncio
async def test_reactivate_reason_presence_uses_python_unicode_strip_semantics(
    test_engine,
):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    actor_id = fixture["actor_id"]
    target_id, _ = await _add_user(test_engine, "employee")
    secret = "identity-suspension-unicode-reason-secret"
    manager = _manager(test_engine, secret)
    token = _token(actor_id, "super_admin", secret)
    suspend_id = uuid.uuid4()
    reactivate_id = uuid.uuid4()

    try:
        suspended = await manager.execute(
            _request("suspend_account", target_id, token, operation_id=suspend_id)
        )
        assert suspended.data.user.is_active is False

        # U+0000 was accepted by the public Python path but cannot be passed in
        # PostgreSQL jsonb. The manager must project only this presence bit.
        reactivated = await manager.execute(
            _request(
                "reactivate_account",
                target_id,
                token,
                operation_id=reactivate_id,
                reason="\0",
            )
        )
        assert reactivated.data.user.is_active is True

        maker = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with maker() as session:
            assert await session.scalar(
                text(
                    "SELECT detail_json FROM public.audit_logs "
                    "WHERE action='user.reactivate' AND target_id=:id"
                ),
                {"id": str(target_id)},
            ) == {"reason_present": True}
    finally:
        await _cleanup_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[suspend_id, reactivate_id],
        )


@pytest.mark.parametrize(
    "scope_rows",
    [
        "SELECT NULL::boolean AS has_projects, NULL::boolean AS all_projects_owned",
        "SELECT true AS has_projects, false AS all_projects_owned "
        "UNION ALL SELECT true, false",
    ],
)
@pytest.mark.asyncio
async def test_malformed_workspace_scope_reply_fails_closed_without_effects(
    test_engine, scope_rows: str
):
    fixture = await _seed(test_engine, role="project_admin", create_key=False)
    actor_id = fixture["actor_id"]
    target_id, _ = await _add_user(test_engine, "employee")
    secret = "identity-suspension-invalid-scope-test-secret"
    manager = _manager(test_engine, secret)
    request_id = uuid.uuid4()
    request = _request(
        "suspend_account",
        target_id,
        _token(actor_id, "project_admin", secret),
        operation_id=request_id,
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    function = "aap_workspace_v1.lock_identity_suspension_scope"
    original_name = f"_sus_scope_orig_{uuid.uuid4().hex}"
    original = f"aap_workspace_v1.{original_name}"
    wrapped = False
    override = f"""
        CREATE FUNCTION {function}(
            p_actor_id uuid, p_target_id uuid, p_operation text
        ) RETURNS TABLE(has_projects boolean, all_projects_owned boolean)
        LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$ {scope_rows} $function$
    """
    try:
        async with maker.begin() as session:
            connection = await session.connection()
            await connection.exec_driver_sql(
                f"ALTER FUNCTION {function}(uuid, uuid, text) RENAME TO {original_name}"
            )
            await connection.exec_driver_sql(override)
            await connection.exec_driver_sql(
                f"REVOKE ALL ON FUNCTION {function}(uuid, uuid, text) FROM PUBLIC"
            )
            await connection.exec_driver_sql(
                f"ALTER FUNCTION {function}(uuid, uuid, text) OWNER TO workspace_code"
            )
            await connection.exec_driver_sql(
                f"GRANT EXECUTE ON FUNCTION {function}(uuid, uuid, text) TO identity_code"
            )
        wrapped = True

        with pytest.raises(SuspensionFailure) as failure:
            await manager.execute(request)
        assert failure.value.code == "unavailable"
        assert failure.value.status_code == 503
        assert failure.value.detail is None

        async with maker() as session:
            target = (
                (
                    await session.execute(
                        text(
                            "SELECT is_active, credential_generation, disabled_kind "
                            "FROM public.users WHERE id=:id"
                        ),
                        {"id": target_id},
                    )
                )
                .mappings()
                .one()
            )
            assert target == {
                "is_active": True,
                "credential_generation": 0,
                "disabled_kind": None,
            }
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_suspension_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": request_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE target_id=:id "
                        "AND action IN ('user.deactivate', 'user.reactivate')"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        if wrapped:
            async with maker.begin() as session:
                connection = await session.connection()
                await connection.exec_driver_sql(
                    f"DROP FUNCTION {function}(uuid, uuid, text)"
                )
                await connection.exec_driver_sql(
                    f"ALTER FUNCTION {original}(uuid, uuid, text) "
                    "RENAME TO lock_identity_suspension_scope"
                )
        await _cleanup_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[request_id],
        )


@pytest.mark.asyncio
async def test_inactive_suspend_noop_precedes_pa_role_and_scope_and_is_receipted(
    test_engine,
):
    fixture = await _seed(test_engine, role="project_admin", create_key=False)
    actor_id = fixture["actor_id"]
    target_id, _ = await _add_user(test_engine, "super_admin", active=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "identity-suspension-noop-test-secret"
    token = _token(actor_id, "project_admin", secret)
    manager = _manager(test_engine, secret)
    operation_id = uuid.uuid4()
    request = _request("suspend_account", target_id, token, operation_id=operation_id)
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET disabled_kind='deleted', "
                    "disabled_reason='Preserve historical state' WHERE id=:id"
                ),
                {"id": target_id},
            )
        first = await manager.execute(request)
        assert first.data.user.is_active is False
        assert first.data.user.role == "super_admin"
        assert first.data.user.disabled_kind == "deleted"
        assert first.data.user.disabled_reason == "Preserve historical state"
        replay = await manager.execute(request)
        assert replay.data == first.data
        async with maker() as session:
            state = (
                (
                    await session.execute(
                        text(
                            "SELECT credential_generation, disabled_kind, disabled_reason "
                            "FROM public.users WHERE id=:id"
                        ),
                        {"id": target_id},
                    )
                )
                .mappings()
                .one()
            )
            assert state == {
                "credential_generation": 0,
                "disabled_kind": "deleted",
                "disabled_reason": "Preserve historical state",
            }
            assert (
                await session.scalar(
                    text(
                        "SELECT audit_id FROM public.identity_suspension_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
            ) is None
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE target_id=:id "
                        "AND action='user.deactivate'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )

        # The accepted no-op freezes its inactive result. If another lifecycle
        # writer activates the target afterward, the old receipt must conflict
        # before PA target-role policy can reject it as a fresh request.
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET is_active=true, status='online', "
                    "disabled_kind=NULL, disabled_at=NULL, disabled_by=NULL, "
                    "disabled_reason=NULL WHERE id=:id"
                ),
                {"id": target_id},
            )
        with pytest.raises(SuspensionFailure) as changed_poststate:
            await manager.execute(request)
        assert changed_poststate.value.code == "operation_conflict"
        assert changed_poststate.value.status_code == 409

        async with maker() as session:
            state = (
                (
                    await session.execute(
                        text(
                            "SELECT is_active, credential_generation, disabled_kind, "
                            "disabled_reason FROM public.users WHERE id=:id"
                        ),
                        {"id": target_id},
                    )
                )
                .mappings()
                .one()
            )
            assert state == {
                "is_active": True,
                "credential_generation": 0,
                "disabled_kind": None,
                "disabled_reason": None,
            }
            receipt = (
                (
                    await session.execute(
                        text(
                            "SELECT result, audit_id FROM "
                            "public.identity_suspension_operation_receipts "
                            "WHERE operation_id=:id"
                        ),
                        {"id": operation_id},
                    )
                )
                .mappings()
                .one()
            )
            assert receipt["result"]["user"]["is_active"] is False
            assert receipt["result"]["user"]["disabled_kind"] == "deleted"
            assert receipt["audit_id"] is None
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE target_id=:id "
                        "AND action='user.deactivate'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        await _cleanup_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
        )


@pytest.mark.asyncio
async def test_pa_empty_scope_rules_and_all_derived_labeling_scope(test_engine):
    pa = await _seed(test_engine, role="project_admin", create_key=False)
    owner = await _seed(test_engine, role="super_admin", create_key=False)
    actor_id = pa["actor_id"]
    owner_id = owner["actor_id"]
    target_id, _ = await _add_user(test_engine, "employee")
    empty_secret = "identity-suspension-pa-empty-secret"
    empty_token = _token(actor_id, "project_admin", empty_secret)
    empty_manager = _manager(test_engine, empty_secret)
    empty_suspend_id = uuid.uuid4()
    empty_reactivate_id = uuid.uuid4()
    empty_suspend = _request(
        "suspend_account",
        target_id,
        empty_token,
        operation_id=empty_suspend_id,
    )
    empty_reactivate = _request(
        "reactivate_account",
        target_id,
        empty_token,
        operation_id=empty_reactivate_id,
        reason=None,
    )
    task_ids: list[uuid.UUID] = []
    lock_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    batch_ids: list[uuid.UUID] = []
    operation_ids = [
        empty_suspend_id,
        empty_reactivate_id,
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
        uuid.uuid4(),
    ]
    try:
        empty_result = await empty_manager.execute(empty_suspend)
        assert empty_result.data.user.is_active is False
        with pytest.raises(SuspensionFailure) as no_scope:
            await empty_manager.execute(empty_reactivate)
        assert no_scope.value.code == "reactivate_not_managed"
        assert no_scope.value.status_code == 403
        assert no_scope.value.detail == "该用户不在你管理的项目内"

        owned_project_id = await _add_project(
            test_engine, actor_id, "Suspension owned project"
        )
        project_ids.append(owned_project_id)
        foreign_project_id = await _add_project(
            test_engine, owner_id, "Suspension foreign project"
        )
        project_ids.append(foreign_project_id)
        await _add_member(test_engine, owned_project_id, target_id, actor_id)
        batch_id = await _add_batch(
            test_engine, foreign_project_id, target_id, owner_id
        )
        batch_ids.append(batch_id)

        # A membership in one owned project cannot hide a foreign Labeling
        # reference from a batch assignment.
        with pytest.raises(SuspensionFailure) as foreign_batch:
            await empty_manager.execute(
                _request(
                    "reactivate_account",
                    target_id,
                    empty_token,
                    operation_id=operation_ids[2],
                    reason=None,
                )
            )
        assert foreign_batch.value.code == "reactivate_scope_forbidden"
        assert foreign_batch.value.detail == "该用户存在不在你管理范围内的项目"

        maker = async_sessionmaker(
            test_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.task_batches WHERE id=:id"),
                {"id": batch_id},
            )
        task_id, lock_id = await _add_task_and_lock(
            test_engine, foreign_project_id, target_id
        )
        task_ids.append(task_id)
        lock_ids.append(lock_id)
        with pytest.raises(SuspensionFailure) as foreign_task:
            await empty_manager.execute(
                _request(
                    "reactivate_account",
                    target_id,
                    empty_token,
                    operation_id=operation_ids[3],
                    reason=None,
                )
            )
        assert foreign_task.value.code == "reactivate_scope_forbidden"
        assert foreign_task.value.detail == "该用户存在不在你管理范围内的项目"

        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.task_locks WHERE id=:id"), {"id": lock_id}
            )
            await session.execute(
                text("DELETE FROM public.tasks WHERE id=:id"), {"id": task_id}
            )
        resumed = await empty_manager.execute(
            _request(
                "reactivate_account",
                target_id,
                empty_token,
                operation_id=operation_ids[4],
                reason="restored",
            )
        )
        assert resumed.data.user.is_active is True

        # Suspend also checks the complete derived set for an active target.
        batch_id = await _add_batch(
            test_engine, foreign_project_id, target_id, owner_id
        )
        batch_ids.append(batch_id)
        with pytest.raises(SuspensionFailure) as suspend_scope:
            await empty_manager.execute(
                _request(
                    "suspend_account",
                    target_id,
                    empty_token,
                    operation_id=operation_ids[5],
                )
            )
        assert suspend_scope.value.code == "suspend_scope_forbidden"
        assert suspend_scope.value.detail == "该用户跨多个项目，须由超级管理员处理"
    finally:
        await _cleanup_world(
            test_engine,
            actor_ids=[actor_id, owner_id],
            target_ids=[target_id],
            operation_ids=operation_ids,
            project_ids=project_ids,
            batch_ids=batch_ids,
            task_ids=task_ids,
        )


@pytest.mark.asyncio
async def test_last_unreserved_super_admin_is_preserved(test_engine):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    actor_id = fixture["actor_id"]
    target_id, _ = await _add_user(test_engine, "super_admin")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(UTC)
    secret = "identity-suspension-last-admin-secret"
    token = _token(actor_id, "super_admin", secret)
    manager = _manager(test_engine, secret)
    operation_id = uuid.uuid4()
    request = _request("suspend_account", target_id, token, operation_id=operation_id)
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET deactivation_requested_at=:requested, "
                    "deactivation_scheduled_at=:scheduled WHERE id=:actor"
                ),
                {
                    "requested": now,
                    "scheduled": now + timedelta(days=7),
                    "actor": actor_id,
                },
            )
        with pytest.raises(SuspensionFailure) as blocked:
            await manager.execute(request)
        assert blocked.value.code == "last_super_admin"
        assert blocked.value.status_code == 400
        assert blocked.value.detail == "不能停用最后一名超级管理员"
        async with maker() as session:
            target = (
                (
                    await session.execute(
                        text(
                            "SELECT is_active, role, credential_generation "
                            "FROM public.users WHERE id=:id"
                        ),
                        {"id": target_id},
                    )
                )
                .mappings()
                .one()
            )
            assert target == {
                "is_active": True,
                "role": "super_admin",
                "credential_generation": 0,
            }
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_suspension_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE target_id=:id "
                        "AND action='user.deactivate'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        await _cleanup_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
        )


@pytest.mark.asyncio
async def test_current_actor_role_and_target_row_contention_are_rechecked(test_engine):
    dsn = _suspension_dsn(test_engine)
    auth_dsn = _auth_dsn(test_engine)
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    actor_id = fixture["actor_id"]
    target_id, _ = await _add_user(test_engine, "employee")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "identity-suspension-stale-role-secret"
    token = _token(actor_id, "super_admin", secret)
    authenticator = Authenticator(PostgresData(auth_dsn), secret)
    admitted = await authenticator.authenticate_bound(token)
    request = _request("suspend_account", target_id, token)
    stale_manager = SuspensionManager(
        _CapturedAuthenticator(admitted), SuspensionPostgresData(dsn), secret
    )
    operation_id = request.operation_id
    try:
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET role='employee' WHERE id=:id"),
                {"id": actor_id},
            )
        with pytest.raises(SuspensionFailure) as denied:
            await stale_manager.execute(request)
        assert denied.value.code == "manager_required"
        assert denied.value.status_code == 403
        assert denied.value.detail == "当前账号已无管理权限"
        current = await authenticator.authenticate_bound(token)
        assert current.principal.role == "employee"
        assert current.principal.generation == admitted.principal.generation

        async with maker() as blocker:
            await blocker.begin()
            await blocker.execute(
                select(User.id).where(User.id == target_id).with_for_update()
            )
            with pytest.raises(SuspensionFailure) as busy:
                await stale_manager.execute(
                    _request("suspend_account", target_id, token)
                )
            assert busy.value.code == "account_busy"
            assert busy.value.status_code == 409
            assert busy.value.detail == "账号正在更新，请刷新后重试"
            await blocker.rollback()
        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT is_active FROM public.users WHERE id=:id"),
                    {"id": target_id},
                )
            ) is True
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_suspension_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 0
            )
    finally:
        await _cleanup_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
        )


@pytest.mark.asyncio
async def test_project_lock_contention_preserves_operation_specific_conflict(
    test_engine,
):
    fixture = await _seed(test_engine, role="project_admin", create_key=False)
    actor_id = fixture["actor_id"]
    target_id, _ = await _add_user(test_engine, "employee")
    project_id = await _add_project(test_engine, actor_id, "Suspension lock project")
    await _add_member(test_engine, project_id, target_id, actor_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "identity-suspension-project-lock-secret"
    token = _token(actor_id, "project_admin", secret)
    manager = _manager(test_engine, secret)
    suspend_id = uuid.uuid4()
    reactivate_id = uuid.uuid4()
    suspend_request = _request(
        "suspend_account", target_id, token, operation_id=suspend_id
    )
    reactivate_request = _request(
        "reactivate_account",
        target_id,
        token,
        operation_id=reactivate_id,
        reason=None,
    )
    try:
        async with maker() as blocker:
            await blocker.begin()
            await blocker.execute(
                select(Project.id).where(Project.id == project_id).with_for_update()
            )
            with pytest.raises(SuspensionFailure) as suspend_busy:
                await manager.execute(suspend_request)
            assert suspend_busy.value.code == "project_busy"
            assert suspend_busy.value.status_code == 409
            assert suspend_busy.value.detail == {"reason": "project_busy"}
            await blocker.rollback()

        suspended = await manager.execute(suspend_request)
        assert suspended.data.user.is_active is False
        async with maker() as blocker:
            await blocker.begin()
            await blocker.execute(
                select(Project.id).where(Project.id == project_id).with_for_update()
            )
            with pytest.raises(SuspensionFailure) as reactivate_busy:
                await manager.execute(reactivate_request)
            assert reactivate_busy.value.code == "account_busy"
            assert reactivate_busy.value.status_code == 409
            assert reactivate_busy.value.detail == "账号正在更新，请刷新后重试"
            await blocker.rollback()
        reactivated = await manager.execute(reactivate_request)
        assert reactivated.data.user.is_active is True
    finally:
        await _cleanup_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[suspend_id, reactivate_id],
            project_ids=[project_id],
        )


@pytest.mark.asyncio
async def test_malformed_native_success_rolls_back_user_audit_and_receipt(test_engine):
    dsn = _suspension_dsn(test_engine)
    auth_dsn = _auth_dsn(test_engine)
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    actor_id = fixture["actor_id"]
    target_id, _ = await _add_user(test_engine, "employee")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "identity-suspension-corrupt-reply-secret"
    token = _token(actor_id, "super_admin", secret)
    request = _request("suspend_account", target_id, token)
    manager = SuspensionManager(
        Authenticator(PostgresData(auth_dsn), secret),
        _CorruptingData(dsn),
        secret,
    )
    try:
        with pytest.raises(IdentityUnavailable):
            await manager.execute(request)
        async with maker() as session:
            state = (
                (
                    await session.execute(
                        text(
                            "SELECT is_active, credential_generation, disabled_kind "
                            "FROM public.users WHERE id=:id"
                        ),
                        {"id": target_id},
                    )
                )
                .mappings()
                .one()
            )
            assert state == {
                "is_active": True,
                "credential_generation": 0,
                "disabled_kind": None,
            }
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_suspension_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": request.operation_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE target_id=:id "
                        "AND action='user.deactivate'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        await _cleanup_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[request.operation_id],
        )
