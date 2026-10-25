"""Replacing the Project query body must retain deployed OID and privilege chains."""

import importlib.util
import os
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]

_STEP = (
    "aap_workspace_project_query_v1.project_query_step("
    "jsonb,text,jsonb,text,text,text[],text[],uuid,text,"
    "timestamptz,timestamptz,bigint,integer)"
)


async def test_access_upgrade_and_downgrade_preserve_oid_owner_and_grant_chain(
    test_engine,
):
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/0264_workspace_access_queries.py"
    )
    spec = importlib.util.spec_from_file_location("access_query_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def migrate(connection, operation):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, operation)()

    async def snapshot(connection):
        return (
            await connection.execute(
                text(
                    "SELECT oid,proowner,proacl::text,provolatile,proparallel,prosecdef,proconfig::text "
                    "FROM pg_proc WHERE oid=to_regprocedure(:signature)"
                ),
                {"signature": _STEP},
            )
        ).one()

    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION {_STEP} TO workspace_project_query_client WITH GRANT OPTION"
                )
            )
            await connection.execute(
                text("SET LOCAL ROLE workspace_project_query_client")
            )
            await connection.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION {_STEP} TO workspace_template_query_client"
                )
            )
            await connection.execute(text("RESET ROLE"))
            original = await snapshot(connection)
            for _ in range(2):
                await connection.run_sync(migrate, "downgrade")
                assert await snapshot(connection) == original
                await connection.run_sync(migrate, "upgrade")
                assert await snapshot(connection) == original
            assert await connection.scalar(
                text(
                    "SELECT NOT EXISTS (SELECT 1 FROM pg_proc AS function, "
                    "LATERAL aclexplode(function.proacl) AS acl "
                    "WHERE function.oid=to_regprocedure(:signature) AND acl.grantee<>function.proowner)"
                ),
                {
                    "signature": _STEP.replace(
                        ".project_query_step(", "._project_query_step_impl("
                    )
                },
            )
        finally:
            await transaction.rollback()
