\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.grant_section_applied', '0', true);

\if :{?grant_annotation_maintenance}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.annotation_maintenance_role', :'annotation_maintenance_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.annotation_maintenance_role'));
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
        RAISE EXCEPTION 'Labeling Annotation maintenance roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling Annotation maintenance role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and batch control LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.annotation_maintenance_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling Annotation maintenance command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"annotation_maintenance_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"annotation_maintenance_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.annotation_maintenance_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.annotation_maintenance_command(jsonb)
    FROM :"annotation_maintenance_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"annotation_maintenance_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.annotation_maintenance_command(jsonb)
    TO :"annotation_maintenance_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.annotation_maintenance_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.annotation_maintenance_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling Annotation maintenance LOGIN is missing its exact one-function profile';
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
        RAISE EXCEPTION 'Labeling Annotation maintenance LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling Annotation maintenance LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling Annotation maintenance LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling Annotation maintenance LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling Annotation maintenance command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_annotation_transfer}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.annotation_transfer_role', :'annotation_transfer_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.annotation_transfer_role'));
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
        RAISE EXCEPTION 'Labeling annotation transfer data roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling annotation transfer data role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and caller LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.annotation_transfer_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling annotation transfer data command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"annotation_transfer_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"annotation_transfer_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.annotation_transfer_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.annotation_transfer_command(jsonb)
    FROM :"annotation_transfer_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"annotation_transfer_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.annotation_transfer_command(jsonb)
    TO :"annotation_transfer_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.annotation_transfer_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.annotation_transfer_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling annotation transfer data LOGIN is missing its exact one-function profile';
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
        RAISE EXCEPTION 'Labeling annotation transfer data LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling annotation transfer data LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling annotation transfer data LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling annotation transfer data LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling annotation transfer data command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_annotation_writes}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.annotation_write_role', :'annotation_write_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.annotation_write_role'));
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
        RAISE EXCEPTION 'Labeling annotation writes roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling annotation writes role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and annotation writes LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.annotation_statistics_command(jsonb)',
        'yoao_labeling_v1.scene_track_domain_command(jsonb)',
        'yoao_labeling_v1.annotation_write_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling annotation writes command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"annotation_write_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"annotation_write_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.annotation_write_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.annotation_statistics_command(jsonb),
    yoao_labeling_v1.scene_track_domain_command(jsonb),
    yoao_labeling_v1.annotation_write_command(jsonb)
    FROM :"annotation_write_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"annotation_write_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.annotation_statistics_command(jsonb),
    yoao_labeling_v1.scene_track_domain_command(jsonb),
    yoao_labeling_v1.annotation_write_command(jsonb)
    TO :"annotation_write_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.annotation_write_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.annotation_statistics_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.scene_track_domain_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.annotation_write_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling annotation writes LOGIN is missing its exact three-function profile';
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
        RAISE EXCEPTION 'Labeling annotation writes LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling annotation writes LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling annotation writes LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling annotation writes LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling annotation writes command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_batch_control}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.batch_control_role', :'batch_control_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.batch_control_role'));
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
        RAISE EXCEPTION 'Labeling batch control roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling batch control role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and batch control LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.batch_control_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling batch control command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"batch_control_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"batch_control_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.batch_control_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.batch_control_command(jsonb)
    FROM :"batch_control_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"batch_control_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.batch_control_command(jsonb)
    TO :"batch_control_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.batch_control_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.batch_control_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling batch control LOGIN is missing its exact one-function profile';
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
        RAISE EXCEPTION 'Labeling batch control LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling batch control LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling batch control LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling batch control LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling batch control command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_class_query}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config(
    'yoao.labeling_class_query_role', :'labeling_class_query_role', true
);

DO $$
DECLARE
    client_name text := current_setting('yoao.labeling_class_query_role');
    role_names text[] := ARRAY[
        current_setting('yoao.labeling_owner'),
        current_setting('yoao.labeling_class_query_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF client_name <> 'yoao_labeling_class_query_client'
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
        WHERE role.rolname = current_setting('yoao.labeling_owner')
    );
    policy_oid oid := pg_catalog.to_regprocedure(
        'yoao_labeling_class_query_v1.class_query_policy()'
    )::oid;
    classes_oid oid := pg_catalog.to_regprocedure(
        'yoao_labeling_class_query_v1.class_counts(uuid)'
    )::oid;
    attributes_oid oid := pg_catalog.to_regprocedure(
        'yoao_labeling_class_query_v1.attribute_rows(uuid)'
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
        labeling_oid, 'yoao_labeling_class_query_v1', 'USAGE'
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
    IF yoao_labeling_class_query_v1.class_query_policy()
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
    role_name text := current_setting('yoao.labeling_class_query_role');
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

GRANT USAGE ON SCHEMA yoao_labeling_class_query_v1
    TO :"labeling_class_query_role";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_class_query_v1.class_query_policy(),
    yoao_labeling_class_query_v1.class_counts(uuid),
    yoao_labeling_class_query_v1.attribute_rows(uuid)
    TO :"labeling_class_query_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.labeling_class_query_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_labeling_class_query_v1.attribute_rows(uuid)',
        'yoao_labeling_class_query_v1.class_counts(uuid)',
        'yoao_labeling_class_query_v1.class_query_policy()'
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
            'yoao_labeling_class_query_v1', 'public'
        )
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (
              pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'USAGE')
              OR pg_catalog.has_schema_privilege(role_oid, namespace.oid, 'CREATE')
          )
    ) OR pg_catalog.has_schema_privilege(
        role_oid, 'yoao_labeling_class_query_v1', 'CREATE'
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
\endif

\if :{?grant_core_read}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.core_read_role', :'core_read_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.core_read_role'));
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
        RAISE EXCEPTION 'Labeling core read data roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling core read data role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and caller LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid)',
        'yoao_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer)',
        'yoao_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(uuid[],integer)',
        'yoao_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean)',
        'yoao_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean)',
        'yoao_labeling_v1.lock_video_segment_for_core_read(uuid,uuid)',
        'yoao_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid)',
        'yoao_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid)',
        'yoao_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid)',
        'yoao_labeling_v1.lock_prediction_rows_for_core_read_share(uuid,uuid[],integer)',
        'yoao_labeling_v1.lock_scene_track_rows_for_core_read(uuid[],integer)',
        'yoao_labeling_v1.lock_scene_track_interval_rows_for_core_read_share(uuid[],integer)',
        'yoao_labeling_v1.lock_prediction_meta_for_core_read_share(uuid)',
        'yoao_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid)',
        'yoao_labeling_v1.lock_video_segment_ids_for_export_share(uuid[],integer)',
        'yoao_labeling_v1.lock_video_quality_run_ids_for_export_share(uuid[],integer)',
        'yoao_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[])',
        'yoao_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.proretset
          AND function.prorettype = CASE v_signature
              WHEN 'yoao_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid)' THEN 'yoao_labeling_v1.task_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer)' THEN 'yoao_labeling_v1.task_dataset_item_link_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(uuid[],integer)' THEN 'yoao_labeling_v1.task_dataset_item_link_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean)' THEN 'yoao_labeling_v1.batch_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean)' THEN 'yoao_labeling_v1.annotation_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_video_segment_for_core_read(uuid,uuid)' THEN 'yoao_labeling_v1.video_segment_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid)' THEN 'yoao_labeling_v1.task_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid)' THEN 'yoao_labeling_v1.task_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid)' THEN 'yoao_labeling_v1.prediction_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_prediction_rows_for_core_read_share(uuid,uuid[],integer)' THEN 'yoao_labeling_v1.prediction_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_scene_track_rows_for_core_read(uuid[],integer)' THEN 'yoao_labeling_v1.scene_track_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_scene_track_interval_rows_for_core_read_share(uuid[],integer)' THEN 'yoao_labeling_v1.scene_track_interval_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_prediction_meta_for_core_read_share(uuid)' THEN 'yoao_labeling_v1.prediction_meta_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid)' THEN 'yoao_labeling_v1.video_segment_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_video_segment_ids_for_export_share(uuid[],integer)' THEN 'uuid'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_video_quality_run_ids_for_export_share(uuid[],integer)' THEN 'uuid'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[])' THEN 'yoao_labeling_v1.task_rows'::pg_catalog.regtype
              WHEN 'yoao_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])' THEN 'yoao_labeling_v1.annotation_rows'::pg_catalog.regtype
          END
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling core read data command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"core_read_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"core_read_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.core_read_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid),
    yoao_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer),
    yoao_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(uuid[],integer),
    yoao_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean),
    yoao_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean),
    yoao_labeling_v1.lock_video_segment_for_core_read(uuid,uuid),
    yoao_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid),
    yoao_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid),
    yoao_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid),
    yoao_labeling_v1.lock_prediction_rows_for_core_read_share(uuid,uuid[],integer),
    yoao_labeling_v1.lock_scene_track_rows_for_core_read(uuid[],integer),
    yoao_labeling_v1.lock_scene_track_interval_rows_for_core_read_share(uuid[],integer),
    yoao_labeling_v1.lock_prediction_meta_for_core_read_share(uuid),
    yoao_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid),
    yoao_labeling_v1.lock_video_segment_ids_for_export_share(uuid[],integer),
    yoao_labeling_v1.lock_video_quality_run_ids_for_export_share(uuid[],integer),
    yoao_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[]),
    yoao_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])
    FROM :"core_read_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"core_read_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid),
    yoao_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer),
    yoao_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(uuid[],integer),
    yoao_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean),
    yoao_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean),
    yoao_labeling_v1.lock_video_segment_for_core_read(uuid,uuid),
    yoao_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid),
    yoao_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid),
    yoao_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid),
    yoao_labeling_v1.lock_prediction_rows_for_core_read_share(uuid,uuid[],integer),
    yoao_labeling_v1.lock_scene_track_rows_for_core_read(uuid[],integer),
    yoao_labeling_v1.lock_scene_track_interval_rows_for_core_read_share(uuid[],integer),
    yoao_labeling_v1.lock_prediction_meta_for_core_read_share(uuid),
    yoao_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid),
    yoao_labeling_v1.lock_video_segment_ids_for_export_share(uuid[],integer),
    yoao_labeling_v1.lock_video_quality_run_ids_for_export_share(uuid[],integer),
    yoao_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[]),
    yoao_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])
    TO :"core_read_role";
GRANT SELECT ON yoao_labeling_v1.task_rows,
    yoao_labeling_v1.batch_rows,
    yoao_labeling_v1.annotation_rows,
    yoao_labeling_v1.prediction_rows,
    yoao_labeling_v1.prediction_meta_rows,
    yoao_labeling_v1.failed_prediction_rows,
    yoao_labeling_v1.task_lock_rows,
    yoao_labeling_v1.task_dataset_item_link_rows,
    yoao_labeling_v1.scene_track_rows,
    yoao_labeling_v1.scene_track_interval_rows,
    yoao_labeling_v1.video_segment_rows,
    yoao_labeling_v1.video_tracker_job_rows,
    yoao_labeling_v1.video_track_quality_run_rows,
    yoao_labeling_v1.annotation_draft_rows
    TO :"core_read_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.core_read_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_task_for_core_read(uuid,text,boolean,uuid)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_task_dataset_item_links_for_core_read(uuid,boolean,uuid,integer)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_task_dataset_item_link_rows_for_core_read_share(uuid[],integer)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_batch_rows_for_core_read(uuid[],uuid,text,boolean)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_annotation_rows_for_core_read(uuid[],uuid,uuid,text,boolean)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_video_segment_for_core_read(uuid,uuid)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_task_rows_for_core_read(uuid[],uuid,uuid)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_task_rows_for_core_read_share(uuid[],uuid,uuid)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_prediction_for_core_read_share(uuid,uuid)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_prediction_rows_for_core_read_share(uuid,uuid[],integer)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_scene_track_rows_for_core_read(uuid[],integer)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_scene_track_interval_rows_for_core_read_share(uuid[],integer)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_prediction_meta_for_core_read_share(uuid)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_video_segment_for_core_read_share(uuid,uuid)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_video_segment_ids_for_export_share(uuid[],integer)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_video_quality_run_ids_for_export_share(uuid[],integer)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_active_learning_feedback_task_rows(uuid,uuid[])'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.lock_active_learning_feedback_annotation_rows(uuid[])')
    ];
    v_allowed_relations oid[] := ARRAY[
        pg_catalog.to_regclass('yoao_labeling_v1.task_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.batch_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.annotation_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.prediction_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.prediction_meta_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.failed_prediction_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.task_lock_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.task_dataset_item_link_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.scene_track_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.scene_track_interval_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.video_segment_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.video_tracker_job_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.video_track_quality_run_rows'),
        pg_catalog.to_regclass('yoao_labeling_v1.annotation_draft_rows')
    ];
    v_projection oid;
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling core read data LOGIN is missing its exact seven-function profile';
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
        RAISE EXCEPTION 'Labeling core read data LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling core read data LOGIN has foreign schema privileges';
    END IF;
    IF pg_catalog.array_position(v_allowed_relations,NULL) IS NOT NULL THEN
        RAISE EXCEPTION 'Labeling core read public projections are missing';
    END IF;
    FOREACH v_projection IN ARRAY v_allowed_relations LOOP
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_class WHERE oid=v_projection AND relkind='v')
           OR NOT pg_catalog.has_table_privilege(v_client,v_projection,'SELECT')
           OR pg_catalog.has_table_privilege(v_client,v_projection,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_client,v_projection,'INSERT,UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'Labeling core read public projection is not SELECT-only';
        END IF;
    END LOOP;
    FOR v_relation IN
        SELECT relation.oid, relation.relkind
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r','p','v','m','f','S')
          AND NOT (relation.oid=ANY(v_allowed_relations))
    LOOP
        IF (v_relation.relkind <> 'S' AND (
                pg_catalog.has_table_privilege(v_client,v_relation.oid,
                    'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR pg_catalog.has_any_column_privilege(v_client,v_relation.oid,
                    'SELECT,INSERT,UPDATE,REFERENCES')
            )) OR (v_relation.relkind = 'S' AND
                pg_catalog.has_sequence_privilege(v_client,v_relation.oid,
                    'SELECT,UPDATE,USAGE')) THEN
            RAISE EXCEPTION 'Labeling core read data LOGIN has data privileges outside its public projections';
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
        RAISE EXCEPTION 'Labeling core read data LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling core read data command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_cross_frame}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.cross_frame_role', :'cross_frame_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.cross_frame_role'));
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
        RAISE EXCEPTION 'Labeling cross-frame writes roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling cross-frame writes role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and batch control LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.cross_frame_write_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling cross-frame writes command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"cross_frame_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"cross_frame_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.cross_frame_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.cross_frame_write_command(jsonb)
    FROM :"cross_frame_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"cross_frame_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.cross_frame_write_command(jsonb)
    TO :"cross_frame_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.cross_frame_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.cross_frame_write_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling cross-frame writes LOGIN is missing its exact one-function profile';
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
        RAISE EXCEPTION 'Labeling cross-frame writes LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling cross-frame writes LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling cross-frame writes LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling cross-frame writes LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling cross-frame writes command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_discussion}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.discussion_role', :'discussion_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.discussion_role'));
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
        RAISE EXCEPTION 'Labeling discussion and event data roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling discussion and event data role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and caller LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.discussion_command(jsonb)',
        'yoao_labeling_v1.task_event_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling discussion and event data command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"discussion_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"discussion_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.discussion_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.discussion_command(jsonb),
    yoao_labeling_v1.task_event_command(jsonb)
    FROM :"discussion_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"discussion_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.discussion_command(jsonb),
    yoao_labeling_v1.task_event_command(jsonb)
    TO :"discussion_role";
GRANT SELECT ON yoao_labeling_v1.annotation_comment_facts,
    yoao_labeling_v1.annotation_feedback_facts,
    yoao_labeling_v1.task_event_facts,
    public.v_annotation_feedback_unified
    TO :"discussion_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.discussion_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.discussion_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.task_event_command(jsonb)')
    ];
    v_allowed_relations oid[] := ARRAY[
        pg_catalog.to_regclass('yoao_labeling_v1.annotation_comment_facts'),
        pg_catalog.to_regclass('yoao_labeling_v1.annotation_feedback_facts'),
        pg_catalog.to_regclass('yoao_labeling_v1.task_event_facts'),
        pg_catalog.to_regclass('public.v_annotation_feedback_unified')
    ];
    v_projection oid;
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling discussion and event data LOGIN is missing its exact two-function profile';
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
        RAISE EXCEPTION 'Labeling discussion and event data LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling discussion and event data LOGIN has foreign schema privileges';
    END IF;
    IF pg_catalog.array_position(v_allowed_relations,NULL) IS NOT NULL THEN
        RAISE EXCEPTION 'Labeling discussion public projections are missing';
    END IF;
    FOREACH v_projection IN ARRAY v_allowed_relations LOOP
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_class WHERE oid=v_projection AND relkind='v')
           OR NOT pg_catalog.has_table_privilege(v_client,v_projection,'SELECT')
           OR pg_catalog.has_table_privilege(v_client,v_projection,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR pg_catalog.has_any_column_privilege(v_client,v_projection,'INSERT,UPDATE,REFERENCES') THEN
            RAISE EXCEPTION 'Labeling discussion public projection is not SELECT-only';
        END IF;
    END LOOP;
    FOR v_relation IN
        SELECT relation.oid, relation.relkind
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.relkind IN ('r','p','v','m','f','S')
          AND NOT (relation.oid=ANY(v_allowed_relations))
    LOOP
        IF (v_relation.relkind <> 'S' AND (
                pg_catalog.has_table_privilege(v_client,v_relation.oid,
                    'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR pg_catalog.has_any_column_privilege(v_client,v_relation.oid,
                    'SELECT,INSERT,UPDATE,REFERENCES')
            )) OR (v_relation.relkind = 'S' AND
                pg_catalog.has_sequence_privilege(v_client,v_relation.oid,
                    'SELECT,UPDATE,USAGE')) THEN
            RAISE EXCEPTION 'Labeling discussion and event data LOGIN has data privileges outside its public projections';
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
        RAISE EXCEPTION 'Labeling discussion and event data LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling discussion and event data command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_editing}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.editing_role', :'editing_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.editing_role'));
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
        RAISE EXCEPTION 'Labeling editing roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling editing role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and editing LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.annotation_bulk_command(jsonb)',
        'yoao_labeling_v1.video_bbox_editing_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling editing command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"editing_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"editing_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.editing_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.annotation_bulk_command(jsonb),
    yoao_labeling_v1.video_bbox_editing_command(jsonb)
    FROM :"editing_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"editing_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.annotation_bulk_command(jsonb),
    yoao_labeling_v1.video_bbox_editing_command(jsonb)
    TO :"editing_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.editing_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.annotation_bulk_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.video_bbox_editing_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling editing LOGIN is missing its exact two-function profile';
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
        RAISE EXCEPTION 'Labeling editing LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling editing LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling editing LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling editing LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling editing command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_mask_edit}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.mask_edit_role', :'mask_edit_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.mask_edit_role'));
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
        RAISE EXCEPTION 'Labeling Mask edit data roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling Mask edit data role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and caller LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.mask_edit_command(jsonb)',
        'yoao_labeling_v1.mask_revision_command(jsonb)',
        'yoao_labeling_v1.ai_mask_accept_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling Mask edit data command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=pg_catalog.to_regprocedure(
            'yoao_labeling_v1._mask_revision_canonical_json(jsonb)'
        )
          AND function.proowner=v_owner AND function.prosecdef
          AND function.provolatile='i' AND function.prokind='f'
          AND function.prorettype='text'::pg_catalog.regtype
          AND function.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling Mask revision digest helper is missing or changed';
    END IF;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"mask_edit_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"mask_edit_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.mask_edit_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1._mask_revision_canonical_json(jsonb)
    FROM :"mask_edit_role", PUBLIC;
REVOKE ALL ON FUNCTION yoao_labeling_v1.mask_edit_command(jsonb),
    yoao_labeling_v1.mask_revision_command(jsonb),
    yoao_labeling_v1.ai_mask_accept_command(jsonb)
    FROM :"mask_edit_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"mask_edit_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.mask_edit_command(jsonb),
    yoao_labeling_v1.mask_revision_command(jsonb),
    yoao_labeling_v1.ai_mask_accept_command(jsonb)
    TO :"mask_edit_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.mask_edit_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.mask_edit_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.mask_revision_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.ai_mask_accept_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling Mask edit data LOGIN is missing its exact three-function profile';
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
        RAISE EXCEPTION 'Labeling Mask edit data LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling Mask edit data LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling Mask edit data LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling Mask edit data LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling Mask edit data command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_mask_gc_data}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.mask_gc_role', :'mask_gc_role', true);

DO $$
DECLARE
    v_gc oid;
    v_owner oid;
BEGIN
    SELECT oid INTO v_gc FROM pg_catalog.pg_roles
    WHERE rolname = current_setting('yoao.mask_gc_role')
      AND rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    SELECT oid INTO v_owner FROM pg_catalog.pg_roles
    WHERE rolname = current_setting('yoao.labeling_owner')
      AND NOT rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR v_gc IS NULL OR v_owner IS NULL OR v_gc = v_owner
       OR pg_catalog.pg_has_role(v_gc, v_owner, 'MEMBER') THEN
        RAISE EXCEPTION 'Unexpected database, caller or Labeling GC roles';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = v_gc OR membership.roleid = v_gc
    ) THEN
        RAISE EXCEPTION 'The Labeling Mask GC role must have no role memberships';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"mask_gc_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"mask_gc_role";
REVOKE ALL ON SCHEMA public, yoao_labeling_v1 FROM :"mask_gc_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"mask_gc_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"mask_gc_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public, yoao_labeling_v1 FROM :"mask_gc_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_labeling_v1 FROM PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"mask_gc_role";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1.claim_mask_gc_sweep(jsonb),
    yoao_labeling_v1.stage_mask_gc_page(jsonb),
    yoao_labeling_v1.claim_mask_gc_deletes(jsonb),
    yoao_labeling_v1.finish_mask_gc_delete(jsonb),
    yoao_labeling_v1.read_mask_gc_progress(jsonb),
    yoao_labeling_v1.inspect_mask_gc_page(jsonb)
    TO :"mask_gc_role";

DO $$
DECLARE
    role_name text := current_setting('yoao.mask_gc_role');
    v_callable integer;
BEGIN
    IF has_schema_privilege(role_name, 'public', 'CREATE')
       OR has_schema_privilege(role_name, 'yoao_labeling_v1', 'CREATE') THEN
        RAISE EXCEPTION 'The Labeling Mask GC role has direct schema access';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
              has_table_privilege(
                  role_name, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
              OR has_any_column_privilege(role_name, relation.oid, 'SELECT')
              OR has_any_column_privilege(role_name, relation.oid, 'INSERT')
              OR has_any_column_privilege(role_name, relation.oid, 'UPDATE')
              OR has_any_column_privilege(role_name, relation.oid, 'REFERENCES')
          )
    ) THEN
        RAISE EXCEPTION 'The Labeling Mask GC role has raw table privileges';
    END IF;
    SELECT count(*) INTO v_callable
    FROM pg_catalog.pg_proc AS proc
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = proc.pronamespace
    WHERE namespace.nspname = 'yoao_labeling_v1'
      AND proc.prokind = 'f'
      AND has_function_privilege(role_name, proc.oid, 'EXECUTE');
    IF v_callable <> 6
       OR has_function_privilege(
            role_name, 'yoao_labeling_v1.read_mask_write_epochs(jsonb)', 'EXECUTE'
       )
       OR has_function_privilege(
            role_name, 'yoao_labeling_v1.begin_mask_content_write(jsonb)', 'EXECUTE'
       )
       OR has_function_privilege(
            role_name, 'yoao_labeling_v1.lock_mask_write_epochs(jsonb)', 'EXECUTE'
       ) THEN
        RAISE EXCEPTION 'The Labeling Mask GC role must execute only the six GC operations';
    END IF;
END $$;
\endif

\if :{?grant_mask_quality}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.quality_role', :'quality_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.quality_role'));
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
        'yoao_labeling_v1.mask_qc_command(jsonb)',
        'yoao_labeling_v1.mask_repair_command(jsonb)',
        'yoao_labeling_v1.raster_mask_retention_query(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile=CASE
              WHEN v_signature='yoao_labeling_v1.raster_mask_retention_query(jsonb)' THEN 's'
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
    v_role pg_catalog.name := current_setting('yoao.quality_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.mask_qc_command(jsonb),
    yoao_labeling_v1.mask_repair_command(jsonb),
    yoao_labeling_v1.raster_mask_retention_query(jsonb)
    FROM :"quality_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"quality_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.mask_qc_command(jsonb),
    yoao_labeling_v1.mask_repair_command(jsonb),
    yoao_labeling_v1.raster_mask_retention_query(jsonb)
    TO :"quality_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.quality_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.mask_qc_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.mask_repair_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.raster_mask_retention_query(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
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
        RAISE EXCEPTION 'Labeling Mask QC and repair LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
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
\endif

\if :{?grant_preannotation_result_data}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.jobs_owner', :'jobs_owner', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.result_role', :'result_role', true);

DO $$
DECLARE
    v_client oid;
    v_jobs oid;
    v_labeling oid;
BEGIN
    SELECT oid INTO v_client FROM pg_catalog.pg_roles
    WHERE rolname = current_setting('yoao.result_role')
      AND rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    SELECT oid INTO v_jobs FROM pg_catalog.pg_roles
    WHERE rolname = current_setting('yoao.jobs_owner')
      AND NOT rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    SELECT oid INTO v_labeling FROM pg_catalog.pg_roles
    WHERE rolname = current_setting('yoao.labeling_owner')
      AND NOT rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR v_client IS NULL OR v_jobs IS NULL OR v_labeling IS NULL
       OR v_client IN (v_jobs, v_labeling)
       OR pg_catalog.pg_has_role(v_client, v_jobs, 'MEMBER')
       OR pg_catalog.pg_has_role(v_client, v_labeling, 'MEMBER') THEN
        RAISE EXCEPTION 'Unexpected database, caller or preannotation result roles';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = v_client OR membership.roleid = v_client
    ) THEN
        RAISE EXCEPTION 'The preannotation result role must have no role memberships';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"result_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"result_role";
REVOKE ALL ON SCHEMA public, yoao_jobs_v1, yoao_labeling_v1 FROM :"result_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"result_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"result_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_jobs_v1, yoao_labeling_v1 FROM :"result_role";
GRANT USAGE ON SCHEMA yoao_jobs_v1, yoao_labeling_v1 TO :"result_role";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.claim_batch_result_execution(jsonb),
    yoao_jobs_v1.heartbeat_batch_result_execution(jsonb),
    yoao_jobs_v1.release_batch_result_execution(jsonb),
    yoao_jobs_v1.stage_batch_result_members(jsonb),
    yoao_jobs_v1.start_batch_result_execution(jsonb),
    yoao_jobs_v1.read_batch_result_execution(jsonb),
    yoao_jobs_v1.mark_batch_result_overwrite(jsonb),
    yoao_jobs_v1.skip_batch_result_member(jsonb),
    yoao_jobs_v1.finish_batch_result_execution(jsonb),
    yoao_jobs_v1.start_batch_result_member(jsonb),
    yoao_labeling_v1.begin_batch_result(jsonb),
    yoao_labeling_v1.put_batch_result_part(jsonb),
    yoao_labeling_v1.seal_batch_result(jsonb),
    yoao_labeling_v1.read_batch_result(jsonb),
    yoao_labeling_v1.read_prediction_retry(jsonb),
    yoao_jobs_v1.claim_prediction_retry_deliveries(jsonb),
    yoao_jobs_v1.finish_prediction_retry_delivery(jsonb)
    TO :"result_role";

DO $$
DECLARE
    role_name text := current_setting('yoao.result_role');
    v_callable integer;
BEGIN
    IF has_schema_privilege(role_name, 'public', 'CREATE')
       OR has_schema_privilege(role_name, 'yoao_jobs_v1', 'CREATE')
       OR has_schema_privilege(role_name, 'yoao_labeling_v1', 'CREATE')
       OR has_database_privilege(role_name, current_database(), 'TEMP') THEN
        RAISE EXCEPTION 'The preannotation result role has direct schema or TEMP access';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r','p','v','m','f')
          AND (
              has_table_privilege(role_name, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR has_any_column_privilege(role_name, relation.oid, 'SELECT')
              OR has_any_column_privilege(role_name, relation.oid, 'INSERT')
              OR has_any_column_privilege(role_name, relation.oid, 'UPDATE')
              OR has_any_column_privilege(role_name, relation.oid, 'REFERENCES')
          )
    ) THEN
        RAISE EXCEPTION 'The preannotation result role has raw table privileges';
    END IF;
    SELECT count(*) INTO v_callable
    FROM pg_catalog.pg_proc AS proc
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = proc.pronamespace
    WHERE namespace.nspname IN ('yoao_jobs_v1','yoao_labeling_v1')
      AND proc.prokind = 'f'
      AND has_function_privilege(role_name, proc.oid, 'EXECUTE');
    IF v_callable <> 17
       OR has_function_privilege(role_name, 'yoao_labeling_v1.lock_batch_result_write(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'yoao_labeling_v1.finish_batch_result_write(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'yoao_labeling_v1.finish_batch_result_failure(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'yoao_jobs_v1.lock_batch_result_execution(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'yoao_labeling_v1.admit_prediction_retry(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'yoao_labeling_v1.finish_prediction_retry_write(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'yoao_labeling_v1.finish_prediction_retry_failure(jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'The preannotation result role must execute exactly 17 executor capabilities';
    END IF;
END $$;
\endif

\if :{?grant_prediction}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.prediction_role', :'prediction_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.prediction_role'));
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
        RAISE EXCEPTION 'Labeling prediction writes roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling prediction writes role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and editing LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.prediction_record_command(jsonb)',
        'yoao_labeling_v1.annotation_prediction_command(jsonb)',
        'yoao_labeling_v1.prediction_partition_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling prediction writes command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"prediction_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"prediction_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.prediction_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.prediction_record_command(jsonb),
    yoao_labeling_v1.annotation_prediction_command(jsonb),
    yoao_labeling_v1.prediction_partition_command(jsonb)
    FROM :"prediction_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"prediction_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.prediction_record_command(jsonb),
    yoao_labeling_v1.annotation_prediction_command(jsonb),
    yoao_labeling_v1.prediction_partition_command(jsonb)
    TO :"prediction_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.prediction_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.prediction_record_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.annotation_prediction_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.prediction_partition_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling prediction writes LOGIN is missing its exact three-function profile';
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
        RAISE EXCEPTION 'Labeling prediction writes LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling prediction writes LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling prediction writes LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling prediction writes LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling prediction writes command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_role_audit}
SELECT set_config('yoao.grant_section_applied', '1', true);
SET LOCAL search_path = pg_catalog, pg_temp;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.audit_consumer', :'consumer_role', true);

DO $preflight$
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
END
$preflight$;

CREATE TEMP VIEW _aap_labeling_role_audit_task_legacy_ref
WITH (security_barrier = true) AS
SELECT task.id, task.project_id, task.status, task.assignee_id, task.reviewer_id,
       task.batch_id, task.review_round_id, task.reviewer_claimed_at,
       task.reviewed_at, task.first_reviewed_at, task.first_review_result,
       NULL::jsonb AS annotation_contributor_ids,
       NULL::jsonb AS review_contributor_ids,
       NULL::uuid AS review_submitter_id
FROM public.tasks AS task;

CREATE TEMP VIEW _aap_labeling_role_audit_batch_ref
WITH (security_barrier = true) AS
SELECT batch.id, batch.annotator_id, batch.reviewer_id
FROM public.task_batches AS batch;

CREATE TEMP VIEW _aap_labeling_role_audit_annotation_ref
WITH (security_barrier = true) AS
SELECT annotation.source, annotation.user_id, annotation.is_active,
       annotation.was_cancelled
FROM public.annotations AS annotation;

CREATE TEMP VIEW _aap_labeling_role_audit_lock_ref
WITH (security_barrier = true) AS
SELECT task_lock.expire_at
FROM public.task_locks AS task_lock;

CREATE TEMP VIEW _aap_labeling_role_audit_capabilities_ref
WITH (security_barrier = true) AS
SELECT EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.tasks'::pg_catalog.regclass
      AND attribute.attname::text = 'annotation_contributor_ids'
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
) AS task_annotation_contributor_ids,
EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.tasks'::pg_catalog.regclass
      AND attribute.attname::text = 'review_contributor_ids'
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
) AS task_review_contributor_ids,
EXISTS (
    SELECT 1 FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'public.tasks'::pg_catalog.regclass
      AND attribute.attname::text = 'review_submitter_id'
      AND attribute.attnum > 0 AND NOT attribute.attisdropped
) AS task_review_submitter_id;

DO $install$
DECLARE
    v_migration name := current_setting('yoao.migration_role');
    v_labeling name := current_setting('yoao.labeling_owner');
    v_consumer name := current_setting('yoao.audit_consumer');
    v_migration_oid oid;
    v_labeling_oid oid;
    v_consumer_oid oid;
    v_tasks_oid oid;
    v_batches_oid oid;
    v_annotations_oid oid;
    v_locks_oid oid;
    v_schema_oid oid;
    v_view_oid oid;
    v_reference_oids oid[];
    v_legacy_task_reference oid := pg_catalog.to_regclass(
        'pg_temp._aap_labeling_role_audit_task_legacy_ref'
    );
    v_view_names text[] := ARRAY[
        'task_facts', 'task_batch_facts', 'annotation_facts',
        'task_lock_facts', 'schema_capabilities'
    ];
    v_source_names text[] := ARRAY[
        'tasks', 'task_batches', 'annotations', 'task_locks'
    ];
    v_view_name text;
    v_source_name text;
    v_all_source_columns text;
    v_view_columns text;
    v_columns text[];
    v_column_types text[];
    v_expected_columns text[];
    v_expected_types text[];
    v_view_definition text;
    v_task_annotation_expression text;
    v_task_review_expression text;
    v_task_submitter_expression text;
    v_has_annotation_ids boolean;
    v_has_review_ids boolean;
    v_has_review_submitter boolean;
    v_granted_create boolean := false;
    v_index integer;
BEGIN
    IF v_migration !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_labeling !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_consumer !~ '^[a-z_][a-z0-9_]{0,62}$'
       OR v_labeling = v_migration
       OR v_consumer IN (
            v_migration, v_labeling, 'identity_api_consumer',
            'workspace_api_consumer', 'yoao_labeling_core_read_client'
       ) THEN
        RAISE EXCEPTION 'Invalid or overlapping Labeling audit roles';
    END IF;
    IF pg_catalog.array_position(ARRAY[v_legacy_task_reference], NULL::oid) IS NOT NULL THEN
        RAISE EXCEPTION 'Failed to create Labeling audit legacy reference view';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(20261023, 303);

    SELECT role.oid INTO v_migration_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_migration;
    SELECT role.oid INTO v_labeling_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_labeling;
    SELECT role.oid INTO v_consumer_oid
    FROM pg_catalog.pg_roles AS role WHERE role.rolname = v_consumer;
    IF v_migration_oid IS NULL OR v_labeling_oid IS NULL OR v_consumer_oid IS NULL THEN
        RAISE EXCEPTION 'Migration, Labeling owner, and audit consumer roles must exist';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.oid = v_labeling_oid
          AND NOT role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS role WHERE role.oid = v_consumer_oid
          AND role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Labeling owner must be NOLOGIN and audit consumer a restricted LOGIN';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = v_consumer_oid OR membership.roleid = v_consumer_oid
           OR membership.member = v_labeling_oid
           OR (membership.roleid = v_labeling_oid
               AND membership.member <> v_migration_oid)
    ) THEN
        RAISE EXCEPTION 'Audit consumer or Labeling owner has an unsafe role membership';
    END IF;
    IF NOT pg_catalog.pg_has_role(v_migration_oid, v_labeling_oid, 'MEMBER')
       AND NOT EXISTS (
           SELECT 1 FROM pg_catalog.pg_roles AS role
           WHERE role.oid = v_migration_oid AND role.rolsuper
       ) THEN
        RAISE EXCEPTION 'Migration role cannot transfer views to the Labeling owner';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM (VALUES
            ('tasks', 'id', 'uuid'),
            ('tasks', 'project_id', 'uuid'),
            ('tasks', 'status', 'character varying(30)'),
            ('tasks', 'assignee_id', 'uuid'),
            ('tasks', 'reviewer_id', 'uuid'),
            ('tasks', 'batch_id', 'uuid'),
            ('tasks', 'review_round_id', 'uuid'),
            ('tasks', 'reviewer_claimed_at', 'timestamp with time zone'),
            ('tasks', 'reviewed_at', 'timestamp with time zone'),
            ('tasks', 'first_reviewed_at', 'timestamp with time zone'),
            ('tasks', 'first_review_result', 'character varying(16)'),
            ('task_batches', 'id', 'uuid'),
            ('task_batches', 'annotator_id', 'uuid'),
            ('task_batches', 'reviewer_id', 'uuid'),
            ('annotations', 'source', 'character varying(20)'),
            ('annotations', 'user_id', 'uuid'),
            ('annotations', 'is_active', 'boolean'),
            ('annotations', 'was_cancelled', 'boolean'),
            ('task_locks', 'expire_at', 'timestamp with time zone')
        ) AS expected(table_name, column_name, type_name)
        LEFT JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.nspname = 'public'
        LEFT JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = namespace.oid
         AND relation.relname = expected.table_name
        LEFT JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = relation.oid
         AND attribute.attname = expected.column_name
         AND attribute.attnum > 0 AND NOT attribute.attisdropped
        WHERE relation.oid IS NULL OR relation.relkind <> 'r'
           OR relation.relowner <> v_migration_oid
           OR attribute.attnum IS NULL
           OR pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
                IS DISTINCT FROM expected.type_name
    ) THEN
        RAISE EXCEPTION 'Historical Labeling source schema is incompatible';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM (VALUES
            ('annotation_contributor_ids', 'jsonb'),
            ('review_contributor_ids', 'jsonb'),
            ('review_submitter_id', 'uuid')
        ) AS expected(column_name, type_name)
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.nspname = 'public'
        JOIN pg_catalog.pg_class AS relation
          ON relation.relnamespace = namespace.oid AND relation.relname = 'tasks'
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = relation.oid
         AND attribute.attname = expected.column_name
         AND attribute.attnum > 0 AND NOT attribute.attisdropped
        WHERE pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
              IS DISTINCT FROM expected.type_name
    ) THEN
        RAISE EXCEPTION 'Optional Labeling preparation columns have incompatible types';
    END IF;

    SELECT relation.oid INTO v_tasks_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'tasks';
    SELECT relation.oid INTO v_batches_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'task_batches';
    SELECT relation.oid INTO v_annotations_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'annotations';
    SELECT relation.oid INTO v_locks_oid
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'public' AND relation.relname = 'task_locks';
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_index AS primary_key
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = primary_key.indrelid AND attribute.attname = 'id'
        WHERE primary_key.indrelid = v_tasks_oid AND primary_key.indisprimary
          AND primary_key.indnatts = 1 AND primary_key.indkey[0] = attribute.attnum
    ) OR NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_index AS primary_key
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = primary_key.indrelid AND attribute.attname = 'id'
        WHERE primary_key.indrelid = v_batches_oid AND primary_key.indisprimary
          AND primary_key.indnatts = 1 AND primary_key.indkey[0] = attribute.attnum
    ) THEN
        RAISE EXCEPTION 'Historical Task and TaskBatch IDs must remain primary keys';
    END IF;
    IF v_tasks_oid IS NULL OR v_batches_oid IS NULL
       OR v_annotations_oid IS NULL OR v_locks_oid IS NULL THEN
        RAISE EXCEPTION 'A required Labeling audit source is missing';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        WHERE relation.oid = ANY(ARRAY[
            v_tasks_oid, v_batches_oid, v_annotations_oid, v_locks_oid
        ]) AND relation.relowner = v_consumer_oid
    ) THEN
        RAISE EXCEPTION 'Audit consumer must not own a private Labeling source';
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_tasks_oid
          AND attribute.attname = 'annotation_contributor_ids'
          AND attribute.attnum > 0 AND NOT attribute.attisdropped
    ) INTO v_has_annotation_ids;
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_tasks_oid
          AND attribute.attname = 'review_contributor_ids'
          AND attribute.attnum > 0 AND NOT attribute.attisdropped
    ) INTO v_has_review_ids;
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_tasks_oid
          AND attribute.attname = 'review_submitter_id'
          AND attribute.attnum > 0 AND NOT attribute.attisdropped
    ) INTO v_has_review_submitter;
    v_task_annotation_expression := CASE WHEN v_has_annotation_ids
        THEN 'task.annotation_contributor_ids' ELSE 'NULL::jsonb' END;
    v_task_review_expression := CASE WHEN v_has_review_ids
        THEN 'task.review_contributor_ids' ELSE 'NULL::jsonb' END;
    v_task_submitter_expression := CASE WHEN v_has_review_submitter
        THEN 'task.review_submitter_id' ELSE 'NULL::uuid' END;
    EXECUTE pg_catalog.format(
        'CREATE TEMP VIEW _aap_labeling_role_audit_task_ref '
        'WITH (security_barrier = true) AS '
        'SELECT task.id, task.project_id, task.status, task.assignee_id, '
        'task.reviewer_id, task.batch_id, task.review_round_id, '
        'task.reviewer_claimed_at, task.reviewed_at, task.first_reviewed_at, '
        'task.first_review_result, %s AS annotation_contributor_ids, '
        '%s AS review_contributor_ids, %s AS review_submitter_id '
        'FROM public.tasks AS task',
        v_task_annotation_expression, v_task_review_expression,
        v_task_submitter_expression
    );
    v_reference_oids := ARRAY[
        pg_catalog.to_regclass('pg_temp._aap_labeling_role_audit_task_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_labeling_role_audit_batch_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_labeling_role_audit_annotation_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_labeling_role_audit_lock_ref')::oid,
        pg_catalog.to_regclass('pg_temp._aap_labeling_role_audit_capabilities_ref')::oid
    ];
    IF pg_catalog.array_position(v_reference_oids, NULL::oid) IS NOT NULL THEN
        RAISE EXCEPTION 'Failed to create Labeling audit reference views';
    END IF;

    IF NOT pg_catalog.has_schema_privilege(v_labeling_oid, 'public', 'USAGE') THEN
        EXECUTE pg_catalog.format('GRANT USAGE ON SCHEMA public TO %I', v_labeling);
    END IF;
    EXECUTE pg_catalog.format(
        'GRANT SELECT (id, project_id, status, assignee_id, reviewer_id, batch_id, '
        'review_round_id, reviewer_claimed_at, reviewed_at, first_reviewed_at, '
        'first_review_result) ON TABLE public.tasks TO %I', v_labeling
    );
    IF v_has_annotation_ids THEN
        EXECUTE pg_catalog.format(
            'GRANT SELECT (annotation_contributor_ids) ON TABLE public.tasks TO %I',
            v_labeling
        );
    END IF;
    IF v_has_review_ids THEN
        EXECUTE pg_catalog.format(
            'GRANT SELECT (review_contributor_ids) ON TABLE public.tasks TO %I',
            v_labeling
        );
    END IF;
    IF v_has_review_submitter THEN
        EXECUTE pg_catalog.format(
            'GRANT SELECT (review_submitter_id) ON TABLE public.tasks TO %I',
            v_labeling
        );
    END IF;
    EXECUTE pg_catalog.format(
        'GRANT SELECT (id, annotator_id, reviewer_id) '
        'ON TABLE public.task_batches TO %I', v_labeling
    );
    EXECUTE pg_catalog.format(
        'GRANT SELECT (source, user_id, is_active, was_cancelled) '
        'ON TABLE public.annotations TO %I', v_labeling
    );
    EXECUTE pg_catalog.format(
        'GRANT SELECT (expire_at) ON TABLE public.task_locks TO %I', v_labeling
    );
    IF pg_catalog.has_table_privilege(v_labeling_oid, v_tasks_oid, 'SELECT')
       OR pg_catalog.has_table_privilege(v_labeling_oid, v_batches_oid, 'SELECT')
       OR pg_catalog.has_table_privilege(v_labeling_oid, v_annotations_oid, 'SELECT')
       OR pg_catalog.has_table_privilege(v_labeling_oid, v_locks_oid, 'SELECT') THEN
        RAISE EXCEPTION 'Labeling audit owner has unexpected table-level source SELECT';
    END IF;

    SELECT namespace.oid INTO v_schema_oid
    FROM pg_catalog.pg_namespace AS namespace
    WHERE namespace.nspname = 'yoao_labeling_audit_v1';
    IF v_schema_oid IS NULL THEN
        EXECUTE pg_catalog.format(
            'CREATE SCHEMA yoao_labeling_audit_v1 AUTHORIZATION %I', v_labeling
        );
        SELECT namespace.oid INTO v_schema_oid
        FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_labeling_audit_v1';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.oid = v_schema_oid AND namespace.nspowner = v_labeling_oid
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace = v_schema_oid
          AND relation.relname <> ALL (v_view_names)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_proc AS routine
        WHERE routine.pronamespace = v_schema_oid
    ) THEN
        RAISE EXCEPTION 'yoao_labeling_audit_v1 has a foreign owner or unexpected object';
    END IF;

    FOR v_index IN 1..5 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('yoao_labeling_audit_v1.%I', v_view_name)
        );
        IF v_view_oid IS NULL THEN
            IF NOT pg_catalog.has_schema_privilege(v_migration_oid, v_schema_oid, 'CREATE')
               AND NOT v_granted_create THEN
                EXECUTE pg_catalog.format(
                    'GRANT CREATE ON SCHEMA yoao_labeling_audit_v1 TO %I', v_migration
                );
                v_granted_create := true;
            END IF;
            v_view_definition := pg_catalog.pg_get_viewdef(v_reference_oids[v_index], true);
            EXECUTE pg_catalog.format(
                'CREATE VIEW yoao_labeling_audit_v1.%I '
                'WITH (security_barrier = true) AS %s',
                v_view_name, v_view_definition
            );
            EXECUTE pg_catalog.format(
                'ALTER VIEW yoao_labeling_audit_v1.%I OWNER TO %I',
                v_view_name, v_labeling
            );
            v_view_oid := pg_catalog.to_regclass(
                pg_catalog.format('yoao_labeling_audit_v1.%I', v_view_name)
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_class AS relation
            WHERE relation.oid = v_view_oid AND relation.relkind = 'v'
              AND relation.relowner = v_labeling_oid
              AND COALESCE(relation.reloptions, ARRAY[]::text[])
                  @> ARRAY['security_barrier=true']
        ) THEN
            RAISE EXCEPTION 'yoao_labeling_audit_v1.% has an unexpected owner, kind, or security option',
                v_view_name;
        END IF;
        SELECT pg_catalog.array_agg(attribute.attname::text ORDER BY attribute.attnum),
               pg_catalog.array_agg(
                   pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
                   ORDER BY attribute.attnum
               )
        INTO v_columns, v_column_types
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_view_oid
          AND attribute.attnum > 0 AND NOT attribute.attisdropped;
        CASE v_index
            WHEN 1 THEN
                v_expected_columns := ARRAY[
                    'id', 'project_id', 'status', 'assignee_id', 'reviewer_id',
                    'batch_id', 'review_round_id', 'reviewer_claimed_at', 'reviewed_at',
                    'first_reviewed_at', 'first_review_result',
                    'annotation_contributor_ids', 'review_contributor_ids',
                    'review_submitter_id'
                ];
                v_expected_types := ARRAY[
                    'uuid', 'uuid', 'character varying(30)', 'uuid', 'uuid', 'uuid',
                    'uuid', 'timestamp with time zone', 'timestamp with time zone',
                    'timestamp with time zone', 'character varying(16)', 'jsonb',
                    'jsonb', 'uuid'
                ];
            WHEN 2 THEN
                v_expected_columns := ARRAY['id', 'annotator_id', 'reviewer_id'];
                v_expected_types := ARRAY['uuid', 'uuid', 'uuid'];
            WHEN 3 THEN
                v_expected_columns := ARRAY['source', 'user_id', 'is_active', 'was_cancelled'];
                v_expected_types := ARRAY[
                    'character varying(20)', 'uuid', 'boolean', 'boolean'
                ];
            WHEN 4 THEN
                v_expected_columns := ARRAY['expire_at'];
                v_expected_types := ARRAY['timestamp with time zone'];
            ELSE
                v_expected_columns := ARRAY[
                    'task_annotation_contributor_ids', 'task_review_contributor_ids',
                    'task_review_submitter_id'
                ];
                v_expected_types := ARRAY['boolean', 'boolean', 'boolean'];
        END CASE;
        v_view_definition := pg_catalog.pg_get_viewdef(v_reference_oids[v_index], true);
        IF v_columns IS DISTINCT FROM v_expected_columns
           OR v_column_types IS DISTINCT FROM v_expected_types THEN
            RAISE EXCEPTION 'yoao_labeling_audit_v1.% has an unexpected shape', v_view_name;
        END IF;
        IF pg_catalog.pg_get_viewdef(v_view_oid, true) IS DISTINCT FROM v_view_definition THEN
            IF v_index = 1
               AND (v_has_annotation_ids OR v_has_review_ids OR v_has_review_submitter)
               AND pg_catalog.pg_get_viewdef(v_view_oid, true) IS DISTINCT FROM
                   pg_catalog.pg_get_viewdef(v_legacy_task_reference, true) THEN
                RAISE EXCEPTION 'yoao_labeling_audit_v1.% has an unexpected definition',
                    v_view_name;
            END IF;
            IF v_index <> 1
               OR NOT (v_has_annotation_ids OR v_has_review_ids OR v_has_review_submitter) THEN
                RAISE EXCEPTION 'yoao_labeling_audit_v1.% has an unexpected definition',
                    v_view_name;
            END IF;
            IF NOT pg_catalog.has_schema_privilege(v_migration_oid, v_schema_oid, 'CREATE')
               AND NOT v_granted_create THEN
                EXECUTE pg_catalog.format(
                    'GRANT CREATE ON SCHEMA yoao_labeling_audit_v1 TO %I', v_migration
                );
                v_granted_create := true;
            END IF;
            EXECUTE pg_catalog.format(
                'CREATE OR REPLACE VIEW yoao_labeling_audit_v1.task_facts '
                'WITH (security_barrier = true) AS %s',
                v_view_definition
            );
        END IF;
    END LOOP;
    IF v_granted_create THEN
        EXECUTE pg_catalog.format(
            'REVOKE CREATE ON SCHEMA yoao_labeling_audit_v1 FROM %I', v_migration
        );
    END IF;

    EXECUTE 'REVOKE ALL ON SCHEMA yoao_labeling_audit_v1 FROM PUBLIC';
    EXECUTE pg_catalog.format(
        'REVOKE ALL ON SCHEMA yoao_labeling_audit_v1 FROM %I', v_consumer
    );
    FOR v_index IN 1..4 LOOP
        v_source_name := v_source_names[v_index];
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                     ORDER BY attribute.attnum)
        INTO v_all_source_columns
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = CASE v_index
            WHEN 1 THEN v_tasks_oid WHEN 2 THEN v_batches_oid
            WHEN 3 THEN v_annotations_oid ELSE v_locks_oid END
          AND attribute.attnum > 0 AND NOT attribute.attisdropped;
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE public.%I FROM %I', v_source_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE public.%I FROM %I',
            v_all_source_columns, v_source_name, v_consumer
        );
    END LOOP;
    FOR v_index IN 1..5 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('yoao_labeling_audit_v1.%I', v_view_name)
        );
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ', '
                                     ORDER BY attribute.attnum)
        INTO v_view_columns
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = v_view_oid
          AND attribute.attnum > 0 AND NOT attribute.attisdropped;
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE yoao_labeling_audit_v1.%I FROM PUBLIC', v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE yoao_labeling_audit_v1.%I FROM PUBLIC',
            v_view_columns, v_view_name
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL ON TABLE yoao_labeling_audit_v1.%I FROM %I',
            v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'REVOKE ALL (%s) ON TABLE yoao_labeling_audit_v1.%I FROM %I',
            v_view_columns, v_view_name, v_consumer
        );
        EXECUTE pg_catalog.format(
            'GRANT SELECT ON TABLE yoao_labeling_audit_v1.%I TO %I',
            v_view_name, v_consumer
        );
    END LOOP;
    EXECUTE pg_catalog.format(
        'GRANT USAGE ON SCHEMA yoao_labeling_audit_v1 TO %I', v_consumer
    );

    IF pg_catalog.has_table_privilege(v_consumer_oid, v_tasks_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_tasks_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.tasks access';
    END IF;
    IF pg_catalog.has_table_privilege(v_consumer_oid, v_batches_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_batches_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.task_batches access';
    END IF;
    IF pg_catalog.has_table_privilege(v_consumer_oid, v_annotations_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_annotations_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.annotations access';
    END IF;
    IF pg_catalog.has_table_privilege(v_consumer_oid, v_locks_oid,
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(
           v_consumer_oid, v_locks_oid, 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit consumer retains private public.task_locks access';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(v_consumer_oid, v_schema_oid, 'USAGE')
       OR pg_catalog.has_schema_privilege(v_consumer_oid, v_schema_oid, 'CREATE') THEN
        RAISE EXCEPTION 'Audit consumer has an unexpected Labeling audit schema privilege';
    END IF;
    FOR v_index IN 1..5 LOOP
        v_view_name := v_view_names[v_index];
        v_view_oid := pg_catalog.to_regclass(
            pg_catalog.format('yoao_labeling_audit_v1.%I', v_view_name)
        );
        IF NOT pg_catalog.has_table_privilege(v_consumer_oid, v_view_oid, 'SELECT')
           OR pg_catalog.has_table_privilege(
               v_consumer_oid, v_view_oid,
               'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
           ) OR pg_catalog.has_any_column_privilege(
               v_consumer_oid, v_view_oid, 'INSERT,UPDATE,REFERENCES'
           ) THEN
            RAISE EXCEPTION 'Audit consumer has an unexpected Labeling audit view privilege';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.pg_class AS relation
            CROSS JOIN LATERAL pg_catalog.aclexplode(
                COALESCE(relation.relacl, pg_catalog.acldefault('r', relation.relowner))
            ) AS privilege
            WHERE relation.oid = v_view_oid AND privilege.grantee = 0
        ) THEN
            RAISE EXCEPTION 'PUBLIC retains access to a Labeling audit relation';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(
            COALESCE(namespace.nspacl, pg_catalog.acldefault('n', namespace.nspowner))
        ) AS privilege
        WHERE namespace.oid = v_schema_oid AND privilege.grantee = 0
    ) THEN
        RAISE EXCEPTION 'PUBLIC retains access to yoao_labeling_audit_v1';
    END IF;
END
$install$;
DROP VIEW pg_temp._aap_labeling_role_audit_capabilities_ref;
DROP VIEW pg_temp._aap_labeling_role_audit_lock_ref;
DROP VIEW pg_temp._aap_labeling_role_audit_annotation_ref;
DROP VIEW pg_temp._aap_labeling_role_audit_batch_ref;
DROP VIEW pg_temp._aap_labeling_role_audit_task_legacy_ref;
DROP VIEW pg_temp._aap_labeling_role_audit_task_ref;
\endif

\if :{?grant_scene_read}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.scene_read_role', :'scene_read_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.scene_read_role'));
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
        RAISE EXCEPTION 'Labeling scene read data roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling scene read data role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and caller LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.scene_read_query(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling scene read data command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"scene_read_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"scene_read_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.scene_read_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.scene_read_query(jsonb)
    FROM :"scene_read_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"scene_read_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.scene_read_query(jsonb)
    TO :"scene_read_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.scene_read_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.scene_read_query(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling scene read data LOGIN is missing its exact one-function profile';
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
        RAISE EXCEPTION 'Labeling scene read data LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling scene read data LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling scene read data LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling scene read data LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling scene read data command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_scene_track_commands}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.scene_track_command_role', :'scene_track_command_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.scene_track_command_role'));
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
        RAISE EXCEPTION 'Labeling SceneTrack commands roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling SceneTrack commands role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and batch control LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.scene_track_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling SceneTrack commands command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"scene_track_command_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"scene_track_command_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.scene_track_command_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.scene_track_command(jsonb)
    FROM :"scene_track_command_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"scene_track_command_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.scene_track_command(jsonb)
    TO :"scene_track_command_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.scene_track_command_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.scene_track_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling SceneTrack commands LOGIN is missing its exact one-function profile';
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
        RAISE EXCEPTION 'Labeling SceneTrack commands LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling SceneTrack commands LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling SceneTrack commands LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling SceneTrack commands LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling SceneTrack commands command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_spatial_quality}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.spatial_quality_role', :'spatial_quality_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.spatial_quality_role'));
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
        RAISE EXCEPTION 'Labeling spatial quality data roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling spatial quality data role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and caller LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.spatial_quality_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling spatial quality data command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"spatial_quality_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"spatial_quality_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.spatial_quality_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.spatial_quality_command(jsonb)
    FROM :"spatial_quality_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"spatial_quality_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.spatial_quality_command(jsonb)
    TO :"spatial_quality_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.spatial_quality_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.spatial_quality_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling spatial quality data LOGIN is missing its exact one-function profile';
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
        RAISE EXCEPTION 'Labeling spatial quality data LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling spatial quality data LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling spatial quality data LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling spatial quality data LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling spatial quality data command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_task_control}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.task_control_role', :'task_control_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.task_control_role'));
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
        RAISE EXCEPTION 'Labeling task control roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling task control role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and task control LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.task_edit_lock_command(jsonb)',
        'yoao_labeling_v1.task_workflow_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling task control command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"task_control_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"task_control_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.task_control_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.task_edit_lock_command(jsonb),
    yoao_labeling_v1.task_workflow_command(jsonb)
    FROM :"task_control_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"task_control_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.task_edit_lock_command(jsonb),
    yoao_labeling_v1.task_workflow_command(jsonb)
    TO :"task_control_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.task_control_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.task_edit_lock_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.task_workflow_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling task control LOGIN is missing its exact two-function profile';
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
        RAISE EXCEPTION 'Labeling task control LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling task control LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling task control LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling task control LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling task control command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_task_maintenance}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.task_maintenance_role', :'task_maintenance_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.task_maintenance_role'));
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
        RAISE EXCEPTION 'Labeling task maintenance data roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling task maintenance data role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and caller LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.task_maintenance_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling task maintenance data command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"task_maintenance_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"task_maintenance_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.task_maintenance_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.task_maintenance_command(jsonb)
    FROM :"task_maintenance_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"task_maintenance_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.task_maintenance_command(jsonb)
    TO :"task_maintenance_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.task_maintenance_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.task_maintenance_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling task maintenance data LOGIN is missing its exact one-function profile';
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
        RAISE EXCEPTION 'Labeling task maintenance data LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling task maintenance data LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling task maintenance data LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling task maintenance data LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling task maintenance data command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_video_control}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.video_control_role', :'video_control_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.labeling_owner'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.video_control_role'));
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
        RAISE EXCEPTION 'Labeling video control data roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Labeling video control data role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_client OR membership.roleid=v_client
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Labeling owner and caller LOGIN have unsafe membership';
    END IF;
    FOREACH v_signature IN ARRAY ARRAY[
        'yoao_labeling_v1.video_control_command(jsonb)',
        'yoao_labeling_v1.video_segment_lifecycle_command(jsonb)'
    ] LOOP
    v_function := pg_catalog.to_regprocedure(v_signature);
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND function.prorettype='jsonb'::pg_catalog.regtype
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Labeling video control data command is missing or changed: %', v_signature;
    END IF;
    END LOOP;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"video_control_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"video_control_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.video_control_role');
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

REVOKE ALL ON FUNCTION yoao_labeling_v1.video_control_command(jsonb)
    FROM :"video_control_role", PUBLIC;
REVOKE ALL ON FUNCTION yoao_labeling_v1.video_segment_lifecycle_command(jsonb)
    FROM :"video_control_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"video_control_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.video_control_command(jsonb),
    yoao_labeling_v1.video_segment_lifecycle_command(jsonb)
    TO :"video_control_role";

DO $verify$
DECLARE
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.video_control_role'));
    v_allowed oid[] := ARRAY[
        pg_catalog.to_regprocedure('yoao_labeling_v1.video_control_command(jsonb)'),
        pg_catalog.to_regprocedure('yoao_labeling_v1.video_segment_lifecycle_command(jsonb)')
    ];
    v_relation record;
BEGIN
    IF pg_catalog.array_position(v_allowed,NULL) IS NOT NULL
       OR NOT pg_catalog.has_database_privilege(v_client,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_client,'yoao_labeling_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_client,'public','CREATE')
       OR EXISTS (
           SELECT FROM pg_catalog.unnest(v_allowed) AS capability(oid)
           WHERE NOT pg_catalog.has_function_privilege(v_client,capability.oid,'EXECUTE')
       ) THEN
        RAISE EXCEPTION 'Labeling video control data LOGIN is missing its exact two-function profile';
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
        RAISE EXCEPTION 'Labeling video control data LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_labeling_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Labeling video control data LOGIN has foreign schema privileges';
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
            RAISE EXCEPTION 'Labeling video control data LOGIN has direct data privileges';
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
        RAISE EXCEPTION 'Labeling video control data LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=ANY(v_allowed) AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Labeling video control data command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_video_writer}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.labeling_owner', :'labeling_owner', true);
SELECT set_config('yoao.labeling_video_writer_role', :'labeling_video_writer_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.labeling_video_writer_role');
    owner_name text := current_setting('yoao.labeling_owner');
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_labeling_video_writer_client' OR owner_name <> 'labeling_code'
       OR role_name = owner_name THEN
        RAISE EXCEPTION 'Unexpected Labeling video writer roles';
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
        RAISE EXCEPTION 'Labeling video writer roles are unsafe or have memberships';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (
        SELECT oid FROM pg_catalog.pg_roles
        WHERE rolname = current_setting('yoao.labeling_owner')
    );
    function_oid oid;
    signature text;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_labeling_v1'
    ) OR NOT pg_catalog.has_schema_privilege(
        owner_oid, 'yoao_labeling_v1', 'USAGE'
    ) THEN
        RAISE EXCEPTION 'Labeling video writer Native schema or function is unavailable';
    END IF;
    FOREACH signature IN ARRAY ARRAY[
        'yoao_labeling_v1.video_writer_step(text,text)'
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
        RAISE EXCEPTION 'Jobs Labeling video writer capability has unexpected ownership or settings';
    END IF;
    END LOOP;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"labeling_video_writer_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"labeling_video_writer_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.labeling_video_writer_role');
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
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"labeling_video_writer_role";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.video_writer_step(text,text)
    TO :"labeling_video_writer_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.labeling_video_writer_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_labeling_v1.video_writer_step(text, text)'
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
        RAISE EXCEPTION 'Labeling video writer LOGIN has unexpected functions: %',actual_functions;
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
