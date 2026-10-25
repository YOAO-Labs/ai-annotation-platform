"""Owned restore preserves administrator reset replay and capability boundaries."""

from __future__ import annotations

import json
import os
import secrets
import uuid

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_identity.admin_password_contract import (
    AdminPasswordFailure,
    decode_admin_password_request,
)
from aap_identity.admin_password_manager import AdminPasswordManager
from aap_identity.admin_password_postgres import AdminPasswordPostgresData
from aap_identity.authenticator import Authenticator
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
        reason="requires explicitly enabled owned administrator-password restore acceptance",
    ),
]


async def test_restored_admin_password_receipt_replays_without_second_audit(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret = secrets.token_urlsafe(32)
    credential = _token(actor, secret)
    target = uuid.uuid4()
    operation_id = uuid.uuid4()
    request_id = str(uuid.uuid4())
    clone = f"aap_identity_admin_password_restore_{uuid.uuid4().hex[:12]}_test"
    ownership = f"identity-admin-password-restore:{uuid.uuid4()}"
    owner_url = test_engine.url.set(database=clone)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    restored = create_async_engine(owner_url, hide_parameters=True)
    snapshot = tmp_path / "admin-password-authority.dump"
    created = False

    def request(token: str):
        return decode_admin_password_request(
            {
                "schema": "aap-identity-admin-passwords/v1",
                "operation": "admin_reset_password",
                "operation_id": str(operation_id),
                "token": token,
                "payload": {"target_user_id": str(target)},
                "audit_context": {
                    "method": "POST",
                    "path": f"/api/v1/users/{target}/admin-reset-password",
                    "ip": "127.0.0.1",
                    "request_id": request_id,
                },
            }
        )

    def manager(identity_dsn: str, auth_dsn: str, signing_secret: str):
        return AdminPasswordManager(
            Authenticator(PostgresData(auth_dsn), signing_secret),
            AdminPasswordPostgresData(identity_dsn),
            signing_secret,
        )

    command = request(credential)
    source_manager = manager(
        _dsn("ADMIN_PASSWORD", test_engine), _dsn("AUTH", test_engine), secret
    )
    try:
        async with maker.begin() as db:
            db.add(
                User(
                    id=target,
                    email=f"restore-admin-target-{target.hex}@example.test",
                    name="Restore target",
                    password_hash="old-restore-target-hash",
                    role="viewer",
                    is_active=True,
                )
            )
        original = await source_manager.execute(command)

        async with maker() as db:
            receipt = await db.scalar(
                text(
                    "SELECT to_jsonb(receipt)::text "
                    "FROM public.identity_admin_password_reset_receipts AS receipt "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            assert receipt is not None
            assert command.token not in receipt
            assert secret not in receipt
            assert original.data.temp_password not in receipt
            stored = json.loads(receipt)
            assert "password_hash" not in stored
            assert "password_state_fingerprint" in stored
            audit_count = await db.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor "
                    "AND action='user.password_admin_reset' AND request_id=:request_id"
                ),
                {"actor": actor, "request_id": request_id},
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
            dsn("ADMIN_PASSWORD"),
            {
                "aap_identity_admin_passwords_v1.admin_password_policy()",
                "aap_identity_admin_passwords_v1.execute_admin_password_command(jsonb)",
            },
        )
        restored_manager = manager(dsn("ADMIN_PASSWORD"), dsn("AUTH"), secret)
        assert (
            await AdminPasswordPostgresData(
                dsn("ADMIN_PASSWORD")
            ).admin_password_policy()
        ).policy == "aap-identity-admin-passwords/v1"
        replay = await restored_manager.execute(command)
        assert replay.data == original.data
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor "
                        "AND action='user.password_admin_reset' AND request_id=:request_id"
                    ),
                    {"actor": actor, "request_id": request_id},
                )
                == audit_count
            )

        changed_secret = secrets.token_urlsafe(32)
        with pytest.raises(AdminPasswordFailure, match="operation_conflict"):
            await manager(dsn("ADMIN_PASSWORD"), dsn("AUTH"), changed_secret).execute(
                request(_token(actor, changed_secret))
            )

        refused = _alembic(owner_url, "downgrade", "0243")
        assert refused.returncode != 0
        assert (
            b"Cannot remove Identity administrator password ownership while receipts exist"
            in refused.stdout + refused.stderr
        )
        async with restored.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM public.alembic_version"))
                == "0276"
            )
            await db.execute(
                text(
                    "DELETE FROM public.identity_admin_password_reset_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
        empty = _alembic(owner_url, "downgrade", "0243")
        assert empty.returncode == 0, "Owned empty admin-password downgrade failed"
        async with restored.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM public.alembic_version"))
                == "0243"
            )
        record_property(
            "admin_password_restore",
            "same-secret-replay,no-second-Audit,exact-two-functions,epoch-refusal,populated-downgrade-refusal,empty-downgrade",
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
                    "AND request_id=:request_id"
                ),
                {"actor": actor, "request_id": request_id},
            )
            await db.execute(
                text(
                    "DELETE FROM public.identity_admin_password_reset_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            await db.execute(delete(User).where(User.id == target))
        await _cleanup(maker, actor, [])
        snapshot.unlink(missing_ok=True)
