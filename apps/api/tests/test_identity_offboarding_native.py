from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import aap_identity
from aap_identity.authenticator import Authenticator
from aap_identity.contract import IdentityUnavailable
from aap_identity.offboarding_contract import (
    OffboardingApplyPayload,
    OffboardingFailure,
    OffboardingScopeBasis,
    decode_offboarding_request,
    decode_native_offboarding_reply,
    native_apply_offboarding_command,
    native_prepare_offboarding_command,
)
from aap_identity.offboarding_manager import (
    OffboardingManager,
    offboarding_derivation_id,
    offboarding_request_fingerprint,
)
from aap_identity.offboarding_postgres import OffboardingPostgresData
from aap_identity.postgres import PostgresData
from app.db.models.api_key import ApiKey
from app.db.models.password_reset_token import PasswordResetToken
from app.db.native_schema import NATIVE_MANAGED_TABLES
from tests.test_identity_suspensions_native import (
    _add_batch,
    _add_member,
    _add_project,
    _add_task_and_lock,
    _add_user,
    _auth_dsn,
    _cleanup_world,
    _token,
)

_ROOT = Path(__file__).parents[3]
_SQL_DIR = Path(__file__).parents[1] / "alembic/sql"
_SCHEMA = _SQL_DIR / "0248_identity_offboarding.schema.json"
_SUPPORT = _SQL_DIR / "0248_identity_offboarding_support.sql"
_ARTIFACT = _SQL_DIR / "0248_identity_offboarding.sql"
_MIGRATION = Path(__file__).parents[1] / "alembic/versions/0248_identity_offboarding.py"
_MODULE_GRANTS = _ROOT / "infra/docker/module-data-grants.sql"
_ROLE_GRANTS = _ROOT / "infra/docker/identity-offboarding-grants.sql"
_LAN_GRANTS = _ROOT / "infra/docker/lan-production-grants.sql"
_CANONICAL_SCHEMA = (
    _ROOT / "apps/identity/src/aap_identity/native_offboarding_protocol.schema.json"
)
if not _CANONICAL_SCHEMA.is_file():
    _CANONICAL_SCHEMA = (
        Path(aap_identity.__file__).resolve().parent
        / "native_offboarding_protocol.schema.json"
    )


def _offboarding_dsn(test_engine=None, database: str | None = None) -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity offboarding acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_OFFBOARDING_URL")
    if not value:
        pytest.skip("Identity offboarding client DSN is not configured")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_offboarding_client"
    assert os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    if database is not None:
        parsed = parsed.set(database=database)
    elif test_engine is not None:
        assert (parsed.host, parsed.port, parsed.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def _manager(test_engine, secret: str, *, database: str | None = None):
    return OffboardingManager(
        Authenticator(PostgresData(_auth_dsn(test_engine)), secret),
        OffboardingPostgresData(_offboarding_dsn(test_engine, database)),
        secret,
    )


def _request(
    target_id: uuid.UUID,
    token: str,
    *,
    operation: str,
    operation_id: uuid.UUID | None = None,
    preview_version: str = "",
    reason: str = "",
    mode: str = "handoff",
    projects: list[dict] | None = None,
):
    payload = {"target_user_id": str(target_id)}
    if operation == "commit_offboarding":
        payload.update(
            {
                "preview_version": preview_version,
                "reason": reason,
                "mode": mode,
                "projects": projects or [],
            }
        )
    return decode_offboarding_request(
        {
            "schema": "aap-identity-offboarding/v1",
            "operation": operation,
            "operation_id": str(operation_id or uuid.uuid4()),
            "token": token,
            "audit_context": {
                "method": "GET" if operation == "preview_offboarding" else "POST",
                "path": f"/api/v1/users/{target_id}/offboarding",
                "ip": "127.0.0.1",
                "request_id": str(uuid.uuid4()),
            },
            "payload": payload,
        }
    )


async def _cleanup_offboarding_world(
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
                "DELETE FROM public.identity_offboarding_operation_receipts "
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


def test_frozen_offboarding_artifacts_and_native_owner_boundary():
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

    assert "identity_offboarding_operation_receipts" in NATIVE_MANAGED_TABLES
    assert schema == canonical
    assert _SCHEMA.read_bytes() == _CANONICAL_SCHEMA.read_bytes()
    assert schema["$id"] == "aap-identity-offboarding-native/v1"
    assert migration.index('"0248_identity_offboarding_support.sql"') < migration.index(
        '"0248_identity_offboarding.sql"'
    )
    assert "FOREIGN KEY" not in support
    assert "request_fingerprint text NOT NULL" in support
    assert "derivation_id text NOT NULL" in support
    assert "target_generation bigint NOT NULL" in support
    assert "target_is_active boolean NOT NULL" in support
    assert "result jsonb NOT NULL" in support
    assert "scope_basis jsonb NOT NULL" in support
    assert "identity_directory_project_refs" in support
    compact_migration = "".join(migration.split())
    for signature in (
        "lock_identity_offboarding_scope(uuid,uuid[],uuid[])",
        "identity_offboarding_replay_scope(uuid,uuid[])",
        "lock_identity_offboarding_work(uuid,uuid[])",
        "transfer_identity_offboarding_owners(uuid,jsonb)",
        "transfer_identity_offboarding_work(uuid,jsonb)",
        "record_identity_offboarding_event(jsonb,uuid,jsonb,jsonb)",
    ):
        assert signature in compact_migration
    assert (
        "aap_identity_offboarding_v1.prepare_offboarding_command(jsonb)" in role_grants
    )
    assert "aap_identity_offboarding_v1.apply_offboarding_command(jsonb)" in role_grants
    assert "identity_offboarding_client" in role_grants
    assert "identity_offboarding_operation_receipts" in module_grants
    assert 'GRANT SELECT ON public.projects TO :"identity_owner"' not in module_grants
    assert (
        "aap_identity_offboarding_v1.prepare_offboarding_command(jsonb)" in lan_grants
    )
    assert '"OFFBOARDING": "identity_offboarding_client"' in verifier
    assert "raw preview version" in support
    assert "status = 'offline'" in artifact
    assert "retire_account_credentials(v_target_user_id)" in artifact
    assert "release_identity_offboarding_task_locks" in artifact
    assert "v_audit_id IS NULL OR v_audit_id <= 0" in artifact
    compact_artifact = "".join(artifact.split())
    receipt_insert = compact_artifact.index(
        "INSERTINTOpublic.identity_offboarding_operation_receipts"
    )
    clear_binding = compact_artifact.index(
        "PERFORMpg_catalog.set_config('aap.identity_offboarding_prepare','',true);"
    )
    assert receipt_insert < clear_binding


class _CorruptingOffboardingTransaction:
    def __init__(self, inner):
        self.inner = inner

    async def prepare_offboarding_command(self, payload):
        return await self.inner.prepare_offboarding_command(payload)

    async def apply_offboarding_command(self, payload):
        reply = await self.inner.apply_offboarding_command(payload)
        reply["operation_id"] = str(uuid.uuid4())
        return reply


class _CorruptingOffboardingData:
    def __init__(self, dsn: str):
        self.inner = OffboardingPostgresData(dsn)

    @asynccontextmanager
    async def transaction(self):
        async with self.inner.transaction() as transaction:
            yield _CorruptingOffboardingTransaction(transaction)


@pytest.mark.parametrize(
    ("reason", "expected_reason"),
    [
        pytest.param("  Offboarding accepted  ", "Offboarding accepted", id="reason"),
        pytest.param("", None, id="empty-reason"),
        pytest.param(" \t ", None, id="whitespace-reason"),
    ],
)
@pytest.mark.asyncio
async def test_handoff_updates_owner_and_labeling_and_replays_without_duplicate_audit(
    test_engine, reason, expected_reason
):
    actor_id, _ = await _add_user(test_engine, "super_admin")
    target_id, target_email = await _add_user(test_engine, "employee")
    owner_receiver_id, _ = await _add_user(test_engine, "project_admin")
    annotator_receiver_id, _ = await _add_user(test_engine, "employee")
    project_id = await _add_project(test_engine, target_id, "Offboarding project")
    await _add_member(test_engine, project_id, target_id, actor_id)
    await _add_member(test_engine, project_id, annotator_receiver_id, actor_id)
    batch_id = await _add_batch(test_engine, project_id, target_id, actor_id)
    task_id, lock_id = await _add_task_and_lock(test_engine, project_id, target_id)

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "identity-offboarding-native-test-secret"
    token = _token(actor_id, "super_admin", secret)
    manager = _manager(test_engine, secret)
    operation_id = uuid.uuid4()
    key_id = uuid.uuid4()
    reset_id = uuid.uuid4()
    requested_at = datetime.now(UTC) - timedelta(days=1)
    scheduled_at = datetime.now(UTC) + timedelta(days=6)
    task_assigned_at_before = None
    task_updated_at_before = None

    async with maker.begin() as session:
        session.add(
            ApiKey(
                id=key_id,
                user_id=target_id,
                name="Offboarding target key",
                key_prefix=f"ak_{key_id.hex[:9]}",
                key_hash=f"offboarding-key-{key_id.hex}",
                scopes=["annotations:read"],
            )
        )
        await session.flush()
        session.add(
            PasswordResetToken(
                id=reset_id,
                user_id=target_id,
                token_hash=hashlib.sha256(reset_id.bytes).hexdigest(),
                issued_email=target_email,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        await session.flush()
        await session.execute(
            text(
                "UPDATE public.users SET deactivation_requested_at=:requested, "
                "deactivation_scheduled_at=:scheduled, "
                "deactivation_reason='accepted self deactivation' WHERE id=:target"
            ),
            {"requested": requested_at, "scheduled": scheduled_at, "target": target_id},
        )
        task_times = (
            await session.execute(
                text("SELECT assigned_at, updated_at FROM public.tasks WHERE id=:id"),
                {"id": task_id},
            )
        ).one()
        task_assigned_at_before, task_updated_at_before = task_times

    preview = await manager.execute(
        _request(target_id, token, operation="preview_offboarding")
    )
    assert preview.data.preview.can_commit is True
    commit = _request(
        target_id,
        token,
        operation="commit_offboarding",
        operation_id=operation_id,
        preview_version=preview.data.preview.preview_version,
        reason=reason,
        projects=[
            {
                "project_id": str(project_id),
                "owner_receiver_id": str(owner_receiver_id),
                "annotator_receiver_id": str(annotator_receiver_id),
                "reviewer_receiver_id": None,
            }
        ],
    )

    try:
        result = await manager.execute(commit)
        assert result.data.result.user.is_active is False
        assert result.data.result.user.disabled_kind == "suspended"
        assert result.data.result.user.disabled_reason == expected_reason
        assert result.data.result.user.deactivation_requested_at == requested_at
        assert result.data.result.user.deactivation_scheduled_at == scheduled_at
        assert result.data.result.audit_id > 0
        assert result.data.result.revoked_api_key_ids == [key_id]
        assert len(result.data.result.transfers) == 2

        async with maker() as session:
            project = (
                await session.execute(
                    text("SELECT owner_id FROM public.projects WHERE id=:id"),
                    {"id": project_id},
                )
            ).one()
            batch = (
                await session.execute(
                    text(
                        "SELECT annotator_id, assigned_user_ids::text AS assigned_user_ids, updated_at "
                        "FROM public.task_batches WHERE id=:id"
                    ),
                    {"id": batch_id},
                )
            ).one()
            task = (
                await session.execute(
                    text(
                        "SELECT assignee_id, assigned_at, updated_at "
                        "FROM public.tasks WHERE id=:id"
                    ),
                    {"id": task_id},
                )
            ).one()
            stored_reason = await session.scalar(
                text("SELECT disabled_reason FROM public.users WHERE id=:id"),
                {"id": target_id},
            )
            assert project.owner_id == owner_receiver_id
            assert batch.annotator_id == annotator_receiver_id
            assert json.loads(batch.assigned_user_ids) == [str(annotator_receiver_id)]
            assert task.assignee_id == annotator_receiver_id
            assert stored_reason == expected_reason
            assert task.assigned_at is not None
            assert task.updated_at is not None
            assert (
                task_assigned_at_before is None
            ) or task.assigned_at != task_assigned_at_before
            assert (
                task_updated_at_before is None
            ) or task.updated_at != task_updated_at_before
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM public.task_locks WHERE id=:id"),
                    {"id": lock_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT used_at IS NOT NULL FROM public.password_reset_tokens "
                        "WHERE id=:id"
                    ),
                    {"id": reset_id},
                )
                is True
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE id=:id "
                        "AND action='user.offboard' AND status_code=200"
                    ),
                    {"id": result.data.result.audit_id},
                )
                == 1
            )
            audit_detail = await session.scalar(
                text("SELECT detail_json::text FROM public.audit_logs WHERE id=:id"),
                {"id": result.data.result.audit_id},
            )
            audit_detail = json.loads(audit_detail)
            assert audit_detail["mode"] == "handoff"
            assert audit_detail["reason_present"] is (expected_reason is not None)
            assert expected_reason is None or expected_reason not in json.dumps(
                audit_detail
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT revoked_at IS NOT NULL FROM public.api_keys WHERE id=:id"
                    ),
                    {"id": key_id},
                )
                is True
            )
            stored_receipt = await session.scalar(
                text(
                    "SELECT to_jsonb(receipt)::text "
                    "FROM public.identity_offboarding_operation_receipts AS receipt "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            assert stored_receipt is not None
            assert token not in stored_receipt
            assert secret not in stored_receipt
            stored_receipt_record = json.loads(stored_receipt)
            if expected_reason is None:
                assert (
                    stored_receipt_record["result"]["user"]["disabled_reason"] is None
                )
            else:
                assert expected_reason in stored_receipt

        replay = await manager.execute(commit)
        assert replay.data == result.data
        async with maker() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE id=:id "
                        "AND action='user.offboard'"
                    ),
                    {"id": result.data.result.audit_id},
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_offboarding_operation_receipts WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 1
            )
    finally:
        await _cleanup_offboarding_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id, owner_receiver_id, annotator_receiver_id],
            operation_ids=[operation_id],
            project_ids=[project_id],
            batch_ids=[batch_id],
            task_ids=[task_id],
        )


@pytest.mark.asyncio
async def test_raw_nul_preview_version_is_echoed_without_entering_native_jsonb(
    test_engine,
):
    actor_id, _ = await _add_user(test_engine, "super_admin")
    target_id, _ = await _add_user(test_engine, "employee")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "identity-offboarding-nul-preview-test-secret"
    token = _token(actor_id, "super_admin", secret)
    manager = _manager(test_engine, secret)
    operation_id = uuid.uuid4()
    expected = "preview\u0000version"
    request = _request(
        target_id,
        token,
        operation="commit_offboarding",
        operation_id=operation_id,
        preview_version=expected,
        projects=[],
    )
    try:
        with pytest.raises(OffboardingFailure) as stale:
            await manager.execute(request)
        assert stale.value.code == "offboarding_preview_stale"
        assert stale.value.status_code == 409
        assert stale.value.detail["expected_preview_version"] == expected
        async with maker() as session:
            user = (
                await session.execute(
                    text(
                        "SELECT is_active, credential_generation FROM public.users "
                        "WHERE id=:id"
                    ),
                    {"id": target_id},
                )
            ).one()
            assert user.is_active is True
            assert user.credential_generation == 0
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_offboarding_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE target_id=:id AND action='user.offboard'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        await _cleanup_offboarding_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
        )


@pytest.mark.asyncio
async def test_corrupt_success_reply_rolls_back_native_offboarding_effects(test_engine):
    actor_id, _ = await _add_user(test_engine, "super_admin")
    target_id, _ = await _add_user(test_engine, "employee")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    secret = "identity-offboarding-corrupt-reply-test-secret"
    token = _token(actor_id, "super_admin", secret)
    operation_id = uuid.uuid4()
    manager = _manager(test_engine, secret)
    preview = await manager.execute(
        _request(target_id, token, operation="preview_offboarding")
    )
    commit = _request(
        target_id,
        token,
        operation="commit_offboarding",
        operation_id=operation_id,
        preview_version=preview.data.preview.preview_version,
        projects=[],
    )
    corrupting = OffboardingManager(
        Authenticator(PostgresData(_auth_dsn(test_engine)), secret),
        _CorruptingOffboardingData(_offboarding_dsn(test_engine)),
        secret,
    )
    try:
        with pytest.raises(IdentityUnavailable):
            await corrupting.execute(commit)
        async with maker() as session:
            user = (
                await session.execute(
                    text(
                        "SELECT is_active, credential_generation "
                        "FROM public.users WHERE id=:id"
                    ),
                    {"id": target_id},
                )
            ).one()
            assert user.is_active is True
            assert user.credential_generation == 0
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_offboarding_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE target_id=:id AND action='user.offboard'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        await _cleanup_offboarding_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
        )


@pytest.mark.asyncio
async def test_reserved_active_super_admin_does_not_allow_stopping_last_unreserved_one(
    test_engine,
):
    actor_id, _ = await _add_user(test_engine, "super_admin")
    target_id, _ = await _add_user(test_engine, "super_admin")
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    reserved_requested = datetime.now(UTC) - timedelta(minutes=1)
    reserved_scheduled = datetime.now(UTC) + timedelta(days=6)
    async with maker.begin() as session:
        await session.execute(
            text(
                "UPDATE public.users SET deactivation_requested_at=:requested, "
                "deactivation_scheduled_at=:scheduled WHERE id=:actor"
            ),
            {
                "requested": reserved_requested,
                "scheduled": reserved_scheduled,
                "actor": actor_id,
            },
        )

    secret = "identity-offboarding-last-admin-test-secret"
    token = _token(actor_id, "super_admin", secret)
    operation_id = uuid.uuid4()
    manager = _manager(test_engine, secret)
    request = _request(
        target_id,
        token,
        operation="commit_offboarding",
        operation_id=operation_id,
        preview_version="0" * 64,
        projects=[],
    )
    try:
        with pytest.raises(OffboardingFailure) as last_admin:
            await manager.execute(request)
        assert last_admin.value.code == "last_super_admin"
        assert last_admin.value.status_code == 400
        async with maker() as session:
            status = (
                await session.execute(
                    text(
                        "SELECT is_active, credential_generation "
                        "FROM public.users WHERE id=:id"
                    ),
                    {"id": target_id},
                )
            ).one()
            assert status.is_active is True
            assert status.credential_generation == 0
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_offboarding_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE target_id=:id AND action='user.offboard'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        await _cleanup_offboarding_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
        )


@pytest.mark.asyncio
async def test_native_apply_rejects_inactive_null_disabled_kind_without_effects(
    test_engine,
):
    actor_id, _ = await _add_user(test_engine, "super_admin")
    target_id, _ = await _add_user(test_engine, "employee", active=False)
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker.begin() as session:
        await session.execute(
            text(
                "UPDATE public.users SET disabled_kind=NULL, status='offline' "
                "WHERE id=:target"
            ),
            {"target": target_id},
        )

    secret = "identity-offboarding-null-kind-test-secret"
    token = _token(actor_id, "super_admin", secret)
    operation_id = uuid.uuid4()
    request = _request(
        target_id,
        token,
        operation="commit_offboarding",
        operation_id=operation_id,
        preview_version="0" * 64,
        projects=[],
    )
    authenticator = Authenticator(PostgresData(_auth_dsn(test_engine)), secret)
    verified = await authenticator.authenticate_bound(token)
    fingerprint = offboarding_request_fingerprint(secret, request)
    derivation_id = offboarding_derivation_id(secret)
    prepare_command = native_prepare_offboarding_command(
        request,
        actor=verified.principal,
        expected_key_hash=verified.key_hash,
        request_fingerprint=fingerprint,
        derivation_id=derivation_id,
    )
    dsn = _offboarding_dsn(test_engine)
    try:
        async with OffboardingPostgresData(dsn).transaction() as transaction:
            prepared_raw = await transaction.prepare_offboarding_command(
                prepare_command.model_dump(mode="json", by_alias=True)
            )
            prepared = decode_native_offboarding_reply(prepare_command, prepared_raw)
            apply_payload = OffboardingApplyPayload(
                transaction_id=prepared.data.transaction_id,
                target_user_id=target_id,
                expected_target_generation=prepared.data.target_generation,
                expected_target_is_active=prepared.data.target_is_active,
                mode="handoff",
                normalized_reason=None,
                reason_present=False,
                owner_plan=[],
                labeling_plan=[],
                transfers=[],
                unresolved=[],
                scope_basis=OffboardingScopeBasis(
                    target_role="employee",
                    target_was_active=False,
                    project_ids=[],
                ),
            )
            apply_command = native_apply_offboarding_command(
                request,
                actor=prepared.principal,
                expected_key_hash=verified.key_hash,
                request_fingerprint=fingerprint,
                derivation_id=derivation_id,
                payload=apply_payload,
            )
            with pytest.raises(OffboardingFailure) as rejected:
                await transaction.apply_offboarding_command(
                    apply_command.model_dump(mode="json", by_alias=True)
                )
            assert rejected.value.code == "state_forbidden"
            assert rejected.value.status_code == 409

        async with maker() as session:
            user = (
                await session.execute(
                    text(
                        "SELECT is_active, credential_generation, disabled_kind "
                        "FROM public.users WHERE id=:id"
                    ),
                    {"id": target_id},
                )
            ).one()
            assert user.is_active is False
            assert user.credential_generation == 0
            assert user.disabled_kind is None
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.identity_offboarding_operation_receipts "
                        "WHERE operation_id=:id"
                    ),
                    {"id": operation_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE target_id=:id AND action='user.offboard'"
                    ),
                    {"id": str(target_id)},
                )
                == 0
            )
    finally:
        await _cleanup_offboarding_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
        )
