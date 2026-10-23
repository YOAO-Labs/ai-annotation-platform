CREATE TABLE aap_jobs_v1.video_deletion_clearances (
    job_id uuid PRIMARY KEY,
    never_started_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
REVOKE ALL ON TABLE aap_jobs_v1.video_deletion_clearances FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_video_execution_deletion(
    p_job_ids uuid[],
    p_task_ids uuid[],
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job_ids uuid[];
    v_blocked boolean;
BEGIN
    IF p_job_ids IS NULL OR p_task_ids IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid video deletion guard scope';
    END IF;
    IF (pg_catalog.cardinality(p_job_ids) > 0
        AND pg_catalog.array_ndims(p_job_ids) IS DISTINCT FROM 1)
       OR (pg_catalog.cardinality(p_task_ids) > 0
        AND pg_catalog.array_ndims(p_task_ids) IS DISTINCT FROM 1) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid video deletion guard scope';
    END IF;
    IF pg_catalog.array_position(p_job_ids, NULL::uuid) IS NOT NULL
       OR pg_catalog.array_position(p_task_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid video deletion guard scope';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(target.job_id ORDER BY target.job_id),
        ARRAY[]::uuid[]
    )
    INTO v_job_ids
    FROM (
        SELECT requested.job_id
        FROM pg_catalog.unnest(p_job_ids) AS requested(job_id)
        UNION
        SELECT journal.job_id
        FROM aap_jobs_v1.video_execution_journals AS journal
        WHERE NOT EXISTS (
            SELECT 1
            FROM public.video_tracker_jobs AS job
            WHERE job.id = journal.job_id
        )
          AND (
              journal.task_id = ANY (p_task_ids)
              OR (
                  p_project_id IS NOT NULL
                  AND journal.project_id = p_project_id
              )
          )
    ) AS target;

    -- Preserve the writer lock order: canonical Video Job, execution, journal.
    PERFORM job.id
    FROM public.video_tracker_jobs AS job
    WHERE job.id = ANY (v_job_ids)
    ORDER BY job.id
    FOR UPDATE OF job NOWAIT;

    PERFORM execution.job_id
    FROM aap_jobs_v1.video_executions AS execution
    WHERE execution.job_id = ANY (v_job_ids)
    ORDER BY execution.job_id
    FOR UPDATE OF execution NOWAIT;

    PERFORM journal.job_id
    FROM aap_jobs_v1.video_execution_journals AS journal
    WHERE journal.job_id = ANY (v_job_ids)
    ORDER BY journal.job_id
    FOR UPDATE OF journal NOWAIT;

    SELECT EXISTS (
        SELECT 1
        FROM pg_catalog.unnest(v_job_ids) AS target(job_id)
        LEFT JOIN public.video_tracker_jobs AS job
          ON job.id = target.job_id
        LEFT JOIN aap_jobs_v1.video_executions AS execution
          ON execution.job_id = target.job_id
        LEFT JOIN aap_jobs_v1.video_execution_journals AS journal
          ON journal.job_id = target.job_id
        LEFT JOIN aap_jobs_v1.video_deletion_clearances AS clearance
          ON clearance.job_id = target.job_id
        WHERE (
            execution.job_id IS NOT NULL
            AND (
                journal.job_id IS NULL
                OR journal.run_id IS DISTINCT FROM execution.run_id
                OR journal.execution_retired IS DISTINCT FROM true
                OR (
                    job.id IS NOT NULL
                    AND job.status NOT IN (
                        'completed', 'failed', 'cancelled', 'pending_review',
                        'partially_reviewed', 'accepted', 'discarded'
                    )
                )
            )
        )
        OR (
            execution.job_id IS NULL
            AND (
                (job.id IS NULL AND clearance.job_id IS NULL)
                OR journal.job_id IS NOT NULL
                OR (
                    job.id IS NOT NULL
                    AND (
                        job.started_at IS NOT NULL
                        OR job.status NOT IN (
                            'queued', 'cancelled', 'completed', 'failed',
                            'pending_review', 'partially_reviewed', 'accepted', 'discarded'
                        )
                    )
                )
            )
        )
    )
    INTO v_blocked;

    IF v_blocked THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-video-deletion-guard/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', 'video_execution_not_retired'
            )
        );
    END IF;

    -- Record only the Jobs whose continued absence is safe because they never
    -- started. The caller's outer transaction retains or rolls back this proof.
    INSERT INTO aap_jobs_v1.video_deletion_clearances(job_id)
    SELECT job.id
    FROM public.video_tracker_jobs AS job
    LEFT JOIN aap_jobs_v1.video_executions AS execution
      ON execution.job_id = job.id
    LEFT JOIN aap_jobs_v1.video_execution_journals AS journal
      ON journal.job_id = job.id
    WHERE job.id = ANY (v_job_ids)
      AND execution.job_id IS NULL
      AND journal.job_id IS NULL
      AND job.started_at IS NULL
      AND job.status IN (
          'queued', 'cancelled', 'completed', 'failed',
          'pending_review', 'partially_reviewed', 'accepted', 'discarded'
      )
    ON CONFLICT (job_id) DO NOTHING;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-video-deletion-guard/v1',
        'ok', true,
        'data', '{}'::jsonb
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_jobs_v1.guard_video_execution_deletion(uuid[], uuid[], uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.guard_project_video_deletion(
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task_ids uuid[];
    v_annotation_ids uuid[];
    v_job_ids uuid[];
    v_guard jsonb;
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Project video deletion guard scope';
    END IF;

    -- Project is already held FOR UPDATE by the Workspace coordinator.
    PERFORM task_batch.id
    FROM public.task_batches AS task_batch
    WHERE task_batch.project_id = p_project_id
    ORDER BY task_batch.id
    FOR UPDATE OF task_batch NOWAIT;

    PERFORM task.id
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id
    ORDER BY task.id
    FOR UPDATE OF task NOWAIT;

    SELECT COALESCE(
        pg_catalog.array_agg(task.id ORDER BY task.id),
        ARRAY[]::uuid[]
    )
    INTO v_task_ids
    FROM public.tasks AS task
    WHERE task.project_id = p_project_id;

    PERFORM annotation.id
    FROM public.annotations AS annotation
    WHERE annotation.project_id = p_project_id
       OR annotation.task_id = ANY (v_task_ids)
    ORDER BY annotation.id
    FOR UPDATE OF annotation NOWAIT;

    SELECT COALESCE(
        pg_catalog.array_agg(annotation.id ORDER BY annotation.id),
        ARRAY[]::uuid[]
    )
    INTO v_annotation_ids
    FROM public.annotations AS annotation
    WHERE annotation.project_id = p_project_id
       OR annotation.task_id = ANY (v_task_ids);

    SELECT COALESCE(
        pg_catalog.array_agg(target.job_id ORDER BY target.job_id),
        ARRAY[]::uuid[]
    )
    INTO v_job_ids
    FROM (
        SELECT job.id AS job_id
        FROM public.video_tracker_jobs AS job
        WHERE job.task_id = ANY (v_task_ids)
           OR job.annotation_id = ANY (v_annotation_ids)
        UNION
        SELECT source.tracker_job_id AS job_id
        FROM public.annotation_video_tracker_input_sources AS source
        WHERE source.annotation_id = ANY (v_annotation_ids)
        UNION
        SELECT input_scope.tracker_job_id AS job_id
        FROM public.video_tracker_input_scopes AS input_scope
        WHERE input_scope.project_id = p_project_id
          AND NOT EXISTS (
              SELECT 1
              FROM public.video_tracker_jobs AS job
              WHERE job.id = input_scope.tracker_job_id
          )
    ) AS target;

    v_guard := aap_jobs_v1.guard_video_execution_deletion(
        v_job_ids, v_task_ids, p_project_id
    );
    IF v_guard IS NULL
       OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-video-deletion-guard/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'video deletion guard contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
           OR v_guard -> 'error' IS DISTINCT FROM
                pg_catalog.jsonb_build_object(
                    'code', 'video_execution_not_retired'
                )
           OR (v_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'video deletion guard contract mismatch';
        END IF;
        RETURN v_guard;
    END IF;
    IF v_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
       OR (v_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'video deletion guard contract mismatch';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-video-deletion-guard/v1',
        'ok', true,
        'data', '{}'::jsonb
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.guard_project_video_deletion(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.guard_dataset_video_deletion(
    p_dataset_id uuid,
    p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task_ids uuid[];
    v_annotation_ids uuid[];
    v_job_ids uuid[];
    v_guard jsonb;
BEGIN
    IF p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid Dataset video deletion guard scope';
    END IF;

    -- Workspace has already locked Dataset/Project/association, and unlink
    -- has already run lock_dataset_unlink_scope before its retention guards.
    SELECT COALESCE(
        pg_catalog.array_agg(snapshot.task_id ORDER BY snapshot.task_id),
        ARRAY[]::uuid[]
    )
    INTO v_task_ids
    FROM aap_labeling_v1._dataset_unlink_task_snapshot(
        p_dataset_id, p_project_id
    ) AS snapshot;

    PERFORM annotation.id
    FROM public.annotations AS annotation
    WHERE annotation.task_id = ANY (v_task_ids)
    ORDER BY annotation.id
    FOR UPDATE OF annotation NOWAIT;

    SELECT COALESCE(
        pg_catalog.array_agg(annotation.id ORDER BY annotation.id),
        ARRAY[]::uuid[]
    )
    INTO v_annotation_ids
    FROM public.annotations AS annotation
    WHERE annotation.task_id = ANY (v_task_ids);

    SELECT COALESCE(
        pg_catalog.array_agg(target.job_id ORDER BY target.job_id),
        ARRAY[]::uuid[]
    )
    INTO v_job_ids
    FROM (
        SELECT job.id AS job_id
        FROM public.video_tracker_jobs AS job
        WHERE job.task_id = ANY (v_task_ids)
           OR job.annotation_id = ANY (v_annotation_ids)
        UNION
        SELECT source.tracker_job_id AS job_id
        FROM public.annotation_video_tracker_input_sources AS source
        WHERE source.annotation_id = ANY (v_annotation_ids)
        UNION
        SELECT input_scope.tracker_job_id AS job_id
        FROM public.video_tracker_input_scopes AS input_scope
        WHERE input_scope.project_id = p_project_id
          AND (
              input_scope.task_id = ANY (v_task_ids)
              OR input_scope.scope #>> '{task,dataset_item_id}' IN (
                  SELECT item_reference.item_id::text
                  FROM aap_assets_v1.dataset_item_references(
                      p_dataset_id, NULL::uuid
                  ) AS item_reference
              )
          )
          AND NOT EXISTS (
              SELECT 1
              FROM public.video_tracker_jobs AS job
              WHERE job.id = input_scope.tracker_job_id
          )
    ) AS target;

    v_guard := aap_jobs_v1.guard_video_execution_deletion(
        v_job_ids, v_task_ids, NULL::uuid
    );
    IF v_guard IS NULL
       OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-video-deletion-guard/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'video deletion guard contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
           OR v_guard -> 'error' IS DISTINCT FROM
                pg_catalog.jsonb_build_object(
                    'code', 'video_execution_not_retired'
                )
           OR (v_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'video deletion guard contract mismatch';
        END IF;
        RETURN v_guard;
    END IF;
    IF v_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
       OR (v_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'video deletion guard contract mismatch';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-video-deletion-guard/v1',
        'ok', true,
        'data', '{}'::jsonb
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.guard_dataset_video_deletion(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_workspace_v1.delete_project(
    p_operation_id uuid, p_actor_id uuid, p_project_id uuid, p_audit_context jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_manager jsonb;
    v_guard jsonb;
    v_video_guard jsonb;
    v_labeling jsonb;
    v_request jsonb;
    v_saved public.project_deletion_operations%ROWTYPE;
    v_result jsonb;
    v_project_id uuid;
    v_display_id text;
    v_name text;
    v_tasks bigint;
    v_annotations bigint;
    v_audit_id bigint;
    v_ip pg_catalog.inet;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN aap_workspace_v1._project_delete_failure('invalid_request');
    END IF;
    IF p_audit_context IS NOT NULL THEN
        IF pg_catalog.jsonb_typeof(p_audit_context) IS DISTINCT FROM 'object' THEN
            RETURN aap_workspace_v1._project_delete_failure('invalid_request');
        END IF;
        IF (p_audit_context - ARRAY['method', 'path', 'ip', 'request_id']) <> '{}'::jsonb
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_each(p_audit_context) AS field(key, value)
               WHERE pg_catalog.jsonb_typeof(field.value) NOT IN ('string', 'null')
           )
           OR pg_catalog.char_length(p_audit_context ->> 'method') > 8
           OR pg_catalog.char_length(p_audit_context ->> 'path') > 256
           OR pg_catalog.char_length(p_audit_context ->> 'request_id') > 36
           OR pg_catalog.char_length(p_audit_context ->> 'ip') > 64 THEN
            RETURN aap_workspace_v1._project_delete_failure('invalid_request');
        END IF;
        BEGIN
            v_ip := NULLIF(p_audit_context ->> 'ip', '')::pg_catalog.inet;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_workspace_v1._project_delete_failure('invalid_request');
        END;
    END IF;

    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project deletion identity contract mismatch';
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete/v1', 'ok', false, 'error', v_actor -> 'error'
        );
    END IF;
    -- Serialize the idempotency key even when a conflicting request names another project.
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-project-delete:' || p_operation_id::text, 0)
    );
    PERFORM aap_learning_v1.lock_project_deletion(p_project_id);
    v_request := pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', p_actor_id, 'project_id', p_project_id
    );
    SELECT receipt.* INTO v_saved
    FROM public.project_deletion_operations AS receipt
    WHERE receipt.operation_id = p_operation_id FOR UPDATE;
    IF FOUND THEN
        IF v_saved.request IS DISTINCT FROM v_request THEN
            RETURN aap_workspace_v1._project_delete_failure('operation_conflict');
        END IF;
        RETURN v_saved.result;
    END IF;

    v_manager := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_manager ->> 'schema' IS DISTINCT FROM 'aap-project-manager/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project deletion authority contract mismatch';
    END IF;
    IF v_manager -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete/v1', 'ok', false, 'error', v_manager -> 'error'
        );
    END IF;
    SELECT project.id, project.display_id, project.name
    INTO v_project_id, v_display_id, v_name FROM public.projects AS project
    WHERE project.id = p_project_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_workspace_v1._project_delete_failure('project_missing');
    END IF;
    v_guard := aap_learning_v1.guard_project_deletion(p_project_id);
    IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-project-delete-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project deletion retention contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete/v1', 'ok', false, 'error', v_guard -> 'error'
        );
    END IF;

    v_video_guard := aap_labeling_v1.guard_project_video_deletion(p_project_id);
    IF v_video_guard IS NULL
       OR v_video_guard ->> 'schema' IS DISTINCT FROM 'aap-video-deletion-guard/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project video deletion guard contract mismatch';
    END IF;
    IF v_video_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_video_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
           OR v_video_guard -> 'error' IS DISTINCT FROM
                pg_catalog.jsonb_build_object('code', 'video_execution_not_retired')
           OR (v_video_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project video deletion guard contract mismatch';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete/v1', 'ok', false,
            'error', v_video_guard -> 'error'
        );
    END IF;
    IF v_video_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
       OR (v_video_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project video deletion guard contract mismatch';
    END IF;

    -- Owner helpers do not acquire Dataset parents after this Project lock.
    PERFORM aap_diagnostics_v1.detach_project_references(p_project_id);
    PERFORM aap_assets_v1.delete_project_owned_records(p_project_id);
    PERFORM aap_inference_v1.clear_project_ml_backend_pool(p_project_id);
    PERFORM aap_inference_v1.clear_project_pipelines(p_project_id);
    PERFORM aap_insights_v1.clear_project_task_views(p_project_id);
    PERFORM aap_export_v1.clear_project_export_artifacts(p_project_id);
    PERFORM aap_jobs_v1.detach_project_references(p_project_id);
    PERFORM aap_learning_v1.delete_project_owned_records(p_project_id);
    v_labeling := aap_labeling_v1.delete_project_owned_records(p_project_id);
    IF v_labeling ->> 'schema' IS DISTINCT FROM 'aap-project-labeling-delete/v1'
       OR v_labeling -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_labeling #> '{data,deleted_tasks}') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_labeling #> '{data,deleted_annotations}') IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project deletion labeling contract mismatch';
    END IF;
    v_tasks := (v_labeling #>> '{data,deleted_tasks}')::bigint;
    v_annotations := (v_labeling #>> '{data,deleted_annotations}')::bigint;
    IF v_tasks < 0 OR v_annotations < 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid project deletion counts';
    END IF;
    DELETE FROM public.project_members WHERE project_id = p_project_id;
    UPDATE public.project_templates SET source_project_id = NULL WHERE source_project_id = p_project_id;
    DELETE FROM public.projects WHERE id = p_project_id;
    v_audit_id := aap_audit_v1.record_project_deletion(
        p_operation_id, p_actor_id, p_project_id, v_display_id, v_name,
        v_tasks, v_annotations, p_audit_context
    );
    v_result := pg_catalog.jsonb_build_object(
        'schema', 'aap-project-delete/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id, 'actor_id', p_actor_id,
            'project_id', p_project_id, 'deleted_tasks', v_tasks,
            'deleted_annotations', v_annotations, 'audit_id', v_audit_id
        )
    );
    INSERT INTO public.project_deletion_operations(operation_id, actor_id, project_id, request, result)
    VALUES (p_operation_id, p_actor_id, p_project_id, v_request, v_result);
    RETURN v_result;
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        -- The exception block rolls back every owner mutation before this reply.
        RETURN aap_workspace_v1._project_delete_failure('project_busy');
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_workspace_v1.unlink_dataset(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid,
    p_project_id uuid, p_cleanup_bucket text, p_audit_context jsonb DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_schema constant text := 'aap-dataset-unlink/v1';
    v_actor jsonb;
    v_manager jsonb;
    v_request jsonb;
    v_saved public.dataset_unlink_operations%ROWTYPE;
    v_dataset_id uuid;
    v_project_id uuid;
    v_association_id uuid;
    v_guard jsonb;
    v_video_guard jsonb;
    v_deleted jsonb;
    v_data jsonb;
    v_tasks bigint;
    v_annotations bigint;
    v_batches bigint;
    v_batch_ids uuid[];
    v_audit_id bigint;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_project_id IS NULL OR p_cleanup_bucket IS NULL
       OR pg_catalog.char_length(p_cleanup_bucket) NOT BETWEEN 3 AND 63
       OR p_cleanup_bucket !~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
       OR p_cleanup_bucket ~ '^[0-9]+(\.[0-9]+){3}$'
       OR aap_audit_v1.valid_request_context(p_audit_context) IS DISTINCT FROM true THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'invalid_request');
    END IF;
    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink identity contract mismatch';
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false, 'error', v_actor -> 'error'
        );
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-dataset-unlink:' || p_operation_id::text, 0)
    );
    v_request := pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', p_actor_id,
        'dataset_id', p_dataset_id, 'project_id', p_project_id,
        'cleanup_bucket', p_cleanup_bucket
    );
    SELECT receipt.* INTO v_saved
    FROM public.dataset_unlink_operations AS receipt
    WHERE receipt.operation_id = p_operation_id FOR UPDATE;
    IF FOUND THEN
        IF v_saved.request IS DISTINCT FROM v_request THEN
            RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'operation_conflict');
        END IF;
        RETURN v_saved.result;
    END IF;

    -- Keep the existing append/create order: Dataset precedes Project/association.
    v_dataset_id := aap_assets_v1.lock_dataset_reference(p_dataset_id);
    IF v_dataset_id IS NULL THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'dataset_missing');
    END IF;
    v_manager := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_manager ->> 'schema' IS DISTINCT FROM 'aap-project-manager/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink authority contract mismatch';
    END IF;
    IF v_manager -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false, 'error', v_manager -> 'error'
        );
    END IF;
    SELECT project.id INTO v_project_id FROM public.projects AS project
    WHERE project.id = p_project_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'project_missing');
    END IF;
    v_association_id := aap_assets_v1.lock_current_association(p_dataset_id, p_project_id);
    IF v_association_id IS NULL THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'link_missing');
    END IF;
    PERFORM aap_labeling_v1.lock_dataset_unlink_scope(p_dataset_id, p_project_id);
    v_guard := aap_learning_v1.guard_dataset_unlink(p_dataset_id, p_project_id);
    IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink learning contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false, 'error', v_guard -> 'error'
        );
    END IF;
    v_guard := aap_labeling_v1.guard_dataset_unlink(p_dataset_id, p_project_id);
    IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink labeling guard contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false, 'error', v_guard -> 'error'
        );
    END IF;

    v_video_guard := aap_labeling_v1.guard_dataset_video_deletion(
        p_dataset_id, p_project_id
    );
    IF v_video_guard IS NULL
       OR v_video_guard ->> 'schema' IS DISTINCT FROM 'aap-video-deletion-guard/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink video deletion guard contract mismatch';
    END IF;
    IF v_video_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_video_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
           OR v_video_guard -> 'error' IS DISTINCT FROM
                pg_catalog.jsonb_build_object('code', 'video_execution_not_retired')
           OR (v_video_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink video deletion guard contract mismatch';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false, 'error', v_video_guard -> 'error'
        );
    END IF;
    IF v_video_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
       OR (v_video_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink video deletion guard contract mismatch';
    END IF;

    PERFORM aap_diagnostics_v1.detach_dataset_task_references(p_dataset_id, p_project_id);
    PERFORM aap_assets_v1.prepare_dataset_unlink_cleanup(
        p_operation_id, p_dataset_id, p_project_id, p_cleanup_bucket
    );
    PERFORM aap_jobs_v1.cancel_dataset_task_creation(
        p_dataset_id, p_project_id, v_association_id
    );
    v_deleted := aap_labeling_v1.delete_dataset_tasks(p_dataset_id, p_project_id);
    v_data := v_deleted -> 'data';
    IF v_deleted ->> 'schema' IS DISTINCT FROM 'aap-dataset-task-unlink/v1'
       OR v_deleted -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY['deleted_tasks', 'deleted_annotations', 'deleted_batches', 'deleted_batch_ids', 'soft']) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(v_data -> 'deleted_tasks') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_data -> 'deleted_annotations') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_data -> 'deleted_batches') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_data -> 'deleted_batch_ids') IS DISTINCT FROM 'array'
       OR v_data -> 'soft' IS DISTINCT FROM 'false'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink deletion contract mismatch';
    END IF;
    v_tasks := (v_data ->> 'deleted_tasks')::bigint;
    v_annotations := (v_data ->> 'deleted_annotations')::bigint;
    v_batches := (v_data ->> 'deleted_batches')::bigint;
    v_batch_ids := ARRAY(
        SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(v_data -> 'deleted_batch_ids') AS ids(value)
    );
    IF v_tasks < 0 OR v_annotations < 0 OR v_batches < 0
       OR v_batches <> pg_catalog.cardinality(v_batch_ids)
       OR v_batches <> (SELECT pg_catalog.count(DISTINCT value) FROM pg_catalog.unnest(v_batch_ids) AS ids(value)) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid unlink deletion counts';
    END IF;
    IF aap_assets_v1.remove_dataset_association(
        p_dataset_id, p_project_id, v_association_id
    ) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'unlink association changed';
    END IF;
    v_audit_id := aap_audit_v1.record_dataset_unlink(
        p_operation_id, p_actor_id, p_dataset_id, p_project_id,
        v_tasks, v_annotations, v_batch_ids, p_audit_context
    );
    IF v_audit_id IS NULL OR v_audit_id <= 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid unlink audit receipt';
    END IF;
    v_result := pg_catalog.jsonb_build_object(
        'schema', v_schema, 'ok', true,
        'data', v_data || pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id, 'actor_id', p_actor_id,
            'dataset_id', p_dataset_id, 'project_id', p_project_id,
            'association_id', v_association_id, 'cleanup_bucket', p_cleanup_bucket,
            'audit_id', v_audit_id
        )
    );
    INSERT INTO public.dataset_unlink_operations(operation_id, actor_id, dataset_id, project_id, request, result)
    VALUES (p_operation_id, p_actor_id, p_dataset_id, p_project_id, v_request, v_result);
    RETURN v_result;
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'project_busy');
    WHEN foreign_key_violation THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'source_in_use');
END;
$function$;
