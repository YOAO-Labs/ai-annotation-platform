-- Give one restricted LOGIN SELECT on Audit's published event and daily facts only.
-- Run after migrations and module-data-grants.sql.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v audit_owner=audit_code -v api_role=api_runtime \
--   -v read_client=audit_read_client -f infra/docker/audit-read-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.audit_owner', :'audit_owner', true);
SELECT set_config('aap.api_role', :'api_role', true);
SELECT set_config('aap.read_client', :'read_client', true);

DO $guard$
DECLARE
    v_audit oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.audit_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.read_client'));
    v_relation oid := pg_catalog.to_regclass('aap_audit_v1.audit_events')::oid;
    v_daily oid := pg_catalog.to_regclass('aap_audit_v1.audit_daily_facts')::oid;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
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

-- Reset only this task-supplied LOGIN's relation/function grants. Column-level
-- public table rights are revoked explicitly because table ACL revocation does
-- not remove independently granted column privileges.
REVOKE ALL ON SCHEMA aap_audit_v1 FROM :"read_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_audit_v1 FROM :"read_client";
REVOKE ALL ON ALL TABLES IN SCHEMA aap_audit_v1 FROM :"read_client";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"read_client";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"read_client";
DO $revoke_public_columns$
DECLARE
    v_role name := current_setting('aap.read_client');
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
GRANT USAGE ON SCHEMA aap_audit_v1 TO :"read_client", :"api_role";
GRANT SELECT ON TABLE aap_audit_v1.audit_events TO :"read_client", :"api_role";
GRANT SELECT ON TABLE aap_audit_v1.audit_daily_facts TO :"read_client", :"api_role";
REVOKE ALL ON TABLE public.mv_audit_bi_daily FROM :"api_role", PUBLIC;
REVOKE ALL (day, action, target_type, actor_role, status_family, event_count)
    ON TABLE public.mv_audit_bi_daily FROM :"api_role", PUBLIC;
REVOKE EXECUTE ON FUNCTION aap_audit_v1.refresh_audit_bi_mv()
    FROM :"api_role", PUBLIC;

DO $verify$
DECLARE
    v_audit oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.audit_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.read_client'));
    v_relation oid := pg_catalog.to_regclass('aap_audit_v1.audit_events')::oid;
    v_daily oid := pg_catalog.to_regclass('aap_audit_v1.audit_daily_facts')::oid;
    v_refresh oid := pg_catalog.to_regprocedure('aap_audit_v1.refresh_audit_bi_mv()')::oid;
    v_append oid := pg_catalog.to_regprocedure('aap_audit_v1.append_events(jsonb)')::oid;
    v_query oid := pg_catalog.to_regprocedure('aap_audit_v1.query_events(pg_catalog.jsonb)')::oid;
BEGIN
    IF NOT pg_catalog.has_database_privilege(v_client, current_database(), 'CONNECT')
       OR v_append IS NULL OR v_query IS NULL OR v_refresh IS NULL
       OR NOT pg_catalog.has_schema_privilege(v_client, 'aap_audit_v1', 'USAGE')
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
        WHERE namespace.nspname LIKE 'aap\_%' ESCAPE '\'
          AND namespace.nspname<>'aap_audit_v1'
          AND pg_catalog.has_schema_privilege(v_client, namespace.oid, 'USAGE')
    ) THEN
        RAISE EXCEPTION 'Audit read client has a foreign module schema grant';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS procedure
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=procedure.pronamespace
        WHERE namespace.nspname='aap_audit_v1'
          AND pg_catalog.has_function_privilege(v_client, procedure.oid, 'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Audit read client has an Audit function capability';
    END IF;
    IF pg_catalog.has_database_privilege(v_client, current_database(), 'CREATE')
       OR pg_catalog.has_database_privilege(v_client, current_database(), 'TEMP')
       OR pg_catalog.has_schema_privilege(v_client, 'public', 'CREATE')
       OR pg_catalog.has_schema_privilege(v_client, 'aap_audit_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Audit read client has database or schema creation privileges';
    END IF;
    IF NOT pg_catalog.has_schema_privilege(v_api, 'aap_audit_v1', 'USAGE')
       OR NOT pg_catalog.has_table_privilege(v_api, v_relation, 'SELECT')
       OR NOT pg_catalog.has_table_privilege(v_api, v_daily, 'SELECT')
       OR pg_catalog.has_table_privilege(
            v_api, v_daily,
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       )
       OR pg_catalog.has_any_column_privilege(
            v_api, v_daily, 'INSERT,UPDATE,REFERENCES'
       )
       OR pg_catalog.has_table_privilege(
            v_api, 'public.mv_audit_bi_daily',
            'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       )
       OR pg_catalog.has_any_column_privilege(
            v_api, 'public.mv_audit_bi_daily', 'SELECT,INSERT,UPDATE,REFERENCES'
       )
       OR pg_catalog.has_function_privilege(v_api, v_refresh, 'EXECUTE')
       OR NOT pg_catalog.has_table_privilege(v_api, 'public.audit_logs', 'SELECT') THEN
        RAISE EXCEPTION 'API role is missing relation access or existing Audit reads';
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
COMMIT;
