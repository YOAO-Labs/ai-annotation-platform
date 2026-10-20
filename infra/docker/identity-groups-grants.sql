-- Grant the exact Identity group command LOGIN after migrations and
-- module-data-grants. The restricted role must have no memberships.
-- Database provisioning revokes PUBLIC TEMP; this script changes no PUBLIC ACL.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v identity_owner=identity_code -v workspace_owner=workspace_code \
--   -v audit_owner=audit_code -v identity_group_role=identity_group_client \
--   -f infra/docker/identity-groups-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config('aap.audit_owner', :'audit_owner', true);
SELECT set_config('aap.identity_group_role', :'identity_group_role', true);

DO $$
DECLARE
    role_name text;
    role_oid oid;
    identity_oid oid;
    workspace_oid oid;
    audit_oid oid;
    migration_oid oid;
    workspace_function_oid oid;
    audit_function_oid oid;
    role_names text[] := ARRAY[
        current_setting('aap.identity_owner'),
        current_setting('aap.workspace_owner'),
        current_setting('aap.audit_owner'),
        current_setting('aap.identity_group_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT pg_catalog.count(DISTINCT name)
        FROM pg_catalog.unnest(role_names) AS expanded(name)) <> 4
       OR current_setting('aap.migration_role') = ANY(role_names) THEN
        RAISE EXCEPTION 'Migration, owners and group client must be distinct';
    END IF;
    IF current_setting('aap.identity_group_role') <> 'identity_group_client' THEN
        RAISE EXCEPTION 'Identity groups must use identity_group_client';
    END IF;

    FOREACH role_name IN ARRAY role_names LOOP
        IF role_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe Identity group role name';
        END IF;
        SELECT role.oid INTO role_oid
        FROM pg_catalog.pg_roles AS role WHERE role.rolname = role_name;
        IF NOT FOUND OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.oid = role_oid
              AND (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = role_oid OR membership.roleid = role_oid
        ) THEN
            RAISE EXCEPTION 'Unsafe or missing Identity group role';
        END IF;
    END LOOP;

    SELECT role.oid INTO identity_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.identity_owner');
    SELECT role.oid INTO workspace_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.workspace_owner');
    SELECT role.oid INTO audit_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.audit_owner');
    SELECT role.oid INTO migration_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.migration_role');
    SELECT role.oid INTO role_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.identity_group_role');
    workspace_function_oid := pg_catalog.to_regprocedure(
        'aap_workspace_v1.lock_group_assignment_scope(uuid,uuid[],boolean)'
    )::oid;
    audit_function_oid := pg_catalog.to_regprocedure(
        'aap_audit_v1.record_identity_group_event(text,jsonb,jsonb,jsonb)'
    )::oid;

    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid IN (identity_oid, workspace_oid, audit_oid)
          AND (role.rolcanlogin OR role.rolinherit)
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.oid = role_oid AND role.rolcanlogin AND NOT role.rolinherit
    ) THEN
        RAISE EXCEPTION 'Identity owners must be NOLOGIN and group client LOGIN NOINHERIT';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'aap_identity_groups_v1'
          AND namespace.nspowner = migration_oid
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
        WHERE namespace.nspname = 'aap_identity_groups_v1'
          AND function.prokind = 'f' AND function.proowner <> identity_oid
    ) THEN
        RAISE EXCEPTION 'Identity group schema or function owner is unexpected';
    END IF;
    IF workspace_function_oid IS NULL OR audit_function_oid IS NULL
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = workspace_function_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = audit_function_oid) IS DISTINCT FROM audit_oid THEN
        RAISE EXCEPTION 'Workspace or Audit group capability has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid,
        workspace_function_oid,
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,
        audit_function_oid,
        'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid,
        pg_catalog.to_regprocedure('aap_audit_v1.valid_request_context(jsonb)')::oid,
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Identity owner is missing Workspace or Audit capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"identity_group_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"identity_group_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('aap.identity_group_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA aap_identity_groups_v1 TO :"identity_group_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_groups_v1.group_policy(),
    aap_identity_groups_v1.execute_group_command(jsonb)
    TO :"identity_group_role";

-- Fail closed unless this LOGIN can reach exactly the two native group entrypoints.
DO $$
DECLARE
    role_oid oid;
    allowed_functions oid[] := ARRAY[
        pg_catalog.to_regprocedure('aap_identity_groups_v1.group_policy()')::oid,
        pg_catalog.to_regprocedure('aap_identity_groups_v1.execute_group_command(jsonb)')::oid
    ];
    actual_functions oid[];
BEGIN
    SELECT role.oid INTO role_oid FROM pg_catalog.pg_roles AS role
    WHERE role.rolname = current_setting('aap.identity_group_role');
    IF pg_catalog.array_position(allowed_functions, NULL::oid) IS NOT NULL THEN
        RAISE EXCEPTION 'Identity group entrypoint is missing';
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(function.oid ORDER BY function.oid), ARRAY[]::oid[])
    INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = function.pronamespace
    WHERE left(namespace.nspname, 4) = 'aap_'
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM (
        SELECT pg_catalog.array_agg(allowed.oid ORDER BY allowed.oid)
        FROM pg_catalog.unnest(allowed_functions) AS allowed(oid)
    ) THEN
        RAISE EXCEPTION 'Identity group LOGIN has an unexpected module function capability';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname ~ '^aap_[a-z0-9_]+_v1$'
          AND namespace.nspname <> 'aap_identity_groups_v1'
          AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Identity group LOGIN has a foreign module schema';
    END IF;
    IF pg_catalog.has_database_privilege(role_oid, current_database(), 'CREATE,TEMP') THEN
        RAISE EXCEPTION 'Identity group LOGIN has database CREATE or TEMP';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
    ) THEN
        RAISE EXCEPTION 'Identity group LOGIN can create schema objects';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(role_oid, relation.oid,
                   'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
            OR pg_catalog.has_any_column_privilege(role_oid, relation.oid,
                   'SELECT,INSERT,UPDATE,REFERENCES'))
    ) THEN
        RAISE EXCEPTION 'Identity group LOGIN has direct relation access';
    END IF;
END $$;
COMMIT;
