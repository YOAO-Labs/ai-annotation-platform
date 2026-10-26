-- Grant caller-UOW preparation to API and claim/finish/status only to the
-- separately configured durable-dispatch relay LOGIN. Neither role can touch
-- the private ledger table directly.
-- Run after migration 0368 and module-data-grants.sql as the migration role.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v jobs_owner=jobs_code -v api_role=api_runtime \
--   -v dispatch_relay_role=jobs_dispatch_relay \
--   -f infra/docker/jobs-dispatch-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.jobs_owner', :'jobs_owner', true);
SELECT set_config('aap.api_role', :'api_role', true);
SELECT set_config('aap.dispatch_relay_role', :'dispatch_relay_role', true);

DO $guard$
DECLARE
    v_jobs oid := (SELECT oid FROM pg_catalog.pg_roles
                   WHERE rolname=current_setting('aap.jobs_owner'));
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('aap.api_role'));
    v_relay oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('aap.dispatch_relay_role'));
    v_function record;
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
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
            ('aap_jobs_v1.prepare_dispatch(jsonb)', 'jsonb', false, 'v'),
            ('aap_jobs_v1.claim_dispatches(jsonb)', 'jsonb', false, 'v'),
            ('aap_jobs_v1.finish_dispatch(jsonb)', 'jsonb', false, 'v'),
            ('aap_jobs_v1.dispatch_status(uuid)', 'jsonb', false, 's')
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
        WHERE relation.oid='aap_jobs_v1.dispatch_intents'::regclass
          AND relation.relkind='r'
          AND relation.relowner=v_jobs
    ) THEN
        RAISE EXCEPTION 'Jobs dispatch table ownership differs';
    END IF;
END;
$guard$;

REVOKE ALL ON SCHEMA aap_jobs_v1 FROM :"dispatch_relay_role";
REVOKE ALL ON SCHEMA public FROM :"dispatch_relay_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_jobs_v1 FROM :"dispatch_relay_role";
REVOKE ALL ON ALL TABLES IN SCHEMA aap_jobs_v1, public FROM :"dispatch_relay_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"dispatch_relay_role";
REVOKE ALL ON FUNCTION
    aap_jobs_v1.prepare_dispatch(jsonb),
    aap_jobs_v1.claim_dispatches(jsonb),
    aap_jobs_v1.finish_dispatch(jsonb),
    aap_jobs_v1.dispatch_status(uuid)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION aap_jobs_v1.claim_dispatches(jsonb)
    FROM :"api_role";
REVOKE ALL ON TABLE aap_jobs_v1.dispatch_intents
    FROM :"api_role", :"dispatch_relay_role";
REVOKE ALL (
    intent_id, job_id, purpose, execution_id, envelope, natural_identity,
    state, lease_token, generation, attempts, lease_expires_at,
    next_attempt_at, accepted_at, created_at, updated_at
) ON TABLE aap_jobs_v1.dispatch_intents
    FROM :"api_role", :"dispatch_relay_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"dispatch_relay_role";
GRANT USAGE ON SCHEMA aap_jobs_v1 TO :"api_role", :"dispatch_relay_role";
GRANT EXECUTE ON FUNCTION
    aap_jobs_v1.prepare_dispatch(jsonb),
    aap_jobs_v1.finish_dispatch(jsonb),
    aap_jobs_v1.dispatch_status(uuid)
    TO :"api_role";
GRANT EXECUTE ON FUNCTION
    aap_jobs_v1.claim_dispatches(jsonb),
    aap_jobs_v1.finish_dispatch(jsonb),
    aap_jobs_v1.dispatch_status(uuid)
    TO :"dispatch_relay_role";

DO $verify$
DECLARE
    v_api oid := (SELECT oid FROM pg_catalog.pg_roles
                  WHERE rolname=current_setting('aap.api_role'));
    v_relay oid := (SELECT oid FROM pg_catalog.pg_roles
                    WHERE rolname=current_setting('aap.dispatch_relay_role'));
    v_api_allowed oid[] := ARRAY[
        'aap_jobs_v1.prepare_dispatch(jsonb)'::regprocedure::oid,
        'aap_jobs_v1.finish_dispatch(jsonb)'::regprocedure::oid,
        'aap_jobs_v1.dispatch_status(uuid)'::regprocedure::oid
    ];
    v_relay_allowed oid[] := ARRAY[
        'aap_jobs_v1.claim_dispatches(jsonb)'::regprocedure::oid,
        'aap_jobs_v1.finish_dispatch(jsonb)'::regprocedure::oid,
        'aap_jobs_v1.dispatch_status(uuid)'::regprocedure::oid
    ];
BEGIN
    IF EXISTS (
        SELECT FROM pg_catalog.unnest(v_api_allowed) AS allowed(function_oid)
        WHERE NOT pg_catalog.has_function_privilege(v_api,function_oid,'EXECUTE')
    ) OR pg_catalog.has_function_privilege(
        v_api,'aap_jobs_v1.claim_dispatches(jsonb)'::regprocedure,'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'API Jobs dispatch capability profile differs';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.unnest(v_relay_allowed) AS allowed(function_oid)
        WHERE NOT pg_catalog.has_function_privilege(v_relay,function_oid,'EXECUTE')
    ) OR pg_catalog.has_function_privilege(
        v_relay,'aap_jobs_v1.prepare_dispatch(jsonb)'::regprocedure,'EXECUTE'
    ) THEN
        RAISE EXCEPTION 'Jobs dispatch relay capability profile differs';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_proc AS function
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid=function.pronamespace
        WHERE namespace.nspname LIKE 'aap\_%' ESCAPE '\'
          AND function.oid<>ALL(v_relay_allowed)
          AND pg_catalog.has_function_privilege(v_relay,function.oid,'EXECUTE')
    ) THEN
        RAISE EXCEPTION 'Jobs dispatch relay has unrelated AAP function EXECUTE';
    END IF;
    IF pg_catalog.has_table_privilege(
           v_api,'aap_jobs_v1.dispatch_intents',
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
           v_api,'aap_jobs_v1.dispatch_intents','SELECT,INSERT,UPDATE,REFERENCES'
       ) OR pg_catalog.has_table_privilege(
           v_relay,'aap_jobs_v1.dispatch_intents',
           'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'
       ) OR pg_catalog.has_any_column_privilege(
           v_relay,'aap_jobs_v1.dispatch_intents','SELECT,INSERT,UPDATE,REFERENCES'
       ) THEN
        RAISE EXCEPTION 'Jobs dispatch private table has direct API or relay privileges';
    END IF;
    IF pg_catalog.has_database_privilege(
           v_relay,current_database(),'CREATE,TEMP'
       ) OR pg_catalog.has_schema_privilege(v_relay,'aap_jobs_v1','CREATE')
       OR pg_catalog.has_schema_privilege(v_relay,'public','CREATE') THEN
        RAISE EXCEPTION 'Jobs dispatch relay has unsafe database or schema privileges';
    END IF;
    -- PUBLIC schema USAGE permits name resolution, not relation access.
    -- Reject effective table/column/sequence rights across every owner schema.
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
               OR (namespace.nspname NOT IN ('public','aap_jobs_v1')
                   AND pg_catalog.has_schema_privilege(v_relay,namespace.oid,'USAGE')))
    ) THEN
        RAISE EXCEPTION 'Jobs dispatch relay has unrelated relation or schema privileges';
    END IF;
END;
$verify$;
COMMIT;
