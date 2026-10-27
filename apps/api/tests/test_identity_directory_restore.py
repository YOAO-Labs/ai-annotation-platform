"""Owned restore preserves accepted directory exports and exact capabilities."""

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
from aap_identity.directory_contract import DirectoryFailure, decode_directory_request
from aap_identity.directory_manager import DirectoryManager
from aap_identity.directory_postgres import DirectoryPostgresData
from aap_identity.postgres import PostgresData
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
        or os.getenv("AAP_IDENTITY_DIRECTORY_LIVE_ACCEPTANCE") != "1",
        reason="requires explicitly enabled owned directory restore acceptance",
    ),
]


async def test_restored_directory_export_replays_snapshot_and_rejects_epoch(
    test_engine, tmp_path, record_property
):
    maker, actor = await _world(test_engine)
    secret = secrets.token_urlsafe(32)
    operation_id = uuid.uuid4()
    command = decode_directory_request(
        {
            "schema": "aap-identity-directory/v1",
            "operation": "export_users",
            "operation_id": str(operation_id),
            "token": _token(actor, secret),
            "payload": {"format": "json", "status": "all"},
            "audit_context": {
                "method": "GET",
                "path": "/restore/directory",
                "ip": "127.0.0.1",
                "request_id": "directory-restore",
            },
        }
    )

    def manager(directory_dsn, auth_dsn, signing_secret=secret):
        return DirectoryManager(
            Authenticator(PostgresData(auth_dsn), signing_secret),
            DirectoryPostgresData(directory_dsn),
            signing_secret,
        )

    original = await manager(
        _dsn("DIRECTORY", test_engine), _dsn("AUTH", test_engine)
    ).execute(command)
    clone = f"aap_directory_restore_{uuid.uuid4().hex[:12]}_test"
    ownership = f"identity-directory-restore:{uuid.uuid4()}"
    owner_url = test_engine.url.set(database=clone)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    restored = create_async_engine(owner_url, hide_parameters=True)
    snapshot = tmp_path / "directory-authority.dump"
    created = False
    try:
        async with maker() as db:
            receipt = await db.scalar(
                text(
                    "SELECT result::text FROM public.identity_directory_export_receipts WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
            assert (
                receipt is not None
                and command.token not in receipt
                and secret not in receipt
            )
            frozen = json.loads(receipt)
            assert all(
                set(row)
                == {
                    "id",
                    "email",
                    "name",
                    "role",
                    "group_name",
                    "group_id",
                    "status",
                    "created_at",
                }
                for row in frozen["users"]
            )
            audit_count = await db.scalar(
                text(
                    "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor AND action='user.export'"
                ),
                {"actor": actor},
            )
            assert audit_count == 1
        snapshot.touch(mode=0o600)
        snapshot.write_bytes(
            _postgres_tool(
                test_engine, ["pg_dump", "-U", "owner", "-Fc", test_engine.url.database]
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

        def dsn(kind):
            return (
                make_url(_dsn(kind, test_engine))
                .set(database=clone)
                .render_as_string(hide_password=False)
            )

        await _assert_exact_identity_functions(
            dsn("DIRECTORY"),
            {
                "aap_identity_directory_v1.directory_policy()",
                "aap_identity_directory_v1.execute_directory_command(jsonb)",
            },
        )
        assert (
            await DirectoryPostgresData(dsn("DIRECTORY")).directory_policy()
        ).policy == "aap-identity-directory/v1"
        new_email = f"restored-directory-{uuid.uuid4().hex}@example.test"
        async with restored.begin() as db:
            await db.execute(
                text(
                    "UPDATE public.users SET email=:email,name='Changed restored profile' WHERE id=:id"
                ),
                {"email": new_email, "id": actor},
            )
        changed_context = command.model_copy(
            update={
                "audit_context": command.audit_context.model_copy(
                    update={"request_id": "after-directory-restore"}
                )
            }
        )
        replay = await manager(dsn("DIRECTORY"), dsn("AUTH")).execute(changed_context)
        assert replay.data == original.data
        assert replay.principal.email == new_email
        assert replay.data.exported_by == original.principal.email
        async with restored.connect() as db:
            assert (
                await db.scalar(
                    text(
                        "SELECT count(*) FROM public.audit_logs WHERE actor_id=:actor AND action='user.export'"
                    ),
                    {"actor": actor},
                )
                == audit_count
            )
        changed_secret = secrets.token_urlsafe(32)
        changed_request = command.model_copy(
            update={"token": _token(actor, changed_secret)}
        )
        with pytest.raises(DirectoryFailure, match="operation_conflict"):
            await manager(dsn("DIRECTORY"), dsn("AUTH"), changed_secret).execute(
                changed_request
            )
        refused = _alembic(owner_url, "downgrade", "bo0242")
        assert refused.returncode != 0
        assert (
            b"Cannot remove Identity directory ownership while export receipts exist"
            in refused.stdout + refused.stderr
        )
        async with restored.begin() as db:
            await db.execute(
                text(
                    "DELETE FROM public.identity_directory_export_receipts WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
        assert _alembic(owner_url, "downgrade", "bo0242").returncode == 0
        record_property(
            "directory_restore",
            "same-eight-field-export-and-metadata,current-principal-profile,exact-two-functions,no-repeat-Audit,epoch-refusal,populated-downgrade-refusal",
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
            await db.execute(
                text(
                    "DELETE FROM public.identity_directory_export_receipts WHERE operation_id=:id"
                ),
                {"id": operation_id},
            )
        await _cleanup(maker, actor, [])
        snapshot.unlink(missing_ok=True)
