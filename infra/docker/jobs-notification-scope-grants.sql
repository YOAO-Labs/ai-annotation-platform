-- Publish only Jobs' notification-scope facts to the API and one read-only peer.
-- Run after migrations and module-data-grants.sql as the migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v jobs_owner=jobs_code -v api_role=api_runtime \
--   -v read_client=jobs_notification_scope_client \
--   -f infra/docker/jobs-notification-scope-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.jobs_owner', :'jobs_owner', true);
SELECT set_config('aap.api_role', :'api_role', true);
SELECT set_config('aap.read_client', :'read_client', true);

DO $guard$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.read_client'));
    v_relation oid := pg_catalog.to_regclass('aap_jobs_v1.notification_scope_facts')::oid;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF v_jobs IS NULL OR v_api IS NULL OR v_client IS NULL OR v_relation IS NULL
       OR v_jobs IN (v_api, v_client) OR v_api=v_client THEN
        RAISE EXCEPTION 'Jobs fact roles or relation are missing or overlap';
    END IF;
    IF NOT (SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
                   AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
            FROM pg_catalog.pg_roles WHERE oid=v_jobs)
       OR NOT (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
                   AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
                   AND NOT rolinherit
               FROM pg_catalog.pg_roles WHERE oid=v_client)
       OR EXISTS (
            SELECT FROM pg_catalog.pg_auth_members
            WHERE member=v_client OR roleid=v_client
       ) THEN
        RAISE EXCEPTION 'Unsafe Jobs owner or fact reader';
    END IF;
    IF NOT (SELECT relkind='v' AND relowner=v_jobs
                   AND reloptions=ARRAY['security_barrier=true']::text[]
            FROM pg_catalog.pg_class WHERE oid=v_relation) THEN
        RAISE EXCEPTION 'Jobs fact relation must be an owner security-barrier view';
    END IF;
    IF NOT pg_catalog.has_column_privilege(v_jobs, 'public.async_jobs', 'id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_jobs, 'public.async_jobs', 'kind', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_jobs, 'public.async_jobs', 'project_id', 'SELECT')
       OR NOT pg_catalog.has_column_privilege(v_jobs, 'public.async_jobs', 'retained_project_id', 'SELECT') THEN
        RAISE EXCEPTION 'Jobs owner is missing a required source column';
    END IF;
END;
$guard$;

REVOKE ALL ON SCHEMA aap_jobs_v1 FROM :"read_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_jobs_v1 FROM :"read_client";
REVOKE ALL ON ALL TABLES IN SCHEMA aap_jobs_v1 FROM :"read_client";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"read_client";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"read_client";
DO $revoke_columns$
DECLARE
    v_relation record;
    v_columns text;
BEGIN
    FOR v_relation IN
        SELECT relation.oid, namespace.nspname, relation.relname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname IN ('public', 'aap_jobs_v1')
          AND relation.relkind IN ('r','p','v','m','f')
    LOOP
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname), ',' ORDER BY attnum)
        INTO v_columns
        FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns, v_relation.nspname, v_relation.relname,
                current_setting('aap.read_client'));
        END IF;
    END LOOP;
END;
$revoke_columns$;

REVOKE ALL ON TABLE aap_jobs_v1.notification_scope_facts FROM :"api_role", PUBLIC;
REVOKE ALL (id, kind, scope_project_id)
    ON TABLE aap_jobs_v1.notification_scope_facts FROM :"api_role", PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"read_client";
GRANT USAGE ON SCHEMA aap_jobs_v1 TO :"read_client", :"api_role";
GRANT SELECT ON aap_jobs_v1.notification_scope_facts TO :"read_client", :"api_role";

DO $verify$
DECLARE
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('aap.read_client'));
    v_relation oid := 'aap_jobs_v1.notification_scope_facts'::regclass;
BEGIN
    IF NOT pg_catalog.has_table_privilege(v_client, v_relation, 'SELECT')
       OR NOT pg_catalog.has_table_privilege(v_api, v_relation, 'SELECT')
       OR pg_catalog.has_table_privilege(v_client, v_relation,
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_client, v_relation, 'INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_table_privilege(v_api, v_relation,
            'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_api, v_relation, 'INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_database_privilege(v_client, current_database(), 'CREATE,TEMP')
       OR EXISTS (
            SELECT FROM pg_catalog.pg_auth_members WHERE member=v_client OR roleid=v_client
       ) THEN
        RAISE EXCEPTION 'Jobs fact reader is not limited to relation SELECT';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.oid<>v_relation
          AND ((relation.relkind IN ('r','p','v','m','f')
                AND (pg_catalog.has_table_privilege(v_client, relation.oid,
                        'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR pg_catalog.has_any_column_privilege(v_client, relation.oid,
                        'SELECT,INSERT,UPDATE,REFERENCES')))
               OR (relation.relkind='S' AND pg_catalog.has_sequence_privilege(
                    v_client, relation.oid, 'USAGE,SELECT,UPDATE')))
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace
        WHERE namespace.nspname LIKE 'aap\_%' ESCAPE '\'
          AND pg_catalog.has_function_privilege(v_client, function.oid, 'EXECUTE')
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client, namespace.oid, 'CREATE')
               OR (namespace.nspname NOT IN ('public','aap_jobs_v1')
                   AND pg_catalog.has_schema_privilege(v_client, namespace.oid, 'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Jobs fact reader has unrelated data, function or schema privileges';
    END IF;
END;
$verify$;
COMMIT;
