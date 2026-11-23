\set ON_ERROR_STOP on
BEGIN;
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
COMMIT;
