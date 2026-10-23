-- Grant the Inference pipelines LOGIN only its two closed native functions.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v identity_owner=identity_code -v workspace_owner=workspace_code
--   -v inference_owner=inference_code
--   -v inference_pipelines_role=inference_pipelines_client
--   -f infra/docker/inference-pipelines-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.inference_owner', :'inference_owner', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'aap.inference_pipelines_role', :'inference_pipelines_role', true
);

DO $$
DECLARE
    role_name text := current_setting('aap.inference_pipelines_role');
    role_names text[] := ARRAY[
        current_setting('aap.identity_owner'),
        current_setting('aap.inference_owner'),
        current_setting('aap.workspace_owner'),
        current_setting('aap.inference_pipelines_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'inference_pipelines_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 4 THEN
        RAISE EXCEPTION 'Unexpected Inference pipelines role names';
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
        RAISE EXCEPTION 'Unsafe or inheriting Inference pipelines roles';
    END IF;
END $$;

DO $$
DECLARE
    inference_oid oid := (
        SELECT oid FROM pg_catalog.pg_roles
        WHERE rolname = current_setting('aap.inference_owner')
    );
    policy_oid oid := pg_catalog.to_regprocedure(
        'aap_inference_pipelines_v1.pipeline_policy()'
    )::oid;
    step_oid oid := pg_catalog.to_regprocedure(
        'aap_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[])'
    )::oid;
    dependency record;
    dependency_oid oid;
BEGIN
    IF inference_oid IS NULL OR policy_oid IS NULL OR step_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Inference pipelines owner or LOGIN capability';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        WHERE function.oid IN (policy_oid, step_oid)
          AND (function.proowner IS DISTINCT FROM inference_oid
               OR function.prorettype IS DISTINCT FROM 'jsonb'::pg_catalog.regtype
               OR NOT function.prosecdef
               OR function.proconfig IS NULL
               OR NOT (function.proconfig @> ARRAY[
                   'search_path=pg_catalog, pg_temp', 'TimeZone=UTC',
                   'DateStyle=ISO, YMD', 'extra_float_digits=3'
               ]::text[]))
    ) THEN
        RAISE EXCEPTION 'An Inference pipelines RPC has the wrong owner or settings';
    END IF;
    FOR dependency IN
        SELECT * FROM (VALUES
            ('aap_workspace_v1.project_access_step(uuid,uuid,text)',
             current_setting('aap.workspace_owner')),
            ('aap_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)',
             current_setting('aap.workspace_owner')),
            ('aap_identity_v1.workspace_template_query_org_ids(uuid)',
             current_setting('aap.identity_owner'))
        ) AS required(signature, owner_name)
    LOOP
        dependency_oid := pg_catalog.to_regprocedure(dependency.signature)::oid;
        IF dependency_oid IS NULL
           OR (SELECT pg_catalog.pg_get_userbyid(function.proowner)
               FROM pg_catalog.pg_proc AS function
               WHERE function.oid=dependency_oid) IS DISTINCT FROM dependency.owner_name
           OR NOT pg_catalog.has_function_privilege(inference_oid,dependency_oid,'EXECUTE')
           OR NOT pg_catalog.has_schema_privilege(
               inference_oid,
               (SELECT pronamespace FROM pg_catalog.pg_proc WHERE oid=dependency_oid),
               'USAGE'
           ) THEN
            RAISE EXCEPTION 'Inference owner lacks a required public capability';
        END IF;
    END LOOP;
    IF NOT pg_catalog.has_schema_privilege(
        inference_oid,'aap_inference_pipelines_v1','USAGE'
    ) OR NOT pg_catalog.has_column_privilege(
        inference_oid,'public.project_pipelines','id','SELECT'
    ) OR NOT pg_catalog.has_column_privilege(
        inference_oid,'public.project_pipelines','id','INSERT'
    ) OR NOT pg_catalog.has_column_privilege(
        inference_oid,'public.project_pipelines','stages','UPDATE'
    ) OR NOT pg_catalog.has_table_privilege(
        inference_oid,'public.project_pipelines','DELETE'
    ) THEN
        RAISE EXCEPTION 'Inference owner lacks required pipeline data privileges';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"inference_pipelines_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"inference_pipelines_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('aap.inference_pipelines_role');
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

GRANT USAGE ON SCHEMA aap_inference_pipelines_v1
    TO :"inference_pipelines_role";
GRANT EXECUTE ON FUNCTION
    aap_inference_pipelines_v1.pipeline_policy(),
    aap_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[])
    TO :"inference_pipelines_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.inference_pipelines_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'aap_inference_pipelines_v1.pipeline_policy()',
        'aap_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[])'
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
        RAISE EXCEPTION 'Inference pipelines LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'aap_inference_pipelines_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'aap_inference_pipelines_v1', 'CREATE'
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
        RAISE EXCEPTION 'Inference pipelines LOGIN has non-function capabilities';
    END IF;
END $$;
COMMIT;
