-- Grant the Labeling class-query LOGIN only its three closed native functions.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v labeling_owner=labeling_code
--   -v labeling_class_query_role=labeling_class_query_client
--   -f infra/docker/labeling-class-query-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config(
    'aap.labeling_class_query_role', :'labeling_class_query_role', true
);

DO $$
DECLARE
    client_name text := current_setting('aap.labeling_class_query_role');
    role_names text[] := ARRAY[
        current_setting('aap.labeling_owner'),
        current_setting('aap.labeling_class_query_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF client_name <> 'labeling_class_query_client'
       OR (SELECT pg_catalog.count(DISTINCT candidate.name)
           FROM pg_catalog.unnest(role_names) AS candidate(name)) <> 2 THEN
        RAISE EXCEPTION 'Unexpected Labeling class-query role names';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(role_names) AS candidate(name)
        WHERE NOT EXISTS (
            SELECT 1
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (
                  role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls
              )
              AND role.rolcanlogin = (candidate.name = client_name)
        )
        OR pg_catalog.pg_has_role(candidate.name, current_user, 'MEMBER')
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT role.oid
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = client_name
        ) OR membership.roleid = (
            SELECT role.oid
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = client_name
        )
    ) THEN
        RAISE EXCEPTION 'Unsafe or inheriting Labeling class-query roles';
    END IF;
END $$;

DO $$
DECLARE
    labeling_oid oid := (
        SELECT role.oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.labeling_owner')
    );
    policy_oid oid := pg_catalog.to_regprocedure(
        'aap_labeling_class_query_v1.class_query_policy()'
    )::oid;
    classes_oid oid := pg_catalog.to_regprocedure(
        'aap_labeling_class_query_v1.class_counts(uuid)'
    )::oid;
    attributes_oid oid := pg_catalog.to_regprocedure(
        'aap_labeling_class_query_v1.attribute_rows(uuid)'
    )::oid;
BEGIN
    IF labeling_oid IS NULL
       OR policy_oid IS NULL OR classes_oid IS NULL OR attributes_oid IS NULL THEN
        RAISE EXCEPTION 'Missing Labeling class-query owner or LOGIN capability';
    END IF;
    IF (SELECT function.proowner FROM pg_catalog.pg_proc AS function
        WHERE function.oid = policy_oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = classes_oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.proowner FROM pg_catalog.pg_proc AS function
           WHERE function.oid = attributes_oid) IS DISTINCT FROM labeling_oid
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = policy_oid)
            IS DISTINCT FROM 'jsonb'::pg_catalog.regtype
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = classes_oid)
            IS DISTINCT FROM 'record'::pg_catalog.regtype
       OR (SELECT function.prorettype FROM pg_catalog.pg_proc AS function
           WHERE function.oid = attributes_oid)
            IS DISTINCT FROM 'text'::pg_catalog.regtype
       OR (SELECT function.proretset FROM pg_catalog.pg_proc AS function
           WHERE function.oid = policy_oid) IS DISTINCT FROM false
       OR (SELECT function.proretset FROM pg_catalog.pg_proc AS function
           WHERE function.oid = classes_oid) IS DISTINCT FROM true
       OR (SELECT function.proretset FROM pg_catalog.pg_proc AS function
           WHERE function.oid = attributes_oid) IS DISTINCT FROM true
       OR (SELECT pg_catalog.pg_get_function_result(function.oid)
           FROM pg_catalog.pg_proc AS function
           WHERE function.oid = policy_oid) IS DISTINCT FROM 'jsonb'
       OR (SELECT pg_catalog.pg_get_function_result(function.oid)
           FROM pg_catalog.pg_proc AS function
           WHERE function.oid = classes_oid)
            IS DISTINCT FROM 'TABLE(class_name text, annotation_count bigint)'
       OR (SELECT pg_catalog.pg_get_function_result(function.oid)
           FROM pg_catalog.pg_proc AS function
           WHERE function.oid = attributes_oid)
            IS DISTINCT FROM 'TABLE(attributes_json text)'
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_proc AS function
           WHERE function.oid IN (policy_oid, classes_oid, attributes_oid)
             AND (
                 function.prokind <> 'f'
                 OR function.provolatile <> 's'
                 OR function.prosecdef IS NOT TRUE
                 OR function.proconfig IS DISTINCT FROM
                    ARRAY['search_path=pg_catalog, pg_temp']::text[]
             )
       ) THEN
        RAISE EXCEPTION 'A Labeling class-query function has the wrong owner, signature, or security';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(
            function.proacl,
            pg_catalog.acldefault('f', function.proowner)
        )) AS acl
        WHERE function.oid IN (policy_oid, classes_oid, attributes_oid)
          AND acl.grantee = 0
          AND acl.privilege_type = 'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling class-query functions must not be executable by PUBLIC';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(
        labeling_oid, 'aap_labeling_class_query_v1', 'USAGE'
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(ARRAY[
            'id', 'project_id', 'is_active', 'class_name', 'attributes'
        ]::text[]) AS column_name(name)
        WHERE NOT pg_catalog.has_column_privilege(
            labeling_oid, 'public.annotations', column_name.name, 'SELECT'
        )
    ) THEN
        RAISE EXCEPTION 'Labeling owner lacks its class-query read capability';
    END IF;
    IF aap_labeling_class_query_v1.class_query_policy()
       IS DISTINCT FROM '{"schema":"aap-labeling-class-query/v1"}'::jsonb THEN
        RAISE EXCEPTION 'Unexpected Labeling class-query policy';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database"
    FROM :"labeling_class_query_role";
GRANT CONNECT ON DATABASE :"expected_database"
    TO :"labeling_class_query_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('aap.labeling_class_query_role');
BEGIN
    FOR namespace IN
        SELECT ns.nspname
        FROM pg_catalog.pg_namespace AS ns
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

GRANT USAGE ON SCHEMA aap_labeling_class_query_v1
    TO :"labeling_class_query_role";
GRANT EXECUTE ON FUNCTION
    aap_labeling_class_query_v1.class_query_policy(),
    aap_labeling_class_query_v1.class_counts(uuid),
    aap_labeling_class_query_v1.attribute_rows(uuid)
    TO :"labeling_class_query_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.labeling_class_query_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'aap_labeling_class_query_v1.attribute_rows(uuid)',
        'aap_labeling_class_query_v1.class_counts(uuid)',
        'aap_labeling_class_query_v1.class_query_policy()'
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
      AND pg_catalog.has_function_privilege(
          role_oid, function.oid, 'EXECUTE'
      );
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Labeling class-query LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'aap_labeling_class_query_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (
              pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
              OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
          )
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'aap_labeling_class_query_v1', 'CREATE'
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'public', 'CREATE'
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
              pg_catalog.has_table_privilege(
                  role_oid, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              ) OR pg_catalog.has_any_column_privilege(
                  role_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
              )
          )
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_namespace AS namespace
        JOIN pg_catalog.pg_class AS sequence
          ON sequence.relnamespace = namespace.oid
        WHERE namespace.nspname NOT IN ('pg_catalog', 'information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND sequence.relkind = 'S'
          AND (
              pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'USAGE')
              OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'SELECT')
              OR pg_catalog.has_sequence_privilege(role_oid, sequence.oid, 'UPDATE')
          )
    ) OR pg_catalog.has_database_privilege(
        role_oid, current_database(), 'CREATE,TEMP'
    ) OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = role_oid OR membership.roleid = role_oid
    ) THEN
        RAISE EXCEPTION 'Labeling class-query LOGIN has non-function capabilities';
    END IF;
END $$;
COMMIT;
