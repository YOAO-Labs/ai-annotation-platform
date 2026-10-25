from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.notification import Notification
from app.db.models.user import User
from app.db.native_schema import NATIVE_MANAGED_TABLES
from tests.test_identity_auth_native import _cleanup, _seed


_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_ARTIFACT = _SQL_DIR / "0241_identity_deactivation.sql"
_SCHEMA = _SQL_DIR / "0241_identity_deactivation.schema.json"
_MIGRATION = (
    Path(__file__).parents[1] / "alembic/versions/0241_identity_deactivation.py"
)
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_LEGACY_GRANTS = _ROOT / "infra/docker/lan-production-grants.sql"
_ROLE_GRANTS = _ROOT / "infra/docker/identity-deactivation-grants.sql"


def _context() -> dict[str, str]:
    return {
        "method": "POST",
        "path": "/v1/identity/deactivation/execute",
        "ip": "127.0.0.1",
        "request_id": str(uuid.uuid4()),
    }


def _actor(actor_id: uuid.UUID, *, role: str = "employee") -> dict:
    return {
        "actor_id": str(actor_id),
        "name": "stale caller name",
        "email": "stale-caller@example.test",
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
    actor: dict,
    payload: dict,
    *,
    fingerprint: str | None = None,
) -> dict:
    return {
        "schema": "aap-identity-deactivation-native/v1",
        "operation": operation,
        "operation_id": str(operation_id),
        "actor": actor,
        "expected_key_hash": None,
        "request_fingerprint": fingerprint or uuid.uuid4().hex * 2,
        "derivation_id": "d" * 64,
        "audit_context": _context(),
        "payload": payload,
    }


def test_frozen_native_deactivation_artifacts_and_lock_contract():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    artifact = _ARTIFACT.read_text(encoding="utf-8")
    migration = _MIGRATION.read_text(encoding="utf-8")
    module_grants = _MODULE_GRANTS.read_text(encoding="utf-8")
    legacy_grants = _LEGACY_GRANTS.read_text(encoding="utf-8")
    role_grants = _ROLE_GRANTS.read_text(encoding="utf-8")

    assert "identity_deactivation_operation_receipts" in NATIVE_MANAGED_TABLES
    assert "identity_deactivation_due_receipts" in NATIVE_MANAGED_TABLES
    assert "identity_deactivation_requests" in NATIVE_MANAGED_TABLES
    assert "notification_delivery_outbox" in NATIVE_MANAGED_TABLES
    assert migration.index('"0241_deactivation_notifications.sql"') < migration.index(
        '"0241_identity_deactivation.sql"'
    )
    assert "lock_super_admin_invariant()" in artifact
    assert "FOR UPDATE NOWAIT" in artifact
    assert "identity_deactivation_due_receipts" in artifact
    assert "identity_deactivation_requests" in artifact
    assert "record_identity_deactivation_event" in artifact
    assert "create_identity_deactivation_notifications" in artifact
    assert "app.allow_audit_update" in artifact
    assert "REVOKE ALL ON FUNCTION" in artifact
    command = artifact[
        artifact.index(
            "CREATE FUNCTION aap_identity_deactivation_v1.execute_deactivation_command"
        ) : artifact.index(
            "REVOKE ALL ON FUNCTION aap_identity_deactivation_v1.execute_deactivation_command"
        )
    ]
    assert command.index("lock_super_admin_invariant()") < command.index(
        "_actor(v_actor, v_expected_key_hash)"
    )
    assert schema["$defs"]["policy"]["properties"]["policy"]["const"] == (
        "aap-identity-deactivation/v1"
    )
    assert "aap-identity-deactivation-due-native/v1" in json.dumps(schema)
    assert set(schema["x-postgresql-functions"]) == {
        "aap_identity_deactivation_v1.deactivation_policy()",
        "aap_identity_deactivation_v1.execute_deactivation_command(jsonb)",
        "aap_identity_deactivation_v1.due_policy()",
        "aap_identity_deactivation_v1.process_due_deactivations(jsonb)",
    }
    assert schema["x-login-role-capabilities"]["notification_publish_client"] == [
        "aap_notifications_v1.identity_delivery_policy()",
        "aap_notifications_v1.claim_identity_deliveries(jsonb)",
        "aap_notifications_v1.finish_identity_delivery(jsonb)",
    ]
    assert "identity_deactivation_operation_receipts" in module_grants
    assert "record_identity_deactivation_event" in module_grants
    assert "lock_super_admin_invariant" in legacy_grants
    assert "process_due_deactivations" in legacy_grants
    assert "notification_publish_client" in role_grants


@pytest.mark.asyncio
async def test_self_request_cancel_and_replay_are_frozen_and_atomic(test_engine):
    fixture = await _seed(test_engine, role="employee", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    admin_id = uuid.uuid4()
    admin_email = f"deactivation-admin-{admin_id.hex}@example.test"
    user_ids = [fixture["actor_id"], admin_id]
    request_id, cancel_id = uuid.uuid4(), uuid.uuid4()
    actor = _actor(fixture["actor_id"])
    reason = "\u2003privacy request\u2003"
    request = _command(
        "request_self_deactivation",
        request_id,
        actor,
        {"reason": reason},
        fingerprint="a" * 64,
    )
    cancel = _command(
        "cancel_self_deactivation", cancel_id, actor, {}, fingerprint="b" * 64
    )

    async def execute(command: dict) -> dict:
        async with maker.begin() as session:
            return await session.scalar(
                text(
                    "SELECT aap_identity_deactivation_v1.execute_deactivation_command("
                    "CAST(:command AS jsonb))"
                ),
                {"command": json.dumps(command)},
            )

    try:
        async with maker.begin() as session:
            session.add(
                User(
                    id=admin_id,
                    email=admin_email,
                    name="Deactivation notification receiver",
                    password_hash="test-only",
                    role="super_admin",
                    is_active=True,
                )
            )

        accepted = await execute(request)
        assert accepted["ok"] is True
        assert accepted["operation_id"] == str(request_id)
        account = accepted["data"]["account"]
        assert account["id"] == str(fixture["actor_id"])
        assert account["deactivation_requested_at"] is not None
        assert account["deactivation_scheduled_at"] is not None
        assert "deactivation_reason" not in account

        async with maker() as session:
            stored = await session.get(User, fixture["actor_id"])
            assert stored is not None
            assert stored.deactivation_reason == "privacy request"
            assert (
                stored.deactivation_scheduled_at - stored.deactivation_requested_at
                == timedelta(days=7)
            )
            audit_detail = await session.scalar(
                text(
                    "SELECT detail_json FROM public.audit_logs WHERE actor_id=:actor "
                    "AND action='user.deactivation_request' ORDER BY id DESC LIMIT 1"
                ),
                {"actor": fixture["actor_id"]},
            )
            assert audit_detail == {
                "scheduled_at": account["deactivation_scheduled_at"],
                "reason_present": True,
            }
            notification_count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(Notification)
                .where(
                    Notification.type == "user.deactivation_requested",
                    Notification.target_id == fixture["actor_id"],
                )
            )
            assert notification_count == 1

        replay = await execute(request)
        assert replay["data"] == accepted["data"]
        async with maker() as session:
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor "
                    "AND action='user.deactivation_request'"
                ),
                {"actor": fixture["actor_id"]},
            )
            assert audit_count == 1

        cancelled = await execute(cancel)
        assert cancelled["ok"] is True
        assert cancelled["data"]["account"]["deactivation_requested_at"] is None
        assert cancelled["data"]["account"]["deactivation_scheduled_at"] is None
        assert (await execute(request))["data"] == accepted["data"]
    finally:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM public.identity_deactivation_operation_receipts "
                    "WHERE operation_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [request_id, cancel_id]},
            )
            await session.execute(
                text(
                    "DELETE FROM public.identity_deactivation_requests "
                    "WHERE user_id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": user_ids},
            )
        await _cleanup(test_engine, fixture)
        async with maker.begin() as session:
            await session.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await session.execute(
                text("DELETE FROM public.audit_logs WHERE actor_id=:id"),
                {"id": admin_id},
            )
            await session.execute(sa.delete(User).where(User.id == admin_id))


@pytest.mark.asyncio
async def test_due_page_completes_legacy_request_redacts_audit_and_replays(test_engine):
    fixture = await _seed(test_engine, role="viewer", create_key=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    operation_id = uuid.uuid4()
    scheduled_at = datetime.now(UTC) - timedelta(minutes=5)
    requested_at = scheduled_at - timedelta(days=7)
    try:
        async with maker.begin() as session:
            user = await session.get(User, fixture["actor_id"])
            assert user is not None
            user.deactivation_requested_at = requested_at
            user.deactivation_reason = "legacy reason retained only on User"
            user.deactivation_scheduled_at = scheduled_at

        request = {
            "schema": "aap-identity-deactivation-due-native/v1",
            "operation_id": str(operation_id),
            "limit": 16,
        }
        async with maker.begin() as session:
            result = await session.scalar(
                text(
                    "SELECT aap_identity_deactivation_v1.process_due_deactivations("
                    "CAST(:request AS jsonb))"
                ),
                {"request": json.dumps(request)},
            )
        assert result == {
            "schema": "aap-identity-deactivation-due-native/v1",
            "ok": True,
            "operation_id": str(operation_id),
            "processed": 1,
            "examined": 1,
            "blocked_last_super_admin": 0,
            "has_more": False,
        }

        async with maker() as session:
            user = await session.get(User, fixture["actor_id"])
            assert user is not None
            assert user.is_active is False
            assert user.status == "offline"
            assert user.disabled_kind == "deleted"
            assert user.disabled_by == fixture["actor_id"]
            assert user.disabled_reason == "legacy reason retained only on User"
            assert user.deactivation_requested_at is None
            assert user.deactivation_reason is None
            assert user.deactivation_scheduled_at is None
            audit = (
                await session.execute(
                    text(
                        "SELECT actor_email, actor_role, detail_json FROM public.audit_logs "
                        "WHERE actor_id=:actor AND action='user.deactivation_approve' "
                        "ORDER BY id DESC LIMIT 1"
                    ),
                    {"actor": fixture["actor_id"]},
                )
            ).one()
            assert audit.actor_email is None and audit.actor_role is None
            assert audit.detail_json["auto"] is True
            assert audit.detail_json["email"] == user.email
            correlation = (
                await session.execute(
                    text(
                        "SELECT request_id, state, origin FROM "
                        "public.identity_deactivation_requests WHERE user_id=:user"
                    ),
                    {"user": fixture["actor_id"]},
                )
            ).one()
            assert correlation.state == "completed"
            assert correlation.origin == "legacy"
            receipt = await session.scalar(
                text(
                    "SELECT count(*) FROM public.identity_deactivation_due_receipts "
                    "WHERE operation_id=:operation"
                ),
                {"operation": operation_id},
            )
            assert receipt == 1

        async with maker.begin() as session:
            replay = await session.scalar(
                text(
                    "SELECT aap_identity_deactivation_v1.process_due_deactivations("
                    "CAST(:request AS jsonb))"
                ),
                {"request": json.dumps(request)},
            )
        assert replay == result
    finally:
        async with maker.begin() as session:
            await session.execute(
                text(
                    "DELETE FROM public.identity_deactivation_due_receipts "
                    "WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            await session.execute(
                text(
                    "DELETE FROM public.identity_deactivation_requests WHERE user_id=:id"
                ),
                {"id": fixture["actor_id"]},
            )
            await session.execute(
                text(
                    "DELETE FROM public.notifications WHERE target_type='user' "
                    "AND target_id=:id AND type='user.deactivation_completed'"
                ),
                {"id": fixture["actor_id"]},
            )
        await _cleanup(test_engine, fixture)
