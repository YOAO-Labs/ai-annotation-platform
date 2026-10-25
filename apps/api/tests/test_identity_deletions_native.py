from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aap_identity
from aap_identity.authenticator import Authenticator
from aap_identity.deletion_contract import (
    DeletionFailure,
    decode_deletion_request,
    decode_native_deletion_reply,
    native_deletion_command,
)
from aap_identity.deletion_manager import (
    DeletionManager,
    deletion_derivation_id,
    deletion_request_fingerprint,
)
from aap_identity.deletion_postgres import DeletionPostgresData
from aap_identity.postgres import PostgresData
from app.config import settings
from app.db.models.api_key import ApiKey
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.task import Task
from app.db.models.task_lock import TaskLock
from app.db.native_schema import NATIVE_MANAGED_TABLES
from tests.test_identity_auth_native import _seed
from tests.test_identity_suspensions_native import (
    _add_batch,
    _add_member,
    _add_project,
    _add_user,
    _auth_dsn,
    _cleanup_world,
    _token,
)

_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_SCHEMA = _SQL_DIR / "0247_identity_deletions.schema.json"
_SUPPORT = _SQL_DIR / "0247_identity_deletions_support.sql"
_ARTIFACT = _SQL_DIR / "0247_identity_deletions.sql"
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0247_identity_deletions.py"
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_ROLE_GRANTS = _ROOT / "infra/docker/identity-deletions-grants.sql"
_LAN_GRANTS = _ROOT / "infra/docker/lan-production-grants.sql"
_CANONICAL_SCHEMA = (
    _ROOT / "apps/identity/src/aap_identity/native_deletion_protocol.schema.json"
)
if not _CANONICAL_SCHEMA.is_file():
    _CANONICAL_SCHEMA = (
        Path(aap_identity.__file__).resolve().parent
        / "native_deletion_protocol.schema.json"
    )


def _deletion_dsn(test_engine=None) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity deletion acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_DELETION_URL")
    if not value:
        pytest.skip("Identity deletion client DSN is not configured")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_deletion_client"
    assert os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    if test_engine is not None:
        assert (parsed.host, parsed.port, parsed.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _manager(test_engine, secret: str) -> DeletionManager:
    return DeletionManager(
        Authenticator(PostgresData(_auth_dsn(test_engine)), secret),
        DeletionPostgresData(_deletion_dsn(test_engine)),
        secret,
    )


class _CapturedDeletionAuthenticator:
    def __init__(self, verified):
        self.verified = verified

    async def authenticate_bound(self, _token: str):
        return self.verified


def _request(
    target_id: uuid.UUID,
    token: str,
    *,
    operation_id: uuid.UUID | None = None,
    receiver_id: uuid.UUID | None = None,
):
    return decode_deletion_request(
        {
            "schema": "aap-identity-deletions/v1",
            "operation": "delete_account",
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": token,
            "audit_context": {
                "method": "DELETE",
                "path": f"/api/v1/users/{target_id}",
                "ip": "127.0.0.1",
                "request_id": str(uuid.uuid4()),
            },
            "payload": {
                "target_user_id": str(target_id),
                "transfer_to_user_id": str(receiver_id) if receiver_id else None,
            },
        }
    )


async def _insert_task(
    test_engine,
    *,
    project_id: uuid.UUID,
    assignee_id: uuid.UUID | None,
    status: str,
    created_at: datetime,
    reviewer_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    lock_user_id: uuid.UUID | None = None,
    lock_expire_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    task_id = uuid.uuid4()
    lock_id = uuid.uuid4() if lock_user_id else None
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(
            Task(
                id=task_id,
                project_id=project_id,
                batch_id=batch_id,
                display_id=f"DEL-{task_id.hex[:10]}",
                file_name=f"delete-{task_id.hex}.jpg",
                file_path=f"delete/{task_id.hex}.jpg",
                status=status,
                assignee_id=assignee_id,
                reviewer_id=reviewer_id,
                created_at=created_at,
            )
        )
        await session.flush()
        if lock_id is not None:
            session.add(
                TaskLock(
                    id=lock_id,
                    task_id=task_id,
                    user_id=lock_user_id,
                    expire_at=lock_expire_at
                    or datetime.now(UTC) + timedelta(minutes=5),
                )
            )
            await session.flush()
    return task_id, lock_id


async def _insert_actor_history(
    test_engine, actor_id: uuid.UUID, *, redacted: bool = False
) -> int:
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        return await session.scalar(
            text(
                "INSERT INTO public.audit_logs ("
                "actor_id, actor_email, actor_role, action, target_type, target_id, "
                "method, path, status_code, ip, request_id, detail_json"
                ") VALUES ("
                ":actor_id, :actor_email, :actor_role, "
                "'annotation.update', 'task', 'historical-task', 'PATCH', "
                "'/api/v1/tasks/historical-task', 200, '127.0.0.1'::inet, "
                ":request_id, CAST(:detail AS jsonb)"
                ") RETURNING id"
            ),
            {
                "actor_id": actor_id,
                "actor_email": None if redacted else "deleted-user@example.test",
                "actor_role": None if redacted else "employee",
                "request_id": str(uuid.uuid4()),
                "detail": json.dumps({"preserve": "history"}),
            },
        )


async def _cleanup_deletion_world(
    test_engine,
    *,
    actor_ids=(),
    target_ids=(),
    operation_ids=(),
    project_ids=(),
    batch_ids=(),
    task_ids=(),
):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(
            text(
                "DELETE FROM public.identity_deletion_operation_receipts "
                "WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": list(operation_ids)},
        )
    await _cleanup_world(
        test_engine,
        actor_ids=actor_ids,
        target_ids=target_ids,
        operation_ids=operation_ids,
        project_ids=project_ids,
        batch_ids=batch_ids,
        task_ids=task_ids,
    )


def test_frozen_identity_deletion_artifacts_and_native_owner_boundary():
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

    assert "identity_deletion_operation_receipts" in NATIVE_MANAGED_TABLES
    assert schema == canonical
    assert _SCHEMA.read_bytes() == _CANONICAL_SCHEMA.read_bytes()
    assert schema["$id"] == "aap-identity-deletions-native/v1"
    assert migration.index('"0247_identity_deletions_support.sql"') < migration.index(
        '"0247_identity_deletions.sql"'
    )
    assert "FOREIGN KEY" not in support
    assert "target_generation bigint NOT NULL" in support
    assert "target_is_active boolean NOT NULL" in support
    assert "lock_identity_deletion_scope(uuid,uuid)" in migration
    assert "identity_deletion_transfer_covered(uuid,uuid[])" in migration
    assert "identity_deletion_work(uuid)" in migration
    assert "transfer_identity_deletion_work(uuid,uuid)" in migration
    assert "record_identity_deletion_event(jsonb,uuid,jsonb,jsonb)" in migration
    assert "RETURNS TABLE(has_projects boolean, all_projects_owned boolean)" in support
    assert "pending_task_count bigint" in support
    assert "locked_task_count bigint" in support
    assert "sample_task_ids uuid[]" in support
    assert "pending_project_ids uuid[]" in support
    assert "task.assignee_id = p_target_user_id" in support
    assert "task.status IN ('pending', 'in_progress', 'review', 'rejected')" in support
    assert "task_lock.user_id = p_target_user_id" in support
    assert "SET assignee_id = p_to_user_id" in support
    assert "assigned_at = CURRENT_TIMESTAMP" in support
    assert "updated_at = CURRENT_TIMESTAMP" in support
    assert "retire_account_credentials" not in artifact
    assert "disabled_reason = '管理员删除账号'" in artifact
    assert (
        "IF v_transferred_count IS NULL OR v_transferred_count < 0 THEN\n"
        "            RAISE EXCEPTION"
    ) in artifact
    assert "IF v_audit_id IS NULL THEN\n        RAISE EXCEPTION" in artifact
    assert "operation_conflict" in artifact
    assert artifact.index(
        "receipt.target_generation IS DISTINCT FROM v_target_generation"
    ) < artifact.index("IF NOT v_target_active THEN")
    assert "identity_deletion_operation_receipts" in module_grants
    assert "aap_workspace_v1.identity_deletion_transfer_covered" in module_grants
    assert "aap_labeling_v1.transfer_identity_deletion_work" in module_grants
    assert (
        'GRANT UPDATE (assignee_id, assigned_at) ON public.tasks TO :"labeling_owner"'
        in module_grants
    )
    assert "aap_audit_v1.record_identity_deletion_event" in module_grants
    audit_helper = support.split(
        "CREATE FUNCTION aap_audit_v1.record_identity_deletion_event(", 1
    )[1]
    assert audit_helper.index("UPDATE public.audit_logs AS audit") < audit_helper.index(
        "INSERT INTO public.audit_logs"
    )
    assert "IF v_actor_id = p_target_user_id THEN" in audit_helper
    assert "SET detail_json" not in audit_helper
    assert "'redacted_audit_rows', v_redacted_rows" in audit_helper
    assert "audit_oid, 'public.audit_logs', 'detail_json', 'UPDATE'" in role_grants
    assert "labeling_oid, 'public.tasks', 'assignee_id', 'UPDATE'" in role_grants
    assert "labeling_oid, 'public.tasks', 'assigned_at', 'UPDATE'" in role_grants
    assert "labeling_oid, 'public.tasks', 'updated_at', 'UPDATE'" in role_grants
    assert "GRANT UPDATE (detail_json)" not in module_grants
    assert "identity_deletion_role=identity_deletion_client" in role_grants
    expected_functions = role_grants.split("expected_functions text[] := ARRAY[", 1)[
        1
    ].split("];", 1)[0]
    assert expected_functions.index("deletion_policy()") < expected_functions.index(
        "execute_deletion_command"
    )
    assert "aap_identity_deletions_v1.deletion_policy()" in lan_grants
    assert "aap_identity_deletions_v1.execute_deletion_command(jsonb)" in lan_grants
    assert '"DELETION": "identity_deletion_client"' in verifier


@pytest.mark.asyncio
async def test_pending_work_summary_transfer_redaction_and_credentials_are_legacy_exact(
    test_engine,
):
    actor_fixture = await _seed(test_engine, role="super_admin", create_key=False)
    actor_id = actor_fixture["actor_id"]
    target_id, target_email = await _add_user(test_engine, "employee")
    receiver_id, _ = await _add_user(test_engine, "employee")
    project_id = await _add_project(test_engine, actor_id, "Deletion task project")
    batch_id = await _add_batch(test_engine, project_id, target_id, actor_id)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "identity-deletion-transfer-test-secret"
    token = _token(actor_id, "super_admin", secret)
    manager = _manager(test_engine, secret)
    blocker_operation_id = uuid.uuid4()
    delete_operation_id = uuid.uuid4()
    blocker_request = _request(target_id, token, operation_id=blocker_operation_id)
    delete_request = _request(
        target_id,
        token,
        operation_id=delete_operation_id,
        receiver_id=receiver_id,
    )
    key_id = uuid.uuid4()
    reset_id = uuid.uuid4()
    now = datetime.now(UTC)
    requested_at = now - timedelta(minutes=3)
    scheduled_at = now + timedelta(days=6)
    task_ids: list[uuid.UUID] = []
    lock_ids: list[uuid.UUID] = []
    operation_ids = [blocker_operation_id, delete_operation_id]
    history_ids = []

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
            session.add(
                ApiKey(
                    id=key_id,
                    user_id=target_id,
                    name="Deletion preserve-key",
                    key_prefix=f"ak_{key_id.hex[:9]}",
                    key_hash=f"delete-preserve-key-{key_id.hex}",
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
            await session.flush()

        history_ids = [
            await _insert_actor_history(test_engine, target_id),
            await _insert_actor_history(test_engine, target_id, redacted=True),
        ]

        eligible_statuses = (
            "pending",
            "in_progress",
            "review",
            "rejected",
            "pending",
            "in_progress",
        )
        eligible_task_ids: list[uuid.UUID] = []
        for index, status in enumerate(eligible_statuses):
            task_id, lock_id = await _insert_task(
                test_engine,
                project_id=project_id,
                assignee_id=target_id,
                status=status,
                created_at=now - timedelta(minutes=index + 1),
                lock_user_id=target_id,
                lock_expire_at=(
                    now - timedelta(minutes=1)
                    if index == 0
                    else now + timedelta(minutes=5)
                ),
            )
            eligible_task_ids.append(task_id)
            task_ids.append(task_id)
            assert lock_id is not None
            lock_ids.append(lock_id)

        completed_task_id, completed_lock_id = await _insert_task(
            test_engine,
            project_id=project_id,
            assignee_id=target_id,
            status="completed",
            created_at=now - timedelta(minutes=20),
            lock_user_id=target_id,
            lock_expire_at=now - timedelta(minutes=2),
        )
        task_ids.append(completed_task_id)
        assert completed_lock_id is not None
        lock_ids.append(completed_lock_id)

        reviewer_task_id, _ = await _insert_task(
            test_engine,
            project_id=project_id,
            assignee_id=None,
            reviewer_id=target_id,
            status="review",
            created_at=now - timedelta(minutes=30),
        )
        task_ids.append(reviewer_task_id)
        batch_task_id, _ = await _insert_task(
            test_engine,
            project_id=project_id,
            assignee_id=None,
            status="pending",
            created_at=now - timedelta(minutes=40),
            batch_id=batch_id,
        )
        task_ids.append(batch_task_id)

        with pytest.raises(DeletionFailure) as blocked:
            await manager.execute(blocker_request)
        async with maker() as session:
            expected_samples = list(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM public.tasks "
                            "WHERE id=ANY(CAST(:ids AS uuid[])) "
                            "ORDER BY created_at DESC LIMIT 5"
                        ),
                        {"ids": eligible_task_ids},
                    )
                )
            )
            expected_sample_ids = [str(row[0]) for row in expected_samples]
        assert blocked.value.code == "has_pending_tasks"
        assert blocked.value.status_code == 409
        assert blocked.value.detail == {
            "reason": "has_pending_tasks",
            "pending_task_count": 6,
            "locked_task_count": 7,
            "sample_task_ids": expected_sample_ids,
            "message": "该用户当前有 6 个未完成任务、7 个锁定任务，需先转交",
        }
        all_task_ids = [
            *eligible_task_ids,
            completed_task_id,
            reviewer_task_id,
            batch_task_id,
        ]
        async with maker() as session:
            baseline_updated_at = {
                row.id: row.updated_at
                for row in (
                    await session.execute(
                        text(
                            "SELECT id, updated_at FROM public.tasks "
                            "WHERE id=ANY(CAST(:ids AS uuid[]))"
                        ),
                        {"ids": all_task_ids},
                    )
                ).all()
            }

        deleted = await manager.execute(delete_request)
        assert deleted.data.user.id == target_id
        assert deleted.data.user.is_active is False
        assert deleted.data.user.status == "offline"
        assert deleted.data.user.disabled_kind == "deleted"
        assert deleted.data.user.disabled_reason == "管理员删除账号"

        replay = await manager.execute(delete_request)
        assert replay.data == deleted.data

        async with maker() as session:
            target = (
                (
                    await session.execute(
                        text(
                            "SELECT credential_generation, is_active, disabled_kind, "
                            "disabled_reason, deactivation_requested_at, "
                            "deactivation_scheduled_at, deactivation_reason "
                            "FROM public.users WHERE id=:id"
                        ),
                        {"id": target_id},
                    )
                )
                .mappings()
                .one()
            )
            assert target == {
                "credential_generation": 0,
                "is_active": False,
                "disabled_kind": "deleted",
                "disabled_reason": "管理员删除账号",
                "deactivation_requested_at": requested_at,
                "deactivation_scheduled_at": scheduled_at,
                "deactivation_reason": "Accepted self-deactivation",
            }
            assert (
                await session.scalar(
                    text("SELECT revoked_at FROM public.api_keys WHERE id=:id"),
                    {"id": key_id},
                )
                is None
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT used_at FROM public.password_reset_tokens WHERE id=:id"
                    ),
                    {"id": reset_id},
                )
                is None
            )
            transferred = (
                await session.execute(
                    text(
                        "SELECT id, assignee_id, assigned_at, updated_at "
                        "FROM public.tasks WHERE id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": eligible_task_ids},
                )
            ).all()
            assert len(transferred) == len(eligible_task_ids)
            assert all(
                row.assignee_id == receiver_id
                and row.assigned_at
                and row.updated_at > baseline_updated_at[row.id]
                for row in transferred
            )
            untouched = (
                await session.execute(
                    text(
                        "SELECT id, assignee_id, reviewer_id, updated_at "
                        "FROM public.tasks WHERE id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [completed_task_id, reviewer_task_id, batch_task_id]},
                )
            ).all()
            by_id = {row.id: row for row in untouched}
            assert by_id[completed_task_id].assignee_id == target_id
            assert (
                by_id[completed_task_id].updated_at
                == baseline_updated_at[completed_task_id]
            )
            assert by_id[reviewer_task_id].assignee_id is None
            assert by_id[reviewer_task_id].reviewer_id == target_id
            assert (
                by_id[reviewer_task_id].updated_at
                == baseline_updated_at[reviewer_task_id]
            )
            assert by_id[batch_task_id].assignee_id is None
            assert by_id[batch_task_id].updated_at == baseline_updated_at[batch_task_id]
            assert (
                await session.scalar(
                    text("SELECT annotator_id FROM public.task_batches WHERE id=:id"),
                    {"id": batch_id},
                )
                == target_id
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM public.task_locks WHERE user_id=:id"),
                    {"id": target_id},
                )
                == 0
            )
            prior_audits = (
                (
                    await session.execute(
                        text(
                            "SELECT actor_id, actor_email, actor_role, detail_json "
                            "FROM public.audit_logs WHERE id=ANY(CAST(:ids AS bigint[]))"
                        ),
                        {"ids": history_ids},
                    )
                )
                .mappings()
                .all()
            )
            assert len(prior_audits) == 2
            assert all(
                row["actor_id"] == target_id
                and row["actor_email"] is None
                and row["actor_role"] is None
                and row["detail_json"] == {"preserve": "history"}
                for row in prior_audits
            )
            deletion_audit = (
                (
                    await session.execute(
                        text(
                            "SELECT status_code, detail_json FROM public.audit_logs "
                            "WHERE actor_id=:actor AND target_id=:target "
                            "AND action='user.delete'"
                        ),
                        {"actor": actor_id, "target": str(target_id)},
                    )
                )
                .mappings()
                .one()
            )
            assert deletion_audit["status_code"] == 200
            assert deletion_audit["detail_json"] == {
                "email": target_email,
                "role": "employee",
                "transferred_to": str(receiver_id),
                "transferred_count": 6,
                "released_locks": 7,
                "redacted_audit_rows": 2,
            }
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor "
                        "AND target_id=:target AND action='user.delete'"
                    ),
                    {"actor": actor_id, "target": str(target_id)},
                )
                == 1
            )
            receipts = (
                (
                    await session.execute(
                        text(
                            "SELECT target_generation, target_is_active, transfer_to_user_id, "
                            "result, audit_id FROM public.identity_deletion_operation_receipts "
                            "WHERE operation_id=:id"
                        ),
                        {"id": delete_operation_id},
                    )
                )
                .mappings()
                .one()
            )
            assert receipts["target_generation"] == 0
            assert receipts["target_is_active"] is False
            assert receipts["transfer_to_user_id"] == receiver_id
            assert receipts["result"]["user"]["is_active"] is False
            assert receipts["audit_id"] is not None
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_deletion_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": blocker_operation_id},
                )
                == 0
            )
    finally:
        await _cleanup_deletion_world(
            test_engine,
            actor_ids=[actor_id, target_id, receiver_id],
            target_ids=[target_id],
            operation_ids=operation_ids,
            project_ids=[project_id],
            batch_ids=[batch_id],
            task_ids=task_ids,
        )


@pytest.mark.asyncio
async def test_inactive_delete_noop_ignores_receiver_then_stale_retry_conflicts(
    test_engine,
):
    actor_fixture = await _seed(test_engine, role="project_admin", create_key=False)
    actor_id = actor_fixture["actor_id"]
    target_id, _ = await _add_user(test_engine, "viewer", active=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "identity-deletion-inactive-noop-secret"
    manager = _manager(test_engine, secret)
    operation_id = uuid.uuid4()
    request = _request(
        target_id,
        _token(actor_id, "project_admin", secret),
        operation_id=operation_id,
        receiver_id=uuid.uuid4(),
    )

    try:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET disabled_kind='deleted', "
                    "disabled_reason='Preserve inactive history' WHERE id=:id"
                ),
                {"id": target_id},
            )
        first = await manager.execute(request)
        assert first.data.user.is_active is False
        assert first.data.user.role == "viewer"
        assert first.data.user.disabled_kind == "deleted"
        assert first.data.user.disabled_reason == "Preserve inactive history"
        assert (await manager.execute(request)).data == first.data

        async with maker.begin() as session:
            await session.execute(
                text(
                    "UPDATE public.users SET is_active=true, status='online', "
                    "disabled_kind=NULL, disabled_at=NULL, disabled_by=NULL, "
                    "disabled_reason=NULL WHERE id=:id"
                ),
                {"id": target_id},
            )
        with pytest.raises(DeletionFailure) as stale:
            await manager.execute(request)
        assert stale.value.code == "operation_conflict"
        assert stale.value.status_code == 409

        async with maker() as session:
            current = (
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
            assert current == {
                "is_active": True,
                "credential_generation": 0,
                "disabled_kind": None,
            }
            receipt = (
                (
                    await session.execute(
                        text(
                            "SELECT result, audit_id FROM "
                            "public.identity_deletion_operation_receipts "
                            "WHERE operation_id=:id"
                        ),
                        {"id": operation_id},
                    )
                )
                .mappings()
                .one()
            )
            assert receipt["result"]["user"]["is_active"] is False
            assert receipt["audit_id"] is None
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE target_id=:id "
                        "AND action='user.delete'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        await _cleanup_deletion_world(
            test_engine,
            actor_ids=[actor_id, target_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
        )


@pytest.mark.asyncio
async def test_pa_delete_scope_and_receiver_membership_are_legacy_exact(test_engine):
    pa_fixture = await _seed(test_engine, role="project_admin", create_key=False)
    owner_fixture = await _seed(test_engine, role="super_admin", create_key=False)
    pa_id = pa_fixture["actor_id"]
    owner_id = owner_fixture["actor_id"]
    cross_target_id, _ = await _add_user(test_engine, "employee")
    target_id, _ = await _add_user(test_engine, "employee")
    receiver_id, _ = await _add_user(test_engine, "employee")
    owned_project_id = await _add_project(test_engine, pa_id, "PA deletion project")
    foreign_project_id = await _add_project(
        test_engine, owner_id, "Foreign deletion project"
    )
    now = datetime.now(UTC)
    cross_task, _ = await _insert_task(
        test_engine,
        project_id=foreign_project_id,
        assignee_id=cross_target_id,
        status="pending",
        created_at=now,
    )
    pending_task, _ = await _insert_task(
        test_engine,
        project_id=owned_project_id,
        assignee_id=target_id,
        status="pending",
        created_at=now,
    )
    member_id = None
    secret = "identity-deletion-pa-scope-secret"
    manager = _manager(test_engine, secret)
    token = _token(pa_id, "project_admin", secret)
    cross_operation_id = uuid.uuid4()
    blocked_receiver_operation_id = uuid.uuid4()
    accepted_operation_id = uuid.uuid4()
    cross_request = _request(cross_target_id, token, operation_id=cross_operation_id)
    blocked_receiver_request = _request(
        target_id,
        token,
        operation_id=blocked_receiver_operation_id,
        receiver_id=receiver_id,
    )
    accepted_request = _request(
        target_id,
        token,
        operation_id=accepted_operation_id,
        receiver_id=receiver_id,
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_ids = [
        cross_operation_id,
        blocked_receiver_operation_id,
        accepted_operation_id,
    ]
    try:
        with pytest.raises(DeletionFailure) as cross_scope:
            await manager.execute(cross_request)
        assert cross_scope.value.code == "delete_scope_forbidden"
        assert cross_scope.value.status_code == 403
        assert cross_scope.value.detail == "该用户跨多个项目，须由超级管理员处理"

        with pytest.raises(DeletionFailure) as receiver_scope:
            await manager.execute(blocked_receiver_request)
        assert receiver_scope.value.code == "receiver_scope_forbidden"
        assert receiver_scope.value.status_code == 403

        member_id = await _add_member(test_engine, owned_project_id, receiver_id, pa_id)
        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.project_members SET role='viewer' WHERE id=:id"),
                {"id": member_id},
            )
        accepted = await manager.execute(accepted_request)
        assert accepted.data.user.is_active is False
        assert accepted.data.user.disabled_kind == "deleted"

        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT assignee_id FROM public.tasks WHERE id=:id"),
                    {"id": pending_task},
                )
                == receiver_id
            )
            assert (
                await session.scalar(
                    text("SELECT is_active FROM public.users WHERE id=:id"),
                    {"id": cross_target_id},
                )
                is True
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_deletion_operation_receipts "
                        "WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
                    ),
                    {"ids": [cross_operation_id, blocked_receiver_operation_id]},
                )
                == 0
            )
    finally:
        await _cleanup_deletion_world(
            test_engine,
            actor_ids=[pa_id, owner_id, cross_target_id, target_id, receiver_id],
            target_ids=[cross_target_id, target_id],
            operation_ids=operation_ids,
            project_ids=[owned_project_id, foreign_project_id],
            task_ids=[cross_task, pending_task],
        )


@pytest.mark.asyncio
async def test_stale_super_admin_snapshot_maps_fresh_project_admin_scope_failure(
    test_engine,
):
    actor_fixture = await _seed(test_engine, role="super_admin", create_key=False)
    owner_fixture = await _seed(test_engine, role="super_admin", create_key=False)
    actor_id = actor_fixture["actor_id"]
    owner_id = owner_fixture["actor_id"]
    target_id, _ = await _add_user(test_engine, "employee")
    foreign_project_id = await _add_project(
        test_engine, owner_id, "Stale role deletion scope"
    )
    await _add_member(test_engine, foreign_project_id, target_id, owner_id)

    secret = "identity-deletion-stale-role-scope-secret"
    token = _token(actor_id, "super_admin", secret)
    authenticated = Authenticator(PostgresData(_auth_dsn(test_engine)), secret)
    accepted = await authenticated.authenticate_bound(token)
    assert accepted.principal.role == "super_admin"
    generation = accepted.principal.generation

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(
            text("UPDATE public.users SET role='project_admin' WHERE id=:id"),
            {"id": actor_id},
        )
        current = (
            (
                await session.execute(
                    text(
                        "SELECT role, credential_generation FROM public.users WHERE id=:id"
                    ),
                    {"id": actor_id},
                )
            )
            .mappings()
            .one()
        )
        assert current == {
            "role": "project_admin",
            "credential_generation": generation,
        }

    operation_id = uuid.uuid4()
    request = _request(target_id, token, operation_id=operation_id)
    manager = DeletionManager(
        _CapturedDeletionAuthenticator(accepted),
        DeletionPostgresData(_deletion_dsn(test_engine)),
        secret,
    )
    try:
        with pytest.raises(DeletionFailure) as denied:
            await manager.execute(request)
        assert denied.value.code == "delete_scope_forbidden"
        assert denied.value.status_code == 403
        assert denied.value.detail == "该用户跨多个项目，须由超级管理员处理"

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
                        "SELECT count(*) FROM public.identity_deletion_operation_receipts "
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
                        "AND action='user.delete'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        await _cleanup_deletion_world(
            test_engine,
            actor_ids=[actor_id, owner_id, target_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
            project_ids=[foreign_project_id],
        )


@pytest.mark.asyncio
async def test_delete_last_unreserved_super_admin_is_rejected(test_engine):
    actor_fixture = await _seed(test_engine, role="super_admin", create_key=False)
    actor_id = actor_fixture["actor_id"]
    target_id, _ = await _add_user(test_engine, "super_admin")
    now = datetime.now(UTC)
    scheduled_at = now + timedelta(days=6)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(
            text(
                "UPDATE public.users SET deactivation_requested_at=:requested, "
                "deactivation_scheduled_at=:scheduled, deactivation_reason='reserve actor' "
                "WHERE id=:id"
            ),
            {
                "requested": now - timedelta(minutes=1),
                "scheduled": scheduled_at,
                "id": actor_id,
            },
        )

    secret = "identity-deletion-last-super-admin-secret"
    operation_id = uuid.uuid4()
    manager = _manager(test_engine, secret)
    request = _request(
        target_id,
        _token(actor_id, "super_admin", secret),
        operation_id=operation_id,
    )
    try:
        with pytest.raises(DeletionFailure) as last_admin:
            await manager.execute(request)
        assert last_admin.value.code == "last_super_admin"
        assert last_admin.value.status_code == 400
        assert last_admin.value.detail == "不能删除最后一名超级管理员"
        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT is_active FROM public.users WHERE id=:id"),
                    {"id": target_id},
                )
                is True
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_deletion_operation_receipts "
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
                        "AND action='user.delete'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        await _cleanup_deletion_world(
            test_engine,
            actor_ids=[actor_id, target_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
        )


@pytest.mark.asyncio
async def test_malformed_deletion_audit_reply_rolls_back_transfer_and_soft_delete(
    test_engine,
):
    actor_fixture = await _seed(test_engine, role="super_admin", create_key=False)
    actor_id = actor_fixture["actor_id"]
    target_id, _ = await _add_user(test_engine, "employee")
    receiver_id, _ = await _add_user(test_engine, "employee")
    project_id = await _add_project(test_engine, actor_id, "Deletion rollback project")
    task_id, lock_id = await _insert_task(
        test_engine,
        project_id=project_id,
        assignee_id=target_id,
        status="pending",
        created_at=datetime.now(UTC),
        lock_user_id=target_id,
    )
    secret = "identity-deletion-rollback-secret"
    operation_id = uuid.uuid4()
    manager = _manager(test_engine, secret)
    request = _request(
        target_id,
        _token(actor_id, "super_admin", secret),
        operation_id=operation_id,
        receiver_id=receiver_id,
    )
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    owner_engine = create_async_engine(settings.effective_migration_database_url)
    function = "aap_audit_v1.record_identity_deletion_event"
    original_name = f"_identity_del_audit_{uuid.uuid4().hex}"
    original = f"aap_audit_v1.{original_name}"
    wrapped = False
    override = f"""
        CREATE FUNCTION {function}(
            p_actor jsonb, p_target_user_id uuid, p_detail jsonb, p_context jsonb
        ) RETURNS bigint
        LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $function$ SELECT NULL::bigint $function$
    """
    try:
        async with owner_engine.begin() as owner:
            assert await owner.scalar(text("SELECT current_database()")) == (
                test_engine.url.database
            )
            await owner.exec_driver_sql(
                f"ALTER FUNCTION {function}(jsonb, uuid, jsonb, jsonb) "
                f"RENAME TO {original_name}"
            )
            await owner.exec_driver_sql(override)
            await owner.exec_driver_sql(
                f"REVOKE ALL ON FUNCTION {function}(jsonb, uuid, jsonb, jsonb) "
                "FROM PUBLIC"
            )
            await owner.exec_driver_sql(
                f"ALTER FUNCTION {function}(jsonb, uuid, jsonb, jsonb) OWNER TO audit_code"
            )
            await owner.exec_driver_sql(
                f"GRANT EXECUTE ON FUNCTION {function}(jsonb, uuid, jsonb, jsonb) "
                "TO identity_code"
            )
        wrapped = True

        verified = await manager.auth.authenticate_bound(request.token)
        native_command = native_deletion_command(
            request,
            actor=verified.principal,
            expected_key_hash=verified.key_hash,
            request_fingerprint=deletion_request_fingerprint(secret, request),
            derivation_id=deletion_derivation_id(secret),
        )
        async with manager.data.transaction() as native_transaction:
            direct_reply = await native_transaction.execute_deletion_command(
                native_command.model_dump(mode="json", by_alias=True)
            )
        with pytest.raises(DeletionFailure) as direct_failure:
            decode_native_deletion_reply(native_command, direct_reply)
        assert direct_failure.value.code == "unavailable"
        assert direct_failure.value.status_code == 503
        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT is_active FROM public.users WHERE id=:id"),
                    {"id": target_id},
                )
            ) is True
            assert (
                await session.scalar(
                    text("SELECT assignee_id FROM public.tasks WHERE id=:id"),
                    {"id": task_id},
                )
            ) == target_id
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM public.task_locks WHERE id=:id"),
                    {"id": lock_id},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_deletion_operation_receipts "
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
                        "AND action='user.delete'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )

        with pytest.raises(DeletionFailure) as malformed:
            await manager.execute(request)
        assert malformed.value.code == "unavailable"
        assert malformed.value.status_code == 503

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
                    text("SELECT assignee_id FROM public.tasks WHERE id=:id"),
                    {"id": task_id},
                )
                == target_id
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM public.task_locks WHERE id=:id"),
                    {"id": lock_id},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_deletion_operation_receipts "
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
                        "AND action='user.delete'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        try:
            if wrapped:
                async with owner_engine.begin() as owner:
                    await owner.exec_driver_sql(
                        f"DROP FUNCTION {function}(jsonb, uuid, jsonb, jsonb)"
                    )
                    await owner.exec_driver_sql(
                        f"ALTER FUNCTION {original}(jsonb, uuid, jsonb, jsonb) "
                        "RENAME TO record_identity_deletion_event"
                    )
        finally:
            await owner_engine.dispose()
        await _cleanup_deletion_world(
            test_engine,
            actor_ids=[actor_id, target_id, receiver_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
            project_ids=[project_id],
            task_ids=[task_id],
        )
