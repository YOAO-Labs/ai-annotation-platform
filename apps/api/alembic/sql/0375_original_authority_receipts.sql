-- Narrow owner facts for the original/reference authority chain.
--
-- Both functions expose only the facts a consumer already had to re-check, never
-- credential material or private core rows. The caller keeps its own transaction,
-- lock order and receipts; neither function commits, retries or interprets domain
-- state.

-- Accepted API-key facts: the exact non-secret revision (digest of the stored
-- verifier), the current stored scopes and the current stored expiry. A rotation,
-- scope edit or expiry extension changes the returned facts, so a persisted
-- receipt can bind them exactly without ever exposing api_keys.key_hash.
CREATE FUNCTION aap_identity_v1.read_accepted_api_key_facts(
    p_actor_id pg_catalog.uuid,
    p_api_key_id pg_catalog.uuid
)
RETURNS TABLE(
    scopes pg_catalog.text[],
    expires_at pg_catalog.timestamptz,
    revision pg_catalog.text
)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_actor_id uuid;
    v_revoked_at timestamptz;
    v_expires_at timestamptz;
    v_scopes jsonb;
    v_key_hash text;
BEGIN
    IF p_actor_id IS NULL OR p_api_key_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid_request';
    END IF;

    -- Preserve bounded credential admission. Callers can retry contention in a
    -- savepoint without holding the request indefinitely behind key maintenance.
    SELECT key.user_id, key.revoked_at, key.expires_at, key.scopes, key.key_hash
    INTO v_actor_id, v_revoked_at, v_expires_at, v_scopes, v_key_hash
    FROM public.api_keys AS key
    WHERE key.id = p_api_key_id
    FOR SHARE NOWAIT;
    IF NOT FOUND
       OR v_actor_id IS DISTINCT FROM p_actor_id
       OR v_revoked_at IS NOT NULL
       OR (v_expires_at IS NOT NULL
           AND v_expires_at <= pg_catalog.clock_timestamp()) THEN
        RETURN;
    END IF;
    IF pg_catalog.jsonb_typeof(v_scopes) IS DISTINCT FROM 'array'
       OR v_key_hash IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023',
            MESSAGE='invalid stored API key facts';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(v_scopes) AS stored(value)
        WHERE pg_catalog.jsonb_typeof(stored.value) IS DISTINCT FROM 'string'
    ) THEN
        RAISE EXCEPTION USING ERRCODE='22023',
            MESSAGE='invalid stored API key facts';
    END IF;

    RETURN QUERY
    SELECT
        ARRAY(
            SELECT stored.value #>> '{}'
            FROM pg_catalog.jsonb_array_elements(v_scopes) WITH ORDINALITY
                AS stored(value, position)
            ORDER BY stored.position
        ),
        v_expires_at,
        pg_catalog.encode(
            pg_catalog.sha256(pg_catalog.convert_to(v_key_hash, 'UTF8')), 'hex'
        );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_identity_v1.read_accepted_api_key_facts(uuid, uuid) FROM PUBLIC;
-- aap-statement-break

-- Immutable execution-snapshot binding. The caller reads or locks the Job row
-- through the existing Jobs capability first, so this statement only re-reads
-- the fixed column in the same transaction.
CREATE FUNCTION aap_jobs_v1.read_job_execution_snapshot(p_job_id pg_catalog.uuid)
RETURNS pg_catalog.uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT job.execution_snapshot_id
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_jobs_v1.read_job_execution_snapshot(uuid) FROM PUBLIC;
-- aap-statement-break

-- Single uploading Task creation with fixed fields only. Authority is the same
-- Workspace creation/locking helper used by ordinary task creation, evaluated
-- inside the caller's unit of work; no caller-supplied columns are accepted.
CREATE FUNCTION aap_labeling_v1.create_uploading_task(
    p_task_id pg_catalog.uuid,
    p_project_id pg_catalog.uuid,
    p_display_id pg_catalog.text,
    p_file_name pg_catalog.text,
    p_file_path pg_catalog.text,
    p_file_type pg_catalog.text,
    p_actor_id pg_catalog.uuid
)
RETURNS pg_catalog.bool
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_authority jsonb;
    v_created boolean := false;
BEGIN
    IF p_task_id IS NULL OR p_project_id IS NULL OR p_actor_id IS NULL
       OR p_display_id IS NULL OR p_display_id !~ '^[A-Za-z0-9-]{1,30}$'
       OR p_file_name IS NULL OR p_file_name = ''
       OR pg_catalog.char_length(p_file_name) > 500
       OR p_file_path IS NULL OR p_file_path = ''
       OR pg_catalog.char_length(p_file_path) > 1000
       OR p_file_type IS NULL OR p_file_type NOT IN ('image', 'video') THEN
        RETURN false;
    END IF;

    -- Recheck current actor/project manager authority and lock the project row
    -- in the caller transaction. Reuses the canonical creation capability.
    v_authority := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_authority IS NULL OR (v_authority ->> 'ok')::boolean IS DISTINCT FROM true THEN
        RETURN false;
    END IF;

    INSERT INTO public.tasks (
        id, project_id, display_id, file_name, file_path, file_type, status
    )
    VALUES (
        p_task_id, p_project_id, p_display_id, p_file_name, p_file_path,
        p_file_type, 'uploading'
    )
    ON CONFLICT (id) DO NOTHING;
    v_created := FOUND;
    RETURN v_created;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.create_uploading_task(
    uuid, uuid, text, text, text, text, uuid) FROM PUBLIC;

-- aap-statement-break

-- Exact SHARE NOWAIT task locks for a bounded scope. The public relation is
-- SELECT-only for ordinary roles, so callers cannot take row locks on it.
CREATE FUNCTION aap_labeling_v1.lock_task_rows_for_core_read_share(
    p_task_ids pg_catalog.uuid[],
    p_project_id pg_catalog.uuid,
    p_batch_id pg_catalog.uuid
)
RETURNS SETOF aap_labeling_v1.task_rows
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT
        task.id, task.project_id, task.dataset_item_id, task.display_id,
        task.file_name, task.file_path, task.file_type,
        task.media_source_generation, task.media_object_identity,
        task.media_source_drifted, task.tags, task.status, task.assignee_id,
        task.assignee_is_override, task.assigned_at, task.is_labeled,
        task.overlap, task.total_annotations, task.total_predictions,
        task.precomputed_agreement, task.sequence_order, task.thumbnail_path,
        task.blurhash, task.batch_id, task.version, task.submitted_at,
        task.reviewer_id, task.reviewer_is_override, task.reviewer_claimed_at,
        task.review_round_id, task.first_review_eligible, task.first_reviewed_at,
        task.first_review_result, task.first_review_contributor_ids,
        task.annotation_contributor_ids, task.review_contributor_ids,
        task.review_submitter_id, task.reviewed_at, task.reject_reason,
        task.reject_reason_type, task.skip_reason, task.skipped_at,
        task.reopened_count, task.last_reopened_at, task.created_at,
        task.updated_at, task.thumbnail_output_id, task.original_id
    FROM public.tasks AS task
    WHERE task.id = ANY(p_task_ids)
      AND (p_project_id IS NULL OR task.project_id = p_project_id)
      AND (p_batch_id IS NULL OR task.batch_id = p_batch_id)
    ORDER BY task.id
    FOR SHARE NOWAIT;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.lock_task_rows_for_core_read_share(uuid[], uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

-- Exact immutable TaskOriginal binding facts owned by Assets. Labeling never
-- reads task_originals directly; the publication capability verifies these.
CREATE FUNCTION aap_assets_v1.read_task_original_binding(p_original_id pg_catalog.uuid)
RETURNS TABLE(
    task_id pg_catalog.uuid,
    project_id pg_catalog.uuid,
    creation_id pg_catalog.uuid,
    status pg_catalog.text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT original.task_id, original.project_id, original.creation_id,
           creation.status::text
    FROM public.task_originals AS original
    JOIN public.managed_original_creations AS creation
      ON creation.id = original.creation_id
    WHERE original.id = p_original_id
      AND creation.owner_kind = 'task'
      AND creation.status = 'verified'
      AND creation.dataset_id IS NULL
      AND creation.task_id = original.task_id
      AND creation.project_id = original.project_id
      AND creation.sha256 = original.sha256
      AND creation.size_bytes = original.size_bytes
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_assets_v1.read_task_original_binding(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.lock_prediction_for_core_read_share(
    p_prediction_id pg_catalog.uuid,
    p_task_id pg_catalog.uuid
)
RETURNS SETOF aap_labeling_v1.prediction_rows
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT prediction.id, prediction.task_id, prediction.project_id,
           prediction.ml_backend_id, prediction.ml_backend_pool_id,
           prediction.model_version, prediction.score, prediction.tool_unit_id,
           prediction.result, prediction.cluster, prediction.mislabeling,
           prediction.source, prediction.rejected_shape_indexes,
           prediction.created_at
    FROM public.predictions AS prediction
    WHERE prediction.id = p_prediction_id
      AND (p_task_id IS NULL OR prediction.task_id = p_task_id)
    FOR SHARE NOWAIT;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.lock_prediction_for_core_read_share(uuid, uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.lock_prediction_meta_for_core_read_share(
    p_meta_id pg_catalog.uuid
)
RETURNS SETOF aap_labeling_v1.prediction_meta_rows
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT meta.id, meta.prediction_id, meta.prediction_created_at,
           meta.failed_prediction_id, meta.inference_time_ms, meta.prompt_tokens,
           meta.completion_tokens, meta.total_tokens, meta.prompt_cost,
           meta.completion_cost, meta.total_cost, meta.extra, meta.created_at
    FROM public.prediction_metas AS meta
    WHERE meta.id = p_meta_id
    FOR SHARE NOWAIT;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.lock_prediction_meta_for_core_read_share(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.lock_video_segment_for_core_read_share(
    p_segment_id pg_catalog.uuid,
    p_dataset_item_id pg_catalog.uuid
)
RETURNS SETOF aap_labeling_v1.video_segment_rows
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT segment.id, segment.dataset_item_id, segment.segment_index,
           segment.start_frame, segment.end_frame, segment.assignee_id,
           segment.status, segment.locked_by, segment.locked_at,
           segment.lock_expires_at, segment.created_at, segment.updated_at
    FROM public.video_segments AS segment
    WHERE segment.id = p_segment_id
      AND (p_dataset_item_id IS NULL
           OR segment.dataset_item_id = p_dataset_item_id)
    FOR SHARE NOWAIT;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.lock_video_segment_for_core_read_share(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break

-- CAS publication with verified Assets binding. First publish requires the
-- caller's exact current status; same-original completion is idempotent across
-- later statuses. Different pointers are rejected.
CREATE FUNCTION aap_labeling_v1.publish_task_original(
    p_task_id pg_catalog.uuid,
    p_project_id pg_catalog.uuid,
    p_actor_id pg_catalog.uuid,
    p_original_id pg_catalog.uuid,
    p_creation_id pg_catalog.uuid,
    p_expected_status pg_catalog.text
)
RETURNS TABLE(original_id pg_catalog.uuid, status pg_catalog.text, version pg_catalog.int8)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_authority jsonb;
    v_original record;
    v_task public.tasks%ROWTYPE;
BEGIN
    IF p_task_id IS NULL OR p_project_id IS NULL OR p_actor_id IS NULL
       OR p_original_id IS NULL OR p_creation_id IS NULL
       OR p_expected_status IS NULL OR p_expected_status = '' THEN
        RETURN;
    END IF;

    v_authority := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_authority IS NULL OR (v_authority ->> 'ok')::boolean IS DISTINCT FROM true THEN
        RETURN;
    END IF;

    SELECT * INTO v_original
    FROM aap_assets_v1.read_task_original_binding(p_original_id);
    IF NOT FOUND
       OR v_original.task_id IS DISTINCT FROM p_task_id
       OR v_original.project_id IS DISTINCT FROM p_project_id
       OR v_original.creation_id IS DISTINCT FROM p_creation_id THEN
        RETURN;
    END IF;

    BEGIN
        SELECT * INTO v_task FROM public.tasks AS task
        WHERE task.id = p_task_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN;
    END;
    IF NOT FOUND OR v_task.project_id IS DISTINCT FROM p_project_id
       OR v_task.dataset_item_id IS NOT NULL
       OR EXISTS (
           SELECT 1 FROM public.task_dataset_item_links AS link
           WHERE link.task_id = p_task_id
       ) THEN
        RETURN;
    END IF;

    IF v_task.original_id IS NULL THEN
        -- Actual CAS: only the caller's observed uploading row may transition.
        IF v_task.status IS DISTINCT FROM p_expected_status
           OR p_expected_status IS DISTINCT FROM 'uploading' THEN
            RETURN;
        END IF;
        UPDATE public.tasks AS task
        SET original_id = p_original_id,
            status = 'pending',
            updated_at = pg_catalog.clock_timestamp()
        WHERE task.id = p_task_id
          AND task.original_id IS NULL
          AND task.status = p_expected_status;
        IF NOT FOUND THEN
            RETURN;
        END IF;
    ELSIF v_task.original_id IS DISTINCT FROM p_original_id THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT task.original_id, task.status::text, task.version::bigint
    FROM public.tasks AS task
    WHERE task.id = p_task_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.publish_task_original(
    uuid, uuid, uuid, uuid, uuid, text) FROM PUBLIC;
