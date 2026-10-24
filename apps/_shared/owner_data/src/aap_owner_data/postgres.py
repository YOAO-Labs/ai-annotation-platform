"""Verify an owner credential has only its closed RPC capability."""

from __future__ import annotations


class FunctionRoleUnavailable(Exception):
    pass


async def verify_function_only_role(
    connection, *, schema: str, functions: set[tuple[str, str]], role: str | None = None
) -> None:
    expected = {f"{schema}.{name}({arguments})" for name, arguments in functions}
    rows = await connection.fetch("""
                SELECT namespace.nspname || '.' || function.proname || '(' ||
                       pg_catalog.oidvectortypes(function.proargtypes) || ')' AS signature
                FROM pg_catalog.pg_proc AS function
                JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace
                JOIN pg_catalog.pg_type AS return_type ON return_type.oid=function.prorettype
                WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                  AND namespace.nspname NOT LIKE 'pg_toast%'
                  AND namespace.nspname NOT LIKE 'pg_temp_%'
                  AND function.prokind IN ('f','p','a','w')
                  AND NOT (return_type.typnamespace='pg_catalog'::pg_catalog.regnamespace
                      AND return_type.typname IN ('trigger','event_trigger'))
                  AND NOT EXISTS (
                      SELECT 1 FROM pg_catalog.pg_depend AS dependency
                      WHERE dependency.classid='pg_catalog.pg_proc'::pg_catalog.regclass
                        AND dependency.objid=function.oid
                        AND dependency.refclassid='pg_catalog.pg_extension'::pg_catalog.regclass
                        AND dependency.deptype='e')
                  AND (pg_catalog.left(namespace.nspname,4)='aap_'
                    OR function.prosecdef
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.aclexplode(COALESCE(function.proacl,
                            pg_catalog.acldefault('f',function.proowner))) AS acl
                        WHERE acl.privilege_type='EXECUTE' AND acl.grantee<>0
                          AND pg_catalog.pg_has_role(current_user,acl.grantee,'USAGE')))
                  AND pg_catalog.has_function_privilege(current_user,function.oid,'EXECUTE')
                """)
    if {row["signature"] for row in rows} != expected:
        raise FunctionRoleUnavailable()
    role = role or schema.removeprefix("aap_").removesuffix("_v1") + "_client"
    denied = await connection.fetchval(
        """
                SELECT current_user <> $1
                    OR current_user <> session_user
                    OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=current_user
                        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
                    OR EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members
                        WHERE member=current_user::pg_catalog.regrole)
                    OR pg_catalog.has_database_privilege(current_user,current_database(),'CREATE,TEMP')
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
                        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                          AND namespace.nspname NOT LIKE 'pg_toast%'
                          AND namespace.nspname NOT LIKE 'pg_temp_%'
                          AND (pg_catalog.has_schema_privilege(current_user,namespace.oid,'CREATE')
                            OR (namespace.nspname NOT IN ('public',$2)
                              AND pg_catalog.has_schema_privilege(current_user,namespace.oid,'USAGE'))))
                    OR EXISTS (
                        SELECT 1 FROM pg_catalog.pg_class AS relation
                        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=relation.relnamespace
                        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
                          AND namespace.nspname NOT LIKE 'pg_toast%'
                          AND namespace.nspname NOT LIKE 'pg_temp_%'
                          AND ((relation.relkind IN ('r','p','v','m','f')
                            AND (pg_catalog.has_table_privilege(current_user,relation.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                              OR pg_catalog.has_any_column_privilege(current_user,relation.oid,'SELECT,INSERT,UPDATE,REFERENCES')))
                            OR (relation.relkind='S' AND pg_catalog.has_sequence_privilege(current_user,relation.oid,'SELECT,UPDATE,USAGE'))))
                """,
        role,
        schema,
    )
    if denied is not False:
        raise FunctionRoleUnavailable()
