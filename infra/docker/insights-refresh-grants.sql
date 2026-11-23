\set ON_ERROR_STOP on
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.refresh_role', :'refresh_role', true);

DO $guard$
DECLARE
    refresh_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('yoao.refresh_role'));
    capability oid := to_regprocedure('yoao_insights_v1.refresh_user_perf_daily()');
    table_permissions text := 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER';
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF NOT EXISTS (
           SELECT FROM pg_roles
           WHERE oid = refresh_oid AND rolcanlogin AND NOT rolinherit
             AND rolname NOT IN (current_user, session_user)
             AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
       ) OR EXISTS (
           SELECT FROM pg_auth_members
           WHERE refresh_oid IN (member, roleid)
       ) THEN
        RAISE EXCEPTION 'Insights refresh requires a distinct restricted LOGIN with no memberships';
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_proc AS routine
        JOIN pg_class AS mv ON mv.oid = to_regclass('public.mv_user_perf_daily')
        JOIN pg_roles AS owner ON owner.oid = routine.proowner
        WHERE routine.oid = capability AND routine.proowner = mv.relowner
          AND (owner.rolname = current_user OR NOT owner.rolcanlogin)
          AND mv.relkind = 'm' AND routine.proowner <> refresh_oid AND routine.prosecdef
          AND routine.prokind = 'f' AND routine.prorettype = 'void'::regtype
          AND routine.provolatile = 'v' AND routine.proparallel = 'u'
          AND routine.proconfig = ARRAY['search_path=pg_catalog, pg_temp']::text[]
          AND has_database_privilege(routine.proowner, current_database(), 'TEMP')
    ) THEN
        RAISE EXCEPTION 'Insights refresh function is missing or does not match its materialized-view owner';
    END IF;
    IF has_database_privilege(refresh_oid, current_database(), 'CREATE,TEMP')
       OR EXISTS (SELECT FROM pg_database WHERE datdba = refresh_oid)
       OR EXISTS (
           SELECT FROM pg_namespace AS namespace
           WHERE namespace.nspowner = refresh_oid
              OR has_schema_privilege(refresh_oid, namespace.oid, 'CREATE')
              OR (namespace.nspname NOT IN ('public', 'yoao_insights_v1', 'pg_catalog', 'information_schema')
                  AND left(namespace.nspname, 8) NOT IN ('pg_toast', 'pg_temp_')
                  AND has_schema_privilege(refresh_oid, namespace.oid, 'USAGE'))
       ) THEN
        RAISE EXCEPTION 'Insights refresh LOGIN has database creation or foreign schema privileges';
    END IF;
    IF current_setting('server_version_num')::integer >= 170000 THEN
        table_permissions := table_permissions || ',MAINTAIN';
    END IF;
    IF EXISTS (
        SELECT FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND left(namespace.nspname, 8) NOT IN ('pg_toast', 'pg_temp_')
          AND CASE
              WHEN relation.relkind = 'S' THEN relation.relowner = refresh_oid
                  OR has_sequence_privilege(refresh_oid, relation.oid, 'SELECT,UPDATE,USAGE')
              WHEN relation.relkind IN ('r','p','v','m','f') THEN
                  relation.relowner = refresh_oid
                  OR has_table_privilege(refresh_oid, relation.oid, table_permissions)
                  OR has_any_column_privilege(refresh_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES')
              ELSE false
          END
    ) THEN
        RAISE EXCEPTION 'Insights refresh LOGIN has direct data privileges';
    END IF;
    IF EXISTS (
        SELECT FROM pg_proc AS routine
        JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
        WHERE routine.proowner = refresh_oid
           OR (routine.oid <> capability
               AND namespace.nspname NOT IN ('pg_catalog', 'information_schema')
               AND left(namespace.nspname, 8) NOT IN ('pg_toast', 'pg_temp_')
               AND NOT EXISTS (
                   SELECT FROM pg_depend AS dependency
                   WHERE dependency.classid = 'pg_proc'::regclass AND dependency.objid = routine.oid
                     AND dependency.refclassid = 'pg_extension'::regclass AND dependency.deptype = 'e'
               )
               AND (left(namespace.nspname, 5) = 'yoao_' OR left(namespace.nspname, 4) = 'aap_'
                    OR routine.prosecdef OR EXISTS (
                        SELECT FROM aclexplode(routine.proacl) AS acl
                        WHERE acl.grantee = refresh_oid AND acl.privilege_type = 'EXECUTE'
                    ))
               AND has_function_privilege(refresh_oid, routine.oid, 'EXECUTE'))
    ) THEN
        RAISE EXCEPTION 'Insights refresh LOGIN has another application routine';
    END IF;
END;
$guard$;

REVOKE ALL ON FUNCTION yoao_insights_v1.refresh_user_perf_daily() FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"refresh_role";
GRANT USAGE ON SCHEMA yoao_insights_v1 TO :"refresh_role";
GRANT EXECUTE ON FUNCTION yoao_insights_v1.refresh_user_perf_daily() TO :"refresh_role";

DO $verify$
DECLARE
    refresh_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('yoao.refresh_role'));
    capability oid := 'yoao_insights_v1.refresh_user_perf_daily()'::regprocedure;
BEGIN
    IF NOT has_database_privilege(refresh_oid, current_database(), 'CONNECT')
       OR has_database_privilege(refresh_oid, current_database(), 'CONNECT WITH GRANT OPTION')
       OR NOT has_schema_privilege(refresh_oid, 'yoao_insights_v1', 'USAGE')
       OR has_schema_privilege(refresh_oid, 'yoao_insights_v1', 'USAGE WITH GRANT OPTION')
       OR NOT has_function_privilege(refresh_oid, capability, 'EXECUTE')
       OR has_function_privilege(refresh_oid, capability, 'EXECUTE WITH GRANT OPTION')
       OR EXISTS (
           SELECT FROM pg_proc AS routine
           CROSS JOIN LATERAL aclexplode(COALESCE(routine.proacl, acldefault('f', routine.proowner))) AS acl
           WHERE routine.oid = capability AND acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
       ) THEN
        RAISE EXCEPTION 'Insights refresh LOGIN is not limited to its exact capability';
    END IF;
END;
$verify$;
COMMIT;
