\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.grant_section_applied', '0', true);

\if :{?grant_backend_probe}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.inference_owner', :'inference_owner', true);
SELECT set_config('yoao.inference_backend_probe_role', :'inference_backend_probe_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.inference_backend_probe_role');
    owner_name text := current_setting('yoao.inference_owner');
    candidate record;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_inference_backend_probe_client' OR role_name = owner_name THEN
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
    owner_oid oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.inference_owner'));
    required record;
    function_oid oid;
BEGIN
    IF (SELECT relowner FROM pg_catalog.pg_class WHERE oid=pg_catalog.to_regclass(
        'yoao_inference_backend_probe_v1.probe_secret')) IS DISTINCT FROM owner_oid THEN
        RAISE EXCEPTION 'Backend probe key has an unexpected owner';
    END IF;
    FOR required IN SELECT * FROM (VALUES
        ('yoao_inference_backend_probe_v1.backend_probe_policy()','jsonb'),
        ('yoao_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)','text')
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
    role_name text := current_setting('yoao.inference_backend_probe_role');
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
GRANT USAGE ON SCHEMA yoao_inference_backend_probe_v1 TO :"inference_backend_probe_role";
GRANT EXECUTE ON FUNCTION yoao_inference_backend_probe_v1.backend_probe_policy(),
    yoao_inference_backend_probe_v1.open_backend_probe_subject(text,uuid)
    TO :"inference_backend_probe_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.inference_backend_probe_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_inference_backend_probe_v1.backend_probe_policy()',
        'yoao_inference_backend_probe_v1.open_backend_probe_subject(text, uuid)'
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
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
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
            'yoao_inference_backend_probe_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_inference_backend_probe_v1', 'CREATE'
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
\endif

\if :{?grant_health_data}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.inference_owner', :'inference_owner', true);
SELECT set_config('yoao.inference_health_data_role', :'inference_health_data_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.inference_health_data_role');
    owner_name text := current_setting('yoao.inference_owner');
    candidate record;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_inference_health_data_client' OR role_name = owner_name THEN
        RAISE EXCEPTION 'Unexpected Inference health data roles';
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
            RAISE EXCEPTION 'Unsafe Inference health data roles';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS role ON role.oid IN (membership.member,membership.roleid)
        WHERE role.rolname = role_name
    ) THEN
        RAISE EXCEPTION 'Inference health data LOGIN must have no memberships';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.inference_owner'));
    required record;
    function_oid oid;
BEGIN
    FOR required IN SELECT * FROM (VALUES
        ('yoao_inference_health_data_v1.health_data_policy()','jsonb'),
        ('yoao_inference_health_data_v1.backend_row(uuid,boolean)','record'),
        ('yoao_inference_health_data_v1.membership_row(uuid,text,boolean)','record'),
        ('yoao_inference_health_data_v1.observation_clock()','timestamp with time zone'),
        ('yoao_inference_health_data_v1.lock_resource(text)','void'),
        ('yoao_inference_health_data_v1.try_promotion()','boolean'),
        ('yoao_inference_health_data_v1.write_backend(uuid,text,timestamp with time zone,text,boolean,boolean)','integer'),
        ('yoao_inference_health_data_v1.member_pool(uuid)','uuid'),
        ('yoao_inference_health_data_v1.pool_row(uuid,uuid)','record'),
        ('yoao_inference_health_data_v1.member_row(uuid)','record'),
        ('yoao_inference_health_data_v1.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text)','record')
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
            RAISE EXCEPTION 'Health data capability has unexpected ownership or settings';
        END IF;
    END LOOP;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"inference_health_data_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"inference_health_data_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.inference_health_data_role');
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
GRANT USAGE ON SCHEMA yoao_inference_health_data_v1 TO :"inference_health_data_role";
GRANT EXECUTE ON FUNCTION yoao_inference_health_data_v1.health_data_policy(),
    yoao_inference_health_data_v1.backend_row(uuid,boolean),
    yoao_inference_health_data_v1.membership_row(uuid,text,boolean),
    yoao_inference_health_data_v1.observation_clock(),
    yoao_inference_health_data_v1.lock_resource(text),
    yoao_inference_health_data_v1.try_promotion(),
    yoao_inference_health_data_v1.write_backend(uuid,text,timestamp with time zone,text,boolean,boolean),
    yoao_inference_health_data_v1.member_pool(uuid),
    yoao_inference_health_data_v1.pool_row(uuid,uuid),
    yoao_inference_health_data_v1.member_row(uuid),
    yoao_inference_health_data_v1.apply_reconciliation(uuid,uuid,text,text,text,bigint,text,text)
    TO :"inference_health_data_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.inference_health_data_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_inference_health_data_v1.apply_reconciliation(uuid, uuid, text, text, text, bigint, text, text)',
        'yoao_inference_health_data_v1.backend_row(uuid, boolean)',
        'yoao_inference_health_data_v1.health_data_policy()',
        'yoao_inference_health_data_v1.lock_resource(text)',
        'yoao_inference_health_data_v1.member_pool(uuid)',
        'yoao_inference_health_data_v1.member_row(uuid)',
        'yoao_inference_health_data_v1.membership_row(uuid, text, boolean)',
        'yoao_inference_health_data_v1.observation_clock()',
        'yoao_inference_health_data_v1.pool_row(uuid, uuid)',
        'yoao_inference_health_data_v1.try_promotion()',
        'yoao_inference_health_data_v1.write_backend(uuid, text, timestamp with time zone, text, boolean, boolean)'
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
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
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
        RAISE EXCEPTION 'Inference health data LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_inference_health_data_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_inference_health_data_v1', 'CREATE'
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
        RAISE EXCEPTION 'Inference health data LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_health_observation}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.inference_owner', :'inference_owner', true);
SELECT set_config('yoao.inference_health_observation_role', :'inference_health_observation_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.inference_health_observation_role');
    owner_name text := current_setting('yoao.inference_owner');
    candidate record;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_inference_health_observation_client' OR role_name = owner_name THEN
        RAISE EXCEPTION 'Unexpected Inference health observation roles';
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
            RAISE EXCEPTION 'Unsafe Inference health observation roles';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS role ON role.oid IN (membership.member,membership.roleid)
        WHERE role.rolname = role_name
    ) THEN
        RAISE EXCEPTION 'Inference health observation LOGIN must have no memberships';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.inference_owner'));
    required record;
    function_oid oid;
BEGIN
    IF (SELECT relowner FROM pg_catalog.pg_class WHERE oid=pg_catalog.to_regclass(
        'yoao_inference_health_observation_v1.observation_secret')) IS DISTINCT FROM owner_oid THEN
        RAISE EXCEPTION 'Health observation key has an unexpected owner';
    END IF;
    FOR required IN SELECT * FROM (VALUES
        ('yoao_inference_health_observation_v1.health_observation_policy()','jsonb'),
        ('yoao_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid)','text'),
        ('yoao_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text)','text')
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
            RAISE EXCEPTION 'Health observation capability has unexpected ownership or settings';
        END IF;
    END LOOP;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"inference_health_observation_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"inference_health_observation_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.inference_health_observation_role');
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
GRANT USAGE ON SCHEMA yoao_inference_health_observation_v1 TO :"inference_health_observation_role";
GRANT EXECUTE ON FUNCTION yoao_inference_health_observation_v1.health_observation_policy(),
    yoao_inference_health_observation_v1.open_health_observation_subject(text,uuid,uuid),
    yoao_inference_health_observation_v1.seal_health_observation(text,uuid,uuid,text)
    TO :"inference_health_observation_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.inference_health_observation_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_inference_health_observation_v1.health_observation_policy()',
        'yoao_inference_health_observation_v1.open_health_observation_subject(text, uuid, uuid)',
        'yoao_inference_health_observation_v1.seal_health_observation(text, uuid, uuid, text)'
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
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
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
        RAISE EXCEPTION 'Inference health observation LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_inference_health_observation_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_inference_health_observation_v1', 'CREATE'
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
        RAISE EXCEPTION 'Inference health observation LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_health_origin}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.inference_owner', :'inference_owner', true);
SELECT set_config('yoao.inference_health_origin_role', :'inference_health_origin_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.inference_health_origin_role');
    owner_name text := current_setting('yoao.inference_owner');
    candidate record;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_inference_health_origin_client' OR role_name = owner_name THEN
        RAISE EXCEPTION 'Unexpected Inference health origin roles';
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
            RAISE EXCEPTION 'Unsafe Inference health origin roles';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS role ON role.oid IN (membership.member,membership.roleid)
        WHERE role.rolname = role_name
    ) THEN
        RAISE EXCEPTION 'Inference health origin LOGIN must have no memberships';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.inference_owner'));
    required record;
    function_oid oid;
BEGIN
    IF (SELECT relowner FROM pg_catalog.pg_class WHERE oid=pg_catalog.to_regclass(
        'yoao_inference_health_observation_v1.observation_secret')) IS DISTINCT FROM owner_oid THEN
        RAISE EXCEPTION 'Health observation key has an unexpected owner';
    END IF;
    FOR required IN SELECT * FROM (VALUES
        ('yoao_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision)','text'),
        ('yoao_inference_health_observation_v1.health_observation_policy()','jsonb'),
        ('yoao_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text)','text')
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
            RAISE EXCEPTION 'Health observation capability has unexpected ownership or settings';
        END IF;
    END LOOP;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"inference_health_origin_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"inference_health_origin_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.inference_health_origin_role');
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
GRANT USAGE ON SCHEMA yoao_inference_health_observation_v1 TO :"inference_health_origin_role";
GRANT EXECUTE ON FUNCTION yoao_inference_health_observation_v1.capture_health_observation_subject(uuid,uuid,text,text,double precision),
    yoao_inference_health_observation_v1.health_observation_policy(),
    yoao_inference_health_observation_v1.verify_health_observation(text,uuid,uuid,text)
    TO :"inference_health_origin_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.inference_health_origin_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_inference_health_observation_v1.capture_health_observation_subject(uuid, uuid, text, text, double precision)',
        'yoao_inference_health_observation_v1.health_observation_policy()',
        'yoao_inference_health_observation_v1.verify_health_observation(text, uuid, uuid, text)'
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
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
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
        RAISE EXCEPTION 'Inference health origin LOGIN has unexpected functions: %',
            actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog', 'information_schema',
            'yoao_inference_health_observation_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_inference_health_observation_v1', 'CREATE'
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
        RAISE EXCEPTION 'Inference health origin LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_owner}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.inference_owner', :'inference_owner', true);
SELECT set_config('yoao.inference_client', :'client_role', true);
SELECT set_config('yoao.inference_profile', :'profile', true);

DO $profile$
DECLARE
    v_role name := current_setting('yoao.inference_client');
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=v_role);
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.inference_owner'));
    v_profile text := current_setting('yoao.inference_profile');
    v_schema text := 'yoao_inference_backend_endpoint_v1';
    v_signatures text[];
    v_allowed oid[];
    v_signature text;
    v_function oid;
    v_namespace record;
    v_relation record;
BEGIN
    IF current_database()<>current_setting('yoao.expected_database')
       OR current_user<>current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected Inference database or migration role';
    END IF;
    IF v_owner IS NULL OR v_client IS NULL OR v_owner=v_client
       OR current_user::pg_catalog.regrole=v_client
       OR NOT EXISTS (
           SELECT FROM pg_catalog.pg_roles WHERE oid=v_owner
             AND NOT rolcanlogin AND NOT rolinherit
             AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
       ) OR NOT EXISTS (
           SELECT FROM pg_catalog.pg_roles WHERE oid=v_client
             AND rolcanlogin AND NOT rolinherit
             AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
       ) OR EXISTS (
           SELECT FROM pg_catalog.pg_auth_members
           WHERE member=v_client OR roleid=v_client OR member=v_owner
              OR (roleid=v_owner AND member<>current_user::pg_catalog.regrole)
       ) THEN
        RAISE EXCEPTION 'Unsafe Inference owner/client role profile';
    END IF;
    IF v_profile IN ('setup','health_observation','catalog_fallback','stats',
                     'backend_read','lifecycle','execution') THEN
        v_signatures := ARRAY[
            v_schema||'.backend_endpoint_policy()',
            v_schema||'.open_'||v_profile||'_endpoint(text,uuid,uuid,text)'
        ];
    ELSIF v_profile IN ('gpu_health_observation','gpu_control') THEN
        v_signatures := ARRAY[
            v_schema||'.backend_endpoint_policy()',
            v_schema||'.capture_backend_endpoint_subject(uuid,uuid,text,text)',
            v_schema||'.open_'||CASE v_profile WHEN 'gpu_health_observation'
                THEN 'health_observation' ELSE 'gpu_control' END||'_endpoint(text,uuid,uuid,text)'
        ];
        IF v_profile='gpu_control' THEN
            v_signatures := pg_catalog.array_append(v_signatures,
                v_schema||'.open_installation_terminal_endpoint(uuid,uuid,text,text,text)');
        END IF;
    ELSIF v_profile='backend_stats' THEN
        v_signatures := ARRAY[
            v_schema||'.backend_endpoint_policy()',
            v_schema||'.capture_backend_endpoint_subject(uuid,uuid,text,text)',
            v_schema||'.stats_endpoint_rows()',
            v_schema||'.open_stats_endpoint(text,uuid,uuid,text)'
        ];
    ELSIF v_profile='gpu_authority' THEN
        v_schema := 'yoao_inference_gpu_v1';
        v_signatures := ARRAY[v_schema||'.gpu_authority_policy()',
                              v_schema||'.gpu_authority_step(text,text)'];
    ELSIF v_profile='gpu_collector' THEN
        IF v_role <> 'yoao_inference_gpu_collector_client' THEN
            RAISE EXCEPTION 'GPU collector requires its dedicated LOGIN';
        END IF;
        v_schema := 'yoao_inference_gpu_collector_v1';
        v_signatures := ARRAY[v_schema||'.gpu_collector_policy()',
                              v_schema||'.gpu_collector_step(text,text)'];
    ELSE
        RAISE EXCEPTION 'Unknown closed Inference owner profile';
    END IF;
    FOREACH v_signature IN ARRAY v_signatures LOOP
        v_function := pg_catalog.to_regprocedure(v_signature);
        IF v_function IS NULL OR NOT EXISTS (
            SELECT FROM pg_catalog.pg_proc WHERE oid=v_function AND proowner=v_owner
              AND prosecdef AND prokind='f'
              AND proconfig @> ARRAY['search_path=pg_catalog, pg_temp','TimeZone=UTC']::text[]
        ) THEN
            RAISE EXCEPTION 'Missing or changed Inference owner capability: %',v_signature;
        END IF;
        v_allowed := pg_catalog.array_append(v_allowed,v_function);
    END LOOP;
    EXECUTE pg_catalog.format('REVOKE ALL ON DATABASE %I FROM %I',current_database(),v_role);
    EXECUTE pg_catalog.format('GRANT CONNECT ON DATABASE %I TO %I',current_database(),v_role);
    FOR v_namespace IN
        SELECT nspname FROM pg_catalog.pg_namespace
        WHERE nspname NOT IN ('pg_catalog','information_schema')
          AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp_%'
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON SCHEMA %I FROM %I',v_namespace.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I',v_namespace.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I',v_namespace.nspname,v_role);
        EXECUTE pg_catalog.format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I',v_namespace.nspname,v_role);
    END LOOP;
    EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA %I TO %I',v_schema,v_role);
    FOREACH v_function IN ARRAY v_allowed LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',v_function::pg_catalog.regprocedure);
        EXECUTE pg_catalog.format('GRANT EXECUTE ON FUNCTION %s TO %I',v_function::pg_catalog.regprocedure,v_role);
    END LOOP;
    IF pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR EXISTS (
           SELECT FROM pg_catalog.pg_namespace
           WHERE nspname NOT IN ('pg_catalog','information_schema')
             AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp_%'
             AND (pg_catalog.has_schema_privilege(v_client,oid,'CREATE')
                  OR (nspname NOT IN ('public',v_schema)
                      AND pg_catalog.has_schema_privilege(v_client,oid,'USAGE')))
       ) OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Inference owner LOGIN has unsafe database/schema privileges';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc p
        JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_catalog.pg_type t ON t.oid=p.prorettype
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'
          AND p.prokind IN ('f','p','a','w')
          AND NOT (t.typnamespace='pg_catalog'::pg_catalog.regnamespace
                   AND t.typname IN ('trigger','event_trigger'))
          AND NOT EXISTS (
              SELECT FROM pg_catalog.pg_depend d
              WHERE d.classid='pg_catalog.pg_proc'::pg_catalog.regclass AND d.objid=p.oid
                AND d.refclassid='pg_catalog.pg_extension'::pg_catalog.regclass AND d.deptype='e'
          ) AND (pg_catalog.left(n.nspname,4)='aap_' OR pg_catalog.left(n.nspname,5)='yoao_' OR p.prosecdef OR EXISTS (
              SELECT FROM pg_catalog.aclexplode(COALESCE(p.proacl,pg_catalog.acldefault('f',p.proowner))) a
              WHERE a.privilege_type='EXECUTE' AND a.grantee<>0
                AND pg_catalog.pg_has_role(v_client,a.grantee,'USAGE')
          )) AND NOT (p.oid=ANY(v_allowed))
          AND pg_catalog.has_function_privilege(v_client,p.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Inference owner LOGIN can execute an unrelated function';
    END IF;
    FOR v_relation IN
        SELECT c.oid,c.relkind FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
          AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'
          AND c.relkind IN ('r','p','v','m','f','S')
    LOOP
        IF (v_relation.relkind<>'S' AND (
                pg_catalog.has_table_privilege(v_client,v_relation.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR pg_catalog.has_any_column_privilege(v_client,v_relation.oid,'SELECT,INSERT,UPDATE,REFERENCES')
            )) OR (v_relation.relkind='S' AND
                pg_catalog.has_sequence_privilege(v_client,v_relation.oid,'SELECT,UPDATE,USAGE')) THEN
            RAISE EXCEPTION 'Inference owner LOGIN has direct relation privileges';
        END IF;
    END LOOP;
    IF EXISTS (SELECT FROM pg_catalog.pg_class WHERE relowner=v_client)
       OR EXISTS (SELECT FROM pg_catalog.pg_namespace WHERE nspowner=v_client)
       OR EXISTS (SELECT FROM pg_catalog.pg_database WHERE datdba=v_client)
       OR EXISTS (SELECT FROM pg_catalog.pg_proc WHERE proowner=v_client) THEN
        RAISE EXCEPTION 'Inference owner LOGIN owns database objects';
    END IF;
END;
$profile$;
\endif

\if :{?grant_pipelines}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.inference_owner', :'inference_owner', true);
SELECT set_config('yoao.workspace_owner', :'workspace_owner', true);
SELECT set_config(
    'yoao.inference_pipelines_role', :'inference_pipelines_role', true
);

DO $$
DECLARE
    role_name text := current_setting('yoao.inference_pipelines_role');
    role_names text[] := ARRAY[
        current_setting('yoao.identity_owner'),
        current_setting('yoao.inference_owner'),
        current_setting('yoao.workspace_owner'),
        current_setting('yoao.inference_pipelines_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_inference_pipelines_client'
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
        WHERE rolname = current_setting('yoao.inference_owner')
    );
    policy_oid oid := pg_catalog.to_regprocedure(
        'yoao_inference_pipelines_v1.pipeline_policy()'
    )::oid;
    step_oid oid := pg_catalog.to_regprocedure(
        'yoao_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[])'
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
            ('yoao_workspace_v1.project_access_step(uuid,uuid,text)',
             current_setting('yoao.workspace_owner')),
            ('yoao_workspace_project_query_v1.project_query_step(jsonb,text,jsonb,text,text,text[],text[],uuid,text,timestamptz,timestamptz,bigint,integer)',
             current_setting('yoao.workspace_owner')),
            ('yoao_identity_v1.workspace_template_query_org_ids(uuid)',
             current_setting('yoao.identity_owner'))
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
        inference_oid,'yoao_inference_pipelines_v1','USAGE'
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
    role_name text := current_setting('yoao.inference_pipelines_role');
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

GRANT USAGE ON SCHEMA yoao_inference_pipelines_v1
    TO :"inference_pipelines_role";
GRANT EXECUTE ON FUNCTION
    yoao_inference_pipelines_v1.pipeline_policy(),
    yoao_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[])
    TO :"inference_pipelines_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.inference_pipelines_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_inference_pipelines_v1.pipeline_policy()',
        'yoao_inference_pipelines_v1.pipeline_step(jsonb, text, jsonb, text[], text[])'
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
          pg_catalog.left(namespace.nspname, 4) = 'aap_' OR pg_catalog.left(namespace.nspname, 5) = 'yoao_'
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
            'yoao_inference_pipelines_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_inference_pipelines_v1', 'CREATE'
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
\endif

\if :{?grant_pool_reference}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.inference_owner', :'inference_owner', true);
SELECT set_config('yoao.pool_reference_role', :'pool_reference_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.inference_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.pool_reference_role'));
    v_function oid;
    v_signature text;
BEGIN
    IF current_database()<>current_setting('yoao.expected_database')
       OR current_user<>current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF v_owner IS NULL OR v_client IS NULL
       OR v_owner=v_client
       OR current_user::pg_catalog.regrole=v_client THEN
        RAISE EXCEPTION 'Inference Pool reference data roles are missing or overlap';
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles AS role
        WHERE role.oid=v_owner AND NOT role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles AS role
        WHERE role.oid=v_client AND role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Unsafe Inference Pool reference data role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Inference owner and caller LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_inference_v1.ai_mask_pool_reference_row(uuid)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='s'
          AND function.prokind='f'
          AND function.proretset
          AND function.prorettype='record'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Inference Pool reference data command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"pool_reference_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"pool_reference_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.pool_reference_role');
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

REVOKE ALL ON FUNCTION yoao_inference_v1.ai_mask_pool_reference_row(uuid)
    FROM :"pool_reference_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_inference_v1 TO :"pool_reference_role";
GRANT EXECUTE ON FUNCTION yoao_inference_v1.ai_mask_pool_reference_row(uuid)
    TO :"pool_reference_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.pool_reference_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_inference_v1.ai_mask_pool_reference_row(uuid)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_inference_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_inference_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Inference Pool reference data LOGIN is missing its exact one-function profile';
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
          AND NOT (return_type.typnamespace='pg_catalog'::pg_catalog.regnamespace
              AND return_type.typname IN ('trigger','event_trigger'))
          AND NOT EXISTS (
              SELECT FROM pg_catalog.pg_depend AS dependency
              WHERE dependency.classid='pg_catalog.pg_proc'::pg_catalog.regclass
                AND dependency.objid=function.oid
                AND dependency.refclassid='pg_catalog.pg_extension'::pg_catalog.regclass
                AND dependency.deptype='e'
          )
          AND (pg_catalog.left(namespace.nspname,4)='aap_' OR pg_catalog.left(namespace.nspname,5)='yoao_'
               OR function.prosecdef
               OR EXISTS (
                   SELECT FROM pg_catalog.aclexplode(COALESCE(function.proacl,
                       pg_catalog.acldefault('f',function.proowner))) AS acl
                   WHERE acl.privilege_type='EXECUTE' AND acl.grantee<>0
                     AND pg_catalog.pg_has_role(v_client,acl.grantee,'USAGE')
               ))
          AND NOT (function.oid=ANY(v_allowed))
          AND pg_catalog.has_function_privilege(v_client,function.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Inference Pool reference data LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_inference_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Inference Pool reference data LOGIN has foreign schema privileges';
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
                pg_catalog.has_table_privilege(v_client,v_relation.oid,
                    'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR pg_catalog.has_any_column_privilege(v_client,v_relation.oid,
                    'SELECT,INSERT,UPDATE,REFERENCES')
            )) OR (v_relation.relkind = 'S' AND
                pg_catalog.has_sequence_privilege(v_client,v_relation.oid,
                    'SELECT,UPDATE,USAGE')) THEN
            RAISE EXCEPTION 'Inference Pool reference data LOGIN has direct data privileges';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.relowner=v_client
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspowner=v_client
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_database AS database
        WHERE database.datdba=v_client
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.proowner=v_client
    ) THEN
        RAISE EXCEPTION 'Inference Pool reference data LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Inference Pool reference data command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_registry_ingress}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.inference_owner', :'inference_owner', true);
SELECT set_config('yoao.inference_registry_ingress_role', :'inference_registry_ingress_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.inference_registry_ingress_role');
    owner_name text := current_setting('yoao.inference_owner');
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_inference_registry_ingress_client'
       OR owner_name <> 'inference_code'
       OR role_name = owner_name THEN
        RAISE EXCEPTION 'Unexpected Registry ingress roles';
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
        RAISE EXCEPTION 'Unsafe Registry ingress roles';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS role
          ON role.oid IN (membership.member, membership.roleid)
        WHERE role.rolname = role_name
    ) THEN
        RAISE EXCEPTION 'Registry ingress LOGIN must have no memberships';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (
        SELECT oid FROM pg_catalog.pg_roles
        WHERE rolname = current_setting('yoao.inference_owner')
    );
    required record;
    function_oid oid;
BEGIN
    IF (SELECT nspowner FROM pg_catalog.pg_namespace
        WHERE nspname = 'yoao_inference_registry_ingress_v1') IS DISTINCT FROM owner_oid
       OR (SELECT relowner FROM pg_catalog.pg_class AS relation
           JOIN pg_catalog.pg_namespace AS namespace
             ON namespace.oid = relation.relnamespace
           WHERE namespace.nspname = 'yoao_inference_registry_ingress_v1'
             AND relation.relname = 'env_sync_receipts'
             AND relation.relkind = 'r') IS DISTINCT FROM owner_oid THEN
        RAISE EXCEPTION 'Registry ingress private objects have unexpected owners';
    END IF;
    FOR required IN SELECT * FROM (VALUES
        ('yoao_inference_registry_ingress_v1.registry_ingress_policy()','jsonb'),
        ('yoao_inference_registry_ingress_v1.registry_by_url(text)','record'),
        ('yoao_inference_registry_ingress_v1.registry_candidates(text)','record'),
        ('yoao_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text)','record'),
        ('yoao_inference_registry_ingress_v1.singleton_pool(uuid)','record'),
        ('yoao_inference_registry_ingress_v1.create_singleton_pool(uuid,uuid,uuid,text,text,text)','record'),
        ('yoao_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text)','timestamp with time zone'),
        ('yoao_inference_registry_ingress_v1.disconnect_env_registry(uuid,text)','timestamp with time zone'),
        ('yoao_inference_registry_ingress_v1.tag_seed_registry(uuid,text)','record'),
        ('yoao_inference_registry_ingress_v1.sync_result(uuid,text)','jsonb'),
        ('yoao_inference_registry_ingress_v1.record_sync_result(uuid,text,integer)','jsonb')
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
            RAISE EXCEPTION 'Registry ingress capability has unexpected ownership or settings';
        END IF;
    END LOOP;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"inference_registry_ingress_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"inference_registry_ingress_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.inference_registry_ingress_role');
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
GRANT USAGE ON SCHEMA yoao_inference_registry_ingress_v1
    TO :"inference_registry_ingress_role";
GRANT EXECUTE ON FUNCTION
    yoao_inference_registry_ingress_v1.registry_ingress_policy(),
    yoao_inference_registry_ingress_v1.registry_by_url(text),
    yoao_inference_registry_ingress_v1.registry_candidates(text),
    yoao_inference_registry_ingress_v1.create_registry(uuid,text,text,text,boolean,text),
    yoao_inference_registry_ingress_v1.singleton_pool(uuid),
    yoao_inference_registry_ingress_v1.create_singleton_pool(uuid,uuid,uuid,text,text,text),
    yoao_inference_registry_ingress_v1.apply_observation(uuid,text,text,text,boolean,text,timestamptz,text,boolean,text),
    yoao_inference_registry_ingress_v1.disconnect_env_registry(uuid,text),
    yoao_inference_registry_ingress_v1.tag_seed_registry(uuid,text),
    yoao_inference_registry_ingress_v1.sync_result(uuid,text),
    yoao_inference_registry_ingress_v1.record_sync_result(uuid,text,integer)
    TO :"inference_registry_ingress_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.inference_registry_ingress_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_inference_registry_ingress_v1.apply_observation(uuid, text, text, text, boolean, text, timestamp with time zone, text, boolean, text)',
        'yoao_inference_registry_ingress_v1.create_registry(uuid, text, text, text, boolean, text)',
        'yoao_inference_registry_ingress_v1.create_singleton_pool(uuid, uuid, uuid, text, text, text)',
        'yoao_inference_registry_ingress_v1.disconnect_env_registry(uuid, text)',
        'yoao_inference_registry_ingress_v1.record_sync_result(uuid, text, integer)',
        'yoao_inference_registry_ingress_v1.registry_by_url(text)',
        'yoao_inference_registry_ingress_v1.registry_candidates(text)',
        'yoao_inference_registry_ingress_v1.registry_ingress_policy()',
        'yoao_inference_registry_ingress_v1.singleton_pool(uuid)',
        'yoao_inference_registry_ingress_v1.sync_result(uuid, text)',
        'yoao_inference_registry_ingress_v1.tag_seed_registry(uuid, text)'
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
          pg_catalog.left(namespace.nspname,4) = 'aap_' OR pg_catalog.left(namespace.nspname,5) = 'yoao_'
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
        RAISE EXCEPTION 'Registry ingress LOGIN has unexpected functions: %',actual_functions;
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog','information_schema','yoao_inference_registry_ingress_v1','public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid,namespace.oid,'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid,namespace.oid,'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid,'yoao_inference_registry_ingress_v1','CREATE'
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
        RAISE EXCEPTION 'Registry ingress LOGIN has non-function capabilities';
    END IF;
END $$;
\endif

\if :{?grant_routing}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.inference_owner', :'inference_owner', true);
SELECT set_config('yoao.inference_routing_role', :'inference_routing_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.inference_routing_role');
    owner_name text := current_setting('yoao.inference_owner');
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_inference_routing_client'
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
        WHERE rolname = current_setting('yoao.inference_owner')
    );
    actual_tables text[];
    expected_tables text[] := ARRAY[
        'attempts', 'authority_secret', 'command_receipts', 'control_attempts',
        'controls', 'fixture_prediction_test_scopes', 'plugin_replacement_bindings',
        'plugin_terminal_update_guard', 'prediction_execution_scopes',
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
        WHERE nspname = 'yoao_inference_routing_v1') IS DISTINCT FROM owner_oid THEN
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
     WHERE namespace.nspname = 'yoao_inference_routing_v1'
       AND relation.relkind IN ('r', 'p');
    IF actual_tables IS DISTINCT FROM expected_tables OR EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'yoao_inference_routing_v1'
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
      WHERE namespace.nspname = 'yoao_inference_routing_v1'
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
        ('yoao_inference_routing_v1.model_dispatch_step(text)','jsonb'),
        ('yoao_inference_routing_v1.prediction_scope_reserve(text)','jsonb'),
        ('yoao_inference_routing_v1.prediction_test_scope_reserve(text)','jsonb'),
        ('yoao_inference_routing_v1.routing_policy()','jsonb'),
        ('yoao_inference_routing_v1.routing_step(text,text)','jsonb'),
        ('yoao_inference_routing_v1.scope_retirement_step(text,text)','jsonb'),
        ('yoao_inference_routing_v1.supersede_unbegun_scope(text)','jsonb'),
        ('yoao_inference_routing_v1.video_execution_scope_step(text,text)','jsonb')
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
    role_name text := current_setting('yoao.inference_routing_role');
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
GRANT USAGE ON SCHEMA yoao_inference_routing_v1
    TO :"inference_routing_role";
GRANT EXECUTE ON FUNCTION
    yoao_inference_routing_v1.model_dispatch_step(text),
    yoao_inference_routing_v1.prediction_scope_reserve(text),
    yoao_inference_routing_v1.prediction_test_scope_reserve(text),
    yoao_inference_routing_v1.routing_policy(),
    yoao_inference_routing_v1.routing_step(text,text),
    yoao_inference_routing_v1.scope_retirement_step(text,text),
    yoao_inference_routing_v1.supersede_unbegun_scope(text),
    yoao_inference_routing_v1.video_execution_scope_step(text,text)
    TO :"inference_routing_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.inference_routing_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_inference_routing_v1.model_dispatch_step(text)',
        'yoao_inference_routing_v1.prediction_scope_reserve(text)',
        'yoao_inference_routing_v1.prediction_test_scope_reserve(text)',
        'yoao_inference_routing_v1.routing_policy()',
        'yoao_inference_routing_v1.routing_step(text, text)',
        'yoao_inference_routing_v1.scope_retirement_step(text, text)',
        'yoao_inference_routing_v1.supersede_unbegun_scope(text)',
        'yoao_inference_routing_v1.video_execution_scope_step(text, text)'
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
          pg_catalog.left(namespace.nspname,4) = 'aap_' OR pg_catalog.left(namespace.nspname,5) = 'yoao_'
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
            'yoao_inference_routing_v1._routing_step_0279(text,text)'
        ),
        'EXECUTE'
    ) OR pg_catalog.has_function_privilege(
        role_oid,
        pg_catalog.to_regprocedure(
            'yoao_inference_routing_v1._scope_retirement_step_0280(text,text)'
        ),
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Routing LOGIN must not call private legacy dispatchers';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN (
            'pg_catalog','information_schema','yoao_inference_routing_v1','public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(role_oid,namespace.oid,'USAGE')
               OR pg_catalog.has_schema_privilege(role_oid,namespace.oid,'CREATE'))
    ) OR pg_catalog.has_schema_privilege(
        role_oid,'yoao_inference_routing_v1','CREATE'
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
\endif

\if :{?grant_video_restore}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.inference_owner', :'inference_owner', true);
SELECT set_config('yoao.video_restore_operator_role', :'video_restore_operator_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.video_restore_operator_role');
    owner_name text := current_setting('yoao.inference_owner');
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_video_restore_operator_client' OR owner_name <> 'inference_code'
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
        WHERE rolname = current_setting('yoao.inference_owner')
    );
    function_oid oid;
    signature text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_inference_routing_v1'
    ) OR NOT pg_catalog.has_schema_privilege(
        owner_oid, 'yoao_inference_routing_v1', 'USAGE'
    ) THEN
        RAISE EXCEPTION 'Video restore operator Native schema or function is unavailable';
    END IF;
    FOREACH signature IN ARRAY ARRAY[
        'yoao_inference_routing_v1.video_restore_step(text,text)'
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
    role_name text := current_setting('yoao.video_restore_operator_role');
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
GRANT USAGE ON SCHEMA yoao_inference_routing_v1 TO :"video_restore_operator_role";
GRANT EXECUTE ON FUNCTION yoao_inference_routing_v1.video_restore_step(text,text)
    TO :"video_restore_operator_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.video_restore_operator_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_inference_routing_v1.video_restore_step(text, text)'
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
          pg_catalog.left(namespace.nspname,4) = 'aap_' OR pg_catalog.left(namespace.nspname,5) = 'yoao_'
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
\endif

DO $$
BEGIN
    IF current_setting('yoao.grant_section_applied', true) IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION 'No capability section selected; pass -v grant_<capability>=1';
    END IF;
END $$;
COMMIT;
