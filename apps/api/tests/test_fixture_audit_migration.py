"""Exercise the Audit receipt cutover from the actual historical revision."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_recovery import _alembic


_API = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned disposable module-data verifier",
    ),
]


def _upgrade(url, revision):
    config = Config(str(_API / "alembic.ini"))
    config.set_main_option("script_location", str(_API / "alembic"))
    config.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    command.upgrade(config, revision)


@pytest.mark.parametrize(
    "populated", [True, False], ids=["legacy-audit", "empty-audit"]
)
async def test_audit_receipt_forward_migration(test_engine, populated):
    _verify_postgres(test_engine.url)
    assert test_engine.url.host == "127.0.0.1"
    assert test_engine.url.username == "owner"
    assert test_engine.url.database.endswith("_test")
    async with test_engine.connect() as source:
        user, database = (
            await source.execute(text("SELECT current_user, current_database()"))
        ).one()
        assert user == "owner" and database == test_engine.url.database
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

    clone = f"aap_fixture_audit_migration_{uuid4().hex[:12]}_test"
    ownership = f"fixture-audit-migration:{uuid4()}"
    owner_url = test_engine.url.set(database=clone)
    admin = create_async_engine(
        test_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    historical = create_async_engine(owner_url)
    created = False
    try:
        async with admin.connect() as db:
            await db.execute(text(f'CREATE DATABASE "{clone}" TEMPLATE template0'))
            created = True
            await db.execute(text(f"COMMENT ON DATABASE \"{clone}\" IS '{ownership}'"))
            quote_identifier = db.dialect.identifier_preparer.quote_identifier
            await db.execute(text(f'REVOKE ALL ON DATABASE "{clone}" FROM PUBLIC'))
            for grantee, privilege, grantable in acl:
                assert privilege in {"CREATE", "CONNECT", "TEMPORARY"}
                role = "PUBLIC" if grantee == "PUBLIC" else quote_identifier(grantee)
                suffix = " WITH GRANT OPTION" if grantable else ""
                await db.execute(
                    text(f'GRANT {privilege} ON DATABASE "{clone}" TO {role}{suffix}')
                )

        prepared = await asyncio.to_thread(_alembic, owner_url, "upgrade", "bo0436")
        assert prepared.returncode == 0, "Historical migration setup failed"
        run_id, audit_id, user_id = uuid4(), uuid4(), uuid4()
        async with historical.begin() as db:
            assert (
                await db.scalar(text("SELECT version_num FROM alembic_version"))
                == "bo0436"
            )
            await db.execute(
                text(
                    "INSERT INTO yoao_fixture_v1.fixture_runs (id,family) VALUES (:id,'owned')"
                ),
                {"id": run_id},
            )
            await db.execute(
                text(
                    "INSERT INTO yoao_fixture_v1.fixture_run_resources "
                    "(run_id,kind,resource_id,logical_key,state) "
                    "VALUES (:run_id,'user',:resource_id,'other','published')"
                ),
                {"run_id": run_id, "resource_id": user_id},
            )
            if populated:
                await db.execute(
                    text(
                        "INSERT INTO yoao_fixture_v1.fixture_run_resources "
                        "(run_id,kind,resource_id,logical_key,state) "
                        "VALUES (:run_id,'audit',:resource_id,'legacy','published')"
                    ),
                    {"run_id": run_id, "resource_id": audit_id},
                )
            rows_before = (
                await db.scalars(
                    text(
                        "SELECT to_jsonb(resource) FROM "
                        "yoao_fixture_v1.fixture_run_resources AS resource ORDER BY kind"
                    )
                )
            ).all()
            kind_constraint = await db.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid='yoao_fixture_v1.fixture_run_resources'::regclass "
                    "AND conname='ck_fixture_run_resource_kind'"
                )
            )
            assert kind_constraint is not None
            assert (
                await db.scalar(
                    text("SELECT to_regclass('yoao_fixture_v1.fixture_audit_events')")
                )
                is None
            )

        if populated:
            with pytest.raises(DBAPIError) as failure:
                await asyncio.to_thread(_upgrade, owner_url, "bo0438")
            assert failure.value.orig.sqlstate == "55000"
        else:
            await asyncio.to_thread(_upgrade, owner_url, "bo0438")

        async with historical.begin() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == (
                "bo0436" if populated else "bo0438"
            )
            rows_after = (
                await db.scalars(
                    text(
                        "SELECT to_jsonb(resource) FROM "
                        "yoao_fixture_v1.fixture_run_resources AS resource ORDER BY kind"
                    )
                )
            ).all()
            assert rows_after == rows_before
            native = await db.scalar(
                text("SELECT to_regclass('yoao_fixture_v1.fixture_audit_events')")
            )
            if populated:
                assert native is None
                assert (
                    await db.scalar(
                        text(
                            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                            "WHERE conrelid='yoao_fixture_v1.fixture_run_resources'::regclass "
                            "AND conname='ck_fixture_run_resource_kind'"
                        )
                    )
                    == kind_constraint
                )
            else:
                assert native is not None
                pk_columns = await db.scalar(
                    text(
                        "SELECT array_agg(attribute.attname ORDER BY key.ordinality) "
                        "FROM pg_constraint AS constraint_row "
                        "CROSS JOIN LATERAL unnest(constraint_row.conkey) "
                        "WITH ORDINALITY AS key(attnum, ordinality) "
                        "JOIN pg_attribute AS attribute ON attribute.attrelid=constraint_row.conrelid "
                        "AND attribute.attnum=key.attnum "
                        "WHERE constraint_row.conrelid="
                        "'yoao_fixture_v1.fixture_audit_events'::regclass "
                        "AND constraint_row.contype='p'"
                    )
                )
                assert pk_columns == ["audit_id", "audit_created_at"]
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM yoao_fixture_v1.fixture_audit_events"
                        )
                    )
                    == 0
                )
                with pytest.raises(IntegrityError):
                    async with db.begin_nested():
                        await db.execute(
                            text(
                                "INSERT INTO yoao_fixture_v1.fixture_run_resources "
                                "(run_id,kind,resource_id,logical_key) "
                                "VALUES (:run_id,'audit',:resource_id,'new')"
                            ),
                            {"run_id": run_id, "resource_id": audit_id},
                        )
    finally:
        await historical.dispose()
        try:
            if created:
                _verify_postgres(test_engine.url)
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
                    await db.execute(text(f'DROP DATABASE "{clone}"'))
        finally:
            await admin.dispose()
