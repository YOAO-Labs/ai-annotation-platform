-- Jobs projects the durable bulk Job without reading the Assets manifest tables.
CREATE FUNCTION aap_jobs_v1._accepted_image_input_manifest_reply(
    p_data jsonb, p_code text
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-accepted-image-input-manifest/v1',
        'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-jobs-accepted-image-input-manifest/v1',
        'ok', false, 'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._accepted_image_input_manifest_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.project_accepted_image_input_manifest_job(
    p_job_id uuid,
    p_task_id uuid,
    p_actor_id uuid,
    p_project_id uuid,
    p_selection jsonb,
    p_selection_digest text,
    p_require_active boolean
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_selection jsonb;
    v_digest jsonb;
    v_task_ids jsonb;
    v_scope jsonb;
    v_cancel jsonb;
    v_cancel_requested boolean := false;
    v_active boolean;
    v_count integer;
    v_distinct integer;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL
       OR p_project_id IS NULL OR p_selection IS NULL
       OR (p_selection_digest IS NOT NULL
           AND p_selection_digest !~ '^[0-9a-f]{64}$')
       OR p_require_active IS NULL THEN
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.async_jobs AS job
        WHERE job.id = p_job_id
        FOR SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'job_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'job_not_active');
    END IF;
    IF v_job.kind IS DISTINCT FROM 'batch_predict'
       OR (v_job.user_id IS DISTINCT FROM p_actor_id
           AND (p_require_active OR v_job.user_id IS NOT NULL))
       OR (v_job.project_id IS DISTINCT FROM p_project_id
           AND (p_require_active OR v_job.project_id IS NOT NULL
                OR v_job.retained_project_id IS DISTINCT FROM p_project_id))
       OR (v_job.retained_project_id IS NOT NULL
           AND v_job.retained_project_id IS DISTINCT FROM p_project_id)
       OR v_job.celery_task_id IS DISTINCT FROM v_job.id::text
       OR pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object'
       OR v_job.payload ->> 'execution_scope' IS DISTINCT FROM 'bulk'
       OR v_job.payload ->> 'accepted_input_manifest_protocol'
            IS DISTINCT FROM 'aap-assets-input-manifest/v1'
       OR v_job.payload ->> 'accepted_input_manifest_id' IS DISTINCT FROM p_job_id::text THEN
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    v_selection := v_job.payload -> 'input_selection';
    IF pg_catalog.jsonb_typeof(v_selection) IS DISTINCT FROM 'object'
       OR (v_selection - ARRAY['batch_id', 'task_ids', 'predict_mode'])
            IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_selection ?& ARRAY['batch_id', 'task_ids', 'predict_mode'])
       OR pg_catalog.jsonb_typeof(v_selection -> 'batch_id') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(v_selection -> 'task_ids') NOT IN ('array', 'null')
       OR pg_catalog.jsonb_typeof(v_selection -> 'predict_mode') IS DISTINCT FROM 'string'
       OR v_selection ->> 'predict_mode' NOT IN ('skip_predicted', 'overwrite', 'append')
       OR v_selection IS DISTINCT FROM p_selection
       OR NOT (v_job.payload ? 'batch_id')
       OR pg_catalog.jsonb_typeof(v_job.payload -> 'batch_id') NOT IN ('string', 'null')
       OR v_job.payload -> 'batch_id' IS DISTINCT FROM v_selection -> 'batch_id' THEN
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;

    v_task_ids := v_selection -> 'task_ids';
    IF v_task_ids = 'null'::jsonb THEN
        IF v_job.payload ? 'task_ids' OR v_job.payload ? 'scope' THEN
            RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
        END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(v_job.payload -> 'task_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(v_job.payload -> 'scope') IS DISTINCT FROM 'object'
           OR ((v_job.payload -> 'scope') - 'task_ids') IS DISTINCT FROM '{}'::jsonb
           OR pg_catalog.jsonb_typeof(v_job.payload #> '{scope,task_ids}') IS DISTINCT FROM 'array' THEN
            RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
        END IF;
        SELECT pg_catalog.count(*)::integer,
               pg_catalog.count(DISTINCT (entry.value #>> '{}')::uuid)::integer
        INTO v_count, v_distinct
        FROM pg_catalog.jsonb_array_elements(v_job.payload -> 'task_ids') AS entry(value)
        WHERE pg_catalog.jsonb_typeof(entry.value) = 'string'
          AND entry.value #>> '{}' ~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
        IF v_count <> pg_catalog.jsonb_array_length(v_job.payload -> 'task_ids')
           OR v_distinct <> v_count
           OR v_job.payload -> 'scope' -> 'task_ids'
                IS DISTINCT FROM v_job.payload -> 'task_ids'
           OR (SELECT pg_catalog.array_agg((entry.value #>> '{}')::uuid ORDER BY (entry.value #>> '{}')::uuid)
               FROM pg_catalog.jsonb_array_elements(v_job.payload -> 'task_ids') AS entry(value))
                IS DISTINCT FROM
              (SELECT pg_catalog.array_agg((entry.value #>> '{}')::uuid ORDER BY (entry.value #>> '{}')::uuid)
               FROM pg_catalog.jsonb_array_elements(v_task_ids) AS entry(value))
           OR (p_task_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(v_task_ids) AS entry(value)
                WHERE entry.value #>> '{}' = p_task_id::text
           )) THEN
            RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
        END IF;
    END IF;

    v_digest := v_job.payload -> 'accepted_input_manifest_digest';
    IF v_digest IS NOT NULL
       AND (pg_catalog.jsonb_typeof(v_digest) IS DISTINCT FROM 'string'
            OR v_digest #>> '{}' !~ '^[0-9a-f]{64}$'
            OR (p_selection_digest IS NOT NULL
                AND v_digest #>> '{}' IS DISTINCT FROM p_selection_digest)) THEN
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    v_cancel := v_job.payload -> 'cancel_requested';
    IF v_cancel IS NULL THEN
        v_cancel_requested := false;
    ELSIF pg_catalog.jsonb_typeof(v_cancel) = 'boolean' THEN
        v_cancel_requested := v_cancel = 'true'::jsonb;
    ELSE
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    IF v_job.status NOT IN ('pending', 'running', 'completed', 'failed', 'cancelled') THEN
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    -- Historical reads rely on the immutable Assets header/member after FK
    -- nulling; only the complete live Job identity may grant execution.
    v_active := COALESCE(
        v_job.user_id = p_actor_id AND v_job.project_id = p_project_id
        AND v_job.status IN ('pending', 'running') AND NOT v_cancel_requested,
        false
    );
    IF p_require_active AND NOT v_active THEN
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'job_not_active');
    END IF;
    RETURN aap_jobs_v1._accepted_image_input_manifest_reply(
        pg_catalog.jsonb_build_object(
            'job_id', v_job.id,
            'task_id', p_task_id,
            'actor_id', v_job.user_id,
            'project_id', v_job.project_id,
            'status', v_job.status,
            'active', v_active,
            'cancel_requested', v_cancel_requested
        ), NULL
    );
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
WHEN OTHERS THEN
    RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.project_accepted_image_input_manifest_job(
    uuid, uuid, uuid, uuid, jsonb, text, boolean
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.accepted_image_input_manifest_job_consistency(
    p_job_id uuid,
    p_task_id uuid,
    p_actor_id uuid,
    p_project_id uuid,
    p_selection jsonb,
    p_selection_digest text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_reply jsonb;
    v_data jsonb;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL
       OR p_project_id IS NULL OR p_selection IS NULL
       OR p_selection_digest IS NULL OR p_selection_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_job
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id;
    IF NOT FOUND THEN
        -- Assets verifies the immutable local header/member before relying on
        -- this frozen grant after normal AsyncJob history pruning.
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(
            pg_catalog.jsonb_build_object(
                'job_id', p_job_id, 'task_id', p_task_id, 'actor_id', p_actor_id,
                'project_id', p_project_id, 'exists', false,
                'consistent', true, 'active', false
            ), NULL
        );
    END IF;
    v_reply := aap_jobs_v1.project_accepted_image_input_manifest_job(
        p_job_id, p_task_id, p_actor_id, p_project_id,
        p_selection, p_selection_digest, false
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-jobs-accepted-image-input-manifest/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_jobs_v1._accepted_image_input_manifest_reply(
            pg_catalog.jsonb_build_object(
                'job_id', p_job_id, 'task_id', p_task_id, 'actor_id', p_actor_id,
                'project_id', p_project_id, 'exists', true,
                'consistent', false, 'active', false
            ), NULL
        );
    END IF;
    v_data := v_reply -> 'data';
    RETURN aap_jobs_v1._accepted_image_input_manifest_reply(
        pg_catalog.jsonb_build_object(
            'job_id', p_job_id, 'task_id', p_task_id, 'actor_id', p_actor_id,
            'project_id', p_project_id, 'exists', true,
            'consistent', true, 'active', v_data -> 'active'
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.accepted_image_input_manifest_job_consistency(
    uuid, uuid, uuid, uuid, jsonb, text
) FROM PUBLIC;
-- aap-statement-break

-- Labeling streams eligible membership through one portal. The cursor's first
-- row is a bounded header; later rows are canonical selected Tasks.
CREATE FUNCTION aap_labeling_v1._accepted_image_input_manifest_capture_reply(
    p_data jsonb, p_code text
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-accepted-image-input-manifest/v1',
        'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-accepted-image-input-manifest/v1',
        'ok', false, 'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._accepted_image_input_manifest_capture_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.open_accepted_image_input_manifest_members(
    p_actor_id uuid, p_project_id uuid, p_selection jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_access_code text;
    v_batch_id uuid;
    v_task_ids uuid[];
    v_has_explicit boolean;
    v_predict_mode text;
    v_cursor refcursor;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL
       OR pg_catalog.jsonb_typeof(p_selection) IS DISTINCT FROM 'object'
       OR (p_selection - ARRAY['batch_id', 'task_ids', 'predict_mode'])
            IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_selection ?& ARRAY['batch_id', 'task_ids', 'predict_mode'])
       OR pg_catalog.jsonb_typeof(p_selection -> 'batch_id') NOT IN ('string', 'null')
       OR pg_catalog.jsonb_typeof(p_selection -> 'task_ids') NOT IN ('array', 'null')
       OR pg_catalog.jsonb_typeof(p_selection -> 'predict_mode') IS DISTINCT FROM 'string'
       OR p_selection ->> 'predict_mode' NOT IN ('skip_predicted', 'overwrite', 'append') THEN
        RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        IF p_selection -> 'batch_id' <> 'null'::jsonb THEN
            v_batch_id := (p_selection ->> 'batch_id')::uuid;
        END IF;
        v_has_explicit := p_selection -> 'task_ids' <> 'null'::jsonb;
        IF v_has_explicit THEN
            IF pg_catalog.jsonb_array_length(p_selection -> 'task_ids') NOT BETWEEN 1 AND 200 THEN
                RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'invalid_request');
            END IF;
            SELECT COALESCE(pg_catalog.array_agg(
                (entry.value #>> '{}')::uuid ORDER BY (entry.value #>> '{}')::uuid
            ), ARRAY[]::uuid[])
            INTO v_task_ids
            FROM pg_catalog.jsonb_array_elements(p_selection -> 'task_ids') AS entry(value)
            WHERE pg_catalog.jsonb_typeof(entry.value) = 'string'
              AND entry.value #>> '{}' ~
                  '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
            IF pg_catalog.cardinality(v_task_ids) <> pg_catalog.jsonb_array_length(p_selection -> 'task_ids')
               OR pg_catalog.cardinality(v_task_ids) <> (
                   SELECT pg_catalog.count(DISTINCT value)::integer
                   FROM pg_catalog.unnest(v_task_ids) AS requested(value)
               )
               OR v_task_ids IS DISTINCT FROM (
                   SELECT COALESCE(pg_catalog.array_agg(value ORDER BY value), ARRAY[]::uuid[])
                   FROM pg_catalog.unnest(v_task_ids) AS requested(value)
               ) THEN
                RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'invalid_request');
            END IF;
        END IF;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'invalid_request');
    END;
    v_predict_mode := p_selection ->> 'predict_mode';

    v_access := aap_workspace_v1.read_media_project_access(p_actor_id, p_project_id);
    v_access_code := aap_labeling_v1._image_workspace_failure(v_access);
    IF v_access_code IS NOT NULL THEN
        RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, v_access_code);
    END IF;

    v_cursor := 'aap_manifest_' || pg_catalog.replace(pg_catalog.gen_random_uuid()::text, '-', '');
    OPEN v_cursor FOR
        WITH eligible AS MATERIALIZED (
            SELECT task.id, task.project_id, task.dataset_item_id, task.batch_id,
                   task.file_type, task.file_path, task.created_at, task.version,
                   task.media_source_generation, task.total_predictions,
                   aap_labeling_v1._image_source_snapshot_fields(
                       task.id, task.created_at, task.project_id,
                       task.dataset_item_id, task.file_type, task.file_path,
                       task.media_source_generation
                   ) AS task_source_snapshot
            FROM public.tasks AS task
            LEFT JOIN public.task_batches AS batch ON batch.id = task.batch_id
            WHERE task.project_id = p_project_id
              AND task.status = 'pending'
              AND (NOT v_has_explicit OR task.id = ANY(v_task_ids))
              AND (v_batch_id IS NULL OR task.batch_id = v_batch_id)
              AND (
                  task.batch_id IS NULL
                  OR (batch.id IS NOT NULL AND batch.project_id = p_project_id
                      AND NOT batch.admin_locked
                      AND (batch.status = 'active'
                           OR (batch.status = 'draft'
                               AND batch.annotator_id IS NULL
                               AND batch.reviewer_id IS NULL)))
              )
              AND NOT EXISTS (
                  SELECT 1 FROM public.task_locks AS task_lock
                  WHERE task_lock.task_id = task.id
                    AND task_lock.expire_at > pg_catalog.clock_timestamp()
              )
        ), counts AS MATERIALIZED (
            SELECT pg_catalog.count(*)::bigint AS candidate_count,
                   pg_catalog.count(*) FILTER (
                       WHERE v_predict_mode <> 'skip_predicted'
                          OR COALESCE(eligible.total_predictions, 0) = 0
                   )::bigint AS selected_count,
                   COALESCE(pg_catalog.bool_or(eligible.file_type IS DISTINCT FROM 'image'), false)
                       AS unsupported_media,
                   CASE WHEN v_batch_id IS NULL THEN true ELSE EXISTS (
                       SELECT 1 FROM public.task_batches AS batch
                       WHERE batch.id = v_batch_id
                         AND batch.project_id = p_project_id
                         AND NOT batch.admin_locked
                         AND (batch.status = 'active'
                              OR (batch.status = 'draft'
                                  AND batch.annotator_id IS NULL
                                  AND batch.reviewer_id IS NULL))
                   ) END AS batch_valid,
                   v_has_explicit AS has_explicit,
                   pg_catalog.cardinality(v_task_ids)::bigint AS requested_count
            FROM eligible
        ), selected AS MATERIALIZED (
            SELECT eligible.*,
                   pg_catalog.row_number() OVER (ORDER BY eligible.id)::bigint AS ordinal
            FROM eligible
            WHERE v_predict_mode <> 'skip_predicted'
               OR COALESCE(eligible.total_predictions, 0) = 0
        ), rows AS (
            SELECT 0 AS row_kind, 0::bigint AS ordinal,
                   CASE
                       WHEN NOT counts.batch_valid THEN pg_catalog.jsonb_build_object(
                           'kind', 'error', 'code', 'task_scope_changed')
                       WHEN counts.unsupported_media THEN pg_catalog.jsonb_build_object(
                           'kind', 'error', 'code', 'unsupported_media')
                       WHEN counts.has_explicit AND counts.candidate_count <> counts.requested_count
                           THEN pg_catalog.jsonb_build_object(
                               'kind', 'error', 'code', 'task_scope_changed')
                       ELSE pg_catalog.jsonb_build_object(
                           'kind', 'header',
                           'candidate_count', counts.candidate_count,
                           'selected_count', counts.selected_count
                       )
                   END AS row_data
            FROM counts
            UNION ALL
            SELECT 1, selected.ordinal,
                   pg_catalog.jsonb_build_object(
                       'kind', 'member',
                       'ordinal', selected.ordinal,
                       'task_id', selected.id,
                       'project_id', selected.project_id,
                       'batch_id', selected.batch_id,
                       'task_version', selected.version,
                       'task_source_generation', selected.media_source_generation,
                       'dataset_item_id', selected.dataset_item_id,
                       'task_source_snapshot', selected.task_source_snapshot,
                       'file_path', selected.file_path,
                       'file_type', selected.file_type
                   )
            FROM selected
            CROSS JOIN counts
            WHERE counts.batch_valid
              AND NOT counts.unsupported_media
              AND (NOT counts.has_explicit OR counts.candidate_count = counts.requested_count)
        )
        SELECT rows.row_data FROM rows ORDER BY rows.row_kind, rows.ordinal;
    RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(
        pg_catalog.jsonb_build_object('cursor', v_cursor::text), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'source_busy');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.open_accepted_image_input_manifest_members(uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break

-- Revalidate current pending/editability scope after the external HEAD, then
-- keep Task and eligible batch rows locked while Assets binds the observed bytes.
CREATE FUNCTION aap_labeling_v1.lock_accepted_image_input_manifest_member(
    p_actor_id uuid,
    p_project_id uuid,
    p_task_id uuid,
    p_captured_batch_id uuid,
    p_selected_batch_id uuid,
    p_predict_mode text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_code text;
    v_task public.tasks%ROWTYPE;
    v_batch public.task_batches%ROWTYPE;
    v_has_batch boolean := false;
    v_snapshot text;
BEGIN
    IF p_actor_id IS NULL OR p_project_id IS NULL OR p_task_id IS NULL
       OR p_predict_mode NOT IN ('skip_predicted', 'overwrite', 'append') THEN
        RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'invalid_request');
    END IF;
    v_access := aap_workspace_v1.lock_media_project_access(p_actor_id, p_project_id);
    v_code := aap_labeling_v1._image_workspace_failure(v_access);
    IF v_code IS NOT NULL THEN
        RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, v_code);
    END IF;
    BEGIN
        SELECT task.id, task.project_id, task.dataset_item_id, task.batch_id,
               task.status, task.file_type, task.file_path, task.created_at,
               task.version, task.media_source_generation, task.total_predictions
        INTO v_task.id, v_task.project_id, v_task.dataset_item_id, v_task.batch_id,
             v_task.status, v_task.file_type, v_task.file_path, v_task.created_at,
             v_task.version, v_task.media_source_generation, v_task.total_predictions
        FROM public.tasks AS task
        WHERE task.id = p_task_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'source_missing');
    END IF;
    IF v_task.project_id IS DISTINCT FROM p_project_id
       OR v_task.status IS DISTINCT FROM 'pending'
       OR v_task.batch_id IS DISTINCT FROM p_captured_batch_id
       OR (p_selected_batch_id IS NOT NULL AND v_task.batch_id IS DISTINCT FROM p_selected_batch_id)
       OR v_task.file_type IS DISTINCT FROM 'image' THEN
        RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'task_scope_changed');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.task_locks AS task_lock
        WHERE task_lock.task_id = p_task_id
          AND task_lock.expire_at > pg_catalog.clock_timestamp()
    ) THEN
        RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'task_scope_changed');
    END IF;
    IF p_predict_mode = 'skip_predicted'
       AND COALESCE(v_task.total_predictions, 0) <> 0 THEN
        RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'task_scope_changed');
    END IF;
    IF v_task.batch_id IS NOT NULL THEN
        BEGIN
            SELECT batch.id, batch.project_id, batch.status, batch.admin_locked,
                   batch.annotator_id, batch.reviewer_id
            INTO v_batch.id, v_batch.project_id, v_batch.status, v_batch.admin_locked,
                 v_batch.annotator_id, v_batch.reviewer_id
            FROM public.task_batches AS batch
            WHERE batch.id = v_task.batch_id
            FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'source_busy');
        END;
        IF NOT FOUND OR v_batch.project_id IS DISTINCT FROM p_project_id
           OR v_batch.admin_locked
           OR NOT (v_batch.status = 'active'
                   OR (v_batch.status = 'draft'
                       AND v_batch.annotator_id IS NULL
                       AND v_batch.reviewer_id IS NULL)) THEN
            RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'task_scope_changed');
        END IF;
    END IF;
    v_snapshot := aap_labeling_v1._image_source_snapshot(v_task);
    RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(
        pg_catalog.jsonb_build_object(
            'task_id', v_task.id,
            'project_id', v_task.project_id,
            'batch_id', v_task.batch_id,
            'task_version', v_task.version,
            'task_source_generation', v_task.media_source_generation,
            'dataset_item_id', v_task.dataset_item_id,
            'task_source_snapshot', v_snapshot,
            'file_path', v_task.file_path,
            'file_type', v_task.file_type
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._accepted_image_input_manifest_capture_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_accepted_image_input_manifest_member(
    uuid, uuid, uuid, uuid, uuid, text
) FROM PUBLIC;
