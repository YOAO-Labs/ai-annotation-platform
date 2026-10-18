-- Run with psql as the migration owner after the application migration head is
-- installed. All changes are confined to the selected database and runtime role.
-- psql -v expected_database=annotation_production -v application_role=anno_prod_app
--       -v migration_role=anno_prod_owner -f lan-production-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.application_role', :'application_role', true);
SELECT set_config('aap.migration_role', :'migration_role', true);

DO $$
BEGIN
    IF current_database() <> current_setting('aap.expected_database') THEN
        RAISE EXCEPTION 'Unexpected database; refusing to change application grants';
    END IF;
    IF current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Run this script as the migration owner';
    END IF;
    IF current_setting('aap.application_role') = current_user
       OR pg_has_role(current_setting('aap.application_role'), current_user, 'MEMBER') THEN
        RAISE EXCEPTION 'The runtime role must not inherit the migration owner';
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname = current_setting('aap.application_role')
          AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) THEN
        RAISE EXCEPTION 'The runtime role must not hold cluster administration privileges';
    END IF;
END $$;

REVOKE ALL ON DATABASE :"expected_database" FROM PUBLIC;
-- Concurrent materialized-view refresh uses temporary relations internally.
GRANT CONNECT, TEMPORARY ON DATABASE :"expected_database" TO :"application_role";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO :"application_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"application_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"application_role";
GRANT USAGE ON SCHEMA aap_workspace_v1, aap_assets_v1, aap_labeling_v1,
    aap_jobs_v1 TO :"application_role";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.lock_project_reference(uuid),
    aap_workspace_v1.lock_project_manager(uuid, uuid),
    aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb),
    aap_assets_v1.lock_dataset_reference(uuid),
    aap_assets_v1.lock_current_association(uuid, uuid),
    aap_assets_v1.associate_project(uuid, uuid, uuid),
    aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid),
    aap_labeling_v1.accept_dataset_task_creation(uuid, uuid, uuid, uuid, uuid, uuid, text, integer),
    aap_labeling_v1.prepare_dataset_task_creation(uuid, jsonb),
    aap_labeling_v1.read_dataset_task_creation(uuid),
    aap_labeling_v1.advance_dataset_task_creation(uuid, integer),
    aap_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid),
    aap_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid),
    aap_jobs_v1.claim_task_creation_job(uuid, text),
    aap_jobs_v1.finish_task_creation_job(uuid, uuid),
    aap_jobs_v1.fail_task_creation_job(uuid, uuid, text),
    aap_jobs_v1.cancel_task_creation_job(uuid, uuid)
    TO :"application_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"application_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"application_role";

-- Existing maintenance tasks create/drop monthly partitions and refresh these
-- two materialized views via the normal runtime connection. Ownership is limited
-- to these objects; the migration role inherits the runtime role, never vice versa.
DO $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT relid FROM pg_partition_tree('public.audit_logs')
        UNION
        SELECT relid FROM pg_partition_tree('public.predictions')
    LOOP
        EXECUTE format('ALTER TABLE %s OWNER TO %I', obj.relid::regclass,
                       current_setting('aap.application_role'));
    END LOOP;
    EXECUTE format('ALTER MATERIALIZED VIEW public.mv_user_perf_daily OWNER TO %I',
                   current_setting('aap.application_role'));
    EXECUTE format('ALTER MATERIALIZED VIEW public.mv_audit_bi_daily OWNER TO %I',
                   current_setting('aap.application_role'));
END $$;

REVOKE INSERT, UPDATE, DELETE ON TABLE public.alembic_version FROM :"application_role";
REVOKE DELETE ON TABLE public.gpu_backend_memberships, public.gpu_backend_fences
    FROM :"application_role";

DO $$
BEGIN
    IF has_table_privilege(current_setting('aap.application_role'),
                           'public.gpu_backend_memberships', 'DELETE')
       OR has_table_privilege(current_setting('aap.application_role'),
                              'public.gpu_backend_fences', 'DELETE') THEN
        RAISE EXCEPTION 'The runtime role still has effective GPU tombstone DELETE privileges';
    END IF;
END $$;
COMMIT;
