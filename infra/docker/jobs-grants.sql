\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.grant_section_applied', '0', true);

\if :{?grant_dispatch}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.jobs_owner', :'jobs_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.dispatch_relay_role', :'dispatch_relay_role', true);

DO $guard$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles
                   WHERE rolname=current_setting('yoao.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('yoao.api_role'));
    v_relay oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.dispatch_relay_role'));
    v_function record;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration role';
    END IF;
    IF v_jobs IS NULL OR v_api IS NULL OR v_relay IS NULL
       OR v_jobs IN (v_api, v_relay) OR v_api = v_relay THEN
        RAISE EXCEPTION 'Jobs dispatch roles are missing or overlap';
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
               FROM pg_catalog.pg_roles WHERE oid=v_relay)
       OR EXISTS (
            SELECT FROM pg_catalog.pg_auth_members
            WHERE member=v_relay OR roleid=v_relay
               OR (roleid=v_jobs AND member<>current_user::pg_catalog.regrole)
       ) THEN
        RAISE EXCEPTION 'Unsafe Jobs dispatch owner or relay LOGIN';
    END IF;
    FOR v_function IN
        SELECT * FROM (VALUES
            ('yoao_jobs_v1.prepare_dispatch(jsonb)', 'jsonb', false, 'v'),
            ('yoao_jobs_v1.prepare_media_delivery(jsonb)', 'jsonb', false, 'v'),
            ('yoao_jobs_v1.fixture_media_backfill_route(uuid)', 'text', false, 'v'),
            ('yoao_jobs_v1.fixture_media_work_exists(text,text)', 'boolean', false, 's'),
            ('yoao_jobs_v1.claim_dispatches(jsonb)', 'jsonb', false, 'v'),
            ('yoao_jobs_v1.finish_dispatch(jsonb)', 'jsonb', false, 'v'),
            ('yoao_jobs_v1.dispatch_status(uuid)', 'jsonb', false, 's')
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
              AND function.proparallel='u'
              AND function.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
        ) THEN
            RAISE EXCEPTION 'Jobs dispatch capability ownership or signature differs';
        END IF;
    END LOOP;
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_class AS relation
        WHERE relation.oid='yoao_jobs_v1.dispatch_intents'::regclass
          AND relation.relkind='r'
          AND relation.relowner=v_jobs
    ) THEN
        RAISE EXCEPTION 'Jobs dispatch table ownership differs';
    END IF;
END;
$guard$;

REVOKE ALL ON SCHEMA yoao_jobs_v1 FROM :"dispatch_relay_role";
REVOKE ALL ON SCHEMA public FROM :"dispatch_relay_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_jobs_v1 FROM :"dispatch_relay_role";
REVOKE ALL ON ALL TABLES IN SCHEMA yoao_jobs_v1, public FROM :"dispatch_relay_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"dispatch_relay_role";
REVOKE ALL ON FUNCTION
    yoao_jobs_v1.prepare_dispatch(jsonb),
    yoao_jobs_v1.prepare_media_delivery(jsonb),
    yoao_jobs_v1.fixture_media_backfill_route(uuid),
    yoao_jobs_v1.fixture_media_work_exists(text,text),
    yoao_jobs_v1.claim_dispatches(jsonb),
    yoao_jobs_v1.finish_dispatch(jsonb),
    yoao_jobs_v1.dispatch_status(uuid)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION yoao_jobs_v1.claim_dispatches(jsonb)
    FROM :"api_role";
REVOKE ALL ON FUNCTION
    yoao_jobs_v1.prepare_fixture_media_delivery(uuid,uuid,uuid,text,uuid,text,uuid,text,jsonb,jsonb),
    yoao_assets_v1.media_producer_binding(text,uuid,text,uuid,jsonb)
    FROM :"api_role";
REVOKE ALL ON TABLE yoao_jobs_v1.dispatch_intents
    FROM :"api_role", :"dispatch_relay_role";
REVOKE ALL (
    intent_id, job_id, purpose, execution_id, envelope, natural_identity,
    state, lease_token, generation, attempts, lease_expires_at,
    next_attempt_at, accepted_at, created_at, updated_at
) ON TABLE yoao_jobs_v1.dispatch_intents
    FROM :"api_role", :"dispatch_relay_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"dispatch_relay_role";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"api_role", :"dispatch_relay_role";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.prepare_dispatch(jsonb),
    yoao_jobs_v1.prepare_media_delivery(jsonb),
    yoao_jobs_v1.fixture_media_backfill_route(uuid),
    yoao_jobs_v1.fixture_media_work_exists(text,text),
    yoao_jobs_v1.finish_dispatch(jsonb),
    yoao_jobs_v1.dispatch_status(uuid)
    TO :"api_role";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.claim_dispatches(jsonb),
    yoao_jobs_v1.finish_dispatch(jsonb),
    yoao_jobs_v1.dispatch_status(uuid)
    TO :"dispatch_relay_role";

DO $verify$
DECLARE
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('yoao.api_role'));
    v_relay oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('yoao.dispatch_relay_role'));
    v_api_allowed oid[] := ARRAY[
        'yoao_jobs_v1.prepare_dispatch(jsonb)'::regprocedure::oid,
        'yoao_jobs_v1.prepare_media_delivery(jsonb)'::regprocedure::oid,
        'yoao_jobs_v1.fixture_media_backfill_route(uuid)'::regprocedure::oid,
        'yoao_jobs_v1.fixture_media_work_exists(text,text)'::regprocedure::oid,
        'yoao_jobs_v1.finish_dispatch(jsonb)'::regprocedure::oid,
        'yoao_jobs_v1.dispatch_status(uuid)'::regprocedure::oid
    ];
    v_relay_allowed oid[] := ARRAY[
        'yoao_jobs_v1.claim_dispatches(jsonb)'::regprocedure::oid,
        'yoao_jobs_v1.finish_dispatch(jsonb)'::regprocedure::oid,
        'yoao_jobs_v1.dispatch_status(uuid)'::regprocedure::oid
    ];
BEGIN
    IF EXISTS (
        SELECT FROM pg_catalog.unnest(v_api_allowed) AS allowed(function_oid)
        WHERE NOT pg_catalog.has_function_privilege(v_api,function_oid,'EXECUTE')
    ) OR pg_catalog.has_function_privilege(
        v_api,'yoao_jobs_v1.claim_dispatches(jsonb)'::regprocedure,'EXECUTE'
    ) OR pg_catalog.has_function_privilege(
        v_api,
        'yoao_jobs_v1.prepare_fixture_media_delivery(uuid,uuid,uuid,text,uuid,text,uuid,text,jsonb,jsonb)'::regprocedure,
        'EXECUTE'
    ) OR pg_catalog.has_function_privilege(
        v_api,'yoao_assets_v1.media_producer_binding(text,uuid,text,uuid,jsonb)'::regprocedure,
        'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'API Jobs dispatch capability profile differs';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=function.pronamespace
        WHERE namespace.nspname='yoao_jobs_v1'
          AND function.proname LIKE '%fixture_media%'
          AND function.oid NOT IN (
              'yoao_jobs_v1.fixture_media_backfill_route(uuid)'::regprocedure,
              'yoao_jobs_v1.fixture_media_work_exists(text,text)'::regprocedure
          )
          AND pg_catalog.has_function_privilege(v_api,function.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'API has private fixture media EXECUTE';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.unnest(v_relay_allowed) AS allowed(function_oid)
        WHERE NOT pg_catalog.has_function_privilege(v_relay,function_oid,'EXECUTE')
    ) OR pg_catalog.has_function_privilege(
        v_relay,'yoao_jobs_v1.prepare_dispatch(jsonb)'::regprocedure,'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Jobs dispatch relay capability profile differs';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=function.pronamespace
        WHERE (namespace.nspname LIKE 'aap\_%' ESCAPE '\'
               OR namespace.nspname LIKE 'yoao\_%' ESCAPE '\')
          AND function.oid<>ALL(v_relay_allowed)
          AND pg_catalog.has_function_privilege(v_relay,function.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Jobs dispatch relay has unrelated AAP function EXECUTE';
    END IF;
    IF pg_catalog.has_table_privilege(
           v_api,'yoao_jobs_v1.dispatch_intents',
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
           v_api,'yoao_jobs_v1.dispatch_intents','SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
           v_relay,'yoao_jobs_v1.dispatch_intents',
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
           v_relay,'yoao_jobs_v1.dispatch_intents','SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Jobs dispatch private table has direct API or relay privileges';
    END IF;
    IF pg_catalog.has_database_privilege(
           v_relay,current_database(),'CREATE,TEMP'
       ) OR pg_catalog.has_schema_privilege(v_relay,'yoao_jobs_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_relay,'public','CREATE') THEN
        RAISE EXCEPTION 'Jobs dispatch relay has unsafe database or schema privileges';
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
                         v_relay,relation.oid,
                         'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
                     )
                     OR pg_catalog.has_any_column_privilege(
                         v_relay,relation.oid,'SELECT,INSERT,UPDATE,REFERENCES'
                     )))
               OR (relation.relkind='S' AND pg_catalog.has_sequence_privilege(
                    v_relay,relation.oid,'USAGE,SELECT,UPDATE')))
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_relay,namespace.oid,'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_jobs_v1')
                   AND pg_catalog.has_schema_privilege(v_relay,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Jobs dispatch relay has unrelated relation or schema privileges';
    END IF;
END;
$verify$;
\endif

\if :{?grant_lifecycle}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.jobs_owner', :'jobs_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.lifecycle_client', :'lifecycle_client', true);

DO $guard$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles
                   WHERE rolname=current_setting('yoao.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('yoao.lifecycle_client'));
    v_function record;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
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
            ('yoao_jobs_v1.read_job(uuid)','record',true,'s'),
            ('yoao_jobs_v1.read_job_execution_snapshot(uuid)','uuid',false,'s'),
            ('yoao_jobs_v1.read_job_progress(uuid)','record',true,'s'),
            ('yoao_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid)','boolean',false,'v'),
            ('yoao_jobs_v1.jobs_by_execution_id(text)','record',true,'s'),
            ('yoao_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text)','boolean',false,'v'),
            ('yoao_jobs_v1.apply_lifecycle(uuid,jsonb)','record',true,'v'),
            ('yoao_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text)','boolean',false,'v'),
            ('yoao_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz)','uuid[]',false,'v'),
            ('yoao_jobs_v1.delete_batch_predict_jobs(uuid)','uuid[]',false,'v'),
            ('yoao_jobs_v1.delete_terminal_jobs_before(timestamptz,text[])','uuid[]',false,'v')
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
                  WHEN v_function.signature='yoao_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid)'
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
        WHERE relation.oid='yoao_jobs_v1.async_job_creation'::regclass
          AND relation.relkind='v'
          AND relation.relowner=current_setting('yoao.jobs_owner')::regrole
          AND relation.reloptions @> ARRAY['check_option=local']::text[]
    ) THEN
        RAISE EXCEPTION 'Jobs creation relation ownership or check option differs';
    END IF;
END;
$view_guard$;

REVOKE ALL ON SCHEMA yoao_jobs_v1 FROM :"lifecycle_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_jobs_v1 FROM :"lifecycle_client";
REVOKE ALL ON ALL TABLES IN SCHEMA yoao_jobs_v1, public FROM :"lifecycle_client";
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
        WHERE namespace.nspname IN ('public', 'yoao_jobs_v1')
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
                current_setting('yoao.lifecycle_client')
            );
        END IF;
    END LOOP;
END;
$revoke_columns$;
REVOKE ALL ON FUNCTION
    yoao_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid),
    yoao_jobs_v1.read_job_execution_snapshot(uuid),
    yoao_jobs_v1.read_job_progress(uuid),
    yoao_jobs_v1.read_job(uuid),
    yoao_jobs_v1.jobs_by_execution_id(text),
    yoao_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text),
    yoao_jobs_v1.apply_lifecycle(uuid,jsonb),
    yoao_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text),
    yoao_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz),
    yoao_jobs_v1.delete_batch_predict_jobs(uuid),
    yoao_jobs_v1.delete_terminal_jobs_before(timestamptz,text[])
    FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"lifecycle_client";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"api_role", :"lifecycle_client";
GRANT SELECT, INSERT ON yoao_jobs_v1.async_job_creation
    TO :"api_role", :"lifecycle_client";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid),
    yoao_jobs_v1.read_job_execution_snapshot(uuid),
    yoao_jobs_v1.read_job_progress(uuid),
    yoao_jobs_v1.read_job(uuid),
    yoao_jobs_v1.jobs_by_execution_id(text),
    yoao_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text),
    yoao_jobs_v1.apply_lifecycle(uuid,jsonb),
    yoao_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text),
    yoao_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz),
    yoao_jobs_v1.delete_batch_predict_jobs(uuid),
    yoao_jobs_v1.delete_terminal_jobs_before(timestamptz,text[])
    TO :"api_role", :"lifecycle_client";

DO $verify$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles
                   WHERE rolname=current_setting('yoao.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('yoao.lifecycle_client'));
    v_allowed oid[] := ARRAY[
        'yoao_jobs_v1.bind_prediction_execution_snapshot(uuid,uuid,uuid)'::regprocedure::oid,
        'yoao_jobs_v1.read_job_execution_snapshot(uuid)'::regprocedure::oid,
        'yoao_jobs_v1.read_job_progress(uuid)'::regprocedure::oid,
        'yoao_jobs_v1.read_job(uuid)'::regprocedure::oid,
        'yoao_jobs_v1.jobs_by_execution_id(text)'::regprocedure::oid,
        'yoao_jobs_v1.insert_video_summary(uuid,text,uuid,uuid,jsonb,text)'::regprocedure::oid,
        'yoao_jobs_v1.apply_lifecycle(uuid,jsonb)'::regprocedure::oid,
        'yoao_jobs_v1.mark_dispatch_failed(uuid,timestamptz,text)'::regprocedure::oid,
        'yoao_jobs_v1.cancel_batch_predict_jobs(uuid,timestamptz)'::regprocedure::oid,
        'yoao_jobs_v1.delete_batch_predict_jobs(uuid)'::regprocedure::oid,
        'yoao_jobs_v1.delete_terminal_jobs_before(timestamptz,text[])'::regprocedure::oid
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
       OR pg_catalog.has_table_privilege(v_client,'yoao_jobs_v1.async_job_creation','UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
       OR pg_catalog.has_any_column_privilege(v_client,'yoao_jobs_v1.async_job_creation','UPDATE,REFERENCES')
       OR NOT pg_catalog.has_table_privilege(v_client,'yoao_jobs_v1.async_job_creation','SELECT')
       OR NOT pg_catalog.has_table_privilege(v_client,'yoao_jobs_v1.async_job_creation','INSERT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP') THEN
        RAISE EXCEPTION 'Jobs lifecycle profile exposes private writes or unsafe creation';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=function.pronamespace
        WHERE (namespace.nspname LIKE 'aap\_%' ESCAPE '\'
               OR namespace.nspname LIKE 'yoao\_%' ESCAPE '\')
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
          AND relation.oid <> 'yoao_jobs_v1.async_job_creation'::regclass
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
               OR (namespace.nspname NOT IN ('public','yoao_jobs_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Jobs lifecycle client has unrelated data or schema privileges';
    END IF;
END;
$verify$;
\endif

\if :{?grant_list_query}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.jobs_owner', :'jobs_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.list_client', :'list_client', true);

DO $guard$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles
                   WHERE rolname=current_setting('yoao.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('yoao.list_client'));
    v_function record;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
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
            ('yoao_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer)',
                'record', true),
            ('yoao_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)',
                'bigint', false),
            ('yoao_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)',
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

GRANT USAGE ON SCHEMA yoao_workspace_v1, yoao_workspace_project_query_v1
    TO :"jobs_owner";
GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_workspace_v1.actor_project_memberships(uuid),
    yoao_workspace_project_query_v1.project_search_scope(uuid,text)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid)
    TO :"jobs_owner";

REVOKE ALL ON SCHEMA yoao_jobs_v1 FROM :"list_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_jobs_v1 FROM :"list_client";
REVOKE ALL ON ALL TABLES IN SCHEMA yoao_jobs_v1, public FROM :"list_client";
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
        WHERE namespace.nspname IN ('public', 'yoao_jobs_v1')
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
                current_setting('yoao.list_client')
            );
        END IF;
    END LOOP;
END;
$revoke_columns$;
REVOKE ALL ON FUNCTION
    yoao_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)
    FROM :"api_role", :"list_client", PUBLIC;
REVOKE ALL ON FUNCTION
    yoao_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer),
    yoao_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)
    FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"list_client";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"api_role", :"list_client";
GRANT EXECUTE ON FUNCTION
    yoao_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer),
    yoao_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)
    TO :"api_role", :"list_client";

DO $verify$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles
                   WHERE rolname=current_setting('yoao.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles
                     WHERE rolname=current_setting('yoao.list_client'));
    v_allowed oid[] := ARRAY[
        'yoao_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer)'::regprocedure::oid,
        'yoao_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)'::regprocedure::oid
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
            'yoao_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)','EXECUTE')
       OR pg_catalog.has_function_privilege(v_client,
            'yoao_jobs_v1._async_job_list_rows(uuid,text,text[],text[],uuid,text)','EXECUTE')
       OR pg_catalog.has_table_privilege(v_client,'public.async_jobs','SELECT')
       OR pg_catalog.has_any_column_privilege(v_client,'public.async_jobs','SELECT')
       OR NOT pg_catalog.has_function_privilege(v_jobs,
            'yoao_identity_v1.read_actor_facts(uuid)','EXECUTE')
       OR pg_catalog.has_table_privilege(v_jobs,'public.users','SELECT')
       OR pg_catalog.has_any_column_privilege(v_jobs,'public.users','SELECT')
       OR pg_catalog.has_table_privilege(v_jobs,
            'yoao_identity_v1.account_profiles','SELECT')
       OR pg_catalog.has_any_column_privilege(v_jobs,
            'yoao_identity_v1.account_profiles','SELECT')
       OR pg_catalog.has_database_privilege(v_client,current_database(),'CREATE,TEMP') THEN
        RAISE EXCEPTION 'Jobs list profile exposes private reads or a helper';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=function.pronamespace
        WHERE (namespace.nspname LIKE 'aap\_%' ESCAPE '\'
               OR namespace.nspname LIKE 'yoao\_%' ESCAPE '\')
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
               OR (namespace.nspname NOT IN ('public','yoao_jobs_v1')
                   AND pg_catalog.has_schema_privilege(v_client,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Jobs list client has unrelated data or schema privileges';
    END IF;
END;
$verify$;
\endif

\if :{?grant_notification_scope}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.jobs_owner', :'jobs_owner', true);
SELECT set_config('yoao.api_role', :'api_role', true);
SELECT set_config('yoao.read_client', :'read_client', true);

DO $guard$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.read_client'));
    v_relation oid := pg_catalog.to_regclass('yoao_jobs_v1.notification_scope_facts')::oid;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
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

REVOKE ALL ON SCHEMA yoao_jobs_v1 FROM :"read_client";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_jobs_v1 FROM :"read_client";
REVOKE ALL ON ALL TABLES IN SCHEMA yoao_jobs_v1 FROM :"read_client";
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
        WHERE namespace.nspname IN ('public', 'yoao_jobs_v1')
          AND relation.relkind IN ('r','p','v','m','f')
    LOOP
        SELECT pg_catalog.string_agg(pg_catalog.quote_ident(attname), ',' ORDER BY attnum)
        INTO v_columns
        FROM pg_catalog.pg_attribute
        WHERE attrelid=v_relation.oid AND attnum>0 AND NOT attisdropped;
        IF v_columns IS NOT NULL THEN
            EXECUTE pg_catalog.format('REVOKE ALL (%s) ON TABLE %I.%I FROM %I',
                v_columns, v_relation.nspname, v_relation.relname,
                current_setting('yoao.read_client'));
        END IF;
    END LOOP;
END;
$revoke_columns$;

REVOKE ALL ON TABLE yoao_jobs_v1.notification_scope_facts FROM :"api_role", PUBLIC;
REVOKE ALL (id, kind, scope_project_id)
    ON TABLE yoao_jobs_v1.notification_scope_facts FROM :"api_role", PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"read_client";
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"read_client", :"api_role";
GRANT SELECT ON yoao_jobs_v1.notification_scope_facts TO :"read_client", :"api_role";

DO $verify$
DECLARE
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.api_role'));
    v_client oid := (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_setting('yoao.read_client'));
    v_relation oid := 'yoao_jobs_v1.notification_scope_facts'::regclass;
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
        WHERE (namespace.nspname LIKE 'aap\_%' ESCAPE '\'
               OR namespace.nspname LIKE 'yoao\_%' ESCAPE '\')
          AND pg_catalog.has_function_privilege(v_client, function.oid, 'EXECUTE')
    ) OR EXISTS (
        SELECT FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname NOT IN ('pg_catalog','information_schema')
          AND namespace.nspname NOT LIKE 'pg_toast%'
          AND namespace.nspname NOT LIKE 'pg_temp_%'
          AND (pg_catalog.has_schema_privilege(v_client, namespace.oid, 'CREATE')
               OR (namespace.nspname NOT IN ('public','yoao_jobs_v1')
                   AND pg_catalog.has_schema_privilege(v_client, namespace.oid, 'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Jobs fact reader has unrelated data, function or schema privileges';
    END IF;
END;
$verify$;
\endif

\if :{?grant_recovery}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT pg_catalog.set_config('yoao.expected_database', :'expected_database', true);
SELECT pg_catalog.set_config('yoao.migration_role', :'migration_role', true);
SELECT pg_catalog.set_config('yoao.application_role', :'application_role', true);
SELECT pg_catalog.set_config('yoao.dispatch_role', :'dispatch_role', true);
SELECT pg_catalog.set_config('yoao.publication_role', :'publication_role', true);
SET LOCAL search_path = pg_catalog, pg_temp;

DO $grants$
DECLARE
    v_app name := current_setting('yoao.application_role');
    v_dispatch name := current_setting('yoao.dispatch_role');
    v_publication name := current_setting('yoao.publication_role');
    v_dispatch_functions text[] := ARRAY[
        'yoao_jobs_v1.claim_dispatches(jsonb)',
        'yoao_jobs_v1.finish_dispatch(jsonb)',
        'yoao_jobs_v1.dispatch_status(uuid)'
    ];
    v_publication_functions text[] := ARRAY[
        'yoao_jobs_v1.begin_dispatch_publication(uuid,uuid,text,bigint,uuid)',
        'yoao_jobs_v1.finish_dispatch_publication(uuid,uuid,text,bigint,uuid)',
        'yoao_jobs_v1.refuse_dispatch_publication(uuid,uuid,text,bigint,uuid)',
        'yoao_jobs_v1.retry_publication_intent(uuid,text,text,jsonb,jsonb)',
        'yoao_jobs_v1.begin_retry_publication(uuid,uuid,text,text,jsonb,jsonb,integer,uuid)',
        'yoao_jobs_v1.finish_retry_publication(uuid,uuid,text,integer,uuid)',
        'yoao_jobs_v1.begin_fixture_media_publication(uuid,uuid)',
        'yoao_jobs_v1.finish_fixture_media_publication(uuid,uuid,text)'
    ];
    v_owner oid := current_user::regrole;
    v_app_oid oid := to_regrole(v_app);
    v_signature text;
    v_login name;
    v_role oid;
    v_allowed oid[];
    v_function oid;
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR current_user <> session_user
       OR current_user IN (v_app, v_dispatch, v_publication)
       OR v_app IN (v_dispatch, v_publication) OR v_dispatch = v_publication THEN
        RAISE EXCEPTION 'Unexpected Jobs grants database or overlapping roles';
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_roles WHERE oid=v_app_oid AND rolcanlogin
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR pg_has_role(v_app_oid,v_owner,'MEMBER') THEN
        RAISE EXCEPTION 'Jobs producers require a distinct ordinary runtime LOGIN';
    END IF;
    IF EXISTS (
        SELECT FROM (VALUES ('yoao_jobs_v1.dispatch_intents'),
                            ('yoao_jobs_v1.dispatch_publication_attempts')) AS expected(relation)
        LEFT JOIN pg_class ON pg_class.oid=to_regclass(expected.relation)
        WHERE relowner IS DISTINCT FROM v_owner OR relkind IS DISTINCT FROM 'r'
    ) THEN
        RAISE EXCEPTION 'Expected migration-owned Jobs relations; use the modular owner grant profiles for a split database';
    END IF;

    FOREACH v_login IN ARRAY ARRAY[v_dispatch, v_publication] LOOP
        SELECT oid INTO v_role FROM pg_roles WHERE rolname=v_login AND rolcanlogin
          AND NOT (rolinherit OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
        IF v_role IS NULL OR EXISTS (
            SELECT FROM pg_auth_members WHERE member=v_role OR roleid=v_role
        ) OR has_database_privilege(v_role,current_database(),'CREATE,TEMP') THEN
            RAISE EXCEPTION 'Unsafe Jobs LOGIN or inherited CREATE/TEMP privilege: %', v_login;
        END IF;
        v_allowed := ARRAY[]::oid[];
        FOREACH v_signature IN ARRAY CASE WHEN v_login=v_dispatch THEN v_dispatch_functions ELSE v_publication_functions END LOOP
            v_function := to_regprocedure(v_signature);
            IF NOT EXISTS (
                SELECT FROM pg_proc WHERE oid=v_function AND proowner=v_owner AND prosecdef
                  AND prokind='f' AND NOT proretset AND proparallel='u'
                  AND proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
            ) THEN
                RAISE EXCEPTION 'Jobs capability differs: %', v_signature;
            END IF;
            v_allowed := array_append(v_allowed,v_function);
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',v_signature,v_login);
        END LOOP;
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I',current_database(),v_login);
        EXECUTE format('GRANT USAGE ON SCHEMA yoao_jobs_v1 TO %I',v_login);
        IF EXISTS (
            SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE (left(n.nspname,4)='aap_' OR left(n.nspname,5)='yoao_')
              AND p.oid<>ALL(v_allowed) AND has_function_privilege(v_role,p.oid,'EXECUTE')
        ) OR EXISTS (
            SELECT FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog','information_schema')
              AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'
              AND ((c.relkind IN ('r','p','v','m','f') AND
                    (has_table_privilege(v_role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                     OR has_any_column_privilege(v_role,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')))
                   OR (c.relkind='S' AND has_sequence_privilege(v_role,c.oid,'USAGE,SELECT,UPDATE')))
        ) OR EXISTS (
            SELECT FROM pg_namespace n
            WHERE n.nspname NOT IN ('pg_catalog','information_schema')
              AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'
              AND (has_schema_privilege(v_role,n.oid,'CREATE') OR
                   (n.nspname NOT IN ('public','yoao_jobs_v1') AND has_schema_privilege(v_role,n.oid,'USAGE')))
        ) THEN
            RAISE EXCEPTION 'Jobs LOGIN has unrelated privileges: %', v_login;
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT FROM unnest(ARRAY[v_dispatch_functions[1]] || v_publication_functions) AS forbidden(signature)
        WHERE has_function_privilege(v_app_oid,to_regprocedure(signature),'EXECUTE')
    ) OR EXISTS (
        SELECT FROM (VALUES ('yoao_jobs_v1.dispatch_intents'),
                            ('yoao_jobs_v1.dispatch_publication_attempts')) AS private(relation)
        WHERE has_table_privilege(v_app_oid,to_regclass(relation),'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
           OR has_any_column_privilege(v_app_oid,to_regclass(relation),'SELECT,INSERT,UPDATE,REFERENCES')
    ) THEN
        RAISE EXCEPTION 'Ordinary runtime LOGIN has Jobs claim, publication or private table authority';
    END IF;
    IF NOT has_function_privilege(v_app_oid,'yoao_jobs_v1.prepare_dispatch(jsonb)','EXECUTE')
       OR NOT has_function_privilege(v_app_oid,'yoao_jobs_v1.finish_dispatch(jsonb)','EXECUTE')
       OR NOT has_function_privilege(v_app_oid,'yoao_jobs_v1.dispatch_status(uuid)','EXECUTE') THEN
        RAISE EXCEPTION 'Install the ordinary runtime grants before Jobs recovery';
    END IF;
END;
$grants$;
\endif

\if :{?grant_video_execution}
SELECT set_config('yoao.grant_section_applied', '1', true);
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.jobs_owner', :'jobs_owner', true);
SELECT set_config('yoao.video_execution_role', :'video_execution_role', true);

DO $$
DECLARE
    role_name text := current_setting('yoao.video_execution_role');
    owner_name text := current_setting('yoao.jobs_owner');
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF role_name <> 'yoao_video_execution_client' OR owner_name <> 'jobs_code'
       OR role_name = owner_name THEN
        RAISE EXCEPTION 'Unexpected video execution roles';
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
        RAISE EXCEPTION 'Video execution roles are unsafe or have memberships';
    END IF;
END $$;

DO $$
DECLARE
    owner_oid oid := (
        SELECT oid FROM pg_catalog.pg_roles
        WHERE rolname = current_setting('yoao.jobs_owner')
    );
    function_oid oid;
    signature text;
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS namespace
        WHERE namespace.nspname = 'yoao_jobs_v1'
          AND namespace.nspowner IS DISTINCT FROM owner_oid
    ) THEN
        RAISE EXCEPTION 'Video execution Native schema or function is unavailable';
    END IF;
    FOREACH signature IN ARRAY ARRAY[
        'yoao_jobs_v1.video_execution_step(text,text)',
        'yoao_jobs_v1.video_execution_checkpoint_step(text,text)'
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
        RAISE EXCEPTION 'Jobs video execution capability has unexpected ownership or settings';
    END IF;
    END LOOP;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"video_execution_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"video_execution_role";
DO $$
DECLARE
    namespace record;
    role_name text := current_setting('yoao.video_execution_role');
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
GRANT USAGE ON SCHEMA yoao_jobs_v1 TO :"video_execution_role";
GRANT EXECUTE ON FUNCTION yoao_jobs_v1.video_execution_step(text,text),
    yoao_jobs_v1.video_execution_checkpoint_step(text,text)
    TO :"video_execution_role";

DO $$
DECLARE
    role_oid oid := (
        SELECT role.oid FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_setting('yoao.video_execution_role')
    );
    actual_functions text[];
    expected_functions text[] := ARRAY[
        'yoao_jobs_v1.video_execution_checkpoint_step(text, text)',
        'yoao_jobs_v1.video_execution_step(text, text)'
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
        RAISE EXCEPTION 'Video execution LOGIN has unexpected functions: %',actual_functions;
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
