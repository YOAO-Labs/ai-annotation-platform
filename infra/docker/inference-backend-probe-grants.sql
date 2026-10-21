-- Grant the credential-owning Inference probe LOGIN only policy/open capabilities.
-- Apply module-data-grants.sql first in the same explicitly named database.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v inference_owner=inference_code
--   -v inference_backend_probe_role=inference_backend_probe_client
--   -f infra/docker/inference-backend-probe-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.inference_owner', :'inference_owner', true);
SELECT set_config('aap.inference_backend_probe_role', :'inference_backend_probe_role', true);

DO $$
DECLARE
    role_name text := current_setting('aap.inference_backend_probe_role');
    owner_name text := current_setting('aap.inference_owner');
    candidate record;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'inference_backend_probe_client' OR role_name = owner_name THEN
        RAISE EXCEPTION 'Unexpected Inference backend probe roles';
    END IF;
    FOR candidate IN SELECT * FROM (VALUES (role_name,true),(owner_name,false)) AS required(name,login)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = candidate.name
              AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                       OR role.rolreplication OR role.rolbypassrls)
              AND role.rolcanlogin = candidate.login
        ) OR pg_catalog.pg_has_role(candidate.name,current_user,'MEMBER') THEN
            RAISE EXCEPTION 'Unsafe Inference backend probe roles';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS role ON role.oid IN (membership.member,membership.roleid)
        WHERE role.rolname = role_name
    ) THEN
        RAISE EXCEPTION 'Inference backend probe LOGIN must have no memberships';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.inference_owner'));
    required record;
    function_oid oid;
BEGIN
    IF (SELECT relowner FROM pg_catalog.pg_class WHERE oid=pg_catalog.to_regclass(
        'aap_inference_backend_probe_v1.probe_secret')) IS DISTINCT FROM owner_oid THEN
        RAISE EXCEPTION 'Backend probe key has an unexpected owner';
    END IF;
    FOR required IN SELECT * FROM (VALUES
        ('aap_inference_backend_probe_v1.backend_probe_policy()','jsonb'),
        ('aap_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)','text')
    ) AS capability(signature,result_type)
    LOOP
        function_oid := pg_catalog.to_regprocedure(required.signature)::oid;
        IF function_oid IS NULL OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_proc AS function
            WHERE function.oid=function_oid
              AND (function.proowner IS DISTINCT FROM owner_oid
                   OR function.prorettype IS DISTINCT FROM pg_catalog.to_regtype(required.result_type)
                   OR NOT function.prosecdef
                   OR function.proconfig IS NULL
                   OR NOT (function.proconfig @> ARRAY[
                       'search_path=pg_catalog, pg_temp','TimeZone=UTC',
                       'DateStyle=ISO, YMD','extra_float_digits=3'
                   ]::text[]))
        ) THEN
            RAISE EXCEPTION 'Backend probe capability has unexpected ownership or settings';
        END IF;
    END LOOP;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"inference_backend_probe_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"inference_backend_probe_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('aap.inference_backend_probe_role');
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
GRANT USAGE ON SCHEMA aap_inference_backend_probe_v1 TO :"inference_backend_probe_role";
GRANT EXECUTE ON FUNCTION aap_inference_backend_probe_v1.backend_probe_policy(),
    aap_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)
    TO :"inference_backend_probe_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('aap.inference_backend_probe_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'aap_inference_backend_probe_v1.backend_probe_policy()',
        'aap_inference_backend_probe_v1.open_backend_probe_subject(text, uuid)'
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
        RAISE EXCEPTION 'Inference backend probe LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'aap_inference_backend_probe_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'aap_inference_backend_probe_v1', 'CREATE'
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
        RAISE EXCEPTION 'Inference backend probe LOGIN has non-function capabilities';
    END IF;
END $$;
COMMIT;
