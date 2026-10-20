"""Independent Node consumer and live owner qualification for invitations."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
import secrets
import subprocess
import time
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete, select, text, update

from aap_identity.security import create_access_token
from app.db.models.group import Group
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.system_setting import SystemSetting
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from tests.factory import create_project, create_user
from tests.identity_recovery_live_helpers import SMTPInbox, start_mail
from tests.test_identity_admission_live import (
    ROOT,
    _dsn,
    _inspect_docker,
    _port,
    _start,
    _start_container_identity,
    _start_identity,
    _token,
    _world,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_INVITATIONS_LIVE_ACCEPTANCE") != "1",
        reason="requires the explicitly enabled owned Identity invitation verifier",
    ),
]


def _command(operation, token, payload=None, *, operation_id=None):
    return {
        "schema": "aap-identity-invitations/v1",
        "operation": operation,
        "operation_id": str(operation_id or uuid.uuid4()),
        "token": token,
        "audit_context": {
            "method": "POST",
            "path": "/acceptance/identity-invitations",
            "ip": "127.0.0.1",
            "request_id": str(uuid.uuid4()),
        },
        "payload": payload or {},
    }


def _node(identity, control, request):
    result = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_invitations_peer.mjs")],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": request},
            ensure_ascii=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, "Independent Node invitation peer failed"
    hidden = [control]
    if isinstance(request.get("token"), str):
        hidden.append(request["token"])
    payload = request.get("payload", {})
    for field in ("invitation_token", "password"):
        value = payload.get(field)
        if isinstance(value, str) and len(value) >= 4:
            hidden.append(value)
    for secret in hidden:
        assert secret not in result.stdout
        assert secret not in result.stderr
    return json.loads(result.stdout)


def _recovery_command(operation, payload, *, operation_id=None):
    return {
        "schema": "aap-identity-recovery/v1",
        "operation": operation,
        "operation_id": str(operation_id or uuid.uuid4()),
        "audit_context": {
            "method": "POST",
            "path": "/acceptance/recovery-v1",
            "ip": "127.0.0.1",
            "request_id": str(uuid.uuid4()),
        },
        "payload": payload,
    }


def _node_recovery(identity, control, request):
    result = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_recovery_peer.mjs")],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": request},
            ensure_ascii=False,
        ),
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, "Recovery V1 peer failed"
    return json.loads(result.stdout)


def _start_mail_v2(test_engine, directory, encryption_key, smtp, *, port=None):
    port = port or _port()
    values = {
        "DATABASE_URL": _dsn("MAIL_V2", test_engine),
        "SETTINGS_DATABASE_URL": _dsn("MAIL_SETTINGS", test_engine),
        "ENCRYPTION_KEY": encryption_key,
    }
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "IDENTITY_MAIL_V2_HOST": "127.0.0.1",
        "IDENTITY_MAIL_V2_PORT": str(port),
        "IDENTITY_MAIL_V2_SMTP_HOST": "127.0.0.1",
        "IDENTITY_MAIL_V2_SMTP_PORT": str(smtp.port),
        "IDENTITY_MAIL_V2_SMTP_FROM": "noreply@example.test",
        "IDENTITY_MAIL_V2_POLL_INTERVAL_SECONDS": "0.1",
        "IDENTITY_MAIL_V2_LEASE_SECONDS": "2",
        "IDENTITY_MAIL_V2_RETRY_DELAY_SECONDS": "0.2",
        "IDENTITY_MAIL_V2_CONNECT_TIMEOUT_SECONDS": "3",
        "IDENTITY_MAIL_V2_COMMAND_TIMEOUT_SECONDS": "5",
    }
    for name, value in values.items():
        path = directory / f"invitation-mail-v2-{name.lower()}"
        path.write_text(value)
        path.chmod(0o600)
        environment[f"IDENTITY_MAIL_V2_{name}_FILE"] = str(path)
    assert all(
        name == "PATH" or name.startswith("IDENTITY_MAIL_V2_") for name in environment
    )
    return _start(
        [
            os.environ["AAP_IDENTITY_RUNTIME_PYTHON"],
            "-I",
            "-m",
            "aap_identity_runtime.mail_worker_v2",
        ],
        environment,
        directory,
        directory / f"invitation-mail-v2-{port}.log",
        port,
        "/ready",
    )


async def _set_daily_quota(maker, actor_id, limit):
    async with maker.begin() as db:
        previous = await db.get(SystemSetting, "max_invitations_per_day")
        snapshot = None
        if previous is not None:
            snapshot = {
                "value_type": previous.value_type,
                "value_json": previous.value_json,
                "updated_by": previous.updated_by,
                "updated_at": previous.updated_at,
            }
            previous.value_type = "int"
            previous.value_json = limit
            previous.updated_by = actor_id
        else:
            db.add(
                SystemSetting(
                    key="max_invitations_per_day",
                    value_type="int",
                    value_json=limit,
                    updated_by=actor_id,
                )
            )
    return snapshot


async def _restore_daily_quota(maker, snapshot):
    async with maker.begin() as db:
        if snapshot is None:
            await db.execute(
                delete(SystemSetting).where(
                    SystemSetting.key == "max_invitations_per_day"
                )
            )
        else:
            setting = await db.get(SystemSetting, "max_invitations_per_day")
            if setting is not None:
                setting.value_type = snapshot["value_type"]
                setting.value_json = snapshot["value_json"]
                setting.updated_by = snapshot["updated_by"]
                setting.updated_at = snapshot["updated_at"]


async def _invitation_ids(maker, actor_ids):
    async with maker() as db:
        return list(
            (
                await db.scalars(
                    select(UserInvitation.id).where(
                        UserInvitation.invited_by.in_(actor_ids)
                    )
                )
            ).all()
        )


async def _cleanup_invitations(
    maker,
    *,
    actor_ids,
    project_ids=(),
    invitation_ids=(),
    group_ids=(),
    user_operation_ids=(),
    recovery_operation_ids=(),
):
    actors = [uuid.UUID(str(value)) for value in actor_ids]
    projects = [uuid.UUID(str(value)) for value in project_ids]
    invitations = [
        uuid.UUID(str(value))
        for value in [*invitation_ids, *(await _invitation_ids(maker, actors))]
    ]
    operations = [uuid.UUID(str(value)) for value in user_operation_ids]
    recovery_ops = [uuid.UUID(str(value)) for value in recovery_operation_ids]
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
        await db.execute(
            text(
                "DELETE FROM public.identity_invitation_operation_receipts "
                "WHERE actor_id=ANY(CAST(:actors AS uuid[])) "
                "OR operation_id=ANY(CAST(:operations AS uuid[]))"
            ),
            {"actors": actors, "operations": operations},
        )
        if recovery_ops:
            await db.execute(
                text(
                    "DELETE FROM public.identity_recovery_operation_receipts "
                    "WHERE actor_id=ANY(CAST(:actors AS uuid[])) "
                    "OR operation_id=ANY(CAST(:operations AS uuid[]))"
                ),
                {"actors": actors, "operations": recovery_ops},
            )
        await db.execute(
            text(
                "DELETE FROM public.identity_credential_mail_outbox "
                "WHERE actor_id=ANY(CAST(:actors AS uuid[])) "
                "OR invitation_id=ANY(CAST(:invitations AS uuid[]))"
            ),
            {"actors": actors, "invitations": invitations},
        )
        if invitations:
            await db.execute(
                delete(UserInvitation).where(UserInvitation.id.in_(invitations))
            )
        await db.execute(
            text(
                "DELETE FROM public.password_reset_tokens WHERE user_id=ANY(CAST(:actors AS uuid[]))"
            ),
            {"actors": actors},
        )
        await db.execute(
            text(
                "DELETE FROM public.email_verification_tokens WHERE user_id=ANY(CAST(:actors AS uuid[]))"
            ),
            {"actors": actors},
        )
        await db.execute(
            delete(ProjectMember).where(
                ProjectMember.user_id.in_(actors)
                | ProjectMember.project_id.in_(projects)
            )
        )
        targets = [str(value) for value in [*actors, *projects, *invitations]]
        await db.execute(
            text(
                "DELETE FROM public.audit_logs WHERE actor_id=ANY(CAST(:actors AS uuid[])) "
                "OR target_id=ANY(CAST(:targets AS text[]))"
            ),
            {"actors": actors, "targets": targets},
        )
        if group_ids:
            await db.execute(
                update(User)
                .where(User.group_id.in_(list(group_ids)))
                .values(group_id=None, group_name=None)
            )
            await db.execute(delete(Group).where(Group.id.in_(list(group_ids))))
        if projects:
            await db.execute(delete(Project).where(Project.id.in_(projects)))
        await db.execute(delete(User).where(User.id.in_(actors)))


async def _count(maker, sql, params):
    async with maker() as db:
        return await db.scalar(text(sql), params)


async def _mail_rows(maker, invitation_id):
    async with maker() as db:
        return list(
            (
                await db.execute(
                    text(
                        "SELECT id,status,attempt,last_reason,lease_expires_at,purpose,"
                        "token_hash,token_ciphertext,invitation_context "
                        "FROM public.identity_credential_mail_outbox "
                        "WHERE invitation_id=:invitation_id AND purpose='invitation' "
                        "ORDER BY created_at,id"
                    ),
                    {"invitation_id": invitation_id},
                )
            ).mappings()
        )


async def _wait_row_status(maker, outbox_id, status, *, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with maker() as db:
            result = await db.execute(
                text(
                    "SELECT status,attempt,lease_expires_at FROM public.identity_credential_mail_outbox "
                    "WHERE id=:id"
                ),
                {"id": outbox_id},
            )
            row = result.mappings().first()
        if row is not None and row["status"] == status:
            return row
        await asyncio.sleep(0.03)
    pytest.fail(f"Invitation MailV2 row did not reach {status}")


async def test_installed_node_manager_batch_quota_replay_and_catalog_reads(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    actor_ids = [actor]
    operation_ids = []
    invitation_ids = []
    identity = None
    quota_snapshot = None
    quota_changed = False
    suffix = uuid.uuid4().hex
    try:
        quota_snapshot = await _set_daily_quota(maker, actor, 30)
        quota_changed = True
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
            enable_invitations=True,
        )
        token = _token(actor, secret)

        create = _command(
            "create_invitation",
            token,
            {
                "email": f" Manager-Create-{suffix}@Example.test ",
                "role": "employee",
                "group_name": "  Manager Group  ",
                "project_id": None,
                "project_member_role": None,
            },
        )
        create_id = uuid.UUID(create["operation_id"])
        operation_ids.append(create_id)
        created = await asyncio.to_thread(_node, identity, control, create)
        assert created["ok"] and created["data"]["token"]
        assert created["data"]["invite_url"].endswith(
            f"/register?token={created['data']['token']}"
        )
        replay_create = await asyncio.to_thread(_node, identity, control, create)
        assert replay_create == created
        async with maker() as db:
            created_invitation = await db.scalar(
                select(UserInvitation).where(
                    UserInvitation.token == created["data"]["token"]
                )
            )
            assert created_invitation is not None
            assert created_invitation.email == f"manager-create-{suffix}@example.test"
            assert created_invitation.group_name == "Manager Group"
            invitation_ids.append(created_invitation.id)
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_invitation_operation_receipts "
                        "WHERE operation_id=:operation_id AND actor_id=:actor"
                    ),
                    {"operation_id": create_id, "actor": actor},
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='user.invite' AND target_id=:target"
                    ),
                    {"actor": actor, "target": created_invitation.email},
                )
                == 1
            )

        resend = _command(
            "resend_invitation", token, {"invitation_id": str(created_invitation.id)}
        )
        resend_id = uuid.UUID(resend["operation_id"])
        operation_ids.append(resend_id)
        resent = await asyncio.to_thread(_node, identity, control, resend)
        assert resent["ok"]
        assert resent["data"]["token"] != created["data"]["token"]
        assert (await asyncio.to_thread(_node, identity, control, resend)) == resent
        async with maker() as db:
            current_invitation = await db.get(UserInvitation, created_invitation.id)
            assert current_invitation is not None
            assert current_invitation.token == resent["data"]["token"]
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='user.invite_resend' AND target_id=:target"
                    ),
                    {"actor": actor, "target": str(created_invitation.id)},
                )
                == 1
            )

        emails = [f"batch-{index}-{suffix}@example.test" for index in range(30)]
        batch_items = [
            {
                "email": emails[0],
                "role": "employee",
                "group_name": None,
                "project_id": None,
                "project_member_role": None,
            },
            {
                "email": emails[0],
                "role": "employee",
                "group_name": None,
                "project_id": None,
                "project_member_role": None,
            },
        ]
        batch_items.extend(
            {
                "email": emails[index],
                "role": "employee",
                "group_name": None,
                "project_id": None,
                "project_member_role": None,
            }
            for index in range(1, 30)
        )
        batch_items.append(
            {
                "email": f"batch-over-quota-{suffix}@example.test",
                "role": "employee",
                "group_name": None,
                "project_id": None,
                "project_member_role": None,
            }
        )
        batch_items.extend(
            {
                "email": f"invalid-{index}-{suffix}@example.test",
                "role": "legacy_invalid",
                "group_name": None,
                "project_id": None,
                "project_member_role": None,
            }
            for index in range(468)
        )
        assert len(batch_items) == 500
        preview = _command("preview_invitation_batch", token, {"items": batch_items})
        preview_id = uuid.UUID(preview["operation_id"])
        operation_ids.append(preview_id)
        preview_result = await asyncio.to_thread(_node, identity, control, preview)
        assert preview_result["ok"]
        assert preview_result["data"]["preview"] is True
        assert preview_result["data"]["succeeded"] == 29
        assert preview_result["data"]["failed"] == 471
        assert all(
            item["token"] is None and item["invite_url"] is None
            for item in preview_result["data"]["items"]
        )
        assert preview_result["data"]["items"][1]["error"] == "本批次中邮箱重复"
        assert "最多邀请 30 人" in preview_result["data"]["items"][31]["error"]
        async with maker() as db:
            assert (
                await db.scalar(
                    select(text("count(*)"))
                    .select_from(UserInvitation)
                    .where(UserInvitation.invited_by == actor)
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_invitation_operation_receipts "
                        "WHERE operation_id=:operation_id"
                    ),
                    {"operation_id": preview_id},
                )
                == 0
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='user.bulk_invite'"
                    ),
                    {"actor": actor},
                )
                == 0
            )

        apply = _command("apply_invitation_batch", token, {"items": batch_items})
        apply_id = uuid.UUID(apply["operation_id"])
        operation_ids.append(apply_id)
        applied = await asyncio.to_thread(_node, identity, control, apply)
        assert applied["ok"] and applied["data"]["succeeded"] == 29
        assert applied["data"]["failed"] == 471
        replay_apply = await asyncio.to_thread(_node, identity, control, apply)
        assert replay_apply == applied
        batch_invitation_ids = [
            uuid.UUID(item["invitation_id"])
            for item in applied["data"]["items"]
            if item["ok"]
        ]
        invitation_ids.extend(batch_invitation_ids)
        async with maker() as db:
            assert (
                await db.scalar(
                    select(text("count(*)"))
                    .select_from(UserInvitation)
                    .where(UserInvitation.invited_by == actor)
                )
                == 30
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_invitation_operation_receipts "
                        "WHERE operation_id=:operation_id AND actor_id=:actor"
                    ),
                    {"operation_id": apply_id, "actor": actor},
                )
                == 1
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='user.bulk_invite'"
                    ),
                    {"actor": actor},
                )
                == 29
            )

        revoke = _command(
            "revoke_invitation", token, {"invitation_id": str(created_invitation.id)}
        )
        revoke_id = uuid.UUID(revoke["operation_id"])
        operation_ids.append(revoke_id)
        revoked = await asyncio.to_thread(_node, identity, control, revoke)
        assert revoked["ok"] and revoked["data"] == {}
        assert (await asyncio.to_thread(_node, identity, control, revoke)) == revoked

        list_request = _command(
            "list_invitations", token, {"status": "all", "scope": "me"}
        )
        list_id = uuid.UUID(list_request["operation_id"])
        operation_ids.append(list_id)
        listed = await asyncio.to_thread(_node, identity, control, list_request)
        assert listed["ok"] and len(listed["data"]["items"]) == 30
        assert "token" not in listed["data"]["items"][0]
        assert any(
            item["id"] == str(created_invitation.id) and item["status"] == "revoked"
            for item in listed["data"]["items"]
        )

        query = _command(
            "query_invitations",
            token,
            {
                "page": 1,
                "page_size": 50,
                "status": "pending",
                "scope": "me",
                "project_id": None,
                "role": None,
                "email": None,
                "search": None,
                "created_from": None,
                "created_to": None,
            },
        )
        query_id = uuid.UUID(query["operation_id"])
        operation_ids.append(query_id)
        queried = await asyncio.to_thread(_node, identity, control, query)
        assert queried["ok"] and queried["data"]["total"] == 29
        assert queried["data"]["pages"] == 1

        stats = _command("invitation_stats", token, {"status": "all", "scope": "me"})
        stats_id = uuid.UUID(stats["operation_id"])
        operation_ids.append(stats_id)
        stat_result = await asyncio.to_thread(_node, identity, control, stats)
        assert stat_result["ok"]
        assert stat_result["data"] == {
            "total": 30,
            "pending": 29,
            "accepted": 0,
            "expired": 0,
            "revoked": 1,
        }

        export = _command(
            "export_invitations",
            token,
            {
                "format": "json",
                "status": "all",
                "scope": "me",
                "project_id": None,
                "role": None,
                "email": None,
                "search": None,
                "created_from": None,
                "created_to": None,
            },
        )
        export_id = uuid.UUID(export["operation_id"])
        operation_ids.append(export_id)
        exported = await asyncio.to_thread(_node, identity, control, export)
        assert exported["ok"] and len(exported["data"]["items"]) == 30
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='invitation.export'"
                    ),
                    {"actor": actor},
                )
                == 1
            )
        record_property(
            "invitation_manager_operations",
            "create-resend-replay,500-preview-rollback,quota-partial-apply,revoke-list-query-stats-export",
        )
    finally:
        if identity is not None:
            identity.stop()
        if quota_changed:
            await _restore_daily_quota(maker, quota_snapshot)
        await _cleanup_invitations(
            maker,
            actor_ids=actor_ids,
            invitation_ids=invitation_ids,
            user_operation_ids=operation_ids,
        )


async def test_public_resolve_register_creates_group_membership_and_rolls_back_together(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    actor_ids = [actor]
    project_ids = []
    invitation_ids = []
    operation_ids = []
    group_ids = []
    identity = None
    try:
        async with maker.begin() as db:
            project = await create_project(
                db, owner_id=actor, name=f"Invitation registration {suffix}"
            )
        project_ids.append(project.id)
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
            enable_invitations=True,
        )
        manager_token = _token(actor, secret)
        email = f"invited-registration-{suffix}@example.test"
        group_name = f"InvitationGroup-{suffix[:10]}"
        create = _command(
            "create_invitation",
            manager_token,
            {
                "email": email,
                "role": "employee",
                "group_name": f"  {group_name}  ",
                "project_id": str(project.id),
                "project_member_role": "annotator",
            },
        )
        create_id = uuid.UUID(create["operation_id"])
        operation_ids.append(create_id)
        created = await asyncio.to_thread(_node, identity, control, create)
        assert created["ok"]
        proof = created["data"]["token"]
        async with maker() as db:
            invitation = await db.scalar(
                select(UserInvitation).where(UserInvitation.token == proof)
            )
            assert invitation is not None
            invitation_ids.append(invitation.id)
            assert invitation.group_name == group_name

        resolve = _command(
            "resolve_invitation",
            None,
            {"invitation_token": proof},
        )
        resolve_id = uuid.UUID(resolve["operation_id"])
        operation_ids.append(resolve_id)
        resolved = await asyncio.to_thread(_node, identity, control, resolve)
        assert resolved["ok"] and resolved["principal"] is None
        assert resolved["data"]["email"] == email
        assert resolved["data"]["project_id"] == str(project.id)
        assert resolved["data"]["project_member_role"] == "annotator"
        assert resolved["data"]["group_name"] == group_name
        assert proof not in json.dumps(resolved, ensure_ascii=False)

        password = "InvitationStrong123"
        register = _command(
            "register_invitation",
            None,
            {
                "invitation_token": proof,
                "name": "New invited annotator",
                "password": password,
            },
        )
        register_id = uuid.UUID(register["operation_id"])
        operation_ids.append(register_id)
        registered = await asyncio.to_thread(_node, identity, control, register)
        assert registered["ok"] and registered["principal"] is None
        new_user_id = uuid.UUID(registered["data"]["user"]["id"])
        assert new_user_id == register_id
        assert registered["data"]["user"]["role"] == "employee"
        assert registered["data"]["user"]["status"] == "online"
        assert registered["data"]["user"]["email_verified_at"] is not None
        assert registered["data"]["acceptance"]["project_member_role"] == "annotator"
        assert registered["data"]["acceptance"]["active_batch_count"] == 0
        assert proof not in json.dumps(registered, ensure_ascii=False)
        assert password not in json.dumps(registered, ensure_ascii=False)
        actor_ids.append(new_user_id)
        replay_registration = await asyncio.to_thread(
            _node, identity, control, register
        )
        assert replay_registration == registered

        async with maker() as db:
            user = await db.get(User, new_user_id)
            assert user is not None
            assert user.email == email and user.role == "employee"
            assert user.is_active and user.status == "online"
            assert user.email_verified_at is not None
            assert user.group_id is not None and user.group_name == group_name
            group = await db.get(Group, user.group_id)
            assert group is not None and group.name == group_name
            group_ids.append(group.id)
            membership = await db.scalar(
                select(ProjectMember).where(
                    ProjectMember.project_id == project.id,
                    ProjectMember.user_id == user.id,
                )
            )
            assert membership is not None and membership.role == "annotator"
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='user.register' AND target_id=:target"
                    ),
                    {"actor": actor, "target": str(new_user_id)},
                )
                == 1
            )
            receipt = await db.scalar(
                text(
                    "SELECT result::text FROM public.identity_invitation_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": register_id},
            )
            assert receipt is not None
            assert proof not in receipt and password not in receipt
            assert registered["data"]["access_token"] not in receipt

        async with maker.begin() as db:
            await db.execute(
                update(User)
                .where(User.id == new_user_id)
                .values(credential_generation=User.credential_generation + 1)
            )

        retired_replay = await asyncio.to_thread(_node, identity, control, register)
        assert retired_replay["ok"] is False
        assert retired_replay["error"] == {
            "code": "operation_conflict",
            "status_code": 409,
            "detail": "operation_conflict",
        }
        assert proof not in json.dumps(retired_replay, ensure_ascii=False)
        assert password not in json.dumps(retired_replay, ensure_ascii=False)

        retired_credential = _command(
            "accept_invitation",
            registered["data"]["access_token"],
            {"invitation_token": proof},
        )
        operation_ids.append(uuid.UUID(retired_credential["operation_id"]))
        retired_result = await asyncio.to_thread(
            _node, identity, control, retired_credential
        )
        assert retired_result["ok"] is False
        assert retired_result["error"]["code"] == "generation_outdated"
        assert proof not in json.dumps(retired_result, ensure_ascii=False)
        async with maker() as db:
            assert (
                await db.scalar(
                    select(User.credential_generation).where(User.id == new_user_id)
                )
                == 1
            )

        bad_email = f"invited-group-failure-{suffix}@example.test"
        bad_group_name = "x" * 101
        bad_create = _command(
            "create_invitation",
            manager_token,
            {
                "email": bad_email,
                "role": "employee",
                "group_name": "temporary",
                "project_id": str(project.id),
                "project_member_role": "reviewer",
            },
        )
        bad_create_id = uuid.UUID(bad_create["operation_id"])
        operation_ids.append(bad_create_id)
        bad_created = await asyncio.to_thread(_node, identity, control, bad_create)
        assert bad_created["ok"]
        bad_proof = bad_created["data"]["token"]
        async with maker.begin() as db:
            bad_invitation = await db.scalar(
                select(UserInvitation).where(UserInvitation.token == bad_proof)
            )
            assert bad_invitation is not None
            invitation_ids.append(bad_invitation.id)
            await db.execute(
                update(UserInvitation)
                .where(UserInvitation.id == bad_invitation.id)
                .values(group_name=bad_group_name)
            )
        bad_register = _command(
            "register_invitation",
            None,
            {
                "invitation_token": bad_proof,
                "name": "Should roll back",
                "password": "InvitationStrong456",
            },
        )
        bad_register_id = uuid.UUID(bad_register["operation_id"])
        operation_ids.append(bad_register_id)
        rejected = await asyncio.to_thread(_node, identity, control, bad_register)
        assert rejected["ok"] is False
        assert rejected["error"] == {
            "code": "policy_rejected",
            "status_code": 409,
            "detail": "邀请中的数据组名称无效，请联系管理员重发邀请",
        }
        assert bad_proof not in json.dumps(rejected, ensure_ascii=False)
        assert "InvitationStrong456" not in json.dumps(rejected, ensure_ascii=False)
        async with maker() as db:
            assert (
                await db.scalar(select(User.id).where(User.email == bad_email)) is None
            )
            assert (
                await db.scalar(select(Group.id).where(Group.name == bad_group_name))
                is None
            )
            assert (
                await db.scalar(
                    select(ProjectMember.id).where(
                        ProjectMember.project_id == project.id,
                        ProjectMember.user_id == bad_register_id,
                    )
                )
                is None
            )
            bad_invitation = await db.get(UserInvitation, bad_invitation.id)
            assert bad_invitation is not None and bad_invitation.accepted_at is None
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_invitation_operation_receipts "
                        "WHERE operation_id=:operation_id"
                    ),
                    {"operation_id": bad_register_id},
                )
                == 0
            )
        record_property(
            "invitation_public_registration",
            "opaque-proof,owner-jwt,group-and-membership-atomicity,registration-replay,generation-retirement,no-secret-receipt,rollback-on-invalid-group",
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_invitations(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            invitation_ids=invitation_ids,
            group_ids=group_ids,
            user_operation_ids=operation_ids,
        )


async def test_existing_account_acceptance_rechecks_issuer_owner_and_serializes_races(
    test_engine, tmp_path, record_property
):
    maker, super_admin = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex
    actor_ids = [super_admin]
    project_ids = []
    invitation_ids = []
    operation_ids = []
    try:
        async with maker.begin() as db:
            manager = await create_user(
                db,
                "project_admin",
                f"invitation-owner-{suffix}@example.test",
                "Invitation project owner",
            )
            replacement_owner = await create_user(
                db,
                "super_admin",
                f"invitation-new-owner-{suffix}@example.test",
                "Replacement project owner",
            )
            existing = await create_user(
                db,
                "employee",
                f"existing-invitee-{suffix}@example.test",
                "Existing employee",
            )
            project = await create_project(
                db, owner_id=manager.id, name=f"Acceptance project {suffix}"
            )
            task_batch = TaskBatch(
                project_id=project.id,
                display_id=f"B-{suffix[:10]}",
                name="Active annotator batch",
                status="active",
                annotator_id=existing.id,
                created_by=manager.id,
            )
            db.add(task_batch)
        actor_ids.extend([manager.id, replacement_owner.id, existing.id])
        project_ids.append(project.id)
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
            enable_invitations=True,
        )
        manager_token = create_access_token(
            str(manager.id), "project_admin", secret=secret, expire_minutes=15
        )
        existing_token = create_access_token(
            str(existing.id), "employee", secret=secret, expire_minutes=15
        )

        transfer_invite = _command(
            "create_invitation",
            manager_token,
            {
                "email": f"owner-change-{suffix}@example.test",
                "role": "employee",
                "group_name": None,
                "project_id": str(project.id),
                "project_member_role": "annotator",
            },
        )
        operation_ids.append(uuid.UUID(transfer_invite["operation_id"]))
        transfer_created = await asyncio.to_thread(
            _node, identity, control, transfer_invite
        )
        assert transfer_created["ok"]
        transfer_proof = transfer_created["data"]["token"]
        async with maker.begin() as db:
            transfer_row = await db.scalar(
                select(UserInvitation).where(UserInvitation.token == transfer_proof)
            )
            assert transfer_row is not None
            invitation_ids.append(transfer_row.id)
            await db.execute(
                update(Project)
                .where(Project.id == project.id)
                .values(owner_id=replacement_owner.id)
            )
        resolve_after_transfer = _command(
            "resolve_invitation", None, {"invitation_token": transfer_proof}
        )
        operation_ids.append(uuid.UUID(resolve_after_transfer["operation_id"]))
        stale_owner = await asyncio.to_thread(
            _node, identity, control, resolve_after_transfer
        )
        assert stale_owner["ok"] is False
        assert stale_owner["error"]["code"] == "invitation_scope_invalid"
        # Revoke/resend remain owned by the original inviter row even though the
        # project owner changed; proof resolution still fails after token rotation.
        resend_after_transfer = _command(
            "resend_invitation",
            manager_token,
            {"invitation_id": str(transfer_row.id)},
        )
        operation_ids.append(uuid.UUID(resend_after_transfer["operation_id"]))
        resent_transfer = await asyncio.to_thread(
            _node, identity, control, resend_after_transfer
        )
        assert resent_transfer["ok"]
        assert resent_transfer["data"]["token"] != transfer_proof
        stale_resolve = _command(
            "resolve_invitation",
            None,
            {"invitation_token": resent_transfer["data"]["token"]},
        )
        operation_ids.append(uuid.UUID(stale_resolve["operation_id"]))
        assert (await asyncio.to_thread(_node, identity, control, stale_resolve))[
            "error"
        ]["code"] == "invitation_scope_invalid"
        async with maker.begin() as db:
            await db.execute(
                update(Project)
                .where(Project.id == project.id)
                .values(owner_id=manager.id)
            )
            await db.execute(
                update(User).where(User.id == manager.id).values(is_active=False)
            )
        inactive_issuer_resolve = _command(
            "resolve_invitation",
            None,
            {"invitation_token": resent_transfer["data"]["token"]},
        )
        operation_ids.append(uuid.UUID(inactive_issuer_resolve["operation_id"]))
        inactive_issuer = await asyncio.to_thread(
            _node, identity, control, inactive_issuer_resolve
        )
        assert inactive_issuer["ok"] is False
        assert inactive_issuer["error"]["code"] == "invitation_scope_invalid"
        async with maker.begin() as db:
            await db.execute(
                update(User).where(User.id == manager.id).values(is_active=True)
            )

        accept_invite = _command(
            "create_invitation",
            manager_token,
            {
                "email": existing.email,
                "role": "employee",
                "group_name": None,
                "project_id": str(project.id),
                "project_member_role": "annotator",
            },
        )
        operation_ids.append(uuid.UUID(accept_invite["operation_id"]))
        accept_created = await asyncio.to_thread(
            _node, identity, control, accept_invite
        )
        assert accept_created["ok"]
        accept_proof = accept_created["data"]["token"]
        async with maker.begin() as db:
            accept_row = await db.scalar(
                select(UserInvitation).where(UserInvitation.token == accept_proof)
            )
            assert accept_row is not None
            invitation_ids.append(accept_row.id)

        accept_a = _command(
            "accept_invitation", existing_token, {"invitation_token": accept_proof}
        )
        accept_b = _command(
            "accept_invitation", existing_token, {"invitation_token": accept_proof}
        )
        operation_ids.extend(
            [uuid.UUID(accept_a["operation_id"]), uuid.UUID(accept_b["operation_id"])]
        )
        results = await asyncio.gather(
            asyncio.to_thread(_node, identity, control, accept_a),
            asyncio.to_thread(_node, identity, control, accept_b),
        )
        successful = [result for result in results if result["ok"]]
        failed = [result for result in results if not result["ok"]]
        assert len(successful) == len(failed) == 1
        assert failed[0]["error"]["code"] in {
            "invitation_used",
            "current_scope_busy",
        }
        winner_request = accept_a if results[0]["ok"] else accept_b
        replayed = await asyncio.to_thread(_node, identity, control, winner_request)
        assert replayed == successful[0]
        accepted = successful[0]
        assert accepted["data"]["user"]["id"] == str(existing.id)
        assert accepted["data"]["user"]["role"] == "employee"
        assert accepted["data"]["acceptance"]["active_batch_count"] == 1
        assert accepted["data"]["acceptance"]["next_action"] == "start_work"
        assert accepted["data"]["acceptance"]["next_action_label"] == "开始工作"
        assert (
            accepted["data"]["acceptance"]["responsible_person_name"]
            == "Invitation project owner"
        )
        async with maker() as db:
            invitation = await db.get(UserInvitation, accept_row.id)
            assert invitation is not None
            assert invitation.accepted_user_id == existing.id
            assert (
                await db.scalar(
                    select(ProjectMember.id).where(
                        ProjectMember.project_id == project.id,
                        ProjectMember.user_id == existing.id,
                        ProjectMember.role == "annotator",
                    )
                )
                is not None
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='user.invite_accept_existing' "
                        "AND target_id=:target"
                    ),
                    {"actor": existing.id, "target": str(accept_row.id)},
                )
                == 1
            )

        account_only_email = f"account-only-invitee-{suffix}@example.test"
        account_invite = _command(
            "create_invitation",
            manager_token,
            {
                "email": account_only_email,
                "role": "employee",
                "group_name": None,
                "project_id": None,
                "project_member_role": None,
            },
        )
        operation_ids.append(uuid.UUID(account_invite["operation_id"]))
        account_created = await asyncio.to_thread(
            _node, identity, control, account_invite
        )
        assert account_created["ok"]
        account_proof = account_created["data"]["token"]
        async with maker.begin() as db:
            account_row = await db.scalar(
                select(UserInvitation).where(UserInvitation.token == account_proof)
            )
            assert account_row is not None
            invitation_ids.append(account_row.id)
            account_only = await create_user(
                db,
                "employee",
                account_only_email,
                "Account-only existing employee",
            )
        actor_ids.append(account_only.id)
        account_only_token = create_access_token(
            str(account_only.id), "employee", secret=secret, expire_minutes=15
        )
        account_accept = _command(
            "accept_invitation", account_only_token, {"invitation_token": account_proof}
        )
        operation_ids.append(uuid.UUID(account_accept["operation_id"]))
        account_rejected = await asyncio.to_thread(
            _node, identity, control, account_accept
        )
        assert account_rejected["ok"] is False
        assert account_rejected["error"] == {
            "code": "policy_rejected",
            "status_code": 409,
            "detail": "该邀请仅用于创建新账号，已有账号只能接受带目标项目的邀请",
        }
        assert account_proof not in json.dumps(account_rejected, ensure_ascii=False)
        async with maker() as db:
            account_row = await db.get(UserInvitation, account_row.id)
            assert account_row is not None and account_row.accepted_at is None
            assert (
                await db.scalar(
                    select(ProjectMember.id).where(
                        ProjectMember.user_id == account_only.id
                    )
                )
                is None
            )
        record_property(
            "invitation_existing_acceptance",
            "issuer-project-owner-fence,resend-after-owner-transfer,existing-account-role-preserved,concurrent-accept,replay,account-only-rejected,labeling-work-hint",
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_invitations(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            invitation_ids=invitation_ids,
            user_operation_ids=operation_ids,
        )


def _kill_process(process):
    if process.process.poll() is None:
        process.process.kill()
        process.process.wait(timeout=8)
    process.log.close()


async def test_mail_v2_recovers_unknown_delivery_keeps_v1_draining_and_suppresses_stale_links(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    encryption_key = Fernet.generate_key().decode()
    suffix = uuid.uuid4().hex
    actor_ids = [actor]
    project_ids = []
    invitation_ids = []
    operation_ids = []
    recovery_operation_ids = []
    identity = mail_v1 = mail_v2 = None
    smtp_v1 = smtp_v2 = smtp_blocked = None
    try:
        async with maker.begin() as db:
            actor_user = await db.get(User, actor)
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
            enable_invitations=True,
            recovery_mail_key=encryption_key,
        )
        manager_token = _token(actor, secret)
        current_invite = _command(
            "create_invitation",
            manager_token,
            {
                "email": f"mail-v2-current-{suffix}@example.test",
                "role": "employee",
                "group_name": None,
                "project_id": None,
                "project_member_role": None,
            },
        )
        operation_ids.append(uuid.UUID(current_invite["operation_id"]))
        current_created = await asyncio.to_thread(
            _node, identity, control, current_invite
        )
        assert current_created["ok"]
        old_proof = current_created["data"]["token"]
        async with maker() as db:
            current_row = await db.scalar(
                select(UserInvitation).where(UserInvitation.token == old_proof)
            )
            assert current_row is not None
            invitation_ids.append(current_row.id)

        stale_send = _command(
            "send_invitation_email",
            manager_token,
            {"invitation_id": str(current_row.id)},
        )
        stale_send_id = uuid.UUID(stale_send["operation_id"])
        operation_ids.append(stale_send_id)
        stale_send_result = await asyncio.to_thread(
            _node, identity, control, stale_send
        )
        assert (
            stale_send_result["ok"]
            and stale_send_result["data"]["mail_status"] == "pending"
        )
        assert (
            await asyncio.to_thread(_node, identity, control, stale_send)
        ) == stale_send_result
        stale_rows = await _mail_rows(maker, current_row.id)
        assert len(stale_rows) == 1 and stale_rows[0]["status"] == "pending"
        assert old_proof not in stale_rows[0]["token_hash"]
        assert old_proof not in stale_rows[0]["token_ciphertext"]

        resend = _command(
            "resend_invitation", manager_token, {"invitation_id": str(current_row.id)}
        )
        operation_ids.append(uuid.UUID(resend["operation_id"]))
        resent = await asyncio.to_thread(_node, identity, control, resend)
        assert resent["ok"] and resent["data"]["token"] != old_proof
        current_proof = resent["data"]["token"]
        current_send = _command(
            "send_invitation_email",
            manager_token,
            {"invitation_id": str(current_row.id)},
        )
        current_send_id = uuid.UUID(current_send["operation_id"])
        operation_ids.append(current_send_id)
        current_send_result = await asyncio.to_thread(
            _node, identity, control, current_send
        )
        assert current_send_result["ok"]

        # Pending mail is revalidated after accept/revoke, so neither stale
        # invitation is delivered when the independent worker later claims.
        async with maker.begin() as db:
            target = await create_user(
                db,
                "employee",
                f"mail-v2-accepted-{suffix}@example.test",
                "Accepted before mail claim",
            )
            actor_ids.append(target.id)
            project = await create_project(
                db, owner_id=actor, name=f"Mail acceptance project {suffix}"
            )
            project_ids.append(project.id)
        target_token = create_access_token(
            str(target.id), "employee", secret=secret, expire_minutes=15
        )
        accepted_invite = _command(
            "create_invitation",
            manager_token,
            {
                "email": target.email,
                "role": "employee",
                "group_name": None,
                "project_id": str(project.id),
                "project_member_role": "annotator",
            },
        )
        operation_ids.append(uuid.UUID(accepted_invite["operation_id"]))
        accepted_created = await asyncio.to_thread(
            _node, identity, control, accepted_invite
        )
        assert accepted_created["ok"]
        accepted_proof = accepted_created["data"]["token"]
        async with maker() as db:
            accepted_invitation = await db.scalar(
                select(UserInvitation).where(UserInvitation.token == accepted_proof)
            )
            assert accepted_invitation is not None
            invitation_ids.append(accepted_invitation.id)
        accepted_send = _command(
            "send_invitation_email",
            manager_token,
            {"invitation_id": str(accepted_invitation.id)},
        )
        operation_ids.append(uuid.UUID(accepted_send["operation_id"]))
        assert (await asyncio.to_thread(_node, identity, control, accepted_send))["ok"]
        accept = _command(
            "accept_invitation", target_token, {"invitation_token": accepted_proof}
        )
        operation_ids.append(uuid.UUID(accept["operation_id"]))
        assert (await asyncio.to_thread(_node, identity, control, accept))["ok"]

        revoked_invite = _command(
            "create_invitation",
            manager_token,
            {
                "email": f"mail-v2-revoked-{suffix}@example.test",
                "role": "employee",
                "group_name": None,
                "project_id": None,
                "project_member_role": None,
            },
        )
        operation_ids.append(uuid.UUID(revoked_invite["operation_id"]))
        revoked_created = await asyncio.to_thread(
            _node, identity, control, revoked_invite
        )
        assert revoked_created["ok"]
        revoked_proof = revoked_created["data"]["token"]
        async with maker() as db:
            revoked_invitation = await db.scalar(
                select(UserInvitation).where(UserInvitation.token == revoked_proof)
            )
            assert revoked_invitation is not None
            invitation_ids.append(revoked_invitation.id)
        revoked_send = _command(
            "send_invitation_email",
            manager_token,
            {"invitation_id": str(revoked_invitation.id)},
        )
        operation_ids.append(uuid.UUID(revoked_send["operation_id"]))
        assert (await asyncio.to_thread(_node, identity, control, revoked_send))["ok"]
        revoke = _command(
            "revoke_invitation",
            manager_token,
            {"invitation_id": str(revoked_invitation.id)},
        )
        operation_ids.append(uuid.UUID(revoke["operation_id"]))
        assert (await asyncio.to_thread(_node, identity, control, revoke))["ok"]

        recovery_reset = _recovery_command(
            "request_password_reset",
            {"email": actor_user.email, "captcha_verified": True},
        )
        recovery_id = uuid.UUID(recovery_reset["operation_id"])
        recovery_operation_ids.append(recovery_id)
        assert (
            await asyncio.to_thread(_node_recovery, identity, control, recovery_reset)
        )["ok"]
        async with maker() as db:
            current_rows = await db.execute(
                text(
                    "SELECT id,status FROM public.identity_credential_mail_outbox "
                    "WHERE invitation_id=:invitation_id AND purpose='invitation' "
                    "ORDER BY created_at,id"
                ),
                {"invitation_id": current_row.id},
            )
            current_mail_rows = list(current_rows.mappings())
            assert len(current_mail_rows) == 2
            assert current_mail_rows[-1]["status"] == "pending"
            assert all(
                row["status"] in {"pending", "cancelled", "rejected", "expired"}
                for row in current_mail_rows
            )

        smtp_v1 = SMTPInbox()
        mail_v1 = start_mail(test_engine, tmp_path, encryption_key, smtp_v1)
        await smtp_v1.wait(1)
        await asyncio.to_thread(mail_v1.stop)
        mail_v1 = None
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT status FROM public.identity_credential_mail_outbox WHERE id=:id"
                    ),
                    {"id": recovery_id},
                )
                == "delivered"
            )
            untouched = await db.execute(
                text(
                    "SELECT status,worker_id FROM public.identity_credential_mail_outbox "
                    "WHERE invitation_id=:invitation_id AND purpose='invitation'"
                ),
                {"invitation_id": current_row.id},
            )
            untouched_rows = list(untouched.mappings())
            assert len(untouched_rows) == 2
            assert untouched_rows[-1]["status"] == "pending"
            assert all(
                (row["status"] == "pending" and row["worker_id"] is None)
                or row["status"] in {"cancelled", "rejected", "expired"}
                for row in untouched_rows
            )

        # The V2 worker may send only the current link; the older purpose-aware
        # Recovery V1 sender has already drained its own reset without claiming
        # any of these invitation rows.
        smtp_v2 = SMTPInbox(lose_ack=1)
        mail_v2 = _start_mail_v2(test_engine, tmp_path, encryption_key, smtp_v2)
        await smtp_v2.wait(2)
        current_rows = await _mail_rows(maker, current_row.id)
        assert len(current_rows) == 2
        stale_row, delivered_row = current_rows
        assert stale_row["status"] in {"cancelled", "rejected", "expired"}
        assert delivered_row["status"] == "delivered" and delivered_row["attempt"] >= 2
        expected_message_id = f"<aap.{delivered_row['id']}.invitation@aap.invalid>"
        assert [message["Message-ID"] for message in smtp_v2.messages] == [
            expected_message_id,
            expected_message_id,
        ]
        assert smtp_v2.tokens() == [current_proof, current_proof]
        assert old_proof not in smtp_v2.tokens()
        assert accepted_proof not in smtp_v2.tokens()
        assert revoked_proof not in smtp_v2.tokens()
        accepted_rows = await _mail_rows(maker, accepted_invitation.id)
        revoked_rows = await _mail_rows(maker, revoked_invitation.id)
        assert accepted_rows and all(
            row["status"] in {"cancelled", "rejected", "expired"}
            for row in accepted_rows
        )
        assert revoked_rows and all(
            row["status"] in {"cancelled", "rejected", "expired"}
            for row in revoked_rows
        )
        await asyncio.to_thread(mail_v2.stop)
        mail_v2 = None

        # Kill the second publisher after SMTP accepted DATA but before 250;
        # takeover retries the same outbox row and stable Message-ID.
        smtp_blocked = SMTPInbox(pause_ack=True)
        mail_v2 = _start_mail_v2(test_engine, tmp_path, encryption_key, smtp_blocked)
        send_after_kill = _command(
            "send_invitation_email",
            manager_token,
            {"invitation_id": str(current_row.id)},
        )
        operation_ids.append(uuid.UUID(send_after_kill["operation_id"]))
        killed_send_result = await asyncio.to_thread(
            _node, identity, control, send_after_kill
        )
        assert killed_send_result["ok"]
        await smtp_blocked.wait(1)
        latest_rows = await _mail_rows(maker, current_row.id)
        leased = latest_rows[-1]
        assert leased["status"] == "leased" and leased["lease_expires_at"] is not None
        stable_message_id = f"<aap.{leased['id']}.invitation@aap.invalid>"
        _kill_process(mail_v2)
        mail_v2 = None
        async with maker.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.identity_credential_mail_outbox "
                    "SET lease_expires_at=clock_timestamp()-interval '1 second',"
                    "available_at=clock_timestamp()-interval '1 second' "
                    "WHERE id=:id AND status='leased'"
                ),
                {"id": leased["id"]},
            )
        smtp_blocked.release.set()
        mail_v2 = _start_mail_v2(test_engine, tmp_path, encryption_key, smtp_blocked)
        await smtp_blocked.wait(2)
        second_outbox = await _wait_row_status(
            maker, leased["id"], "delivered", timeout=20
        )
        assert second_outbox["attempt"] >= 2
        assert [message["Message-ID"] for message in smtp_blocked.messages] == [
            stable_message_id,
            stable_message_id,
        ]
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='invitation.send_email'"
                    ),
                    {"actor": actor},
                )
                >= 2
            )
        log_path = (
            tmp_path / f"invitation-mail-v2-{mail_v2.origin.rsplit(':', 1)[1]}.log"
        )
        assert log_path.is_file()
        assert old_proof not in log_path.read_text(errors="replace")
        assert current_proof not in log_path.read_text(errors="replace")
        record_property(
            "invitation_mail_v2",
            "recovery-v1-still-drains,stale-resend-accepted-revoked-links-suppressed,unknown-smtp-stable-message-id,killed-worker-lease-takeover",
        )
    finally:
        if mail_v1 is not None:
            await asyncio.to_thread(mail_v1.stop)
        if mail_v2 is not None:
            await asyncio.to_thread(mail_v2.stop)
        for inbox in (smtp_v1, smtp_v2, smtp_blocked):
            if inbox is not None:
                inbox.close()
        if identity is not None:
            identity.stop()
        await _cleanup_invitations(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            invitation_ids=invitation_ids,
            user_operation_ids=operation_ids,
            recovery_operation_ids=recovery_operation_ids,
        )


async def test_invitation_container_unbounded_catalog_has_only_three_owner_functions(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    suffix = uuid.uuid4().hex[:12]
    actor_ids = [actor]
    operation_ids = []
    invitation_ids = []
    identity = None
    count = 600
    try:
        now = datetime.now(UTC)
        expires = now + timedelta(days=7)
        async with maker.begin() as db:
            rows = [
                UserInvitation(
                    email=f"catalog-{suffix}-{index}@example.test",
                    role="employee",
                    group_name=None,
                    project_role=None,
                    project_id=None,
                    token=f"catalog_{suffix}_{index}_{secrets.token_hex(8)}",
                    expires_at=expires,
                    invited_by=actor,
                    created_at=now - timedelta(seconds=index),
                )
                for index in range(count)
            ]
            db.add_all(rows)
            await db.flush()
            invitation_ids.extend(row.id for row in rows)
        identity = await _start_container_identity(
            test_engine,
            tmp_path,
            secret,
            control,
            enable_accounts=False,
            enable_groups=False,
            enable_deactivation=False,
            enable_deactivation_operator=False,
            enable_invitations=True,
            enable_presence=False,
        )
        token = _token(actor, secret)
        list_request = _command(
            "list_invitations", token, {"status": "all", "scope": "me"}
        )
        operation_ids.append(uuid.UUID(list_request["operation_id"]))
        listed = await asyncio.to_thread(_node, identity, control, list_request)
        assert listed["ok"] and len(listed["data"]["items"]) >= count
        list_bytes = len(json.dumps(listed).encode())
        assert list_bytes > 64 * 1024

        export = _command(
            "export_invitations",
            token,
            {
                "format": "json",
                "status": "all",
                "scope": "me",
                "project_id": None,
                "role": None,
                "email": None,
                "search": None,
                "created_from": None,
                "created_to": None,
            },
        )
        operation_ids.append(uuid.UUID(export["operation_id"]))
        exported = await asyncio.to_thread(_node, identity, control, export)
        assert exported["ok"] and len(exported["data"]["items"]) >= count
        export_bytes = len(json.dumps(exported).encode())
        assert export_bytes > 64 * 1024
        details = _inspect_docker(identity.container_id)
        assert details["HostConfig"]["ReadonlyRootfs"] is True
        assert details["Config"]["User"] == "10003:10003"
        assert 0 < details["HostConfig"]["Memory"] <= 256 * 1024 * 1024

        async with maker.begin() as db:
            functions = set(
                (
                    await db.execute(
                        text(
                            "SELECT proc.proname FROM pg_catalog.pg_proc AS proc "
                            "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                            "WHERE ns.nspname='aap_identity_invitations_v1' "
                            "AND pg_catalog.has_function_privilege("
                            "'identity_invitation_client',proc.oid,'EXECUTE')"
                        )
                    )
                ).scalars()
            )
            assert functions == {
                "invitation_policy",
                "prepare_invitation_command",
                "apply_invitation_command",
            }
            for table in (
                "public.users",
                "public.user_invitations",
                "public.projects",
                "public.project_members",
                "public.groups",
                "public.system_settings",
                "public.task_batches",
            ):
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert not await db.scalar(
                        text(
                            "SELECT pg_catalog.has_table_privilege("
                            ":role,:table_name,:privilege)"
                        ),
                        {
                            "role": "identity_invitation_client",
                            "table_name": table,
                            "privilege": privilege,
                        },
                    )
            for schema_name in ("aap_identity_v1", "aap_workspace_v1", "aap_group_v1"):
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_proc AS proc "
                        "JOIN pg_catalog.pg_namespace AS ns ON ns.oid=proc.pronamespace "
                        "WHERE ns.nspname=:schema AND pg_catalog.has_function_privilege("
                        "'identity_invitation_client',proc.oid,'EXECUTE'))"
                    ),
                    {"schema": schema_name},
                )
        record_property(
            "invitation_bounded_container",
            json.dumps(
                {
                    "rows": len(listed["data"]["items"]),
                    "list_bytes": list_bytes,
                    "export_bytes": export_bytes,
                    "memory": details["HostConfig"]["Memory"],
                    "readonly_rootfs": details["HostConfig"]["ReadonlyRootfs"],
                    "user": details["Config"]["User"],
                    "functions": sorted(functions),
                }
            ),
        )
    finally:
        if identity is not None:
            identity.stop()
        await _cleanup_invitations(
            maker,
            actor_ids=actor_ids,
            invitation_ids=invitation_ids,
            user_operation_ids=operation_ids,
        )
