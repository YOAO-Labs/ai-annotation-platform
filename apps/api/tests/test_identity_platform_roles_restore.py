"""Owned restore preserves frozen platform-role receipts and capabilities."""

from __future__ import annotations

import json
import os
import secrets
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_identity.authenticator import Authenticator
from aap_identity.platform_role_contract import (
    PlatformRoleFailure,
    decode_platform_role_request,
)
from aap_identity.platform_role_manager import PlatformRoleManager
from aap_identity.platform_role_postgres import PlatformRolePostgresData
from aap_identity.postgres import PostgresData
from app.db.models.user import User
from tests.test_identity_admission_live import _cleanup, _dsn, _token, _world
from tests.test_identity_recovery import (
    _alembic,
    _assert_exact_identity_functions,
    _postgres_tool,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_LIVE_ACCEPTANCE") != "1",
        reason="requires explicitly enabled owned platform-role restore acceptance",
    ),
]


async def test_restored_platform_role_receipt_replays_frozen_result(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret = secrets.token_urlsafe(32)
    credential = _token(actor, secret)
    target = uuid.uuid4()
    operation_id = uuid.uuid4()
    request_id = str(uuid.uuid4())
    clone = f"aap_identity_platform_role_restore_{uuid.uuid4().hex[:12]}_test"
    ownership = f"identity-platform-role-restore:{uuid.uuid4()}"
    owner_url = test_engine.url.set(database=clone)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    restored = create_async_engine(owner_url, hide_parameters=True)
    snapshot = tmp_path / "platform-role-authority.dump"
    created = False

    request = decode_platform_role_request(
        {
            "schema": "aap-identity-platform-roles/v1",
            "operation": "change_platform_role",
            "operation_id": str(operation_id),
            "token": credential,
            "payload": {"target_user_id": str(target), "role": "viewer"},
            "audit_context": {
                "method": "PATCH",
                "path": f"/api/v1/users/{target}/role",
                "ip": "127.0.0.1",
                "request_id": request_id,
            },
        }
    )

    def manager(role_dsn: str, auth_dsn: str, signing_secret: str):
        return PlatformRoleManager(
            Authenticator(PostgresData(auth_dsn), signing_secret),
            PlatformRolePostgresData(role_dsn),
            signing_secret,
        )

    source_manager = manager(
        _dsn("PLATFORM_ROLE", test_engine), _dsn("AUTH", test_engine), secret
    )
    try:
        async with maker.begin() as db:
            db.add(
                User(
                    id=target,
                    email=f"restore-platform-role-{target.hex}@example.test",
                    name="Restore role target",
                    password_hash="fixture-only",
                    role="employee",
                    is_active=True,
                )
            )
        original = await source_manager.execute(request)
        assert original.data.user.role == "viewer"

        async with maker() as db:
            receipt = await db.scalar(
                text(
                    "SELECT to_jsonb(receipt)::text "
                    "FROM public.identity_platform_role_operation_receipts AS receipt "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            assert receipt is not None
            assert request.token not in receipt
            assert secret not in receipt
            stored = json.loads(receipt)
            assert "user" in stored["result"]
            assert "password_hash" not in receipt
            audit_count = await db.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor "
                    "AND action='user.role_change' AND target_id=:target_id"
                ),
                {"actor": actor, "target_id": str(target)},
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
                            "SELECT COALESCE(role.rolname,'PUBLIC'), entry.privilege_type, entry.is_grantable "
                            "FROM pg_database AS database CROSS JOIN LATERAL aclexplode(COALESCE(database.datacl,acldefault('d',database.datdba))) AS entry "
                            "LEFT JOIN pg_roles AS role ON role.oid=entry.grantee WHERE database.datname=current_database()"
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

        def dsn(kind: str) -> str:
            return (
                make_url(_dsn(kind, test_engine))
                .set(database=clone)
                .render_as_string(hide_password=False)
            )

        await _assert_exact_identity_functions(
            dsn("PLATFORM_ROLE"),
            {
                "aap_identity_platform_roles_v1.platform_role_policy()",
                "aap_identity_platform_roles_v1.execute_platform_role_command(jsonb)",
            },
        )
        restored_manager = manager(dsn("PLATFORM_ROLE"), dsn("AUTH"), secret)
        assert (
            await PlatformRolePostgresData(dsn("PLATFORM_ROLE")).platform_role_policy()
        ).policy == "aap-identity-platform-roles/v1"
        async with restored.begin() as db:
            await db.execute(
                text("UPDATE public.users SET role='employee' WHERE id=:id"),
                {"id": target},
            )
        replay = await restored_manager.execute(request)
        assert replay.data.user.role == "viewer"
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text("SELECT role FROM public.users WHERE id=:id"),
                    {"id": target},
                )
                == "employee"
            )
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor "
                        "AND action='user.role_change' AND target_id=:target_id"
                    ),
                    {"actor": actor, "target_id": str(target)},
                )
                == audit_count
            )

        changed_secret = secrets.token_urlsafe(32)
        changed_request = request.model_copy(
            update={"token": _token(actor, changed_secret)}
        )
        with pytest.raises(PlatformRoleFailure, match="operation_conflict"):
            await manager(dsn("PLATFORM_ROLE"), dsn("AUTH"), changed_secret).execute(
                changed_request
            )

        refused = _alembic(owner_url, "downgrade", "bo0244")
        assert refused.returncode != 0
        assert (
            b"Cannot remove Identity platform-role ownership while receipts exist"
            in refused.stdout + refused.stderr
        )
        async with restored.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM public.identity_platform_role_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
        assert _alembic(owner_url, "downgrade", "bo0244").returncode == 0
        async with restored.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM public.alembic_version"))
                == "bo0244"
            )
        record_property(
            "platform_role_restore",
            "same-secret-frozen-replay,no-repeat-Audit,exact-two-functions,epoch-refusal,populated-downgrade-refusal,empty-downgrade",
        )
    finally:
        await restored.dispose()
        if created:
            async with admin.connect() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT shobj_description(oid,'pg_database') FROM pg_database WHERE datname=:name"
                        ),
                        {"name": clone},
                    )
                    == ownership
                )
                assert not await db.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=:name AND backend_type IS DISTINCT FROM 'autovacuum worker')"
                    ),
                    {"name": clone},
                )
                await db.execute(text(f'DROP DATABASE "{clone}"'))
        await admin.dispose()
        async with maker.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update='true'"))
            await db.execute(
                text(
                    "DELETE FROM public.audit_logs WHERE actor_id=:actor "
                    "AND target_id=:target_id"
                ),
                {"actor": actor, "target_id": str(target)},
            )
            await db.execute(
                text(
                    "DELETE FROM public.identity_platform_role_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            await db.execute(delete(User).where(User.id == target))
        await _cleanup(maker, actor, [])
        snapshot.unlink(missing_ok=True)
