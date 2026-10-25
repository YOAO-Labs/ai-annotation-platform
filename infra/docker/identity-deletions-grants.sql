-- Grant only the Identity deletion policy and command to its dedicated
-- LOGIN consumer. User and owner tables remain inaccessible to this role.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v identity_owner=identity_code -v workspace_owner=workspace_code
--   -v labeling_owner=labeling_code -v audit_owner=audit_code
--   -v identity_deletion_role=identity_deletion_client
--   -f infra/docker/identity-deletions-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config('aap.audit_owner', :'audit_owner', true);
SELECT set_config(
    'aap.identity_deletion_role', :'identity_deletion_role', true
);

DO $$
DECLARE
    role_name text := current_setting('aap.identity_deletion_role');
    role_names text[] := ARRAY[
        current_setting('aap.identity_owner'),
        current_setting('aap.workspace_owner'),
        current_setting('aap.labeling_owner'),
        current_setting('aap.audit_owner'),
        current_setting('aap.identity_deletion_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'identity_deletion_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM unnest(role_names) AS candidate(name)) <> 5 THEN
        RAISE EXCEPTION 'Unexpected Identity deletion role names';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(role_names) AS candidate(name)
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
        RAISE EXCEPTION 'Unsafe or inheriting Identity deletion roles';
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
    labeling_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.labeling_owner')
    );
    audit_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.audit_owner')
    );
    deletion_role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.identity_deletion_role')
    );
BEGIN
    IF identity_oid IS NULL OR workspace_oid IS NULL OR labeling_oid IS NULL
       OR audit_oid IS NULL OR deletion_role_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Identity deletion code owner or client';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = pg_catalog.to_regprocedure(
            'aap_identity_deletions_v1.deletion_policy()'
        )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_identity_deletions_v1.execute_deletion_command(jsonb)'
           )::oid) IS DISTINCT FROM identity_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_workspace_v1.lock_identity_deletion_scope(uuid,uuid)'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_workspace_v1.identity_deletion_transfer_covered(uuid,uuid[])'
           )::oid) IS DISTINCT FROM workspace_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_labeling_v1.identity_deletion_work(uuid)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_labeling_v1.transfer_identity_deletion_work(uuid,uuid)'
           )::oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = pg_catalog.to_regprocedure(
               'aap_audit_v1.record_identity_deletion_event(jsonb,uuid,jsonb,jsonb)'
           )::oid) IS DISTINCT FROM audit_oid THEN
        RAISE EXCEPTION 'An Identity deletion capability has the wrong owner';
    END IF;
    IF NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'aap_workspace_v1.lock_identity_deletion_scope(uuid,uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'aap_workspace_v1.identity_deletion_transfer_covered(uuid,uuid[])'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'aap_labeling_v1.identity_deletion_work(uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'aap_labeling_v1.transfer_identity_deletion_work(uuid,uuid)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_function_privilege(
        identity_oid, pg_catalog.to_regprocedure(
            'aap_audit_v1.record_identity_deletion_event(jsonb,uuid,jsonb,jsonb)'
        )::oid, 'EXECUTE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'operation_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'actor_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'operation', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'target_user_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'transfer_to_user_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'request_fingerprint', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'derivation_id', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'target_generation', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'target_is_active', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'result', 'SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.identity_deletion_operation_receipts',
        'result', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'is_active', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_kind', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_by', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        identity_oid, 'public.users', 'disabled_reason', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        audit_oid, 'public.audit_logs', 'detail_json', 'INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.tasks', 'assignee_id', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.tasks', 'assigned_at', 'UPDATE'
    ) OR NOT pg_catalog.has_column_privilege(
        labeling_oid, 'public.tasks', 'updated_at', 'UPDATE'
    ) THEN
        RAISE EXCEPTION 'Identity deletion owners are missing a receipt, Workspace, Labeling or Audit capability';
    END IF;
    IF pg_catalog.has_table_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.tasks', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.task_batches', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.projects', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.project_members', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.tasks', 'SELECT,INSERT,UPDATE,REFERENCES'
    ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.task_batches', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
           identity_oid, 'public.task_locks', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.task_locks', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_any_column_privilege(
           identity_oid, 'public.audit_logs', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
           deletion_role_oid, 'public.identity_deletion_operation_receipts',
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) OR pg_catalog.has_table_privilege(
           deletion_role_oid, 'public.users', 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE'
       ) THEN
        RAISE EXCEPTION 'Identity deletion owner/client has raw table access';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"identity_deletion_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"identity_deletion_role";
DO $$
DECLARE
    namespace record;
    column_grant record;
    role_name text := current_setting('aap.identity_deletion_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%'
          AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, role_name);
    END LOOP;
    FOR column_grant IN
        SELECT grant_row.table_schema, grant_row.table_name,
               grant_row.column_name, grant_row.privilege_type
        FROM information_schema.column_privileges AS grant_row
        WHERE grant_row.grantee = role_name
    LOOP
        EXECUTE pg_catalog.format(
            'REVOKE %s (%I) ON TABLE %I.%I FROM %I',
            column_grant.privilege_type,
            column_grant.column_name,
            column_grant.table_schema,
            column_grant.table_name,
            role_name
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA aap_identity_deletions_v1
    TO :"identity_deletion_role";
GRANT EXECUTE ON FUNCTION
    aap_identity_deletions_v1.deletion_policy(),
    aap_identity_deletions_v1.execute_deletion_command(jsonb)
    TO :"identity_deletion_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.identity_deletion_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'aap_identity_deletions_v1.deletion_policy()',
        'aap_identity_deletions_v1.execute_deletion_command(jsonb)'
    ];
BEGIN
    SELECT COALESCE(pg_catalog.array_agg(
        namespace.nspname || '.' || function.proname || '(' ||
        pg_catalog.oidvectortypes(function.proargtypes) || ')'
        ORDER BY namespace.nspname, function.proname
    ), ARRAY[]::text[]) INTO actual_functions
    FROM pg_catalog.pg_proc AS function
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = function.pronamespace
    WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
      AND namespace.nspname NOT LIKE 'pg_toast%'
      AND namespace.nspname NOT LIKE 'pg_temp_%'
      AND function.prokind IN ('f', 'p', 'a', 'w')
      AND function.prorettype NOT IN (
          'trigger'::pg_catalog.regtype, 'event_trigger'::pg_catalog.regtype
      )
      AND NOT EXISTS (
          SELECT 1
          FROM pg_catalog.pg_depend AS dependency
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
        RAISE EXCEPTION 'Identity deletion LOGIN has unexpected functions: %', actual_functions;
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
           role_oid, 'aap_identity_deletions_v1', 'USAGE'
       ) OR pg_catalog.has_schema_privilege(
           role_oid, 'aap_identity_deletions_v1', 'CREATE'
       ) OR pg_catalog.has_schema_privilege(role_oid, 'public', 'CREATE')
       OR pg_catalog.has_database_privilege(
           role_oid, current_database(), 'CREATE,TEMP'
       ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
                  'pg_catalog', 'information_schema',
                  'aap_identity_deletions_v1', 'public'
              )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.has_table_privilege(role_oid, relation.oid, 'SELECT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'INSERT')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'UPDATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'DELETE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRUNCATE')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'REFERENCES')
               OR pg_catalog.has_table_privilege(role_oid, relation.oid, 'TRIGGER')
               OR pg_catalog.has_any_column_privilege(
                   role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
               ))
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
               OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE'))
    ) THEN
        RAISE EXCEPTION 'Identity deletion LOGIN has non-function capabilities';
    END IF;
END $$;
COMMIT;
