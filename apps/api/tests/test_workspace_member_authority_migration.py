"""Exercise the additive member timestamp fix with already-installed role ACLs."""

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

_ARGUMENTS = (
    "jsonb,text,jsonb,text,text,text[],text[],uuid,text,"
    "timestamptz,timestamptz,bigint,integer"
)
_STEP = f"aap_workspace_project_query_v1.project_query_step({_ARGUMENTS})"
_IMPL = f"aap_workspace_project_query_v1._project_query_step_impl({_ARGUMENTS})"


async def test_member_temporal_migration_preserves_client_acl_and_hides_impl(
    test_engine,
):
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/0262_workspace_member_authority_temporals.py"
    )
    spec = importlib.util.spec_from_file_location("member_temporal_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(connection, operation):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, operation)()

    async def state(connection):
        return (
            await connection.execute(
                text(
                    "SELECT pg_get_userbyid(proowner) AS owner, "
                    "has_function_privilege('workspace_project_query_client',oid,'EXECUTE') AS client, "
                    "has_function_privilege('legacy_runtime',oid,'EXECUTE') AS runtime, "
                    "has_function_privilege('workspace_project_query_client',oid,'EXECUTE WITH GRANT OPTION') AS grantable "
                    "FROM pg_proc WHERE oid=to_regprocedure(:signature)"
                ),
                {"signature": _STEP},
            )
        ).one()

    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            # The downgrade is intentionally run after ownership and client grants,
            # exercising the deployed path that a fresh-schema check cannot cover.
            await connection.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION {_STEP} "
                    "TO workspace_project_query_client WITH GRANT OPTION"
                )
            )
            original = await state(connection)
            assert tuple(original) == ("workspace_code", True, True, True)
            for _ in range(2):
                await connection.run_sync(run, "downgrade")
                assert await state(connection) == original
                assert await connection.scalar(
                    text("SELECT to_regprocedure(:signature) IS NULL"),
                    {"signature": _IMPL},
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
                await connection.run_sync(run, "upgrade")
                assert await state(connection) == original
                assert await connection.scalar(
                    text(
                        "SELECT has_function_privilege('workspace_template_query_client', "
                        "to_regprocedure(:signature), 'EXECUTE')"
                    ),
                    {"signature": _STEP},
                )
                assert await connection.scalar(
                    text(
                        "SELECT pg_get_userbyid(proowner)='workspace_code' "
                        "AND NOT has_function_privilege('workspace_project_query_client',oid,'EXECUTE') "
                        "AND NOT has_function_privilege('legacy_runtime',oid,'EXECUTE') "
                        "AND NOT EXISTS (SELECT 1 FROM aclexplode(proacl) AS acl "
                        "WHERE acl.grantee<>proowner AND acl.privilege_type='EXECUTE') "
                        "FROM pg_proc WHERE oid=to_regprocedure(:signature)"
                    ),
                    {"signature": _IMPL},
                )
        finally:
            await transaction.rollback()
