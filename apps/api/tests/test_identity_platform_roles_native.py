from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aap_identity
from aap_identity.authenticator import Authenticator, VerifiedCredential
from aap_identity.contract import IdentityUnavailable
from aap_identity.platform_role_contract import (
    PlatformRoleFailure,
    decode_labeling_platform_role_facts,
    decode_platform_role_request,
    decode_workspace_platform_role_facts,
    validate_platform_role_fact_coverage,
)
from aap_identity.platform_role_manager import PlatformRoleManager
from aap_identity.platform_role_postgres import PlatformRolePostgresData
from aap_identity.postgres import PostgresData
from aap_identity.security import create_access_token
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.db.native_schema import NATIVE_MANAGED_TABLES
from tests.test_identity_auth_native import _cleanup, _seed


_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_NATIVE_SCHEMA = _SQL_DIR / "0245_identity_platform_roles.schema.json"
_WORKSPACE_SCHEMA = _SQL_DIR / "0245_platform_role_workspace_facts.schema.json"
_LABELING_SCHEMA = _SQL_DIR / "0245_platform_role_labeling_facts.schema.json"
_CANONICAL_ROOT = _ROOT / "apps/identity/src/aap_identity"
if not (_CANONICAL_ROOT / "native_platform_role_protocol.schema.json").is_file():
    _CANONICAL_ROOT = Path(aap_identity.__file__).resolve().parent
_SUPPORT = _SQL_DIR / "0245_identity_platform_roles_support.sql"
_ARTIFACT = _SQL_DIR / "0245_identity_platform_roles.sql"
_MIGRATION = (
    Path(__file__).parents[1] / "alembic/versions/0245_identity_platform_roles.py"
)
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_ROLE_GRANTS = _ROOT / "infra/docker/identity-platform-roles-grants.sql"
_LAN_GRANTS = _ROOT / "infra/docker/lan-production-grants.sql"
_SECRET = "platform-role-native-test-signing-secret"


def _platform_role_dsn() -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity platform-role acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_PLATFORM_ROLE_URL")
    if not value:
        pytest.skip("Identity platform-role client DSN is not configured")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_platform_role_client"
    assert os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _auth_dsn(test_engine) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity platform-role JWT checks require the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_AUTH_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert (parsed.host, parsed.port, parsed.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _context(operation: str) -> dict[str, str]:
    return {
        "method": "GET" if operation == "preview_role_change" else "PATCH",
        "path": "/api/v1/users/role/preview"
        if operation == "preview_role_change"
        else "/api/v1/users/role",
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }


def _request(
    target_id: uuid.UUID,
    role: str,
    token: str,
    *,
    operation: str = "change_platform_role",
    operation_id: uuid.UUID | None = None,
):
    return decode_platform_role_request(
        {
            "schema": "aap-identity-platform-roles/v1",
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": token,
            "audit_context": _context(operation),
            "payload": {"target_user_id": str(target_id), "role": role},
        }
    )


def _token(actor_id: uuid.UUID, secret: str) -> str:
    return create_access_token(
        str(actor_id), "super_admin", secret=secret, expire_minutes=15
    )


class _CapturedAuthenticator:
    def __init__(self, verified: VerifiedCredential):
        self.verified = verified

    async def authenticate_bound(self, token: str) -> VerifiedCredential:
        return self.verified


async def _add_user(test_engine, role: str, *, email: str | None = None):
    user_id = uuid.uuid4()
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(
            User(
                id=user_id,
                email=email or f"platform-role-{user_id.hex}@example.test",
                name=f"Platform role {role}",
                password_hash="test-only-role-password-hash",
                credential_generation=0,
                role=role,
                is_active=True,
            )
        )
    return user_id


async def _cleanup_world(
    test_engine,
    fixture,
    *,
    user_ids=(),
    project_ids=(),
    batch_ids=(),
    task_ids=(),
    operation_ids=(),
):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await session.execute(
            text(
                "DELETE FROM public.identity_platform_role_operation_receipts "
                "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": list(operation_ids)},
        )
        await session.execute(
            text(
                "DELETE FROM public.audit_logs WHERE actor_id=:actor_id "
                "OR target_id = ANY(CAST(:target_ids AS text[]))"
            ),
            {
                "actor_id": fixture["actor_id"],
                "target_ids": [str(user_id) for user_id in user_ids],
            },
        )
        await session.execute(
            text(
                "DELETE FROM public.tasks WHERE id=ANY(CAST(:task_ids AS uuid[])) "
                "OR project_id=ANY(CAST(:project_ids AS uuid[]))"
            ),
            {"task_ids": list(task_ids), "project_ids": list(project_ids)},
        )
        await session.execute(
            text(
                "DELETE FROM public.task_batches WHERE id=ANY(CAST(:batch_ids AS uuid[])) "
                "OR project_id=ANY(CAST(:project_ids AS uuid[]))"
            ),
            {"batch_ids": list(batch_ids), "project_ids": list(project_ids)},
        )
        await session.execute(
            text(
                "DELETE FROM public.project_members "
                "WHERE project_id=ANY(CAST(:project_ids AS uuid[])) "
                "OR user_id=ANY(CAST(:user_ids AS uuid[]))"
            ),
            {"project_ids": list(project_ids), "user_ids": list(user_ids)},
        )
        await session.execute(
            text("DELETE FROM public.projects WHERE id=ANY(CAST(:ids AS uuid[]))"),
            {"ids": list(project_ids)},
        )
        await session.execute(delete(User).where(User.id.in_(list(user_ids))))
    await _cleanup(test_engine, fixture)


def test_frozen_identity_platform_role_artifacts_and_snapshot_order():
    native_schema = json.loads(_NATIVE_SCHEMA.read_text(encoding="utf-8"))
    workspace_schema = json.loads(_WORKSPACE_SCHEMA.read_text(encoding="utf-8"))
    labeling_schema = json.loads(_LABELING_SCHEMA.read_text(encoding="utf-8"))
    canonical_native = json.loads(
        (_CANONICAL_ROOT / "native_platform_role_protocol.schema.json").read_text(
            encoding="utf-8"
        )
    )
    canonical_workspace = json.loads(
        (_CANONICAL_ROOT / "platform_role_workspace_facts.schema.json").read_text(
            encoding="utf-8"
        )
    )
    canonical_labeling = json.loads(
        (_CANONICAL_ROOT / "platform_role_labeling_facts.schema.json").read_text(
            encoding="utf-8"
        )
    )
    support = _SUPPORT.read_text(encoding="utf-8")
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    role_grants = _ROLE_GRANTS.read_text(encoding="utf-8")
    lan_grants = _LAN_GRANTS.read_text(encoding="utf-8")

    assert "identity_platform_role_operation_receipts" in NATIVE_MANAGED_TABLES
    assert native_schema == canonical_native
    assert workspace_schema == canonical_workspace
    assert labeling_schema == canonical_labeling
    assert migration.index(
        '"0245_identity_platform_roles_support.sql"'
    ) < migration.index('"0245_identity_platform_roles.sql"')
    assert "FOREIGN KEY" not in support
    assert "identity_platform_role_facts(uuid)" in support
    assert "p_membership_project_ids uuid[]" in support
    assert "lock_identity_platform_role_owned_projects" in support
    assert "WITH ORDINALITY" in support
    assert "task_locks" not in support
    assert "ORDER BY project.name ASC, project.id" in support
    assert "FOR UPDATE OF project NOWAIT" in support
    assert "record_identity_platform_role_change" in support
    assert artifact.index("lock_super_admin_invariant()") < artifact.index(
        "FOR UPDATE OF account NOWAIT"
    )
    assert artifact.index("FOR UPDATE OF account NOWAIT") < artifact.index(
        "_lock_current_actor("
    )
    assert artifact.index("IF v_target_role = v_requested_role THEN") < artifact.index(
        "lock_identity_platform_role_owned_projects"
    )
    assert artifact.index("platform_role_change_blocked") < artifact.index(
        "aap_identity_v1.super_admin_status("
    )
    assert "aap_identity_platform_roles_v1.execute_platform_role_command(jsonb)" in (
        role_grants
    )
    expected_functions = role_grants.split("expected_functions text[] := ARRAY[", 1)[
        1
    ].split("];", 1)[0]
    assert expected_functions.index(
        "execute_platform_role_command"
    ) < expected_functions.index("platform_role_policy")
    assert "identity_platform_role_client" in role_grants
    assert "aap_identity_platform_roles_v1.execute_platform_role_command(jsonb)" in (
        lan_grants
    )
    assert "identity_platform_role_operation_receipts" in module_grants


@pytest.mark.asyncio
async def test_noop_receipt_replays_original_user_without_audit(test_engine):
    dsn = _platform_role_dsn()
    auth_dsn = _auth_dsn(test_engine)
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id = await _add_user(test_engine, "employee")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "platform-role-noop-native-secret"
    token = _token(fixture["actor_id"], secret)
    manager = PlatformRoleManager(
        Authenticator(PostgresData(auth_dsn), secret),
        PlatformRolePostgresData(dsn),
        secret,
    )
    operation_id = uuid.uuid4()
    request = _request(target_id, "employee", token, operation_id=operation_id)
    try:
        first = await manager.execute(request)
        assert first.data.user.role == "employee"
        async with maker() as session:
            receipt = await session.execute(
                text(
                    "SELECT result, audit_id FROM public.identity_platform_role_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            row = receipt.mappings().one()
            assert row["result"]["user"]["role"] == "employee"
            assert row["audit_id"] is None
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE action='user.role_change' "
                        "AND target_id=:target_id"
                    ),
                    {"target_id": str(target_id)},
                )
                == 0
            )
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET role='viewer' WHERE id=:id"),
                {"id": target_id},
            )

        replay = await manager.execute(request)
        assert replay.data.user.role == "employee"
        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT role FROM public.users WHERE id=:id"),
                    {"id": target_id},
                )
                == "viewer"
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE action='user.role_change' "
                        "AND target_id=:target_id"
                    ),
                    {"target_id": str(target_id)},
                )
                == 0
            )
    finally:
        await _cleanup_world(
            test_engine,
            fixture,
            user_ids=[target_id],
            operation_ids=[operation_id],
        )


@pytest.mark.asyncio
async def test_a_to_b_to_a_replay_returns_frozen_b_without_reapplying(test_engine):
    dsn = _platform_role_dsn()
    auth_dsn = _auth_dsn(test_engine)
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id = await _add_user(test_engine, "employee")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "platform-role-history-native-secret"
    token = _token(fixture["actor_id"], secret)
    manager = PlatformRoleManager(
        Authenticator(PostgresData(auth_dsn), secret),
        PlatformRolePostgresData(dsn),
        secret,
    )
    b_operation_id, a_operation_id = uuid.uuid4(), uuid.uuid4()
    b_request = _request(target_id, "viewer", token, operation_id=b_operation_id)
    a_request = _request(target_id, "employee", token, operation_id=a_operation_id)
    original_email = f"platform-role-{target_id.hex}@example.test"
    new_email = f"renamed-platform-role-{target_id.hex}@example.test"
    try:
        b_result = await manager.execute(b_request)
        assert b_result.data.user.role == "viewer"
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET email=:email WHERE id=:id"),
                {"email": new_email, "id": target_id},
            )
        a_result = await manager.execute(a_request)
        assert a_result.data.user.role == "employee"
        replay = await manager.execute(b_request)
        assert replay.data.user.role == "viewer"
        assert replay.data.user.email == original_email

        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT role FROM public.users WHERE id=:id"),
                    {"id": target_id},
                )
                == "employee"
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE action='user.role_change' "
                        "AND target_id=:target_id"
                    ),
                    {"target_id": str(target_id)},
                )
                == 2
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_platform_role_operation_receipts "
                        "WHERE target_user_id=:target_id"
                    ),
                    {"target_id": target_id},
                )
                == 2
            )
    finally:
        await _cleanup_world(
            test_engine,
            fixture,
            user_ids=[target_id],
            operation_ids=[b_operation_id, a_operation_id],
        )


@pytest.mark.asyncio
async def test_preview_single_snapshot_counts_and_mutation_blocker_order(test_engine):
    dsn = _platform_role_dsn()
    auth_dsn = _auth_dsn(test_engine)
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id = await _add_user(test_engine, "super_admin")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "platform-role-facts-native-secret"
    token = _token(fixture["actor_id"], secret)
    manager = PlatformRoleManager(
        Authenticator(PostgresData(auth_dsn), secret),
        PlatformRolePostgresData(dsn),
        secret,
    )
    project_id, batch_id = uuid.uuid4(), uuid.uuid4()
    task_ids = [uuid.uuid4() for _ in range(3)]
    project_display_id = f"pr-{project_id.hex[:17]}"
    batch_display_id = f"b-{batch_id.hex[:27]}"
    operation_ids = [uuid.uuid4(), uuid.uuid4()]
    now = datetime.now(UTC)
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET deactivation_requested_at=:requested, "
                    "deactivation_scheduled_at=:scheduled WHERE id=:actor_id"
                ),
                {
                    "requested": now,
                    "scheduled": now + timedelta(days=7),
                    "actor_id": fixture["actor_id"],
                },
            )
            session.add(
                Project(
                    id=project_id,
                    display_id=project_display_id,
                    name="Archived role facts project",
                    type_label="image",
                    type_key="detection",
                    owner_id=target_id,
                    status="archived",
                )
            )
            await session.flush()
            session.add(
                ProjectMember(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    user_id=target_id,
                    role="annotator",
                    assigned_by=fixture["actor_id"],
                )
            )
            session.add(
                TaskBatch(
                    id=batch_id,
                    project_id=project_id,
                    display_id=batch_display_id,
                    name="Role fact batch",
                    status="completed",
                    annotator_id=target_id,
                    reviewer_id=target_id,
                    created_by=fixture["actor_id"],
                )
            )
            await session.flush()
            session.add_all(
                [
                    Task(
                        id=task_ids[0],
                        project_id=project_id,
                        display_id=f"pr-task-{task_ids[0].hex[:20]}",
                        file_name="explicit-override.jpg",
                        file_path=f"/{task_ids[0].hex}.jpg",
                        status="pending",
                        batch_id=batch_id,
                        assignee_id=fixture["actor_id"],
                    ),
                    Task(
                        id=task_ids[1],
                        project_id=project_id,
                        display_id=f"pr-task-{task_ids[1].hex[:20]}",
                        file_name="unbatched-rejected.jpg",
                        file_path=f"/{task_ids[1].hex}.jpg",
                        status="rejected",
                        assignee_id=target_id,
                    ),
                    Task(
                        id=task_ids[2],
                        project_id=project_id,
                        display_id=f"pr-task-{task_ids[2].hex[:20]}",
                        file_name="completed-fallback.jpg",
                        file_path=f"/{task_ids[2].hex}.jpg",
                        status="completed",
                        batch_id=batch_id,
                        assignee_id=target_id,
                        reviewer_id=target_id,
                    ),
                ]
            )

        async with maker() as session:
            workspace_raw = await session.scalar(
                text(
                    "SELECT aap_workspace_v1.identity_platform_role_facts(:target_id)"
                ),
                {"target_id": target_id},
            )
            workspace = decode_workspace_platform_role_facts(
                workspace_raw, target_user_id=target_id
            )
            labeling_raw = await session.scalar(
                text(
                    "SELECT aap_labeling_v1.identity_platform_role_facts("
                    ":target_id, CAST(:project_ids AS uuid[]))"
                ),
                {
                    "target_id": target_id,
                    "project_ids": [
                        item.project_id for item in workspace.membership_projects
                    ],
                },
            )
            labeling = decode_labeling_platform_role_facts(
                labeling_raw, target_user_id=target_id
            )
            validate_platform_role_fact_coverage(workspace, labeling)
            assert workspace.owned_project_count == 1
            assert workspace.non_viewer_membership_count == 1
            assert labeling.unfinished_effective_work_count == 2
            assert len(labeling.membership_projects) == 1
            counts = labeling.membership_projects[0]
            assert counts.annotator_batch_count == 1
            assert counts.reviewer_batch_count == 1
            assert counts.assigned_task_count == 2
            assert counts.review_task_count == 1

        preview = await manager.execute(
            _request(
                target_id,
                "viewer",
                token,
                operation="preview_role_change",
                operation_id=operation_ids[0],
            )
        )
        assert preview.data.blockers == [
            "不能降级最后一名超级管理员",
            "账号仍持有标注/质检项目职责，请先在项目中变更职责",
            "账号仍是项目负责人，请先转移项目后变更平台角色",
            "账号仍有未完成的标注/质检工作，请先交接",
        ]
        assert preview.data.can_change is False
        assert preview.data.other_project_count == 0
        assert preview.data.assigned_batch_count == 2
        assert preview.data.assigned_task_count == 2
        assert preview.data.review_task_count == 1
        assert len(preview.data.projects) == 1
        project = preview.data.projects[0]
        assert project.project_id == project_id
        assert project.project_name == "Archived role facts project"
        assert project.membership_role == "annotator"

        with pytest.raises(PlatformRoleFailure) as raised:
            await manager.execute(
                _request(
                    target_id,
                    "viewer",
                    token,
                    operation_id=operation_ids[1],
                )
            )
        assert raised.value.code == "platform_role_change_blocked"
        assert raised.value.status_code == 409
        assert raised.value.detail == {
            "reason": "platform_role_change_blocked",
            "blockers": [
                "账号仍持有标注/质检项目职责，请先在项目中变更职责",
                "账号仍是项目负责人，请先转移项目后变更平台角色",
                "账号仍有未完成的标注/质检工作，请先交接",
            ],
        }
        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT role FROM public.users WHERE id=:target_id"),
                    {"target_id": target_id},
                )
                == "super_admin"
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE action='user.role_change' "
                        "AND target_id=:target_id"
                    ),
                    {"target_id": str(target_id)},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_platform_role_operation_receipts "
                        "WHERE target_user_id=:target_id"
                    ),
                    {"target_id": target_id},
                )
                == 0
            )
    finally:
        await _cleanup_world(
            test_engine,
            fixture,
            user_ids=[target_id],
            project_ids=[project_id],
            batch_ids=[batch_id],
            task_ids=task_ids,
            operation_ids=operation_ids,
        )


@pytest.mark.asyncio
async def test_last_unreserved_super_admin_blocks_then_allows_demotion(test_engine):
    dsn = _platform_role_dsn()
    auth_dsn = _auth_dsn(test_engine)
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id = await _add_user(test_engine, "super_admin")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "platform-role-last-super-admin-secret"
    token = _token(fixture["actor_id"], secret)
    manager = PlatformRoleManager(
        Authenticator(PostgresData(auth_dsn), secret),
        PlatformRolePostgresData(dsn),
        secret,
    )
    actor_time = datetime.now(UTC)
    blocked_operation_id = uuid.uuid4()
    allowed_operation_id = uuid.uuid4()
    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET deactivation_requested_at=:requested, "
                    "deactivation_scheduled_at=:scheduled WHERE id=:actor_id"
                ),
                {
                    "requested": actor_time,
                    "scheduled": actor_time + timedelta(days=7),
                    "actor_id": fixture["actor_id"],
                },
            )

        with pytest.raises(PlatformRoleFailure) as blocked:
            await manager.execute(
                _request(
                    target_id,
                    "employee",
                    token,
                    operation_id=blocked_operation_id,
                )
            )
        assert blocked.value.code == "last_super_admin"
        assert blocked.value.status_code == 400
        assert blocked.value.detail == "不能降级最后一名超级管理员"

        second_admin = await _add_user(test_engine, "super_admin")
        allowed = await manager.execute(
            _request(
                target_id,
                "employee",
                token,
                operation_id=allowed_operation_id,
            )
        )
        assert allowed.data.user.role == "employee"
        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT role FROM public.users WHERE id=:id"),
                    {"id": target_id},
                )
                == "employee"
            )
            assert (
                await session.scalar(
                    text("SELECT role FROM public.users WHERE id=:id"),
                    {"id": second_admin},
                )
                == "super_admin"
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE action='user.role_change' "
                        "AND target_id=:target_id"
                    ),
                    {"target_id": str(target_id)},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_platform_role_operation_receipts "
                        "WHERE target_user_id=:target_id"
                    ),
                    {"target_id": target_id},
                )
                == 1
            )
    finally:
        ids = [target_id]
        if "second_admin" in locals():
            ids.append(second_admin)
        await _cleanup_world(
            test_engine,
            fixture,
            user_ids=ids,
            operation_ids=[blocked_operation_id, allowed_operation_id],
        )


@pytest.mark.asyncio
async def test_stale_admitted_super_admin_cannot_change_role_after_demotion(
    test_engine,
):
    dsn = _platform_role_dsn()
    auth_dsn = _auth_dsn(test_engine)
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id = await _add_user(test_engine, "employee")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "platform-role-stale-manager-secret"
    token = _token(fixture["actor_id"], secret)
    authenticator = Authenticator(PostgresData(auth_dsn), secret)
    admitted = await authenticator.authenticate_bound(token)
    assert admitted.principal.role == "super_admin"
    generation = admitted.principal.generation
    request = _request(target_id, "viewer", token)
    manager = PlatformRoleManager(
        _CapturedAuthenticator(admitted), PlatformRolePostgresData(dsn), secret
    )
    try:
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.users SET role='employee' WHERE id=:id"),
                {"id": fixture["actor_id"]},
            )

        with pytest.raises(PlatformRoleFailure) as raised:
            await manager.execute(request)
        assert raised.value.code == "super_admin_required"
        assert raised.value.status_code == 403
        assert raised.value.detail == "当前账号已无管理权限"
        current = await authenticator.authenticate_bound(token)
        assert current.principal.role == "employee"
        assert current.principal.generation == generation

        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT role FROM public.users WHERE id=:id"),
                    {"id": target_id},
                )
                == "employee"
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_platform_role_operation_receipts "
                        "WHERE operation_id=:operation_id"
                    ),
                    {"operation_id": request.operation_id},
                )
                == 0
            )
    finally:
        await _cleanup_world(
            test_engine,
            fixture,
            user_ids=[target_id],
            operation_ids=[request.operation_id],
        )


@pytest.mark.asyncio
async def test_invalid_native_response_rolls_back_role_audit_and_receipt(test_engine):
    dsn = _platform_role_dsn()
    auth_dsn = _auth_dsn(test_engine)
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id = await _add_user(test_engine, "employee")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "platform-role-reply-rollback-secret"
    token = _token(fixture["actor_id"], secret)
    authenticator = Authenticator(PostgresData(auth_dsn), secret)
    request = _request(target_id, "viewer", token)

    class _CorruptReplyTransaction:
        def __init__(self, transaction):
            self.transaction = transaction

        async def execute_platform_role_command(self, payload):
            reply = await self.transaction.execute_platform_role_command(payload)
            reply["data"]["user"]["role"] = "employee"
            return reply

    class _CorruptingData:
        def __init__(self):
            self.inner = PlatformRolePostgresData(dsn)

        @asynccontextmanager
        async def transaction(self):
            async with self.inner.transaction() as transaction:
                yield _CorruptReplyTransaction(transaction)

    manager = PlatformRoleManager(authenticator, _CorruptingData(), secret)
    try:
        with pytest.raises(IdentityUnavailable):
            await manager.execute(request)
        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT role FROM public.users WHERE id=:id"),
                    {"id": target_id},
                )
                == "employee"
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE action='user.role_change' "
                        "AND target_id=:target_id"
                    ),
                    {"target_id": str(target_id)},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_platform_role_operation_receipts "
                        "WHERE operation_id=:operation_id"
                    ),
                    {"operation_id": request.operation_id},
                )
                == 0
            )
    finally:
        await _cleanup_world(
            test_engine,
            fixture,
            user_ids=[target_id],
            operation_ids=[request.operation_id],
        )


@pytest.mark.asyncio
async def test_project_owner_lock_contention_returns_project_busy(test_engine):
    dsn = _platform_role_dsn()
    auth_dsn = _auth_dsn(test_engine)
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    target_id = await _add_user(test_engine, "employee")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    project = Project(
        id=project_id,
        display_id=f"pr-{project_id.hex[:17]}",
        name="Contended owner project",
        type_label="image",
        type_key="detection",
        owner_id=target_id,
        status="archived",
    )
    async with maker.begin() as session:
        session.add(project)
    secret = "platform-role-project-busy-secret"
    token = _token(fixture["actor_id"], secret)
    manager = PlatformRoleManager(
        Authenticator(PostgresData(auth_dsn), secret),
        PlatformRolePostgresData(dsn),
        secret,
    )
    operation_id = uuid.uuid4()
    request = _request(target_id, "viewer", token, operation_id=operation_id)
    try:
        async with maker() as blocker:
            async with blocker.begin():
                await blocker.execute(
                    text("SELECT id FROM public.projects WHERE id=:id FOR UPDATE"),
                    {"id": project_id},
                )
                with pytest.raises(PlatformRoleFailure) as raised:
                    await manager.execute(request)
                assert raised.value.code == "project_busy"
                assert raised.value.status_code == 409
                assert raised.value.detail == {"reason": "project_busy"}
        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT role FROM public.users WHERE id=:id"),
                    {"id": target_id},
                )
                == "employee"
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_platform_role_operation_receipts "
                        "WHERE operation_id=:operation_id"
                    ),
                    {"operation_id": operation_id},
                )
                == 0
            )
    finally:
        await _cleanup_world(
            test_engine,
            fixture,
            user_ids=[target_id],
            project_ids=[project_id],
            operation_ids=[operation_id],
        )
