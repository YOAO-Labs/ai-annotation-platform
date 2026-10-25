"""Installed Identity owner tests for platform-role changes."""

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

from aap_identity.security import create_access_token
from app.db.models.api_key import ApiKey
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from tests.factory import (
    create_api_key,
    create_membership,
    create_project,
    create_task,
    create_user,
)
from tests.test_identity_admission_live import ROOT, _start_identity, _token, _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_PLATFORM_ROLE_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity platform-role verifier",
    ),
]


def _platform_role_request(
    operation, token, target_id, role, *, operation_id=None, request_id=None
):
    operation_id = operation_id or uuid.uuid4()
    request_id = request_id or uuid.uuid4()
    method = "GET" if operation == "preview_role_change" else "PATCH"
    path = f"/api/v1/users/{target_id}/role"
    if operation == "preview_role_change":
        path += f"/preview?role={role}"
    return {
        "schema": "aap-identity-platform-roles/v1",
        "operation": operation,
        "operation_id": str(operation_id),
        "token": token,
        "audit_context": {
            "method": method,
            "path": path,
            "ip": "127.0.0.1",
            "request_id": str(request_id),
        },
        "payload": {"target_user_id": str(target_id), "role": role},
    }


def _node_response(identity, control, request, *, discard=False, timeout=120):
    peer = ROOT / "apps/api/tests/fixtures/identity_platform_role_peer.mjs"
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
    assert result.returncode == 0, "Independent Node platform-role consumer failed"
    assert control not in result.stdout and control not in result.stderr
    assert (
        request["token"] not in result.stdout and request["token"] not in result.stderr
    )
    return json.loads(result.stdout), result.stdout


def _node(identity, control, request, *, discard=False, timeout=120):
    return _node_response(identity, control, request, discard=discard, timeout=timeout)[
        0
    ]


async def _node_with_busy_retry(
    identity, control, request, semaphore, *, max_attempts=8
):
    started = time.perf_counter()
    attempts = 0
    retries = 0
    retryable = {"account_busy", "credential_busy", "project_busy"}
    while attempts < max_attempts:
        attempts += 1
        async with semaphore:
            result = await asyncio.to_thread(
                _node, identity, control, request, timeout=45
            )
        if result["ok"] is True:
            return {
                "result": result,
                "attempts": attempts,
                "retries": retries,
                "latency_ms": (time.perf_counter() - started) * 1000,
            }
        if result.get("error", {}).get("code") not in retryable:
            pytest.fail("Platform-role owner sample returned a non-retryable error")
        if attempts == max_attempts:
            pytest.fail("Platform-role owner sample exhausted bounded retries")
        retries += 1
        await asyncio.sleep(min(0.025 * 2 ** (retries - 1), 0.2))
    pytest.fail("Platform-role owner sample did not complete")


def _role_token(user_id, role, secret, generation=0):
    return create_access_token(
        str(user_id), role, secret=secret, expire_minutes=15, gen=generation
    )


def _assert_no_secret_log(path, values):
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8", errors="replace")
    for value in values:
        if value and value in content:
            pytest.fail("A credential appeared in the owned Identity log")


async def _cleanup_platform_roles(maker, *, user_ids, project_ids=(), operation_ids=()):
    users = [uuid.UUID(str(value)) for value in user_ids]
    projects = [uuid.UUID(str(value)) for value in project_ids]
    operations = [uuid.UUID(str(value)) for value in operation_ids]
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await db.execute(
            text(
                "DELETE FROM public.identity_platform_role_operation_receipts "
                "WHERE actor_id=ANY(CAST(:users AS uuid[])) "
                "OR target_user_id=ANY(CAST(:users AS uuid[])) "
                "OR operation_id=ANY(CAST(:operations AS uuid[]))"
            ),
            {"users": users, "operations": operations},
        )
        await db.execute(
            text(
                "DELETE FROM public.audit_logs WHERE actor_id=ANY(CAST(:users AS uuid[])) "
                "OR target_id=ANY(CAST(:targets AS text[]))"
            ),
            {"users": users, "targets": [str(value) for value in users]},
        )
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


async def _user_snapshot(maker, user_id):
    async with maker() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT id, email, name, role, is_active, credential_generation, "
                        "preferences FROM public.users WHERE id=:id"
                    ),
                    {"id": user_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def _generation_map(maker, user_ids):
    async with maker() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT id, credential_generation FROM public.users "
                    "WHERE id=ANY(CAST(:ids AS uuid[])) ORDER BY id"
                ),
                {"ids": user_ids},
            )
        ).all()
        return {row.id: row.credential_generation for row in rows}


async def _receipts(engine, operation_ids):
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT operation_id, actor_id, target_user_id, operation, "
                    "request_fingerprint, derivation_id, result, audit_id, created_at "
                    "FROM public.identity_platform_role_operation_receipts "
                    "WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": operation_ids},
            )
        ).mappings()
        return {row["operation_id"]: dict(row) for row in rows}


async def _audit_rows(maker, audit_ids):
    async with maker() as db:
        rows = await db.execute(
            select(
                AuditLog.id,
                AuditLog.actor_id,
                AuditLog.action,
                AuditLog.target_type,
                AuditLog.target_id,
                AuditLog.status_code,
                AuditLog.detail_json,
            )
            .where(AuditLog.id.in_(audit_ids))
            .order_by(AuditLog.id)
        )
        return [dict(row._mapping) for row in rows]


async def _gather_all(*awaitables):
    results = await asyncio.gather(*awaitables, return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise failures[0]
    return results


async def _audit_count_for_user(maker, actor_id, target_id):
    async with maker() as db:
        return await db.scalar(
            text(
                "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor "
                "AND target_id=:target AND action='user.role_change'"
            ),
            {"actor": actor_id, "target": str(target_id)},
        )


async def _project_rows(maker, project_ids):
    async with maker() as db:
        return (
            await db.execute(
                select(Project.id, Project.owner_id, Project.name, Project.status)
                .where(Project.id.in_(project_ids))
                .order_by(Project.id)
            )
        ).all()


async def _membership_rows(maker, project_ids):
    async with maker() as db:
        return (
            await db.execute(
                select(
                    ProjectMember.id,
                    ProjectMember.project_id,
                    ProjectMember.user_id,
                    ProjectMember.role,
                    ProjectMember.version,
                )
                .where(ProjectMember.project_id.in_(project_ids))
                .order_by(ProjectMember.id)
            )
        ).all()


async def _task_rows(maker, project_ids):
    async with maker() as db:
        return (
            await db.execute(
                select(
                    Task.id,
                    Task.project_id,
                    Task.batch_id,
                    Task.status,
                    Task.assignee_id,
                    Task.reviewer_id,
                )
                .where(Task.project_id.in_(project_ids))
                .order_by(Task.id)
            )
        ).all()


async def _batch_rows(maker, project_ids):
    async with maker() as db:
        return (
            await db.execute(
                select(
                    TaskBatch.id,
                    TaskBatch.project_id,
                    TaskBatch.status,
                    TaskBatch.annotator_id,
                    TaskBatch.reviewer_id,
                )
                .where(TaskBatch.project_id.in_(project_ids))
                .order_by(TaskBatch.id)
            )
        ).all()


async def _key_rows(maker, user_ids):
    async with maker() as db:
        return (
            await db.execute(
                select(
                    ApiKey.id,
                    ApiKey.user_id,
                    ApiKey.key_hash,
                    ApiKey.scopes,
                    ApiKey.expires_at,
                    ApiKey.revoked_at,
                )
                .where(ApiKey.user_id.in_(user_ids))
                .order_by(ApiKey.id)
            )
        ).all()


async def test_installed_platform_role_preview_replay_and_fences(
    test_engine, tmp_path, record_property
):
    maker, actor_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    user_ids = [actor_id]
    project_ids = []
    operation_ids = []
    identity = None
    log_secrets = []
    now = datetime.now(UTC)
    async with maker.begin() as db:
        actor = await db.get(User, actor_id)
        actor.deactivation_requested_at = now - timedelta(days=1)
        actor.deactivation_scheduled_at = now + timedelta(days=6)
        reserved_request = await create_user(
            db,
            "super_admin",
            f"platform-role-reserved-request-{suffix}@example.test",
            "Reserved request admin",
        )
        reserved_request.deactivation_requested_at = now
        reserved_schedule = await create_user(
            db,
            "super_admin",
            f"platform-role-reserved-schedule-{suffix}@example.test",
            "Reserved schedule admin",
        )
        reserved_schedule.deactivation_scheduled_at = now + timedelta(days=2)
        last_super_admin = await create_user(
            db,
            "super_admin",
            f"platform-role-last-admin-{suffix}@example.test",
            "Last unreserved administrator",
        )
        cycle_target = await create_user(
            db,
            "employee",
            f"platform-role-cycle-{suffix}@example.test",
            "Cycle target",
        )
        noop_target = await create_user(
            db,
            "employee",
            f"platform-role-noop-{suffix}@example.test",
            "No-op target",
        )
        project_busy_target = await create_user(
            db,
            "employee",
            f"platform-role-project-busy-{suffix}@example.test",
            "Project lock target",
        )
        account_busy_target = await create_user(
            db,
            "employee",
            f"platform-role-account-busy-{suffix}@example.test",
            "Account lock target",
        )
        legacy_target = await create_user(
            db,
            "annotator",
            f"platform-role-legacy-{suffix}@example.test",
            "Historical platform role",
        )
        inactive_target = await create_user(
            db,
            "employee",
            f"platform-role-inactive-{suffix}@example.test",
            "Inactive role target",
        )
        inactive_target.is_active = False
        explicit_other = await create_user(
            db,
            "employee",
            f"platform-role-explicit-other-{suffix}@example.test",
            "Explicit assignment override",
        )
        blocker_z = await create_project(
            db,
            owner_id=last_super_admin.id,
            name=f"Z archived role project {suffix}",
        )
        blocker_z.status = "archived"
        blocker_a = await create_project(
            db,
            owner_id=actor_id,
            name=f"A completed role project {suffix}",
        )
        blocker_a.status = "completed"
        await create_membership(
            db,
            project_id=blocker_z.id,
            user_id=last_super_admin.id,
            role="annotator",
            assigned_by=actor_id,
        )
        await create_membership(
            db,
            project_id=blocker_a.id,
            user_id=last_super_admin.id,
            role="viewer",
            assigned_by=actor_id,
        )
        batch_both = TaskBatch(
            project_id=blocker_z.id,
            display_id=f"PR-BOTH-{suffix[:8]}",
            name="Both responsibility slots",
            status="completed",
            annotator_id=last_super_admin.id,
            reviewer_id=last_super_admin.id,
        )
        batch_annotator = TaskBatch(
            project_id=blocker_z.id,
            display_id=f"PR-ANN-{suffix[:8]}",
            name="Annotator responsibility",
            status="rejected",
            annotator_id=last_super_admin.id,
        )
        db.add_all([batch_both, batch_annotator])
        await db.flush()

        completed_direct = await create_task(
            db, project_id=blocker_z.id, status="completed"
        )
        completed_direct.batch_id = batch_both.id
        completed_direct.assignee_id = last_super_admin.id
        completed_direct.reviewer_id = last_super_admin.id
        unbatched_pending = await create_task(
            db, project_id=blocker_z.id, status="pending"
        )
        unbatched_pending.assignee_id = last_super_admin.id
        unbatched_pending.reviewer_id = last_super_admin.id
        batch_fallback_both = await create_task(
            db, project_id=blocker_z.id, status="pending"
        )
        batch_fallback_both.batch_id = batch_both.id
        reviewer_fallback = await create_task(
            db, project_id=blocker_z.id, status="rejected"
        )
        reviewer_fallback.batch_id = batch_both.id
        reviewer_fallback.assignee_id = explicit_other.id
        assignee_fallback = await create_task(
            db, project_id=blocker_z.id, status="review"
        )
        assignee_fallback.batch_id = batch_both.id
        assignee_fallback.reviewer_id = explicit_other.id
        explicit_overrides = await create_task(
            db, project_id=blocker_z.id, status="pending"
        )
        explicit_overrides.batch_id = batch_both.id
        explicit_overrides.assignee_id = explicit_other.id
        explicit_overrides.reviewer_id = explicit_other.id
        completed_fallback = await create_task(
            db, project_id=blocker_z.id, status="completed"
        )
        completed_fallback.batch_id = batch_annotator.id

        noop_project = await create_project(
            db,
            owner_id=noop_target.id,
            name=f"No-op project {suffix}",
        )
        noop_project.status = "archived"
        project_busy_project = await create_project(
            db,
            owner_id=project_busy_target.id,
            name=f"Project lock target {suffix}",
        )
        project_busy_project.status = "archived"

        sample_targets = []
        sample_projects = []
        for index in range(12):
            target = await create_user(
                db,
                "employee",
                f"platform-role-load-{suffix}-{index}@example.test",
                f"Host sample target {index}",
            )
            project = await create_project(
                db,
                owner_id=target.id,
                name=f"Host sample project {suffix} {index}",
            )
            sample_targets.append(target)
            sample_projects.append(project)

        await db.execute(
            text(
                "UPDATE public.users SET preferences=CAST(:value AS jsonb) WHERE id=:id"
            ),
            {
                "id": cycle_target.id,
                "value": (
                    '{"opaque":{"big_integer":9007199254740993,'
                    '"decimal":0.1234567890123456789,"future":[true,null,"keep"]}}'
                ),
            },
        )
        actor_api_key, actor_api_key_value = await create_api_key(
            db, actor_id, "Platform role owner", ["*"]
        )
        cycle_api_key, cycle_api_key_value = await create_api_key(
            db, cycle_target.id, "Platform role target", ["datasets:read"]
        )

    user_ids.extend(
        [
            reserved_request.id,
            reserved_schedule.id,
            last_super_admin.id,
            cycle_target.id,
            noop_target.id,
            project_busy_target.id,
            account_busy_target.id,
            legacy_target.id,
            inactive_target.id,
            explicit_other.id,
            *(target.id for target in sample_targets),
        ]
    )
    project_ids.extend(
        [
            blocker_z.id,
            blocker_a.id,
            noop_project.id,
            project_busy_project.id,
            *(project.id for project in sample_projects),
        ]
    )
    project_scope_ids = list(project_ids)
    initial_generations = None
    initial_api_key_rows = None

    def role_token(user, role=None):
        return _role_token(user.id, role or user.role, secret)

    actor_token = _token(actor_id, secret)
    reserved_actor_token = role_token(reserved_request)
    log_secrets = [
        secret,
        control,
        actor_token,
        reserved_actor_token,
        actor_api_key_value,
        cycle_api_key_value,
    ]

    def request(
        operation, target_id, role, token=actor_token, *, name=None, op_id=None
    ):
        operation_id = op_id or uuid.uuid4()
        operation_ids.append(operation_id)
        return _platform_role_request(
            operation,
            token,
            target_id,
            role,
            operation_id=operation_id,
        )

    identity = None
    try:
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_invitations=False,
            enable_directory=False,
            enable_admin_password=False,
            enable_platform_role=True,
            enable_deactivation=False,
            enable_presence=False,
        )
        initial_generations = await _generation_map(maker, user_ids)
        initial_api_key_rows = await _key_rows(maker, [actor_id, cycle_target.id])
        initial_project_rows = await _project_rows(maker, project_scope_ids)
        initial_membership_rows = await _membership_rows(maker, project_scope_ids)
        initial_task_rows = await _task_rows(maker, project_scope_ids)
        initial_batch_rows = await _batch_rows(maker, project_scope_ids)

        # API-key admission remains current and the consumer accepts the live
        # Principal while preserving the historical target role.
        api_key_preview_request = request(
            "preview_role_change",
            legacy_target.id,
            "viewer",
            actor_api_key_value,
            name="api_key_preview",
        )
        api_key_preview = await asyncio.to_thread(
            _node, identity, control, api_key_preview_request
        )
        assert api_key_preview["ok"] is True
        assert api_key_preview["data"]["current_role"] == "annotator"
        assert api_key_preview["data"]["requested_role"] == "viewer"
        assert api_key_preview["data"]["can_change"] is True
        assert api_key_preview["data"]["other_project_count"] == 0

        blocker_preview_request = request(
            "preview_role_change", last_super_admin.id, "viewer", name="blocker_preview"
        )
        blocker_preview, _ = await asyncio.to_thread(
            _node_response, identity, control, blocker_preview_request
        )
        assert blocker_preview["ok"] is True
        impact = blocker_preview["data"]
        assert impact["blockers"] == [
            "不能降级最后一名超级管理员",
            "账号仍持有标注/质检项目职责，请先在项目中变更职责",
            "账号仍是项目负责人，请先转移项目后变更平台角色",
            "账号仍有未完成的标注/质检工作，请先交接",
        ]
        assert [project["project_name"] for project in impact["projects"]] == [
            blocker_a.name,
            blocker_z.name,
        ]
        assert [project["membership_role"] for project in impact["projects"]] == [
            "viewer",
            "annotator",
        ]
        assert impact["assigned_batch_count"] == 3
        assert impact["assigned_task_count"] == 2
        assert impact["review_task_count"] == 2
        assert impact["other_project_count"] == 0
        assert impact["can_change"] is False

        blocker_operation = request(
            "change_platform_role", last_super_admin.id, "viewer", name="blocker_change"
        )
        before_blocked_user = await _user_snapshot(maker, last_super_admin.id)
        before_blocked_members = await _membership_rows(
            maker, [blocker_a.id, blocker_z.id]
        )
        before_blocked_tasks = await _task_rows(maker, [blocker_z.id])
        blocked = await asyncio.to_thread(_node, identity, control, blocker_operation)
        assert blocked["ok"] is False
        assert blocked["error"]["code"] == "platform_role_change_blocked"
        assert blocked["error"]["status_code"] == 409
        assert blocked["error"]["detail"] == {
            "reason": "platform_role_change_blocked",
            "blockers": impact["blockers"][1:],
        }
        assert await _user_snapshot(maker, last_super_admin.id) == before_blocked_user
        assert (
            await _membership_rows(maker, [blocker_a.id, blocker_z.id])
            == before_blocked_members
        )
        assert await _task_rows(maker, [blocker_z.id]) == before_blocked_tasks
        assert (
            await _receipts(test_engine, [uuid.UUID(blocker_operation["operation_id"])])
            == {}
        )

        self_preview = await asyncio.to_thread(
            _node,
            identity,
            control,
            request("preview_role_change", actor_id, "viewer", name="self_preview"),
        )
        assert self_preview["data"]["blockers"][0] == "不能修改自己的角色"
        same_role_preview = await asyncio.to_thread(
            _node,
            identity,
            control,
            request(
                "preview_role_change",
                cycle_target.id,
                "employee",
                name="same_role_preview",
            ),
        )
        assert same_role_preview["data"]["blockers"] == ["目标角色与当前角色相同"]

        invalid_role = await asyncio.to_thread(
            _node,
            identity,
            control,
            request(
                "preview_role_change",
                cycle_target.id,
                "future_role",
                name="invalid_role",
            ),
        )
        assert invalid_role["ok"] is False
        assert invalid_role["error"]["code"] == "invalid_platform_role"
        assert invalid_role["error"]["detail"] == "非法平台角色: future_role"

        # A no-op mutation is accepted once, creates a frozen receipt without
        # an Audit, and avoids the owned-Project lock even while that row is held.
        noop_request = request(
            "change_platform_role", noop_target.id, "employee", name="noop"
        )
        project_connection = await test_engine.connect()
        project_transaction = await project_connection.begin()
        try:
            await project_connection.execute(
                text("SELECT id FROM public.projects WHERE id=:id FOR UPDATE"),
                {"id": noop_project.id},
            )
            noop_result, _ = await asyncio.to_thread(
                _node_response, identity, control, noop_request
            )
            assert noop_result["ok"] is True
        finally:
            await project_transaction.rollback()
            await project_connection.close()
        noop_operation = uuid.UUID(noop_request["operation_id"])
        noop_receipt = (await _receipts(test_engine, [noop_operation]))[noop_operation]
        assert noop_receipt["audit_id"] is None
        assert await _audit_count_for_user(maker, actor_id, noop_target.id) == 0
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET email=:email, name='No-op profile changed' WHERE id=:id"
                ),
                {"email": f"noop-renamed-{suffix}@example.test", "id": noop_target.id},
            )
        noop_replay_request = {
            **noop_request,
            "audit_context": {
                **noop_request["audit_context"],
                "path": "/acceptance/noop-replay-after-profile-change",
                "request_id": str(uuid.uuid4()),
            },
        }
        project_connection = await test_engine.connect()
        project_transaction = await project_connection.begin()
        try:
            await project_connection.execute(
                text("SELECT id FROM public.projects WHERE id=:id FOR UPDATE"),
                {"id": noop_project.id},
            )
            noop_replay = await asyncio.to_thread(
                _node, identity, control, noop_replay_request
            )
        finally:
            await project_transaction.rollback()
            await project_connection.close()
        assert noop_replay["data"]["user"] == noop_result["data"]["user"]
        assert (await _user_snapshot(maker, noop_target.id))["email"] != noop_result[
            "data"
        ]["user"]["email"]
        noop_second_request = request(
            "change_platform_role", noop_target.id, "employee", name="noop_second"
        )
        noop_second = await asyncio.to_thread(
            _node, identity, control, noop_second_request
        )
        assert noop_second["ok"] is True
        noop_receipts = await _receipts(
            test_engine,
            [noop_operation, uuid.UUID(noop_second_request["operation_id"])],
        )
        assert len(noop_receipts) == 2
        assert {row["audit_id"] for row in noop_receipts.values()} == {None}
        assert await _audit_count_for_user(maker, actor_id, noop_target.id) == 0

        # A real role change to a manager role still fences Project ownership.
        project_busy_request = request(
            "change_platform_role",
            project_busy_target.id,
            "project_admin",
            name="project_busy",
        )
        project_connection = await test_engine.connect()
        project_transaction = await project_connection.begin()
        try:
            await project_connection.execute(
                text("SELECT id FROM public.projects WHERE id=:id FOR UPDATE"),
                {"id": project_busy_project.id},
            )
            project_busy = await asyncio.to_thread(
                _node, identity, control, project_busy_request
            )
            assert project_busy["ok"] is False
            assert project_busy["error"]["code"] == "project_busy"
            assert project_busy["error"]["detail"] == {"reason": "project_busy"}
        finally:
            await project_transaction.rollback()
            await project_connection.close()
        project_success = await asyncio.to_thread(
            _node, identity, control, project_busy_request
        )
        assert project_success["ok"] is True
        assert project_success["data"]["user"]["role"] == "project_admin"
        assert (await _project_rows(maker, [project_busy_project.id]))[0][
            1
        ] == project_busy_target.id

        account_busy_request = request(
            "change_platform_role",
            account_busy_target.id,
            "viewer",
            name="account_busy",
        )
        account_connection = await test_engine.connect()
        account_transaction = await account_connection.begin()
        try:
            await account_connection.execute(
                text("SELECT id FROM public.users WHERE id=:id FOR UPDATE"),
                {"id": account_busy_target.id},
            )
            account_busy = await asyncio.to_thread(
                _node, identity, control, account_busy_request
            )
            assert account_busy["ok"] is False
            assert account_busy["error"]["code"] == "account_busy"
        finally:
            await account_transaction.rollback()
            await account_connection.close()
        account_success = await asyncio.to_thread(
            _node, identity, control, account_busy_request
        )
        assert account_success["ok"] is True

        cycle_b_request = request(
            "change_platform_role", cycle_target.id, "viewer", name="cycle_b"
        )
        lost_response = await asyncio.to_thread(
            _node,
            identity,
            control,
            cycle_b_request,
            discard=True,
        )
        assert lost_response == {"ok": True, "response_discarded": True}
        cycle_b_operation = uuid.UUID(cycle_b_request["operation_id"])
        cycle_b_receipt = (await _receipts(test_engine, [cycle_b_operation]))[
            cycle_b_operation
        ]
        cycle_b_audit_id = cycle_b_receipt["audit_id"]
        cycle_b_audits = await _audit_rows(maker, [cycle_b_audit_id])
        assert len(cycle_b_audits) == 1
        assert cycle_b_audits[0]["action"] == "user.role_change"
        assert cycle_b_audits[0]["detail_json"] == {
            "email": cycle_target.email,
            "old": "employee",
            "new": "viewer",
        }

        old_origin = identity.origin
        old_port = int(old_origin.rsplit(":", 1)[1])
        identity.stop()
        _assert_no_secret_log(
            tmp_path / f"identity-{old_port}.log",
            [
                secret,
                control,
                actor_token,
                reserved_actor_token,
                actor_api_key_value,
                cycle_api_key_value,
            ],
        )
        identity = _start_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            port=old_port,
            enable_keys=True,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_invitations=False,
            enable_directory=False,
            enable_admin_password=False,
            enable_platform_role=True,
            enable_deactivation=False,
            enable_presence=False,
        )
        cycle_b_frozen, cycle_b_raw = await asyncio.to_thread(
            _node_response, identity, control, cycle_b_request
        )
        assert cycle_b_frozen["ok"] is True
        assert cycle_b_frozen["data"]["user"]["role"] == "viewer"
        assert '"big_integer":9007199254740993' in cycle_b_raw

        cycle_a_request = request(
            "change_platform_role", cycle_target.id, "employee", name="cycle_a"
        )
        cycle_a_result = await asyncio.to_thread(
            _node, identity, control, cycle_a_request
        )
        assert cycle_a_result["data"]["user"]["role"] == "employee"
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET email=:email, name='Cycle profile changed', "
                    "preferences=CAST(:preferences AS jsonb) WHERE id=:id"
                ),
                {
                    "email": f"cycle-renamed-{suffix}@example.test",
                    "preferences": '{"opaque":{"big_integer":42,"new":true}}',
                    "id": cycle_target.id,
                },
            )
        cycle_b_replay_request = {
            **cycle_b_request,
            "audit_context": {
                **cycle_b_request["audit_context"],
                "path": "/acceptance/cycle-b-replay-after-a",
                "request_id": str(uuid.uuid4()),
            },
        }
        cycle_b_replay = await asyncio.to_thread(
            _node, identity, control, cycle_b_replay_request
        )
        assert cycle_b_replay["data"]["user"] == cycle_b_frozen["data"]["user"]
        current_cycle = await _user_snapshot(maker, cycle_target.id)
        assert current_cycle["role"] == "employee"
        assert current_cycle["email"] == f"cycle-renamed-{suffix}@example.test"
        assert await _receipts(test_engine, [cycle_b_operation]) == {
            cycle_b_operation: cycle_b_receipt
        }
        assert len(await _audit_rows(maker, [cycle_b_audit_id])) == 1

        conflict_request = {
            **cycle_b_request,
            "payload": {"target_user_id": str(cycle_target.id), "role": "employee"},
        }
        payload_conflict = await asyncio.to_thread(
            _node, identity, control, conflict_request
        )
        assert payload_conflict["ok"] is False
        assert payload_conflict["error"]["code"] == "operation_conflict"
        actor_conflict_request = {
            **cycle_b_request,
            "token": reserved_actor_token,
        }
        actor_conflict = await asyncio.to_thread(
            _node, identity, control, actor_conflict_request
        )
        assert actor_conflict["ok"] is False
        assert actor_conflict["error"]["code"] == "operation_conflict"

        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET is_active=false WHERE id=:id"),
                {"id": cycle_target.id},
            )
        inactive_replay = await asyncio.to_thread(
            _node, identity, control, cycle_b_request
        )
        assert inactive_replay["ok"] is False
        assert inactive_replay["error"]["code"] == "target_not_found"
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET is_active=true WHERE id=:id"),
                {"id": cycle_target.id},
            )

        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET role='viewer' WHERE id=:id"),
                {"id": actor_id},
            )
        demoted_actor_replay = await asyncio.to_thread(
            _node, identity, control, cycle_b_request
        )
        assert demoted_actor_replay["ok"] is False
        assert demoted_actor_replay["error"]["code"] == "super_admin_required"
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET role='super_admin' WHERE id=:id"),
                {"id": actor_id},
            )
            await db.execute(
                text("UPDATE public.users SET credential_generation=1 WHERE id=:id"),
                {"id": actor_id},
            )
        stale_credential_replay = await asyncio.to_thread(
            _node, identity, control, cycle_b_request
        )
        assert stale_credential_replay["ok"] is False
        assert stale_credential_replay["error"]["code"] == "generation_outdated"
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET credential_generation=0 WHERE id=:id"),
                {"id": actor_id},
            )

        assert await _generation_map(maker, user_ids) == initial_generations
        assert (
            await _key_rows(maker, [actor_id, cycle_target.id]) == initial_api_key_rows
        )
        assert await _project_rows(maker, project_scope_ids) == initial_project_rows
        assert (
            await _membership_rows(maker, project_scope_ids) == initial_membership_rows
        )
        assert await _task_rows(maker, project_scope_ids) == initial_task_rows
        assert await _batch_rows(maker, project_scope_ids) == initial_batch_rows

        # Work blockers stay ahead of the last-unreserved-super-admin guard.
        async with maker.begin() as db:
            await db.execute(delete(Task).where(Task.project_id == blocker_z.id))
            await db.execute(
                delete(TaskBatch).where(TaskBatch.project_id == blocker_z.id)
            )
            await db.execute(
                delete(ProjectMember).where(
                    ProjectMember.project_id.in_([blocker_a.id, blocker_z.id])
                )
            )
            await db.execute(
                text("UPDATE public.projects SET owner_id=:owner WHERE id=:project"),
                {"owner": actor_id, "project": blocker_z.id},
            )
        final_preview = await asyncio.to_thread(
            _node,
            identity,
            control,
            request(
                "preview_role_change",
                last_super_admin.id,
                "viewer",
                name="last_sa_preview",
            ),
        )
        assert final_preview["data"]["blockers"] == ["不能降级最后一名超级管理员"]
        last_sa_change = await asyncio.to_thread(
            _node,
            identity,
            control,
            request(
                "change_platform_role",
                last_super_admin.id,
                "viewer",
                name="last_sa_mutation",
            ),
        )
        assert last_sa_change["ok"] is False
        assert last_sa_change["error"]["code"] == "last_super_admin"

        # Verify role writes did not cascade into ownership, memberships, tasks,
        # batches, credential generations or API-key status.
        role_projects = [
            blocker_a.id,
            blocker_z.id,
            noop_project.id,
            project_busy_project.id,
            *(project.id for project in sample_projects),
        ]
        projects_before = await _project_rows(maker, role_projects)
        memberships_before = await _membership_rows(maker, role_projects)
        tasks_before = await _task_rows(maker, role_projects)
        batches_before = await _batch_rows(maker, role_projects)
        users_before = await asyncio.gather(
            *(
                _user_snapshot(maker, user_id)
                for user_id in [
                    actor_id,
                    cycle_target.id,
                    noop_target.id,
                    account_busy_target.id,
                ]
            )
        )
        keys_before = await _key_rows(maker, [actor_id, cycle_target.id])

        # Finite local host-process sample only. Container resource proof is a
        # separate gate; these measurements make no production-capacity claim.
        load_operation_ids = [uuid.uuid4() for _ in sample_targets]
        load_requests = [
            _platform_role_request(
                "change_platform_role",
                actor_token,
                target.id,
                "project_admin",
                operation_id=operation_id,
            )
            for target, operation_id in zip(
                sample_targets, load_operation_ids, strict=True
            )
        ]
        operation_ids.extend(load_operation_ids)
        concurrency = 4
        semaphore = asyncio.Semaphore(concurrency)
        started = time.perf_counter()
        load_calls = await _gather_all(
            *(
                _node_with_busy_retry(identity, control, item, semaphore)
                for item in load_requests
            )
        )
        batch_elapsed_ms = (time.perf_counter() - started) * 1000
        for target, result in zip(sample_targets, load_calls, strict=True):
            user = result["result"]["data"]["user"]
            assert user["id"] == str(target.id)
            assert user["role"] == "project_admin"
        load_receipts = await _receipts(test_engine, load_operation_ids)
        assert set(load_receipts) == set(load_operation_ids)
        load_audit_ids = [receipt["audit_id"] for receipt in load_receipts.values()]
        assert len(load_audit_ids) == 12 and len(set(load_audit_ids)) == 12
        load_audits = await _audit_rows(maker, load_audit_ids)
        assert len(load_audits) == 12
        written_states = await _gather_all(
            *(_user_snapshot(maker, target.id) for target in sample_targets)
        )
        replay_started = time.perf_counter()
        replay_calls = await _gather_all(
            *(
                _node_with_busy_retry(identity, control, item, semaphore)
                for item in load_requests
            )
        )
        replay_elapsed_ms = (time.perf_counter() - replay_started) * 1000
        assert [item["result"] for item in replay_calls] == [
            item["result"] for item in load_calls
        ]
        assert await _receipts(test_engine, load_operation_ids) == load_receipts
        assert await _audit_rows(maker, load_audit_ids) == load_audits
        replay_states = await _gather_all(
            *(_user_snapshot(maker, target.id) for target in sample_targets)
        )
        assert replay_states == written_states
        projects_after = await _project_rows(maker, role_projects)
        memberships_after = await _membership_rows(maker, role_projects)
        tasks_after = await _task_rows(maker, role_projects)
        batches_after = await _batch_rows(maker, role_projects)
        users_after = await asyncio.gather(
            *(
                _user_snapshot(maker, user_id)
                for user_id in [
                    actor_id,
                    cycle_target.id,
                    noop_target.id,
                    account_busy_target.id,
                ]
            )
        )
        keys_after = await _key_rows(maker, [actor_id, cycle_target.id])
        assert projects_after == projects_before
        assert memberships_after == memberships_before
        assert tasks_after == tasks_before
        assert batches_after == batches_before
        assert users_after == users_before
        assert keys_after == keys_before
        assert await _generation_map(maker, user_ids) == initial_generations
        assert (
            await _key_rows(maker, [actor_id, cycle_target.id]) == initial_api_key_rows
        )
        operation_latencies = [item["latency_ms"] for item in load_calls]
        replay_latencies = [item["latency_ms"] for item in replay_calls]

        def percentiles(values):
            ordered = sorted(values)
            return statistics.median(ordered), ordered[
                math.ceil(0.95 * len(ordered)) - 1
            ]

        p50, p95 = percentiles(operation_latencies)
        replay_p50, replay_p95 = percentiles(replay_latencies)
        record_property(
            "identity_platform_role_host_process_sample",
            json.dumps(
                {
                    "execution_context": "HOST PROCESS",
                    "latency_scope": "Node subprocess and HTTP",
                    "target_count": 12,
                    "concurrency": concurrency,
                    "max_attempts_per_operation": 8,
                    "attempts": sum(item["attempts"] for item in load_calls),
                    "busy_retries": sum(item["retries"] for item in load_calls),
                    "elapsed_ms": round(batch_elapsed_ms, 3),
                    "latency_ms": [round(value, 3) for value in operation_latencies],
                    "p50_ms": round(p50, 3),
                    "p95_ms": round(p95, 3),
                    "replay_attempts": sum(item["attempts"] for item in replay_calls),
                    "replay_busy_retries": sum(
                        item["retries"] for item in replay_calls
                    ),
                    "replay_elapsed_ms": round(replay_elapsed_ms, 3),
                    "replay_latency_ms": [
                        round(value, 3) for value in replay_latencies
                    ],
                    "replay_p50_ms": round(replay_p50, 3),
                    "replay_p95_ms": round(replay_p95, 3),
                    "receipts": len(load_receipts),
                    "audit_rows": len(load_audits),
                    "capacity_claim": "none; bounded host-process sample only",
                },
                sort_keys=True,
            ),
        )

        # Same accepted B operation with another signing-secret epoch fails
        # closed instead of changing the current A role or adding another Audit.
        cycle_before_epoch = await _user_snapshot(maker, cycle_target.id)
        cycle_b_audit_count = len(await _audit_rows(maker, [cycle_b_audit_id]))
        old_origin = identity.origin
        old_port = int(old_origin.rsplit(":", 1)[1])
        identity.stop()
        new_secret = secrets.token_urlsafe(32)
        log_secrets.append(new_secret)
        identity = _start_identity(
            test_engine,
            tmp_path,
            new_secret,
            control,
            port=old_port,
            enable_keys=True,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_invitations=False,
            enable_directory=False,
            enable_admin_password=False,
            enable_platform_role=True,
            enable_deactivation=False,
            enable_presence=False,
        )
        new_actor_token = _role_token(actor_id, "super_admin", new_secret)
        epoch_conflict_request = {
            **cycle_b_request,
            "token": new_actor_token,
        }
        epoch_conflict = await asyncio.to_thread(
            _node, identity, control, epoch_conflict_request
        )
        assert epoch_conflict["ok"] is False
        assert epoch_conflict["error"]["code"] == "operation_conflict"
        assert await _user_snapshot(maker, cycle_target.id) == cycle_before_epoch
        assert len(await _audit_rows(maker, [cycle_b_audit_id])) == cycle_b_audit_count
        record_property(
            "platform_role_replay_epoch_fence",
            "same operation UUID after signing-secret epoch change returned operation_conflict",
        )
    finally:
        if identity:
            old_port = int(identity.origin.rsplit(":", 1)[1])
            _assert_no_secret_log(tmp_path / f"identity-{old_port}.log", log_secrets)
            identity.stop()
        await _cleanup_platform_roles(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
            operation_ids=operation_ids,
        )
