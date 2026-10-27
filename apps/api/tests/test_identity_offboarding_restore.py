"""Owned restore verifies the frozen offboarding receipt and downgrade guard."""

from __future__ import annotations

import json
import os
import secrets
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_identity.authenticator import Authenticator
from aap_identity.offboarding_contract import (
    OffboardingFailure,
    decode_offboarding_request,
)
from aap_identity.offboarding_manager import OffboardingManager
from aap_identity.offboarding_postgres import OffboardingPostgresData
from aap_identity.postgres import PostgresData
from app.db.models.user import User
from tests.test_identity_admission_live import _dsn, _token, _world
from tests.test_identity_recovery import (
    _alembic,
    _assert_exact_identity_functions,
    _postgres_tool,
)
from tests.test_identity_offboarding_native import (
    _cleanup_offboarding_world,
    _offboarding_dsn,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_LIVE_ACCEPTANCE") != "1",
        reason="requires explicitly enabled owned Identity offboarding restore acceptance",
    ),
]


def _manager(identity_dsn: str, auth_dsn: str, secret: str) -> OffboardingManager:
    return OffboardingManager(
        Authenticator(PostgresData(auth_dsn), secret),
        OffboardingPostgresData(identity_dsn),
        secret,
    )


def _request(
    target_id: uuid.UUID,
    token: str,
    *,
    operation_id: uuid.UUID,
    preview_version: str,
    request_id: str,
):
    return decode_offboarding_request(
        {
            "schema": "aap-identity-offboarding/v1",
            "operation": "commit_offboarding",
            "operation_id": str(operation_id),
            "token": token,
            "payload": {
                "target_user_id": str(target_id),
                "preview_version": preview_version,
                "reason": "restore acceptance",
                "mode": "handoff",
                "projects": [],
            },
            "audit_context": {
                "method": "POST",
                "path": f"/api/v1/users/{target_id}/offboarding",
                "ip": "127.0.0.1",
                "request_id": request_id,
            },
        }
    )


async def test_restored_offboarding_receipt_replays_and_populated_downgrade_refuses(
    test_engine, tmp_path, record_property
):
    maker, actor_id = await _world(test_engine)
    secret = secrets.token_urlsafe(32)
    token = _token(actor_id, secret)
    target_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    clone = f"aap_identity_offboarding_restore_{uuid.uuid4().hex[:12]}_test"
    ownership = f"identity-offboarding-restore:{uuid.uuid4()}"
    owner_url = test_engine.url.set(database=clone)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    restored = create_async_engine(owner_url, hide_parameters=True)
    snapshot = tmp_path / "identity-offboarding-authority.dump"
    created = False
    request_id = str(uuid.uuid4())

    def auth_dsn(database: str) -> str:
        return (
            make_url(_dsn("AUTH", test_engine))
            .set(database=database)
            .render_as_string(hide_password=False)
        )

    try:
        async with maker.begin() as session:
            session.add(
                User(
                    id=target_id,
                    email=f"offboarding-restore-{target_id.hex}@example.test",
                    name="Restore offboarding target",
                    password_hash="old-offboarding-restore-hash",
                    role="employee",
                    is_active=True,
                    status="online",
                )
            )

        source_identity_dsn = _offboarding_dsn(test_engine)
        source_auth_dsn = auth_dsn(test_engine.url.database)
        source_manager = _manager(source_identity_dsn, source_auth_dsn, secret)
        preview_request = decode_offboarding_request(
            {
                "schema": "aap-identity-offboarding/v1",
                "operation": "preview_offboarding",
                "operation_id": str(uuid.uuid4()),
                "token": token,
                "payload": {"target_user_id": str(target_id)},
                "audit_context": {
                    "method": "GET",
                    "path": f"/api/v1/users/{target_id}/offboarding",
                    "ip": "127.0.0.1",
                    "request_id": request_id,
                },
            }
        )
        preview = await source_manager.execute(preview_request)
        commit = _request(
            target_id,
            token,
            operation_id=operation_id,
            preview_version=preview.data.preview.preview_version,
            request_id=request_id,
        )
        original = await source_manager.execute(commit)
        assert original.data.result.user.is_active is False
        assert original.data.result.audit_id > 0

        async with maker() as session:
            receipt_text = await session.scalar(
                text(
                    "SELECT to_jsonb(receipt)::text "
                    "FROM public.identity_offboarding_operation_receipts AS receipt "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            assert receipt_text is not None
            assert token not in receipt_text
            assert secret not in receipt_text
            receipt = json.loads(receipt_text)
            assert receipt["target_generation"] == 1
            assert receipt["target_is_active"] is False
            assert receipt["result"]["status"] == "suspended"
            assert receipt["audit_id"] == original.data.result.audit_id
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor AND target_id=:target "
                    "AND action='user.offboard' AND request_id=:request_id"
                ),
                {
                    "actor": actor_id,
                    "target": str(target_id),
                    "request_id": request_id,
                },
            )
            assert audit_count == 1

        snapshot.touch(mode=0o600)
        snapshot.write_bytes(
            _postgres_tool(
                test_engine,
                ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database],
            )
        )
        assert snapshot.read_bytes().startswith(b"PGDMP")
        async with admin.connect() as db:
            await db.execute(text(f'CREATE DATABASE "{clone}" TEMPLATE template0'))
            created = True
            await db.execute(text(f"COMMENT ON DATABASE \"{clone}\" IS '{ownership}'"))
            async with test_engine.connect() as source:
                acl = (
                    await source.execute(
                        text(
                            "SELECT COALESCE(role.rolname,'PUBLIC'), entry.privilege_type, "
                            "entry.is_grantable FROM pg_database AS database "
                            "CROSS JOIN LATERAL aclexplode(COALESCE(database.datacl, "
                            "acldefault('d',database.datdba))) AS entry "
                            "LEFT JOIN pg_roles AS role ON role.oid=entry.grantee "
                            "WHERE database.datname=current_database()"
                        )
                    )
                ).all()
            quote = db.dialect.identifier_preparer.quote_identifier
            await db.execute(text(f'REVOKE ALL ON DATABASE "{clone}" FROM PUBLIC'))
            for grantee, privilege, grantable in acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote(grantee)
                suffix = " WITH GRANT OPTION" if grantable else ""
                await db.execute(
                    text(f'GRANT {privilege} ON DATABASE "{clone}" TO {role}{suffix}')
                )
        _postgres_tool(
            test_engine,
            ["pg_restore", "-U", "owner", "--exit-on-error", "--dbname", clone],
            body=snapshot.read_bytes(),
        )

        restored_identity_dsn = _offboarding_dsn(test_engine, database=clone)
        await _assert_exact_identity_functions(
            restored_identity_dsn,
            {
                "aap_identity_offboarding_v1.apply_offboarding_command(jsonb)",
                "aap_identity_offboarding_v1.offboarding_policy()",
                "aap_identity_offboarding_v1.prepare_offboarding_command(jsonb)",
            },
        )
        restored_manager = _manager(restored_identity_dsn, auth_dsn(clone), secret)
        assert (
            await OffboardingPostgresData(restored_identity_dsn).offboarding_policy()
        ).policy == "aap-identity-offboarding/v1"
        replay = await restored_manager.execute(commit)
        assert replay.data == original.data
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND target_id=:target "
                        "AND action='user.offboard' AND request_id=:request_id"
                    ),
                    {
                        "actor": actor_id,
                        "target": str(target_id),
                        "request_id": request_id,
                    },
                )
                == audit_count
            )

        changed_secret = secrets.token_urlsafe(32)
        changed_token = _token(actor_id, changed_secret)
        with pytest.raises(OffboardingFailure) as epoch:
            await OffboardingManager(
                Authenticator(PostgresData(auth_dsn(clone)), changed_secret),
                OffboardingPostgresData(restored_identity_dsn),
                changed_secret,
            ).execute(
                _request(
                    target_id,
                    changed_token,
                    operation_id=operation_id,
                    preview_version=preview.data.preview.preview_version,
                    request_id=request_id,
                )
            )
        assert getattr(epoch.value, "code", None) == "operation_conflict"

        refused = _alembic(owner_url, "downgrade", "bo0247")
        assert refused.returncode != 0
        assert (
            b"Cannot remove Identity offboarding ownership while receipts exist"
            in refused.stdout + refused.stderr
        )
        async with restored.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM public.identity_offboarding_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
        empty = _alembic(owner_url, "downgrade", "bo0247")
        assert empty.returncode == 0, "Owned empty offboarding downgrade failed"
        async with restored.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM public.alembic_version"))
                == "bo0247"
            )
            assert await db.scalar(
                text("SELECT to_regnamespace('aap_identity_offboarding_v1') IS NULL")
            )
            assert await db.scalar(
                text(
                    "SELECT to_regclass('public.identity_offboarding_operation_receipts') IS NULL"
                )
            )
    finally:
        await restored.dispose()
        if created:
            async with admin.connect() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT shobj_description(oid,'pg_database') "
                            "FROM pg_database WHERE datname=:name"
                        ),
                        {"name": clone},
                    )
                    == ownership
                )
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=:name "
                        "AND backend_type IS DISTINCT FROM 'autovacuum worker')"
                    ),
                    {"name": clone},
                )
                await db.execute(text(f'DROP DATABASE "{clone}"'))
        await admin.dispose()
        await _cleanup_offboarding_world(
            test_engine,
            actor_ids=[actor_id],
            target_ids=[target_id],
            operation_ids=[operation_id],
        )
        snapshot.unlink(missing_ok=True)
    record_property(
        "identity_offboarding_restore",
        "same-secret-replay,no-second-Audit,exact-three-functions,epoch-refusal,populated-downgrade-refusal,empty-downgrade",
    )
