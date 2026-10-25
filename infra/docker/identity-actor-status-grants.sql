-- Grant the standalone Identity active-status read to a dedicated LOGIN.
-- Run after migrations and module-data-grants as the migration role.
-- The status LOGIN receives no table, column, or second function capability.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v identity_owner=identity_code \
--   -v status_role=identity_actor_status_client \
--   -f infra/docker/identity-actor-status-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.status_role', :'status_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('aap.identity_owner'));
    v_status oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('aap.status_role'));
    v_function oid := pg_catalog.to_regprocedure(
        'aap_identity_v1.read_actor_facts(uuid)'
    );
BEGIN
    IF current_database()<>current_setting('aap.expected_database')
       OR current_user<>current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF v_owner IS NULL OR v_status IS NULL
       OR v_owner=v_status
       OR current_user::pg_catalog.regrole=v_status THEN
        RAISE EXCEPTION 'Identity actor-status roles are missing or overlap';
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles AS role
        WHERE role.oid=v_owner AND NOT role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles AS role
        WHERE role.oid=v_status AND role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member IN (v_owner,v_status)
           OR membership.roleid=v_status
           OR (membership.roleid=v_owner AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Unsafe Identity actor-status role profile';
    END IF;
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='s'
          AND function.prokind='f'
          AND function.proretset
          AND function.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
    ) THEN
        RAISE EXCEPTION 'Identity active-status source function is missing or changed';
    END IF;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"status_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"status_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role name := current_setting('aap.status_role');
BEGIN
    FOR v_schema IN
        SELECT namespace.nspname
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            v_schema.nspname,v_role);
    END LOOP;
END;
$revoke$;

GRANT USAGE ON SCHEMA aap_identity_v1 TO :"status_role";
GRANT EXECUTE ON FUNCTION aap_identity_v1.read_actor_facts(uuid)
    TO :"status_role";

DO $verify$
DECLARE
    v_status oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('aap.status_role'));
    v_allowed oid := pg_catalog.to_regprocedure(
        'aap_identity_v1.read_actor_facts(uuid)'
    );
    v_relation record;
BEGIN
    IF v_allowed IS NULL
       OR NOT pg_catalog.has_function_privilege(v_status,v_allowed,'EXECUTE')
       OR pg_catalog.has_database_privilege(v_status,current_database(),'CREATE,TEMP') THEN
        RAISE EXCEPTION 'Identity actor-status LOGIN is missing its exact function profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=function.pronamespace
        JOIN pg_catalog.pg_type AS return_type
          ON return_type.oid=function.prorettype
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND function.prokind IN ('f','p','a','w')
          AND NOT (return_type.typnamespace='pg_catalog'::regnamespace
              AND return_type.typname IN ('trigger','event_trigger'))
          AND NOT EXISTS (
              SELECT FROM pg_catalog.pg_depend AS dependency
              WHERE dependency.classid='pg_catalog.pg_proc'::regclass
                AND dependency.objid=function.oid
                AND dependency.refclassid='pg_catalog.pg_extension'::regclass
                AND dependency.deptype='e'
          )
          AND (pg_catalog.left(namespace.nspname,4)='aap_'
               OR function.prosecdef
               OR EXISTS (
                   SELECT FROM pg_catalog.aclexplode(COALESCE(function.proacl,
                       pg_catalog.acldefault('f',function.proowner))) AS acl
                   WHERE acl.privilege_type='EXECUTE' AND acl.grantee<>0
                     AND pg_catalog.pg_has_role(v_status,acl.grantee,'USAGE')
               ))
          AND function.oid<>v_allowed
          AND pg_catalog.has_function_privilege(v_status,function.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Identity actor-status LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema','public','aap_identity_v1')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_status,namespace.oid,'USAGE')
               OR pg_catalog.has_schema_privilege(v_status,namespace.oid,'CREATE'))
    ) OR pg_catalog.has_schema_privilege(v_status,'public','CREATE')
      OR pg_catalog.has_schema_privilege(v_status,'aap_identity_v1','CREATE') THEN
        RAISE EXCEPTION 'Identity actor-status LOGIN has foreign schema privileges';
    END IF;
    FOR v_relation IN
        SELECT relation.oid, relation.relkind
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r','p','v','m','f','S')
    LOOP
        IF (v_relation.relkind <> 'S' AND (
                pg_catalog.has_table_privilege(v_status,v_relation.oid,
                    'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR pg_catalog.has_any_column_privilege(v_status,v_relation.oid,
                    'SELECT,INSERT,UPDATE,REFERENCES')
            )) OR (v_relation.relkind = 'S' AND
                pg_catalog.has_sequence_privilege(v_status,v_relation.oid,
                    'SELECT,UPDATE,USAGE')) THEN
            RAISE EXCEPTION 'Identity actor-status LOGIN has direct data privileges';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=v_allowed AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Identity actor-status function is executable by PUBLIC';
    END IF;
END;
$verify$;
COMMIT;
