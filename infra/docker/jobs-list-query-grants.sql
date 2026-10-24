-- Grant the two Jobs list reads to the API and one function-only LOGIN.
-- Run after migration 0326 and module-data-grants.sql as the migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v jobs_owner=jobs_code -v api_role=api_runtime \
--   -v list_client=jobs_list_query_client \
--   -f infra/docker/jobs-list-query-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.jobs_owner', :'jobs_owner', true);
SELECT set_config('aap.api_role', :'api_role', true);
SELECT set_config('aap.list_client', :'list_client', true);

DO $guard$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles
                   WHERE rolname=current_setting('aap.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('aap.list_client'));
    v_function record;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF v_jobs IS NULL OR v_api IS NULL OR v_client IS NULL
       OR v_jobs IN (v_api, v_client) OR v_api = v_client THEN
        RAISE EXCEPTION 'Jobs list roles are missing or overlap';
    END IF;
    IF NOT (SELECT NOT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
                   AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
            FROM pg_catalog.pg_roles WHERE oid=v_jobs)
       OR NOT (SELECT rolcanlogin AND NOT rolsuper AND NOT rolcreatedb
                   AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls
               FROM pg_catalog.pg_roles WHERE oid=v_api)
       OR NOT (SELECT rolcanlogin AND NOT rolinherit AND NOT rolsuper
                   AND NOT rolcreatedb AND NOT rolcreaterole
                   AND NOT rolreplication AND NOT rolbypassrls
               FROM pg_catalog.pg_roles WHERE oid=v_client)
       OR EXISTS (
            SELECT FROM pg_catalog.pg_auth_members
            WHERE member IN (v_jobs,v_api,v_client)
               OR roleid IN (v_api,v_client)
               OR (roleid=v_jobs AND member<>current_user::pg_catalog.regrole)
       ) THEN
        RAISE EXCEPTION 'Unsafe Jobs owner or list-query LOGIN';
    END IF;
    FOR v_function IN
        SELECT * FROM (VALUES
            ('aap_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer)',
                'record', true),
            ('aap_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)',
                'bigint', false),
            ('aap_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)',
                'public.async_jobs', true)
        ) AS expected(signature, return_type, returns_set)
    LOOP
        IF NOT EXISTS (
            SELECT FROM pg_catalog.pg_proc AS function
            WHERE function.oid=pg_catalog.to_regprocedure(v_function.signature)
              AND function.proowner=v_jobs
              AND function.prosecdef
              AND function.provolatile='s'
              AND function.prokind='f'
              AND function.prorettype=pg_catalog.to_regtype(v_function.return_type)
              AND function.proretset=v_function.returns_set
              AND function.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
        ) THEN
            RAISE EXCEPTION 'Jobs list function ownership or fixed signature differs';
        END IF;
    END LOOP;
END;
$guard$;

GRANT USAGE ON SCHEMA aap_workspace_v1, aap_workspace_project_query_v1
    TO :"jobs_owner";
GRANT USAGE ON SCHEMA aap_identity_v1 TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.actor_project_memberships(uuid),
    aap_workspace_project_query_v1.project_search_scope(uuid,text)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_identity_v1.read_actor_facts(uuid)
    TO :"jobs_owner";

REVOKE ALL ON SCHEMA aap_jobs_v1 FROM :"list_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_jobs_v1 FROM :"list_client";
REVOKE ALL ON ALL TABLES IN SCHEMA aap_jobs_v1, public FROM :"list_client";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"list_client";
DO $revoke_columns$
DECLARE
    v_relation record;
    v_columns text;
BEGIN
    FOR v_relation IN
        SELECT relation.oid, namespace.nspname, relation.relname
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname IN ('public', 'aap_jobs_v1')
          AND relation.relkind IN ('r','p','v','m','f')
    LOOP
        SELECT pg_catalog.string_agg(
                   pg_catalog.quote_ident(attname), ',' ORDER BY attnum
               )
        INTO v_columns
        FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format(
                'REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns,
                v_relation.nspname,
                v_relation.relname,
                current_setting('aap.list_client')
            );
        END IF;
    END LOOP;
END;
$revoke_columns$;
REVOKE ALL ON FUNCTION
    aap_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)
    FROM :"api_role", :"list_client", PUBLIC;
REVOKE ALL ON FUNCTION
    aap_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer),
    aap_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)
    FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"list_client";
GRANT USAGE ON SCHEMA aap_jobs_v1 TO :"api_role", :"list_client";
GRANT EXECUTE ON FUNCTION
    aap_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer),
    aap_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)
    TO :"api_role", :"list_client";

DO $verify$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles
                   WHERE rolname=current_setting('aap.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('aap.list_client'));
    v_allowed oid[] := ARRAY[
        'aap_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer)'::regprocedure::oid,
        'aap_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)'::regprocedure::oid
    ];
BEGIN
    IF EXISTS (
        SELECT FROM pg_catalog.unnest(v_allowed) AS allowed(function_oid)
        WHERE NOT pg_catalog.has_function_privilege(v_api,function_oid,'EXECUTE')
           OR NOT pg_catalog.has_function_privilege(v_client,function_oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Missing fixed Jobs list function EXECUTE';
    END IF;
    IF pg_catalog.has_function_privilege(v_api,
            'aap_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)','EXECUTE')
       OR pg_catalog.has_function_privilege(v_client,
            'aap_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)','EXECUTE')
       OR pg_catalog.has_table_privilege(v_client,'public.async_jobs','SELECT')
       OR pg_catalog.has_any_column_privilege(v_client,'public.async_jobs','SELECT')
       OR NOT pg_catalog.has_function_privilege(v_jobs,
            'aap_identity_v1.read_actor_facts(uuid)','EXECUTE')
       OR pg_catalog.has_table_privilege(v_jobs,'public.users','SELECT')
       OR pg_catalog.has_any_column_privilege(v_jobs,'public.users','SELECT')
       OR pg_catalog.has_table_privilege(v_jobs,
            'aap_identity_v1.account_profiles','SELECT')
       OR pg_catalog.has_any_column_privilege(v_jobs,
            'aap_identity_v1.account_profiles','SELECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP') THEN
        RAISE EXCEPTION 'Jobs list profile exposes private reads or a helper';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=function.pronamespace
        WHERE namespace.nspname LIKE 'aap\_%' ESCAPE '\'
          AND function.oid<>ALL(v_allowed)
          AND pg_catalog.has_function_privilege(v_client,function.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Jobs list client has unrelated AAP function EXECUTE';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND ((relation.relkind IN ('r','p','v','m','f')
                AND (pg_catalog.has_table_privilege(
                         v_client,relation.oid,
                         'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                     )
                     OR pg_catalog.has_any_column_privilege(
                         v_client,relation.oid,'SELECT,INSERT,UPDATE,REFERENCES'
                     )))
               OR (relation.relkind='S' AND pg_catalog.has_sequence_privilege(
                    v_client,relation.oid,'USAGE,SELECT,UPDATE')))
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','aap_jobs_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Jobs list client has unrelated data or schema privileges';
    END IF;
END;
$verify$;
COMMIT;
