from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aap_identity.authenticator import Authenticator
from aap_identity.invitation_manager import InvitationManager
from aap_identity.invitation_postgres import InvitationPostgresData
from aap_identity.postgres import PostgresData
from aap_identity.invitation_contract import (
    NativeInvitationApplyReply,
    NativeInvitationPrepareReply,
    decode_invitation_request,
    decode_native_invitation_apply_command,
    decode_native_invitation_apply_reply,
    decode_native_invitation_prepare_command,
    decode_native_invitation_prepare_reply,
)

from app.db.models.project import Project
from app.db.models.user import User
from app.db.models.user_invitation import UserInvitation
from app.db.native_schema import NATIVE_MANAGED_TABLES
from tests.factory import create_project
from tests.test_identity_admission_live import _dsn, _token
from tests.test_identity_auth_native import _cleanup, _seed


_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_SUPPORT = _SQL_DIR / "0242_identity_invitations_support.sql"
_ARTIFACT = _SQL_DIR / "0242_identity_invitations.sql"
_SCHEMA = _SQL_DIR / "0242_identity_invitations.schema.json"
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0242_identity_invitations.py"
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_ROLE_GRANTS = _ROOT / "infra/docker/identity-invitations-grants.sql"
_LEGACY_GRANTS = _ROOT / "infra/docker/lan-production-grants.sql"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the explicitly enabled owned function-only role verifier",
)
async def test_narrow_postgres_client_creates_project_invitation(test_engine):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    operation_id = uuid.uuid4()
    secret = "native-project-invitation-test-secret"
    project = None
    email = f"narrow-project-invitation-{operation_id.hex}@invite.test"
    try:
        async with maker.begin() as db:
            project = await create_project(db, owner_id=fixture["actor_id"])
        request = decode_invitation_request(
            {
                "schema": "aap-identity-invitations/v1",
                "operation": "create_invitation",
                "operation_id": str(operation_id),
                "token": _token(fixture["actor_id"], secret),
                "payload": {
                    "email": email,
                    "role": "employee",
                    "project_id": str(project.id),
                    "project_member_role": "annotator",
                    "group_name": "  Native group  ",
                },
                "audit_context": _context(),
            }
        )
        reply = await InvitationManager(
            Authenticator(PostgresData(_dsn("AUTH", test_engine)), secret),
            InvitationPostgresData(_dsn("INVITATION", test_engine)),
            secret,
        ).execute(request)
        assert reply.data.project_id == project.id
        assert reply.data.project_member_role == "annotator"
        async with maker() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT email FROM public.user_invitations WHERE token=:token"
                    ),
                    {"token": reply.data.token},
                )
                == email
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT target_id FROM public.audit_logs WHERE actor_id=:actor AND action='user.invite'"
                    ),
                    {"actor": fixture["actor_id"]},
                )
                == email[:64]
            )
    finally:
        async with maker.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                text("DELETE FROM public.audit_logs WHERE actor_id=:id"),
                {"id": fixture["actor_id"]},
            )
            await db.execute(
                text(
                    "DELETE FROM public.identity_invitation_operation_receipts WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            await db.execute(
                delete(UserInvitation).where(
                    UserInvitation.invited_by == fixture["actor_id"]
                )
            )
            if project is not None:
                await db.execute(delete(Project).where(Project.id == project.id))
        await _cleanup(test_engine, fixture)


def _context() -> dict[str, str]:
    return {
        "method": "POST",
        "path": "/api/v1/invitations/identity",
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }


def _actor(actor_id: uuid.UUID, *, role: str = "super_admin") -> dict:
    return {
        "actor_id": str(actor_id),
        "name": "stale actor name",
        "email": "stale-actor@example.test",
        "role": role,
        "is_active": True,
        "credential_type": "jwt",
        "credential_id": str(uuid.uuid4()),
        "generation": 0,
        "scopes": None,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }


def _command(
    operation: str,
    operation_id: uuid.UUID,
    actor: dict | None,
    args: dict,
    *,
    fingerprint: str | None = None,
) -> dict:
    return {
        "schema": "aap-identity-invitations-native/v1",
        "operation": operation,
        "operation_id": str(operation_id),
        "actor": actor,
        "expected_key_hash": None,
        "request_fingerprint": fingerprint or uuid.uuid4().hex * 2,
        "derivation_id": "d" * 64,
        "audit_context": _context(),
        "config": {
            "max_invitations_per_day": 30,
            "invitation_ttl_days": 7,
            "frontend_base_url": "https://labels.example.test",
            "expire_minutes": 120,
        },
        "args": args,
    }


async def _call(session: AsyncSession, function: str, command: dict) -> dict:
    reply = await session.scalar(
        text(f"SELECT {function}(CAST(:command AS jsonb))"),
        {"command": json.dumps(command)},
    )
    if reply.get("ok") is True:
        model = (
            NativeInvitationPrepareReply
            if function.endswith("prepare_invitation_command")
            else NativeInvitationApplyReply
        )
        try:
            model.model_validate_json(json.dumps(reply))
        except ValidationError as error:
            pytest.fail(
                f"Native reply shape differs: {[(entry['loc'], entry['type']) for entry in error.errors(include_input=False)]}",
                pytrace=False,
            )
        if function.endswith("prepare_invitation_command"):
            decode_native_invitation_prepare_reply(
                decode_native_invitation_prepare_command(command), reply
            )
        elif function.endswith("apply_invitation_command"):
            decode_native_invitation_apply_reply(
                decode_native_invitation_apply_command(command), reply
            )
    return reply


def test_frozen_invitation_artifacts_and_transaction_contract():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    support = _SUPPORT.read_text(encoding="utf-8")
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    role_grants = _ROLE_GRANTS.read_text(encoding="utf-8")
    legacy_grants = _LEGACY_GRANTS.read_text(encoding="utf-8")

    portable_schema = json.loads(
        (
            _ROOT
            / "apps/identity/src/aap_identity/native_invitation_protocol.schema.json"
        ).read_text()
    )
    assert {
        key: value for key, value in schema.items() if not key.startswith("x-")
    } == portable_schema
    assert "identity_invitation_operation_receipts" in NATIVE_MANAGED_TABLES

    assert '"0242_identity_invitations_support.sql"' in migration
    assert (
        migration.index('"0242_identity_invitations_support.sql"')
        < migration.index('"0242_invitation_mail.sql"')
        < migration.index('"0242_identity_invitations.sql"')
    )
    assert (
        "REFERENCES"
        not in support[
            support.index(
                "CREATE TABLE public.identity_invitation_operation_receipts"
            ) : support.index(
                "CREATE INDEX ix_identity_invitation_operation_receipts_actor"
            )
        ]
    )
    assert (
        "token"
        not in support[
            support.index(
                "CREATE TABLE public.identity_invitation_operation_receipts"
            ) : support.index(
                "CREATE INDEX ix_identity_invitation_operation_receipts_actor"
            )
        ]
    )
    assert "_lock_users(v_user_ids)" in artifact
    assert "FOR KEY SHARE NOWAIT" in artifact
    assert "hashtextextended(v_args->>'email', 0)" in artifact
    assert "invitation_quota_reached" in artifact
    assert "AAP_INVITATION_PREVIEW_ROLLBACK" in artifact
    assert "record_identity_invitation_event" in support
    assert "invitation_work_summary" in support
    assert "invitation_policy" in support
    assert "public.project_members" not in artifact
    assert "public.projects" not in artifact
    assert "public.task_batches" not in artifact
    assert "identity_invitation_client" in role_grants
    assert "identity_mail_v2_client" in role_grants
    assert "current_setting('aap.identity_invitation_role')" not in module_grants
    assert "current_setting('aap.identity_mail_v2_role')" not in module_grants
    assert "count(DISTINCT n) FROM unnest(role_names) AS n) <> 16" in module_grants
    assert (
        role_grants.count(
            "aap_identity_invitations_v1.prepare_invitation_command(jsonb)"
        )
        >= 2
    )
    assert role_grants.count("aap_identity_mail_v2.claim_mail(jsonb)") >= 2
    assert (
        "aap_identity_groups_v1.resolve_invitation_group(uuid, text)"
        not in legacy_grants
    )
    assert schema["x-login-role-capabilities"]["identity_invitation_client"] == [
        "aap_identity_invitations_v1.invitation_policy()",
        "aap_identity_invitations_v1.prepare_invitation_command(jsonb)",
        "aap_identity_invitations_v1.apply_invitation_command(jsonb)",
    ]


@pytest.mark.asyncio
async def test_create_receipt_replays_without_storing_the_raw_token(test_engine):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    email = f"invitation-{operation_id.hex}@example.test"
    actor = _actor(fixture["actor_id"])
    args = {
        "email": email,
        "role": "employee",
        "group_name": None,
        "project_id": None,
        "project_member_role": None,
    }
    fingerprint = "a" * 64
    prepare = _command(
        "create_invitation", operation_id, actor, args, fingerprint=fingerprint
    )
    raw_token = f"opaque invitation {operation_id.hex}"
    derived = {
        "password_hash": None,
        "claims": None,
        "token": raw_token,
        "batch_tokens": None,
        "mail_token_hash": None,
        "mail_token_ciphertext": None,
        "mail_encryption_id": None,
        "frontend_base_url": "https://labels.example.test",
        "mail_frontend_url": None,
    }
    applied = {**prepare, "derived": derived}
    try:
        async with maker.begin() as session:
            prepared = await _call(
                session,
                "aap_identity_invitations_v1.prepare_invitation_command",
                prepare,
            )
        assert prepared["ok"] is True
        assert 1 <= prepared["data"]["policy"]["max_invitations_per_day"] <= 1000

        async with maker.begin() as session:
            created = await _call(
                session,
                "aap_identity_invitations_v1.apply_invitation_command",
                applied,
            )
        assert created["ok"] is True
        invitation_id = uuid.UUID(created["data"]["result"]["invitation"]["id"])
        assert (
            created["data"]["result"]["frontend_base_url"]
            == "https://labels.example.test"
        )
        assert "token" not in json.dumps(created["data"]["result"])

        async with maker.begin() as session:
            stored = await session.get(UserInvitation, invitation_id)
            receipt = await session.scalar(
                text(
                    "SELECT result FROM public.identity_invitation_operation_receipts "
                    "WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            audits = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE action='user.invite' "
                    "AND target_id=:email"
                ),
                {"email": email},
            )
        assert stored is not None and stored.token == raw_token
        assert raw_token not in json.dumps(receipt)
        assert audits == 1

        async with maker.begin() as session:
            replay = await _call(
                session,
                "aap_identity_invitations_v1.apply_invitation_command",
                applied,
            )
            replay_audits = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE action='user.invite' "
                    "AND target_id=:email"
                ),
                {"email": email},
            )
        assert replay["data"]["replayed"] is True
        assert replay["data"]["result"] == created["data"]["result"]
        assert replay_audits == 1
    finally:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM public.identity_invitation_operation_receipts "
                    "WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            await session.execute(
                delete(UserInvitation).where(UserInvitation.email == email)
            )
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_read_commands_reject_null_status_and_scope_fields(test_engine):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    actor = _actor(fixture["actor_id"])
    invalid_reads = (
        ("list_invitations", {"status": None, "scope": "me"}),
        (
            "query_invitations",
            {
                "page": 1,
                "page_size": 20,
                "status": None,
                "scope": "me",
                "project_id": None,
                "role": None,
                "email": None,
                "search": None,
                "created_from": None,
                "created_to": None,
            },
        ),
    )
    try:
        for operation, args in invalid_reads:
            command = _command(operation, uuid.uuid4(), actor, args)
            async with maker.begin() as session:
                response = await _call(
                    session,
                    "aap_identity_invitations_v1.prepare_invitation_command",
                    command,
                )
            assert response["ok"] is False
            assert response["error"]["code"] == "invalid_request"
    finally:
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_create_reports_existing_account_role_conflict_with_legacy_detail(
    test_engine,
):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    target_id = uuid.uuid4()
    project_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10].upper()
    email = f"invitation-incompatible-{suffix.lower()}@example.test"
    command = _command(
        "create_invitation",
        uuid.uuid4(),
        _actor(fixture["actor_id"]),
        {
            "email": email,
            "role": "employee",
            "group_name": None,
            "project_id": str(project_id),
            "project_member_role": "annotator",
        },
    )
    try:
        async with maker.begin() as session:
            session.add_all(
                [
                    User(
                        id=target_id,
                        email=email,
                        name="Existing viewer",
                        password_hash="test-only",
                        role="viewer",
                        is_active=True,
                    ),
                    Project(
                        id=project_id,
                        display_id=f"II-{suffix}",
                        name=f"Invitation role {suffix}",
                        type_label="Image",
                        type_key="image",
                        owner_id=fixture["actor_id"],
                    ),
                ]
            )
        async with maker.begin() as session:
            response = await _call(
                session,
                "aap_identity_invitations_v1.prepare_invitation_command",
                command,
            )
        assert response["ok"] is False
        assert response["error"]["code"] == "policy_rejected"
        assert response["error"]["policy"] == "incompatible_account_role"
        assert response["error"]["detail"] == (
            "当前账号是观察者，无法持有标注员项目职责。为避免静默修改平台角色，"
            "请联系管理员调整账号角色后再接受邀请"
        )
    finally:
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.projects WHERE id=:id"), {"id": project_id}
            )
            await session.execute(delete(User).where(User.id == target_id))
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_create_rejects_platform_viewer_with_nonviewer_project_role(test_engine):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10].upper()
    command = _command(
        "create_invitation",
        uuid.uuid4(),
        _actor(fixture["actor_id"]),
        {
            "email": f"invitation-viewer-{suffix.lower()}@example.test",
            "role": "viewer",
            "group_name": None,
            "project_id": str(project_id),
            "project_member_role": "annotator",
        },
    )
    try:
        async with maker.begin() as session:
            session.add(
                Project(
                    id=project_id,
                    display_id=f"II-{suffix}",
                    name=f"Invitation role {suffix}",
                    type_label="Image",
                    type_key="image",
                    owner_id=fixture["actor_id"],
                )
            )
        async with maker.begin() as session:
            response = await _call(
                session,
                "aap_identity_invitations_v1.prepare_invitation_command",
                command,
            )
        assert response["ok"] is False
        assert response["error"]["code"] == "policy_rejected"
        assert response["error"]["policy"] == "platform_project_role_incompatible"
        assert response["error"]["detail"] == (
            "账号平台角色与项目职责不兼容：员工可持有标注员/质检员/观察者，"
            "平台观察者仅可持有观察者，管理员通过项目负责人身份管理项目"
        )
    finally:
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.projects WHERE id=:id"), {"id": project_id}
            )
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_batch_receipt_replay_rejects_a_project_scope_transfer(test_engine):
    fixture = await _seed(test_engine, role="project_admin", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    invitation_id = uuid.uuid4()
    project_id = uuid.uuid4()
    replacement_owner_id = uuid.uuid4()
    suffix = uuid.uuid4().hex[:10].upper()
    email = f"invitation-scope-{operation_id.hex}@example.test"
    args = {
        "items": [
            {
                "index": 0,
                "email": email,
                "role": "employee",
                "group_name": None,
                "project_id": str(project_id),
                "project_member_role": "viewer",
                "preview_invitation_id": None,
                "preview_token": None,
            }
        ]
    }
    command = _command(
        "apply_invitation_batch", operation_id, _actor(fixture["actor_id"]), args
    )
    command["derived"] = {
        "password_hash": None,
        "claims": None,
        "token": None,
        "batch_tokens": [
            {
                "index": 0,
                "invitation_id": str(invitation_id),
                "token": f"batch-token-{operation_id.hex}",
            }
        ],
        "mail_token_hash": None,
        "mail_token_ciphertext": None,
        "mail_encryption_id": None,
        "frontend_base_url": "https://labels.example.test",
        "mail_frontend_url": None,
    }
    try:
        async with maker.begin() as session:
            session.add_all(
                [
                    User(
                        id=replacement_owner_id,
                        email=f"invitation-owner-{suffix}@example.test",
                        name="Replacement project owner",
                        password_hash="test-only",
                        role="project_admin",
                        is_active=True,
                    ),
                    Project(
                        id=project_id,
                        display_id=f"II-{suffix}",
                        name=f"Invitation scope {suffix}",
                        type_label="Image",
                        type_key="image",
                        owner_id=fixture["actor_id"],
                    ),
                ]
            )
        async with maker.begin() as session:
            prepared = await _call(
                session,
                "aap_identity_invitations_v1.prepare_invitation_command",
                {key: value for key, value in command.items() if key != "derived"},
            )
        assert prepared["ok"] is True, prepared
        async with maker.begin() as session:
            applied = await _call(
                session,
                "aap_identity_invitations_v1.apply_invitation_command",
                command,
            )
        assert applied["ok"] is True
        assert applied["data"]["result"]["items"][0]["ok"] is True

        async with maker.begin() as session:
            await session.execute(
                text("UPDATE public.projects SET owner_id=:owner WHERE id=:project"),
                {"owner": replacement_owner_id, "project": project_id},
            )
        async with maker.begin() as session:
            replay = await _call(
                session,
                "aap_identity_invitations_v1.apply_invitation_command",
                command,
            )
        assert replay["ok"] is False
        assert replay["error"]["code"] == "operation_conflict"
    finally:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM public.identity_invitation_operation_receipts WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            await session.execute(
                delete(UserInvitation).where(UserInvitation.email == email)
            )
            await session.execute(
                text("DELETE FROM public.projects WHERE id=:id"), {"id": project_id}
            )
            await session.execute(delete(User).where(User.id == replacement_owner_id))
        await _cleanup(test_engine, fixture)


@pytest.mark.asyncio
async def test_batch_preview_executes_create_then_rolls_back_rows_receipts_and_audit(
    test_engine,
):
    fixture = await _seed(test_engine, role="super_admin", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    email = f"preview-{operation_id.hex}@example.test"
    invitation_id = uuid.uuid4()
    args = {
        "items": [
            {
                "index": 0,
                "email": email,
                "role": "employee",
                "group_name": None,
                "project_id": None,
                "project_member_role": None,
                "preview_invitation_id": str(invitation_id),
                "preview_token": f"preview-secret-{operation_id.hex}",
            }
        ]
    }
    command = _command(
        "preview_invitation_batch", operation_id, _actor(fixture["actor_id"]), args
    )
    try:
        async with maker.begin() as session:
            result = await _call(
                session,
                "aap_identity_invitations_v1.prepare_invitation_command",
                command,
            )
        assert result["ok"] is True
        assert result["data"]["result"]["preview"] is True
        assert result["data"]["result"]["succeeded"] == 1
        assert result["data"]["result"]["items"][0]["invitation_id"] == str(
            invitation_id
        )
        async with maker() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM public.user_invitations WHERE id=:id"),
                    {"id": invitation_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_invitation_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE action='user.bulk_invite' "
                        "AND target_id=:id"
                    ),
                    {"id": str(invitation_id)},
                )
                == 0
            )
    finally:
        await _cleanup(test_engine, fixture)
