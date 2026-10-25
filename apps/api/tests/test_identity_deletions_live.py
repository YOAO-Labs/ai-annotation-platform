"""Installed Identity owner qualification for account deletion."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import math
import os
import secrets
import statistics
import subprocess
import time
import uuid

import pytest
from sqlalchemy import delete, select, text

from aap_identity.directory_contract import DirectoryUserRecord
from aap_identity.recovery_crypto import token_hash
from aap_identity.security import create_access_token, decode_access_token
from app.db.models.api_key import ApiKey
from app.db.models.audit_log import AuditLog
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from tests.factory import (
    create_api_key,
    create_batch,
    create_membership,
    create_project,
    create_task,
    create_user,
)
from tests.identity_recovery_helpers import seed_challenge
from tests.test_identity_admission_live import (
    ROOT,
    _start_identity,
    _token,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_DELETION_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity deletion verifier",
    ),
]


def _delete_request(token, target_id, receiver_id=None, *, operation_id=None):
    operation_id = operation_id or uuid.uuid4()
    request_id = uuid.uuid4()
    return {
        "schema": "aap-identity-deletions/v1",
        "operation": "delete_account",
        "operation_id": str(operation_id),
        "token": token,
        "audit_context": {
            "method": "DELETE",
            "path": f"/api/v1/users/{target_id}",
            "ip": "127.0.0.1",
            "request_id": str(request_id),
        },
        "payload": {
            "target_user_id": str(target_id),
            "transfer_to_user_id": str(receiver_id) if receiver_id else None,
        },
    }


def _deactivation_request(token, user_id, reason, *, operation_id=None):
    return {
        "schema": "aap-identity-deactivation/v1",
        "operation": "request_self_deactivation",
        "operation_id": str(operation_id or uuid.uuid4()),
        "token": token,
        "audit_context": {
            "method": "POST",
            "path": "/acceptance/identity-deletion-reservation",
            "ip": "127.0.0.1",
            "request_id": str(uuid.uuid4()),
        },
        "payload": {"reason": reason},
    }


def _node_response(identity, control, request, *, discard=False, timeout=120):
    peer = ROOT / "apps/api/tests/fixtures/identity_deletions_peer.mjs"
    result = subprocess.run(
        ["node", str(peer)],
        input=json.dumps(
            {
                "mode": "discard" if discard else "call",
                "url": identity.origin,
                "control_token": control,
                "request": request,
            },
            ensure_ascii=False,
            allow_nan=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    assert result.returncode == 0, "Independent Node deletion consumer failed"
    assert control not in result.stderr and request["token"] not in result.stderr
    return json.loads(result.stdout), result.stdout


def _node(identity, control, request, *, discard=False, timeout=120):
    return _node_response(identity, control, request, discard=discard, timeout=timeout)[
        0
    ]


def _deactivation_node(identity, control, request):
    peer = ROOT / "apps/api/tests/fixtures/identity_deactivation_peer.mjs"
    result = subprocess.run(
        ["node", str(peer)],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": request},
            ensure_ascii=False,
            allow_nan=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, "Independent Node deactivation consumer failed"
    assert control not in result.stdout and request["token"] not in result.stdout
    return json.loads(result.stdout)


async def _cleanup_deletions(
    maker,
    *,
    user_ids,
    project_ids=(),
    operation_ids=(),
    deactivation_operation_ids=(),
    jtis=(),
):
    users = [uuid.UUID(str(value)) for value in user_ids]
    projects = [uuid.UUID(str(value)) for value in project_ids]
    operations = [uuid.UUID(str(value)) for value in operation_ids]
    deactivation_operations = [
        uuid.UUID(str(value)) for value in deactivation_operation_ids
    ]
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await db.execute(
            text(
                "DELETE FROM public.identity_deletion_operation_receipts "
                "WHERE actor_id=ANY(CAST(:users AS uuid[])) "
                "OR target_user_id=ANY(CAST(:users AS uuid[])) "
                "OR transfer_to_user_id=ANY(CAST(:users AS uuid[])) "
                "OR operation_id=ANY(CAST(:operations AS uuid[]))"
            ),
            {"users": users, "operations": operations},
        )
        await db.execute(
            text(
                "DELETE FROM public.identity_deactivation_operation_receipts "
                "WHERE actor_id=ANY(CAST(:users AS uuid[])) "
                "OR operation_id=ANY(CAST(:operations AS uuid[]))"
            ),
            {"users": users, "operations": deactivation_operations},
        )
        await db.execute(
            text(
                "DELETE FROM public.identity_deactivation_requests "
                "WHERE user_id=ANY(CAST(:users AS uuid[])) "
                "OR request_id=ANY(CAST(:operations AS uuid[]))"
            ),
            {"users": users, "operations": deactivation_operations},
        )
        await db.execute(
            text(
                "DELETE FROM public.audit_logs WHERE actor_id=ANY(CAST(:users AS uuid[])) "
                "OR target_id=ANY(CAST(:targets AS text[]))"
            ),
            {"users": users, "targets": [str(value) for value in users]},
        )
        if jtis:
            await db.execute(
                text(
                    "DELETE FROM public.identity_revoked_jtis "
                    "WHERE jti=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [uuid.UUID(str(value)) for value in jtis]},
            )
        await db.execute(
            delete(PasswordResetToken).where(PasswordResetToken.user_id.in_(users))
        )
        await db.execute(delete(ApiKey).where(ApiKey.user_id.in_(users)))
        if projects:
            task_ids = select(Task.id).where(Task.project_id.in_(projects))
            await db.execute(
                delete(TaskLock).where(
                    TaskLock.user_id.in_(users) | TaskLock.task_id.in_(task_ids)
                )
            )
            await db.execute(delete(Task).where(Task.project_id.in_(projects)))
            await db.execute(
                delete(TaskBatch).where(TaskBatch.project_id.in_(projects))
            )
            await db.execute(
                delete(ProjectMember).where(
                    ProjectMember.user_id.in_(users)
                    | ProjectMember.project_id.in_(projects)
                )
            )
            await db.execute(delete(Project).where(Project.id.in_(projects)))
        await db.execute(delete(User).where(User.id.in_(users)))


async def _receipts(engine, operation_ids):
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT operation_id, actor_id, operation, target_user_id, "
                "transfer_to_user_id, request_fingerprint, derivation_id, "
                "target_generation, target_is_active, result, audit_id, created_at "
                "FROM public.identity_deletion_operation_receipts "
                "WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [uuid.UUID(str(value)) for value in operation_ids]},
        )
        return {row["operation_id"]: dict(row) for row in rows.mappings()}


async def _user_state(maker, user_id):
    async with maker() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT id, role, status, is_active, disabled_kind, disabled_at, "
                        "disabled_by, disabled_reason, credential_generation, "
                        "deactivation_requested_at, deactivation_scheduled_at, "
                        "deactivation_reason FROM public.users WHERE id=:id"
                    ),
                    {"id": user_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def _task_rows(maker, task_ids):
    async with maker() as db:
        rows = await db.execute(
            text(
                "SELECT id, assignee_id, reviewer_id, status, batch_id, assigned_at, "
                "updated_at "
                "FROM public.tasks WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id"
            ),
            {"ids": task_ids},
        )
        return {row["id"]: dict(row) for row in rows.mappings()}


async def _lock_ids(maker, user_id):
    async with maker() as db:
        return list(
            await db.scalars(
                text("SELECT id FROM public.task_locks WHERE user_id=:id ORDER BY id"),
                {"id": user_id},
            )
        )


async def _audit_rows(maker, *, target_id=None, actor_id=None, audit_ids=()):
    async with maker() as db:
        statement = select(
            AuditLog.id,
            AuditLog.actor_id,
            AuditLog.actor_email,
            AuditLog.actor_role,
            AuditLog.action,
            AuditLog.target_type,
            AuditLog.target_id,
            AuditLog.method,
            AuditLog.path,
            AuditLog.status_code,
            AuditLog.ip,
            AuditLog.detail_json,
            AuditLog.request_id,
        )
        if audit_ids:
            statement = statement.where(AuditLog.id.in_(audit_ids))
        if target_id is not None:
            statement = statement.where(AuditLog.actor_id == target_id)
        if actor_id is not None:
            statement = statement.where(AuditLog.actor_id == actor_id)
        rows = await db.execute(statement.order_by(AuditLog.id))
        return [dict(row._mapping) for row in rows]


def _canonical_user(value):
    return DirectoryUserRecord.model_validate_json(
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    ).model_dump(mode="json")


async def _run_delete_with_busy_retry(identity, control, request, semaphore):
    started = time.perf_counter()
    attempts = 0
    retries = 0
    retryable = {"account_busy", "credential_busy"}
    while attempts < 8:
        attempts += 1
        async with semaphore:
            result = await asyncio.to_thread(
                _node, identity, control, request, timeout=90
            )
        if result.get("ok") is True:
            return {
                "result": result,
                "attempts": attempts,
                "retries": retries,
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        if result.get("error", {}).get("code") not in retryable:
            pytest.fail("Deletion host sample returned a non-retryable error")
        if attempts == 8:
            pytest.fail("Deletion host sample exhausted bounded retries")
        retries += 1
        await asyncio.sleep(min(0.02 * 2 ** (retries - 1), 0.16))
    pytest.fail("Deletion host sample did not complete")


def _percentile(values, percentile):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percentile) - 1)]


async def test_installed_owner_deletion_transfer_replay_and_bounded_sample(
    test_engine, tmp_path, record_property
):
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    identity = None
    user_ids = [actor_id]
    project_ids = []
    operation_ids = []
    deactivation_operation_ids = []
    jtis = []
    try:
        async with maker.begin() as db:
            target = await create_user(
                db,
                "employee",
                f"deletion-target-{suffix}@example.test",
                "Delete target",
            )
            receiver = await create_user(
                db,
                "employee",
                f"deletion-receiver-{suffix}@example.test",
                "Task receiver",
            )
            reviewer = await create_user(
                db,
                "employee",
                f"deletion-reviewer-{suffix}@example.test",
                "Reviewer",
            )
            lock_only = await create_user(
                db,
                "employee",
                f"deletion-lock-only-{suffix}@example.test",
                "Lock-only target",
            )
            no_work = await create_user(
                db,
                "employee",
                f"deletion-no-work-{suffix}@example.test",
                "No-work target",
            )
            invalid_receiver = await create_user(
                db,
                "viewer",
                f"deletion-invalid-receiver-{suffix}@example.test",
                "Invalid transfer role",
            )
            inactive = await create_user(
                db,
                "viewer",
                f"deletion-inactive-{suffix}@example.test",
                "Historical inactive target",
            )
            inactive.is_active = False
            inactive.status = "offline"
            inactive.disabled_kind = "suspended"
            inactive.disabled_at = datetime.now(UTC) - timedelta(days=4)
            inactive.disabled_by = actor_id
            inactive.disabled_reason = "prior suspension"
            batch = await create_batch(
                db, project_id=(await create_project(db, owner_id=actor_id)).id
            )
            project = await db.get(Project, batch.project_id)
            project_ids.append(project.id)
            await create_membership(
                db,
                project_id=project.id,
                user_id=target.id,
                role="annotator",
                assigned_by=actor_id,
            )
            batch.annotator_id = target.id
            batch.reviewer_id = target.id
            batch.assigned_user_ids = [str(target.id), str(receiver.id)]
            base_time = datetime.now(UTC) - timedelta(hours=1)
            direct_tasks = []
            for index, status in enumerate(
                ("pending", "in_progress", "review", "rejected", "pending", "rejected")
            ):
                task = await create_task(db, project_id=project.id, status=status)
                task.assignee_id = target.id
                task.assigned_at = base_time
                task.created_at = base_time + timedelta(seconds=index)
                direct_tasks.append(task)
            completed_task = await create_task(
                db, project_id=project.id, status="completed"
            )
            completed_task.assignee_id = target.id
            completed_task.assigned_at = base_time
            reviewer_task = await create_task(
                db, project_id=project.id, status="review"
            )
            reviewer_task.assignee_id = receiver.id
            reviewer_task.reviewer_id = target.id
            reviewer_task.assigned_at = base_time
            reviewer_task.batch_id = batch.id
            batch_only_task = await create_task(
                db, project_id=project.id, status="pending"
            )
            batch_only_task.assignee_id = receiver.id
            batch_only_task.batch_id = batch.id
            lock_only_task = await create_task(
                db, project_id=project.id, status="completed"
            )
            lock_only_task.assignee_id = receiver.id
            task_ids = [
                task.id
                for task in [
                    *direct_tasks,
                    completed_task,
                    reviewer_task,
                    batch_only_task,
                    lock_only_task,
                ]
            ]
            old_updated_at = base_time - timedelta(days=1)
            for task in [
                *direct_tasks,
                completed_task,
                reviewer_task,
                batch_only_task,
                lock_only_task,
            ]:
                task.updated_at = old_updated_at
            for task in direct_tasks:
                db.add(
                    TaskLock(
                        task_id=task.id,
                        user_id=target.id,
                        expire_at=datetime.now(UTC) + timedelta(hours=1),
                    )
                )
            for task in (reviewer_task, lock_only_task):
                db.add(
                    TaskLock(
                        task_id=task.id,
                        user_id=target.id,
                        expire_at=datetime.now(UTC) - timedelta(hours=1),
                    )
                )
            db.add_all(
                [
                    AuditLog(
                        actor_id=target.id,
                        actor_email=target.email,
                        actor_role=target.role,
                        action="deletion.fixture_history",
                        target_type="task",
                        target_id=str(direct_tasks[0].id),
                        method="PATCH",
                        path="/api/v1/tasks/history",
                        status_code=200,
                        ip="192.0.2.17",
                        detail_json={"preserve": "historic detail"},
                        request_id=str(uuid.uuid4()),
                    ),
                    AuditLog(
                        actor_id=target.id,
                        actor_email=target.email,
                        actor_role=target.role,
                        action="deletion.fixture_second_history",
                        target_type="user",
                        target_id=str(target.id),
                        method="POST",
                        path="/api/v1/users/history",
                        status_code=202,
                        ip="192.0.2.18",
                        detail_json={"second": True},
                        request_id=str(uuid.uuid4()),
                    ),
                ]
            )
            target.credential_generation = 7
            target.status = "online"
            target.last_login_at = datetime.now(UTC) - timedelta(hours=3)
            api_key, api_key_text = await create_api_key(
                db, target.id, "Deletion must retain API key state", ["*"]
            )
            reset_token = await seed_challenge(
                db,
                target,
                purpose="password_reset",
                token=f"deletion-reset-{suffix}",
            )
            db.add(
                TaskLock(
                    task_id=lock_only_task.id,
                    user_id=lock_only.id,
                    expire_at=datetime.now(UTC) - timedelta(hours=2),
                )
            )
            db.add(
                AuditLog(
                    actor_id=target.id,
                    actor_email=target.email,
                    actor_role=target.role,
                    action="deletion.fixture_history_last",
                    target_type="task",
                    target_id=str(direct_tasks[-1].id),
                    method="POST",
                    path="/api/v1/tasks/history-last",
                    status_code=201,
                    ip="192.0.2.19",
                    detail_json={"third": "keep"},
                    request_id=str(uuid.uuid4()),
                )
            )
            await db.flush()
        seeded_task_rows = await _task_rows(maker, task_ids)
        assert all(
            row["updated_at"] == old_updated_at for row in seeded_task_rows.values()
        )
        user_ids.extend(
            [
                target.id,
                receiver.id,
                reviewer.id,
                lock_only.id,
                no_work.id,
                invalid_receiver.id,
                inactive.id,
            ]
        )

        actor_token = _token(actor_id, secret)
        actor_jti = uuid.UUID(decode_access_token(actor_token, secret=secret)["jti"])
        target_token = create_access_token(
            str(target.id), "employee", secret=secret, expire_minutes=15, gen=7
        )
        target_jti = uuid.UUID(decode_access_token(target_token, secret=secret)["jti"])
        jtis.extend([actor_jti, target_jti])
        async with maker() as db:
            api_key_before = await db.scalar(
                select(ApiKey.revoked_at).where(ApiKey.id == api_key.id)
            )
            reset_before = await db.scalar(
                select(PasswordResetToken.used_at).where(
                    PasswordResetToken.token_hash == token_hash(reset_token)
                )
            )
        target_before = await _user_state(maker, target.id)
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_deactivation=True,
            enable_deletions=True,
        )
        reservation_reason = f"delete-reservation-{suffix}"
        reservation_request = _deactivation_request(
            target_token, target.id, reservation_reason
        )
        reservation_operation = uuid.UUID(reservation_request["operation_id"])
        deactivation_operation_ids.append(reservation_operation)
        reservation_result = await asyncio.to_thread(
            _deactivation_node, identity, control, reservation_request
        )
        assert reservation_result["ok"] is True
        reserved = await _user_state(maker, target.id)
        assert reserved["deactivation_requested_at"] is not None
        assert reserved["deactivation_scheduled_at"] - reserved[
            "deactivation_requested_at"
        ] == timedelta(days=7)
        assert reserved["deactivation_reason"] == reservation_reason
        old_audits = await _audit_rows(maker, target_id=target.id)
        assert len(old_audits) == 4

        no_transfer = _delete_request(actor_token, target.id)
        no_transfer_operation = uuid.UUID(no_transfer["operation_id"])
        operation_ids.append(no_transfer_operation)
        blocked = await asyncio.to_thread(_node, identity, control, no_transfer)
        assert blocked["ok"] is False
        assert blocked["error"]["code"] == "has_pending_tasks"
        assert blocked["error"]["status_code"] == 409
        detail = blocked["error"]["detail"]
        assert detail["pending_task_count"] == len(direct_tasks) == 6
        assert detail["locked_task_count"] == 8
        assert detail["sample_task_ids"] == [
            str(task.id) for task in reversed(direct_tasks[-5:])
        ]
        assert (
            detail["message"] == "该用户当前有 6 个未完成任务、8 个锁定任务，需先转交"
        )
        assert await _receipts(test_engine, [no_transfer_operation]) == {}

        lock_only_request = _delete_request(actor_token, lock_only.id)
        lock_only_operation = uuid.UUID(lock_only_request["operation_id"])
        operation_ids.append(lock_only_operation)
        lock_only_blocked = await asyncio.to_thread(
            _node, identity, control, lock_only_request
        )
        assert lock_only_blocked["ok"] is False
        assert lock_only_blocked["error"]["code"] == "has_pending_tasks"
        assert lock_only_blocked["error"]["detail"]["pending_task_count"] == 0
        assert lock_only_blocked["error"]["detail"]["locked_task_count"] == 1
        assert lock_only_blocked["error"]["detail"]["sample_task_ids"] == []

        missing_receiver_request = _delete_request(
            actor_token, no_work.id, uuid.uuid4()
        )
        missing_receiver_operation = uuid.UUID(missing_receiver_request["operation_id"])
        operation_ids.append(missing_receiver_operation)
        missing_receiver = await asyncio.to_thread(
            _node, identity, control, missing_receiver_request
        )
        assert missing_receiver["ok"] is False
        assert missing_receiver["error"]["code"] == "receiver_unavailable"
        assert missing_receiver["error"]["status_code"] == 400
        assert await _receipts(test_engine, [missing_receiver_operation]) == {}

        same_receiver_request = _delete_request(actor_token, no_work.id, no_work.id)
        same_receiver_operation = uuid.UUID(same_receiver_request["operation_id"])
        operation_ids.append(same_receiver_operation)
        same_receiver = await asyncio.to_thread(
            _node, identity, control, same_receiver_request
        )
        assert same_receiver["ok"] is False
        assert same_receiver["error"]["code"] == "receiver_is_target"

        role_receiver_request = _delete_request(
            actor_token, no_work.id, invalid_receiver.id
        )
        role_receiver_operation = uuid.UUID(role_receiver_request["operation_id"])
        operation_ids.append(role_receiver_operation)
        role_receiver = await asyncio.to_thread(
            _node, identity, control, role_receiver_request
        )
        assert role_receiver["ok"] is False
        assert role_receiver["error"]["code"] == "receiver_role_invalid"

        self_delete_request = _delete_request(actor_token, actor_id)
        self_delete_operation = uuid.UUID(self_delete_request["operation_id"])
        operation_ids.append(self_delete_operation)
        self_delete = await asyncio.to_thread(
            _node, identity, control, self_delete_request
        )
        assert self_delete["ok"] is False
        assert self_delete["error"]["code"] == "self_delete"

        project_connection = await test_engine.connect()
        project_transaction = await project_connection.begin()
        try:
            await project_connection.execute(
                text("SELECT id FROM public.projects WHERE id=:id FOR UPDATE"),
                {"id": project.id},
            )
            project_busy_request = _delete_request(actor_token, target.id)
            project_busy_operation = uuid.UUID(project_busy_request["operation_id"])
            operation_ids.append(project_busy_operation)
            project_busy = await asyncio.to_thread(
                _node, identity, control, project_busy_request
            )
            assert project_busy["ok"] is False
            assert project_busy["error"]["code"] == "project_busy"
            assert project_busy["error"]["status_code"] == 409
            assert project_busy["error"]["detail"] == {"reason": "project_busy"}
        finally:
            await project_transaction.rollback()
            await project_connection.close()

        user_connection = await test_engine.connect()
        user_transaction = await user_connection.begin()
        try:
            await user_connection.execute(
                text("SELECT id FROM public.users WHERE id=:id FOR UPDATE"),
                {"id": target.id},
            )
            account_busy_request = _delete_request(actor_token, target.id)
            account_busy_operation = uuid.UUID(account_busy_request["operation_id"])
            operation_ids.append(account_busy_operation)
            account_busy = await asyncio.to_thread(
                _node, identity, control, account_busy_request
            )
            assert account_busy["ok"] is False
            assert account_busy["error"]["code"] == "account_busy"
            assert account_busy["error"]["status_code"] == 409
        finally:
            await user_transaction.rollback()
            await user_connection.close()

        delete_request = _delete_request(actor_token, target.id, receiver.id)
        delete_operation = uuid.UUID(delete_request["operation_id"])
        operation_ids.append(delete_operation)
        assert await asyncio.to_thread(
            _node, identity, control, delete_request, discard=True
        ) == {"ok": True, "response_discarded": True}
        receipt = (await _receipts(test_engine, [delete_operation]))[delete_operation]
        assert receipt["actor_id"] == actor_id
        assert receipt["operation"] == "delete_account"
        assert receipt["target_user_id"] == target.id
        assert receipt["transfer_to_user_id"] == receiver.id
        assert receipt["target_generation"] == 7
        assert receipt["target_is_active"] is False
        assert receipt["audit_id"] is not None
        frozen_user = receipt["result"]["user"]
        assert frozen_user["is_active"] is False
        assert frozen_user["disabled_kind"] == "deleted"
        assert frozen_user["disabled_by"] == str(actor_id)
        assert frozen_user["disabled_reason"] == "管理员删除账号"
        state_after = await _user_state(maker, target.id)
        assert state_after["is_active"] is False
        assert state_after["status"] == "offline"
        assert state_after["disabled_kind"] == "deleted"
        assert (
            state_after["credential_generation"]
            == target_before["credential_generation"]
            == 7
        )
        assert (
            state_after["deactivation_requested_at"]
            == reserved["deactivation_requested_at"]
        )
        assert (
            state_after["deactivation_scheduled_at"]
            == reserved["deactivation_scheduled_at"]
        )
        assert state_after["deactivation_reason"] == reservation_reason
        async with maker() as db:
            assert (
                await db.scalar(
                    select(ApiKey.revoked_at).where(ApiKey.id == api_key.id)
                )
                is None
            )
            assert api_key_before is None
            assert (
                await db.scalar(
                    select(PasswordResetToken.used_at).where(
                        PasswordResetToken.token_hash == token_hash(reset_token)
                    )
                )
                is None
            )
            assert reset_before is None
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_revoked_jtis WHERE jti=:jti"
                    ),
                    {"jti": target_jti},
                )
                == 0
            )

        transferred_rows = await _task_rows(maker, task_ids)
        for task in direct_tasks:
            row = transferred_rows[task.id]
            assert row["assignee_id"] == receiver.id
            assert row["assigned_at"] is not None and row["assigned_at"] > base_time
            assert row["updated_at"] > old_updated_at
        assert transferred_rows[completed_task.id]["assignee_id"] == target.id
        assert transferred_rows[completed_task.id]["updated_at"] == old_updated_at
        assert transferred_rows[reviewer_task.id]["assignee_id"] == receiver.id
        assert transferred_rows[reviewer_task.id]["reviewer_id"] == target.id
        assert transferred_rows[reviewer_task.id]["updated_at"] == old_updated_at
        assert transferred_rows[batch_only_task.id]["assignee_id"] == receiver.id
        assert transferred_rows[batch_only_task.id]["batch_id"] == batch.id
        assert transferred_rows[batch_only_task.id]["updated_at"] == old_updated_at
        assert transferred_rows[lock_only_task.id]["updated_at"] == old_updated_at
        updated_at_after_delete = {
            task_id: row["updated_at"] for task_id, row in transferred_rows.items()
        }
        assert {
            "annotator_id": batch.annotator_id,
            "reviewer_id": batch.reviewer_id,
            "assigned_user_ids": batch.assigned_user_ids,
        } == {
            "annotator_id": target.id,
            "reviewer_id": target.id,
            "assigned_user_ids": [str(target.id), str(receiver.id)],
        }
        async with maker() as db:
            assert (
                await db.scalar(
                    select(ProjectMember.id).where(
                        ProjectMember.project_id == project.id,
                        ProjectMember.user_id == target.id,
                    )
                )
                is not None
            )
        assert await _lock_ids(maker, target.id) == []
        redacted = await _audit_rows(maker, target_id=target.id)
        assert len(redacted) == len(old_audits) == 4
        for before, after in zip(old_audits, redacted, strict=True):
            assert after["actor_id"] == before["actor_id"] == target.id
            assert after["actor_email"] is None and after["actor_role"] is None
            for field in (
                "action",
                "target_type",
                "target_id",
                "method",
                "path",
                "status_code",
                "ip",
                "detail_json",
                "request_id",
            ):
                assert after[field] == before[field]
        delete_audit = await _audit_rows(maker, audit_ids=[receipt["audit_id"]])
        assert len(delete_audit) == 1
        assert delete_audit[0]["action"] == "user.delete"
        assert delete_audit[0]["actor_id"] == actor_id
        assert delete_audit[0]["target_id"] == str(target.id)
        assert delete_audit[0]["detail_json"] == {
            "email": target.email,
            "role": "employee",
            "transferred_to": str(receiver.id),
            "transferred_count": len(direct_tasks),
            "released_locks": 8,
            "redacted_audit_rows": 4,
        }

        port = int(identity.origin.rsplit(":", 1)[1])
        log_path = tmp_path / f"identity-{port}.log"
        log_values = [
            actor_token,
            target_token,
            control,
            api_key_text,
            reset_token,
            reservation_reason,
        ]
        if log_path.exists():
            log_contents = log_path.read_text(encoding="utf-8", errors="replace")
            assert all(value not in log_contents for value in log_values)
        identity.stop()
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            port=port,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_deactivation=True,
            enable_deletions=True,
        )

        async with maker.begin() as db:
            post_delete_task = await create_task(
                db, project_id=project.id, status="pending"
            )
            post_delete_task.assignee_id = target.id
            post_delete_task.assigned_at = datetime.now(UTC)
            post_delete_lock = TaskLock(
                task_id=post_delete_task.id,
                user_id=target.id,
                expire_at=datetime.now(UTC) + timedelta(hours=1),
            )
            db.add(post_delete_lock)
            post_delete_audit = AuditLog(
                actor_id=target.id,
                actor_email="new-history-after-delete@example.test",
                actor_role="employee",
                action="deletion.post_acceptance_history",
                target_type="task",
                target_id=str(post_delete_task.id),
                method="PATCH",
                path="/api/v1/tasks/after-delete",
                status_code=200,
                ip="192.0.2.21",
                detail_json={"created_after_acceptance": True},
                request_id=str(uuid.uuid4()),
            )
            db.add(post_delete_audit)
            await db.flush()
            post_delete_audit_id = post_delete_audit.id
        replayed, _ = await asyncio.to_thread(
            _node_response, identity, control, delete_request
        )
        assert replayed["ok"] is True
        assert _canonical_user(replayed["data"]["user"]) == _canonical_user(
            receipt["result"]["user"]
        )
        assert await _receipts(test_engine, [delete_operation]) == {
            delete_operation: receipt
        }
        replayed_task_rows = await _task_rows(maker, task_ids)
        assert {
            task_id: row["updated_at"] for task_id, row in replayed_task_rows.items()
        } == updated_at_after_delete
        assert await _lock_ids(maker, target.id) == [post_delete_lock.id]
        post_delete_rows = await _task_rows(maker, [post_delete_task.id])
        assert post_delete_rows[post_delete_task.id]["assignee_id"] == target.id
        post_delete_audit_rows = await _audit_rows(
            maker, audit_ids=[post_delete_audit_id]
        )
        assert len(post_delete_audit_rows) == 1
        assert (
            post_delete_audit_rows[0]["actor_email"]
            == "new-history-after-delete@example.test"
        )
        assert post_delete_audit_rows[0]["actor_role"] == "employee"
        assert len(await _audit_rows(maker, audit_ids=[receipt["audit_id"]])) == 1

        changed_request = json.loads(json.dumps(delete_request))
        changed_request["payload"]["transfer_to_user_id"] = str(no_work.id)
        changed_replay = await asyncio.to_thread(
            _node, identity, control, changed_request
        )
        assert changed_replay["ok"] is False
        assert changed_replay["error"]["code"] == "operation_conflict"

        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET credential_generation=8 WHERE id=:id"),
                {"id": target.id},
            )
        generation_conflict = await asyncio.to_thread(
            _node, identity, control, delete_request
        )
        assert generation_conflict["ok"] is False
        assert generation_conflict["error"]["code"] == "operation_conflict"
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET credential_generation=7 WHERE id=:id"),
                {"id": target.id},
            )
            await db.execute(
                text("UPDATE public.users SET is_active=true WHERE id=:id"),
                {"id": target.id},
            )
        active_conflict = await asyncio.to_thread(
            _node, identity, control, delete_request
        )
        assert active_conflict["ok"] is False
        assert active_conflict["error"]["code"] == "operation_conflict"
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET is_active=false WHERE id=:id"),
                {"id": target.id},
            )
        assert await _receipts(test_engine, [delete_operation]) == {
            delete_operation: receipt
        }
        assert len(await _audit_rows(maker, audit_ids=[receipt["audit_id"]])) == 1

        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET role='viewer' WHERE id=:id"),
                {"id": actor_id},
            )
        manager_replay = await asyncio.to_thread(
            _node, identity, control, delete_request
        )
        assert manager_replay["ok"] is False
        assert manager_replay["error"]["code"] == "manager_required"
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET role='super_admin' WHERE id=:id"),
                {"id": actor_id},
            )

        inactive_before = await _user_state(maker, inactive.id)
        inactive_request = _delete_request(actor_token, inactive.id, uuid.uuid4())
        inactive_operation = uuid.UUID(inactive_request["operation_id"])
        operation_ids.append(inactive_operation)
        inactive_noop = await asyncio.to_thread(
            _node, identity, control, inactive_request
        )
        assert inactive_noop["ok"] is True
        assert inactive_noop["data"]["user"]["disabled_kind"] == "suspended"
        assert inactive_noop["data"]["user"]["disabled_reason"] == "prior suspension"
        assert await _receipts(test_engine, [inactive_operation]) != {}
        assert await _audit_rows(maker, target_id=inactive.id) == []
        assert await _user_state(maker, inactive.id) == inactive_before
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET is_active=true WHERE id=:id"),
                {"id": inactive.id},
            )
        inactive_conflict = await asyncio.to_thread(
            _node, identity, control, inactive_request
        )
        assert inactive_conflict["ok"] is False
        assert inactive_conflict["error"]["code"] == "operation_conflict"
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET is_active=false WHERE id=:id"),
                {"id": inactive.id},
            )

        sample_targets = []
        sample_requests = []
        async with maker.begin() as db:
            for index in range(12):
                sample_target = await create_user(
                    db,
                    "employee",
                    f"deletion-host-sample-{suffix}-{index}@example.test",
                    f"Host deletion sample {index}",
                )
                sample_targets.append(sample_target)
                request = _delete_request(actor_token, sample_target.id)
                sample_requests.append(request)
                operation_ids.append(uuid.UUID(request["operation_id"]))
        user_ids.extend(target.id for target in sample_targets)
        semaphore = asyncio.Semaphore(4)
        started = time.perf_counter()
        sample_results = await asyncio.gather(
            *(
                _run_delete_with_busy_retry(identity, control, request, semaphore)
                for request in sample_requests
            )
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        sample_operation_ids = [
            uuid.UUID(request["operation_id"]) for request in sample_requests
        ]
        sample_receipts = await _receipts(test_engine, sample_operation_ids)
        assert len(sample_receipts) == 12
        sample_audits = await _audit_rows(
            maker,
            audit_ids=[
                sample_receipts[operation]["audit_id"]
                for operation in sample_operation_ids
            ],
        )
        assert len(sample_audits) == 12
        attempts = sum(item["attempts"] for item in sample_results)
        retries = sum(item["retries"] for item in sample_results)
        latencies = [item["latency_ms"] for item in sample_results]
        sample_metrics = {
            "scope": "host process",
            "concurrency": 4,
            "operations": 12,
            "attempts": attempts,
            "busy_retries": retries,
            "elapsed_ms": round(elapsed_ms, 3),
            "p50_ms": round(statistics.median(latencies), 3),
            "p95_ms": round(_percentile(latencies, 0.95), 3),
            "per_operation_attempts": [item["attempts"] for item in sample_results],
            "per_operation_retries": [item["retries"] for item in sample_results],
            "per_operation_ms": [round(value, 3) for value in latencies],
        }
        record_property(
            "identity_deletion_host_process_sample",
            json.dumps(sample_metrics, sort_keys=True),
        )

        for request in sample_requests:
            replay = await asyncio.to_thread(_node, identity, control, request)
            assert replay["ok"] is True
        assert await _receipts(test_engine, sample_operation_ids) == sample_receipts
        sample_audits_after = await _audit_rows(
            maker,
            audit_ids=[
                sample_receipts[operation]["audit_id"]
                for operation in sample_operation_ids
            ],
        )
        assert sample_audits_after == sample_audits

        async with maker.begin() as db:
            revoked_at = datetime.now(UTC) + timedelta(days=8)
            await db.execute(
                text("SELECT aap_identity_v1.revoke_jti(CAST(:jti AS uuid), :until)"),
                {"jti": actor_jti, "until": revoked_at},
            )
        revoked_actor_replay = await asyncio.to_thread(
            _node, identity, control, delete_request
        )
        assert revoked_actor_replay["ok"] is False
        assert revoked_actor_replay["error"]["code"] == "credential_revoked"
    finally:
        if identity:
            identity.stop()
        await _cleanup_deletions(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
            operation_ids=operation_ids,
            deactivation_operation_ids=deactivation_operation_ids,
            jtis=jtis,
        )


async def test_installed_owner_project_admin_deletion_scope_and_receiver_membership(
    test_engine, tmp_path
):
    maker, super_admin_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    identity = None
    user_ids = [super_admin_id]
    project_ids = []
    operation_ids = []
    try:
        async with maker.begin() as db:
            manager = await create_user(
                db,
                "project_admin",
                f"deletion-pa-{suffix}@example.test",
                "Deletion project admin",
            )
            target = await create_user(
                db, "employee", f"deletion-pa-target-{suffix}@example.test", "PA target"
            )
            receiver = await create_user(
                db,
                "employee",
                f"deletion-pa-receiver-{suffix}@example.test",
                "Nonmember receiver",
            )
            empty_target = await create_user(
                db,
                "employee",
                f"deletion-pa-empty-{suffix}@example.test",
                "Empty-scope target",
            )
            scope_target = await create_user(
                db,
                "employee",
                f"deletion-pa-scope-{suffix}@example.test",
                "Foreign-scope target",
            )
            foreign_owner = await create_user(
                db,
                "super_admin",
                f"deletion-foreign-owner-{suffix}@example.test",
                "Foreign project owner",
            )
            project = await create_project(db, owner_id=manager.id)
            foreign_project = await create_project(db, owner_id=foreign_owner.id)
            project_ids.extend([project.id, foreign_project.id])
            await create_membership(
                db,
                project_id=project.id,
                user_id=target.id,
                role="annotator",
                assigned_by=manager.id,
            )
            batch = await create_batch(db, project_id=project.id)
            batch.annotator_id = target.id
            batch.assigned_user_ids = [str(target.id)]
            task = await create_task(db, project_id=project.id, status="pending")
            task.assignee_id = target.id
            batch_task = await create_task(
                db, project_id=project.id, status="completed"
            )
            batch_task.assignee_id = target.id
            batch_task.batch_id = batch.id
            db.add(
                TaskLock(
                    task_id=task.id,
                    user_id=target.id,
                    expire_at=datetime.now(UTC) - timedelta(minutes=1),
                )
            )
            await create_membership(
                db,
                project_id=foreign_project.id,
                user_id=scope_target.id,
                role="annotator",
                assigned_by=foreign_owner.id,
            )
        user_ids.extend(
            [
                manager.id,
                target.id,
                receiver.id,
                empty_target.id,
                scope_target.id,
                foreign_owner.id,
            ]
        )
        manager_token = create_access_token(
            str(manager.id), "project_admin", secret=secret, expire_minutes=15, gen=0
        )
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_deletions=True,
        )

        scope_request = _delete_request(manager_token, scope_target.id)
        scope_operation = uuid.UUID(scope_request["operation_id"])
        operation_ids.append(scope_operation)
        scope_result = await asyncio.to_thread(_node, identity, control, scope_request)
        assert scope_result["ok"] is False
        assert scope_result["error"]["code"] == "delete_scope_forbidden"
        assert scope_result["error"]["detail"] == "该用户跨多个项目，须由超级管理员处理"
        assert await _receipts(test_engine, [scope_operation]) == {}

        not_member_request = _delete_request(manager_token, target.id, receiver.id)
        not_member_operation = uuid.UUID(not_member_request["operation_id"])
        operation_ids.append(not_member_operation)
        not_member = await asyncio.to_thread(
            _node, identity, control, not_member_request
        )
        assert not_member["ok"] is False
        assert not_member["error"]["code"] == "receiver_scope_forbidden"
        assert not_member["error"]["status_code"] == 403
        assert (await _task_rows(maker, [task.id]))[task.id]["assignee_id"] == target.id
        assert len(await _lock_ids(maker, target.id)) == 1
        assert await _receipts(test_engine, [not_member_operation]) == {}
        async with maker.begin() as db:
            await create_membership(
                db,
                project_id=project.id,
                user_id=receiver.id,
                role="reviewer",
                assigned_by=manager.id,
            )
        accepted_request = _delete_request(manager_token, target.id, receiver.id)
        accepted_operation = uuid.UUID(accepted_request["operation_id"])
        operation_ids.append(accepted_operation)
        accepted = await asyncio.to_thread(_node, identity, control, accepted_request)
        assert accepted["ok"] is True
        assert accepted["data"]["user"]["is_active"] is False
        assert (await _task_rows(maker, [task.id]))[task.id][
            "assignee_id"
        ] == receiver.id
        assert await _lock_ids(maker, target.id) == []

        empty_request = _delete_request(manager_token, empty_target.id)
        empty_operation = uuid.UUID(empty_request["operation_id"])
        operation_ids.append(empty_operation)
        empty_result = await asyncio.to_thread(_node, identity, control, empty_request)
        assert empty_result["ok"] is True
        assert empty_result["data"]["user"]["is_active"] is False
        empty_receipt = (await _receipts(test_engine, [empty_operation]))[
            empty_operation
        ]
        empty_audit = await _audit_rows(maker, audit_ids=[empty_receipt["audit_id"]])
        assert len(empty_audit) == 1
        assert set(empty_audit[0]["detail_json"]) == {
            "email",
            "role",
            "transferred_to",
            "transferred_count",
            "released_locks",
        }

        self_receiver_target = await _add_employee(maker, suffix, "self-receiver")
        user_ids.append(self_receiver_target.id)
        self_receiver_project = await _make_project_with_task(
            maker, manager.id, self_receiver_target.id, suffix
        )
        project_ids.append(self_receiver_project["project"].id)
        self_receiver_request = _delete_request(
            manager_token, self_receiver_target.id, manager.id
        )
        self_receiver_operation = uuid.UUID(self_receiver_request["operation_id"])
        operation_ids.append(self_receiver_operation)
        self_receiver_result = await asyncio.to_thread(
            _node, identity, control, self_receiver_request
        )
        assert self_receiver_result["ok"] is True
        assert self_receiver_result["data"]["user"]["is_active"] is False
        self_receiver_rows = await _task_rows(maker, self_receiver_project["task_ids"])
        assert (
            self_receiver_rows[self_receiver_project["task_ids"][0]]["assignee_id"]
            == manager.id
        )
    finally:
        if identity:
            identity.stop()
        await _cleanup_deletions(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
            operation_ids=operation_ids,
        )


async def _add_employee(maker, suffix, label):
    async with maker.begin() as db:
        return await create_user(
            db,
            "employee",
            f"deletion-{label}-{suffix}@example.test",
            f"Deletion {label}",
        )


async def _make_project_with_task(maker, owner_id, target_id, suffix):
    async with maker.begin() as db:
        project = await create_project(db, owner_id=owner_id)
        batch = await create_batch(db, project_id=project.id)
        batch.annotator_id = target_id
        task = await create_task(db, project_id=project.id, status="pending")
        task.assignee_id = target_id
        db.add(
            TaskLock(
                task_id=task.id,
                user_id=target_id,
                expire_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        )
        return {"project": project, "batch": batch, "task_ids": [task.id]}
