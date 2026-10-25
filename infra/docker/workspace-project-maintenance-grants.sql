-- Grant the trusted Workspace maintenance LOGIN only its six fixed native functions.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v workspace_owner=workspace_code
--   -v workspace_project_maintenance_role=workspace_project_maintenance_client
--   -f infra/docker/workspace-project-maintenance-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'aap.workspace_project_maintenance_role', :'workspace_project_maintenance_role', true
);

DO $$
DECLARE
    role_name text := current_setting('aap.workspace_project_maintenance_role');
    role_names text[] := ARRAY[
        current_setting('aap.workspace_owner'),
        current_setting('aap.workspace_project_maintenance_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'workspace_project_maintenance_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 2 THEN
        RAISE EXCEPTION 'Unexpected Workspace project-maintenance role names';
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
        RAISE EXCEPTION 'Unsafe or inheriting Workspace project-maintenance roles';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.workspace_owner')
    );
    signature text;
    function_oid oid;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'aap_workspace_v1.read_import_project(text)',
        'aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb)',
        'aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)',
        'aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb)',
        'aap_workspace_v1.write_seed_members(jsonb)',
        'aap_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)',
        'aap_workspace_v1.create_media_seed_project(uuid,text,text,uuid,text,jsonb)',
        'aap_workspace_v1.write_seed_project_projection(uuid,jsonb)',
        'aap_workspace_v1.pin_seed_member_assignment(uuid,timestamptz)'
    ] LOOP
        function_oid := pg_catalog.to_regprocedure(signature);
        IF function_oid IS NULL OR NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_proc AS function
            WHERE function.oid = function_oid
              AND function.proowner = owner_oid
              AND function.prorettype = 'jsonb'::pg_catalog.regtype
              AND function.prosecdef
        ) THEN
            RAISE EXCEPTION 'Wrong Workspace project-maintenance capability owner or shape';
        END IF;
    END LOOP;
    IF NOT pg_catalog.has_schema_privilege(owner_oid, 'aap_workspace_v1', 'USAGE')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'display_id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'point_cloud_quality_config', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'point_cloud_quality_config', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'updated_at', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'updated_at', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'created_at', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'ml_backend_pool_id', 'UPDATE')
       OR NOT pg_catalog.has_column_privilege(owner_oid, 'public.projects', 'preannotate_pipeline', 'UPDATE') THEN
        RAISE EXCEPTION 'Workspace owner lacks required project-maintenance capabilities';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"workspace_project_maintenance_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"workspace_project_maintenance_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('aap.workspace_project_maintenance_role');
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

GRANT USAGE ON SCHEMA aap_workspace_v1
    TO :"workspace_project_maintenance_role";
GRANT EXECUTE ON FUNCTION
    aap_workspace_v1.read_import_project(text),
    aap_workspace_v1.create_import_project(uuid,text,text,uuid,jsonb),
    aap_workspace_v1.create_point_cloud_seed_project(uuid,text,text,uuid,jsonb),
    aap_workspace_v1.write_nuscenes_seed_defaults(uuid,text,jsonb),
    aap_workspace_v1.write_seed_members(jsonb),
    aap_workspace_v1.write_point_cloud_quality_config(uuid,jsonb)
    TO :"workspace_project_maintenance_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.workspace_project_maintenance_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'aap_workspace_v1.create_import_project(uuid, text, text, uuid, jsonb)',
        'aap_workspace_v1.create_point_cloud_seed_project(uuid, text, text, uuid, jsonb)',
        'aap_workspace_v1.read_import_project(text)',
        'aap_workspace_v1.write_nuscenes_seed_defaults(uuid, text, jsonb)',
        'aap_workspace_v1.write_point_cloud_quality_config(uuid, jsonb)',
        'aap_workspace_v1.write_seed_members(jsonb)'
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
        RAISE EXCEPTION 'Workspace project-maintenance LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'aap_workspace_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'aap_workspace_v1', 'CREATE'
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
        RAISE EXCEPTION 'Workspace project-maintenance LOGIN has non-function capabilities';
    END IF;
END $$;
COMMIT;
