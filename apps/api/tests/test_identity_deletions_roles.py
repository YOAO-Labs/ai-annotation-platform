from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_identity.contract import IdentityUnavailable
from aap_identity.deletion_postgres import DeletionPostgresData
from app.config import settings


def _dsn() -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity deletion LOGIN acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_DELETION_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_deletion_client"
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_identity_deletion_login_has_only_its_two_functions_and_no_raw_access():
    connection = await asyncpg.connect(_dsn())
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "identity_deletion_client"
        )
        rows = await connection.fetch(
            """
            SELECT namespace.nspname || '.' || function.proname || '(' ||
                   pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
            FROM pg_catalog.pg_proc AS function
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = function.pronamespace
            JOIN pg_catalog.pg_type AS return_type
              ON return_type.oid = function.prorettype
            WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
              AND namespace.nspname NOT LIKE 'pg_toast%'
              AND namespace.nspname NOT LIKE 'pg_temp_%'
              AND function.prokind IN ('f','p','a','w')
              AND NOT (
                  return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
                  AND return_type.typname IN ('trigger','event_trigger')
              )
              AND NOT EXISTS (
                  SELECT 1 FROM pg_catalog.pg_depend AS dependency
                  WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
                    AND dependency.objid = function.oid
                    AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
                    AND dependency.deptype = 'e'
              )
              AND (
                  pg_catalog.left(namespace.nspname,4)='aap_'
                  OR function.prosecdef
                  OR EXISTS (
                      SELECT 1 FROM pg_catalog.aclexplode(COALESCE(
                          function.proacl,
                          pg_catalog.acldefault('f', function.proowner)
                      )) AS acl
                      WHERE acl.privilege_type='EXECUTE' AND acl.grantee<>0
                        AND pg_catalog.pg_has_role(
                            current_user, acl.grantee, 'USAGE'
                        )
                  )
              )
              AND pg_catalog.has_function_privilege(
                  current_user, function.oid, 'EXECUTE'
              )
            ORDER BY namespace.nspname, function.proname,
                     pg_catalog.oidvectortypes(function.proargtypes)
            """
        )
        assert [row["signature"] for row in rows] == [
            "aap_identity_deletions_v1.deletion_policy()",
            "aap_identity_deletions_v1.execute_deletion_command(jsonb)",
        ]
        assert await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_deletions_v1', 'USAGE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_deletions_v1', 'CREATE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, 'public', 'CREATE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_database_privilege(current_user, "
            "current_database(), 'CREATE,TEMP')"
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                WHERE namespace.nspname NOT IN (
                    'pg_catalog','information_schema',
                    'aap_identity_deletions_v1','public'
                )
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND (pg_catalog.has_schema_privilege(
                           current_user, namespace.oid, 'USAGE'
                       ) OR pg_catalog.has_schema_privilege(
                           current_user, namespace.oid, 'CREATE'
                       ))
            )
            """
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_class AS relation
                  ON relation.relnamespace = namespace.oid
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND relation.relkind IN ('r','p','v','m','f')
                  AND (
                      pg_catalog.has_table_privilege(current_user, relation.oid, 'SELECT')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'INSERT')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'UPDATE')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'DELETE')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'TRUNCATE')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'REFERENCES')
                      OR pg_catalog.has_table_privilege(current_user, relation.oid, 'TRIGGER')
                      OR pg_catalog.has_any_column_privilege(
                          current_user, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
                      )
                  )
            )
            """
        )
        assert not await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_namespace AS namespace
                JOIN pg_catalog.pg_class AS sequence
                  ON sequence.relnamespace = namespace.oid
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND sequence.relkind = 'S'
                  AND (pg_catalog.has_sequence_privilege(
                           current_user, sequence.oid, 'USAGE'
                       ) OR pg_catalog.has_sequence_privilege(
                           current_user, sequence.oid, 'SELECT'
                       ) OR pg_catalog.has_sequence_privilege(
                           current_user, sequence.oid, 'UPDATE'
                       ))
            )
            """
        )

        policy = await connection.fetchval(
            "SELECT aap_identity_deletions_v1.deletion_policy()"
        )
        assert json.loads(policy) == {
            "schema": "aap-identity-deletions-policy/v1",
            "policy": "aap-identity-deletions/v1",
        }
        for value in (None, "null", "[]", "1", '"scalar"', "{}"):
            result = await connection.fetchval(
                "SELECT aap_identity_deletions_v1.execute_deletion_command($1::jsonb)",
                value,
            )
            assert json.loads(result) == {
                "schema": "aap-identity-deletions-native/v1",
                "ok": False,
                "error": {
                    "code": "invalid_request",
                    "status_code": 400,
                    "detail": None,
                },
            }

        forbidden = (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.tasks LIMIT 1",
            "SELECT * FROM public.task_locks LIMIT 1",
            "SELECT * FROM public.identity_deletion_operation_receipts LIMIT 1",
            "SELECT aap_workspace_v1.lock_identity_deletion_scope(NULL::uuid,NULL::uuid)",
            "SELECT aap_labeling_v1.identity_deletion_work(NULL::uuid)",
            "SELECT aap_audit_v1.record_identity_deletion_event('{}'::jsonb,NULL,'{}'::jsonb,'{}'::jsonb)",
            "CREATE TEMP TABLE deletion_forbidden(id integer)",
            "SET ROLE identity_code",
        )
        for statement in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_deletion_policy_rejects_an_extra_security_definer_function():
    dsn = _dsn()
    function_name = f"identity_deletion_probe_{uuid.uuid4().hex}"
    owner_engine = create_async_engine(settings.effective_migration_database_url)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE FUNCTION public.{function_name}() RETURNS integer "
                    "LANGUAGE sql IMMUTABLE SECURITY DEFINER AS $$ SELECT 1 $$"
                )
            )
            await connection.execute(
                text(f"REVOKE ALL ON FUNCTION public.{function_name}() FROM PUBLIC")
            )
            await connection.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION public.{function_name}() "
                    "TO identity_deletion_client"
                )
            )

        with pytest.raises(IdentityUnavailable):
            await DeletionPostgresData(dsn).deletion_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await owner_engine.dispose()


@pytest.mark.asyncio
async def test_public_invoker_cannot_write_deletion_receipts():
    function_name = f"deletion_insert_probe_{uuid.uuid4().hex}"
    owner_engine = create_async_engine(settings.effective_migration_database_url)
    connection = None
    try:
        async with owner_engine.begin() as owner:
            await owner.execute(
                text(
                    f"CREATE FUNCTION public.{function_name}() RETURNS integer "
                    "LANGUAGE plpgsql AS $$ BEGIN "
                    "INSERT INTO public.identity_deletion_operation_receipts ("
                    "operation_id,actor_id,operation,target_user_id,"
                    "request_fingerprint,derivation_id,target_generation,"
                    "target_is_active,result,audit_id) VALUES ("
                    "pg_catalog.gen_random_uuid(),pg_catalog.gen_random_uuid(),"
                    "'delete_account',pg_catalog.gen_random_uuid(),"
                    "pg_catalog.repeat('0',64),pg_catalog.repeat('0',64),"
                    "0,true,'{}'::jsonb,NULL); "
                    "RAISE EXCEPTION 'probe insert unexpectedly succeeded'; "
                    "END $$"
                )
            )
            await owner.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION public.{function_name}() "
                    "TO identity_deletion_client"
                )
            )

        connection = await asyncpg.connect(_dsn())
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_table_privilege(current_user, "
            "'public.identity_deletion_operation_receipts', 'INSERT')"
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await connection.fetchval(f"SELECT public.{function_name}()")
    finally:
        if connection is not None:
            await connection.close()
        async with owner_engine.begin() as owner:
            await owner.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await owner_engine.dispose()
