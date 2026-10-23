-- Grant one restricted client the fixed Audit read command only.
-- Run after migrations and module-data-grants.sql.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v audit_owner=audit_code -v identity_owner=identity_code \
--   -v api_role=api_runtime -v query_client=audit_query_client \
--   -f infra/docker/audit-query-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.audit_owner', :'audit_owner', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.api_role', :'api_role', true);
SELECT set_config('aap.query_client', :'query_client', true);

DO $guard$
DECLARE
    v_audit oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.audit_owner'));
    v_identity oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.identity_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.query_client'));
    v_query oid := pg_catalog.to_regprocedure('aap_audit_v1.query_events(pg_catalog.jsonb)')::oid;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
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

-- A query LOGIN gets only the fixed owner function, no public-table or append access.
REVOKE ALL ON SCHEMA aap_audit_v1 FROM :"query_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_audit_v1 FROM :"query_client";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"query_client";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"query_client";
DO $revoke$
DECLARE
    v_role name := current_setting('aap.query_client');
    v_relation record;
    v_columns text;
BEGIN
    FOR v_relation IN
        SELECT relation.oid, namespace.nspname, relation.relname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=relation.relnamespace
        WHERE relation.relkind IN ('r','p','v','m','f')
          AND namespace.nspname='public'
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
GRANT USAGE ON SCHEMA aap_audit_v1 TO :"query_client";
GRANT EXECUTE ON FUNCTION aap_audit_v1.query_events(pg_catalog.jsonb) TO :"query_client";
GRANT USAGE ON SCHEMA aap_audit_v1 TO :"api_role";
GRANT EXECUTE ON FUNCTION aap_audit_v1.query_events(pg_catalog.jsonb) TO :"api_role";

DO $verify$
DECLARE
    v_audit oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.audit_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.query_client'));
    v_query oid := pg_catalog.to_regprocedure('aap_audit_v1.query_events(pg_catalog.jsonb)')::oid;
    v_append oid := pg_catalog.to_regprocedure('aap_audit_v1.append_events(jsonb)')::oid;
BEGIN
    IF NOT pg_catalog.has_database_privilege(v_client, current_database(), 'CONNECT')
       OR NOT pg_catalog.has_schema_privilege(v_client, 'aap_audit_v1', 'USAGE')
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
        WHERE namespace.nspname='aap_audit_v1'
          AND pg_catalog.has_function_privilege(v_client, procedure.oid, 'EXECUTE')
          AND procedure.oid<>v_query
    ) THEN
        RAISE EXCEPTION 'Audit query client has another Audit function capability';
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
        RAISE EXCEPTION 'Audit query client has privileges on a public relation';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname LIKE 'aap\_%' ESCAPE '\'
          AND namespace.nspname<>'aap_audit_v1'
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
       OR pg_catalog.has_schema_privilege(v_client, 'aap_audit_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Audit query client has database or schema creation privileges';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(v_api, 'aap_audit_v1', 'USAGE')
       OR NOT pg_catalog.has_function_privilege(v_api, v_query, 'EXECUTE')
       OR NOT pg_catalog.has_table_privilege(v_api, 'public.audit_logs', 'SELECT') THEN
        RAISE EXCEPTION 'API role is missing query execution or existing Audit reads';
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
            v_audit, 'aap_identity_v1.account_profiles', 'email', 'SELECT'
       ) THEN
        RAISE EXCEPTION 'Audit query owner is missing a required column read';
    END IF;
END;
$verify$;
COMMIT;
