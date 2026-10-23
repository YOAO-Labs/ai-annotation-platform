-- Give the Video restore operator LOGIN exactly one Inference Native function.
-- Apply module-data-grants.sql first in the same explicitly named database.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v inference_owner=inference_code -v video_restore_operator_role=video_restore_operator_client
--   -f infra/docker/video-restore-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.inference_owner', :'inference_owner', true);
SELECT set_config('aap.video_restore_operator_role', :'video_restore_operator_role', true);

DO $$
DECLARE
    role_name text := current_setting('aap.video_restore_operator_role');
    owner_name text := current_setting('aap.inference_owner');
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'video_restore_operator_client' OR owner_name <> 'inference_code'
       OR role_name = owner_name THEN
        RAISE EXCEPTION 'Unexpected Video restore operator roles';
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
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = (
            SELECT oid FROM pg_catalog.pg_roles WHERE rolname = role_name
        ) OR membership.roleid = (
            SELECT oid FROM pg_catalog.pg_roles WHERE rolname = role_name
        )
    ) THEN
        RAISE EXCEPTION 'Video restore operator roles are unsafe or have memberships';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (
        SELECT oid FROM pg_catalog.pg_roles
        WHERE rolname = current_setting('aap.inference_owner')
    );
    function_oid oid;
    signature text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'aap_inference_routing_v1'
    ) OR NOT pg_catalog.has_schema_privilege(
        owner_oid, 'aap_inference_routing_v1', 'USAGE'
    ) THEN
        RAISE EXCEPTION 'Video restore operator Native schema or function is unavailable';
    END IF;
    FOREACH signature IN ARRAY ARRAY[
        'aap_inference_routing_v1.video_restore_step(text,text)'
    ] LOOP
    function_oid := pg_catalog.to_regprocedure(signature);
    IF function_oid IS NULL OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS function
        WHERE function.oid = function_oid
          AND (function.proowner IS DISTINCT FROM owner_oid
               OR function.prorettype IS DISTINCT FROM 'jsonb'::pg_catalog.regtype
               OR NOT function.prosecdef
               OR function.proconfig IS NULL
               OR NOT (function.proconfig @> ARRAY[
                   'search_path=pg_catalog, pg_temp','TimeZone=UTC',
                   'DateStyle=ISO, YMD','extra_float_digits=3'
               ]::text[]))
    ) THEN
        RAISE EXCEPTION 'Video restore operator capability has unexpected ownership or settings';
    END IF;
    END LOOP;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"video_restore_operator_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"video_restore_operator_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('aap.video_restore_operator_role');
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
GRANT USAGE ON SCHEMA aap_inference_routing_v1 TO :"video_restore_operator_role";
GRANT EXECUTE ON FUNCTION aap_inference_routing_v1.video_restore_step(text,text)
    TO :"video_restore_operator_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.video_restore_operator_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'aap_inference_routing_v1.video_restore_step(text, text)'
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
        RAISE EXCEPTION 'Video restore operator LOGIN has unexpected functions: %',actual_functions;
    END IF;
END $$;
COMMIT;
