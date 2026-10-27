"""Owned restore preserves administrative deletion receipts and authority."""

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
from aap_identity.deletion_contract import (
    DeletionFailure,
    decode_deletion_request,
)
from aap_identity.deletion_manager import DeletionManager
from aap_identity.deletion_postgres import DeletionPostgresData
from aap_identity.postgres import PostgresData
from app.db.models.user import User
from tests.test_identity_admission_live import _dsn, _token, _world
from tests.test_identity_deletions_native import _cleanup_deletion_world
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
        reason="requires explicitly enabled owned Identity deletion restore acceptance",
    ),
]


def _deletion_dsn(test_engine, database: str | None = None) -> str:
    raw = os.environ["AAP_MODULE_DATA_IDENTITY_DELETION_URL"]
    value = make_url(raw)
    if database is None:
        assert (value.host, value.port, value.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
    else:
        value = value.set(database=database)
    assert value.database and value.database.endswith(("_test", "_e2e"))
    assert value.username == "identity_deletion_client"
    return value.set(drivername="postgresql").render_as_string(hide_password=False)


async def test_restored_deletion_receipt_replays_and_populated_downgrade_refuses(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret = secrets.token_urlsafe(32)
    credential = _token(actor, secret)
    target = uuid.uuid4()
    operation_id = uuid.uuid4()
    request_id = str(uuid.uuid4())
    clone = f"aap_identity_deletion_restore_{uuid.uuid4().hex[:12]}_test"
    ownership = f"identity-deletion-restore:{uuid.uuid4()}"
    owner_url = test_engine.url.set(database=clone)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    restored = create_async_engine(owner_url, hide_parameters=True)
    snapshot = tmp_path / "identity-deletion-authority.dump"
    created = False

    def request(token: str):
        return decode_deletion_request(
            {
                "schema": "aap-identity-deletions/v1",
                "operation": "delete_account",
                "operation_id": str(operation_id),
                "token": token,
                "payload": {
                    "target_user_id": str(target),
                    "transfer_to_user_id": None,
                },
                "audit_context": {
                    "method": "DELETE",
                    "path": f"/api/v1/users/{target}",
                    "ip": "127.0.0.1",
                    "request_id": request_id,
                },
            }
        )

    def manager(identity_dsn: str, auth_dsn: str, signing_secret: str):
        return DeletionManager(
            Authenticator(PostgresData(auth_dsn), signing_secret),
            DeletionPostgresData(identity_dsn),
            signing_secret,
        )

    command = request(credential)
    source_manager = manager(
        _deletion_dsn(test_engine), _dsn("AUTH", test_engine), secret
    )
    try:
        async with maker.begin() as db:
            db.add(
                User(
                    id=target,
                    email=f"deletion-restore-{target.hex}@example.test",
                    name="Restore deletion target",
                    password_hash="old-deletion-restore-hash",
                    role="employee",
                    is_active=True,
                )
            )

        original = await source_manager.execute(command)
        assert original.data.user.id == target
        assert original.data.user.is_active is False
        assert original.data.user.disabled_kind == "deleted"

        async with maker() as db:
            receipt_text = await db.scalar(
                text(
                    "SELECT to_jsonb(receipt)::text "
                    "FROM public.identity_deletion_operation_receipts AS receipt "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
            assert receipt_text is not None
            assert credential not in receipt_text
            assert secret not in receipt_text
            stored = json.loads(receipt_text)
            assert stored["target_generation"] == 0
            assert stored["target_is_active"] is False
            assert stored["result"]["user"]["is_active"] is False
            assert stored["audit_id"] is not None
            audit_count = await db.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs "
                    "WHERE actor_id=:actor AND target_id=:target "
                    "AND action='user.delete' AND request_id=:request_id"
                ),
                {
                    "actor": actor,
                    "target": str(target),
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

        def auth_dsn(database: str) -> str:
            return (
                make_url(_dsn("AUTH", test_engine))
                .set(database=database)
                .render_as_string(hide_password=False)
            )

        restored_identity_dsn = _deletion_dsn(test_engine, clone)
        await _assert_exact_identity_functions(
            restored_identity_dsn,
            {
                "aap_identity_deletions_v1.deletion_policy()",
                "aap_identity_deletions_v1.execute_deletion_command(jsonb)",
            },
        )
        restored_manager = manager(restored_identity_dsn, auth_dsn(clone), secret)
        assert (
            await DeletionPostgresData(restored_identity_dsn).deletion_policy()
        ).policy == "aap-identity-deletions/v1"
        replay = await restored_manager.execute(command)
        assert replay.data == original.data
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs "
                        "WHERE actor_id=:actor AND target_id=:target "
                        "AND action='user.delete' AND request_id=:request_id"
                    ),
                    {
                        "actor": actor,
                        "target": str(target),
                        "request_id": request_id,
                    },
                )
                == audit_count
            )

        changed_secret = secrets.token_urlsafe(32)
        with pytest.raises(DeletionFailure) as epoch:
            await manager(
                restored_identity_dsn, auth_dsn(clone), changed_secret
            ).execute(request(_token(actor, changed_secret)))
        assert epoch.value.code == "operation_conflict"
        assert epoch.value.status_code == 409

        refused = _alembic(owner_url, "downgrade", "bo0246")
        assert refused.returncode != 0
        assert (
            b"Cannot remove Identity deletion ownership while receipts exist"
            in refused.stdout + refused.stderr
        )
        async with restored.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM public.identity_deletion_operation_receipts "
                    "WHERE operation_id=:operation_id"
                ),
                {"operation_id": operation_id},
            )
        empty = _alembic(owner_url, "downgrade", "bo0246")
        assert empty.returncode == 0, "Owned empty deletion downgrade failed"
        async with restored.connect() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM public.alembic_version"))
                == "bo0246"
            )
            assert await db.scalar(
                text("SELECT to_regnamespace('aap_identity_deletions_v1') IS NULL")
            )
            assert await db.scalar(
                text(
                    "SELECT to_regclass('public.identity_deletion_operation_receipts') IS NULL"
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
        await _cleanup_deletion_world(
            test_engine,
            actor_ids=[actor],
            target_ids=[target],
            operation_ids=[operation_id],
        )
        snapshot.unlink(missing_ok=True)
    record_property(
        "identity_deletion_restore",
        "same-secret-replay,no-second-Audit,exact-two-functions,epoch-refusal,populated-downgrade-refusal,empty-downgrade",
    )
