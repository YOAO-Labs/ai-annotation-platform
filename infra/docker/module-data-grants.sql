-- Run after migrations as the schema owner. Roles must already exist.
-- psql -v expected_database=annotation_test -v migration_role=owner
--   -v identity_owner=identity_code -v workspace_owner=workspace_code
--   -v assets_owner=assets_code -v labeling_owner=labeling_code
--   -v jobs_owner=jobs_code -v learning_owner=learning_code
--   -v inference_owner=inference_code -v insights_owner=insights_code
--   -v diagnostics_owner=diagnostics_code -v export_owner=export_code
--   -v audit_owner=audit_code -v workflow_role=append_client
--   -v asset_cleanup_role=asset_cleanup_client
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
SELECT set_config('aap.learning_owner', :'learning_owner', true);
SELECT set_config('aap.inference_owner', :'inference_owner', true);
SELECT set_config('aap.insights_owner', :'insights_owner', true);
SELECT set_config('aap.diagnostics_owner', :'diagnostics_owner', true);
SELECT set_config('aap.export_owner', :'export_owner', true);
SELECT set_config('aap.audit_owner', :'audit_owner', true);
SELECT set_config('aap.workflow_role', :'workflow_role', true);
SELECT set_config('aap.asset_cleanup_role', :'asset_cleanup_role', true);

DO $$
DECLARE
    role_name text;
    role_names text[] := ARRAY[
        current_setting('aap.identity_owner'),
        current_setting('aap.workspace_owner'),
        current_setting('aap.assets_owner'),
        current_setting('aap.labeling_owner'),
        current_setting('aap.jobs_owner'),
        current_setting('aap.learning_owner'),
        current_setting('aap.inference_owner'),
        current_setting('aap.insights_owner'),
        current_setting('aap.diagnostics_owner'),
        current_setting('aap.export_owner'),
        current_setting('aap.audit_owner'),
        current_setting('aap.workflow_role'),
        current_setting('aap.asset_cleanup_role')
    ];
BEGIN
    IF current_database() <> current_setting('aap.expected_database')
       OR current_user <> current_setting('aap.migration_role') THEN
        RAISE EXCEPTION 'Unexpected database or migration owner';
    END IF;
    IF (SELECT count(DISTINCT n) FROM unnest(role_names) AS n) <> 13 THEN
        RAISE EXCEPTION 'Module owners and caller must be distinct roles';
    END IF;
    FOREACH role_name IN ARRAY role_names LOOP
        IF NOT EXISTS (
            SELECT FROM pg_roles WHERE rolname = role_name
              AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
              AND rolcanlogin = (role_name IN (
                  current_setting('aap.workflow_role'),
                  current_setting('aap.asset_cleanup_role')
              ))
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
    IF EXISTS (
        SELECT FROM pg_auth_members AS membership
        WHERE membership.roleid = (
            SELECT oid FROM pg_roles
            WHERE rolname = current_setting('aap.asset_cleanup_role')
        )
    ) THEN
        RAISE EXCEPTION 'The Assets cleanup role must not have inheriting members';
    END IF;
END $$;

REVOKE ALL ON SCHEMA aap_identity_v1, aap_workspace_v1, aap_assets_v1,
    aap_labeling_v1, aap_jobs_v1, aap_learning_v1, aap_inference_v1,
    aap_insights_v1, aap_diagnostics_v1, aap_export_v1, aap_audit_v1 FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_identity_v1, aap_workspace_v1,
    aap_assets_v1, aap_labeling_v1, aap_jobs_v1, aap_learning_v1,
    aap_inference_v1, aap_insights_v1, aap_diagnostics_v1, aap_export_v1,
    aap_audit_v1 FROM PUBLIC;
REVOKE ALL ON SCHEMA aap_identity_v1, aap_workspace_v1, aap_assets_v1,
    aap_labeling_v1, aap_jobs_v1, aap_learning_v1, aap_inference_v1,
    aap_insights_v1, aap_diagnostics_v1, aap_export_v1, aap_audit_v1
    FROM :"asset_cleanup_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA aap_identity_v1, aap_workspace_v1,
    aap_assets_v1, aap_labeling_v1, aap_jobs_v1, aap_learning_v1,
    aap_inference_v1, aap_insights_v1, aap_diagnostics_v1, aap_export_v1,
    aap_audit_v1 FROM :"asset_cleanup_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"workflow_role";
REVOKE ALL ON DATABASE :"expected_database" FROM :"asset_cleanup_role";
GRANT CONNECT ON DATABASE :"expected_database" TO :"asset_cleanup_role";
REVOKE ALL ON SCHEMA public FROM :"asset_cleanup_role";
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM :"asset_cleanup_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM :"asset_cleanup_role";
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM :"asset_cleanup_role";
GRANT USAGE ON SCHEMA public TO :"identity_owner", :"workspace_owner",
    :"assets_owner", :"labeling_owner", :"jobs_owner", :"learning_owner",
    :"inference_owner", :"insights_owner", :"diagnostics_owner",
    :"export_owner", :"audit_owner";

-- Identity and Workspace provide only the current actor/project facts needed
-- to retain their row locks through the caller-owned transaction.
GRANT SELECT (id, role, is_active, email), UPDATE (id)
    ON public.users TO :"identity_owner";
GRANT SELECT (id, owner_id, data_type, scene_mode, display_id, name), UPDATE (id),
    DELETE ON public.projects TO :"workspace_owner";
GRANT SELECT (project_id, user_id, role), UPDATE (id), DELETE
    ON public.project_members TO :"workspace_owner";
GRANT SELECT (source_project_id), UPDATE (source_project_id)
    ON public.project_templates TO :"workspace_owner";
GRANT SELECT (operation_id, actor_id, project_id, request, result, created_at),
    INSERT (operation_id, actor_id, project_id, request, result), UPDATE (operation_id)
    ON public.project_deletion_operations TO :"workspace_owner";
GRANT SELECT (operation_id, actor_id, dataset_id, project_id, request, result, created_at),
    INSERT (operation_id, actor_id, dataset_id, project_id, request, result),
    UPDATE (operation_id) ON public.dataset_unlink_operations TO :"workspace_owner";
GRANT SELECT (id), INSERT (
    actor_id, actor_email, actor_role, action, target_type, target_id,
    method, path, status_code, ip, request_id, detail_json
) ON public.audit_logs TO :"audit_owner";
DO $$
BEGIN
    EXECUTE format(
        'GRANT USAGE ON SEQUENCE %s TO %I',
        pg_get_serial_sequence('public.audit_logs', 'id'),
        current_setting('aap.audit_owner')
    );
END $$;

-- Assets owns catalog/source rows and their exact upload/source-deletion receipts.
GRANT SELECT (id, data_type, name), UPDATE (id, file_count, updated_at), DELETE
    ON public.datasets TO :"assets_owner";
GRANT SELECT (id, project_id, dataset_id), INSERT (id, project_id, dataset_id),
    UPDATE (id) ON public.project_datasets TO :"assets_owner";
GRANT DELETE ON public.project_datasets TO :"assets_owner";
GRANT SELECT (id, dataset_id), UPDATE (id), DELETE
    ON public.scenes TO :"assets_owner";
GRANT SELECT, INSERT (
    id, dataset_id, file_name, file_path, file_type, file_size,
    content_hash, width, height, metadata
), UPDATE (id), DELETE
    ON public.dataset_items TO :"assets_owner";
GRANT SELECT ON public.dataset_upload_intents TO :"assets_owner";
GRANT INSERT (
    item_id, dataset_id, actor_id, file_name, file_type, content_type, staging_key
), UPDATE (file_path, publication_id, state, result, updated_at)
    ON public.dataset_upload_intents TO :"assets_owner";
GRANT SELECT (operation_id, actor_id, dataset_id, item_id, request, result, created_at),
    INSERT (operation_id, actor_id, dataset_id, item_id, request, result),
    UPDATE (operation_id) ON public.dataset_source_deletion_operations
    TO :"assets_owner";
GRANT SELECT (project_id), DELETE ON public.storage_connections TO :"assets_owner";
GRANT SELECT (id, task_id, dataset_item_id), UPDATE (id), DELETE
    ON public.image_pyramid_assets TO :"assets_owner";
GRANT SELECT (asset_id, generation, status, lease_expires_at), UPDATE (generation)
    ON public.image_pyramid_generations TO :"assets_owner";
GRANT SELECT (
    operation_id, asset_id, bucket, prefix, created_at, completed_at, not_before
), INSERT (operation_id, asset_id, bucket, prefix, not_before),
    UPDATE (completed_at)
    ON public.asset_storage_deletions TO :"assets_owner";

-- Labeling owns all task creation mutations, counter writes, and receipts.
GRANT SELECT (id, project_id, dataset_item_id, batch_id, status), UPDATE (id), DELETE
    ON public.tasks TO :"labeling_owner";
GRANT SELECT (id, project_id, dataset_id, display_id, annotator_id, status),
    UPDATE (id, dataset_id, total_tasks, completed_tasks, review_tasks, updated_at), DELETE
    ON public.task_batches TO :"labeling_owner";
GRANT SELECT (id, project_id, task_id), DELETE
    ON public.predictions TO :"labeling_owner";
GRANT SELECT (id, project_id, task_id), DELETE
    ON public.failed_predictions TO :"labeling_owner";
GRANT SELECT (prediction_id, failed_prediction_id), DELETE
    ON public.prediction_metas TO :"labeling_owner";
GRANT SELECT (
    id, project_id, task_id, parent_annotation_id, parent_prediction_id,
    sensor_dataset_item_id
), UPDATE (parent_annotation_id, parent_prediction_id), DELETE
    ON public.annotations TO :"labeling_owner";
GRANT SELECT (id, project_id, task_id, annotation_id, thread_parent_id),
    UPDATE (thread_parent_id), DELETE
    ON public.annotation_feedbacks TO :"labeling_owner";
GRANT SELECT (task_id, annotation_id), DELETE
    ON public.annotation_drafts TO :"labeling_owner";
GRANT SELECT (task_id), DELETE ON public.task_locks TO :"labeling_owner";
GRANT SELECT (project_id, annotation_id), DELETE
    ON public.annotation_comments TO :"labeling_owner";
GRANT SELECT (project_id), DELETE ON public.task_events TO :"labeling_owner";
GRANT SELECT (project_id), DELETE ON public.mask_review_scopes TO :"labeling_owner";
GRANT SELECT (project_id), DELETE ON public.mask_qc_issues TO :"labeling_owner";
GRANT SELECT (project_id), DELETE ON public.mask_qc_runs TO :"labeling_owner";
GRANT SELECT (project_id, scene_id), DELETE
    ON public.point_cloud_quality_issues TO :"labeling_owner";
GRANT SELECT (project_id), DELETE
    ON public.point_cloud_quality_evaluations TO :"labeling_owner";
GRANT SELECT (project_id, scope_json), DELETE
    ON public.point_cloud_quality_runs TO :"labeling_owner";
GRANT SELECT (project_id), DELETE
    ON public.mask_annotation_revisions TO :"labeling_owner";
GRANT SELECT (project_id), DELETE
    ON public.mask_repair_batches TO :"labeling_owner";
GRANT SELECT (project_id), DELETE
    ON public.mask_format_imports TO :"labeling_owner";
GRANT SELECT (project_id, scene_id), DELETE ON public.scene_tracks TO :"labeling_owner";
GRANT SELECT (scene_id) ON public.scene_track_operations TO :"labeling_owner";
GRANT SELECT (dataset_item_id) ON public.video_segments,
    public.video_tracker_jobs TO :"labeling_owner";
GRANT INSERT (
    id, project_id, dataset_item_id, display_id, file_name, file_path, file_type,
    tags, status, assignee_is_override, is_labeled, overlap, total_annotations,
    total_predictions, reviewer_is_override, first_review_eligible,
    annotation_contributor_ids, reopened_count, batch_id, version
) ON public.tasks TO :"labeling_owner";
GRANT SELECT (id, total_tasks), UPDATE (
    total_tasks, completed_tasks, review_tasks, in_progress_tasks,
    batch_summary, updated_at
)
    ON public.projects TO :"labeling_owner";
GRANT SELECT (task_id, dataset_item_id, role),
    INSERT (id, task_id, dataset_item_id, role, sensor_name)
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
    completed_at, updated_at, project_id, retained_project_id
) ON public.async_jobs TO :"jobs_owner";

-- Learning holds project retention evidence, task-derived streams and recipes.
GRANT SELECT (project_id) ON public.active_learning_rounds TO :"learning_owner";
GRANT SELECT (task_id, dataset_item_id) ON public.active_learning_pool_members,
    public.active_learning_candidates TO :"learning_owner";
GRANT SELECT (project_id) ON public.training_studies,
    public.trained_model_versions, public.project_model_selections,
    public.project_model_releases, public.model_decisions,
    public.sample_evaluation_runs, public.learning_policies,
    public.training_dataset_versions, public.training_runs,
    public.training_previews, public.learning_triggers
    TO :"learning_owner";
GRANT SELECT (project_id), DELETE ON public.project_task_datasets,
    public.training_recipes, public.training_recipe_revisions
    TO :"learning_owner";

-- Other owners expose only their project cleanup predicates and row mutations.
GRANT SELECT (project_id), DELETE ON public.project_ml_backend_pool,
    public.project_pipelines TO :"inference_owner";
GRANT SELECT (project_id), DELETE ON public.project_task_views TO :"insights_owner";
GRANT SELECT (project_id, task_id), UPDATE (project_id, task_id)
    ON public.bug_reports TO :"diagnostics_owner";
GRANT SELECT (project_id), DELETE ON public.export_artifacts TO :"export_owner";

-- Cross-owner function calls share one PostgreSQL transaction and retain the
-- dataset -> current authority -> association -> Jobs/Labeling lock order.
GRANT USAGE ON SCHEMA aap_identity_v1
    TO :"identity_owner", :"workspace_owner", :"assets_owner", :"audit_owner";
GRANT USAGE ON SCHEMA aap_workspace_v1 TO :"workspace_owner", :"assets_owner",
    :"labeling_owner", :"jobs_owner", :"workflow_role";
GRANT USAGE ON SCHEMA aap_assets_v1 TO :"assets_owner", :"labeling_owner",
    :"jobs_owner", :"learning_owner", :"workspace_owner", :"workflow_role",
    :"asset_cleanup_role";
GRANT USAGE ON SCHEMA aap_labeling_v1 TO :"labeling_owner", :"jobs_owner",
    :"assets_owner", :"learning_owner", :"diagnostics_owner",
    :"workspace_owner", :"workflow_role";
GRANT USAGE ON SCHEMA aap_jobs_v1 TO :"jobs_owner", :"labeling_owner",
    :"workspace_owner", :"assets_owner", :"workflow_role";
GRANT USAGE ON SCHEMA aap_learning_v1
    TO :"learning_owner", :"workspace_owner", :"assets_owner";
GRANT USAGE ON SCHEMA aap_inference_v1 TO :"inference_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA aap_insights_v1 TO :"insights_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA aap_diagnostics_v1
    TO :"diagnostics_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA aap_export_v1 TO :"export_owner", :"workspace_owner";
GRANT USAGE ON SCHEMA aap_audit_v1
    TO :"audit_owner", :"workspace_owner", :"assets_owner";

GRANT EXECUTE ON FUNCTION aap_identity_v1.lock_active_actor(uuid)
    TO :"workspace_owner", :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_identity_v1.lock_audit_actor(uuid)
    TO :"audit_owner";
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
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_current_association(uuid, uuid),
    aap_assets_v1.lock_dataset_reference(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.lock_scene_source_references(uuid[])
    TO :"labeling_owner", :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.associate_project(uuid, uuid, uuid)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.delete_project_owned_records(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.project_task_references(uuid)
    TO :"assets_owner", :"learning_owner", :"diagnostics_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.dataset_item_references(uuid, uuid)
    TO :"labeling_owner", :"learning_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.dataset_scene_references(uuid),
    aap_assets_v1.dataset_association_references(uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.dataset_task_references(uuid, uuid)
    TO :"assets_owner", :"learning_owner", :"diagnostics_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.delete_project_owned_records(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid),
    aap_labeling_v1.accept_dataset_task_creation(uuid, uuid, uuid, uuid, uuid, uuid, text, integer),
    aap_labeling_v1.prepare_dataset_task_creation(uuid, jsonb),
    aap_labeling_v1.read_dataset_task_creation(uuid),
    aap_labeling_v1.advance_dataset_task_creation(uuid, integer)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid)
    TO :"assets_owner";
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
GRANT EXECUTE ON FUNCTION aap_jobs_v1.detach_project_references(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.cancel_dataset_task_creation(uuid, uuid, uuid)
    TO :"workspace_owner", :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.guard_dataset_source_deletion(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_jobs_v1.task_creation_execution_active(uuid, uuid)
    TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION aap_learning_v1.lock_project_deletion(uuid),
    aap_learning_v1.guard_project_deletion(uuid),
    aap_learning_v1.delete_project_owned_records(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_inference_v1.clear_project_ml_backend_pool(uuid),
    aap_inference_v1.clear_project_pipelines(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_insights_v1.clear_project_task_views(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_diagnostics_v1.detach_project_references(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_export_v1.clear_project_export_artifacts(uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.record_project_deletion(
    uuid, uuid, uuid, text, text, bigint, bigint, jsonb
) TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_learning_v1.guard_dataset_unlink(uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_learning_v1.guard_dataset_source_deletion(uuid, uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.guard_dataset_source_deletion(uuid, uuid),
    aap_labeling_v1.detach_deleted_dataset_attribution(uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_labeling_v1.lock_dataset_unlink_scope(uuid, uuid),
    aap_labeling_v1.guard_dataset_unlink(uuid, uuid),
    aap_labeling_v1.preview_dataset_unlink(uuid, uuid),
    aap_labeling_v1.delete_dataset_tasks(uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.prepare_dataset_unlink_cleanup(
    uuid, uuid, uuid, text
), aap_assets_v1.remove_dataset_association(uuid, uuid, uuid)
    TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_diagnostics_v1.detach_dataset_task_references(
    uuid, uuid
) TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.valid_request_context(jsonb),
    aap_audit_v1.record_dataset_unlink(
        uuid, uuid, uuid, uuid, bigint, bigint, uuid[], jsonb
    ) TO :"workspace_owner";
GRANT EXECUTE ON FUNCTION aap_audit_v1.valid_request_context(jsonb),
    aap_audit_v1.record_dataset_source_deletion(
        uuid, uuid, uuid, uuid, bigint, bigint, bigint, bigint, bigint, bigint, jsonb
    ) TO :"assets_owner";
GRANT EXECUTE ON FUNCTION aap_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
),
    aap_assets_v1.complete_storage_deletion(uuid, uuid)
    TO :"asset_cleanup_role";
REVOKE ALL ON FUNCTION aap_assets_v1.pending_storage_deletions(uuid, integer)
    FROM :"asset_cleanup_role";
GRANT EXECUTE ON FUNCTION aap_assets_v1.delete_dataset(
    uuid, uuid, uuid, text, jsonb
), aap_assets_v1.delete_dataset_item(
    uuid, uuid, uuid, uuid, text, jsonb
), aap_assets_v1.reserve_dataset_upload(
    uuid, uuid, uuid, text, text
), aap_assets_v1.lock_dataset_upload(uuid, uuid, uuid),
    aap_assets_v1.find_dataset_upload_duplicate(uuid, uuid, uuid, text),
    aap_assets_v1.complete_dataset_upload(
        uuid, uuid, uuid, uuid, bigint, text, integer, integer
    ), aap_assets_v1.discard_dataset_upload(uuid, uuid, uuid, text)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid),
    aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)
    TO :"workflow_role";
GRANT EXECUTE ON FUNCTION aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)
    TO :"workflow_role";

-- Keep schemas owned by the migrator; NOLOGIN code owners cannot replace code.
GRANT CREATE ON SCHEMA aap_identity_v1 TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.lock_active_actor(uuid) OWNER TO :"identity_owner";
ALTER FUNCTION aap_identity_v1.lock_audit_actor(uuid) OWNER TO :"identity_owner";
REVOKE CREATE ON SCHEMA aap_identity_v1 FROM :"identity_owner";

GRANT CREATE ON SCHEMA aap_workspace_v1 TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_project_reference(uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.lock_project_manager(uuid, uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1._project_delete_failure(text) OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1._dataset_unlink_failure(text, text)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid)
    OWNER TO :"workspace_owner";
ALTER FUNCTION aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb)
    OWNER TO :"workspace_owner";
REVOKE CREATE ON SCHEMA aap_workspace_v1 FROM :"workspace_owner";

GRANT CREATE ON SCHEMA aap_assets_v1 TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_dataset_reference(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_current_association(uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_append_sources(uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.associate_project(uuid, uuid, uuid) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_task_creation_items(uuid, uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_scene_source_references(uuid[])
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.delete_project_owned_records(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.dataset_item_references(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.dataset_scene_references(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.remove_dataset_association(uuid, uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.prepare_dataset_unlink_cleanup(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.pending_storage_deletions(uuid, integer)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.complete_storage_deletion(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._dataset_upload_failure(text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._dataset_upload_success(jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._dataset_upload_actor(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._dataset_upload_facts(public.dataset_upload_intents)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._accept_dataset_upload(
    uuid, uuid, uuid, uuid, bigint, text, integer, integer
) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._finish_dataset_upload(uuid, uuid, uuid, uuid, bigint)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.reserve_dataset_upload(uuid, uuid, uuid, text, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.lock_dataset_upload(uuid, uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.find_dataset_upload_duplicate(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.complete_dataset_upload(
    uuid, uuid, uuid, uuid, bigint, text, integer, integer
) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.discard_dataset_upload(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._source_delete_failure(text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.dataset_association_references(uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._lock_dataset_source_deletion(uuid, uuid)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._queue_pyramid_deletions(uuid, uuid[], text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._delete_dataset_source_records(uuid, uuid, uuid, text)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1._delete_dataset_source(
    uuid, uuid, uuid, uuid, text, jsonb
) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.delete_dataset(uuid, uuid, uuid, text, jsonb)
    OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.delete_dataset_item(
    uuid, uuid, uuid, uuid, text, jsonb
) OWNER TO :"assets_owner";
ALTER FUNCTION aap_assets_v1.pending_storage_deletions_v2(
    text, timestamptz, uuid, uuid, integer
) OWNER TO :"assets_owner";
REVOKE CREATE ON SCHEMA aap_assets_v1 FROM :"assets_owner";

GRANT CREATE ON SCHEMA aap_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.append_dataset_items(uuid, uuid[], uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.accept_dataset_task_creation(
    uuid, uuid, uuid, uuid, uuid, uuid, text, integer
) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.prepare_dataset_task_creation(uuid, jsonb) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.read_dataset_task_creation(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.advance_dataset_task_creation(uuid, integer) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.project_task_references(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.delete_project_owned_records(uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1._dataset_unlink_task_snapshot(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.dataset_task_references(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.lock_dataset_unlink_scope(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.guard_dataset_unlink(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.preview_dataset_unlink(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.delete_dataset_tasks(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.guard_dataset_source_deletion(uuid, uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION aap_labeling_v1.detach_deleted_dataset_attribution(uuid)
    OWNER TO :"labeling_owner";
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
ALTER FUNCTION aap_jobs_v1.detach_project_references(uuid) OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.retain_async_job_project_scope() OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.cancel_dataset_task_creation(uuid, uuid, uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.guard_dataset_source_deletion(uuid)
    OWNER TO :"jobs_owner";
ALTER FUNCTION aap_jobs_v1.task_creation_execution_active(uuid, uuid)
    OWNER TO :"jobs_owner";
REVOKE CREATE ON SCHEMA aap_jobs_v1 FROM :"jobs_owner";

GRANT CREATE ON SCHEMA aap_learning_v1 TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.lock_project_deletion(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.guard_project_deletion(uuid) OWNER TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.delete_project_owned_records(uuid)
    OWNER TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.guard_dataset_item_deletion(uuid, uuid)
    OWNER TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.guard_dataset_unlink(uuid, uuid)
    OWNER TO :"learning_owner";
ALTER FUNCTION aap_learning_v1.guard_dataset_source_deletion(uuid, uuid)
    OWNER TO :"learning_owner";
REVOKE CREATE ON SCHEMA aap_learning_v1 FROM :"learning_owner";

GRANT CREATE ON SCHEMA aap_inference_v1 TO :"inference_owner";
ALTER FUNCTION aap_inference_v1.clear_project_ml_backend_pool(uuid)
    OWNER TO :"inference_owner";
ALTER FUNCTION aap_inference_v1.clear_project_pipelines(uuid)
    OWNER TO :"inference_owner";
REVOKE CREATE ON SCHEMA aap_inference_v1 FROM :"inference_owner";

GRANT CREATE ON SCHEMA aap_insights_v1 TO :"insights_owner";
ALTER FUNCTION aap_insights_v1.clear_project_task_views(uuid)
    OWNER TO :"insights_owner";
REVOKE CREATE ON SCHEMA aap_insights_v1 FROM :"insights_owner";

GRANT CREATE ON SCHEMA aap_diagnostics_v1 TO :"diagnostics_owner";
ALTER FUNCTION aap_diagnostics_v1.detach_project_references(uuid)
    OWNER TO :"diagnostics_owner";
ALTER FUNCTION aap_diagnostics_v1.detach_dataset_task_references(uuid, uuid)
    OWNER TO :"diagnostics_owner";
REVOKE CREATE ON SCHEMA aap_diagnostics_v1 FROM :"diagnostics_owner";

GRANT CREATE ON SCHEMA aap_export_v1 TO :"export_owner";
ALTER FUNCTION aap_export_v1.clear_project_export_artifacts(uuid)
    OWNER TO :"export_owner";
REVOKE CREATE ON SCHEMA aap_export_v1 FROM :"export_owner";

GRANT CREATE ON SCHEMA aap_audit_v1 TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_project_deletion(
    uuid, uuid, uuid, text, text, bigint, bigint, jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.valid_request_context(jsonb)
    OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_dataset_unlink(
    uuid, uuid, uuid, uuid, bigint, bigint, uuid[], jsonb
) OWNER TO :"audit_owner";
ALTER FUNCTION aap_audit_v1.record_dataset_source_deletion(
    uuid, uuid, uuid, uuid, bigint, bigint, bigint, bigint, bigint, bigint, jsonb
) OWNER TO :"audit_owner";
REVOKE CREATE ON SCHEMA aap_audit_v1 FROM :"audit_owner";

DO $$
DECLARE
    target text;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'public.users', 'public.projects', 'public.project_members',
        'public.project_templates', 'public.project_deletion_operations',
        'public.dataset_unlink_operations', 'public.dataset_source_deletion_operations',
        'public.dataset_upload_intents', 'public.asset_storage_deletions',
        'public.audit_logs', 'public.datasets', 'public.scenes',
        'public.dataset_items', 'public.project_datasets',
        'public.storage_connections', 'public.image_pyramid_assets',
        'public.tasks', 'public.task_batches', 'public.task_locks',
        'public.predictions', 'public.prediction_metas', 'public.failed_predictions',
        'public.annotations', 'public.annotation_feedbacks', 'public.annotation_drafts',
        'public.annotation_comments', 'public.task_events',
        'public.scene_tracks', 'public.mask_review_scopes',
        'public.mask_qc_runs', 'public.mask_qc_issues',
        'public.mask_annotation_revisions', 'public.mask_repair_batches',
        'public.mask_format_imports', 'public.point_cloud_quality_runs',
        'public.point_cloud_quality_issues', 'public.point_cloud_quality_evaluations',
        'public.task_append_operations',
        'public.task_creation_operations', 'public.task_creation_chunks',
        'public.active_learning_rounds', 'public.active_learning_pool_members',
        'public.active_learning_candidates', 'public.training_studies',
        'public.trained_model_versions', 'public.project_model_selections',
        'public.project_model_releases', 'public.model_decisions',
        'public.sample_evaluation_runs', 'public.learning_policies',
        'public.training_dataset_versions', 'public.training_runs',
        'public.training_previews', 'public.learning_triggers',
        'public.project_task_datasets', 'public.training_recipes',
        'public.training_recipe_revisions', 'public.project_ml_backend_pool',
        'public.project_pipelines', 'public.project_task_views',
        'public.bug_reports', 'public.export_artifacts', 'public.async_jobs'
    ] LOOP
        IF has_any_column_privilege(current_setting('aap.workflow_role'), target, 'SELECT,INSERT,UPDATE,REFERENCES')
           OR has_table_privilege(current_setting('aap.workflow_role'), target, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'Caller has direct privileges on %; use a clean dedicated role', target;
        END IF;
        IF has_any_column_privilege(current_setting('aap.asset_cleanup_role'), target, 'SELECT,INSERT,UPDATE,REFERENCES')
           OR has_table_privilege(current_setting('aap.asset_cleanup_role'), target, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') THEN
            RAISE EXCEPTION 'Asset cleanup role has direct privileges on %; use a clean dedicated role', target;
        END IF;
    END LOOP;

    IF has_database_privilege(current_setting('aap.workflow_role'), current_database(), 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'public', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_identity_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_workspace_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_assets_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_labeling_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_jobs_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_learning_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_inference_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_insights_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_diagnostics_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_export_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.workflow_role'), 'aap_audit_v1', 'CREATE') THEN
        RAISE EXCEPTION 'Caller must not hold schema or database CREATE privileges';
    END IF;

    IF has_database_privilege(current_setting('aap.asset_cleanup_role'), current_database(), 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'public', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_identity_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_workspace_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_assets_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_labeling_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_jobs_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_learning_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_inference_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_insights_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_diagnostics_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_export_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_audit_v1', 'CREATE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_identity_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_workspace_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_labeling_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_jobs_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_learning_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_inference_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_insights_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_diagnostics_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_export_v1', 'USAGE')
       OR has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_audit_v1', 'USAGE') THEN
        RAISE EXCEPTION 'Asset cleanup role must be confined to the Assets schema';
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

    IF has_function_privilege(current_setting('aap.workflow_role'),
        'aap_identity_v1.lock_active_actor(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1.lock_project_reference(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_dataset_reference(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_current_association(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_append_sources(uuid,uuid[])', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_task_creation_items(uuid,uuid[])', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_identity_v1.lock_audit_actor(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1._project_delete_failure(text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.delete_project_owned_records(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.project_task_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.delete_project_owned_records(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.detach_project_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.retain_async_job_project_scope()', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.lock_project_deletion(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.guard_project_deletion(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.delete_project_owned_records(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_inference_v1.clear_project_ml_backend_pool(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_inference_v1.clear_project_pipelines(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_insights_v1.clear_project_task_views(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_diagnostics_v1.detach_project_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_export_v1.clear_project_export_artifacts(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_audit_v1.record_project_deletion(uuid,uuid,uuid,text,text,bigint,bigint,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller must use only the public project deletion capability';
    END IF;

    IF has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.pending_storage_deletions(uuid,integer)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.complete_storage_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.cancel_dataset_task_creation(uuid,uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_audit_v1.valid_request_context(jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_audit_v1.record_dataset_unlink(uuid,uuid,uuid,uuid,bigint,bigint,uuid[],jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1._dataset_unlink_failure(text,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.dataset_item_references(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.dataset_scene_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.prepare_dataset_unlink_cleanup(uuid,uuid,uuid,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.remove_dataset_association(uuid,uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_diagnostics_v1.detach_dataset_task_references(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.guard_dataset_item_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.guard_dataset_unlink(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1._dataset_unlink_task_snapshot(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.dataset_task_references(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.lock_dataset_unlink_scope(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.guard_dataset_unlink(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.preview_dataset_unlink(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.delete_dataset_tasks(uuid,uuid)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller must use the Workspace dataset-unlink capability';
    END IF;

    IF has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._accept_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._finish_dataset_upload(uuid,uuid,uuid,uuid,bigint)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._dataset_upload_failure(text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._dataset_upload_success(jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._dataset_upload_actor(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._dataset_upload_facts(public.dataset_upload_intents)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._delete_dataset_source(uuid,uuid,uuid,uuid,text,jsonb)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._delete_dataset_source_records(uuid,uuid,uuid,text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._source_delete_failure(text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._lock_dataset_source_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1._queue_pyramid_deletions(uuid,uuid[],text)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.dataset_association_references(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.guard_dataset_source_deletion(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_jobs_v1.task_creation_execution_active(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_learning_v1.guard_dataset_source_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.guard_dataset_source_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_labeling_v1.detach_deleted_dataset_attribution(uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.workflow_role'),
        'aap_audit_v1.record_dataset_source_deletion(uuid,uuid,uuid,uuid,bigint,bigint,bigint,bigint,bigint,bigint,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Workflow caller must use the public Assets source/upload capabilities';
    END IF;

    IF NOT has_schema_privilege(current_setting('aap.asset_cleanup_role'), 'aap_assets_v1', 'USAGE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.complete_storage_deletion(uuid,uuid)', 'EXECUTE')
       OR has_function_privilege(current_setting('aap.asset_cleanup_role'),
        'aap_assets_v1.pending_storage_deletions(uuid,integer)', 'EXECUTE')
       OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_proc AS proc
           WHERE proc.pronamespace = 'aap_assets_v1'::pg_catalog.regnamespace
             AND pg_catalog.has_function_privilege(
                 current_setting('aap.asset_cleanup_role'), proc.oid, 'EXECUTE'
             )
             AND proc.oid NOT IN (
                 'aap_assets_v1.pending_storage_deletions_v2(text,timestamptz,uuid,uuid,integer)'::pg_catalog.regprocedure,
                 'aap_assets_v1.complete_storage_deletion(uuid,uuid)'::pg_catalog.regprocedure
             )
       ) OR EXISTS (
           SELECT 1
           FROM pg_catalog.pg_proc AS proc
           JOIN pg_catalog.pg_namespace AS namespace
             ON namespace.oid = proc.pronamespace
           WHERE namespace.nspname <> 'aap_assets_v1'
             AND pg_catalog.left(namespace.nspname, 4) = 'aap_'
             AND pg_catalog.right(namespace.nspname, 3) = '_v1'
             AND pg_catalog.has_function_privilege(
                 current_setting('aap.asset_cleanup_role'), proc.oid, 'EXECUTE'
             )
       ) THEN
        RAISE EXCEPTION 'Asset cleanup role must execute only its outbox functions';
    END IF;

    IF NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller is missing the public project deletion capability';
    END IF;
    IF NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1.preview_dataset_unlink(uuid,uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_workspace_v1.unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller is missing the public dataset-unlink capabilities';
    END IF;
    IF NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.delete_dataset(uuid,uuid,uuid,text,jsonb)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.delete_dataset_item(uuid,uuid,uuid,uuid,text,jsonb)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.reserve_dataset_upload(uuid,uuid,uuid,text,text)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_dataset_upload(uuid,uuid,uuid)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.find_dataset_upload_duplicate(uuid,uuid,uuid,text)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.complete_dataset_upload(uuid,uuid,uuid,uuid,bigint,text,integer,integer)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.discard_dataset_upload(uuid,uuid,uuid,text)', 'EXECUTE')
       OR NOT has_function_privilege(current_setting('aap.workflow_role'),
        'aap_assets_v1.lock_scene_source_references(uuid[])', 'EXECUTE') THEN
        RAISE EXCEPTION 'Caller is missing a public Assets source or upload capability';
    END IF;
END $$;
COMMIT;
