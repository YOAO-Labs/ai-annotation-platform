-- Grant the fixed Jobs lifecycle capabilities to the API and one function-only LOGIN.
-- Run after migration 0327 and module-data-grants.sql as the migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v jobs_owner=jobs_code -v api_role=api_runtime \
--   -v lifecycle_client=jobs_lifecycle_client \
--   -f infra/docker/jobs-lifecycle-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.jobs_owner', :'jobs_owner', true);
SELECT set_config('aap.api_role', :'api_role', true);
SELECT set_config('aap.lifecycle_client', :'lifecycle_client', true);

DO $guard$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles
                   WHERE rolname=current_setting('aap.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('aap.lifecycle_client'));
    v_function record;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF v_jobs IS NULL OR v_api IS NULL OR v_client IS NULL
       OR v_jobs IN (v_api, v_client) OR v_api = v_client THEN
        RAISE EXCEPTION 'Jobs lifecycle roles are missing or overlap';
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
        RAISE EXCEPTION 'Unsafe Jobs owner or lifecycle LOGIN';
    END IF;
    FOR v_function IN
        SELECT * FROM (VALUES
            ('aap_jobs_v1.read_job(uuid)','record',true,'s'),
            ('aap_jobs_v1.read_job_execution_snapshot(uuid)','uuid',false,'s'),
            ('aap_jobs_v1.read_job_progress(uuid)','record',true,'s'),
            ('aap_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid)','boolean',false,'v'),
            ('aap_jobs_v1.jobs_by_execution_id(text)','record',true,'s'),
            ('aap_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text)','boolean',false,'v'),
            ('aap_jobs_v1.apply_lifecycle(uuid,jsonb)','record',true,'v'),
            ('aap_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text)','boolean',false,'v'),
            ('aap_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz)','uuid[]',false,'v'),
            ('aap_jobs_v1.delete_batch_predict_jobs(uuid)','uuid[]',false,'v'),
            ('aap_jobs_v1.delete_terminal_jobs_before(timestamptz,text[])','uuid[]',false,'v')
        ) AS expected(signature, return_type, returns_set, volatility)
    LOOP
        IF NOT EXISTS (
            SELECT FROM pg_catalog.pg_proc AS function
            WHERE function.oid=pg_catalog.to_regprocedure(v_function.signature)
              AND function.proowner=v_jobs
              AND function.prosecdef
              AND function.provolatile=v_function.volatility::"char"
              AND function.prokind='f'
              AND function.prorettype=pg_catalog.to_regtype(v_function.return_type)
              AND function.proretset=v_function.returns_set
              AND function.proconfig=CASE
                  WHEN v_function.signature='aap_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid)'
                  THEN ARRAY['search_path=pg_catalog, pg_temp','TimeZone=UTC']::text[]
                  ELSE ARRAY['search_path=pg_catalog, pg_temp']::text[]
              END
        ) THEN
            RAISE EXCEPTION 'Jobs lifecycle function ownership or fixed signature differs';
        END IF;
    END LOOP;
END;
$guard$;

DO $view_guard$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.oid='aap_jobs_v1.async_job_creation'::regclass
          AND relation.relkind='v'
          AND relation.relowner=current_setting('aap.jobs_owner')::regrole
          AND relation.reloptions @> ARRAY['check_option=local']::text[]
    ) THEN
        RAISE EXCEPTION 'Jobs creation relation ownership or check option differs';
    END IF;
END;
$view_guard$;

REVOKE ALL ON SCHEMA aap_jobs_v1 FROM :"lifecycle_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_jobs_v1 FROM :"lifecycle_client";
REVOKE ALL ON ALL TABLES IN SCHEMA aap_jobs_v1, public FROM :"lifecycle_client";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"lifecycle_client";
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
                current_setting('aap.lifecycle_client')
            );
        END IF;
    END LOOP;
END;
$revoke_columns$;
REVOKE ALL ON FUNCTION
    aap_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid),
    aap_jobs_v1.read_job_execution_snapshot(uuid),
    aap_jobs_v1.read_job_progress(uuid),
    aap_jobs_v1.read_job(uuid),
    aap_jobs_v1.jobs_by_execution_id(text),
    aap_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text),
    aap_jobs_v1.apply_lifecycle(uuid,jsonb),
    aap_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text),
    aap_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz),
    aap_jobs_v1.delete_batch_predict_jobs(uuid),
    aap_jobs_v1.delete_terminal_jobs_before(timestamptz,text[])
    FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"lifecycle_client";
GRANT USAGE ON SCHEMA aap_jobs_v1 TO :"api_role", :"lifecycle_client";
GRANT SELECT, INSERT ON aap_jobs_v1.async_job_creation
    TO :"api_role", :"lifecycle_client";
GRANT EXECUTE ON FUNCTION
    aap_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid),
    aap_jobs_v1.read_job_execution_snapshot(uuid),
    aap_jobs_v1.read_job_progress(uuid),
    aap_jobs_v1.read_job(uuid),
    aap_jobs_v1.jobs_by_execution_id(text),
    aap_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text),
    aap_jobs_v1.apply_lifecycle(uuid,jsonb),
    aap_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text),
    aap_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz),
    aap_jobs_v1.delete_batch_predict_jobs(uuid),
    aap_jobs_v1.delete_terminal_jobs_before(timestamptz,text[])
    TO :"api_role", :"lifecycle_client";

DO $verify$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles
                   WHERE rolname=current_setting('aap.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('aap.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('aap.lifecycle_client'));
    v_allowed oid[] := ARRAY[
        'aap_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid)'::regprocedure::oid,
        'aap_jobs_v1.read_job_execution_snapshot(uuid)'::regprocedure::oid,
        'aap_jobs_v1.read_job_progress(uuid)'::regprocedure::oid,
        'aap_jobs_v1.read_job(uuid)'::regprocedure::oid,
        'aap_jobs_v1.jobs_by_execution_id(text)'::regprocedure::oid,
        'aap_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text)'::regprocedure::oid,
        'aap_jobs_v1.apply_lifecycle(uuid,jsonb)'::regprocedure::oid,
        'aap_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text)'::regprocedure::oid,
        'aap_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz)'::regprocedure::oid,
        'aap_jobs_v1.delete_batch_predict_jobs(uuid)'::regprocedure::oid,
        'aap_jobs_v1.delete_terminal_jobs_before(timestamptz,text[])'::regprocedure::oid
    ];
BEGIN
    IF EXISTS (
        SELECT FROM pg_catalog.unnest(v_allowed) AS allowed(function_oid)
        WHERE NOT pg_catalog.has_function_privilege(v_api,function_oid,'EXECUTE')
           OR NOT pg_catalog.has_function_privilege(v_client,function_oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Missing fixed Jobs lifecycle function EXECUTE';
    END IF;
    IF pg_catalog.has_table_privilege(v_client,'public.async_jobs','SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_client,'public.async_jobs','SELECT,INSERT,UPDATE,REFERENCES')
       OR pg_catalog.has_table_privilege(v_client,'aap_jobs_v1.async_job_creation','UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_client,'aap_jobs_v1.async_job_creation','UPDATE,REFERENCES')
       OR NOT pg_catalog.has_table_privilege(v_client,'aap_jobs_v1.async_job_creation','SELECT')
       OR NOT pg_catalog.has_table_privilege(v_client,'aap_jobs_v1.async_job_creation','INSERT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP') THEN
        RAISE EXCEPTION 'Jobs lifecycle profile exposes private writes or unsafe creation';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=function.pronamespace
        WHERE namespace.nspname LIKE 'aap\_%' ESCAPE '\'
          AND function.oid<>ALL(v_allowed)
          AND pg_catalog.has_function_privilege(v_client,function.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Jobs lifecycle client has unrelated AAP function EXECUTE';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=relation.relnamespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND relation.oid <> 'aap_jobs_v1.async_job_creation'::regclass
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
        RAISE EXCEPTION 'Jobs lifecycle client has unrelated data or schema privileges';
    END IF;
END;
$verify$;
COMMIT;
