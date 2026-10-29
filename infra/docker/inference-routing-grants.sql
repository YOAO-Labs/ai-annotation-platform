-- Grant the routing LOGIN only its four fixed Native capabilities.
-- Apply module-data-grants.sql first in the same explicitly named database.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v inference_owner=inference_code
--   -v inference_routing_role=inference_routing_client
--   -f infra/docker/inference-routing-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.inference_owner', :'inference_owner', true);
SELECT set_config('aap.inference_routing_role', :'inference_routing_role', true);

DO $$
DECLARE
    role_name text := current_setting('aap.inference_routing_role');
    owner_name text := current_setting('aap.inference_owner');
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'inference_routing_client'
       OR owner_name <> 'inference_code'
       OR role_name = owner_name THEN
        RAISE EXCEPTION 'Unexpected routing roles';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = role_name AND role.rolcanlogin
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = owner_name AND NOT role.rolcanlogin
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                   OR role.rolreplication OR role.rolbypassrls)
    ) OR pg_catalog.pg_has_role(role_name, current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'Unsafe routing roles';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS role
          ON role.oid IN (membership.member, membership.roleid)
        WHERE role.rolname = role_name
    ) THEN
        RAISE EXCEPTION 'Routing LOGIN must have no memberships';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (
        SELECT oid FROM pg_catalog.pg_roles
        WHERE rolname = current_setting('aap.inference_owner')
    );
    actual_tables text[];
    expected_tables text[] := ARRAY[
        'attempts', 'authority_secret', 'command_receipts', 'control_attempts',
        'controls', 'plugin_replacement_bindings', 'plugin_terminal_update_guard',
        'registry_state', 'scope_retirements', 'scopes',
        'video_execution_scope_permits', 'video_restore_attempts',
        'video_restore_contexts', 'video_restore_function_restore',
        'video_restore_no_scope_receipts', 'video_restore_records',
        'video_restore_scope_retirements', 'video_restore_scopes', 'weights'
    ];
    bad_function text;
    required record;
    function_oid oid;
BEGIN
    IF (SELECT nspowner FROM pg_catalog.pg_namespace
        WHERE nspname = 'aap_inference_routing_v1') IS DISTINCT FROM owner_oid THEN
        RAISE EXCEPTION 'Routing schema has unexpected owner';
    END IF;
    SELECT COALESCE(pg_catalog.array_agg(
                        relation.relname ORDER BY relation.relname COLLATE "C"
                    ),
                    ARRAY[]::text[])
      INTO actual_tables
      FROM pg_catalog.pg_class AS relation
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
     WHERE namespace.nspname = 'aap_inference_routing_v1'
       AND relation.relkind IN ('r', 'p');
    IF actual_tables IS DISTINCT FROM expected_tables OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'aap_inference_routing_v1'
          AND relation.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
          AND relation.relowner IS DISTINCT FROM owner_oid
    ) THEN
        RAISE EXCEPTION 'Routing private relations have unexpected inventory or owner';
    END IF;
    SELECT pg_catalog.format('%I.%I(%s)', namespace.nspname, function.proname,
                             pg_catalog.oidvectortypes(function.proargtypes))
      INTO bad_function
      FROM pg_catalog.pg_proc AS function
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = function.pronamespace
      WHERE namespace.nspname = 'aap_inference_routing_v1'
        AND (function.proowner IS DISTINCT FROM owner_oid
             OR NOT function.prosecdef
             OR function.proconfig IS NULL
             OR NOT (function.proconfig @> ARRAY[
                 'search_path=pg_catalog, pg_temp','TimeZone=UTC',
                 'DateStyle=ISO, YMD','extra_float_digits=3'
             ]::text[]))
      LIMIT 1;
    IF bad_function IS NOT NULL THEN
        RAISE EXCEPTION 'Routing Native function has unexpected owner or settings: %',
            bad_function;
    END IF;
    FOR required IN SELECT * FROM (VALUES
        ('aap_inference_routing_v1.routing_policy()','jsonb'),
        ('aap_inference_routing_v1.routing_step(text,text)','jsonb'),
        ('aap_inference_routing_v1.scope_retirement_step(text,text)','jsonb'),
        ('aap_inference_routing_v1.video_execution_scope_step(text,text)','jsonb')
    ) AS capability(signature,result_type)
    LOOP
        function_oid := pg_catalog.to_regprocedure(required.signature)::oid;
        IF function_oid IS NULL OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_proc AS function
            WHERE function.oid = function_oid
              AND (function.proowner IS DISTINCT FROM owner_oid
                   OR function.prorettype IS DISTINCT FROM pg_catalog.to_regtype(required.result_type)
                   OR NOT function.prosecdef
                   OR function.proconfig IS NULL
                   OR NOT (function.proconfig @> ARRAY[
                       'search_path=pg_catalog, pg_temp','TimeZone=UTC',
                       'DateStyle=ISO, YMD','extra_float_digits=3'
                   ]::text[]))
        ) THEN
            RAISE EXCEPTION 'Routing Native capability has unexpected ownership or settings';
        END IF;
    END LOOP;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"inference_routing_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"inference_routing_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('aap.inference_routing_role');
BEGIN
    FOR namespace IN SELECT ns.nspname FROM pg_catalog.pg_namespace AS ns
        WHERE ns.nspname NOT IN ('pg_catalog','information_schema')
          AND ns.nspname NOT LIKE 'pg_toast%' AND ns.nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I',namespace.nspname,role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',namespace.nspname,role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',namespace.nspname,role_name);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',namespace.nspname,role_name);
    END LOOP;
END $$;
GRANT USAGE ON SCHEMA aap_inference_routing_v1
    TO :"inference_routing_role";
GRANT EXECUTE ON FUNCTION
    aap_inference_routing_v1.routing_policy(),
    aap_inference_routing_v1.routing_step(text,text),
    aap_inference_routing_v1.scope_retirement_step(text,text),
    aap_inference_routing_v1.video_execution_scope_step(text,text)
    TO :"inference_routing_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.inference_routing_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'aap_inference_routing_v1.routing_policy()',
        'aap_inference_routing_v1.routing_step(text, text)',
        'aap_inference_routing_v1.scope_retirement_step(text, text)',
        'aap_inference_routing_v1.video_execution_scope_step(text, text)'
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
          pg_catalog.left(namespace.nspname,4) = 'aap_'
          OR function.prosecdef
          OR EXISTS (
              SELECT 1 FROM pg_catalog.aclexplode(COALESCE(
                  function.proacl, pg_catalog.acldefault('f',function.proowner)
              )) AS acl
              WHERE acl.privilege_type = 'EXECUTE'
                AND acl.grantee <> 0
                AND pg_catalog.pg_has_role(role_oid,acl.grantee,'USAGE')
          )
      )
      AND pg_catalog.has_function_privilege(role_oid,function.oid,'EXECUTE');
    IF actual_functions IS DISTINCT FROM expected_functions THEN
        RAISE EXCEPTION 'Routing LOGIN has unexpected functions: %',actual_functions;
    END IF;
    IF pg_catalog.has_function_privilege(
        role_oid,
        pg_catalog.to_regprocedure(
            'aap_inference_routing_v1._routing_step_0279(text,text)'
        ),
        'EXECUTE'
    ) OR pg_catalog.has_function_privilege(
        role_oid,
        pg_catalog.to_regprocedure(
            'aap_inference_routing_v1._scope_retirement_step_0280(text,text)'
        ),
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Routing LOGIN must not call private legacy dispatchers';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog','information_schema','aap_inference_routing_v1','public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid,namespace.oid,'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid,namespace.oid,'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid,'aap_inference_routing_v1','CREATE'
    ) OR pg_catalog.has_schema_privilege(role_oid,'public','CREATE')
      OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS relation ON relation.relnamespace=namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND relation.relkind IN ('r','p','v','m','f')
            AND (pg_catalog.has_table_privilege(
                     role_oid,relation.oid,
                     'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                 ) OR pg_catalog.has_any_column_privilege(
                     role_oid,relation.oid,'SELECT,INSERT,UPDATE,REFERENCES'
                 ))
      ) OR EXISTS (
          SELECT 1
          FROM pg_catalog.pg_namespace AS namespace
          JOIN pg_catalog.pg_class AS sequence ON sequence.relnamespace=namespace.oid
          WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
            AND namespace.nspname NOT LIKE 'pg_toast%'
            AND namespace.nspname NOT LIKE 'pg_temp_%'
            AND sequence.relkind='S'
            AND (pg_catalog.has_sequence_privilege(role_oid,sequence.oid,'USAGE')
                 OR pg_catalog.has_sequence_privilege(role_oid,sequence.oid,'SELECT')
                 OR pg_catalog.has_sequence_privilege(role_oid,sequence.oid,'UPDATE'))
      ) OR pg_catalog.has_database_privilege(
          role_oid,current_database(),'CREATE,TEMP'
      ) THEN
        RAISE EXCEPTION 'Routing LOGIN has non-function capabilities';
    END IF;
END $$;
COMMIT;
