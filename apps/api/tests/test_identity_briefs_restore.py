"""Read-only Identity briefs capability survives an owned database restore."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aap_identity.briefs_contract import decode_briefs_request
from aap_identity.briefs_manager import BriefsManager
from aap_identity.briefs_postgres import BriefsPostgresData
from app.db.models.user import User
from tests.test_identity_recovery import (
    _postgres_tool,
    _assert_exact_identity_functions,
)


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.getenv("AAP_IDENTITY_BRIEFS_LIVE_ACCEPTANCE") != "1",
        reason="requires explicitly enabled owned Identity briefs restore acceptance",
    ),
]


def _briefs_dsn(test_engine, database: str | None = None) -> str:
    value = os.environ["AAP_MODULE_DATA_IDENTITY_BRIEFS_URL"]
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_briefs_client"
    assert os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    if database is not None:
        parsed = parsed.set(database=database)
    else:
        assert (parsed.host, parsed.port, parsed.database) == (
            test_engine.url.host,
            test_engine.url.port,
            test_engine.url.database,
        )
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


async def test_restored_database_retains_briefs_policy_and_reads(test_engine, tmp_path):
    source_dsn = _briefs_dsn(test_engine)
    user_id = uuid.uuid4()
    email = f"briefs-restore-{user_id.hex}@example.test"
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as session:
        session.add(
            User(
                id=user_id,
                email=email,
                name="ßeta",
                password_hash="test-only-briefs-restore-hash",
                role="reviewer",
                is_active=False,
                status="offline",
                disabled_kind="suspended",
                avatar_ref="upload:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )
        )

    request = decode_briefs_request(
        {
            "schema": "aap-identity-briefs/v1",
            "operation": "get_user_briefs",
            "payload": {"user_ids": [str(user_id)]},
        }
    )
    original_data = BriefsPostgresData(source_dsn)
    original_policy = await original_data.briefs_policy()
    original = await BriefsManager(original_data).execute(request)

    clone = f"aap_identity_briefs_restore_{uuid.uuid4().hex[:12]}_test"
    ownership = f"identity-briefs-restore:{uuid.uuid4()}"
    admin = create_async_engine(
        test_engine.url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
    )
    snapshot = tmp_path / "identity-briefs.dump"
    created = False
    try:
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

        client_dsn = _briefs_dsn(test_engine, clone)
        await _assert_exact_identity_functions(
            client_dsn,
            {
                "aap_identity_briefs_v1.briefs_policy()",
                "aap_identity_briefs_v1.execute_briefs_command(jsonb)",
            },
        )
        restored_data = BriefsPostgresData(client_dsn)
        assert await restored_data.briefs_policy() == original_policy
        restored = await BriefsManager(restored_data).execute(request)
        assert restored.data == original.data
        assert restored.data.users[0].avatar_initial == "SS"
    finally:
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
        async with maker.begin() as session:
            await session.execute(
                text("DELETE FROM public.users WHERE id=:id"),
                {"id": user_id},
            )
        snapshot.unlink(missing_ok=True)
