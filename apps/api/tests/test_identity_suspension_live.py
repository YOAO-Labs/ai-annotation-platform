"""Installed Identity owner acceptance for administrator suspension and reactivation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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

from aap_identity.recovery_crypto import token_hash
from aap_identity.security import create_access_token, decode_access_token
from aap_identity.directory_contract import DirectoryUserRecord
from app.db.models.api_key import ApiKey
from app.db.models.audit_log import AuditLog
from app.db.models.notification import Notification
from app.db.models.password_reset_token import PasswordResetToken
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_api_key, create_membership, create_project, create_user
from tests.identity_recovery_helpers import seed_challenge
from tests.test_identity_admission_live import (
    ROOT,
    _node_peer,
    _start_identity,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_SUSPENSION_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity suspension verifier",
    ),
]


def _suspension_request(
    operation, token, target_id, *, reason=None, operation_id=None, request_id=None
):
    operation_id = operation_id or uuid.uuid4()
    request_id = request_id or uuid.uuid4()
    action = "deactivate" if operation == "suspend_account" else "reactivate"
    payload = {"target_user_id": str(target_id)}
    if operation == "reactivate_account":
        payload["reason"] = reason
    return {
        "schema": "aap-identity-suspensions/v1",
        "operation": operation,
        "operation_id": str(operation_id),
        "token": token,
        "audit_context": {
            "method": "POST",
            "path": f"/api/v1/users/{target_id}/{action}",
            "ip": "127.0.0.1",
            "request_id": str(request_id),
        },
        "payload": payload,
    }


def _node_response(identity, control, request, *, discard=False, timeout=120):
    peer = ROOT / "apps/api/tests/fixtures/identity_suspensions_peer.mjs"
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
    assert result.returncode == 0, "Independent Node suspension consumer failed"
    secrets_to_hide = [control, request["token"]]
    reason = request["payload"].get("reason")
    if isinstance(reason, str) and len(reason) >= 8:
        secrets_to_hide.append(reason)
    assert all(value not in result.stdout for value in secrets_to_hide)
    assert all(value not in result.stderr for value in secrets_to_hide)
    return json.loads(result.stdout), result.stdout


def _node(identity, control, request, *, discard=False, timeout=120):
    return _node_response(identity, control, request, discard=discard, timeout=timeout)[
        0
    ]


def _deactivation_request(token, *, operation_id=None, reason=None):
    operation_id = operation_id or uuid.uuid4()
    return {
        "schema": "aap-identity-deactivation/v1",
        "operation": "request_self_deactivation",
        "operation_id": str(operation_id),
        "token": token,
        "audit_context": {
            "method": "POST",
            "path": "/api/v1/auth/me/deactivation-request",
            "ip": "127.0.0.1",
            "request_id": str(uuid.uuid4()),
        },
        "payload": {"reason": reason},
    }


def _node_deactivation(identity, control, request):
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
    assert result.returncode == 0, "Existing self-deactivation consumer failed"
    assert (
        request["token"] not in result.stdout and request["token"] not in result.stderr
    )
    return json.loads(result.stdout)


def _user_token(user, secret):
    return create_access_token(
        str(user.id),
        user.role,
        secret=secret,
        expire_minutes=15,
        gen=user.credential_generation,
    )


def _start_suspension_identity(test_engine, tmp_path, secret, control, *, port=None):
    return _start_identity(
        test_engine,
        tmp_path,
        secret,
        control,
        port=port,
        enable_keys=True,
        enable_sessions=False,
        enable_accounts=False,
        enable_groups=False,
        enable_invitations=False,
        enable_directory=False,
        enable_admin_password=False,
        enable_platform_role=False,
        enable_suspension=True,
        enable_deactivation=True,
        enable_presence=False,
    )


async def _cleanup_suspensions(
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
                "DELETE FROM public.identity_suspension_operation_receipts "
                "WHERE actor_id=ANY(CAST(:users AS uuid[])) "
                "OR target_user_id=ANY(CAST(:users AS uuid[])) "
                "OR operation_id=ANY(CAST(:operations AS uuid[]))"
            ),
            {"users": users, "operations": operations},
        )
        if deactivation_operations:
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
            delete(PasswordResetToken).where(PasswordResetToken.user_id.in_(users))
        )
        await db.execute(delete(ApiKey).where(ApiKey.user_id.in_(users)))
        await db.execute(
            delete(Notification).where(
                Notification.user_id.in_(users) | Notification.target_id.in_(users)
            )
        )
        if projects:
            await db.execute(
                delete(ProjectMember).where(
                    ProjectMember.user_id.in_(users)
                    | ProjectMember.project_id.in_(projects)
                )
            )
            await db.execute(delete(Project).where(Project.id.in_(projects)))
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
                    "WHERE jti=ANY(CAST(:jtis AS uuid[]))"
                ),
                {"jtis": [uuid.UUID(str(value)) for value in jtis]},
            )
        await db.execute(delete(User).where(User.id.in_(users)))


async def _lifecycle_state(maker, user_id):
    async with maker() as db:
        row = (
            (
                await db.execute(
                    text(
                        "SELECT id,email,name,role,is_active,status,disabled_kind,disabled_at,"
                        "disabled_by,disabled_reason,deactivation_requested_at,"
                        "deactivation_scheduled_at,deactivation_reason,credential_generation "
                        "FROM public.users WHERE id=:id"
                    ),
                    {"id": user_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


def _canonical_user(value):
    return DirectoryUserRecord.model_validate_json(
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    ).model_dump(mode="json")


async def _api_key_rows(maker, user_id):
    async with maker() as db:
        return (
            await db.execute(
                select(ApiKey.id, ApiKey.key_hash, ApiKey.revoked_at)
                .where(ApiKey.user_id == user_id)
                .order_by(ApiKey.id)
            )
        ).all()


async def _api_key_state(maker, key_id):
    async with maker() as db:
        key = await db.get(ApiKey, key_id)
        await db.refresh(key)
        return key.revoked_at


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


async def _password_reset_state(maker, raw_token):
    async with maker() as db:
        return await db.scalar(
            select(PasswordResetToken.used_at).where(
                PasswordResetToken.token_hash == token_hash(raw_token)
            )
        )


async def _receipts(engine, operation_ids):
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text(
                    "SELECT operation_id,actor_id,operation,target_user_id,"
                    "request_fingerprint,derivation_id,target_generation,target_is_active,"
                    "result,audit_id,created_at "
                    "FROM public.identity_suspension_operation_receipts "
                    "WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": operation_ids},
            )
        ).mappings()
        return {row["operation_id"]: dict(row) for row in rows}


async def _audit_rows(maker, audit_ids):
    if not audit_ids:
        return []
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


async def _audit_count(maker, actor_id, target_id, action):
    async with maker() as db:
        return await db.scalar(
            text(
                "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor "
                "AND target_id=:target AND action=:action"
            ),
            {"actor": actor_id, "target": str(target_id), "action": action},
        )


async def _auth_result(identity, control, credential):
    return await asyncio.to_thread(_node_peer, identity.origin, control, credential)


async def _gather_all(*awaitables):
    results = await asyncio.gather(*awaitables, return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        raise failures[0]
    return results


async def _node_with_busy_retry(
    identity, control, request, semaphore, *, max_attempts=8
):
    started = time.perf_counter()
    attempts = retries = 0
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
        if result.get("error", {}).get("code") not in {
            "account_busy",
            "credential_busy",
            "project_busy",
        }:
            pytest.fail("Suspension host sample returned a non-retryable failure")
        if attempts == max_attempts:
            pytest.fail("Suspension host sample exhausted bounded retries")
        retries += 1
        await asyncio.sleep(min(0.025 * 2 ** (retries - 1), 0.2))
    pytest.fail("Suspension host sample did not complete")


def _percentiles(values):
    ordered = sorted(values)
    return statistics.median(ordered), ordered[math.ceil(0.95 * len(ordered)) - 1]


async def test_installed_suspension_replay_reservation_credentials_and_host_sample(
    test_engine, tmp_path, record_property
):
    maker, reservation_target_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    user_ids = [reservation_target_id]
    project_ids = []
    operation_ids = []
    deactivation_operation_ids = []
    jtis = []
    identity = None
    log_secrets = [secret, control]
    async with maker.begin() as db:
        reservation_target = await db.get(User, reservation_target_id)
        manager = await create_user(
            db,
            "super_admin",
            f"suspension-owner-manager-{suffix}@example.test",
            "Suspension manager",
        )
        account_busy_target = await create_user(
            db,
            "employee",
            f"suspension-account-busy-{suffix}@example.test",
            "Account lock target",
        )
        project_busy_target = await create_user(
            db,
            "employee",
            f"suspension-project-busy-{suffix}@example.test",
            "Project lock target",
        )
        large_target = await create_user(
            db,
            "employee",
            f"suspension-large-profile-{suffix}@example.test",
            "Large profile target",
        )
        project = await create_project(
            db, owner_id=manager.id, name=f"Suspension reservation project {suffix}"
        )
        await create_membership(
            db,
            project_id=project.id,
            user_id=reservation_target.id,
            role="annotator",
            assigned_by=manager.id,
        )
        busy_project = await create_project(
            db, owner_id=manager.id, name=f"Suspension busy project {suffix}"
        )
        await create_membership(
            db,
            project_id=busy_project.id,
            user_id=project_busy_target.id,
            role="annotator",
            assigned_by=manager.id,
        )
        large_preferences = json.dumps(
            {
                "opaque": {
                    "wide": 9007199254740993,
                    "decimal": 0.1234567890123456789,
                    "token": "preference token",
                    "blob": "x" * 80_000,
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert json.loads(large_preferences)["opaque"]["wide"] == 9007199254740993
        await db.execute(
            text(
                "UPDATE public.users SET preferences=CAST(:value AS jsonb) WHERE id=:id"
            ),
            {"value": large_preferences, "id": large_target.id},
        )
        _, target_key_plaintext = await create_api_key(
            db, reservation_target.id, "Suspension old target key", ["*"]
        )
        old_reset_token = await seed_challenge(
            db, reservation_target, token=f"suspension-old-reset-{suffix}"
        )
        sample_targets = []
        for index in range(12):
            sample_targets.append(
                await create_user(
                    db,
                    "employee",
                    f"suspension-host-sample-{suffix}-{index}@example.test",
                    f"Host suspension target {index}",
                )
            )

    user_ids.extend(
        [
            manager.id,
            account_busy_target.id,
            project_busy_target.id,
            large_target.id,
            *(target.id for target in sample_targets),
        ]
    )
    project_ids.extend([project.id, busy_project.id])
    initial_project_rows = await _project_rows(maker, project_ids)
    initial_membership_rows = await _membership_rows(maker, project_ids)
    target_token = _user_token(reservation_target, secret)
    manager_token = _user_token(manager, secret)
    target_jti = uuid.UUID(decode_access_token(target_token, secret=secret)["jti"])
    jtis.append(target_jti)
    log_secrets.extend(
        [target_token, manager_token, target_key_plaintext, old_reset_token]
    )
    try:
        identity = _start_suspension_identity(test_engine, tmp_path, secret, control)
        accepted_reason = f"accepted reservation {suffix}"
        accepted_request = _deactivation_request(
            target_token,
            reason=accepted_reason,
        )
        log_secrets.append(accepted_reason)
        accepted_operation_id = uuid.UUID(accepted_request["operation_id"])
        deactivation_operation_ids.append(accepted_operation_id)
        accepted = await asyncio.to_thread(
            _node_deactivation, identity, control, accepted_request
        )
        assert accepted["ok"] is True
        accepted_lifecycle = await _lifecycle_state(maker, reservation_target_id)
        reservation = (
            accepted_lifecycle["deactivation_requested_at"],
            accepted_lifecycle["deactivation_scheduled_at"],
            accepted_lifecycle["deactivation_reason"],
        )
        assert all(value is not None for value in reservation)
        assert (await _auth_result(identity, control, target_token))["ok"] is True
        assert (await _auth_result(identity, control, target_key_plaintext))[
            "ok"
        ] is True

        # A pending self-deactivation reservation does not count as another
        # unreserved super-admin when reducing the only remaining admin.
        last_admin_request = _suspension_request(
            "suspend_account", target_token, manager.id
        )
        last_admin_operation = uuid.UUID(last_admin_request["operation_id"])
        operation_ids.append(last_admin_operation)
        last_admin = await asyncio.to_thread(
            _node, identity, control, last_admin_request
        )
        assert last_admin["ok"] is False
        assert last_admin["error"]["code"] == "last_super_admin"
        assert await _receipts(test_engine, [last_admin_operation]) == {}

        suspend_request = _suspension_request(
            "suspend_account", manager_token, reservation_target_id
        )
        suspend_operation = uuid.UUID(suspend_request["operation_id"])
        operation_ids.append(suspend_operation)
        discarded = await asyncio.to_thread(
            _node, identity, control, suspend_request, discard=True
        )
        assert discarded == {"ok": True, "response_discarded": True}
        suspend_receipt = (await _receipts(test_engine, [suspend_operation]))[
            suspend_operation
        ]
        assert suspend_receipt["target_generation"] == 1
        assert suspend_receipt["target_is_active"] is False
        assert suspend_receipt["audit_id"] is not None
        assert suspend_receipt["result"]["user"]["is_active"] is False
        assert suspend_receipt["result"]["user"]["disabled_kind"] == "suspended"
        assert suspend_receipt["result"]["user"]["disabled_reason"] == "管理员停用账号"
        assert all(
            secret_value
            not in json.dumps(suspend_receipt["result"], ensure_ascii=False)
            for secret_value in [target_token, target_key_plaintext, old_reset_token]
        )
        suspend_audit = await _audit_rows(maker, [suspend_receipt["audit_id"]])
        assert len(suspend_audit) == 1
        assert suspend_audit[0]["action"] == "user.deactivate"
        assert suspend_audit[0]["target_type"] == "user"
        assert suspend_audit[0]["target_id"] == str(reservation_target_id)
        assert suspend_audit[0]["status_code"] == 200
        assert suspend_audit[0]["detail_json"] == {"email": accepted_lifecycle["email"]}
        state_after_suspend = await _lifecycle_state(maker, reservation_target_id)
        assert state_after_suspend["credential_generation"] == 1
        assert state_after_suspend["is_active"] is False
        assert state_after_suspend["status"] == "offline"
        assert state_after_suspend["disabled_kind"] == "suspended"
        assert state_after_suspend["disabled_reason"] == "管理员停用账号"
        assert (
            state_after_suspend["deactivation_requested_at"],
            state_after_suspend["deactivation_scheduled_at"],
            state_after_suspend["deactivation_reason"],
        ) == reservation
        assert (await _api_key_rows(maker, reservation_target_id))[0][2] is not None
        assert await _password_reset_state(maker, old_reset_token) is not None
        assert (await _auth_result(identity, control, target_token))["ok"] is False
        assert (await _auth_result(identity, control, target_key_plaintext))[
            "ok"
        ] is False

        port = int(identity.origin.rsplit(":", 1)[1])
        old_log = tmp_path / f"identity-{port}.log"
        if old_log.exists():
            old_contents = old_log.read_text(encoding="utf-8", errors="replace")
            assert all(value not in old_contents for value in log_secrets)
        identity.stop()
        identity = _start_suspension_identity(
            test_engine, tmp_path, secret, control, port=port
        )
        replayed_suspend, _ = await asyncio.to_thread(
            _node_response, identity, control, suspend_request
        )
        assert replayed_suspend["ok"] is True
        assert _canonical_user(replayed_suspend["data"]["user"]) == _canonical_user(
            suspend_receipt["result"]["user"]
        )
        assert await _receipts(test_engine, [suspend_operation]) == {
            suspend_operation: suspend_receipt
        }
        assert len(await _audit_rows(maker, [suspend_receipt["audit_id"]])) == 1

        reactivate_request = _suspension_request(
            "reactivate_account",
            manager_token,
            reservation_target_id,
            reason="\u2003\u00a0",
        )
        reactivate_operation = uuid.UUID(reactivate_request["operation_id"])
        operation_ids.append(reactivate_operation)
        project_connection = await test_engine.connect()
        project_transaction = await project_connection.begin()
        try:
            await project_connection.execute(
                text("SELECT id FROM public.projects WHERE id=:id FOR UPDATE"),
                {"id": project.id},
            )
            reactivate_discarded = await asyncio.to_thread(
                _node, identity, control, reactivate_request, discard=True
            )
            assert reactivate_discarded == {"ok": True, "response_discarded": True}
        finally:
            await project_transaction.rollback()
            await project_connection.close()
        reactivate_receipt = (await _receipts(test_engine, [reactivate_operation]))[
            reactivate_operation
        ]
        assert reactivate_receipt["target_generation"] == 2
        assert reactivate_receipt["target_is_active"] is True
        assert reactivate_receipt["audit_id"] is not None
        assert reactivate_receipt["result"]["user"]["is_active"] is True
        assert reactivate_receipt["result"]["user"]["disabled_kind"] is None
        assert reactivate_receipt["result"]["user"]["disabled_reason"] is None
        assert (
            reactivate_receipt["result"]["user"]["deactivation_requested_at"],
            reactivate_receipt["result"]["user"]["deactivation_scheduled_at"],
        ) == (
            suspend_receipt["result"]["user"]["deactivation_requested_at"],
            suspend_receipt["result"]["user"]["deactivation_scheduled_at"],
        )
        assert "reason" not in reactivate_receipt["result"]["user"]
        reactivate_audit = await _audit_rows(maker, [reactivate_receipt["audit_id"]])
        assert len(reactivate_audit) == 1
        assert reactivate_audit[0]["action"] == "user.reactivate"
        assert reactivate_audit[0]["target_id"] == str(reservation_target_id)
        assert reactivate_audit[0]["status_code"] == 200
        assert reactivate_audit[0]["detail_json"] == {"reason_present": False}
        state_after_reactivate = await _lifecycle_state(maker, reservation_target_id)
        assert state_after_reactivate["is_active"] is True
        assert state_after_reactivate["credential_generation"] == 2
        assert (
            state_after_reactivate["deactivation_requested_at"],
            state_after_reactivate["deactivation_scheduled_at"],
            state_after_reactivate["deactivation_reason"],
        ) == reservation

        async with maker.begin() as db:
            new_target_key, new_target_key_plaintext = await create_api_key(
                db, reservation_target_id, "New after reactivation", ["*"]
            )
        log_secrets.append(new_target_key_plaintext)
        async with maker.begin() as db:
            new_reset_token = await seed_challenge(
                db,
                await db.get(User, reservation_target_id),
                token=f"suspension-new-reset-{suffix}",
            )
        log_secrets.append(new_reset_token)
        assert await _password_reset_state(maker, new_reset_token) is None
        assert (await _auth_result(identity, control, new_target_key_plaintext))[
            "ok"
        ] is True
        current_target = await _lifecycle_state(maker, reservation_target_id)
        new_target_token = create_access_token(
            str(reservation_target_id),
            current_target["role"],
            secret=secret,
            expire_minutes=15,
            gen=current_target["credential_generation"],
        )
        assert (await _auth_result(identity, control, new_target_token))["ok"] is True
        log_secrets.append(new_target_token)
        new_key_rows_before_replay = await _api_key_rows(maker, reservation_target_id)

        port = int(identity.origin.rsplit(":", 1)[1])
        identity.stop()
        identity = _start_suspension_identity(
            test_engine, tmp_path, secret, control, port=port
        )
        replayed_reactivate = await asyncio.to_thread(
            _node, identity, control, reactivate_request
        )
        assert _canonical_user(replayed_reactivate["data"]["user"]) == _canonical_user(
            reactivate_receipt["result"]["user"]
        )
        assert (
            await _api_key_rows(maker, reservation_target_id)
            == new_key_rows_before_replay
        )
        assert await _api_key_state(maker, new_target_key.id) is None
        assert await _password_reset_state(maker, new_reset_token) is None
        assert (await _auth_result(identity, control, new_target_key_plaintext))[
            "ok"
        ] is True
        assert await _receipts(test_engine, [reactivate_operation]) == {
            reactivate_operation: reactivate_receipt
        }
        assert len(await _audit_rows(maker, [reactivate_receipt["audit_id"]])) == 1

        later_lifecycle = await asyncio.to_thread(
            _node, identity, control, suspend_request
        )
        assert later_lifecycle["ok"] is False
        assert later_lifecycle["error"]["code"] == "operation_conflict"
        assert (await _lifecycle_state(maker, reservation_target_id))[
            "credential_generation"
        ] == 2
        assert (await _auth_result(identity, control, target_token))["ok"] is False

        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET role='viewer' WHERE id=:id"),
                {"id": manager.id},
            )
        revoked_manager = await asyncio.to_thread(
            _node, identity, control, reactivate_request
        )
        assert revoked_manager["ok"] is False
        assert revoked_manager["error"]["code"] == "manager_required"
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET role='super_admin',credential_generation=1 "
                    "WHERE id=:id"
                ),
                {"id": manager.id},
            )
        stale_manager = await asyncio.to_thread(
            _node, identity, control, reactivate_request
        )
        assert stale_manager["ok"] is False
        assert stale_manager["error"]["code"] == "generation_outdated"
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET credential_generation=0 WHERE id=:id"),
                {"id": manager.id},
            )

        account_busy_request = _suspension_request(
            "suspend_account", manager_token, account_busy_target.id
        )
        account_busy_operation = uuid.UUID(account_busy_request["operation_id"])
        operation_ids.append(account_busy_operation)
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
        assert await _receipts(test_engine, [account_busy_operation]) == {}
        account_success = await asyncio.to_thread(
            _node, identity, control, account_busy_request
        )
        assert account_success["ok"] is True
        account_busy_receipt = (await _receipts(test_engine, [account_busy_operation]))[
            account_busy_operation
        ]
        assert account_busy_receipt["target_generation"] == 1
        assert account_busy_receipt["target_is_active"] is False
        assert account_busy_receipt["audit_id"] is not None

        project_busy_request = _suspension_request(
            "suspend_account", manager_token, project_busy_target.id
        )
        project_busy_operation = uuid.UUID(project_busy_request["operation_id"])
        operation_ids.append(project_busy_operation)
        project_connection = await test_engine.connect()
        project_transaction = await project_connection.begin()
        try:
            await project_connection.execute(
                text("SELECT id FROM public.projects WHERE id=:id FOR UPDATE"),
                {"id": busy_project.id},
            )
            project_busy_result = await asyncio.to_thread(
                _node, identity, control, project_busy_request
            )
            assert project_busy_result["ok"] is False
            assert project_busy_result["error"]["code"] == "project_busy"
            assert project_busy_result["error"]["detail"] == {"reason": "project_busy"}
        finally:
            await project_transaction.rollback()
            await project_connection.close()
        assert await _receipts(test_engine, [project_busy_operation]) == {}
        project_busy_success = await asyncio.to_thread(
            _node, identity, control, project_busy_request
        )
        assert project_busy_success["ok"] is True
        project_busy_receipt = (await _receipts(test_engine, [project_busy_operation]))[
            project_busy_operation
        ]
        assert project_busy_receipt["target_generation"] == 1
        assert project_busy_receipt["target_is_active"] is False
        assert project_busy_receipt["audit_id"] is not None

        large_request = _suspension_request(
            "suspend_account", manager_token, large_target.id
        )
        large_operation = uuid.UUID(large_request["operation_id"])
        operation_ids.append(large_operation)
        large_result, large_raw = await asyncio.to_thread(
            _node_response, identity, control, large_request
        )
        assert large_result["ok"] is True
        assert len(large_raw.encode("utf-8")) > 64 * 1024
        assert '"wide":9007199254740993' in large_raw

        load_operation_ids = [uuid.uuid4() for _ in sample_targets]
        load_requests = [
            _suspension_request(
                "suspend_account",
                manager_token,
                target.id,
                operation_id=operation_id,
            )
            for target, operation_id in zip(
                sample_targets, load_operation_ids, strict=True
            )
        ]
        operation_ids.extend(load_operation_ids)
        concurrency = 4
        semaphore = asyncio.Semaphore(concurrency)
        load_started = time.perf_counter()
        load_calls = await _gather_all(
            *(
                _node_with_busy_retry(identity, control, request, semaphore)
                for request in load_requests
            )
        )
        load_elapsed_ms = (time.perf_counter() - load_started) * 1000
        load_receipts = await _receipts(test_engine, load_operation_ids)
        assert set(load_receipts) == set(load_operation_ids)
        load_audit_ids = [receipt["audit_id"] for receipt in load_receipts.values()]
        assert len(load_audit_ids) == len(set(load_audit_ids)) == 12
        load_audits = await _audit_rows(maker, load_audit_ids)
        assert len(load_audits) == 12
        assert all(
            receipt["target_generation"] == 1 for receipt in load_receipts.values()
        )
        assert all(
            receipt["target_is_active"] is False for receipt in load_receipts.values()
        )

        replay_started = time.perf_counter()
        replay_calls = await _gather_all(
            *(
                _node_with_busy_retry(identity, control, request, semaphore)
                for request in load_requests
            )
        )
        replay_elapsed_ms = (time.perf_counter() - replay_started) * 1000
        assert [item["result"] for item in replay_calls] == [
            item["result"] for item in load_calls
        ]
        assert await _receipts(test_engine, load_operation_ids) == load_receipts
        assert await _audit_rows(maker, load_audit_ids) == load_audits
        assert await _project_rows(maker, project_ids) == initial_project_rows
        assert await _membership_rows(maker, project_ids) == initial_membership_rows
        latencies = [item["latency_ms"] for item in load_calls]
        replay_latencies = [item["latency_ms"] for item in replay_calls]
        p50, p95 = _percentiles(latencies)
        replay_p50, replay_p95 = _percentiles(replay_latencies)
        record_property(
            "identity_suspension_host_process_sample",
            json.dumps(
                {
                    "execution_context": "HOST PROCESS",
                    "latency_scope": "Node subprocess and HTTP",
                    "target_count": 12,
                    "concurrency": concurrency,
                    "max_attempts_per_operation": 8,
                    "attempts": [item["attempts"] for item in load_calls],
                    "busy_retries": [item["retries"] for item in load_calls],
                    "elapsed_ms": round(load_elapsed_ms, 3),
                    "latencies_ms": [round(value, 3) for value in latencies],
                    "p50_ms": round(p50, 3),
                    "p95_ms": round(p95, 3),
                    "replay_attempts": [item["attempts"] for item in replay_calls],
                    "replay_busy_retries": [item["retries"] for item in replay_calls],
                    "replay_elapsed_ms": round(replay_elapsed_ms, 3),
                    "replay_latencies_ms": [
                        round(value, 3) for value in replay_latencies
                    ],
                    "replay_p50_ms": round(replay_p50, 3),
                    "replay_p95_ms": round(replay_p95, 3),
                    "receipts": len(load_receipts),
                    "audit_rows": len(load_audits),
                    "capacity_claim": "none; finite host-process sample only",
                },
                sort_keys=True,
            ),
        )
    finally:
        if identity is not None:
            port = int(identity.origin.rsplit(":", 1)[1])
            log_path = tmp_path / f"identity-{port}.log"
            if log_path.exists():
                log_contents = log_path.read_text(encoding="utf-8", errors="replace")
                assert all(value not in log_contents for value in log_secrets)
            identity.stop()
        await _cleanup_suspensions(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
            operation_ids=operation_ids,
            deactivation_operation_ids=deactivation_operation_ids,
            jtis=jtis,
        )


async def test_installed_suspension_project_admin_scopes_inactive_noop_and_replay_fences(
    test_engine, tmp_path, record_property
):
    maker, super_admin_id = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    user_ids = [super_admin_id]
    project_ids = []
    operation_ids = []
    identity = None
    log_secrets = [secret, control]
    now = datetime.now(UTC)
    async with maker.begin() as db:
        project_admin = await create_user(
            db,
            "project_admin",
            f"suspension-pa-{suffix}@example.test",
            "Project suspension manager",
        )
        empty_target = await create_user(
            db, "employee", f"suspension-empty-{suffix}@example.test", "Empty scope"
        )
        owned_target = await create_user(
            db, "employee", f"suspension-owned-{suffix}@example.test", "Owned scope"
        )
        cross_target = await create_user(
            db, "employee", f"suspension-cross-{suffix}@example.test", "Cross scope"
        )
        inactive_nonemployee = await create_user(
            db,
            "viewer",
            f"suspension-inactive-{suffix}@example.test",
            "Inactive legacy",
        )
        inactive_nonemployee.is_active = False
        inactive_nonemployee.status = "offline"
        inactive_nonemployee.disabled_kind = "deleted"
        inactive_nonemployee.disabled_at = now
        inactive_nonemployee.disabled_by = super_admin_id
        inactive_nonemployee.disabled_reason = "legacy deletion state"
        inactive_cross_target = await create_user(
            db,
            "employee",
            f"suspension-inactive-cross-{suffix}@example.test",
            "Inactive cross scope",
        )
        inactive_cross_target.is_active = False
        inactive_cross_target.status = "offline"
        inactive_cross_target.disabled_kind = "suspended"
        inactive_cross_target.disabled_at = now
        inactive_cross_target.disabled_by = super_admin_id
        inactive_cross_target.disabled_reason = "管理员停用账号"
        nonreactivatable = await create_user(
            db,
            "employee",
            f"suspension-not-reactivatable-{suffix}@example.test",
            "Deleted employee",
        )
        nonreactivatable.is_active = False
        nonreactivatable.status = "offline"
        nonreactivatable.disabled_kind = "deleted"
        nonreactivatable.disabled_at = now
        nonreactivatable.disabled_by = super_admin_id
        nonreactivatable.disabled_reason = "Deleted by legacy workflow"
        role_forbidden_target = await create_user(
            db,
            "viewer",
            f"suspension-role-forbidden-{suffix}@example.test",
            "Viewer target",
        )
        role_forbidden_target.is_active = False
        role_forbidden_target.status = "offline"
        role_forbidden_target.disabled_kind = "suspended"
        role_forbidden_target.disabled_at = now
        role_forbidden_target.disabled_by = super_admin_id
        role_forbidden_target.disabled_reason = "管理员停用账号"
        administrator_target = await create_user(
            db,
            "super_admin",
            f"suspension-pa-superadmin-{suffix}@example.test",
            "Super-admin target",
        )
        pa_project = await create_project(
            db, owner_id=project_admin.id, name=f"PA suspension project {suffix}"
        )
        foreign_project = await create_project(
            db, owner_id=super_admin_id, name=f"Foreign suspension project {suffix}"
        )
        await create_membership(
            db,
            project_id=pa_project.id,
            user_id=owned_target.id,
            role="annotator",
            assigned_by=project_admin.id,
        )
        for target in (cross_target, inactive_cross_target):
            await create_membership(
                db,
                project_id=pa_project.id,
                user_id=target.id,
                role="annotator",
                assigned_by=project_admin.id,
            )
            await create_membership(
                db,
                project_id=foreign_project.id,
                user_id=target.id,
                role="reviewer",
                assigned_by=super_admin_id,
            )
        for target in (inactive_nonemployee,):
            await create_membership(
                db,
                project_id=foreign_project.id,
                user_id=target.id,
                role="annotator",
                assigned_by=super_admin_id,
            )
        for target in (nonreactivatable, role_forbidden_target):
            await create_membership(
                db,
                project_id=pa_project.id,
                user_id=target.id,
                role="annotator",
                assigned_by=project_admin.id,
            )
        _, inactive_key_plaintext = await create_api_key(
            db, inactive_nonemployee.id, "Inactive no-op preservation", ["*"]
        )
        inactive_reset_token = await seed_challenge(
            db, inactive_nonemployee, token=f"suspension-noop-reset-{suffix}"
        )
        _, manager_key_plaintext = await create_api_key(
            db, project_admin.id, "Project suspension manager key", ["*"]
        )

    user_ids.extend(
        [
            empty_target.id,
            owned_target.id,
            cross_target.id,
            inactive_nonemployee.id,
            inactive_cross_target.id,
            nonreactivatable.id,
            role_forbidden_target.id,
            administrator_target.id,
            project_admin.id,
        ]
    )
    project_ids.extend([pa_project.id, foreign_project.id])
    pa_token = _user_token(project_admin, secret)
    log_secrets.extend(
        [
            pa_token,
            manager_key_plaintext,
            inactive_key_plaintext,
            inactive_reset_token,
        ]
    )
    owned_reactivate_reason = f"private owner reason {suffix}"

    def request(operation, target_id, token=pa_token, *, reason=None):
        result = _suspension_request(operation, token, target_id, reason=reason)
        operation_ids.append(uuid.UUID(result["operation_id"]))
        return result

    try:
        identity = _start_suspension_identity(test_engine, tmp_path, secret, control)

        # A project admin can suspend an unassigned employee, while the same
        # empty derived scope is not enough to reactivate an account.
        empty_suspend_request = request(
            "suspend_account", empty_target.id, manager_key_plaintext
        )
        empty_suspend = await asyncio.to_thread(
            _node, identity, control, empty_suspend_request
        )
        assert empty_suspend["ok"] is True
        assert empty_suspend["data"]["user"]["is_active"] is False
        empty_reactivate = await asyncio.to_thread(
            _node, identity, control, request("reactivate_account", empty_target.id)
        )
        assert empty_reactivate["ok"] is False
        assert empty_reactivate["error"]["code"] == "reactivate_not_managed"

        # Owned projects permit both changes. A later ownership transfer fences
        # the frozen reactivation receipt until scope is restored.
        owned_suspend_request = request("suspend_account", owned_target.id)
        owned_suspend = await asyncio.to_thread(
            _node, identity, control, owned_suspend_request
        )
        assert owned_suspend["ok"] is True
        owned_reactivate_request = request(
            "reactivate_account",
            owned_target.id,
            reason=owned_reactivate_reason,
        )
        scope_connection = await test_engine.connect()
        scope_transaction = await scope_connection.begin()
        try:
            await scope_connection.execute(
                text("SELECT id FROM public.projects WHERE id=:id FOR UPDATE"),
                {"id": pa_project.id},
            )
            scope_busy = await asyncio.to_thread(
                _node, identity, control, owned_reactivate_request
            )
            assert scope_busy["ok"] is False
            assert scope_busy["error"]["code"] == "account_busy"
            assert scope_busy["error"]["detail"] == "账号正在更新，请刷新后重试"
        finally:
            await scope_transaction.rollback()
            await scope_connection.close()
        owned_reactivate_id = uuid.UUID(owned_reactivate_request["operation_id"])
        assert await _receipts(test_engine, [owned_reactivate_id]) == {}
        owned_reactivate = await asyncio.to_thread(
            _node, identity, control, owned_reactivate_request
        )
        assert owned_reactivate["ok"] is True
        assert owned_reactivate["data"]["user"]["disabled_reason"] is None
        owned_receipt = (await _receipts(test_engine, [owned_reactivate_id]))[
            owned_reactivate_id
        ]
        assert owned_receipt["audit_id"] is not None
        assert owned_receipt["result"]["user"]["disabled_reason"] is None
        owned_audit = await _audit_rows(maker, [owned_receipt["audit_id"]])
        assert len(owned_audit) == 1
        assert owned_audit[0]["action"] == "user.reactivate"
        assert owned_audit[0]["detail_json"] == {"reason_present": True}
        assert owned_reactivate_reason not in json.dumps(
            owned_audit[0]["detail_json"], ensure_ascii=False
        )
        assert await _password_reset_state(maker, inactive_reset_token) is None
        assert (await _api_key_rows(maker, inactive_nonemployee.id))[0][2] is None

        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.projects SET owner_id=:owner WHERE id=:project"),
                {"owner": super_admin_id, "project": pa_project.id},
            )
        out_of_scope_replay = await asyncio.to_thread(
            _node, identity, control, owned_reactivate_request
        )
        assert out_of_scope_replay["ok"] is False
        assert out_of_scope_replay["error"]["code"] == "reactivate_scope_forbidden"
        assert await _receipts(test_engine, [owned_reactivate_id]) == {
            owned_reactivate_id: owned_receipt
        }
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.projects SET owner_id=:owner WHERE id=:project"),
                {"owner": project_admin.id, "project": pa_project.id},
            )
        restored_scope_replay = await asyncio.to_thread(
            _node, identity, control, owned_reactivate_request
        )
        assert restored_scope_replay == owned_reactivate
        assert len(await _audit_rows(maker, [owned_receipt["audit_id"]])) == 1

        already_active = await asyncio.to_thread(
            _node,
            identity,
            control,
            request("reactivate_account", owned_target.id),
        )
        assert already_active["ok"] is False
        assert already_active["error"]["code"] == "already_active"

        # Cross-project scope is rejected differently for suspension and
        # reactivation; neither failure creates a durable receipt.
        cross_suspend_request = request("suspend_account", cross_target.id)
        cross_suspend = await asyncio.to_thread(
            _node, identity, control, cross_suspend_request
        )
        assert cross_suspend["ok"] is False
        assert cross_suspend["error"]["code"] == "suspend_scope_forbidden"
        cross_suspend_id = uuid.UUID(cross_suspend_request["operation_id"])
        assert await _receipts(test_engine, [cross_suspend_id]) == {}
        cross_reactivate_request = request(
            "reactivate_account", inactive_cross_target.id
        )
        cross_reactivate = await asyncio.to_thread(
            _node, identity, control, cross_reactivate_request
        )
        assert cross_reactivate["ok"] is False
        assert cross_reactivate["error"]["code"] == "reactivate_scope_forbidden"
        cross_reactivate_id = uuid.UUID(cross_reactivate_request["operation_id"])
        assert await _receipts(test_engine, [cross_reactivate_id]) == {}

        # Inactive account handling precedes PA role and scope restrictions.
        inactive_before = await _lifecycle_state(maker, inactive_nonemployee.id)
        noop_audit_count = await _audit_count(
            maker, project_admin.id, inactive_nonemployee.id, "user.deactivate"
        )
        noop_request = request("suspend_account", inactive_nonemployee.id)
        noop = await asyncio.to_thread(_node, identity, control, noop_request)
        assert noop["ok"] is True
        assert noop["data"]["user"]["role"] == "viewer"
        assert noop["data"]["user"]["disabled_kind"] == "deleted"
        noop_id = uuid.UUID(noop_request["operation_id"])
        noop_receipt = (await _receipts(test_engine, [noop_id]))[noop_id]
        assert (
            noop_receipt["target_generation"]
            == inactive_before["credential_generation"]
        )
        assert noop_receipt["target_is_active"] is False
        assert noop_receipt["audit_id"] is None
        assert noop_receipt["result"]["user"]["disabled_kind"] == "deleted"
        assert (
            await _audit_count(
                maker, project_admin.id, inactive_nonemployee.id, "user.deactivate"
            )
            == noop_audit_count
        )
        assert await _password_reset_state(maker, inactive_reset_token) is None
        assert (await _api_key_rows(maker, inactive_nonemployee.id))[0][2] is None
        assert await _lifecycle_state(maker, inactive_nonemployee.id) == inactive_before

        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET is_active=true WHERE id=:id"),
                {"id": inactive_nonemployee.id},
            )
        late_retry = await asyncio.to_thread(_node, identity, control, noop_request)
        assert late_retry["ok"] is False
        assert late_retry["error"]["code"] == "operation_conflict"
        assert (await _lifecycle_state(maker, inactive_nonemployee.id))[
            "is_active"
        ] is True
        assert await _receipts(test_engine, [noop_id]) == {noop_id: noop_receipt}

        # Remaining closed precedence paths are checked on targets whose PA
        # scope is deliberately valid for reactivation.
        forbidden_suspend = await asyncio.to_thread(
            _node,
            identity,
            control,
            request("suspend_account", administrator_target.id),
        )
        assert forbidden_suspend["ok"] is False
        assert forbidden_suspend["error"]["code"] == "suspend_role_forbidden"
        forbidden_reactivate = await asyncio.to_thread(
            _node,
            identity,
            control,
            request("reactivate_account", role_forbidden_target.id),
        )
        assert forbidden_reactivate["ok"] is False
        assert forbidden_reactivate["error"]["code"] == "reactivate_role_forbidden"
        not_reactivatable = await asyncio.to_thread(
            _node,
            identity,
            control,
            request("reactivate_account", nonreactivatable.id),
        )
        assert not_reactivatable["ok"] is False
        assert not_reactivatable["error"]["code"] == "not_reactivatable"

        self_suspend = await asyncio.to_thread(
            _node,
            identity,
            control,
            request("suspend_account", project_admin.id),
        )
        assert self_suspend["ok"] is False
        assert self_suspend["error"]["code"] == "self_suspend"
        self_reactivate = await asyncio.to_thread(
            _node,
            identity,
            control,
            request("reactivate_account", project_admin.id),
        )
        assert self_reactivate["ok"] is False
        assert self_reactivate["error"]["code"] == "self_reactivate"
        missing = await asyncio.to_thread(
            _node, identity, control, request("suspend_account", uuid.uuid4())
        )
        assert missing["ok"] is False
        assert missing["error"]["code"] == "target_not_found"

        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET role='viewer' WHERE id=:id"),
                {"id": project_admin.id},
            )
        manager_revoked = await asyncio.to_thread(
            _node,
            identity,
            control,
            request("suspend_account", owned_target.id),
        )
        assert manager_revoked["ok"] is False
        assert manager_revoked["error"]["code"] == "manager_required"
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET role='project_admin' WHERE id=:id"),
                {"id": project_admin.id},
            )
        record_property(
            "identity_suspension_pa_scope",
            "empty-suspend/nonempty-reactivate,owned and cross-project scope,"
            "inactive-noop-before-role/scope,scope-fenced-replay",
        )
    finally:
        if identity is not None:
            port = int(identity.origin.rsplit(":", 1)[1])
            log_path = tmp_path / f"identity-{port}.log"
            if log_path.exists():
                log_contents = log_path.read_text(encoding="utf-8", errors="replace")
                assert all(value not in log_contents for value in log_secrets)
            identity.stop()
        await _cleanup_suspensions(
            maker,
            user_ids=user_ids,
            project_ids=project_ids,
            operation_ids=operation_ids,
        )
