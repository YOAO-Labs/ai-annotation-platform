-- The preannotation and retry executor receives only its public result protocol.
-- Run after migrations and module-data-grants as the migration role.
-- Database provisioning must revoke PUBLIC TEMP first; this script changes no PUBLIC ACL.
-- psql -v expected_database=annotation_test -v migration_role=owner \
--   -v jobs_owner=jobs_code -v labeling_owner=labeling_code \
--   -v result_role=preannotation_result_client \
--   -f infra/docker/preannotation-result-data-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.jobs_owner', :'jobs_owner', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config('aap.result_role', :'result_role', true);

DO $$
DECLARE
    v_client oid;
    v_jobs oid;
    v_labeling oid;
BEGIN
    SELECT oid INTO v_client FROM pg_catalog.pg_roles
    WHERE rolname = current_setting('aap.result_role')
      AND rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    SELECT oid INTO v_jobs FROM pg_catalog.pg_roles
    WHERE rolname = current_setting('aap.jobs_owner')
      AND NOT rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    SELECT oid INTO v_labeling FROM pg_catalog.pg_roles
    WHERE rolname = current_setting('aap.labeling_owner')
      AND NOT rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls);
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role')
       OR v_client IS NULL OR v_jobs IS NULL OR v_labeling IS NULL
       OR v_client IN (v_jobs, v_labeling)
       OR pg_catalog.pg_has_role(v_client, v_jobs, 'MEMBER')
       OR pg_catalog.pg_has_role(v_client, v_labeling, 'MEMBER') THEN
        RAISE EXCEPTION 'Unexpected database, caller or preannotation result roles';
    END IF;
    IF EXISTS (
        SELECT FROM pg_catalog.pg_auth_members AS membership
        WHERE membership.member = v_client OR membership.roleid = v_client
    ) THEN
        RAISE EXCEPTION 'The preannotation result role must have no role memberships';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM :"result_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"result_role";
REVOKE ALL ON SCHEMA public, aap_jobs_v1, aap_labeling_v1 FROM :"result_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"result_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"result_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_jobs_v1, aap_labeling_v1 FROM :"result_role";
GRANT USAGE ON SCHEMA aap_jobs_v1, aap_labeling_v1 TO :"result_role";
GRANT EXECUTE ON FUNCTION
    aap_jobs_v1.claim_batch_result_execution(jsonb),
    aap_jobs_v1.heartbeat_batch_result_execution(jsonb),
    aap_jobs_v1.release_batch_result_execution(jsonb),
    aap_jobs_v1.stage_batch_result_members(jsonb),
    aap_jobs_v1.start_batch_result_execution(jsonb),
    aap_jobs_v1.read_batch_result_execution(jsonb),
    aap_jobs_v1.mark_batch_result_overwrite(jsonb),
    aap_jobs_v1.skip_batch_result_member(jsonb),
    aap_jobs_v1.finish_batch_result_execution(jsonb),
    aap_jobs_v1.start_batch_result_member(jsonb),
    aap_labeling_v1.begin_batch_result(jsonb),
    aap_labeling_v1.put_batch_result_part(jsonb),
    aap_labeling_v1.seal_batch_result(jsonb),
    aap_labeling_v1.read_batch_result(jsonb),
    aap_labeling_v1.read_prediction_retry(jsonb),
    aap_jobs_v1.claim_prediction_retry_deliveries(jsonb),
    aap_jobs_v1.finish_prediction_retry_delivery(jsonb)
    TO :"result_role";

DO $$
DECLARE
    role_name text := current_setting('aap.result_role');
    v_callable integer;
BEGIN
    IF has_schema_privilege(role_name, 'public', 'CREATE')
       OR has_schema_privilege(role_name, 'aap_jobs_v1', 'CREATE')
       OR has_schema_privilege(role_name, 'aap_labeling_v1', 'CREATE')
       OR has_database_privilege(role_name, current_database(), 'TEMP') THEN
        RAISE EXCEPTION 'The preannotation result role has direct schema or TEMP access';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public'
          AND relation.relkind IN ('r','p','v','m','f')
          AND (
              has_table_privilege(role_name, relation.oid,
                  'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
              OR has_any_column_privilege(role_name, relation.oid, 'SELECT')
              OR has_any_column_privilege(role_name, relation.oid, 'INSERT')
              OR has_any_column_privilege(role_name, relation.oid, 'UPDATE')
              OR has_any_column_privilege(role_name, relation.oid, 'REFERENCES')
          )
    ) THEN
        RAISE EXCEPTION 'The preannotation result role has raw table privileges';
    END IF;
    SELECT count(*) INTO v_callable
    FROM pg_catalog.pg_proc AS proc
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = proc.pronamespace
    WHERE namespace.nspname IN ('aap_jobs_v1','aap_labeling_v1')
      AND proc.prokind = 'f'
      AND has_function_privilege(role_name, proc.oid, 'EXECUTE');
    IF v_callable <> 17
       OR has_function_privilege(role_name, 'aap_labeling_v1.lock_batch_result_write(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'aap_labeling_v1.finish_batch_result_write(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'aap_labeling_v1.finish_batch_result_failure(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'aap_jobs_v1.lock_batch_result_execution(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'aap_labeling_v1.admit_prediction_retry(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'aap_labeling_v1.finish_prediction_retry_write(jsonb)', 'EXECUTE')
       OR has_function_privilege(role_name, 'aap_labeling_v1.finish_prediction_retry_failure(jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'The preannotation result role must execute exactly 17 executor capabilities';
    END IF;
END $$;
COMMIT;
