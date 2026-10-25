-- Grant a Labeling Mask QC and repair LOGIN exactly three owner commands.
-- Run after migrations, module-data-grants.sql and lan-production-grants.sql.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v labeling_owner=labeling_code -v quality_role=labeling_mask_quality_client \
--   -f infra/docker/labeling-mask-quality-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config('aap.quality_role', :'quality_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('aap.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('aap.quality_role'));
    v_function oid;
    v_signature text;
BEGIN
    IF current_database()<>current_setting('aap.expected_database')
       OR current_user<>current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF v_owner IS NULL OR v_client IS NULL
       OR v_owner=v_client
       OR current_user::pg_catalog.regrole=v_client THEN
        RAISE EXCEPTION 'Labeling Mask QC and repair roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling Mask QC and repair role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and Mask QC LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'aap_labeling_v1.mask_qc_command(jsonb)',
        'aap_labeling_v1.mask_repair_command(jsonb)',
        'aap_labeling_v1.raster_mask_retention_query(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile=CASE
              WHEN v_signature='aap_labeling_v1.raster_mask_retention_query(jsonb)' THEN 's'
              ELSE 'v'
          END
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling Mask QC and repair command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"quality_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"quality_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('aap.quality_role');
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

REVOKE ALL ON FUNCTION aap_labeling_v1.mask_qc_command(jsonb),
    aap_labeling_v1.mask_repair_command(jsonb),
    aap_labeling_v1.raster_mask_retention_query(jsonb)
    FROM :"quality_role", PUBLIC;
GRANT USAGE ON SCHEMA aap_labeling_v1 TO :"quality_role";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.mask_qc_command(jsonb),
    aap_labeling_v1.mask_repair_command(jsonb),
    aap_labeling_v1.raster_mask_retention_query(jsonb)
    TO :"quality_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('aap.quality_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('aap_labeling_v1.mask_qc_command(jsonb)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.mask_repair_command(jsonb)'),
        pg_catalog.to_regprocedure('aap_labeling_v1.raster_mask_retention_query(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'aap_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'aap_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling Mask QC and repair LOGIN is missing its exact three-function profile';
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
          AND (pg_catalog.left(namespace.nspname,4)='aap_'
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
        RAISE EXCEPTION 'Labeling Mask QC and repair LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','aap_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling Mask QC and repair LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling Mask QC and repair LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling Mask QC and repair LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling Mask QC and repair command is executable by PUBLIC';
    END IF;
END;
$verify$;
COMMIT;
