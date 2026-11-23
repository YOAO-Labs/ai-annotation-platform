CREATE FUNCTION yoao_learning_v1.training_dataset_task(p_snapshot jsonb)
RETURNS text LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp
RETURN CASE
    WHEN jsonb_typeof(p_snapshot -> 'schema') = 'number' AND p_snapshot ->> 'schema' = '1'
        AND (NOT p_snapshot ? 'task' OR p_snapshot ->> 'task' = 'image-detection')
        AND NOT p_snapshot ?| ARRAY['geometry', 'bridge']
        AND (NOT p_snapshot ? 'format' OR p_snapshot ->> 'format' = 'ultralytics-detection')
    THEN 'detect'
    WHEN jsonb_typeof(p_snapshot -> 'schema') = 'number' AND p_snapshot ->> 'schema' = '2'
        AND p_snapshot @> '{"task":"image-instance-segmentation","geometry":"normalized-simple-polygon","bridge":"ultralytics-simple-polygon-1"}'
        AND (NOT p_snapshot ? 'format' OR p_snapshot ->> 'format' = 'ultralytics-segmentation')
    THEN 'segment'
    WHEN p_snapshot ->> 'schema' = '3' AND p_snapshot @> '{"schema":3,"task":"detect","media_type":"image","format":"ultralytics-yolo","geometry":"normalized-bbox","bridge":"ultralytics-bbox-1"}'
    THEN 'detect'
    WHEN p_snapshot ->> 'schema' = '3' AND p_snapshot @> '{"schema":3,"task":"segment","media_type":"image","format":"ultralytics-yolo","geometry":"normalized-simple-polygon","bridge":"ultralytics-simple-polygon-1"}'
    THEN 'segment'
    WHEN p_snapshot ->> 'schema' = '4' AND p_snapshot @> '{"schema":4,"task":"segment","media_type":"image","format":"aap-instance-rle","geometry":"instance-mask","bridge":"ultralytics-instance-rle-1"}'
    THEN 'segment'
END;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_profile(p_snapshot jsonb, p_task text)
RETURNS jsonb LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp
RETURN jsonb_build_object(
    'schema', p_snapshot -> 'schema', 'task', p_task,
    'media_type', CASE WHEN NOT p_snapshot ? 'media_type' THEN 'image'
        WHEN jsonb_typeof(p_snapshot -> 'media_type') = 'string' THEN p_snapshot ->> 'media_type' END,
    'source_media_type', CASE
        WHEN NOT p_snapshot ? 'source' OR p_snapshot -> 'source' = 'null' THEN 'image'
        WHEN jsonb_typeof(p_snapshot -> 'source') = 'object' THEN CASE
            WHEN NOT (p_snapshot -> 'source') ? 'media_type' THEN 'image'
            WHEN jsonb_typeof(p_snapshot #> '{source,media_type}') = 'string'
                THEN p_snapshot #>> '{source,media_type}' END END,
    'format', CASE WHEN p_snapshot ->> 'bridge' = 'ultralytics-instance-rle-1'
        THEN 'aap-instance-rle' ELSE 'ultralytics-yolo' END,
    'geometry', CASE WHEN p_snapshot ->> 'bridge' = 'ultralytics-instance-rle-1'
        THEN 'instance-mask' WHEN p_task = 'segment'
        THEN 'normalized-simple-polygon' ELSE 'normalized-bbox' END,
    'bridge', CASE WHEN p_snapshot ->> 'bridge' = 'ultralytics-instance-rle-1'
        THEN 'ultralytics-instance-rle-1' WHEN p_task = 'segment'
        THEN 'ultralytics-simple-polygon-1' ELSE 'ultralytics-bbox-1' END
);
-- aap-statement-break
CREATE VIEW yoao_learning_v1.training_dataset_metadata AS
SELECT v.id, v.project_id, v.creator_id, v.parent_id, v.async_job_id, v.name,
    v.status, v.snapshot_digest, v.package_sha256, v.size_bytes, v.progress_pct,
    v.error_code, v.error_message, v.created_at, v.completed_at,
    m.task_dataset_id, m.ordinal, t.recognized_task,
    CASE WHEN jsonb_typeof(v.snapshot -> 'task') = 'string' THEN v.snapshot ->> 'task' END AS raw_task,
    CASE v.snapshot ->> 'task'
        WHEN 'image-instance-segmentation' THEN 'segment'
        WHEN 'image-detection' THEN 'detect'
        ELSE coalesce(v.snapshot ->> 'task', 'detect')
    END AS task,
    coalesce(v.snapshot ->> 'media_type', 'image') AS media_type,
    coalesce(v.snapshot ->> 'format', 'ultralytics-yolo') AS format,
    coalesce(v.snapshot #>> '{source,media_type}', 'image') AS source_media_type,
    coalesce(jsonb_array_length(v.snapshot -> 'classes'), 0) AS class_count,
    coalesce(v.snapshot -> 'annotation_scope', '{}'::jsonb) AS annotation_scope,
    coalesce(v.manifest -> 'summary', v.preparation_report -> 'summary', '{}'::jsonb) AS summary,
    yoao_learning_v1.training_dataset_profile(v.snapshot, t.recognized_task) AS contract_profile
FROM public.training_dataset_versions v
LEFT JOIN public.task_dataset_version_memberships m ON m.version_id = v.id
CROSS JOIN LATERAL (SELECT yoao_learning_v1.training_dataset_task(v.snapshot) AS recognized_task) t;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_facts(p_id uuid, p_project uuid, p_lock boolean)
RETURNS TABLE (
    id uuid, project_id uuid, creator_id uuid, parent_id uuid, async_job_id uuid,
    attempt_id uuid, name text, status text, snapshot_digest text, package_key text,
    package_sha256 text, size_bytes bigint, storage_ref jsonb, snapshot jsonb, manifest jsonb,
    progress_pct integer, error_code text, error_message text, created_at timestamptz,
    completed_at timestamptz
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_lock THEN
        PERFORM v.id FROM public.training_dataset_versions v
        WHERE v.id = p_id AND (p_project IS NULL OR v.project_id = p_project) FOR UPDATE;
        IF NOT FOUND THEN
            RETURN;
        END IF;
    END IF;
    RETURN QUERY SELECT v.id, v.project_id, v.creator_id, v.parent_id, v.async_job_id,
        v.attempt_id, v.name::text, v.status::text, v.snapshot_digest::text, v.package_key::text,
        v.package_sha256::text, v.size_bytes, v.storage_ref,
        (SELECT coalesce(jsonb_object_agg(k, v.snapshot -> k), '{}'::jsonb)
         FROM unnest(ARRAY['schema','task','media_type','format','geometry','bridge',
                           'classes','source','annotation_scope']) k
         WHERE v.snapshot ? k),
        (SELECT coalesce(jsonb_object_agg(k, v.manifest -> k), '{}'::jsonb)
         FROM unnest(ARRAY['schema','task','media_type','format','geometry','bridge','classes',
                           'content_digest','source_digest','source_lineage_contract','summary']) k
         WHERE v.manifest ? k),
        v.progress_pct, v.error_code::text, v.error_message::text, v.created_at, v.completed_at
    FROM public.training_dataset_versions v
    WHERE v.id = p_id AND (p_project IS NULL OR v.project_id = p_project);
END
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_read(p_id uuid, p_project uuid, p_lock boolean)
RETURNS TABLE (
    id uuid, project_id uuid, creator_id uuid, parent_id uuid, async_job_id uuid,
    attempt_id uuid, name text, status text, snapshot_digest text, package_key text,
    package_sha256 text, size_bytes bigint, storage_ref jsonb, snapshot jsonb, manifest jsonb,
    progress_pct integer, error_code text, error_message text, created_at timestamptz,
    completed_at timestamptz, preparation_report jsonb
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_lock THEN
        PERFORM v.id FROM public.training_dataset_versions v
        WHERE v.id = p_id AND (p_project IS NULL OR v.project_id = p_project) FOR UPDATE;
        IF NOT FOUND THEN
            RETURN;
        END IF;
    END IF;
    RETURN QUERY SELECT v.id, v.project_id, v.creator_id, v.parent_id, v.async_job_id,
        v.attempt_id, v.name::text, v.status::text, v.snapshot_digest::text, v.package_key::text,
        v.package_sha256::text, v.size_bytes, v.storage_ref, v.snapshot, v.manifest,
        v.progress_pct, v.error_code::text, v.error_message::text, v.created_at, v.completed_at,
        v.preparation_report
    FROM public.training_dataset_versions v
    WHERE v.id = p_id AND (p_project IS NULL OR v.project_id = p_project);
END
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_hold(p_id uuid, p_project uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (SELECT v.id FROM public.training_dataset_versions v
        WHERE v.id = p_id AND v.project_id = p_project FOR SHARE)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_request(p_project uuid, p_request uuid, p_lock boolean)
RETURNS TABLE (id uuid, request_digest text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT v.id, v.request_digest::text FROM public.training_dataset_versions v
        WHERE v.project_id = p_project AND v.request_id = p_request FOR UPDATE;
    ELSE
        RETURN QUERY SELECT v.id, v.request_digest::text FROM public.training_dataset_versions v
        WHERE v.project_id = p_project AND v.request_id = p_request;
    END IF;
END
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_job_lock(p_job uuid)
RETURNS TABLE (id uuid, project_id uuid, attempt_id uuid, status text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT v.id, v.project_id, v.attempt_id, v.status::text FROM public.training_dataset_versions v
    WHERE v.async_job_id = p_job FOR UPDATE
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_create(
    p_id uuid, p_project uuid, p_creator uuid, p_parent uuid, p_request uuid, p_request_digest text,
    p_attempt uuid, p_name text, p_snapshot jsonb, p_digest text, p_report jsonb
) RETURNS timestamptz LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    INSERT INTO public.training_dataset_versions
        (id, project_id, creator_id, parent_id, request_id, request_digest, attempt_id,
         name, status, snapshot, snapshot_digest, preparation_report, manifest, progress_pct)
    SELECT p_id, p_project, p_creator, p_parent, p_request, p_request_digest, p_attempt,
        p_name, 'pending', p_snapshot, p_digest, p_report, '{}'::jsonb, 0
    WHERE yoao_learning_v1.training_dataset_task(p_snapshot) IS NOT NULL
        AND (p_parent IS NULL OR EXISTS (SELECT 1 FROM public.training_dataset_versions p
             WHERE p.id = p_parent AND p.project_id = p_project AND p.status = 'ready'))
    RETURNING created_at
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_stream(p_project uuid, p_id uuid)
RETURNS TABLE (id uuid, task text) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT s.id, s.task::text FROM public.project_task_datasets s
    WHERE s.project_id = p_project AND (p_id IS NULL OR s.id = p_id)
    ORDER BY s.task, s.id LIMIT 5
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_associate(p_project uuid, p_version uuid)
RETURNS TABLE (task_dataset_id uuid, ordinal bigint, failure text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_task text;
    v_stream uuid;
    v_next bigint;
    v_ordinal bigint;
    v_project uuid;
    v_stream_task text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(p_project::text, 0));
    SELECT yoao_learning_v1.training_dataset_task(v.snapshot) INTO v_task
        FROM public.training_dataset_versions v WHERE v.id = p_version AND v.project_id = p_project;
    IF v_task IS NULL THEN
        RETURN QUERY SELECT NULL::uuid, NULL::bigint, 'unknown_data_contract'::text;
        RETURN;
    END IF;
    SELECT m.task_dataset_id, m.ordinal, s.project_id, s.task
        INTO v_stream, v_ordinal, v_project, v_stream_task
        FROM public.task_dataset_version_memberships m
        JOIN public.project_task_datasets s ON s.id = m.task_dataset_id
        WHERE m.version_id = p_version;
    IF FOUND THEN
        IF v_project <> p_project OR v_stream_task <> v_task THEN
            RETURN QUERY SELECT NULL::uuid, NULL::bigint, 'task_dataset_membership_mismatch'::text;
        ELSE
            RETURN QUERY SELECT v_stream, v_ordinal, NULL::text;
        END IF;
        RETURN;
    END IF;
    SELECT s.id, s.next_ordinal INTO v_stream, v_next FROM public.project_task_datasets s
        WHERE s.project_id = p_project AND s.task = v_task FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO public.project_task_datasets (id, project_id, task)
            VALUES (gen_random_uuid(), p_project, v_task) RETURNING id, next_ordinal INTO v_stream, v_next;
    END IF;
    IF v_next <= coalesce((SELECT max(m.ordinal) FROM public.task_dataset_version_memberships m
                           WHERE m.task_dataset_id = v_stream), 0) THEN
        RETURN QUERY SELECT NULL::uuid, NULL::bigint, 'task_dataset_high_water_invalid'::text;
        RETURN;
    END IF;
    UPDATE public.project_task_datasets SET next_ordinal = v_next + 1 WHERE id = v_stream;
    INSERT INTO public.task_dataset_version_memberships (version_id, task_dataset_id, ordinal)
        VALUES (p_version, v_stream, v_next);
    RETURN QUERY SELECT v_stream, v_next, NULL::text;
END
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_set_storage(
    p_project uuid, p_id uuid, p_attempt uuid, p_ref jsonb
) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    WITH changed AS (
        UPDATE public.training_dataset_versions v SET storage_ref = p_ref
        WHERE v.id = p_id AND v.project_id = p_project AND v.attempt_id = p_attempt AND v.storage_ref IS NULL
          AND ((v.status = 'pending' AND v.async_job_id IS NULL AND p_ref ->> 'origin' = 'new')
            OR (v.status IN ('ready','failed','cancelled') AND p_ref ->> 'origin' = 'verified-retained'
                AND jsonb_typeof(p_ref -> 'evidence') = 'object'))
        RETURNING v.id
    ) SELECT EXISTS (SELECT FROM changed)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_queue(p_id uuid, p_expected uuid, p_attempt uuid, p_job uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    WITH changed AS (
        UPDATE public.training_dataset_versions v
        SET async_job_id = p_job, attempt_id = p_attempt, status = 'pending', progress_pct = 0,
            error_code = NULL, error_message = NULL, completed_at = NULL
        WHERE v.id = p_id AND v.attempt_id = p_expected AND v.storage_ref IS NOT NULL
            AND (v.status IN ('failed','cancelled') OR (v.status = 'pending' AND v.async_job_id IS NULL))
        RETURNING v.id
    ) SELECT EXISTS (SELECT FROM changed)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_start(p_id uuid, p_job uuid, p_attempt uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    WITH changed AS (
        UPDATE public.training_dataset_versions v SET status = 'running'
        WHERE v.id = p_id AND v.async_job_id = p_job AND v.attempt_id = p_attempt AND v.status = 'pending'
        RETURNING v.id
    ) SELECT EXISTS (SELECT FROM changed)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_progress(p_id uuid, p_job uuid, p_attempt uuid, p_progress integer)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    WITH changed AS (
        UPDATE public.training_dataset_versions v SET progress_pct = p_progress
        WHERE v.id = p_id AND v.async_job_id = p_job AND v.attempt_id = p_attempt AND v.status = 'running'
            AND p_progress BETWEEN v.progress_pct AND 80
        RETURNING v.id
    ) SELECT EXISTS (SELECT FROM changed)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_publish(
    p_id uuid, p_job uuid, p_attempt uuid, p_snapshot_digest text, p_storage_ref jsonb,
    p_manifest jsonb, p_package_key text, p_package_sha text, p_bytes bigint, p_completed timestamptz
) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    WITH changed AS (
        UPDATE public.training_dataset_versions v
        SET manifest = p_manifest, package_key = p_package_key, package_sha256 = p_package_sha,
            size_bytes = p_bytes, status = 'ready', progress_pct = 100, completed_at = p_completed
        WHERE v.id = p_id AND v.async_job_id = p_job AND v.attempt_id = p_attempt AND v.status = 'running'
            AND v.snapshot_digest = p_snapshot_digest AND v.storage_ref = p_storage_ref
            AND p_package_key = (v.storage_ref ->> 'prefix') || 'attempts/' || p_attempt::text || '/dataset.zip'
            AND p_manifest ->> 'source_digest' = v.snapshot_digest AND p_package_sha ~ '^[0-9a-f]{64}$'
            AND p_bytes > 0
        RETURNING v.id
    ) SELECT EXISTS (SELECT FROM changed)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_fail(
    p_id uuid, p_job uuid, p_attempt uuid, p_status text, p_cancelled boolean,
    p_code text, p_message text, p_completed timestamptz
) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    WITH changed AS (
        UPDATE public.training_dataset_versions v SET status = CASE WHEN p_cancelled THEN 'cancelled' ELSE 'failed' END,
            error_code = p_code, error_message = p_message, completed_at = p_completed
        WHERE v.id = p_id AND v.async_job_id = p_job AND v.attempt_id = p_attempt
            AND v.status = p_status AND v.status IN ('pending','running')
        RETURNING v.id
    ) SELECT EXISTS (SELECT FROM changed)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_delete(p_project uuid, p_id uuid, p_attempt uuid)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    WITH removed AS (
        DELETE FROM public.training_dataset_versions v
        WHERE v.id = p_id AND v.project_id = p_project AND v.attempt_id = p_attempt
            AND v.status IN ('ready','failed','cancelled')
            AND EXISTS (SELECT FROM public.training_storage_deletions d
                WHERE d.bucket = v.storage_ref ->> 'bucket' AND d.prefix = v.storage_ref ->> 'prefix'
                    AND d.storage_ref = v.storage_ref)
        RETURNING v.id
    ) SELECT EXISTS (SELECT FROM removed)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_manifest(p_project uuid, p_id uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT v.manifest FROM public.training_dataset_versions v WHERE v.id = p_id AND v.project_id = p_project
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_sample_evidence(p_project uuid, p_id uuid, p_task uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'manifest', (SELECT coalesce(jsonb_object_agg(k, v.manifest -> k), '{}'::jsonb)
            FROM unnest(ARRAY['source_digest','source_lineage_contract']) k WHERE v.manifest ? k),
        'manifest_member', manifest_member.member,
        'contract_present', v.snapshot ? 'managed_original_contract',
        'contract', v.snapshot -> 'managed_original_contract',
        'original_member', CASE WHEN manifest_member.member ->> 'asset_reference' IS NULL
            THEN jsonb_build_object('task_id', source.member -> 'task_id', 'original', source.member -> 'original') END
    )
    FROM public.training_dataset_versions v
    LEFT JOIN LATERAL (SELECT jsonb_path_query_first(v.manifest,
        '$.samples[*] ? (@.task_id == $task_id)', jsonb_build_object('task_id', p_task::text))) manifest_member(member) ON true
    LEFT JOIN LATERAL (SELECT jsonb_path_query_first(v.snapshot,
        '$.samples[*] ? (@.task_id == $task_id)', jsonb_build_object('task_id', p_task::text))) source(member) ON true
    WHERE v.id = p_id AND v.project_id = p_project
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_original_history()
RETURNS TABLE (id uuid, snapshot jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT v.id, v.snapshot FROM public.training_dataset_versions v
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_binding_history()
RETURNS TABLE (training_versions bigint, training_runs bigint, training_previews bigint,
    sample_evaluations bigint, active_learning_rounds bigint, deletion_intents bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT (SELECT count(*) FROM public.training_dataset_versions),
           (SELECT count(id) FROM public.training_runs),
           (SELECT count(id) FROM public.training_previews),
           (SELECT count(id) FROM public.sample_evaluation_runs),
           (SELECT count(id) FROM public.active_learning_rounds),
           (SELECT count(*) FROM public.training_storage_deletions)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_asset_versions(p_dataset uuid, p_workbench boolean)
RETURNS TABLE (id uuid, project_id uuid, name text, status text, created_at timestamptz, snapshot_digest text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT v.id, v.project_id, v.name::text, v.status::text, v.created_at, v.snapshot_digest::text
    FROM public.training_dataset_versions v
    WHERE v.snapshot @> jsonb_build_object('samples', jsonb_build_array(jsonb_build_object('dataset_id', p_dataset)))
        OR (p_workbench AND (
            v.snapshot #>> '{source,dataset_id}' = p_dataset::text
            OR EXISTS (
                SELECT FROM yoao_labeling_v1.task_rows t
                JOIN yoao_assets_v1.spatial_item_references i ON i.item_id = t.dataset_item_id
                WHERE i.dataset_id = p_dataset AND t.project_id = v.project_id
                    AND v.snapshot @> jsonb_build_object('samples', jsonb_build_array(jsonb_build_object('task_id', t.id)))
            )
        ))
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_location_versions(p_location uuid)
RETURNS TABLE (id uuid, project_id uuid, name text, status text, created_at timestamptz, snapshot_digest text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT v.id, v.project_id, v.name::text, v.status::text, v.created_at, v.snapshot_digest::text
    FROM public.training_dataset_versions v
    WHERE v.snapshot @> jsonb_build_object('samples',
        jsonb_build_array(jsonb_build_object('source_lineage', jsonb_build_object('location_id', p_location))))
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_analysis_read(p_version uuid)
RETURNS TABLE (version_id uuid, input_digest text, summary jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT a.version_id, a.input_digest::text, a.summary
    FROM public.training_dataset_analyses a WHERE a.version_id = p_version
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_analysis_start(
    p_version uuid, p_project uuid, p_snapshot_digest text, p_package_sha text,
    p_input_digest text, p_summary jsonb
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    PERFORM v.id FROM public.training_dataset_versions v
    WHERE v.id = p_version AND v.project_id = p_project AND v.status = 'ready'
        AND v.snapshot_digest = p_snapshot_digest AND v.package_sha256 IS NOT DISTINCT FROM p_package_sha
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    DELETE FROM public.training_dataset_analyses WHERE version_id = p_version;
    INSERT INTO public.training_dataset_analyses (version_id, input_digest, summary)
        VALUES (p_version, p_input_digest, p_summary);
    RETURN true;
END
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_analysis_add_members(p_version uuid, p_input_digest text, p_members jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF jsonb_array_length(p_members) NOT BETWEEN 1 AND 300 THEN
        RETURN false;
    END IF;
    PERFORM a.version_id FROM public.training_dataset_analyses a
        WHERE a.version_id = p_version AND a.input_digest = p_input_digest FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    INSERT INTO public.training_dataset_analyzed_samples
        (version_id, task_id, included, exclusion_reason, issue, sha256, label_sha256,
        semantic_label_digest, split, width, height, size_bytes, instances, is_negative,
        explicit_negative, class_ids, labels, media_state, media_checked_at)
    SELECT p_version, m.task_id, m.included, m.exclusion_reason, m.issue, m.sha256, m.label_sha256,
        m.semantic_label_digest, m.split, m.width, m.height, m.size_bytes, m.instances, m.is_negative,
        m.explicit_negative, m.class_ids, m.labels, coalesce(m.media_state, 'unchecked'), m.media_checked_at
    FROM jsonb_to_recordset(p_members) AS m(
        task_id uuid, included boolean, exclusion_reason text, issue text, sha256 text, label_sha256 text,
        semantic_label_digest text, split text, width integer, height integer, size_bytes bigint,
        instances integer, is_negative boolean, explicit_negative boolean, class_ids jsonb, labels jsonb,
        media_state text, media_checked_at timestamptz
    );
    RETURN true;
END
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_analysis_sample_page(
    p_version uuid, p_input_digest text, p_limit integer, p_offset bigint,
    p_split text, p_class integer, p_sha text, p_kind text, p_contents text[]
) RETURNS TABLE (total bigint, items jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    WITH filtered AS (
        SELECT s.version_id, s.task_id, s.included, s.exclusion_reason, s.issue, s.sha256, s.label_sha256,
            s.semantic_label_digest, s.split, s.width, s.height, s.size_bytes, s.instances,
            s.is_negative, s.explicit_negative, s.class_ids, s.media_state, s.media_checked_at
        FROM public.training_dataset_analyzed_samples s
        JOIN public.training_dataset_analyses a ON a.version_id = s.version_id
        WHERE s.version_id = p_version AND a.input_digest = p_input_digest
          AND s.included = (p_kind <> 'excluded')
          AND (p_split IS NULL OR s.split = p_split)
          AND (p_class IS NULL OR s.class_ids @> jsonb_build_array(p_class))
          AND (p_sha IS NULL OR s.sha256 = p_sha)
          AND CASE p_kind WHEN 'positive' THEN NOT s.is_negative WHEN 'negative' THEN s.is_negative
                   WHEN 'unavailable' THEN s.issue IS NOT NULL
                   WHEN 'all' THEN true WHEN 'excluded' THEN true ELSE false END
          AND (p_contents IS NULL OR s.sha256 = ANY(p_contents))
    ), page AS (
        SELECT * FROM filtered ORDER BY task_id
        LIMIT least(greatest(p_limit, 1), 100) OFFSET greatest(p_offset, 0)
    )
    SELECT (SELECT count(*) FROM filtered),
           coalesce((SELECT jsonb_agg(to_jsonb(page) ORDER BY task_id) FROM page), '[]'::jsonb)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_analysis_member(p_version uuid, p_task uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'version_id', s.version_id, 'task_id', s.task_id, 'included', s.included, 'exclusion_reason', s.exclusion_reason,
        'issue', s.issue, 'sha256', s.sha256, 'label_sha256', s.label_sha256,
        'semantic_label_digest', s.semantic_label_digest, 'split', s.split, 'width', s.width,
        'height', s.height, 'size_bytes', s.size_bytes, 'instances', s.instances, 'is_negative', s.is_negative,
        'explicit_negative', s.explicit_negative, 'class_ids', s.class_ids, 'labels', s.labels,
        'media_state', s.media_state, 'media_checked_at', s.media_checked_at
    )
    FROM public.training_dataset_analyzed_samples s WHERE s.version_id = p_version AND s.task_id = p_task
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_analysis_observations(p_version uuid)
RETURNS TABLE (task_id uuid, sha256 text, size_bytes bigint, included boolean, media_state text, media_checked_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT s.task_id, s.sha256::text, s.size_bytes, s.included, s.media_state::text, s.media_checked_at
    FROM public.training_dataset_analyzed_samples s WHERE s.version_id = p_version AND s.media_state <> 'unchecked'
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_analysis_media_counts(p_version uuid)
RETURNS TABLE (state text, count bigint, checked_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT s.media_state::text, count(*), max(s.media_checked_at)
    FROM public.training_dataset_analyzed_samples s
    WHERE s.version_id = p_version AND s.included GROUP BY s.media_state
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_analysis_observe_media(
    p_version uuid, p_task uuid, p_sha text, p_size bigint, p_state text, p_checked_at timestamptz
) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    WITH changed AS (
        UPDATE public.training_dataset_analyzed_samples s SET media_state = p_state, media_checked_at = p_checked_at
        WHERE s.version_id = p_version AND s.task_id = p_task AND s.included
            AND s.sha256 IS NOT DISTINCT FROM p_sha AND s.size_bytes IS NOT DISTINCT FROM p_size
            AND p_state IN ('verified','unavailable')
        RETURNING s.task_id
    ) SELECT EXISTS (SELECT FROM changed)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_storage_stage_deletion(
    p_id uuid, p_record uuid, p_purpose text, p_ref jsonb
) RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    INSERT INTO public.training_storage_deletions (id, storage_ref, bucket, prefix)
    SELECT p_id, owned.storage_ref, owned.storage_ref ->> 'bucket', owned.storage_ref ->> 'prefix'
    FROM (
        SELECT v.storage_ref FROM public.training_dataset_versions v
        WHERE p_purpose = 'training-dataset' AND v.id = p_record AND v.status IN ('ready','failed','cancelled')
        UNION ALL
        SELECT r.storage_ref FROM public.training_runs r WHERE p_purpose = 'training-run' AND r.id = p_record
    ) AS owned WHERE owned.storage_ref = p_ref
    RETURNING id
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_storage_claim_deletion(p_id uuid, p_attempted timestamptz)
RETURNS TABLE (bucket text, prefix text, storage_ref jsonb)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    WITH candidate AS (
        SELECT d.id FROM public.training_storage_deletions d
        WHERE d.id = p_id AND NOT EXISTS (
            SELECT FROM public.training_dataset_versions v
            WHERE d.storage_ref ->> 'purpose' = 'training-dataset'
                AND v.id::text = right(rtrim(d.prefix, '/'), 36)
        ) AND NOT EXISTS (
            SELECT FROM public.training_runs r
            WHERE d.storage_ref ->> 'purpose' = 'training-run'
                AND r.id::text = right(rtrim(d.prefix, '/'), 36)
        )
        FOR UPDATE SKIP LOCKED
    )
    UPDATE public.training_storage_deletions d SET last_attempted_at = p_attempted
    WHERE d.id = (SELECT id FROM candidate)
    RETURNING d.bucket::text, d.prefix::text, d.storage_ref
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_storage_complete_deletion(p_id uuid, p_ref jsonb)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    DELETE FROM public.training_storage_deletions d
    WHERE d.id = p_id AND d.storage_ref = p_ref AND d.last_attempted_at IS NOT NULL
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_storage_deletion_batch()
RETURNS TABLE (id uuid) LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT d.id FROM public.training_storage_deletions d
    ORDER BY d.last_attempted_at ASC NULLS FIRST, d.created_at, d.id LIMIT 100
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.training_dataset_high_water_violations(p_project uuid)
RETURNS TABLE (task_dataset_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
    SELECT s.id FROM public.project_task_datasets s
    JOIN public.task_dataset_version_memberships m ON m.task_dataset_id = s.id
    WHERE s.project_id = p_project
    GROUP BY s.id, s.next_ordinal
    HAVING s.next_ordinal <= max(m.ordinal)
    ORDER BY s.id
$function$;
