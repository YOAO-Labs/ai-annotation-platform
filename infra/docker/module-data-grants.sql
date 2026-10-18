-- Run after migrations as the schema owner. Roles must already exist.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v identity_owner=identity_code -v workspace_owner=workspace_code
--   -v assets_owner=assets_code -v labeling_owner=labeling_code
--   -v jobs_owner=jobs_code -v workflow_role=append_client
--   -f infra/docker/module-data-grants.sql
\set ON_ERROR_STOP on
BEGIN;
SELECT set_config('aap.expected_database', :'expected_database', true);
SELECT set_config('aap.migration_role', :'migration_role', true);
SELECT set_config('aap.identity_owner', :'identity_owner', true);
SELECT set_config('aap.workspace_owner', :'workspace_owner', true);
SELECT set_config('aap.assets_owner', :'assets_owner', true);
SELECT set_config('aap.labeling_owner', :'labeling_owner', true);
SELECT set_config('aap.jobs_owner', :'jobs_owner', true);
SELECT set_config('aap.workflow_role', :'workflow_role', true);

DO $$
DECLARE
    role_name text;
    role_names text[] := ARRAY[
        current_setting('aap.identity_owner'),
        current_setting('aap.workspace_owner'),
        current_setting('aap.assets_owner'),
        current_setting('aap.labeling_owner'),
        current_setting('aap.jobs_owner'),
        current_setting('aap.workflow_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT count(DISTINCT n) FROM unnest(role_names) AS n) <> 6 THEN
        RAISE EXCEPTION 'Module owners and caller must be distinct roles';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF NOT EXISTS (
            SELECT FROM pg_roles WHERE rolname = role_name
              AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
              AND rolcanlogin = (role_name = current_setting('aap.workflow_role'))
        ) OR pg_has_role(role_name, current_user, 'MEMBER') THEN
            RAISE EXCEPTION 'Unsafe or missing module role: %', role_name;
        END IF;
        IF EXISTS (
            SELECT FROM pg_auth_members AS m
            WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = role_name)
        ) THEN
            RAISE EXCEPTION 'Module roles must not inherit other roles: %', role_name;
        END IF;
    END LOOP;
END $$;

REVOKE ALL ON SCHEMA aap_identity_v1, aap_workspace_v1, aap_assets_v1,
    aap_labeling_v1, aap_jobs_v1 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_identity_v1, aap_workspace_v1,
    aap_assets_v1, aap_labeling_v1, aap_jobs_v1 FROM PUBLIC;
GRANT CONNECT ON DATABASE :"expected_database" TO :"workflow_role";
GRANT USAGE ON SCHEMA public TO :"identity_owner", :"workspace_owner",
    :"assets_owner", :"labeling_owner", :"jobs_owner";

-- Identity and Workspace provide only the current actor/project facts needed
-- to retain their row locks through the caller-owned transaction.
GRANT SELECT (id, role, is_active), UPDATE (id)
    ON public.users TO :"identity_owner";
GRANT SELECT (id, owner_id, data_type, scene_mode), UPDATE (id)
    ON public.projects TO :"workspace_owner";
GRANT SELECT (project_id, user_id, role), UPDATE (id)
    ON public.project_members TO :"workspace_owner";

-- Assets association and source locks use only the listed source columns.
GRANT SELECT (id, data_type), UPDATE (id)
    ON public.datasets TO :"assets_owner";
GRANT SELECT (id, project_id, dataset_id), INSERT (id, project_id, dataset_id),
    UPDATE (id) ON public.project_datasets TO :"assets_owner";
GRANT SELECT (dataset_id) ON public.scenes TO :"assets_owner";
GRANT SELECT (id, dataset_id, file_name, file_path, file_type), UPDATE (id)
    ON public.dataset_items TO :"assets_owner";

-- Labeling owns all task creation mutations, counter writes, and receipts.
GRANT SELECT (id, project_id, dataset_item_id) ON public.tasks TO :"labeling_owner";
GRANT INSERT (
    id, project_id, dataset_item_id, display_id, file_name, file_path, file_type,
    tags, status, assignee_is_override, is_labeled, overlap, total_annotations,
    total_predictions, reviewer_is_override, first_review_eligible,
    annotation_contributor_ids, reopened_count, batch_id, version
) ON public.tasks TO :"labeling_owner";
GRANT SELECT (id, total_tasks), UPDATE (total_tasks, updated_at)
    ON public.projects TO :"labeling_owner";
GRANT SELECT (task_id), INSERT (id, task_id, dataset_item_id, role, sensor_name)
    ON public.task_dataset_item_links TO :"labeling_owner";
GRANT USAGE ON SEQUENCE public.display_seq_tasks TO :"labeling_owner";
GRANT SELECT, INSERT, UPDATE ON public.task_append_operations TO :"labeling_owner";
GRANT SELECT (
    id, actor_id, dataset_id, project_id, association_id, job_id, mode,
    chunk_size, request, manifest, total, next_chunk, processed, created,
    state, created_at, updated_at
), INSERT (
    id, actor_id, dataset_id, project_id, association_id, job_id, mode,
    chunk_size, request
), UPDATE (
    manifest, total, next_chunk, processed, created, state, updated_at
) ON public.task_creation_operations TO :"labeling_owner";
GRANT SELECT (operation_id, chunk_index, result),
    INSERT (operation_id, chunk_index, result), UPDATE (chunk_index)
    ON public.task_creation_chunks TO :"labeling_owner";

-- Jobs writes its own lifecycle row; no direct table access reaches the caller.
GRANT SELECT ON public.async_jobs TO :"jobs_owner";
GRANT INSERT (id, kind, project_id, user_id, status, progress_pct, payload, result)
    ON public.async_jobs TO :"jobs_owner";
GRANT UPDATE (
    status, progress_pct, result, error_message, celery_task_id, started_at,
    completed_at, updated_at
) ON public.async_jobs TO :"jobs_owner";

-- Cross-owner function calls share one PostgreSQL transaction and retain the
-- dataset -> current authority -> association -> Jobs/Labeling lock order.
GRANT USAGE ON SCHEMA aap_identity_v1 TO :"identity_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA aap_workspace_v1 TO :"workspace_owner", :"assets_owner",
    :"labeling_owner", :"jobs_owner", :"workflow_role";
GRANT USAGE ON SCHEMA aap_assets_v1 TO :"assets_owner", :"labeling_owner",
    :"jobs_owner", :"workflow_role";
GRANT USAGE ON SCHEMA aap_labeling_v1 TO :"labeling_owner", :"jobs_owner",
    :"workflow_role";
GRANT USAGE ON SCHEMA aap_jobs_v1 TO :"jobs_owner", :"labeling_owner",
    :"workflow_role";

GRANT EXECUTE ON FUNCTION aap_identity_v1.lock_active_actor(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.lock_project_reference(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.lock_project_manager(uuid, uuid)
    TO :"assets_owner", :"labeling_owner", :"jobs_owner", :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_dataset_reference(uuid),
    aap_assets_v1.lock_current_association(uuid, uuid),
    aap_assets_v1.lock_append_sources(uuid, uuid[]),
    aap_assets_v1.lock_task_creation_items(uuid, uuid[])
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_current_association(uuid, uuid)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_dataset_reference(uuid)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.associate_project(uuid, uuid, uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid),
    aap_labeling_v1.accept_dataset_task_creation(uuid, uuid, uuid, uuid, uuid, uuid, text, integer),
    aap_labeling_v1.prepare_dataset_task_creation(uuid, jsonb),
    aap_labeling_v1.read_dataset_task_creation(uuid),
    aap_labeling_v1.advance_dataset_task_creation(uuid, integer)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.read_dataset_task_creation(uuid)
    TO :"jobs_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.lock_task_creation_job(uuid, uuid, uuid, uuid, uuid, uuid),
    aap_jobs_v1.update_task_creation_progress(uuid, uuid, bigint, bigint, bigint)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid),
    aap_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid),
    aap_jobs_v1.claim_task_creation_job(uuid, text),
    aap_jobs_v1.finish_task_creation_job(uuid, uuid),
    aap_jobs_v1.fail_task_creation_job(uuid, uuid, text),
    aap_jobs_v1.cancel_task_creation_job(uuid, uuid)
    TO :"workflow_role";

-- Keep schemas owned by the migrator; NOLOGIN code owners cannot replace code.
GRANT CREATE ON SCHEMA aap_identity_v1 TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.lock_active_actor(uuid) OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_v1 FROM :"identity_owner";

GRANT CREATE ON SCHEMA aap_workspace_v1 TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_project_reference(uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_project_manager(uuid, uuid) OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_workspace_v1 FROM :"workspace_owner";

GRANT CREATE ON SCHEMA aap_assets_v1 TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_dataset_reference(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_current_association(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_append_sources(uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.associate_project(uuid, uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_task_creation_items(uuid, uuid[]) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA aap_assets_v1 FROM :"assets_owner";

GRANT CREATE ON SCHEMA aap_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.accept_dataset_task_creation(
    uuid, uuid, uuid, uuid, uuid, uuid, text, integer
) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.prepare_dataset_task_creation(uuid, jsonb) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.read_dataset_task_creation(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.advance_dataset_task_creation(uuid, integer) OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA aap_labeling_v1 FROM :"labeling_owner";

GRANT CREATE ON SCHEMA aap_jobs_v1 TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._task_creation_job_failure(text, text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1._task_creation_job_payload_valid(uuid, jsonb) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.create_task_creation_job(uuid, uuid, uuid, uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.find_task_creation_job(uuid, uuid, uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.claim_task_creation_job(uuid, text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.lock_task_creation_job(uuid, uuid, uuid, uuid, uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.update_task_creation_progress(uuid, uuid, bigint, bigint, bigint) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.finish_task_creation_job(uuid, uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.fail_task_creation_job(uuid, uuid, text) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.cancel_task_creation_job(uuid, uuid) OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM :"jobs_owner";

DO $$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'public.users', 'public.projects', 'public.project_members', 'public.datasets',
        'public.scenes', 'public.dataset_items', 'public.project_datasets',
        'public.tasks', 'public.task_dataset_item_links', 'public.task_append_operations',
        'public.task_creation_operations', 'public.task_creation_chunks', 'public.async_jobs'
    ] LOOP
        IF has_any_column_privilege(current_setting('aap.workflow_role'), target, 'SELECT,INSERT,UPDATE,REFERENCES')
           OR has_table_privilege(current_setting('aap.workflow_role'), target, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'Caller has direct privileges on %; use a clean dedicated role', target;
        END IF;
    END LOOP;

    IF has_database_privilege(current_setting('aap.workflow_role'), current_database(), 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'public', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_identity_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_workspace_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_assets_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_labeling_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_jobs_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Caller must not hold schema or database CREATE privileges';
    END IF;

    IF has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.lock_task_creation_job(uuid,uuid,uuid,uuid,uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.update_task_creation_progress(uuid,uuid,bigint,bigint,bigint)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1._task_creation_job_failure(text,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1._task_creation_job_payload_valid(uuid,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller must not execute Jobs-internal capabilities';
    END IF;
END $$;
COMMIT;
