\set ON_ERROR_STOP on
BEGIN;
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
COMMIT;
