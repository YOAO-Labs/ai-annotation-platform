-- Grant the Workspace configuration LOGIN only its closed native capabilities.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v identity_owner=identity_code -v workspace_owner=workspace_code
--   -v workspace_configuration_role=workspace_configuration_client
--   -f infra/docker/workspace-configuration-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'aap.workspace_configuration_role', :'workspace_configuration_role', true
);

DO $$
DECLARE
    role_name text := current_setting('aap.workspace_configuration_role');
    role_names text[] := ARRAY[
        current_setting('aap.identity_owner'),
        current_setting('aap.workspace_owner'),
        current_setting('aap.workspace_configuration_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'workspace_configuration_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 3 THEN
        RAISE EXCEPTION 'Unexpected Workspace configuration role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = (candidate.name = role_name)
        ) OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        ) OR membership.roleid = (
            SELECT role.oid FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Workspace configuration roles';
    END IF;
END $$;

DO $$
DECLARE
    identity_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.identity_owner')
    );
    workspace_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.workspace_owner')
    );
    client_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.workspace_configuration_role')
    );
    policy_oid oid := pg_catalog.to_regprocedure(
        'aap_workspace_configuration_v1.configuration_policy()'
    )::oid;
    step_oid oid := pg_catalog.to_regprocedure(
        'aap_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[])'
    )::oid;
    guide_oid oid := pg_catalog.to_regprocedure(
        'aap_workspace_configuration_v1.guide_assets_step(jsonb, text, jsonb)'
    )::oid;
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR client_oid IS NULL
       OR policy_oid IS NULL OR step_oid IS NULL OR guide_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Workspace configuration owner or LOGIN capability';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'aap_identity_v1.lock_accounts_share(uuid[],boolean)'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = policy_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = step_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = guide_oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = policy_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = step_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = guide_oid) IS DISTINCT FROM 'jsonb'::pg_catalog.regtype THEN
        RAISE EXCEPTION 'A Workspace configuration RPC has the wrong owner or return type';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'aap_workspace_configuration_v1', 'USAGE'
    ) OR NOT pg_catalog.has_schema_privilege(
        workspace_oid, 'aap_identity_v1', 'USAGE'
    ) OR NOT pg_catalog.has_function_privilege(
        workspace_oid,
        pg_catalog.to_regprocedure('aap_identity_v1.lock_accounts_share(uuid[],boolean)'),
        'EXECUTE'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'owner_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'updated_at', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'owner_id', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        workspace_oid, 'public.projects', 'updated_at', 'UPDATE'
    ) THEN
        RAISE EXCEPTION 'Workspace owner lacks a required configuration capability';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"workspace_configuration_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"workspace_configuration_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('aap.workspace_configuration_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',
            namespace.nspname, role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA aap_workspace_configuration_v1
    TO :"workspace_configuration_role";
GRANT EXECUTE ON FUNCTION
    aap_workspace_configuration_v1.guide_assets_step(jsonb, text, jsonb),
    aap_workspace_configuration_v1.configuration_policy(),
    aap_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[])
    TO :"workspace_configuration_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.workspace_configuration_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'aap_workspace_configuration_v1.configuration_policy()',
        'aap_workspace_configuration_v1.configuration_step(jsonb, text, jsonb, text[], text[])',
        'aap_workspace_configuration_v1.guide_assets_step(jsonb, text, jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname,
                 pg_catalog.oidvectortypes(function.proargtypes)
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    JOIN pg_catalog.pg_type AS return_type
      ON return_type.oid = function.prorettype
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND NOT (
          return_type.typnamespace = 'pg_catalog'::pg_catalog.regnamespace
          AND return_type.typname IN ('trigger', 'event_trigger')
      )
      AND NOT EXISTS (
          SELECT 1 FROM pg_catalog.pg_depend AS dependency
          WHERE dependency.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass
            AND dependency.objid = function.oid
            AND dependency.refclassid = 'pg_catalog.pg_extension'::pg_catalog.regclass
            AND dependency.deptype = 'e'
      )
      AND (
          pg_catalog.left(namespace.nspname, 4) = 'aap_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1
              FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl,
                  pg_catalog.acldefault('f', function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid, acl.grantee, 'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid, function.oid, 'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Workspace configuration LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'aap_workspace_configuration_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'aap_workspace_configuration_v1', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
      OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS relation
            ON relation.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
            AND (pg_catalog.has_table_privilege(
                     role_oid, relation.oid,
                     'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                 ) OR pg_catalog.has_any_column_privilege(
                     role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
                 ))
      ) OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS sequence
            ON sequence.relnamespace = namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND sequence.relkind = 'S'
            AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
                 OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
      ) OR pg_catalog.has_database_privilege(
          role_oid, current_database(), 'CREATE,TEMP'
      ) THEN
        RAISE EXCEPTION 'Workspace configuration LOGIN has non-function capabilities';
    END IF;
END $$;
COMMIT;
