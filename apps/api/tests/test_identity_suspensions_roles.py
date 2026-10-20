from __future__ import annotations

import json
import os
import uuid

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from aap_identity.admin_password_postgres import AdminPasswordPostgresData
from aap_identity.contract import IdentityUnavailable
from aap_identity.directory_postgres import DirectoryPostgresData
from aap_identity.platform_role_postgres import PlatformRolePostgresData
from aap_identity.suspension_postgres import SuspensionPostgresData
from app.config import settings


def _dsn() -> str:
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity suspension LOGIN acceptance requires the owned verifier")
    value = os.environ.get("AAP_MODULE_DATA_IDENTITY_SUSPENSION_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == "identity_suspension_client"
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_identity_suspension_login_has_only_its_two_functions():
    connection = await asyncpg.connect(_dsn())
    try:
        assert await connection.fetchval("SELECT current_user") == (
            "identity_suspension_client"
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
            """
        )
        assert {row["signature"] for row in rows} == {
            "aap_identity_suspensions_v1.suspension_policy()",
            "aap_identity_suspensions_v1.execute_suspension_command(jsonb)",
        }
        assert await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_suspensions_v1', 'USAGE')"
        )
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_schema_privilege(current_user, "
            "'aap_identity_suspensions_v1', 'CREATE')"
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
                    'aap_identity_suspensions_v1','public'
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
            "SELECT aap_identity_suspensions_v1.suspension_policy()"
        )
        assert json.loads(policy) == {
            "schema": "aap-identity-suspensions-policy/v1",
            "policy": "aap-identity-suspensions/v1",
        }
        malformed = await connection.fetchval(
            "SELECT aap_identity_suspensions_v1.execute_suspension_command('{}'::jsonb)"
        )
        assert json.loads(malformed) == {
            "schema": "aap-identity-suspensions-native/v1",
            "ok": False,
            "error": {
                "code": "invalid_request",
                "status_code": 400,
                "detail": None,
            },
        }
        for value in (None, "null", "[]", "1", '"scalar"'):
            invalid = await connection.fetchval(
                "SELECT aap_identity_suspensions_v1.execute_suspension_command($1::jsonb)",
                value,
            )
            assert json.loads(invalid) == {
                "schema": "aap-identity-suspensions-native/v1",
                "ok": False,
                "error": {
                    "code": "invalid_request",
                    "status_code": 400,
                    "detail": None,
                },
            }

        forbidden = (
            "SELECT * FROM public.users LIMIT 1",
            "SELECT * FROM public.identity_suspension_operation_receipts LIMIT 1",
            "SELECT aap_workspace_v1.lock_identity_suspension_scope(NULL::uuid,NULL::uuid,'suspend_account')",
            "SELECT aap_audit_v1.record_identity_suspension_event('user.reactivate','{}'::jsonb,NULL,'{}'::jsonb,'{}'::jsonb)",
            "CREATE TEMP TABLE suspension_forbidden(id integer)",
            "SET ROLE identity_code",
        )
        for statement in forbidden:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(statement)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_suspension_policy_rejects_an_extra_security_definer_function():
    dsn = _dsn()
    function_name = f"identity_suspension_probe_{uuid.uuid4().hex}"
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
                    "TO identity_suspension_client"
                )
            )

        with pytest.raises(IdentityUnavailable):
            await SuspensionPostgresData(dsn).suspension_policy()
    finally:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await owner_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment_key", "role_name", "data_type", "policy_method"),
    [
        (
            "AAP_MODULE_DATA_IDENTITY_SUSPENSION_URL",
            "identity_suspension_client",
            SuspensionPostgresData,
            "suspension_policy",
        ),
        (
            "AAP_MODULE_DATA_IDENTITY_DIRECTORY_URL",
            "identity_directory_client",
            DirectoryPostgresData,
            "directory_policy",
        ),
        (
            "AAP_MODULE_DATA_IDENTITY_ADMIN_PASSWORD_URL",
            "identity_admin_password_client",
            AdminPasswordPostgresData,
            "admin_password_policy",
        ),
        (
            "AAP_MODULE_DATA_IDENTITY_PLATFORM_ROLE_URL",
            "identity_platform_role_client",
            PlatformRolePostgresData,
            "platform_role_policy",
        ),
    ],
    ids=("suspension", "directory", "admin-password", "platform-role"),
)
async def test_suspension_policy_rejects_database_temp_and_recovers_after_revoke(
    environment_key: str,
    role_name: str,
    data_type: type,
    policy_method: str,
):
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("Identity LOGIN acceptance requires the owned verifier")
    value = os.environ.get(environment_key)
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    parsed = make_url(value)
    assert parsed.database and parsed.database.endswith(("_test", "_e2e"))
    assert parsed.username == role_name
    dsn = parsed.set(drivername="postgresql").render_as_string(hide_password=False)
    database = make_url(dsn).database
    assert database
    owner_engine = create_async_engine(settings.effective_migration_database_url)
    temp_granted = False
    rejected = False
    try:
        baseline = await asyncpg.connect(dsn)
        try:
            assert not await baseline.fetchval(
                "SELECT pg_catalog.has_database_privilege("
                "current_user,current_database(),'TEMP')"
            )
        finally:
            await baseline.close()
        await getattr(data_type(dsn), policy_method)()

        async with owner_engine.begin() as owner:
            assert await owner.scalar(text("SELECT current_database()")) == database
            await owner.execute(
                text(
                    "SELECT pg_catalog.set_config("
                    "'aap.temp_probe_role', :role_name, true)"
                ),
                {"role_name": role_name},
            )
            await owner.exec_driver_sql(
                "DO $test$ BEGIN EXECUTE pg_catalog.format("
                "'GRANT TEMPORARY ON DATABASE %I TO %I', "
                "pg_catalog.current_database(), "
                "pg_catalog.current_setting('aap.temp_probe_role')); END $test$"
            )
        temp_granted = True

        connection = await asyncpg.connect(dsn)
        try:
            assert await connection.fetchval(
                "SELECT pg_catalog.has_database_privilege("
                "current_user,current_database(),'TEMP')"
            )
        finally:
            await connection.close()

        try:
            await getattr(data_type(dsn), policy_method)()
        except IdentityUnavailable:
            rejected = True
    finally:
        try:
            if temp_granted:
                async with owner_engine.begin() as owner:
                    await owner.execute(
                        text(
                            "SELECT pg_catalog.set_config("
                            "'aap.temp_probe_role', :role_name, true)"
                        ),
                        {"role_name": role_name},
                    )
                    await owner.exec_driver_sql(
                        "DO $test$ BEGIN EXECUTE pg_catalog.format("
                        "'REVOKE TEMPORARY ON DATABASE %I FROM "
                        "%I', pg_catalog.current_database(), "
                        "pg_catalog.current_setting('aap.temp_probe_role')); "
                        "END $test$"
                    )
        finally:
            await owner_engine.dispose()

    restored = False
    try:
        await getattr(data_type(dsn), policy_method)()
        restored = True
    except IdentityUnavailable:
        pass
    assert rejected, "suspension readiness accepted database TEMP privilege"
    assert restored, "suspension readiness remained unavailable after TEMP revoke"


@pytest.mark.asyncio
async def test_public_invoker_helper_cannot_write_suspension_receipts():
    function_name = f"suspension_insert_probe_{uuid.uuid4().hex}"
    owner_engine = create_async_engine(settings.effective_migration_database_url)
    connection = None
    try:
        async with owner_engine.begin() as owner:
            await owner.execute(
                text(
                    f"CREATE FUNCTION public.{function_name}() RETURNS integer "
                    "LANGUAGE plpgsql AS $$ BEGIN "
                    "INSERT INTO public.identity_suspension_operation_receipts ("
                    "operation_id,actor_id,operation,target_user_id,"
                    "request_fingerprint,derivation_id,target_generation,"
                    "target_is_active,result,audit_id) VALUES ("
                    "pg_catalog.gen_random_uuid(),pg_catalog.gen_random_uuid(),"
                    "'suspend_account',pg_catalog.gen_random_uuid(),"
                    "pg_catalog.repeat('0',64),pg_catalog.repeat('0',64),"
                    "0,false,'{}'::jsonb,NULL); "
                    "RAISE EXCEPTION 'probe insert unexpectedly succeeded'; "
                    "END $$"
                )
            )
            await owner.execute(
                text(
                    f"GRANT EXECUTE ON FUNCTION public.{function_name}() "
                    "TO identity_suspension_client"
                )
            )
        connection = await asyncpg.connect(_dsn())
        assert not await connection.fetchval(
            "SELECT pg_catalog.has_table_privilege(current_user, "
            "'public.identity_suspension_operation_receipts', 'INSERT')"
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
