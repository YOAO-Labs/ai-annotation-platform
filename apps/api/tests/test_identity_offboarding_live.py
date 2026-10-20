"""Installed Identity owner qualification for offboarding preview and commit."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
import secrets
import subprocess
import uuid

import httpx
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
    _verdict,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_OFFBOARDING_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity offboarding verifier",
    ),
]


def _offboarding_request(
    operation, token, target_id, payload=None, *, operation_id=None
):
    operation_id = operation_id or uuid.uuid4()
    path = (
        f"/api/v1/users/{target_id}/offboarding-preview"
        if operation == "preview_offboarding"
        else f"/api/v1/users/{target_id}/offboarding"
    )
    return {
        "schema": "aap-identity-offboarding/v1",
        "operation": operation,
        "operation_id": str(operation_id),
        "token": token,
        "audit_context": {
            "method": "GET" if operation == "preview_offboarding" else "POST",
            "path": path,
            "ip": "127.0.0.1",
            "request_id": str(uuid.uuid4()),
        },
        "payload": payload or {"target_user_id": str(target_id)},
    }


def _deactivation_request(token, reason, *, operation_id=None):
    return {
        "schema": "aap-identity-deactivation/v1",
        "operation": "request_self_deactivation",
        "operation_id": str(operation_id or uuid.uuid4()),
        "token": token,
        "audit_context": {
            "method": "POST",
            "path": "/api/v1/auth/me/deactivation-request",
            "ip": "127.0.0.1",
            "request_id": str(uuid.uuid4()),
        },
        "payload": {"reason": reason},
    }


def _peer(identity, control, request, *, discard=False, timeout=120):
    peer = ROOT / "apps/api/tests/fixtures/identity_offboarding_peer.mjs"
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
    assert result.returncode == 0, "Independent Node offboarding consumer failed"
    assert control not in result.stderr and request["token"] not in result.stderr
    return json.loads(result.stdout)


def _deactivation_peer(identity, control, request):
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
    assert result.returncode == 0, "Independent Node deactivation peer failed"
    assert control not in result.stderr and request["token"] not in result.stderr
    return json.loads(result.stdout)


async def _cleanup_offboarding(
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
                "DELETE FROM public.identity_offboarding_operation_receipts "
                "WHERE actor_id=ANY(CAST(:users AS uuid[])) "
                "OR target_user_id=ANY(CAST(:users AS uuid[])) "
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


async def _offboarding_receipts(engine, operation_ids):
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT operation_id, actor_id, target_user_id, operation, "
                "request_fingerprint, derivation_id, target_generation, "
                "target_is_active, scope_basis, result, audit_id, created_at "
                "FROM public.identity_offboarding_operation_receipts "
                "WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": [uuid.UUID(str(value)) for value in operation_ids]},
        )
        return {row["operation_id"]: dict(row) for row in rows.mappings()}


async def _account_state(maker, user_id):
    async with maker() as db:
        row = await db.execute(
            text(
                "SELECT id, role, status, is_active, disabled_kind, disabled_at, "
                "disabled_by, disabled_reason, credential_generation, "
                "deactivation_requested_at, deactivation_scheduled_at, "
                "deactivation_reason FROM public.users WHERE id=:id"
            ),
            {"id": user_id},
        )
        return dict(row.mappings().one())


async def _task_rows(maker, task_ids):
    async with maker() as db:
        rows = await db.execute(
            text(
                "SELECT id, project_id, batch_id, status, assignee_id, reviewer_id, "
                "assigned_at, version, updated_at FROM public.tasks "
                "WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id"
            ),
            {"ids": task_ids},
        )
        return {row["id"]: dict(row) for row in rows.mappings()}


async def _batch_state(maker, batch_ids):
    async with maker() as db:
        rows = await db.execute(
            text(
                "SELECT id, project_id, annotator_id, reviewer_id, assigned_user_ids, "
                "updated_at FROM public.task_batches "
                "WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id"
            ),
            {"ids": batch_ids},
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


async def _audit_rows(maker, *, action=None, target_id=None, audit_ids=()):
    async with maker() as db:
        statement = select(
            AuditLog.id,
            AuditLog.actor_id,
            AuditLog.actor_email,
            AuditLog.actor_role,
            AuditLog.action,
            AuditLog.target_type,
            AuditLog.target_id,
            AuditLog.status_code,
            AuditLog.detail_json,
            AuditLog.request_id,
        )
        if audit_ids:
            statement = statement.where(AuditLog.id.in_(audit_ids))
        if action is not None:
            statement = statement.where(AuditLog.action == action)
        if target_id is not None:
            statement = statement.where(AuditLog.target_id == str(target_id))
        rows = await db.execute(statement.order_by(AuditLog.id))
        return [dict(row._mapping) for row in rows]


def _canonical_user(value):
    return DirectoryUserRecord.model_validate_json(
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    ).model_dump(mode="json")


async def test_installed_owner_offboarding_handoff_response_loss_replay_and_retirement(
    test_engine, tmp_path
):
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    identity = None
    user_ids = [actor_id]
    project_ids = []
    offboarding_operation_ids = []
    deactivation_operation_ids = []
    jtis = []
    try:
        async with maker.begin() as db:
            target = await create_user(
                db,
                "employee",
                f"offboarding-target-{suffix}@example.test",
                "离职目标 😀",
            )
            owner_receiver = await create_user(
                db,
                "project_admin",
                f"offboarding-owner-{suffix}@example.test",
                "Owner receiver",
            )
            annotator_receiver = await create_user(
                db,
                "employee",
                f"offboarding-annotator-{suffix}@example.test",
                "Annotator receiver",
            )
            reviewer_receiver = await create_user(
                db,
                "employee",
                f"offboarding-reviewer-{suffix}@example.test",
                "Reviewer receiver",
            )
            project = await create_project(db, owner_id=target.id, name="交接项目 😀")
            empty_roles_project = await create_project(
                db, owner_id=target.id, name="仅负责人职责"
            )
            base_time = datetime.now(UTC) - timedelta(hours=2)
            old_time = base_time - timedelta(days=1)
            project.updated_at = old_time
            empty_roles_project.updated_at = old_time
            project_ids.extend([project.id, empty_roles_project.id])
            for member, member_project, member_role in (
                (target, project, "annotator"),
                (annotator_receiver, project, "annotator"),
                (reviewer_receiver, project, "reviewer"),
                (annotator_receiver, empty_roles_project, "annotator"),
                (reviewer_receiver, empty_roles_project, "reviewer"),
            ):
                await create_membership(
                    db,
                    project_id=member_project.id,
                    user_id=member.id,
                    role=member_role,
                    assigned_by=actor_id,
                )
            batch = await create_batch(db, project_id=project.id, name="双职责批次")
            batch.annotator_id = target.id
            batch.reviewer_id = reviewer_receiver.id
            batch.updated_at = old_time
            batch.assigned_user_ids = [
                str(target.id),
                str(annotator_receiver.id),
                str(reviewer_receiver.id),
            ]
            annotator_tasks = []
            for index, state in enumerate(
                ("pending", "in_progress", "review", "rejected")
            ):
                task = await create_task(db, project_id=project.id, status=state)
                task.assignee_id = target.id
                task.reviewer_id = reviewer_receiver.id
                task.batch_id = batch.id
                task.assigned_at = base_time
                task.created_at = base_time + timedelta(seconds=index)
                task.updated_at = old_time
                annotator_tasks.append(task)
            reviewer_tasks = []
            for index, state in enumerate(
                ("pending", "in_progress", "review", "rejected")
            ):
                task = await create_task(db, project_id=project.id, status=state)
                task.assignee_id = annotator_receiver.id
                task.reviewer_id = target.id
                task.batch_id = batch.id
                task.assigned_at = base_time
                task.created_at = base_time + timedelta(seconds=index + 10)
                task.updated_at = old_time
                reviewer_tasks.append(task)
            completed_task = await create_task(
                db, project_id=project.id, status="completed"
            )
            completed_task.assignee_id = target.id
            completed_task.reviewer_id = target.id
            completed_task.batch_id = batch.id
            completed_task.assigned_at = base_time
            completed_task.updated_at = old_time
            task_ids = [
                task.id for task in [*annotator_tasks, *reviewer_tasks, completed_task]
            ]
            for task in (annotator_tasks[0], reviewer_tasks[3], completed_task):
                db.add(
                    TaskLock(
                        task_id=task.id,
                        user_id=target.id,
                        expire_at=datetime.now(UTC) - timedelta(minutes=1),
                    )
                )
            db.add(
                TaskLock(
                    task_id=annotator_tasks[0].id,
                    user_id=annotator_receiver.id,
                    expire_at=datetime.now(UTC) + timedelta(minutes=30),
                )
            )
            target.credential_generation = 5
            target.status = "online"
            api_key, api_key_value = await create_api_key(
                db, target.id, "Offboarding retirement key", ["*"]
            )
            reset_token = await seed_challenge(
                db,
                target,
                purpose="password_reset",
                token=f"offboarding-reset-{suffix}",
            )
            await db.flush()
        user_ids.extend(
            [target.id, owner_receiver.id, annotator_receiver.id, reviewer_receiver.id]
        )
        actor_token = _token(actor_id, secret)
        actor_jti = uuid.UUID(decode_access_token(actor_token, secret=secret)["jti"])
        target_token = create_access_token(
            str(target.id), "employee", secret=secret, expire_minutes=15, gen=5
        )
        target_jti = uuid.UUID(decode_access_token(target_token, secret=secret)["jti"])
        jtis.extend([actor_jti, target_jti])

        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=True,
            enable_sessions=True,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_deactivation=True,
            enable_offboarding=True,
        )
        reservation_reason = f"accepted-reservation-{suffix}"
        reservation = _deactivation_request(target_token, reservation_reason)
        reservation_operation = uuid.UUID(reservation["operation_id"])
        deactivation_operation_ids.append(reservation_operation)
        deactivation_result = await asyncio.to_thread(
            _deactivation_peer, identity, control, reservation
        )
        assert deactivation_result["ok"] is True
        reserved_state = await _account_state(maker, target.id)
        assert reserved_state["deactivation_requested_at"] is not None
        assert reserved_state["deactivation_scheduled_at"] - reserved_state[
            "deactivation_requested_at"
        ] == timedelta(days=7)
        assert reserved_state["deactivation_reason"] == reservation_reason

        preview_request = _offboarding_request(
            "preview_offboarding",
            actor_token,
            target.id,
            {"target_user_id": str(target.id)},
        )
        preview_response = await asyncio.to_thread(
            _peer, identity, control, preview_request
        )
        assert preview_response["ok"] is True
        preview = preview_response["data"]["preview"]
        assert preview["user"]["id"] == str(target.id)
        assert preview["can_commit"] is True
        project_previews = {item["project_id"]: item for item in preview["projects"]}
        assert project_previews[str(project.id)]["roles"]["owner"]["present"] is True
        assert (
            project_previews[str(project.id)]["roles"]["annotator"]["present"] is True
        )
        assert project_previews[str(project.id)]["roles"]["reviewer"]["present"] is True
        assert (
            project_previews[str(empty_roles_project.id)]["roles"]["annotator"][
                "present"
            ]
            is False
        )
        assert (
            project_previews[str(empty_roles_project.id)]["roles"]["reviewer"][
                "present"
            ]
            is False
        )
        assert project_previews[str(empty_roles_project.id)]["roles"]["annotator"][
            "receiver_options"
        ]
        assert project_previews[str(empty_roles_project.id)]["roles"]["reviewer"][
            "receiver_options"
        ]

        reason = "  交接原因 😀  "
        project_mappings = [
            {
                "project_id": str(empty_roles_project.id),
                "owner_receiver_id": str(owner_receiver.id),
                "annotator_receiver_id": None,
                "reviewer_receiver_id": None,
            },
            {
                "project_id": str(project.id),
                "owner_receiver_id": str(owner_receiver.id),
                "annotator_receiver_id": str(annotator_receiver.id),
                "reviewer_receiver_id": str(reviewer_receiver.id),
            },
        ]
        commit_request = _offboarding_request(
            "commit_offboarding",
            actor_token,
            target.id,
            {
                "target_user_id": str(target.id),
                "preview_version": preview["preview_version"],
                "reason": reason,
                "mode": "handoff",
                "projects": project_mappings,
            },
        )
        commit_operation = uuid.UUID(commit_request["operation_id"])
        offboarding_operation_ids.append(commit_operation)
        assert await asyncio.to_thread(
            _peer, identity, control, commit_request, discard=True
        ) == {"ok": True, "response_discarded": True}
        receipt = (await _offboarding_receipts(test_engine, [commit_operation]))[
            commit_operation
        ]
        frozen_result = receipt["result"]
        assert receipt["actor_id"] == actor_id
        assert receipt["target_user_id"] == target.id
        assert receipt["operation"] == "commit_offboarding"
        assert receipt["target_generation"] == 6
        assert receipt["target_is_active"] is False
        assert receipt["audit_id"] is not None
        assert frozen_result["status"] == "suspended"
        assert frozen_result["mode"] == "handoff"
        assert frozen_result["user"]["disabled_kind"] == "suspended"
        assert frozen_result["user"]["disabled_reason"] == reason.strip()
        expected_transfer_roles = []
        for project_id in sorted((project.id, empty_roles_project.id)):
            expected_transfer_roles.extend(
                ["owner", "annotator", "reviewer"]
                if project_id == project.id
                else ["owner"]
            )
        assert [item["role"] for item in frozen_result["transfers"]] == (
            expected_transfer_roles
        )
        transfer_batch_ids = {
            (item["project_id"], item["role"]): item["batch_ids"]
            for item in frozen_result["transfers"]
        }
        assert transfer_batch_ids[(str(project.id), "annotator")] == [str(batch.id)]
        assert transfer_batch_ids[(str(project.id), "reviewer")] == []
        assert transfer_batch_ids[(str(project.id), "owner")] == []
        assert transfer_batch_ids[(str(empty_roles_project.id), "owner")] == []
        assert frozen_result["unresolved"] == []
        assert frozen_result["revoked_api_key_ids"] == [str(api_key.id)]

        project_states = {}
        async with maker() as db:
            for project_row in (project, empty_roles_project):
                row = await db.execute(
                    select(Project.owner_id, Project.updated_at).where(
                        Project.id == project_row.id
                    )
                )
                project_states[project_row.id] = row.one()
            member_ids = list(
                await db.scalars(
                    select(ProjectMember.id).where(
                        ProjectMember.project_id == project.id,
                        ProjectMember.user_id == target.id,
                    )
                )
            )
            current_key = await db.get(ApiKey, api_key.id)
            current_reset = await db.scalar(
                select(PasswordResetToken.used_at).where(
                    PasswordResetToken.token_hash == token_hash(reset_token)
                )
            )
        assert project_states[project.id].owner_id == owner_receiver.id
        assert project_states[empty_roles_project.id].owner_id == owner_receiver.id
        assert project_states[project.id].updated_at > old_time
        assert project_states[empty_roles_project.id].updated_at > old_time
        assert len(member_ids) == 1
        assert current_key.revoked_at is not None
        assert current_reset is not None
        assert await _lock_ids(maker, target.id) == []
        transferred_tasks = await _task_rows(maker, task_ids)
        for task in annotator_tasks:
            row = transferred_tasks[task.id]
            assert row["assignee_id"] == annotator_receiver.id
            assert row["assigned_at"] > base_time
            assert row["updated_at"] > old_time
        for task in reviewer_tasks:
            row = transferred_tasks[task.id]
            assert row["reviewer_id"] == reviewer_receiver.id
            assert row["assignee_id"] == annotator_receiver.id
            assert row["assigned_at"] == base_time
            assert row["updated_at"] > old_time
        completed_state = transferred_tasks[completed_task.id]
        assert completed_state["assignee_id"] == target.id
        assert completed_state["reviewer_id"] == target.id
        assert completed_state["assigned_at"] == base_time
        assert completed_state["updated_at"] == old_time
        batch_state = (await _batch_state(maker, [batch.id]))[batch.id]
        assert batch_state["annotator_id"] == annotator_receiver.id
        assert batch_state["reviewer_id"] == reviewer_receiver.id
        assert batch_state["assigned_user_ids"] == [
            str(annotator_receiver.id),
            str(reviewer_receiver.id),
        ]
        assert batch_state["updated_at"] > old_time
        final_account = await _account_state(maker, target.id)
        assert final_account["credential_generation"] == 6
        assert final_account["is_active"] is False
        assert (
            final_account["deactivation_requested_at"]
            == reserved_state["deactivation_requested_at"]
        )
        assert (
            final_account["deactivation_scheduled_at"]
            == reserved_state["deactivation_scheduled_at"]
        )
        assert final_account["deactivation_reason"] == reservation_reason
        async with maker() as db:
            audit = await db.execute(
                select(
                    AuditLog.action, AuditLog.status_code, AuditLog.detail_json
                ).where(AuditLog.id == receipt["audit_id"])
            )
            audit_row = audit.one()
        assert audit_row.action == "user.offboard"
        assert audit_row.status_code == 200
        assert set(audit_row.detail_json) == {
            "mode",
            "reason_present",
            "transfers",
            "unresolved",
            "revoked_api_key_count",
        }
        assert audit_row.detail_json["reason_present"] is True
        assert reason not in json.dumps(audit_row.detail_json, ensure_ascii=False)
        assert (
            len(await _audit_rows(maker, action="user.offboard", target_id=target.id))
            == 1
        )
        async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
            assert (await _verdict(client, identity, control, target_token))[
                "ok"
            ] is False
            assert (await _verdict(client, identity, control, api_key_value))[
                "ok"
            ] is False

        async with maker.begin() as db:
            new_key, _ = await create_api_key(
                db, target.id, "Added after accepted offboarding", ["*"]
            )
        post_offboard_task = None
        post_offboard_lock_id = None
        async with maker.begin() as db:
            post_offboard_task = await create_task(
                db, project_id=project.id, status="pending"
            )
            post_offboard_task.assignee_id = target.id
            post_offboard_task.updated_at = old_time
            post_offboard_lock = TaskLock(
                task_id=post_offboard_task.id,
                user_id=target.id,
                expire_at=datetime.now(UTC) + timedelta(hours=1),
            )
            db.add(post_offboard_lock)
            await db.flush()
            post_offboard_lock_id = post_offboard_lock.id
        port = int(identity.origin.rsplit(":", 1)[1])
        identity.stop()
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            port=port,
            enable_keys=True,
            enable_sessions=True,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_deactivation=True,
            enable_offboarding=True,
        )
        replayed = await asyncio.to_thread(_peer, identity, control, commit_request)
        assert replayed["ok"] is True
        assert _canonical_user(replayed["data"]["result"]["user"]) == _canonical_user(
            frozen_result["user"]
        )
        assert await _offboarding_receipts(test_engine, [commit_operation]) == {
            commit_operation: receipt
        }
        async with maker() as db:
            replayed_key = await db.get(ApiKey, new_key.id)
            replayed_task = await db.get(Task, post_offboard_task.id)
            replayed_audit_count = await db.scalar(
                select(text("count(*)"))
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "user.offboard",
                    AuditLog.target_id == str(target.id),
                )
            )
        assert replayed_key.revoked_at is None
        assert replayed_task.assignee_id == target.id
        assert replayed_task.updated_at == old_time
        assert replayed_audit_count == 1
        assert await _lock_ids(maker, target.id) == [post_offboard_lock_id]

        stale_request = _offboarding_request(
            "commit_offboarding",
            actor_token,
            target.id,
            {
                "target_user_id": str(target.id),
                "preview_version": preview["preview_version"],
                "reason": reason,
                "mode": "handoff",
                "projects": project_mappings,
            },
        )
        offboarding_operation_ids.append(uuid.UUID(stale_request["operation_id"]))
        stale = await asyncio.to_thread(_peer, identity, control, stale_request)
        assert stale["ok"] is False
        assert stale["error"]["code"] == "offboarding_preview_stale"
        assert (
            stale["error"]["detail"]["expected_preview_version"]
            == preview["preview_version"]
        )
    finally:
        if identity:
            identity.stop()
        await _cleanup_offboarding(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
            operation_ids=offboarding_operation_ids,
            deactivation_operation_ids=deactivation_operation_ids,
            jtis=jtis,
        )


async def test_installed_owner_emergency_offboarding_preserves_work_and_redacts_audit(
    test_engine, tmp_path
):
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    identity = None
    user_ids = [actor_id]
    project_ids = []
    operation_ids = []
    jtis = []
    try:
        async with maker.begin() as db:
            target = await create_user(
                db,
                "employee",
                f"offboarding-emergency-{suffix}@example.test",
                "Emergency target",
            )
            project = await create_project(
                db, owner_id=target.id, name="紧急停用职责项目"
            )
            project_ids.append(project.id)
            old_time = datetime.now(UTC) - timedelta(days=1)
            await create_membership(
                db,
                project_id=project.id,
                user_id=target.id,
                role="annotator",
                assigned_by=actor_id,
            )
            batch = await create_batch(db, project_id=project.id, name="待交接批次")
            batch.annotator_id = target.id
            batch.reviewer_id = target.id
            batch.updated_at = old_time
            batch.assigned_user_ids = [str(target.id)]
            task = await create_task(db, project_id=project.id, status="pending")
            task.assignee_id = target.id
            task.reviewer_id = target.id
            task.batch_id = batch.id
            task.updated_at = old_time
            lock = TaskLock(
                task_id=task.id,
                user_id=target.id,
                expire_at=datetime.now(UTC) + timedelta(hours=1),
            )
            db.add(lock)
            lock_only_tasks = []
            for status in ("pending", "completed"):
                lock_only_task = await create_task(
                    db, project_id=project.id, status=status
                )
                lock_only_task.assignee_id = actor_id
                lock_only_task.reviewer_id = None
                lock_only_task.updated_at = old_time
                db.add(
                    TaskLock(
                        task_id=lock_only_task.id,
                        user_id=target.id,
                        expire_at=datetime.now(UTC) + timedelta(hours=1),
                    )
                )
                lock_only_tasks.append(lock_only_task)
            await db.flush()
            target.credential_generation = 8
            target.status = "online"
            target_token = create_access_token(
                str(target.id), "employee", secret=secret, expire_minutes=15, gen=8
            )
        emergency_task_ids = [task.id, *(item.id for item in lock_only_tasks)]
        emergency_tasks_before = await _task_rows(maker, emergency_task_ids)
        emergency_lock_ids_before = await _lock_ids(maker, target.id)
        assert len(emergency_lock_ids_before) == 3
        user_ids.append(target.id)
        actor_token = _token(actor_id, secret)
        jtis.extend(
            uuid.UUID(decode_access_token(token, secret=secret)["jti"])
            for token in (actor_token, target_token)
        )
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=True,
            enable_sessions=True,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_offboarding=True,
        )
        preview_request = _offboarding_request(
            "preview_offboarding", actor_token, target.id
        )
        preview_response = await asyncio.to_thread(
            _peer, identity, control, preview_request
        )
        preview = preview_response["data"]["preview"]
        project_preview = preview["projects"][0]
        assert project_preview["locked_task_count"] == 3
        assert preview["can_commit"] is False
        assert [item["code"] for item in project_preview["blockers"]] == [
            "no_owner_receiver",
            "no_annotator_receiver",
            "no_reviewer_receiver",
            "historic_mixed_role",
        ]
        reason = "  应急处置😀  "
        commit_request = _offboarding_request(
            "commit_offboarding",
            actor_token,
            target.id,
            {
                "target_user_id": str(target.id),
                "preview_version": preview["preview_version"],
                "reason": reason,
                "mode": "emergency_suspend",
                "projects": [],
            },
        )
        operation_id = uuid.UUID(commit_request["operation_id"])
        operation_ids.append(operation_id)
        response = await asyncio.to_thread(_peer, identity, control, commit_request)
        assert response["ok"] is True
        result = response["data"]["result"]
        assert result["mode"] == "emergency_suspend"
        assert result["transfers"] == []
        unresolved = result["unresolved"]
        assert [(item["project_id"], item["role"]) for item in unresolved] == [
            (str(project.id), role) for role in ("owner", "annotator", "reviewer")
        ]
        for item in unresolved:
            assert item["reason"] == "emergency_suspension_requires_later_handoff"
        assert unresolved[0]["batch_ids"] == []
        assert unresolved[0]["task_count"] == 0
        assert unresolved[0]["lock_count"] == 0
        for item in unresolved[1:]:
            assert item["batch_ids"] == [str(batch.id)]
            assert item["task_count"] == 1
            assert item["lock_count"] == 2
        assert result["user"]["disabled_kind"] == "emergency_suspended"
        assert result["user"]["disabled_reason"] == reason.strip()

        receipt = (await _offboarding_receipts(test_engine, [operation_id]))[
            operation_id
        ]
        assert receipt["target_generation"] == 9
        assert receipt["target_is_active"] is False
        assert receipt["audit_id"] is not None
        async with maker() as db:
            project_owner = await db.scalar(
                select(Project.owner_id).where(Project.id == project.id)
            )
            batch_state = (await _batch_state(maker, [batch.id]))[batch.id]
            task_states = await _task_rows(maker, emergency_task_ids)
            membership_count = await db.scalar(
                select(text("count(*)"))
                .select_from(ProjectMember)
                .where(
                    ProjectMember.project_id == project.id,
                    ProjectMember.user_id == target.id,
                )
            )
        assert project_owner == target.id
        assert batch_state["annotator_id"] == target.id
        assert batch_state["reviewer_id"] == target.id
        assert batch_state["assigned_user_ids"] == [str(target.id)]
        assert batch_state["updated_at"] == old_time
        assert task_states == emergency_tasks_before
        task_state = task_states[task.id]
        assert task_state["assignee_id"] == target.id
        assert task_state["reviewer_id"] == target.id
        assert task_state["updated_at"] == old_time
        for lock_only_task in lock_only_tasks:
            lock_only_state = task_states[lock_only_task.id]
            assert lock_only_state["assignee_id"] == actor_id
            assert lock_only_state["reviewer_id"] is None
            assert lock_only_state["batch_id"] is None
        assert membership_count == 1
        assert await _lock_ids(maker, target.id) == []
        account = await _account_state(maker, target.id)
        assert account["is_active"] is False
        assert account["credential_generation"] == 9
        audits = await _audit_rows(maker, action="user.offboard", target_id=target.id)
        assert len(audits) == 1
        assert audits[0]["detail_json"]["mode"] == "emergency_suspend"
        assert audits[0]["detail_json"]["reason_present"] is True
        assert reason not in json.dumps(audits[0]["detail_json"], ensure_ascii=False)
    finally:
        if identity:
            identity.stop()
        await _cleanup_offboarding(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
            operation_ids=operation_ids,
            jtis=jtis,
        )


async def test_project_admin_replay_uses_accepted_scope_after_handoff(
    test_engine, tmp_path
):
    maker, root_actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    identity = None
    user_ids = [root_actor_id]
    project_ids = []
    operation_ids = []
    jtis = []
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"offboarding-pa-{suffix}@example.test",
                "Project admin",
            )
            target = await create_user(
                db,
                "employee",
                f"offboarding-pa-target-{suffix}@example.test",
                "Direct assignment target",
            )
            receiver = await create_user(
                db,
                "employee",
                f"offboarding-pa-receiver-{suffix}@example.test",
                "Current project member receiver",
            )
            project = await create_project(
                db, owner_id=actor.id, name="Accepted scope replay"
            )
            project_ids.append(project.id)
            await create_membership(
                db,
                project_id=project.id,
                user_id=receiver.id,
                role="annotator",
                assigned_by=actor.id,
            )
            task = await create_task(db, project_id=project.id, status="pending")
            task.assignee_id = target.id
            task.updated_at = datetime.now(UTC) - timedelta(days=1)
            await db.flush()
            target_token = create_access_token(
                str(target.id), "employee", secret=secret, expire_minutes=15
            )
            actor_token = create_access_token(
                str(actor.id), "project_admin", secret=secret, expire_minutes=15
            )
        user_ids.extend([actor.id, target.id, receiver.id])
        jtis.extend(
            uuid.UUID(decode_access_token(token, secret=secret)["jti"])
            for token in (actor_token, target_token)
        )
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_keys=True,
            enable_sessions=True,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_offboarding=True,
        )
        preview_request = _offboarding_request(
            "preview_offboarding", actor_token, target.id
        )
        preview = (await asyncio.to_thread(_peer, identity, control, preview_request))[
            "data"
        ]["preview"]
        assert len(preview["projects"]) == 1
        assert preview["projects"][0]["roles"]["annotator"]["present"] is True
        assert preview["projects"][0]["roles"]["owner"]["present"] is False
        assert preview["projects"][0]["roles"]["reviewer"]["present"] is False

        async with maker.begin() as db:
            db_project = await db.get(Project, project.id)
            db_project.owner_id = root_actor_id
        out_of_scope_operation = uuid.uuid4()
        operation_ids.append(out_of_scope_operation)
        out_of_scope_request = _offboarding_request(
            "commit_offboarding",
            actor_token,
            target.id,
            {
                "target_user_id": str(target.id),
                "preview_version": preview["preview_version"],
                "reason": "out of scope before stale CAS",
                "mode": "handoff",
                "projects": [
                    {
                        "project_id": str(project.id),
                        "owner_receiver_id": None,
                        "annotator_receiver_id": str(receiver.id),
                        "reviewer_receiver_id": None,
                    }
                ],
            },
            operation_id=out_of_scope_operation,
        )
        out_of_scope = await asyncio.to_thread(
            _peer, identity, control, out_of_scope_request
        )
        assert out_of_scope["ok"] is False
        assert out_of_scope["error"]["code"] == "scope_forbidden"
        assert out_of_scope["error"]["status_code"] == 403
        assert (await _account_state(maker, target.id))["is_active"] is True
        assert (await _task_rows(maker, [task.id]))[task.id]["assignee_id"] == target.id
        assert await _offboarding_receipts(test_engine, [out_of_scope_operation]) == {}
        assert (
            await _audit_rows(maker, action="user.offboard", target_id=target.id)
        ) == []
        async with maker.begin() as db:
            db_project = await db.get(Project, project.id)
            db_project.owner_id = actor.id

        preview = (
            await asyncio.to_thread(
                _peer,
                identity,
                control,
                _offboarding_request("preview_offboarding", actor_token, target.id),
            )
        )["data"]["preview"]
        operation_id = uuid.uuid4()
        commit_request = _offboarding_request(
            "commit_offboarding",
            actor_token,
            target.id,
            {
                "target_user_id": str(target.id),
                "preview_version": preview["preview_version"],
                "reason": "PA handoff replay",
                "mode": "handoff",
                "projects": [
                    {
                        "project_id": str(project.id),
                        "owner_receiver_id": None,
                        "annotator_receiver_id": str(receiver.id),
                        "reviewer_receiver_id": None,
                    }
                ],
            },
            operation_id=operation_id,
        )
        operation_ids.append(operation_id)
        lost = await asyncio.to_thread(
            _peer, identity, control, commit_request, discard=True
        )
        assert lost == {"ok": True, "response_discarded": True}
        accepted_receipt = (await _offboarding_receipts(test_engine, [operation_id]))[
            operation_id
        ]
        assert accepted_receipt["scope_basis"] == {
            "target_role": "employee",
            "target_was_active": True,
            "project_ids": [str(project.id)],
        }
        after_handoff = (await _task_rows(maker, [task.id]))[task.id]
        assert after_handoff["assignee_id"] == receiver.id
        assert await _audit_rows(maker, action="user.offboard", target_id=target.id)

        replay = await asyncio.to_thread(_peer, identity, control, commit_request)
        assert replay["ok"] is True
        assert _canonical_user(replay["data"]["result"]["user"]) == _canonical_user(
            accepted_receipt["result"]["user"]
        )
        assert await _offboarding_receipts(test_engine, [operation_id]) == {
            operation_id: accepted_receipt
        }
        assert (await _task_rows(maker, [task.id]))[task.id] == after_handoff
        audits_before_scope_loss = await _audit_rows(
            maker, action="user.offboard", target_id=target.id
        )
        assert len(audits_before_scope_loss) == 1

        async with maker.begin() as db:
            db_project = await db.get(Project, project.id)
            db_project.owner_id = root_actor_id
        denied_replay = await asyncio.to_thread(
            _peer, identity, control, commit_request
        )
        assert denied_replay["ok"] is False
        assert denied_replay["error"]["code"] == "scope_forbidden"
        assert denied_replay["error"]["status_code"] == 403
        assert denied_replay["error"]["detail"] == "该用户存在不在你管理范围内的项目"
        assert await _offboarding_receipts(test_engine, [operation_id]) == {
            operation_id: accepted_receipt
        }
        assert (await _task_rows(maker, [task.id]))[task.id] == after_handoff
        assert (
            len(await _audit_rows(maker, action="user.offboard", target_id=target.id))
            == 1
        )
    finally:
        if identity:
            identity.stop()
        await _cleanup_offboarding(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
            operation_ids=operation_ids,
            jtis=jtis,
        )
