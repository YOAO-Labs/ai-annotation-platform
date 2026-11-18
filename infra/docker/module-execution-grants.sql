-- Grant independent physical publication and export-execution capabilities.
-- Run after module-data-grants.sql; neither LOGIN receives private table access.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v jobs_owner=jobs_code -v export_owner=export_code \
--   -v jobs_publication_role=jobs_publication_client \
--   -v export_execution_role=export_execution_client \
--   -f infra/docker/module-execution-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('yoao.expected_database', :'expected_database', true);
SELECT set_config('yoao.migration_role', :'migration_role', true);
SELECT set_config('yoao.jobs_owner', :'jobs_owner', true);
SELECT set_config('yoao.export_owner', :'export_owner', true);
SELECT set_config('yoao.jobs_publication_role', :'jobs_publication_role', true);
SELECT set_config('yoao.export_execution_role', :'export_execution_role', true);

DO $grants$
DECLARE
    profile record;
    capability text;
    capability_oid oid;
    capability_oids oid[];
    namespace record;
    client_oid oid;
    owner_oid oid;
    names text[] := ARRAY[
        current_setting('yoao.jobs_owner'),
        current_setting('yoao.export_owner'),
        current_setting('yoao.jobs_publication_role'),
        current_setting('yoao.export_execution_role')
    ];
BEGIN
    IF current_database() <> current_setting('yoao.expected_database')
       OR current_user <> current_setting('yoao.migration_role')
       OR current_user = ANY(names)
       OR (SELECT count(DISTINCT name) FROM unnest(names) AS name) <> 4 THEN
        RAISE EXCEPTION 'Unexpected execution grant database or overlapping roles';
    END IF;
    FOR profile IN SELECT * FROM (VALUES
        (current_setting('yoao.jobs_publication_role'), current_setting('yoao.jobs_owner'),
         'aap_jobs_v1', ARRAY[
            'aap_jobs_v1.begin_dispatch_publication(uuid,uuid,text,bigint,uuid)',
            'aap_jobs_v1.finish_dispatch_publication(uuid,uuid,text,bigint,uuid)',
            'aap_jobs_v1.refuse_dispatch_publication(uuid,uuid,text,bigint,uuid)',
            'aap_jobs_v1.retry_publication_intent(uuid,text,text,jsonb,jsonb)',
            'aap_jobs_v1.begin_retry_publication(uuid,uuid,text,text,jsonb,jsonb,integer,uuid)',
            'aap_jobs_v1.finish_retry_publication(uuid,uuid,text,integer,uuid)',
            'aap_jobs_v1.begin_fixture_media_publication(uuid,uuid)',
            'aap_jobs_v1.finish_fixture_media_publication(uuid,uuid,text)'
         ]::text[]),
        (current_setting('yoao.export_execution_role'), current_setting('yoao.export_owner'),
         'aap_export_v1', ARRAY[
            'aap_export_v1.begin_execution(uuid,uuid,text,uuid,integer)',
            'aap_export_v1.enter_execution_io(uuid,uuid)',
            'aap_export_v1.quiesce_execution_io(uuid,uuid)',
            'aap_export_v1.finish_execution(uuid,uuid)'
         ]::text[])
    ) AS profiles(client_name, owner_name, schema_name, signatures)
    LOOP
        IF profile.client_name !~ '^[a-z_][a-z0-9_]{0,62}$'
           OR profile.owner_name !~ '^[a-z_][a-z0-9_]{0,62}$' THEN
            RAISE EXCEPTION 'Unsafe execution role name';
        END IF;
        SELECT oid INTO client_oid FROM pg_catalog.pg_roles
        WHERE rolname=profile.client_name AND rolcanlogin AND NOT rolinherit
          AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
          AND NOT rolreplication AND NOT rolbypassrls;
        SELECT oid INTO owner_oid FROM pg_catalog.pg_roles
        WHERE rolname=profile.owner_name AND NOT rolcanlogin AND NOT rolinherit
          AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole
          AND NOT rolreplication AND NOT rolbypassrls;
        IF client_oid IS NULL OR owner_oid IS NULL OR EXISTS (
            SELECT FROM pg_catalog.pg_auth_members
            WHERE member=client_oid OR roleid=client_oid
        ) THEN
            RAISE EXCEPTION 'Unsafe execution owner or client LOGIN';
        END IF;
        capability_oids := ARRAY[]::oid[];
        FOREACH capability IN ARRAY profile.signatures LOOP
            capability_oid := pg_catalog.to_regprocedure(capability);
            IF capability_oid IS NULL OR NOT EXISTS (
                SELECT FROM pg_catalog.pg_proc AS function
                WHERE function.oid=capability_oid AND function.proowner=owner_oid
                  AND function.prosecdef AND function.prokind='f'
                  AND NOT function.proretset AND function.proparallel='u'
                  AND function.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
            ) THEN
                RAISE EXCEPTION 'Execution capability ownership or configuration differs: %', capability;
            END IF;
            capability_oids := array_append(capability_oids, capability_oid);
        END LOOP;
        EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', current_database(), profile.client_name);
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), profile.client_name);
        FOR namespace IN
            SELECT nspname FROM pg_catalog.pg_namespace
            WHERE nspname NOT IN ('pg_catalog','information_schema')
              AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp_%'
        LOOP
            EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %I', namespace.nspname, profile.client_name);
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', namespace.nspname, profile.client_name);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', namespace.nspname, profile.client_name);
            EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %I', namespace.nspname, profile.client_name);
        END LOOP;
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', profile.schema_name, profile.client_name);
        FOREACH capability IN ARRAY profile.signatures LOOP
            EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', capability);
            EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I', capability, profile.client_name);
        END LOOP;
        IF pg_catalog.has_database_privilege(client_oid,current_database(),'CREATE,TEMP')
           OR EXISTS (
               SELECT FROM pg_catalog.pg_namespace AS ns
               WHERE ns.nspname NOT IN ('pg_catalog','information_schema')
                 AND ns.nspname NOT LIKE 'pg_toast%' AND ns.nspname NOT LIKE 'pg_temp_%'
                 AND (pg_catalog.has_schema_privilege(client_oid,ns.oid,'CREATE')
                      OR (ns.nspname NOT IN ('public',profile.schema_name)
                          AND pg_catalog.has_schema_privilege(client_oid,ns.oid,'USAGE')))
           ) OR EXISTS (
               SELECT FROM pg_catalog.pg_class AS relation
               JOIN pg_catalog.pg_namespace AS ns ON ns.oid=relation.relnamespace
               WHERE ns.nspname NOT IN ('pg_catalog','information_schema')
                 AND ns.nspname NOT LIKE 'pg_toast%' AND ns.nspname NOT LIKE 'pg_temp_%'
                 AND ((relation.relkind IN ('r','p','v','m','f')
                       AND (pg_catalog.has_table_privilege(client_oid,relation.oid,
                                'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                            OR pg_catalog.has_any_column_privilege(client_oid,relation.oid,
                                'SELECT,INSERT,UPDATE,REFERENCES')))
                      OR (relation.relkind='S' AND pg_catalog.has_sequence_privilege(
                              client_oid,relation.oid,'USAGE,SELECT,UPDATE')))
           ) OR EXISTS (
               SELECT FROM pg_catalog.pg_proc AS function
               JOIN pg_catalog.pg_namespace AS ns ON ns.oid=function.pronamespace
               WHERE (ns.nspname LIKE 'aap\_%' ESCAPE '\'
                      OR ns.nspname LIKE 'yoao\_%' ESCAPE '\')
                 AND function.oid<>ALL(capability_oids)
                 AND pg_catalog.has_function_privilege(client_oid,function.oid,'EXECUTE')
           ) OR EXISTS (
               SELECT FROM unnest(capability_oids) AS allowed(function_oid)
               WHERE NOT pg_catalog.has_function_privilege(client_oid,function_oid,'EXECUTE')
           ) THEN
            RAISE EXCEPTION 'Execution client has unexpected effective privileges: %', profile.client_name;
        END IF;
    END LOOP;
END;
$grants$;
COMMIT;
