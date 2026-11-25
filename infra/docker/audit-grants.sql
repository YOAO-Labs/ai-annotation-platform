\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.grant_section_applied', '0', true);

\if :{?grant_append}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.append_client', :'append_client', true);

DO $$
DECLARE
    owner_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('yoao.audit_owner'));
    api_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('yoao.api_role'));
    client_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('yoao.append_client'));
    append_function oid := pg_catalog.to_regprocedure('yoao_audit_v1.append_events(jsonb)')::oid;
    audit_sequence oid := pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id')::regclass::oid;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF owner_oid IS NULL OR api_oid IS NULL OR client_oid IS NULL
       OR append_function IS NULL OR audit_sequence IS NULL OR owner_oid = api_oid
       OR owner_oid = client_oid OR api_oid = client_oid THEN
        RAISE EXCEPTION 'Audit owner, API role, append client, or function is missing';
    END IF;
    IF (SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
        FROM pg_roles WHERE oid = owner_oid) THEN
        RAISE EXCEPTION 'Audit owner must be a restricted NOLOGIN role';
    END IF;
    IF NOT (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
            AND NOT rolreplication AND NOT rolbypassrls AND NOT rolinherit
            FROM pg_roles WHERE oid = client_oid) THEN
        RAISE EXCEPTION 'Audit append client must be a restricted LOGIN NOINHERIT role';
    END IF;
    IF NOT (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
            AND NOT rolreplication AND NOT rolbypassrls
            FROM pg_roles WHERE oid = api_oid) THEN
        RAISE EXCEPTION 'API role must be a restricted LOGIN';
    END IF;
    IF EXISTS (
        SELECT FROM pg_auth_members
        WHERE member IN (client_oid, api_oid) OR roleid IN (client_oid, api_oid)
    ) THEN
        RAISE EXCEPTION 'API and append client roles must not have memberships';
    END IF;
    IF pg_catalog.has_database_privilege(client_oid, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(client_oid, current_database(), 'TEMP')
       OR pg_catalog.has_schema_privilege(client_oid, 'public', 'CREATE')
       OR pg_catalog.has_schema_privilege(client_oid, 'yoao_audit_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Audit append client has database or schema creation privileges';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE (namespace.nspname LIKE 'aap\_%' ESCAPE '\'
               OR namespace.nspname LIKE 'yoao\_%' ESCAPE '\')
          AND namespace.nspname <> 'yoao_audit_v1'
          AND pg_catalog.has_schema_privilege(client_oid, namespace.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Audit append client has a foreign module schema grant';
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc
        WHERE oid = append_function AND proowner = owner_oid AND prosecdef
    ) THEN
        RAISE EXCEPTION 'Audit append function must be owned by the Audit code role';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE (relation.oid IN (
            SELECT relid FROM pg_catalog.pg_partition_tree('public.audit_logs')
        ) OR relation.oid IN ('public.mv_audit_bi_daily'::regclass, audit_sequence))
          AND relation.relowner <> owner_oid
    ) THEN
        RAISE EXCEPTION 'The restricted Audit owner must hold the partition tree, materialization and sequence';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_default_acl AS defaults
        CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl
        WHERE defaults.defaclrole = owner_oid
          AND defaults.defaclnamespace IN (0, 'public'::regnamespace)
          AND defaults.defaclobjtype IN ('r', 'S')
          AND acl.grantee <> owner_oid
    ) THEN
        RAISE EXCEPTION 'Audit owner default privileges expose future private storage';
    END IF;
END $$;

DO $$
DECLARE
    v_role name := current_setting('yoao.api_role');
    v_columns text;
    v_relation record;
BEGIN
    FOR v_relation IN
        SELECT relation.oid, namespace.nspname, relation.relname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE relation.oid IN (
            SELECT relid FROM pg_catalog.pg_partition_tree('public.audit_logs')
        ) OR relation.oid IN ('public.mv_audit_bi_daily'::regclass,
                              'public.audit_archive_outputs'::regclass)
    LOOP
        EXECUTE pg_catalog.format('REVOKE ALL ON TABLE %I.%I FROM %I, PUBLIC',
            v_relation.nspname, v_relation.relname, v_role);
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname), ',' ORDER BY attnum)
        INTO v_columns FROM pg_catalog.pg_attribute
        WHERE attrelid = v_relation.oid AND attnum > 0 AND NOT attisdropped;
        EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I, PUBLIC',
            v_columns, v_relation.nspname, v_relation.relname, v_role);
    END LOOP;
    EXECUTE pg_catalog.format('REVOKE ALL ON SEQUENCE %s FROM %I, PUBLIC',
        pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id'), v_role);
END $$;
REVOKE ALL ON SCHEMA yoao_audit_v1 FROM :"append_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_audit_v1 FROM :"append_client";
REVOKE ALL ON ALL TABLES IN SCHEMA yoao_audit_v1 FROM :"append_client";
GRANT CONNECT ON DATABASE :"expected_database" TO :"append_client";
GRANT USAGE ON SCHEMA yoao_audit_v1 TO :"append_client";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.append_events(jsonb) TO :"append_client";
GRANT USAGE ON SCHEMA yoao_audit_v1 TO :"api_role";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.append_events(jsonb) TO :"api_role";

DO $$
DECLARE
    api_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('yoao.api_role'));
    client_oid oid := (SELECT oid FROM pg_roles WHERE rolname = current_setting('yoao.append_client'));
    append_function oid := pg_catalog.to_regprocedure('yoao_audit_v1.append_events(jsonb)')::oid;
    audit_sequence oid := pg_catalog.pg_get_serial_sequence('public.audit_logs', 'id')::regclass::oid;
BEGIN
    IF NOT pg_catalog.has_database_privilege(client_oid, current_database(), 'CONNECT')
       OR NOT pg_catalog.has_schema_privilege(client_oid, 'yoao_audit_v1', 'USAGE')
       OR NOT pg_catalog.has_function_privilege(client_oid, append_function, 'EXECUTE') THEN
        RAISE EXCEPTION 'Audit append client does not have the exact function-only profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace IN ('public'::regnamespace, 'yoao_audit_v1'::regnamespace)
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (
              pg_catalog.has_table_privilege(
                  client_oid, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
              OR pg_catalog.has_any_column_privilege(
                  client_oid, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
              )
          )
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_class AS public_sequence
        WHERE public_sequence.relnamespace = 'public'::regnamespace
          AND CASE WHEN public_sequence.relkind = 'S'
              THEN pg_catalog.has_sequence_privilege(
                  client_oid, public_sequence.oid, 'USAGE,SELECT,UPDATE'
              )
              ELSE false END
    ) THEN
        RAISE EXCEPTION 'Audit append client has privileges outside its function';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(api_oid, 'yoao_audit_v1', 'USAGE')
       OR NOT pg_catalog.has_function_privilege(api_oid, append_function, 'EXECUTE')
       OR pg_catalog.has_sequence_privilege(api_oid, audit_sequence, 'USAGE,SELECT,UPDATE')
       OR EXISTS (
            SELECT FROM pg_catalog.pg_class AS relation
            WHERE (relation.oid IN (
                SELECT relid FROM pg_catalog.pg_partition_tree('public.audit_logs')
            ) OR relation.oid IN ('public.mv_audit_bi_daily'::regclass,
                                  'public.audit_archive_outputs'::regclass))
              AND (
                  pg_catalog.has_table_privilege(api_oid, relation.oid,
                      'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                  OR pg_catalog.has_any_column_privilege(api_oid, relation.oid,
                      'SELECT,INSERT,UPDATE,REFERENCES')
              )
       ) THEN
        RAISE EXCEPTION 'API role is missing append or retains direct Audit storage access';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS schema ON schema.oid = function.pronamespace
        WHERE schema.nspname = 'yoao_audit_v1'
          AND pg_catalog.has_function_privilege(client_oid, function.oid, 'EXECUTE')
          AND function.oid <> append_function
    ) THEN
        RAISE EXCEPTION 'Audit append client has an extra Audit function grant';
    END IF;
END $$;
\endif

\if :{?grant_maintenance}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config('yoao.maintenance_role', :'maintenance_role', true);

DO $guard$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.audit_owner'));
    v_maintenance oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.maintenance_role'));
    v_function oid := pg_catalog.to_regprocedure(
        'yoao_audit_v1.maintenance_command(jsonb)'
    );
BEGIN
    IF current_database()<>current_setting('yoao.expected_database')
       OR current_user<>current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF v_owner IS NULL OR v_maintenance IS NULL
       OR v_owner=v_maintenance
       OR current_user::pg_catalog.regrole=v_maintenance THEN
        RAISE EXCEPTION 'Audit maintenance roles are missing or overlap';
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles AS role
        WHERE role.oid=v_owner AND NOT role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_roles AS role
        WHERE role.oid=v_maintenance AND role.rolcanlogin AND NOT role.rolinherit
          AND NOT (role.rolsuper OR role.rolcreatedb OR role.rolcreaterole
                  OR role.rolreplication OR role.rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'Unsafe Audit maintenance role profile';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member=v_maintenance OR membership.roleid=v_maintenance
           OR membership.member=v_owner
           OR (membership.roleid=v_owner
               AND membership.member<>current_user::pg_catalog.regrole)
    ) THEN
        RAISE EXCEPTION 'Audit owner and maintenance LOGIN have unsafe membership';
    END IF;
    IF v_function IS NULL OR NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        WHERE function.oid=v_function
          AND function.proowner=v_owner
          AND function.prosecdef
          AND function.provolatile='v'
          AND function.prokind='f'
          AND pg_catalog.cardinality(function.proconfig)=2
          AND function.proconfig @> ARRAY[
              'search_path=pg_catalog, pg_temp', 'TimeZone=UTC'
          ]::text[]
    ) THEN
        RAISE EXCEPTION 'Audit maintenance command is missing or changed';
    END IF;
    IF NOT pg_catalog.has_database_privilege(v_owner, current_database(), 'TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_owner, 'public', 'CREATE')
       OR pg_catalog.has_schema_privilege(v_owner, 'yoao_audit_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Audit owner is missing its bounded partition and refresh rights';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_default_acl AS defaults
        CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl
        WHERE defaults.defaclrole=v_owner
          AND defaults.defaclnamespace IN (0,'public'::pg_catalog.regnamespace)
          AND defaults.defaclobjtype IN ('r','S')
          AND acl.grantee<>v_owner
    ) THEN
        RAISE EXCEPTION 'Audit owner default privileges expose future private storage';
    END IF;
END;
$guard$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"maintenance_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"maintenance_role";
DO $revoke$
DECLARE
    v_schema record;
    v_role pg_catalog.name := current_setting('yoao.maintenance_role');
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

REVOKE ALL ON FUNCTION yoao_audit_v1.maintenance_command(jsonb)
    FROM :"maintenance_role", PUBLIC;
GRANT USAGE ON SCHEMA yoao_audit_v1 TO :"maintenance_role";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.maintenance_command(jsonb)
    TO :"maintenance_role";

DO $verify$
DECLARE
    v_owner oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.audit_owner'));
    v_maintenance oid := (SELECT oid FROM pg_catalog.pg_roles
                          WHERE rolname=current_setting('yoao.maintenance_role'));
    v_allowed oid := pg_catalog.to_regprocedure(
        'yoao_audit_v1.maintenance_command(jsonb)'
    );
    v_relation record;
BEGIN
    IF v_allowed IS NULL
       OR NOT pg_catalog.has_database_privilege(v_maintenance,current_database(),'CONNECT')
       OR pg_catalog.has_database_privilege(v_maintenance,current_database(),'CREATE,TEMP')
       OR NOT pg_catalog.has_schema_privilege(v_maintenance,'yoao_audit_v1','USAGE')
       OR pg_catalog.has_schema_privilege(v_maintenance,'yoao_audit_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_maintenance,'public','CREATE')
       OR NOT pg_catalog.has_function_privilege(v_maintenance,v_allowed,'EXECUTE') THEN
        RAISE EXCEPTION 'Audit maintenance LOGIN is missing its exact function profile';
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
                     AND pg_catalog.pg_has_role(v_maintenance,acl.grantee,'USAGE')
               ))
          AND function.oid<>v_allowed
          AND pg_catalog.has_function_privilege(v_maintenance,function.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Audit maintenance LOGIN can execute another module function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_maintenance,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_audit_v1')
                   AND pg_catalog.has_schema_privilege(v_maintenance,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Audit maintenance LOGIN has foreign schema privileges';
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
                pg_catalog.has_table_privilege(v_maintenance,v_relation.oid,
                    'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR pg_catalog.has_any_column_privilege(v_maintenance,v_relation.oid,
                    'SELECT,INSERT,UPDATE,REFERENCES')
            )) OR (v_relation.relkind = 'S' AND
                pg_catalog.has_sequence_privilege(v_maintenance,v_relation.oid,
                    'SELECT,UPDATE,USAGE')) THEN
            RAISE EXCEPTION 'Audit maintenance LOGIN has direct data privileges';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.relowner=v_maintenance
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspowner=v_maintenance
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_database AS database
        WHERE database.datdba=v_maintenance
    ) THEN
        RAISE EXCEPTION 'Audit maintenance LOGIN owns database objects';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(function.proacl,
            pg_catalog.acldefault('f',function.proowner))) AS acl
        WHERE function.oid=v_allowed AND acl.grantee=0
          AND acl.privilege_type='EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Audit maintenance command is executable by PUBLIC';
    END IF;
END;
$verify$;
\endif

\if :{?grant_query}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config('yoao.identity_owner', :'identity_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.query_client', :'query_client', true);

DO $guard$
DECLARE
    v_audit oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.audit_owner'));
    v_identity oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.identity_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.query_client'));
    v_query oid := pg_catalog.to_regprocedure('yoao_audit_v1.query_events(pg_catalog.jsonb)')::oid;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF v_audit IS NULL OR v_identity IS NULL OR v_api IS NULL OR v_client IS NULL
       OR v_query IS NULL OR v_audit IN (v_identity, v_api, v_client)
       OR v_identity IN (v_api, v_client) OR v_api = v_client THEN
        RAISE EXCEPTION 'Audit query roles or function are missing or overlap';
    END IF;
    IF (SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
               OR rolreplication OR rolbypassrls
        FROM pg_catalog.pg_roles WHERE oid=v_audit)
       OR (SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
                  OR rolreplication OR rolbypassrls
           FROM pg_catalog.pg_roles WHERE oid=v_identity) THEN
        RAISE EXCEPTION 'Audit and Identity code owners must be restricted NOLOGIN roles';
    END IF;
    IF NOT (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
                   AND NOT rolcreaterole AND NOT rolreplication
                   AND NOT rolbypassrls AND NOT rolinherit
            FROM pg_catalog.pg_roles WHERE oid=v_client) THEN
        RAISE EXCEPTION 'Audit query client must be a restricted LOGIN NOINHERIT role';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members
        WHERE member IN (v_api, v_client) OR roleid IN (v_api, v_client)
    ) THEN
        RAISE EXCEPTION 'API and Audit query client roles must not have memberships';
    END IF;
    IF NOT (SELECT prosecdef AND proowner=v_audit
                   AND provolatile='s'
                   AND prorettype='pg_catalog.jsonb'::pg_catalog.regtype
                   AND proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
            FROM pg_catalog.pg_proc WHERE oid=v_query) THEN
        RAISE EXCEPTION 'Audit query function must be a fixed SECURITY DEFINER owned by Audit';
    END IF;
END;
$guard$;

REVOKE ALL ON SCHEMA yoao_audit_v1 FROM :"query_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_audit_v1 FROM :"query_client";
REVOKE ALL ON ALL TABLES IN SCHEMA yoao_audit_v1 FROM :"query_client";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"query_client";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"query_client";
DO $revoke$
DECLARE
    v_role name := current_setting('yoao.query_client');
    v_relation record;
    v_columns text;
BEGIN
    FOR v_relation IN
        SELECT relation.oid, namespace.nspname, relation.relname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=relation.relnamespace
        WHERE relation.relkind IN ('r','p','v','m','f')
          AND namespace.nspname IN ('public','yoao_audit_v1')
    LOOP
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attribute.attname), ',' ORDER BY attribute.attnum)
        INTO v_columns
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid=v_relation.oid
          AND attribute.attnum>0 AND NOT attribute.attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format(
                'REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns, v_relation.nspname, v_relation.relname, v_role
            );
        END IF;
    END LOOP;
END;
$revoke$;
GRANT CONNECT ON DATABASE :"expected_database" TO :"query_client";
GRANT USAGE ON SCHEMA yoao_audit_v1 TO :"query_client";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.query_events(pg_catalog.jsonb) TO :"query_client";
GRANT USAGE ON SCHEMA yoao_audit_v1 TO :"api_role";
GRANT EXECUTE ON FUNCTION yoao_audit_v1.query_events(pg_catalog.jsonb) TO :"api_role";

DO $verify$
DECLARE
    v_audit oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.audit_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.query_client'));
    v_query oid := pg_catalog.to_regprocedure('yoao_audit_v1.query_events(pg_catalog.jsonb)')::oid;
    v_append oid := pg_catalog.to_regprocedure('yoao_audit_v1.append_events(jsonb)')::oid;
BEGIN
    IF NOT pg_catalog.has_database_privilege(v_client, current_database(), 'CONNECT')
       OR NOT pg_catalog.has_schema_privilege(v_client, 'yoao_audit_v1', 'USAGE')
       OR NOT pg_catalog.has_function_privilege(v_client, v_query, 'EXECUTE')
       OR pg_catalog.has_function_privilege(v_client, v_append, 'EXECUTE')
       OR pg_catalog.has_table_privilege(
            v_client, 'public.audit_logs',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       )
       OR pg_catalog.has_any_column_privilege(
            v_client, 'public.audit_logs', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit query client is not limited to the read function';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=procedure.pronamespace
        WHERE namespace.nspname='yoao_audit_v1'
          AND pg_catalog.has_function_privilege(v_client, procedure.oid, 'EXECUTE')
          AND procedure.oid<>v_query
    ) THEN
        RAISE EXCEPTION 'Audit query client has another Audit function capability';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace IN ('public'::regnamespace, 'yoao_audit_v1'::regnamespace)
          AND relation.relkind IN ('r','p','v','m','f')
          AND (
              pg_catalog.has_table_privilege(
                  v_client, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
              OR pg_catalog.has_any_column_privilege(
                  v_client, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
              )
          )
    ) THEN
        RAISE EXCEPTION 'Audit query client has privileges on a public relation';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE (namespace.nspname LIKE 'aap\_%' ESCAPE '\'
               OR namespace.nspname LIKE 'yoao\_%' ESCAPE '\')
          AND namespace.nspname<>'yoao_audit_v1'
          AND pg_catalog.has_schema_privilege(v_client, namespace.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Audit query client has a foreign module schema grant';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace='public'::regnamespace
          AND CASE WHEN relation.relkind='S'
              THEN pg_catalog.has_sequence_privilege(
                  v_client, relation.oid, 'USAGE,SELECT,UPDATE'
              )
              ELSE false END
    ) THEN
        RAISE EXCEPTION 'Audit query client has public sequence privileges';
    END IF;
    IF pg_catalog.has_database_privilege(v_client, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(v_client, current_database(), 'TEMP')
       OR pg_catalog.has_schema_privilege(v_client, 'public', 'CREATE')
       OR pg_catalog.has_schema_privilege(v_client, 'yoao_audit_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Audit query client has database or schema creation privileges';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(v_api, 'yoao_audit_v1', 'USAGE')
       OR NOT pg_catalog.has_function_privilege(v_api, v_query, 'EXECUTE')
       OR pg_catalog.has_sequence_privilege(v_api,
            pg_catalog.pg_get_serial_sequence('public.audit_logs','id'), 'USAGE,SELECT,UPDATE')
       OR EXISTS (
            SELECT FROM pg_catalog.pg_class AS relation
            WHERE (relation.oid IN (
                SELECT relid FROM pg_catalog.pg_partition_tree('public.audit_logs')
            ) OR relation.oid IN ('public.mv_audit_bi_daily'::regclass,
                                  'public.audit_archive_outputs'::regclass))
              AND (
                  pg_catalog.has_table_privilege(v_api, relation.oid,
                      'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                  OR pg_catalog.has_any_column_privilege(v_api, relation.oid,
                      'SELECT,INSERT,UPDATE,REFERENCES')
              )
       ) THEN
        RAISE EXCEPTION 'API role is missing query execution or has direct Audit storage access';
    END IF;
    IF NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'actor_id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'actor_email', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'actor_role', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'action', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'target_type', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'target_id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'method', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'path', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'status_code', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'ip', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'detail_json', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'request_id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'created_at', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(
            v_audit, 'yoao_identity_v1.account_profiles', 'email', 'SELECT'
       ) THEN
        RAISE EXCEPTION 'Audit query owner is missing a required column read';
    END IF;
END;
$verify$;
\endif

\if :{?grant_read}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.audit_owner', :'audit_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.read_client', :'read_client', true);

DO $guard$
DECLARE
    v_audit oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.audit_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.read_client'));
    v_relation oid := pg_catalog.to_regclass('yoao_audit_v1.audit_events')::oid;
    v_daily oid := pg_catalog.to_regclass('yoao_audit_v1.audit_daily_facts')::oid;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF v_audit IS NULL OR v_api IS NULL OR v_client IS NULL
       OR v_audit IN (v_api, v_client) OR v_api = v_client
       OR v_relation IS NULL OR v_daily IS NULL THEN
        RAISE EXCEPTION 'Audit read roles or relation are missing or overlap';
    END IF;
    IF (SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
               OR rolreplication OR rolbypassrls
        FROM pg_catalog.pg_roles WHERE oid=v_audit) THEN
        RAISE EXCEPTION 'Audit code owner must be a restricted NOLOGIN role';
    END IF;
    IF NOT (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
                   AND NOT rolcreaterole AND NOT rolreplication
                   AND NOT rolbypassrls AND NOT rolinherit
            FROM pg_catalog.pg_roles WHERE oid=v_client) THEN
        RAISE EXCEPTION 'Audit read client must be a restricted LOGIN NOINHERIT role';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members
        WHERE member=v_client OR roleid=v_client
    ) THEN
        RAISE EXCEPTION 'Audit read client must not have role memberships';
    END IF;
    IF NOT (SELECT relkind='v'
                   AND relowner=v_audit
                   AND reloptions=ARRAY['security_barrier=true']::text[]
            FROM pg_catalog.pg_class WHERE oid=v_relation) THEN
        RAISE EXCEPTION 'Audit event relation must be an Audit-owned security-barrier view';
    END IF;
    IF NOT (SELECT relkind='v'
                   AND relowner=v_audit
                   AND reloptions=ARRAY['security_barrier=true']::text[]
            FROM pg_catalog.pg_class WHERE oid=v_daily)
       OR NOT (SELECT relkind='m' AND relowner=v_audit
               FROM pg_catalog.pg_class
               WHERE oid='public.mv_audit_bi_daily'::regclass) THEN
        RAISE EXCEPTION 'Audit daily projection and MV must share the Audit owner';
    END IF;
END;
$guard$;

REVOKE ALL ON SCHEMA yoao_audit_v1 FROM :"read_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_audit_v1 FROM :"read_client";
REVOKE ALL ON ALL TABLES IN SCHEMA yoao_audit_v1 FROM :"read_client";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"read_client";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"read_client";
DO $revoke_public_columns$
DECLARE
    v_role name := current_setting('yoao.read_client');
    v_relation record;
    v_columns text;
BEGIN
    FOR v_relation IN
        SELECT relation.oid, namespace.nspname, relation.relname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=relation.relnamespace
        WHERE relation.relkind IN ('r','p','v','m','f')
          AND namespace.nspname IN ('public','yoao_audit_v1')
    LOOP
        SELECT pg_catalog.string_agg(
                   pg_catalog.quote_ident(attribute.attname), ',' ORDER BY attribute.attnum
               )
        INTO v_columns
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid=v_relation.oid
          AND attribute.attnum>0 AND NOT attribute.attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format(
                'REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns, v_relation.nspname, v_relation.relname, v_role
            );
        END IF;
    END LOOP;
END;
$revoke_public_columns$;

GRANT CONNECT ON DATABASE :"expected_database" TO :"read_client";
GRANT USAGE ON SCHEMA yoao_audit_v1 TO :"read_client", :"api_role";
GRANT SELECT ON TABLE yoao_audit_v1.audit_events TO :"read_client", :"api_role";
GRANT SELECT ON TABLE yoao_audit_v1.audit_daily_facts TO :"read_client", :"api_role";
REVOKE ALL ON TABLE public.mv_audit_bi_daily FROM :"api_role", PUBLIC;
REVOKE ALL (day, action, target_type, actor_role, status_family, event_count)
    ON TABLE public.mv_audit_bi_daily FROM :"api_role", PUBLIC;
REVOKE EXECUTE ON FUNCTION yoao_audit_v1.refresh_audit_bi_mv()
    FROM :"api_role", PUBLIC;

DO $verify$
DECLARE
    v_audit oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.audit_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.read_client'));
    v_relation oid := pg_catalog.to_regclass('yoao_audit_v1.audit_events')::oid;
    v_daily oid := pg_catalog.to_regclass('yoao_audit_v1.audit_daily_facts')::oid;
    v_refresh oid := pg_catalog.to_regprocedure('yoao_audit_v1.refresh_audit_bi_mv()')::oid;
    v_append oid := pg_catalog.to_regprocedure('yoao_audit_v1.append_events(jsonb)')::oid;
    v_query oid := pg_catalog.to_regprocedure('yoao_audit_v1.query_events(pg_catalog.jsonb)')::oid;
BEGIN
    IF NOT pg_catalog.has_database_privilege(v_client, current_database(), 'CONNECT')
       OR v_append IS NULL OR v_query IS NULL OR v_refresh IS NULL
       OR NOT pg_catalog.has_schema_privilege(v_client, 'yoao_audit_v1', 'USAGE')
       OR NOT pg_catalog.has_table_privilege(v_client, v_relation, 'SELECT')
       OR NOT pg_catalog.has_table_privilege(v_client, v_daily, 'SELECT')
       OR pg_catalog.has_function_privilege(v_client, v_append, 'EXECUTE')
       OR pg_catalog.has_function_privilege(v_client, v_query, 'EXECUTE')
       OR pg_catalog.has_function_privilege(v_client, v_refresh, 'EXECUTE')
       OR pg_catalog.has_table_privilege(
            v_client, v_relation,
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       )
       OR pg_catalog.has_any_column_privilege(
            v_client, v_relation, 'INSERT,UPDATE,REFERENCES'
       )
       OR pg_catalog.has_table_privilege(
            v_client, v_daily,
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       )
       OR pg_catalog.has_any_column_privilege(
            v_client, v_daily, 'INSERT,UPDATE,REFERENCES'
       )
       OR pg_catalog.has_table_privilege(
            v_client, 'public.audit_logs',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       )
       OR pg_catalog.has_any_column_privilege(
            v_client, 'public.audit_logs', 'SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Audit read client is not limited to relation SELECT';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace='public'::regnamespace
          AND relation.relkind IN ('r','p','v','m','f')
          AND (
              pg_catalog.has_table_privilege(
                  v_client, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
              )
              OR pg_catalog.has_any_column_privilege(
                  v_client, relation.oid, 'SELECT,INSERT,UPDATE,REFERENCES'
              )
          )
    ) THEN
        RAISE EXCEPTION 'Audit read client has privileges on a public relation';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.relnamespace='public'::regnamespace
          AND CASE WHEN relation.relkind='S'
              THEN pg_catalog.has_sequence_privilege(
                  v_client, relation.oid, 'USAGE,SELECT,UPDATE'
              )
              ELSE false END
    ) THEN
        RAISE EXCEPTION 'Audit read client has public sequence privileges';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE (namespace.nspname LIKE 'aap\_%' ESCAPE '\'
               OR namespace.nspname LIKE 'yoao\_%' ESCAPE '\')
          AND namespace.nspname<>'yoao_audit_v1'
          AND pg_catalog.has_schema_privilege(v_client, namespace.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Audit read client has a foreign module schema grant';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=procedure.pronamespace
        WHERE namespace.nspname='yoao_audit_v1'
          AND pg_catalog.has_function_privilege(v_client, procedure.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Audit read client has an Audit function capability';
    END IF;
    IF pg_catalog.has_database_privilege(v_client, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(v_client, current_database(), 'TEMP')
       OR pg_catalog.has_schema_privilege(v_client, 'public', 'CREATE')
       OR pg_catalog.has_schema_privilege(v_client, 'yoao_audit_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Audit read client has database or schema creation privileges';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(v_api, 'yoao_audit_v1', 'USAGE')
       OR NOT pg_catalog.has_table_privilege(v_api, v_relation, 'SELECT')
       OR NOT pg_catalog.has_table_privilege(v_api, v_daily, 'SELECT')
       OR pg_catalog.has_table_privilege(v_api, v_relation,
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_api, v_relation,
            'INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_table_privilege(
            v_api, v_daily,
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       )
       OR pg_catalog.has_any_column_privilege(
            v_api, v_daily, 'INSERT,UPDATE,REFERENCES'
       )
       OR pg_catalog.has_sequence_privilege(v_api,
            pg_catalog.pg_get_serial_sequence('public.audit_logs','id'), 'USAGE,SELECT,UPDATE')
       OR EXISTS (
            SELECT FROM pg_catalog.pg_class AS relation
            WHERE (relation.oid IN (
                SELECT relid FROM pg_catalog.pg_partition_tree('public.audit_logs')
            ) OR relation.oid IN ('public.mv_audit_bi_daily'::regclass,
                                  'public.audit_archive_outputs'::regclass))
              AND (
                  pg_catalog.has_table_privilege(v_api, relation.oid,
                      'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                  OR pg_catalog.has_any_column_privilege(v_api, relation.oid,
                      'SELECT,INSERT,UPDATE,REFERENCES')
              )
       )
       OR pg_catalog.has_function_privilege(v_api, v_refresh, 'EXECUTE') THEN
        RAISE EXCEPTION 'API role is missing read-only Audit relations or has direct Audit storage access';
    END IF;
    IF NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'actor_id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'actor_email', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'actor_role', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'action', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'target_type', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'target_id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'method', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'path', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'status_code', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'ip', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'detail_json', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'request_id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_audit, 'public.audit_logs', 'created_at', 'SELECT') THEN
        RAISE EXCEPTION 'Audit owner is missing a required source column read';
    END IF;
END;
$verify$;
\endif

DO $$
BEGIN
    IF current_setting('yoao.grant_section_applied', true) IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION 'No capability section selected; pass -v grant_<capability>=1';
    END IF;
END $$;
COMMIT;
