"""Installed Identity owner tests for administrator password resets."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import os
import secrets
import subprocess
import uuid

import pytest
from sqlalchemy import delete, text

from aap_identity.security import create_access_token
from app.core.security import hash_password, verify_password
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.user import User
from tests.factory import create_membership, create_project, create_user
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
        or os.getenv("AAP_IDENTITY_ADMIN_PASSWORD_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity administrator-password verifier",
    ),
]


def _password_request(token, target_id, *, operation_id=None, request_id=None):
    operation_id = operation_id or uuid.uuid4()
    request_id = request_id or uuid.uuid4()
    return {
        "schema": "aap-identity-admin-passwords/v1",
        "operation": "admin_reset_password",
        "operation_id": str(operation_id),
        "token": token,
        "audit_context": {
            "method": "POST",
            "path": f"/api/v1/users/{target_id}/admin-reset-password",
            "ip": "127.0.0.1",
            "request_id": str(request_id),
        },
        "payload": {"target_user_id": str(target_id)},
    }


def _node(identity, control, signing_secret, request, *, discard=False):
    peer = ROOT / "apps/api/tests/fixtures/identity_admin_password_peer.mjs"
    result = subprocess.run(
        ["node", str(peer)],
        input=json.dumps(
            {
                "mode": "discard" if discard else "call",
                "url": identity.origin,
                "control_token": control,
                "signing_secret": signing_secret,
                "request": request,
            },
            ensure_ascii=False,
            allow_nan=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        "Independent Node administrator-password consumer failed"
    )
    assert signing_secret not in result.stdout and signing_secret not in result.stderr
    assert control not in result.stdout and control not in result.stderr
    assert (
        request["token"] not in result.stdout and request["token"] not in result.stderr
    )
    return json.loads(result.stdout)


def _auth_token(user, secret):
    return create_access_token(
        str(user.id), user.role, secret=secret, expire_minutes=15
    )


def _assert_no_secret_log(path, values):
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8", errors="replace")
    for value in values:
        if value and value in content:
            pytest.fail("A password or credential appeared in the owned Identity log")


async def _cleanup_admin_password(maker, *, user_ids, project_ids=(), operation_ids=()):
    users = [uuid.UUID(str(value)) for value in user_ids]
    projects = [uuid.UUID(str(value)) for value in project_ids]
    operations = [uuid.UUID(str(value)) for value in operation_ids]
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await db.execute(
            text(
                "DELETE FROM public.identity_admin_password_reset_receipts "
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
            await db.execute(
                delete(ProjectMember).where(ProjectMember.project_id.in_(projects))
            )
            await db.execute(delete(Project).where(Project.id.in_(projects)))
        await db.execute(delete(User).where(User.id.in_(users)))


async def _receipt(engine, operation_id):
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT operation_id, actor_id, target_user_id, operation, "
                        "request_fingerprint, derivation_id, target_email, "
                        "password_admin_reset_at, password_state_fingerprint, audit_id, created_at "
                        "FROM public.identity_admin_password_reset_receipts "
                        "WHERE operation_id=:operation_id"
                    ),
                    {"operation_id": operation_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def _target_state(maker, target_id):
    async with maker() as db:
        user = await db.get(User, target_id)
        await db.refresh(user)
        return user.password_hash, user.password_admin_reset_at, user.email


async def _audit_count(maker, audit_id):
    async with maker() as db:
        return await db.scalar(
            text("SELECT count(*) FROM public.audit_logs WHERE id=:audit_id"),
            {"audit_id": audit_id},
        )


async def test_installed_admin_password_replay_restart_scope_and_fencing(
    test_engine, tmp_path, record_property
):
    from aap_identity.admin_password_manager import (
        admin_password_request_fingerprint,
        derive_admin_password,
    )
    from aap_identity.admin_password_contract import decode_admin_password_request

    maker, super_admin_id = await _world(test_engine)
    signing_secret = secrets.token_urlsafe(32)
    control = secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    actor_ids = [super_admin_id]
    project_ids = []
    operation_ids = []
    identity = None
    super_token = _token(super_admin_id, signing_secret)
    async with maker.begin() as db:
        project_admin = await create_user(
            db,
            "project_admin",
            f"admin-password-pa-{suffix}@example.test",
            "Project administrator",
        )
        reviewer_owned = await create_user(
            db,
            "reviewer",
            f"admin-password-reviewer-owned-{suffix}@example.test",
            "Owned reviewer",
        )
        reviewer_foreign = await create_user(
            db,
            "reviewer",
            f"admin-password-reviewer-foreign-{suffix}@example.test",
            "Foreign reviewer",
        )
        employee = await create_user(
            db,
            "employee",
            f"admin-password-employee-{suffix}@example.test",
            "Unassigned employee",
        )
        inactive_employee = await create_user(
            db,
            "employee",
            f"admin-password-inactive-{suffix}@example.test",
            "Inactive employee",
        )
        inactive_employee.is_active = False
        pa_project = await create_project(
            db, owner_id=project_admin.id, name=f"Admin password owned {suffix}"
        )
        pa_project.status = "archived"
        foreign_project = await create_project(
            db, owner_id=super_admin_id, name=f"Admin password foreign {suffix}"
        )
        await create_membership(
            db,
            project_id=pa_project.id,
            user_id=reviewer_owned.id,
            role="reviewer",
            assigned_by=project_admin.id,
        )
        await create_membership(
            db,
            project_id=foreign_project.id,
            user_id=reviewer_foreign.id,
            role="reviewer",
            assigned_by=super_admin_id,
        )
    actor_ids.extend(
        [
            project_admin.id,
            reviewer_owned.id,
            reviewer_foreign.id,
            employee.id,
            inactive_employee.id,
        ]
    )
    project_ids.extend([pa_project.id, foreign_project.id])
    try:
        identity = _start_identity(
            test_engine,
            tmp_path,
            signing_secret,
            control,
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_admin_password=True,
        )

        operation_id = uuid.uuid4()
        operation_ids.append(operation_id)
        request = _password_request(
            super_token,
            employee.id,
            operation_id=operation_id,
            request_id=uuid.uuid4(),
        )
        model = decode_admin_password_request(request)
        fingerprint = admin_password_request_fingerprint(signing_secret, model)
        expected_password = derive_admin_password(
            signing_secret, super_admin_id, operation_id, fingerprint
        )

        # The first confirmation is intentionally discarded. A restarted owner
        # must return the same operation-bound secret without a second write.
        lost_reply = await asyncio.to_thread(
            _node, identity, control, signing_secret, request, discard=True
        )
        assert lost_reply == {"ok": True, "response_discarded": True}
        first_hash, first_reset_at, original_email = await _target_state(
            maker, employee.id
        )
        assert verify_password(expected_password, first_hash)
        first_receipt = await _receipt(test_engine, operation_id)
        assert first_receipt["target_email"] == original_email
        assert first_receipt["password_admin_reset_at"] == first_reset_at
        expected_state_fingerprint = hashlib.sha256(
            (
                f"aap-identity-admin-password-state/v1:{operation_id}:{first_hash}"
            ).encode("utf-8")
        ).hexdigest()
        assert first_receipt["password_state_fingerprint"] == expected_state_fingerprint
        assert set(first_receipt) == {
            "operation_id",
            "actor_id",
            "target_user_id",
            "operation",
            "request_fingerprint",
            "derivation_id",
            "target_email",
            "password_admin_reset_at",
            "password_state_fingerprint",
            "audit_id",
            "created_at",
        }
        async with maker() as db:
            audit = await db.get(AuditLog, first_receipt["audit_id"])
        assert audit is not None
        assert audit.action == "user.password_admin_reset"
        assert audit.target_type == "user" and audit.target_id == str(employee.id)
        assert audit.status_code == 200
        assert audit.detail_json == {
            "target_email": original_email,
            "target_role": "employee",
        }
        assert expected_password not in json.dumps(audit.detail_json)
        assert await _audit_count(maker, first_receipt["audit_id"]) == 1

        old_origin = identity.origin
        old_port = int(old_origin.rsplit(":", 1)[1])
        identity.stop()
        _assert_no_secret_log(
            tmp_path / f"identity-{old_port}.log",
            [signing_secret, control, super_token, expected_password],
        )
        identity = _start_identity(
            test_engine,
            tmp_path,
            signing_secret,
            control,
            port=int(old_origin.rsplit(":", 1)[1]),
            enable_keys=False,
            enable_sessions=False,
            enable_accounts=False,
            enable_groups=False,
            enable_presence=False,
            enable_admin_password=True,
        )
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET email=:email, name='Profile changed' WHERE id=:id"
                ),
                {"email": f"renamed-{suffix}@example.test", "id": employee.id},
            )
        replay_request = {
            **request,
            "audit_context": {
                **request["audit_context"],
                "path": "/acceptance/replayed-after-profile-change",
                "request_id": str(uuid.uuid4()),
            },
        }
        replay = await asyncio.to_thread(
            _node, identity, control, signing_secret, replay_request
        )
        replay_hash, replay_reset_at, changed_email = await _target_state(
            maker, employee.id
        )
        replay_receipt = await _receipt(test_engine, operation_id)
        assert replay["data"]["target_email"] == original_email
        assert changed_email != original_email
        assert replay_hash == first_hash and replay_reset_at == first_reset_at
        assert replay_receipt == first_receipt
        assert await _audit_count(maker, first_receipt["audit_id"]) == 1
        record_property(
            "admin_password_replay_after_restart",
            "same frozen reply; no repeated update or Audit",
        )

        # A frozen timestamp alone cannot make a reset receipt return a password
        # after another password writer supersedes the stored hash.
        superseding_hash = await asyncio.to_thread(
            hash_password, "SupersedingPassword1!"
        )
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET password_hash=:password_hash WHERE id=:id"
                ),
                {"password_hash": superseding_hash, "id": employee.id},
            )
        superseded = await asyncio.to_thread(
            _node, identity, control, signing_secret, replay_request
        )
        assert superseded["ok"] is False
        assert superseded["error"]["code"] == "operation_conflict"
        assert superseded["error"]["status_code"] == 409
        assert (await _target_state(maker, employee.id))[1] == first_reset_at
        assert await _audit_count(maker, first_receipt["audit_id"]) == 1

        # A timestamp-only change also retires a frozen receipt, even if the
        # bcrypt state is unchanged.
        second_target = reviewer_owned
        second_operation = uuid.uuid4()
        operation_ids.append(second_operation)
        pa_token = _auth_token(project_admin, signing_secret)
        pa_request = _password_request(
            pa_token, second_target.id, operation_id=second_operation
        )
        pa_result = await asyncio.to_thread(
            _node, identity, control, signing_secret, pa_request
        )
        assert pa_result["ok"] is True
        second_hash, second_reset_at, _ = await _target_state(maker, second_target.id)
        second_receipt = await _receipt(test_engine, second_operation)
        changed_timestamp = second_reset_at + timedelta(microseconds=1)
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET password_admin_reset_at=:reset_at WHERE id=:id"
                ),
                {"reset_at": changed_timestamp, "id": second_target.id},
            )
        timestamp_conflict = await asyncio.to_thread(
            _node, identity, control, signing_secret, pa_request
        )
        assert timestamp_conflict["ok"] is False
        assert timestamp_conflict["error"]["code"] == "operation_conflict"
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET password_admin_reset_at=:reset_at WHERE id=:id"
                ),
                {"reset_at": second_reset_at, "id": second_target.id},
            )
        assert (await _target_state(maker, second_target.id))[0] == second_hash
        assert await _audit_count(maker, second_receipt["audit_id"]) == 1

        # Project admins may reset any active employee; lower-ranked roles
        # require a historical membership in a project they currently own.
        global_employee = await asyncio.to_thread(
            _node,
            identity,
            control,
            signing_secret,
            _password_request(pa_token, employee.id, operation_id=uuid.uuid4()),
        )
        assert global_employee["ok"] is True
        foreign_reviewer = await asyncio.to_thread(
            _node,
            identity,
            control,
            signing_secret,
            _password_request(pa_token, reviewer_foreign.id, operation_id=uuid.uuid4()),
        )
        assert foreign_reviewer["ok"] is False
        assert foreign_reviewer["error"]["code"] == "target_out_of_scope"
        higher_role = await asyncio.to_thread(
            _node,
            identity,
            control,
            signing_secret,
            _password_request(pa_token, super_admin_id, operation_id=uuid.uuid4()),
        )
        assert higher_role["ok"] is False
        assert higher_role["error"]["code"] == "role_forbidden"
        async with maker.begin() as db:
            owned_viewer = await create_user(
                db,
                "viewer",
                f"admin-password-viewer-{suffix}@example.test",
                "Owned viewer",
            )
            await create_membership(
                db,
                project_id=pa_project.id,
                user_id=owned_viewer.id,
                role="annotator",
                assigned_by=project_admin.id,
            )
        actor_ids.append(owned_viewer.id)
        owned_viewer_result = await asyncio.to_thread(
            _node,
            identity,
            control,
            signing_secret,
            _password_request(pa_token, owned_viewer.id, operation_id=uuid.uuid4()),
        )
        assert owned_viewer_result["ok"] is True

        inactive = await asyncio.to_thread(
            _node,
            identity,
            control,
            signing_secret,
            _password_request(
                super_token, inactive_employee.id, operation_id=uuid.uuid4()
            ),
        )
        assert inactive["ok"] is False
        assert inactive["error"]["code"] == "target_not_found"
        self_reset = await asyncio.to_thread(
            _node,
            identity,
            control,
            signing_secret,
            _password_request(super_token, super_admin_id, operation_id=uuid.uuid4()),
        )
        assert self_reset["ok"] is False
        assert self_reset["error"]["code"] == "self_reset"

        # Both rows are acquired NOWAIT in UUID order. A held actor or target
        # lock must fail closed with the same bounded retry response.
        for locked_id in (super_admin_id, employee.id):
            connection = await test_engine.connect()
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text("SELECT id FROM public.users WHERE id=:id FOR UPDATE"),
                    {"id": locked_id},
                )
                busy = await asyncio.to_thread(
                    _node,
                    identity,
                    control,
                    signing_secret,
                    _password_request(
                        super_token,
                        employee.id,
                        operation_id=uuid.uuid4(),
                    ),
                )
                assert busy["ok"] is False
                assert busy["error"]["code"] == "account_busy"
            finally:
                await transaction.rollback()
                await connection.close()

        # Existing JWTs remain current until the normal generation fence is
        # explicitly advanced; this feature itself does not advance it.
        async with maker() as db:
            generation = await db.scalar(
                text("SELECT credential_generation FROM public.users WHERE id=:id"),
                {"id": super_admin_id},
            )
        assert generation == 0
        async with maker.begin() as db:
            await db.execute(
                text("UPDATE public.users SET role='viewer' WHERE id=:id"),
                {"id": project_admin.id},
            )
        revoked_role = await asyncio.to_thread(
            _node,
            identity,
            control,
            signing_secret,
            _password_request(
                pa_token,
                employee.id,
                operation_id=uuid.uuid4(),
            ),
        )
        assert revoked_role["ok"] is False
        assert revoked_role["error"]["code"] == "manager_required"

        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET credential_generation=credential_generation+1 "
                    "WHERE id=:id"
                ),
                {"id": super_admin_id},
            )
        stale_credential = await asyncio.to_thread(
            _node,
            identity,
            control,
            signing_secret,
            _password_request(
                super_token,
                employee.id,
                operation_id=uuid.uuid4(),
            ),
        )
        assert stale_credential["ok"] is False
        assert stale_credential["error"]["code"] == "generation_outdated"
        port = int(identity.origin.rsplit(":", 1)[1])
        identity.stop()
        identity = None
        _assert_no_secret_log(
            tmp_path / f"identity-{port}.log",
            [signing_secret, control, super_token, pa_token, expected_password],
        )
    finally:
        if identity:
            identity.stop()
        await _cleanup_admin_password(
            maker,
            user_ids=actor_ids,
            project_ids=project_ids,
            operation_ids=operation_ids,
        )
