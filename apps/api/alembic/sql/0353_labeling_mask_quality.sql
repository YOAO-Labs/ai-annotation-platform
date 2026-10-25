CREATE FUNCTION aap_labeling_v1._quality_exact_keys(p_value jsonb, p_expected text[])
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT pg_catalog.array_agg(key ORDER BY key) =
           (SELECT pg_catalog.array_agg(value ORDER BY value)
            FROM pg_catalog.unnest(p_expected) AS fields(value))
    FROM pg_catalog.jsonb_object_keys(p_value) AS fields(key)
$function$;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.mask_qc_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_field text;
    v_id uuid;
    v_actor_id uuid;
    v_task_id uuid;
    v_issue public.mask_qc_issues%ROWTYPE;
    v_run public.mask_qc_runs%ROWTYPE;
    v_data jsonb;
    v_rows jsonb := '[]'::jsonb;
    v_statuses jsonb := '{}'::jsonb;
    v_created boolean := false;
    v_source jsonb;
    v_version integer;
    v_current boolean;
    v_requested jsonb;
    v_account jsonb;
    v_lock_reply jsonb;
    v_tracker_job public.video_tracker_jobs%ROWTYPE;
    v_count integer := 0;
    v_counts jsonb := '{}'::jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-quality-data/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Mask QC request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'read_source' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','scope','task_ids','annotation_ids'];
        WHEN 'find_run' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','singleflight_key'];
        WHEN 'create_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','project_id','async_job_id','requested_by_id','scope_json','config_revision','config_digest','config_snapshot','source_snapshot','source_snapshot_digest','task_snapshot_digests','singleflight_key','summary'];
        WHEN 'read_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','lock'];
        WHEN 'read_run_by_job' THEN
            v_expected_keys := ARRAY['schema','operation','async_job_id','lock'];
        WHEN 'begin_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','at'];
        WHEN 'progress_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','progress_pct'];
        WHEN 'complete_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','at','summary'];
        WHEN 'fail_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','at','error_message'];
        WHEN 'cancel_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','at'];
        WHEN 'reconcile_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','at','terminal_cancelled','error_message'];
        WHEN 'upsert_issue' THEN
            v_expected_keys := ARRAY['schema','operation','issue'];
        WHEN 'mark_seen' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','run_id','dedupe_keys'];
        WHEN 'read_issue' THEN
            v_expected_keys := ARRAY['schema','operation','issue_id','lock','nowait'];
        WHEN 'read_issues' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','issue_ids'];
        WHEN 'list_issues' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','limit','status','severity','code','task_id','annotation_id','frame','allowed_task_ids','cursor_rank','cursor_created_at','cursor_id'];
        WHEN 'read_task_run_issues' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','run_id'];
        WHEN 'find_latest_task_run' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','task_id'];
        WHEN 'find_current_completed_task_run' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','task_id','config_digest','source_digest'];
        WHEN 'read_open_task_issues' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','severity','issue_ids'];
        WHEN 'decide_issue' THEN
            v_expected_keys := ARRAY['schema','operation','issue_id','expected_task_id','actor_id','status','decided_at'];
        WHEN 'resolve_repaired_issue' THEN
            v_expected_keys := ARRAY['schema','operation','issue_id','actor_id','decided_at'];
        WHEN 'read_issue_statuses' THEN
            v_expected_keys := ARRAY['schema','operation','issue_ids'];
        WHEN 'issue_versions_current' THEN
            v_expected_keys := ARRAY['schema','operation','issue_id'];
        WHEN 'read_annotation_revision' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','annotation_id','annotation_version'];
        WHEN 'read_tracker_candidate_context' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','job_id'];
        WHEN 'read_run_summary' THEN
            v_expected_keys := ARRAY['schema','operation','run_id'];
        ELSE
            RAISE EXCEPTION 'Invalid Mask QC operation' USING ERRCODE = '22023';
    END CASE;
    IF NOT aap_labeling_v1._quality_exact_keys(p_request, v_expected_keys) THEN
        RAISE EXCEPTION 'Invalid Mask QC request fields' USING ERRCODE = '22023';
    END IF;

    IF v_operation = 'read_source' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'project_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'scope') IS DISTINCT FROM 'string'
           OR p_request ->> 'scope' NOT IN ('project','task_ids','annotation_ids')
           OR pg_catalog.jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'annotation_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'task_ids') > 1000
           OR pg_catalog.jsonb_array_length(p_request -> 'annotation_ids') > 1000 THEN
            RAISE EXCEPTION 'Invalid Mask QC source scope' USING ERRCODE = '22023';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'task_ids') AS item(value)
            WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
               OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'annotation_ids') AS item(value)
            WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
               OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ) THEN
            RAISE EXCEPTION 'Invalid Mask QC source identifiers' USING ERRCODE = '22023';
        END IF;
        v_id := (p_request ->> 'project_id')::uuid;
        IF p_request ->> 'scope' = 'task_ids' AND EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids') AS item(id)
            LEFT JOIN public.tasks AS task ON task.id = item.id::uuid AND task.project_id = v_id
            WHERE task.id IS NULL
        ) THEN
            RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,
                'request',p_request,'data',pg_catalog.jsonb_build_object('missing_scope',true,'annotations','[]'::jsonb));
        END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
            'id',annotation.id,'project_id',annotation.project_id,'task_id',annotation.task_id,
            'dataset_item_id',task.dataset_item_id,
            'class_name',annotation.class_name,'source',annotation.source,'track_id',annotation.track_id,
            'version',annotation.version,'geometry',annotation.geometry
        ) ORDER BY annotation.task_id,annotation.id),'[]'::jsonb)
        INTO v_rows
        FROM public.annotations AS annotation
        JOIN public.tasks AS task ON task.id=annotation.task_id
        WHERE annotation.project_id = v_id
          AND annotation.is_active IS TRUE
          AND annotation.was_cancelled IS FALSE
          AND annotation.geometry ->> 'type' IN ('raster_mask','video_track_mask')
          AND (p_request ->> 'scope' <> 'task_ids' OR annotation.task_id IN (
              SELECT item.id::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids') AS item(id)))
          AND (p_request ->> 'scope' <> 'annotation_ids' OR annotation.id IN (
              SELECT item.id::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'annotation_ids') AS item(id)));
        IF p_request ->> 'scope' = 'annotation_ids' AND (
            SELECT pg_catalog.count(DISTINCT item.id::uuid)
            FROM pg_catalog.jsonb_array_elements_text(p_request -> 'annotation_ids') AS item(id)
        ) <> pg_catalog.jsonb_array_length(v_rows) THEN
            RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,
                'request',p_request,'data',pg_catalog.jsonb_build_object('missing_scope',true,'annotations','[]'::jsonb));
        END IF;
        RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,
            'request',p_request,'data',pg_catalog.jsonb_build_object('missing_scope',false,'annotations',v_rows));
    ELSIF v_operation = 'find_run' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'project_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'singleflight_key') IS DISTINCT FROM 'string'
           OR p_request ->> 'singleflight_key' !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'Invalid Mask QC singleflight lookup' USING ERRCODE = '22023';
        END IF;
        SELECT run.* INTO v_run FROM public.mask_qc_runs AS run
        WHERE run.project_id = (p_request ->> 'project_id')::uuid
          AND run.singleflight_key = p_request ->> 'singleflight_key'
          AND run.status IN ('pending','running')
        ORDER BY run.created_at DESC LIMIT 1;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_run); ELSE v_data := NULL; END IF;
    ELSIF v_operation = 'create_run' THEN
        FOREACH v_field IN ARRAY ARRAY['run_id','project_id','async_job_id','requested_by_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RAISE EXCEPTION 'Invalid Mask QC run identity' USING ERRCODE = '22023';
            END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'config_revision') IS DISTINCT FROM 'number'
           OR p_request ->> 'config_revision' !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'config_revision')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'config_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'config_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_snapshot_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'source_snapshot_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'singleflight_key') IS DISTINCT FROM 'string'
           OR p_request ->> 'singleflight_key' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'scope_json') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'config_snapshot') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_snapshot') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'task_snapshot_digests') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'summary') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid Mask QC run snapshot' USING ERRCODE = '22023';
        END IF;
        v_actor_id := (p_request ->> 'requested_by_id')::uuid;
        v_lock_reply := aap_identity_v1.lock_active_actor(v_actor_id);
        IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_quality_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','actor_inactive'))::text;
        END IF;
        INSERT INTO public.mask_qc_runs(
            id,project_id,async_job_id,requested_by_id,status,progress_pct,scope_json,
            config_revision,config_digest,config_snapshot,source_snapshot,source_snapshot_digest,
            task_snapshot_digests,singleflight_key,summary
        ) VALUES (
            (p_request ->> 'run_id')::uuid,(p_request ->> 'project_id')::uuid,
            (p_request ->> 'async_job_id')::uuid,v_actor_id,
            'pending',0,p_request -> 'scope_json',(p_request ->> 'config_revision')::integer,
            p_request ->> 'config_digest',p_request -> 'config_snapshot',p_request -> 'source_snapshot',
            p_request ->> 'source_snapshot_digest',p_request -> 'task_snapshot_digests',
            p_request ->> 'singleflight_key',p_request -> 'summary'
        ) ON CONFLICT (project_id,singleflight_key) WHERE status IN ('pending','running') DO NOTHING
        RETURNING public.mask_qc_runs.* INTO v_run;
        v_created := FOUND;
        IF NOT v_created THEN
            SELECT run.* INTO v_run FROM public.mask_qc_runs AS run
            WHERE run.project_id = (p_request ->> 'project_id')::uuid
              AND run.singleflight_key = p_request ->> 'singleflight_key'
              AND run.status IN ('pending','running')
            ORDER BY run.created_at DESC LIMIT 1;
        END IF;
        v_data := pg_catalog.jsonb_build_object('created',v_created,'row',pg_catalog.to_jsonb(v_run));
    ELSIF v_operation IN ('read_run','read_run_by_job') THEN
        v_field := CASE WHEN v_operation='read_run' THEN 'run_id' ELSE 'async_job_id' END;
        IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
           OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION 'Invalid Mask QC run lookup' USING ERRCODE = '22023';
        END IF;
        v_id := (p_request ->> v_field)::uuid;
        IF (p_request ->> 'lock')::boolean THEN
            IF v_operation='read_run' THEN
                SELECT run.* INTO v_run FROM public.mask_qc_runs AS run WHERE run.id=v_id FOR UPDATE;
            ELSE
                SELECT run.* INTO v_run FROM public.mask_qc_runs AS run WHERE run.async_job_id=v_id FOR UPDATE;
            END IF;
        ELSE
            IF v_operation='read_run' THEN
                SELECT run.* INTO v_run FROM public.mask_qc_runs AS run WHERE run.id=v_id;
            ELSE
                SELECT run.* INTO v_run FROM public.mask_qc_runs AS run WHERE run.async_job_id=v_id;
            END IF;
        END IF;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_run); ELSE v_data := NULL; END IF;
    ELSIF v_operation IN ('begin_run','progress_run','complete_run','fail_run','cancel_run','reconcile_run') THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'run_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'run_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION 'Invalid Mask QC run id' USING ERRCODE = '22023';
        END IF;
        v_id := (p_request ->> 'run_id')::uuid;
        CASE v_operation
            WHEN 'begin_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask QC start time' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET status='running', started_at=COALESCE(run.started_at,(p_request ->> 'at')::timestamptz),
                    progress_pct=GREATEST(run.progress_pct,1), updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status='pending' RETURNING run.* INTO v_run;
            WHEN 'progress_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'progress_pct') IS DISTINCT FROM 'number'
                   OR p_request ->> 'progress_pct' !~ '^(0|[1-9][0-9]{0,2})$'
                   OR (p_request ->> 'progress_pct')::integer NOT BETWEEN 0 AND 100 THEN RAISE EXCEPTION 'Invalid Mask QC progress' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET progress_pct=(p_request ->> 'progress_pct')::integer, updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status IN ('pending','running') RETURNING run.* INTO v_run;
            WHEN 'complete_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(p_request -> 'summary') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid Mask QC completion' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET status='completed',progress_pct=100,summary=p_request -> 'summary',
                    completed_at=(p_request ->> 'at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status IN ('pending','running') RETURNING run.* INTO v_run;
            WHEN 'fail_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask QC failure' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET status='failed',error_message=pg_catalog.left(p_request ->> 'error_message',4000),
                    completed_at=(p_request ->> 'at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status NOT IN ('completed','cancelled') RETURNING run.* INTO v_run;
            WHEN 'cancel_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask QC cancellation time' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET status='cancelled',completed_at=(p_request ->> 'at')::timestamptz,
                    updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status IN ('pending','running') RETURNING run.* INTO v_run;
            WHEN 'reconcile_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(p_request -> 'terminal_cancelled') IS DISTINCT FROM 'boolean'
                   OR pg_catalog.jsonb_typeof(p_request -> 'error_message') NOT IN ('string','null') THEN RAISE EXCEPTION 'Invalid Mask QC terminal reconciliation' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET status=CASE WHEN (p_request ->> 'terminal_cancelled')::boolean THEN 'cancelled' ELSE 'failed' END,
                    error_message=CASE WHEN (p_request ->> 'terminal_cancelled')::boolean THEN run.error_message ELSE pg_catalog.left(COALESCE(p_request ->> 'error_message',''),4000) END,
                    completed_at=(p_request ->> 'at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status IN ('pending','running') RETURNING run.* INTO v_run;
        END CASE;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_run); ELSE v_data := NULL; END IF;
    ELSIF v_operation = 'upsert_issue' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'issue') IS DISTINCT FROM 'object'
           OR NOT aap_labeling_v1._quality_exact_keys(p_request -> 'issue',ARRAY[
            'id','run_id','last_seen_run_id','project_id','task_id','annotation_id','annotation_version',
            'related_annotation_ids','source_versions','code','severity','severity_rank','status',
            'frame_start','frame_end','metric','threshold','region_bbox','region_mask_ref','region_digest',
            'dedupe_key','source','suggestion','updated_at'
           ]) THEN RAISE EXCEPTION 'Invalid Mask QC issue fields' USING ERRCODE = '22023'; END IF;
        FOREACH v_field IN ARRAY ARRAY['id','run_id','last_seen_run_id','project_id','task_id','annotation_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request #> ARRAY['issue',v_field]) IS DISTINCT FROM 'string'
               OR p_request #>> ARRAY['issue',v_field] !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC issue identity' USING ERRCODE = '22023'; END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request #> '{issue,source_versions}') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,related_annotation_ids}') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,metric}') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,threshold}') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,source}') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,region_bbox}') NOT IN ('object','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,region_mask_ref}') NOT IN ('object','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,region_digest}') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,suggestion}') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,frame_start}') NOT IN ('number','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,frame_end}') NOT IN ('number','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,annotation_version}') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,severity_rank}') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,code}') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,severity}') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,dedupe_key}') IS DISTINCT FROM 'string'
           OR (p_request #>> '{issue,annotation_version}') !~ '^[1-9][0-9]{0,9}$'
           OR (p_request #>> '{issue,annotation_version}')::bigint > 2147483647
           OR (p_request #>> '{issue,severity_rank}') !~ '^[0-2]$'
           OR ((p_request #>> '{issue,severity}'='blocker') AND (p_request #>> '{issue,severity_rank}')<>'0')
           OR ((p_request #>> '{issue,severity}'='warning') AND (p_request #>> '{issue,severity_rank}')<>'1')
           OR ((p_request #>> '{issue,severity}'='info') AND (p_request #>> '{issue,severity_rank}')<>'2')
           OR (p_request #>> '{issue,frame_start}' IS NOT NULL AND ((p_request #>> '{issue,frame_start}') !~ '^(0|[1-9][0-9]{0,9})$' OR (p_request #>> '{issue,frame_start}')::bigint > 2147483647))
           OR (p_request #>> '{issue,frame_end}' IS NOT NULL AND ((p_request #>> '{issue,frame_end}') !~ '^(0|[1-9][0-9]{0,9})$' OR (p_request #>> '{issue,frame_end}')::bigint > 2147483647))
           OR ((p_request #>> '{issue,frame_start}' IS NULL) <> (p_request #>> '{issue,frame_end}' IS NULL))
           OR (p_request #>> '{issue,frame_start}' IS NOT NULL AND (p_request #>> '{issue,frame_end}')::bigint < (p_request #>> '{issue,frame_start}')::bigint)
           OR p_request #>> '{issue,dedupe_key}' !~ '^[0-9a-f]{64}$'
           OR p_request #>> '{issue,region_digest}' IS NOT NULL AND p_request #>> '{issue,region_digest}' !~ '^[0-9a-f]{64}$'
           OR p_request #>> '{issue,severity}' NOT IN ('blocker','warning','info')
           OR p_request #>> '{issue,status}' IS DISTINCT FROM 'open'
           OR pg_catalog.length(p_request #>> '{issue,code}') NOT BETWEEN 1 AND 64
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,updated_at}') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Mask QC issue value' USING ERRCODE = '22023';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request #> '{issue,related_annotation_ids}') AS item(value)
            WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
               OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ) THEN RAISE EXCEPTION 'Invalid Mask QC related annotation ids' USING ERRCODE = '22023'; END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_each_text(p_request #> '{issue,source_versions}') AS source(id,version)
            WHERE source.id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR source.version !~ '^[1-9][0-9]{0,9}$'
               OR source.version::bigint > 2147483647
        ) OR (
            p_request #> '{issue,region_mask_ref}' IS DISTINCT FROM 'null'::jsonb
            AND (p_request #> '{issue,region_mask_ref}' ->> 'sha256') IS DISTINCT FROM p_request #>> '{issue,region_digest}'
        ) THEN RAISE EXCEPTION 'Invalid Mask QC issue source versions or region reference' USING ERRCODE = '22023'; END IF;
        INSERT INTO public.mask_qc_issues AS current_issue(
            id,run_id,last_seen_run_id,project_id,task_id,annotation_id,annotation_version,
            related_annotation_ids,source_versions,code,severity,severity_rank,status,
            frame_start,frame_end,metric,threshold,region_bbox,region_mask_ref,region_digest,
            dedupe_key,source,suggestion,updated_at
        ) VALUES (
            (p_request #>> '{issue,id}')::uuid,(p_request #>> '{issue,run_id}')::uuid,
            (p_request #>> '{issue,last_seen_run_id}')::uuid,(p_request #>> '{issue,project_id}')::uuid,
            (p_request #>> '{issue,task_id}')::uuid,(p_request #>> '{issue,annotation_id}')::uuid,
            (p_request #>> '{issue,annotation_version}')::integer,
            ARRAY(SELECT item.value #>> '{}' FROM pg_catalog.jsonb_array_elements(p_request #> '{issue,related_annotation_ids}') AS item(value))::uuid[],
            p_request #> '{issue,source_versions}',p_request #>> '{issue,code}',p_request #>> '{issue,severity}',
            (p_request #>> '{issue,severity_rank}')::smallint,'open',
            (p_request #>> '{issue,frame_start}')::integer,(p_request #>> '{issue,frame_end}')::integer,
            p_request #> '{issue,metric}',p_request #> '{issue,threshold}',p_request #> '{issue,region_bbox}',
            p_request #> '{issue,region_mask_ref}',p_request #>> '{issue,region_digest}',p_request #>> '{issue,dedupe_key}',
            p_request #> '{issue,source}',p_request #>> '{issue,suggestion}',(p_request #>> '{issue,updated_at}')::timestamptz
        ) ON CONFLICT ON CONSTRAINT uq_mask_qc_issues_dedupe DO UPDATE SET
            severity=EXCLUDED.severity,severity_rank=EXCLUDED.severity_rank,
            status=CASE WHEN EXCLUDED.status='stale' THEN 'stale'
                WHEN current_issue.status IN ('resolved','wont_fix') THEN current_issue.status
                ELSE 'open' END,
            metric=EXCLUDED.metric,threshold=EXCLUDED.threshold,region_bbox=EXCLUDED.region_bbox,
            region_mask_ref=EXCLUDED.region_mask_ref,region_digest=EXCLUDED.region_digest,
            source=EXCLUDED.source,suggestion=EXCLUDED.suggestion,updated_at=EXCLUDED.updated_at
        RETURNING current_issue.* INTO v_issue;
        v_data := pg_catalog.to_jsonb(v_issue);
    ELSIF v_operation = 'mark_seen' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'project_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'run_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'run_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'dedupe_keys') IS DISTINCT FROM 'array'
           OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements_text(p_request -> 'dedupe_keys') AS item(value) WHERE item.value !~ '^[0-9a-f]{64}$') THEN
            RAISE EXCEPTION 'Invalid Mask QC seen keys' USING ERRCODE = '22023';
        END IF;
        UPDATE public.mask_qc_issues AS issue SET last_seen_run_id=(p_request ->> 'run_id')::uuid
        WHERE issue.project_id=(p_request ->> 'project_id')::uuid
          AND issue.dedupe_key IN (SELECT value FROM pg_catalog.jsonb_array_elements_text(p_request -> 'dedupe_keys'));
        GET DIAGNOSTICS v_count = ROW_COUNT;
        v_data := pg_catalog.jsonb_build_object('affected',v_count);
    ELSIF v_operation = 'read_issues' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'project_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'issue_ids') IS DISTINCT FROM 'array'
           OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'issue_ids') AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
            RAISE EXCEPTION 'Invalid Mask QC issue list' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(issue) ORDER BY issue.id),'[]'::jsonb)
        INTO v_rows FROM public.mask_qc_issues AS issue
        WHERE issue.project_id=(p_request ->> 'project_id')::uuid
          AND issue.id IN (SELECT (item.value #>> '{}')::uuid FROM pg_catalog.jsonb_array_elements(p_request -> 'issue_ids') AS item(value));
        v_data := v_rows;
    ELSIF v_operation = 'list_issues' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'project_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'limit') IS DISTINCT FROM 'number'
           OR p_request ->> 'limit' !~ '^[1-9][0-9]*$'
           OR (p_request ->> 'limit')::integer NOT BETWEEN 1 AND 201
           OR pg_catalog.jsonb_typeof(p_request -> 'status') NOT IN ('string','null')
           OR (p_request ->> 'status' IS NOT NULL AND p_request ->> 'status' NOT IN ('open','resolved','wont_fix','stale'))
           OR pg_catalog.jsonb_typeof(p_request -> 'severity') NOT IN ('string','null')
           OR (p_request ->> 'severity' IS NOT NULL AND p_request ->> 'severity' NOT IN ('blocker','warning','info'))
           OR pg_catalog.jsonb_typeof(p_request -> 'code') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_request -> 'task_id') NOT IN ('string','null')
           OR (p_request ->> 'task_id' IS NOT NULL AND p_request ->> 'task_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_request -> 'annotation_id') NOT IN ('string','null')
           OR (p_request ->> 'annotation_id' IS NOT NULL AND p_request ->> 'annotation_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_request -> 'frame') NOT IN ('number','null')
           OR (p_request ->> 'frame' IS NOT NULL AND (p_request ->> 'frame' !~ '^(0|[1-9][0-9]{0,9})$' OR (p_request ->> 'frame')::bigint > 2147483647))
           OR pg_catalog.jsonb_typeof(p_request -> 'allowed_task_ids') NOT IN ('array','null')
           OR pg_catalog.jsonb_typeof(p_request -> 'cursor_rank') NOT IN ('number','null')
           OR pg_catalog.jsonb_typeof(p_request -> 'cursor_created_at') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_request -> 'cursor_id') NOT IN ('string','null')
           OR (p_request ->> 'cursor_rank' IS NOT NULL AND p_request ->> 'cursor_rank' !~ '^[0-2]$')
           OR (p_request ->> 'cursor_id' IS NOT NULL AND p_request ->> 'cursor_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR ((p_request ->> 'cursor_rank' IS NULL) <> (p_request ->> 'cursor_created_at' IS NULL))
           OR ((p_request ->> 'cursor_rank' IS NULL) <> (p_request ->> 'cursor_id' IS NULL))
           OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(CASE WHEN pg_catalog.jsonb_typeof(p_request -> 'allowed_task_ids')='array' THEN p_request -> 'allowed_task_ids' ELSE '[]'::jsonb END) AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
            RAISE EXCEPTION 'Invalid Mask QC issue listing request' USING ERRCODE = '22023';
        END IF;
        WITH current_issue AS (
            SELECT issue.*,
                CASE WHEN issue.status='stale' OR EXISTS (
                    SELECT 1 FROM pg_catalog.jsonb_each_text(COALESCE(issue.source_versions,pg_catalog.jsonb_build_object(issue.annotation_id::text,issue.annotation_version))) AS source(id,version)
                    LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
                    WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                       OR annotation.version <> source.version::integer
                ) THEN 'stale' ELSE issue.status END AS effective_status
            FROM public.mask_qc_issues AS issue
            WHERE issue.project_id=(p_request ->> 'project_id')::uuid
              AND (p_request ->> 'severity' IS NULL OR issue.severity=p_request ->> 'severity')
              AND (p_request ->> 'code' IS NULL OR issue.code=p_request ->> 'code')
              AND (p_request ->> 'task_id' IS NULL OR issue.task_id=(p_request ->> 'task_id')::uuid)
              AND (p_request ->> 'annotation_id' IS NULL OR issue.annotation_id=(p_request ->> 'annotation_id')::uuid)
              AND (p_request ->> 'frame' IS NULL OR (issue.frame_start <= (p_request ->> 'frame')::integer AND issue.frame_end >= (p_request ->> 'frame')::integer))
              AND (p_request -> 'allowed_task_ids'='null'::jsonb OR issue.task_id IN (
                  SELECT (item.value #>> '{}')::uuid FROM pg_catalog.jsonb_array_elements(p_request -> 'allowed_task_ids') AS item(value)))
        )
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(row_data) ORDER BY row_data.severity_rank,row_data.created_at DESC,row_data.id DESC),'[]'::jsonb)
        INTO v_rows FROM (
            SELECT current_issue.* FROM current_issue
            WHERE (p_request ->> 'status' IS NULL
                   OR (p_request ->> 'status'='stale' AND current_issue.effective_status='stale')
                   OR (p_request ->> 'status'<>'stale' AND current_issue.effective_status=p_request ->> 'status'))
              AND (p_request ->> 'cursor_rank' IS NULL
                   OR current_issue.severity_rank > (p_request ->> 'cursor_rank')::integer
                   OR (current_issue.severity_rank=(p_request ->> 'cursor_rank')::integer AND current_issue.created_at < (p_request ->> 'cursor_created_at')::timestamptz)
                   OR (current_issue.severity_rank=(p_request ->> 'cursor_rank')::integer AND current_issue.created_at=(p_request ->> 'cursor_created_at')::timestamptz AND current_issue.id<(p_request ->> 'cursor_id')::uuid))
            ORDER BY current_issue.severity_rank,current_issue.created_at DESC,current_issue.id DESC
            LIMIT (p_request ->> 'limit')::integer
        ) AS row_data;
        v_data := v_rows;
    ELSIF v_operation = 'read_task_run_issues' THEN
        FOREACH v_field IN ARRAY ARRAY['task_id','run_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC task run lookup' USING ERRCODE = '22023'; END IF;
        END LOOP;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(issue) ORDER BY issue.id),'[]'::jsonb)
        INTO v_rows FROM public.mask_qc_issues AS issue
        WHERE issue.task_id=(p_request ->> 'task_id')::uuid
          AND issue.last_seen_run_id=(p_request ->> 'run_id')::uuid;
        v_data := v_rows;
    ELSIF v_operation = 'find_latest_task_run' THEN
        FOREACH v_field IN ARRAY ARRAY['project_id','task_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC task run lookup' USING ERRCODE = '22023'; END IF;
        END LOOP;
        SELECT run.* INTO v_run FROM public.mask_qc_runs AS run
        WHERE run.project_id=(p_request ->> 'project_id')::uuid
          AND run.task_snapshot_digests ? (p_request ->> 'task_id')
        ORDER BY run.created_at DESC,run.id DESC LIMIT 1;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_run); ELSE v_data := NULL; END IF;
    ELSIF v_operation = 'find_current_completed_task_run' THEN
        FOREACH v_field IN ARRAY ARRAY['project_id','task_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC task run lookup' USING ERRCODE = '22023'; END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'config_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'config_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'source_digest' !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'Invalid Mask QC task run digest' USING ERRCODE = '22023'; END IF;
        SELECT run.* INTO v_run FROM public.mask_qc_runs AS run
        WHERE run.project_id=(p_request ->> 'project_id')::uuid
          AND run.status='completed'
          AND run.config_digest=p_request ->> 'config_digest'
          AND run.task_snapshot_digests ->> (p_request ->> 'task_id')=p_request ->> 'source_digest'
        ORDER BY run.completed_at DESC,run.id DESC LIMIT 1;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_run); ELSE v_data := NULL; END IF;
    ELSIF v_operation = 'read_open_task_issues' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'task_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'severity') NOT IN ('string','null')
           OR (p_request ->> 'severity' IS NOT NULL AND p_request ->> 'severity' NOT IN ('blocker','warning','info'))
           OR pg_catalog.jsonb_typeof(p_request -> 'issue_ids') NOT IN ('array','null')
           OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(CASE WHEN pg_catalog.jsonb_typeof(p_request -> 'issue_ids')='array' THEN p_request -> 'issue_ids' ELSE '[]'::jsonb END) AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN RAISE EXCEPTION 'Invalid Mask QC open issue query' USING ERRCODE = '22023'; END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(issue) ORDER BY issue.id),'[]'::jsonb)
        INTO v_rows FROM public.mask_qc_issues AS issue
        WHERE issue.task_id=(p_request ->> 'task_id')::uuid
          AND issue.status='open'
          AND (p_request ->> 'severity' IS NULL OR issue.severity=p_request ->> 'severity')
          AND (p_request -> 'issue_ids'='null'::jsonb OR issue.id IN (
              SELECT (item.value #>> '{}')::uuid FROM pg_catalog.jsonb_array_elements(p_request -> 'issue_ids') AS item(value)))
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.jsonb_each_text(COALESCE(issue.source_versions,pg_catalog.jsonb_build_object(issue.annotation_id::text,issue.annotation_version))) AS source(id,version)
              LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
              WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                 OR annotation.version <> source.version::integer
          );
        v_data := v_rows;
    ELSIF v_operation IN ('read_issue','decide_issue','resolve_repaired_issue') THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'issue_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'issue_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC issue id' USING ERRCODE = '22023'; END IF;
        v_id := (p_request ->> 'issue_id')::uuid;
        IF v_operation = 'read_issue' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean'
               OR pg_catalog.jsonb_typeof(p_request -> 'nowait') IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'Invalid Mask QC issue lock' USING ERRCODE = '22023'; END IF;
            IF (p_request ->> 'lock')::boolean THEN
                IF (p_request ->> 'nowait')::boolean THEN
                    SELECT issue.* INTO v_issue FROM public.mask_qc_issues AS issue WHERE issue.id=v_id FOR UPDATE NOWAIT;
                ELSE
                    SELECT issue.* INTO v_issue FROM public.mask_qc_issues AS issue WHERE issue.id=v_id FOR UPDATE;
                END IF;
            ELSE
                SELECT issue.* INTO v_issue FROM public.mask_qc_issues AS issue WHERE issue.id=v_id;
            END IF;
            IF FOUND THEN v_data := pg_catalog.to_jsonb(v_issue); ELSE v_data := NULL; END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'actor_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'decided_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask QC decision actor or time' USING ERRCODE = '22023'; END IF;
            v_actor_id := (p_request ->> 'actor_id')::uuid;
            v_lock_reply := aap_identity_v1.lock_active_actor(v_actor_id);
            IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,'request',p_request,
                    'data',pg_catalog.jsonb_build_object('changed',false,'reason','actor_inactive'));
            END IF;
            SELECT issue.* INTO v_issue FROM public.mask_qc_issues AS issue WHERE issue.id=v_id FOR UPDATE NOWAIT;
            IF NOT FOUND THEN v_data := pg_catalog.jsonb_build_object('changed',false,'reason','issue_missing');
            ELSE
                IF v_operation='decide_issue' THEN
                    IF pg_catalog.jsonb_typeof(p_request -> 'expected_task_id') IS DISTINCT FROM 'string'
                       OR p_request ->> 'expected_task_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                       OR pg_catalog.jsonb_typeof(p_request -> 'status') IS DISTINCT FROM 'string'
                       OR p_request ->> 'status' NOT IN ('open','resolved','wont_fix') THEN RAISE EXCEPTION 'Invalid Mask QC decision' USING ERRCODE = '22023'; END IF;
                    v_current := v_issue.task_id=(p_request ->> 'expected_task_id')::uuid
                        AND NOT EXISTS (
                            SELECT 1 FROM pg_catalog.jsonb_each_text(COALESCE(v_issue.source_versions,pg_catalog.jsonb_build_object(v_issue.annotation_id::text,v_issue.annotation_version))) AS source(id,version)
                            LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
                            WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                               OR annotation.version <> source.version::integer
                        );
                    IF NOT v_current THEN v_data := pg_catalog.jsonb_build_object('changed',false,'reason','issue_stale');
                    ELSE
                        UPDATE public.mask_qc_issues AS issue SET status=p_request ->> 'status',
                            resolved_by_id=CASE WHEN p_request ->> 'status'='open' THEN NULL ELSE v_actor_id END,
                            resolved_at=CASE WHEN p_request ->> 'status'='open' THEN NULL ELSE (p_request ->> 'decided_at')::timestamptz END,
                            updated_at=pg_catalog.transaction_timestamp()
                        WHERE issue.id=v_id RETURNING issue.* INTO v_issue;
                        v_data := pg_catalog.jsonb_build_object('changed',true,'row',pg_catalog.to_jsonb(v_issue));
                    END IF;
                ELSE
                    UPDATE public.mask_qc_issues AS issue SET status='resolved',resolved_by_id=v_actor_id,
                        resolved_at=(p_request ->> 'decided_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                    WHERE issue.id=v_id RETURNING issue.* INTO v_issue;
                    v_data := pg_catalog.jsonb_build_object('changed',FOUND,'row',CASE WHEN FOUND THEN pg_catalog.to_jsonb(v_issue) ELSE NULL END);
                END IF;
            END IF;
        END IF;
    ELSIF v_operation = 'read_issue_statuses' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'issue_ids') IS DISTINCT FROM 'array'
           OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'issue_ids') AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string' OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
            RAISE EXCEPTION 'Invalid Mask QC issue ids' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(pg_catalog.jsonb_object_agg(issue.id::text,
            CASE WHEN issue.status='stale' OR EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_each_text(COALESCE(issue.source_versions,pg_catalog.jsonb_build_object(issue.annotation_id::text,issue.annotation_version))) AS source(id,version)
                LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
                WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                   OR annotation.version <> source.version::integer
            ) THEN 'stale' ELSE issue.status END),'{}'::jsonb)
        INTO v_statuses FROM public.mask_qc_issues AS issue
        WHERE issue.id IN (SELECT (item.value #>> '{}')::uuid FROM pg_catalog.jsonb_array_elements(p_request -> 'issue_ids') AS item(value));
        v_data := v_statuses;
    ELSIF v_operation = 'issue_versions_current' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'issue_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'issue_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC issue id' USING ERRCODE = '22023'; END IF;
        SELECT EXISTS (
            SELECT 1 FROM public.mask_qc_issues AS issue
            WHERE issue.id=(p_request ->> 'issue_id')::uuid
              AND (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(COALESCE(issue.source_versions,'{}'::jsonb)) AS object_keys(key)) > 0
              AND NOT EXISTS (
                  SELECT 1 FROM pg_catalog.jsonb_each_text(issue.source_versions) AS source(id,version)
                  LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
                  WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                     OR annotation.version <> source.version::integer
              )
        ) INTO v_current;
        v_data := pg_catalog.to_jsonb(v_current);
    ELSIF v_operation = 'read_annotation_revision' THEN
        FOREACH v_field IN ARRAY ARRAY['task_id','annotation_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask revision identity' USING ERRCODE = '22023'; END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'annotation_version') IS DISTINCT FROM 'number'
           OR p_request ->> 'annotation_version' !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'annotation_version')::bigint > 2147483647 THEN RAISE EXCEPTION 'Invalid Mask revision version' USING ERRCODE = '22023'; END IF;
        SELECT pg_catalog.jsonb_build_object(
            'geometry',revision.geometry,'source_kind',revision.source_kind,
            'expires_at',revision.expires_at
        )
        INTO v_data FROM public.mask_annotation_revisions AS revision
        WHERE revision.task_id=(p_request ->> 'task_id')::uuid
          AND revision.annotation_id=(p_request ->> 'annotation_id')::uuid
          AND revision.annotation_version=(p_request ->> 'annotation_version')::integer;
    ELSIF v_operation = 'read_tracker_candidate_context' THEN
        FOREACH v_field IN ARRAY ARRAY['task_id','job_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid tracker candidate context identity' USING ERRCODE = '22023'; END IF;
        END LOOP;
        SELECT job.id,job.task_id,job.revision,job.staged_result,job.annotation_id,job.prompt
        INTO v_tracker_job FROM public.video_tracker_jobs AS job
        WHERE job.id=(p_request ->> 'job_id')::uuid
          AND job.task_id=(p_request ->> 'task_id')::uuid;
        IF FOUND THEN
            v_data := pg_catalog.jsonb_build_object(
                'id',v_tracker_job.id,'task_id',v_tracker_job.task_id,
                'revision',v_tracker_job.revision,'staged_result',v_tracker_job.staged_result,
                'annotation_id',v_tracker_job.annotation_id,'prompt',v_tracker_job.prompt
            );
        ELSE v_data := NULL; END IF;
    ELSIF v_operation = 'read_run_summary' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'run_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'run_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC run id' USING ERRCODE = '22023'; END IF;
        SELECT pg_catalog.jsonb_build_object(
            'issue_count',COALESCE(pg_catalog.sum(summary.count),0),
            'issues_by_severity',pg_catalog.jsonb_build_object(
                'blocker',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.severity='blocker'),0),
                'warning',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.severity='warning'),0),
                'info',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.severity='info'),0)),
            'issues_by_status',pg_catalog.jsonb_build_object(
                'open',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.effective_status='open'),0),
                'resolved',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.effective_status='resolved'),0),
                'wont_fix',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.effective_status='wont_fix'),0),
                'stale',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.effective_status='stale'),0)))
        INTO v_counts FROM (
            SELECT issue.severity,
                CASE WHEN issue.status='stale' OR EXISTS (
                    SELECT 1 FROM pg_catalog.jsonb_each_text(COALESCE(issue.source_versions,pg_catalog.jsonb_build_object(issue.annotation_id::text,issue.annotation_version))) AS source(id,version)
                    LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
                    WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                       OR annotation.version <> source.version::integer
                ) THEN 'stale' ELSE issue.status END AS effective_status,
                pg_catalog.count(*)::integer AS count
            FROM public.mask_qc_issues AS issue
            WHERE issue.last_seen_run_id=(p_request ->> 'run_id')::uuid
            GROUP BY issue.severity,effective_status
        ) AS summary;
        SELECT run.* INTO v_run FROM public.mask_qc_runs AS run WHERE run.id=(p_request ->> 'run_id')::uuid;
        IF FOUND THEN
            v_data := COALESCE(v_run.summary,'{}'::jsonb) || v_counts;
        ELSE
            v_data := NULL;
        END IF;
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,'request',p_request,'data',v_data);
EXCEPTION
    WHEN invalid_text_representation OR invalid_datetime_format OR datetime_field_overflow OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'Invalid Mask QC request value' USING ERRCODE = '22023';
END;
$function$;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.mask_repair_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_field text;
    v_id uuid;
    v_batch public.mask_repair_batches%ROWTYPE;
    v_scope public.mask_review_scopes%ROWTYPE;
    v_segment public.video_segments%ROWTYPE;
    v_data jsonb;
    v_rows jsonb := '[]'::jsonb;
    v_actor_id uuid;
    v_lock_reply jsonb;
    v_count integer := 0;
    v_task public.tasks%ROWTYPE;
    v_annotation public.annotations%ROWTYPE;
    v_issue public.mask_qc_issues%ROWTYPE;
    v_tracker_job public.video_tracker_jobs%ROWTYPE;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-quality-data/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Mask repair request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'create_batch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','project_id','requested_by_id','token_hash','plan_digest','request_json','plan_json','result_json','receipt_expires_at'];
        WHEN 'read_batch_by_id' THEN v_expected_keys := ARRAY['schema','operation','batch_id','lock'];
        WHEN 'read_batch_by_token' THEN v_expected_keys := ARRAY['schema','operation','token_hash','lock'];
        WHEN 'read_batch_by_job' THEN v_expected_keys := ARRAY['schema','operation','job_id','terminal_kind','lock'];
        WHEN 'attach_job' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id'];
        WHEN 'attach_rollback_job' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id'];
        WHEN 'resume_batch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','actor_id'];
        WHEN 'begin_execution' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','at'];
        WHEN 'begin_rollback' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id'];
        WHEN 'record_shard' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','shard_id','result_value','rollback_expires_at'];
        WHEN 'record_candidate' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','item_index','result_value'];
        WHEN 'record_shard_failure' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','shard_id','task_id','error_message'];
        WHEN 'finish_execution' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','status','completed_at'];
        WHEN 'finish_rollback' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id','rolled_back_at','result_json'];
        WHEN 'fail_rollback' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id','error_message','result_json','failed_at'];
        WHEN 'fail_dispatch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','error_message'];
        WHEN 'fail_rollback_dispatch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','error_message'];
        WHEN 'cancel_pending' THEN v_expected_keys := ARRAY['schema','operation','batch_id','status','completed_at'];
        WHEN 'reconcile_terminal' THEN v_expected_keys := ARRAY['schema','operation','job_id','terminal_kind','terminal_cancelled','status','completed_at'];
        WHEN 'insert_review_scope' THEN v_expected_keys := ARRAY['schema','operation','scope_id','project_id','task_id','annotation_id','qc_issue_id','source_job_id','expected_segment_id','reviewer_id','source_annotation_version','result_annotation_version','source_job_revision','frame_start','frame_end','region_mask_ref','region_digest','candidate_digest','decision'];
        WHEN 'read_review_scopes' THEN v_expected_keys := ARRAY['schema','operation','annotation_id','result_annotation_version','frame_start','frame_end','lock'];
        WHEN 'read_repair_targets' THEN v_expected_keys := ARRAY['schema','operation','issue_id','include_related'];
        WHEN 'read_video_segment' THEN v_expected_keys := ARRAY['schema','operation','dataset_item_id','frame_index','requested_id'];
        ELSE RAISE EXCEPTION 'Invalid Mask repair operation' USING ERRCODE = '22023';
    END CASE;
    IF NOT aap_labeling_v1._quality_exact_keys(p_request, v_expected_keys) THEN
        RAISE EXCEPTION 'Invalid Mask repair request fields' USING ERRCODE = '22023';
    END IF;

    IF v_operation = 'create_batch' THEN
        FOREACH v_field IN ARRAY ARRAY['batch_id','project_id','requested_by_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RAISE EXCEPTION 'Invalid Mask repair identity' USING ERRCODE = '22023';
            END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'token_hash') IS DISTINCT FROM 'string'
           OR p_request ->> 'token_hash' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'plan_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'plan_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'request_json') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'plan_json') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'result_json') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'receipt_expires_at') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Mask repair plan' USING ERRCODE = '22023';
        END IF;
        v_actor_id := (p_request ->> 'requested_by_id')::uuid;
        v_lock_reply := aap_identity_v1.lock_active_actor(v_actor_id);
        IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,
                'request',p_request,'data',pg_catalog.jsonb_build_object('row',NULL,'reason','actor_inactive'));
        END IF;
        INSERT INTO public.mask_repair_batches(
            id,project_id,requested_by_id,token_hash,status,plan_digest,request_json,plan_json,result_json,receipt_expires_at
        ) VALUES (
            (p_request ->> 'batch_id')::uuid,(p_request ->> 'project_id')::uuid,v_actor_id,
            p_request ->> 'token_hash','planned',p_request ->> 'plan_digest',p_request -> 'request_json',
            p_request -> 'plan_json',p_request -> 'result_json',(p_request ->> 'receipt_expires_at')::timestamptz
        ) RETURNING public.mask_repair_batches.* INTO v_batch;
        v_data := pg_catalog.to_jsonb(v_batch);
    ELSIF v_operation IN ('read_batch_by_id','read_batch_by_token','read_batch_by_job') THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'Invalid Mask repair lock' USING ERRCODE = '22023'; END IF;
        IF v_operation='read_batch_by_id' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'batch_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair batch id' USING ERRCODE = '22023'; END IF;
            IF (p_request ->> 'lock')::boolean THEN
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.id=(p_request ->> 'batch_id')::uuid FOR UPDATE;
            ELSE
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.id=(p_request ->> 'batch_id')::uuid;
            END IF;
        ELSIF v_operation='read_batch_by_token' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'token_hash') IS DISTINCT FROM 'string'
               OR p_request ->> 'token_hash' !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'Invalid Mask repair receipt' USING ERRCODE = '22023'; END IF;
            IF (p_request ->> 'lock')::boolean THEN
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.token_hash=p_request ->> 'token_hash' FOR UPDATE;
            ELSE
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.token_hash=p_request ->> 'token_hash';
            END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(p_request -> 'job_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'terminal_kind') IS DISTINCT FROM 'string'
               OR p_request ->> 'terminal_kind' NOT IN ('mask_repair','mask_repair_rollback') THEN RAISE EXCEPTION 'Invalid Mask repair job lookup' USING ERRCODE = '22023'; END IF;
            IF (p_request ->> 'lock')::boolean THEN
                IF p_request ->> 'terminal_kind'='mask_repair_rollback' THEN
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.rollback_async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
                ELSE
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
                END IF;
            ELSE
                IF p_request ->> 'terminal_kind'='mask_repair_rollback' THEN
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.rollback_async_job_id=(p_request ->> 'job_id')::uuid;
                ELSE
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.async_job_id=(p_request ->> 'job_id')::uuid;
                END IF;
            END IF;
        END IF;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); ELSE v_data := NULL; END IF;
    ELSIF v_operation IN ('attach_job','attach_rollback_job','resume_batch','begin_execution','begin_rollback',
        'record_shard','record_candidate','record_shard_failure','finish_execution','finish_rollback',
        'fail_rollback','fail_dispatch','fail_rollback_dispatch','cancel_pending','reconcile_terminal') THEN
        IF v_operation='reconcile_terminal' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'job_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'terminal_kind') IS DISTINCT FROM 'string'
               OR p_request ->> 'terminal_kind' NOT IN ('mask_repair','mask_repair_rollback')
               OR pg_catalog.jsonb_typeof(p_request -> 'terminal_cancelled') IS DISTINCT FROM 'boolean'
               OR pg_catalog.jsonb_typeof(p_request -> 'status') IS DISTINCT FROM 'string'
               OR p_request ->> 'status' NOT IN ('cancelled','partial','failed','rollback_failed')
               OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair terminal state' USING ERRCODE = '22023'; END IF;
            IF p_request ->> 'terminal_kind'='mask_repair_rollback' AND (p_request ->> 'terminal_cancelled')::boolean THEN
                v_data := NULL;
            ELSE
                IF p_request ->> 'terminal_kind'='mask_repair_rollback' THEN
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.rollback_async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
                ELSE
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
                END IF;
                IF FOUND THEN
                    UPDATE public.mask_repair_batches AS batch SET status=p_request ->> 'status',completed_at=(p_request ->> 'completed_at')::timestamptz,
                        updated_at=pg_catalog.transaction_timestamp() WHERE batch.id=v_batch.id RETURNING batch.* INTO v_batch;
                    v_data := pg_catalog.to_jsonb(v_batch);
                ELSE v_data := NULL; END IF;
            END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'batch_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair batch id' USING ERRCODE = '22023'; END IF;
            v_id := (p_request ->> 'batch_id')::uuid;
            SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.id=v_id FOR UPDATE;
            IF NOT FOUND THEN v_data := NULL;
            ELSE
                CASE v_operation
                    WHEN 'attach_job' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair job id' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET async_job_id=(p_request ->> 'async_job_id')::uuid,status='pending',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.status='planned' RETURNING batch.* INTO v_batch;
                    WHEN 'attach_rollback_job' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair rollback job id' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET rollback_async_job_id=(p_request ->> 'rollback_job_id')::uuid,status='rolling_back',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.status IN ('completed','partial') RETURNING batch.* INTO v_batch;
                    WHEN 'resume_batch' THEN
                        FOREACH v_field IN ARRAY ARRAY['async_job_id','actor_id'] LOOP
                            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
                               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair resume identity' USING ERRCODE = '22023'; END IF;
                        END LOOP;
                        v_actor_id := (p_request ->> 'actor_id')::uuid;
                        v_lock_reply := aap_identity_v1.lock_active_actor(v_actor_id);
                        IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN v_data := pg_catalog.jsonb_build_object('changed',false,'reason','actor_inactive');
                        ELSE
                            UPDATE public.mask_repair_batches AS batch SET async_job_id=(p_request ->> 'async_job_id')::uuid,requested_by_id=v_actor_id,status='pending',updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id AND batch.status IN ('failed','partial') RETURNING batch.* INTO v_batch;
                            IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); ELSE v_data := NULL; END IF;
                        END IF;
                    WHEN 'begin_execution' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair execution binding' USING ERRCODE = '22023'; END IF;
                        PERFORM (p_request ->> 'at')::timestamptz;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid AND v_batch.status IN ('pending','running') THEN
                            UPDATE public.mask_repair_batches AS batch SET status='running',updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        END IF;
                    WHEN 'begin_rollback' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair rollback binding' USING ERRCODE = '22023'; END IF;
                        IF v_batch.rollback_async_job_id IS DISTINCT FROM (p_request ->> 'rollback_job_id')::uuid THEN
                            v_data := pg_catalog.jsonb_build_object('changed',false);
                        END IF;
                    WHEN 'record_shard' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'shard_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'shard_id' !~ '^[0-9a-f]{24}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value') IS DISTINCT FROM 'object'
                           OR pg_catalog.jsonb_typeof(p_request -> 'rollback_expires_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair shard result' USING ERRCODE = '22023'; END IF;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid THEN
                            UPDATE public.mask_repair_batches AS batch SET
                                result_json=COALESCE(batch.result_json,'{}'::jsonb) || pg_catalog.jsonb_build_object('shards',COALESCE(batch.result_json -> 'shards','{}'::jsonb) || pg_catalog.jsonb_build_object(p_request ->> 'shard_id',p_request -> 'result_value')),
                                rollback_expires_at=(p_request ->> 'rollback_expires_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                    WHEN 'record_candidate' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'item_index') IS DISTINCT FROM 'number'
                           OR p_request ->> 'item_index' !~ '^(0|[1-9][0-9]*)$'
                           OR (p_request ->> 'item_index')::bigint > 2147483647
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid Mask repair candidate' USING ERRCODE = '22023'; END IF;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid THEN
                            UPDATE public.mask_repair_batches AS batch SET
                                result_json=COALESCE(batch.result_json,'{}'::jsonb) || pg_catalog.jsonb_build_object('candidates',COALESCE(batch.result_json -> 'candidates','{}'::jsonb) || pg_catalog.jsonb_build_object((p_request ->> 'item_index'),p_request -> 'result_value')),
                                updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                    WHEN 'record_shard_failure' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'task_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'shard_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'shard_id' !~ '^[0-9a-f]{24}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair shard failure' USING ERRCODE = '22023'; END IF;
                        IF pg_catalog.length(p_request ->> 'error_message') > 500 THEN RAISE EXCEPTION 'Invalid Mask repair shard failure length' USING ERRCODE = '22023'; END IF;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid THEN
                            UPDATE public.mask_repair_batches AS batch SET
                                result_json=COALESCE(batch.result_json,'{}'::jsonb) || pg_catalog.jsonb_build_object('shards',COALESCE(batch.result_json -> 'shards','{}'::jsonb) || pg_catalog.jsonb_build_object(p_request ->> 'shard_id',pg_catalog.jsonb_build_object('status','failed','task_id',p_request ->> 'task_id','reason',pg_catalog.left(p_request ->> 'error_message',500)))),
                                updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                    WHEN 'finish_execution' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR p_request ->> 'status' NOT IN ('completed','partial','failed')
                           OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair completion' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status=p_request ->> 'status',completed_at=(p_request ->> 'completed_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.async_job_id=(p_request ->> 'async_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'finish_rollback' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'rolled_back_at') IS DISTINCT FROM 'string'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_json') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid Mask repair rollback completion' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status='rolled_back',rolled_back_at=(p_request ->> 'rolled_back_at')::timestamptz,
                            result_json=p_request -> 'result_json',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.rollback_async_job_id=(p_request ->> 'rollback_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'fail_rollback' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_json') IS DISTINCT FROM 'object'
                           OR pg_catalog.jsonb_typeof(p_request -> 'failed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair rollback failure' USING ERRCODE = '22023'; END IF;
                        PERFORM (p_request ->> 'failed_at')::timestamptz;
                        UPDATE public.mask_repair_batches AS batch SET status='rollback_failed',
                            result_json=p_request -> 'result_json',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.rollback_async_job_id=(p_request ->> 'rollback_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'fail_dispatch' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair dispatch failure' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status='failed',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.async_job_id=(p_request ->> 'async_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'fail_rollback_dispatch' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair rollback dispatch failure' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status='rollback_failed',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.rollback_async_job_id=(p_request ->> 'async_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'cancel_pending' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'status') IS DISTINCT FROM 'string'
                           OR p_request ->> 'status' NOT IN ('cancelled','partial')
                           OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair cancellation' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status=p_request ->> 'status',completed_at=(p_request ->> 'completed_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.status IN ('pending','running') RETURNING batch.* INTO v_batch;
                END CASE;
                IF v_data IS NULL OR pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object' THEN
                    IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); END IF;
                END IF;
                IF v_data IS NULL OR pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object' THEN
                    IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                END IF;
            END IF;
        END IF;
    ELSIF v_operation = 'insert_review_scope' THEN
        FOREACH v_field IN ARRAY ARRAY['scope_id','project_id','task_id','annotation_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask review scope identity' USING ERRCODE = '22023'; END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'qc_issue_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'qc_issue_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_job_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'source_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'expected_segment_id') NOT IN ('string','null')
           OR (p_request ->> 'expected_segment_id' IS NOT NULL AND p_request ->> 'expected_segment_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_request -> 'reviewer_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'reviewer_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'region_mask_ref') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_annotation_version') IS DISTINCT FROM 'number'
           OR (p_request ->> 'source_annotation_version') !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'source_annotation_version')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'result_annotation_version') IS DISTINCT FROM 'number'
           OR (p_request ->> 'result_annotation_version') !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'result_annotation_version')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'source_job_revision') IS DISTINCT FROM 'number'
           OR (p_request ->> 'source_job_revision') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'source_job_revision')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_start') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_start') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_start')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_end') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_end') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_end')::bigint > 2147483647
           OR (p_request ->> 'frame_end')::bigint < (p_request ->> 'frame_start')::bigint
           OR pg_catalog.jsonb_typeof(p_request -> 'region_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'region_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'candidate_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'candidate_digest' !~ '^sha256:[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'decision') IS DISTINCT FROM 'string'
           OR p_request ->> 'decision' NOT IN ('accept','reject') THEN RAISE EXCEPTION 'Invalid Mask review scope facts' USING ERRCODE = '22023'; END IF;
        v_actor_id := (p_request ->> 'reviewer_id')::uuid;
        v_lock_reply := aap_identity_v1.lock_active_actor(v_actor_id);
        IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_data := pg_catalog.jsonb_build_object('created',false,'reason','actor_inactive');
        ELSE
            SELECT task.id,task.project_id INTO v_task
            FROM public.tasks AS task WHERE task.id=(p_request ->> 'task_id')::uuid FOR UPDATE NOWAIT;
            SELECT annotation.id,annotation.project_id,annotation.task_id,annotation.version,annotation.is_active,annotation.was_cancelled
            INTO v_annotation FROM public.annotations AS annotation
            WHERE annotation.id=(p_request ->> 'annotation_id')::uuid FOR KEY SHARE;
            SELECT issue.id,issue.project_id,issue.task_id,issue.annotation_id,issue.region_digest,issue.region_mask_ref
            INTO v_issue FROM public.mask_qc_issues AS issue
            WHERE issue.id=(p_request ->> 'qc_issue_id')::uuid FOR KEY SHARE;
            SELECT job.id,job.task_id,job.revision,job.segment_id,job.from_frame,job.to_frame
            INTO v_tracker_job FROM public.video_tracker_jobs AS job
            WHERE job.id=(p_request ->> 'source_job_id')::uuid FOR UPDATE;
            IF v_task.id IS NULL OR v_annotation.id IS NULL OR v_issue.id IS NULL OR v_tracker_job.id IS NULL
               OR v_task.project_id IS DISTINCT FROM (p_request ->> 'project_id')::uuid
               OR v_annotation.project_id IS DISTINCT FROM (p_request ->> 'project_id')::uuid
               OR v_annotation.task_id IS DISTINCT FROM (p_request ->> 'task_id')::uuid
               OR v_annotation.version IS DISTINCT FROM (p_request ->> 'result_annotation_version')::integer
               OR v_annotation.is_active IS NOT TRUE OR v_annotation.was_cancelled IS TRUE
               OR v_issue.project_id IS DISTINCT FROM (p_request ->> 'project_id')::uuid
               OR v_issue.task_id IS DISTINCT FROM (p_request ->> 'task_id')::uuid
               OR v_issue.annotation_id IS DISTINCT FROM (p_request ->> 'annotation_id')::uuid
               OR v_issue.status IS DISTINCT FROM 'open'
               OR v_issue.annotation_version IS DISTINCT FROM (p_request ->> 'source_annotation_version')::integer
               OR v_issue.source_versions ->> v_annotation.id::text IS DISTINCT FROM p_request ->> 'source_annotation_version'
               OR v_issue.region_digest IS DISTINCT FROM p_request ->> 'region_digest'
               OR v_issue.region_mask_ref IS DISTINCT FROM p_request -> 'region_mask_ref'
               OR (p_request -> 'region_mask_ref' ->> 'sha256') IS DISTINCT FROM p_request ->> 'region_digest'
               OR v_tracker_job.task_id IS DISTINCT FROM (p_request ->> 'task_id')::uuid
               OR v_tracker_job.status NOT IN ('partially_reviewed','accepted','discarded','cancelled')
               OR v_tracker_job.revision::bigint IS DISTINCT FROM (p_request ->> 'source_job_revision')::bigint + 1
               OR v_tracker_job.segment_id IS DISTINCT FROM (p_request ->> 'expected_segment_id')::uuid
               OR (p_request ->> 'frame_start')::integer < v_tracker_job.from_frame
               OR (p_request ->> 'frame_end')::integer > v_tracker_job.to_frame THEN
                v_data := pg_catalog.jsonb_build_object('created',false,'reason','review_scope_changed');
            ELSE
        INSERT INTO public.mask_review_scopes(
            id,project_id,task_id,annotation_id,qc_issue_id,source_job_id,reviewer_id,
            source_annotation_version,result_annotation_version,source_job_revision,frame_start,frame_end,
            region_mask_ref,region_digest,candidate_digest,decision
        ) VALUES (
            (p_request ->> 'scope_id')::uuid,(p_request ->> 'project_id')::uuid,(p_request ->> 'task_id')::uuid,
            (p_request ->> 'annotation_id')::uuid,(p_request ->> 'qc_issue_id')::uuid,(p_request ->> 'source_job_id')::uuid,
            (p_request ->> 'reviewer_id')::uuid,(p_request ->> 'source_annotation_version')::integer,
            (p_request ->> 'result_annotation_version')::integer,(p_request ->> 'source_job_revision')::integer,
            (p_request ->> 'frame_start')::integer,(p_request ->> 'frame_end')::integer,p_request -> 'region_mask_ref',
            p_request ->> 'region_digest',p_request ->> 'candidate_digest',p_request ->> 'decision'
        ) RETURNING public.mask_review_scopes.* INTO v_scope;
        v_data := pg_catalog.jsonb_build_object('created',true,'row',pg_catalog.to_jsonb(v_scope));
            END IF;
        END IF;
    ELSIF v_operation = 'read_review_scopes' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'annotation_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'annotation_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'result_annotation_version') IS DISTINCT FROM 'number'
           OR (p_request ->> 'result_annotation_version') !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'result_annotation_version')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_start') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_start') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_start')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_end') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_end') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_end')::bigint > 2147483647
           OR (p_request ->> 'frame_end')::bigint < (p_request ->> 'frame_start')::bigint
           OR pg_catalog.jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'Invalid Mask review scope query' USING ERRCODE = '22023'; END IF;
        IF (p_request ->> 'lock')::boolean THEN
            FOR v_scope IN SELECT scope.* FROM public.mask_review_scopes AS scope
                WHERE scope.annotation_id=(p_request ->> 'annotation_id')::uuid
                  AND scope.result_annotation_version=(p_request ->> 'result_annotation_version')::integer
                  AND scope.frame_start <= (p_request ->> 'frame_end')::integer
                  AND scope.frame_end >= (p_request ->> 'frame_start')::integer
                ORDER BY scope.id FOR UPDATE
            LOOP v_rows := v_rows || pg_catalog.jsonb_build_array(pg_catalog.to_jsonb(v_scope)); END LOOP;
        ELSE
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(scope) ORDER BY scope.id),'[]'::jsonb)
            INTO v_rows FROM public.mask_review_scopes AS scope
            WHERE scope.annotation_id=(p_request ->> 'annotation_id')::uuid
              AND scope.result_annotation_version=(p_request ->> 'result_annotation_version')::integer
              AND scope.frame_start <= (p_request ->> 'frame_end')::integer
              AND scope.frame_end >= (p_request ->> 'frame_start')::integer;
        END IF;
        v_data := v_rows;
    ELSIF v_operation = 'read_repair_targets' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'issue_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'issue_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'include_related') IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'Invalid Mask repair target query' USING ERRCODE = '22023'; END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
            'id',annotation.id,'project_id',annotation.project_id,'task_id',annotation.task_id,
            'version',annotation.version,'geometry',annotation.geometry,'is_locked',annotation.is_locked,
            'class_name',annotation.class_name,'tool_unit_id',annotation.tool_unit_id
        ) ORDER BY annotation.id),'[]'::jsonb)
        INTO v_rows
        FROM public.mask_qc_issues AS issue
        JOIN public.annotations AS annotation ON (
            (NOT (p_request ->> 'include_related')::boolean AND annotation.id=issue.annotation_id)
            OR ((p_request ->> 'include_related')::boolean
                AND (annotation.id=issue.annotation_id OR annotation.id=ANY(issue.related_annotation_ids))
                AND annotation.is_active IS TRUE
                AND annotation.was_cancelled IS FALSE)
        )
        WHERE issue.id=(p_request ->> 'issue_id')::uuid;
        v_data := v_rows;
    ELSIF v_operation = 'read_video_segment' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'dataset_item_id') NOT IN ('string','null')
           OR (p_request ->> 'dataset_item_id' IS NOT NULL AND p_request ->> 'dataset_item_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_index') IS DISTINCT FROM 'number'
           OR p_request ->> 'frame_index' !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_index')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'requested_id') NOT IN ('string','null')
           OR (p_request ->> 'requested_id' IS NOT NULL AND p_request ->> 'requested_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN RAISE EXCEPTION 'Invalid Labeling video segment lookup' USING ERRCODE = '22023'; END IF;
        SELECT segment.* INTO v_segment FROM public.video_segments AS segment
        WHERE segment.dataset_item_id=(p_request ->> 'dataset_item_id')::uuid
          AND segment.start_frame <= (p_request ->> 'frame_index')::integer
          AND segment.end_frame >= (p_request ->> 'frame_index')::integer
          AND (p_request ->> 'requested_id' IS NULL OR segment.id=(p_request ->> 'requested_id')::uuid)
        ORDER BY segment.segment_index ASC LIMIT 1;
        IF FOUND THEN
            v_data := pg_catalog.jsonb_build_object(
                'id',v_segment.id,'locked_by',v_segment.locked_by,
                'lock_expires_at',v_segment.lock_expires_at
            );
        ELSE v_data := NULL; END IF;
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,'request',p_request,'data',v_data);
EXCEPTION
    WHEN invalid_text_representation OR invalid_datetime_format OR datetime_field_overflow OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'Invalid Mask repair request value' USING ERRCODE = '22023';
END;
$function$;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._raster_mask_reference_keys()
RETURNS TABLE(object_key text)
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT DISTINCT reference.object_key
    FROM (
        SELECT value #>> '{}' AS object_key
        FROM public.annotations AS annotation
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(annotation.geometry,'$.**.object_key') AS value
        WHERE annotation.is_active IS TRUE
        UNION ALL
        SELECT value #>> '{}'
        FROM public.predictions AS prediction
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(prediction.result,'$.**.object_key') AS value
        UNION ALL
        SELECT value #>> '{}'
        FROM public.batch_result_payloads AS payload
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(payload.body,'$.**.object_key') AS value
        WHERE payload.complete IS TRUE AND payload.body IS NOT NULL
        UNION ALL
        SELECT root.object_key
        FROM public.mask_gc_roots AS root
        JOIN public.video_tracker_jobs AS job ON job.id::text=root.root_id
        WHERE root.root_kind='video_tracker_running_outputs' AND job.status='running'
        UNION ALL
        SELECT value #>> '{}'
        FROM public.video_tracker_jobs AS job
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(job.staged_result,'$.**.object_key') AS value
        WHERE job.staged_result IS NOT NULL AND job.status IN ('pending_review','partially_reviewed','cancelled')
        UNION ALL
        SELECT value #>> '{}'
        FROM public.ai_mask_accept_decisions AS decision
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(decision.response_json,'$.**.object_key') AS value
        WHERE decision.expires_at > pg_catalog.transaction_timestamp()
        UNION ALL
        SELECT value #>> '{}'
        FROM public.mask_annotation_revisions AS revision
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(revision.geometry,'$.**.object_key') AS value
        WHERE revision.expires_at > pg_catalog.transaction_timestamp()
        UNION ALL
        SELECT value #>> '{}'
        FROM public.mask_qc_runs AS run
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(run.source_snapshot,'$.**.object_key') AS value
        WHERE run.status IN ('pending','running')
        UNION ALL
        SELECT issue.region_mask_ref ->> 'object_key'
        FROM public.mask_qc_issues AS issue
        UNION ALL
        SELECT scope.region_mask_ref ->> 'object_key'
        FROM public.mask_review_scopes AS scope
        UNION ALL
        SELECT value #>> '{}'
        FROM public.video_tracker_input_scopes AS scope
        CROSS JOIN LATERAL (
            SELECT aap_labeling_v1.video_tracker_input_referenced(scope.input_id) AS reply OFFSET 0
        ) AS retention
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(scope.scope,'$.**.object_key') AS value
        WHERE retention.reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
           OR retention.reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR retention.reply #>> '{data,input_id}' IS DISTINCT FROM scope.input_id::text
           OR retention.reply #> '{data,referenced}' IS DISTINCT FROM 'false'::jsonb
        UNION ALL
        SELECT value #>> '{}'
        FROM public.mask_repair_batches AS batch
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(batch.plan_json,'$.**.object_key') AS value
        WHERE ((batch.status='planned' AND batch.receipt_expires_at > pg_catalog.transaction_timestamp())
            OR batch.status IN ('pending','running','rolling_back')
            OR (batch.status IN ('completed','partial','rollback_failed') AND batch.rollback_expires_at > pg_catalog.transaction_timestamp()))
    ) AS reference
    WHERE reference.object_key IS NOT NULL
$function$;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.raster_mask_retention_query(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_data jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-quality-data/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Raster Mask retention request' USING ERRCODE = '22023';
    END IF;
    IF p_request ->> 'operation'='read_all_references' THEN
        IF NOT aap_labeling_v1._quality_exact_keys(p_request,ARRAY['schema','operation']) THEN RAISE EXCEPTION 'Invalid Raster Mask retention fields' USING ERRCODE = '22023'; END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(reference.object_key ORDER BY reference.object_key),'[]'::jsonb)
        INTO v_data FROM aap_labeling_v1._raster_mask_reference_keys() AS reference
        WHERE reference.object_key LIKE 'raster-masks/sha256/%';
    ELSIF p_request ->> 'operation'='check_reference' THEN
        IF NOT aap_labeling_v1._quality_exact_keys(p_request,ARRAY['schema','operation','object_key'])
           OR pg_catalog.jsonb_typeof(p_request -> 'object_key') IS DISTINCT FROM 'string'
           OR pg_catalog.length(p_request ->> 'object_key') NOT BETWEEN 1 AND 1024 THEN RAISE EXCEPTION 'Invalid Raster Mask reference key' USING ERRCODE = '22023'; END IF;
        SELECT pg_catalog.to_jsonb(EXISTS (
            SELECT 1 FROM aap_labeling_v1._raster_mask_reference_keys() AS reference
            WHERE reference.object_key=p_request ->> 'object_key'
        )) INTO v_data;
    ELSE
        RAISE EXCEPTION 'Invalid Raster Mask retention operation' USING ERRCODE = '22023';
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,'request',p_request,'data',v_data);
END;
$function$;
-- aap-statement-break

REVOKE ALL ON FUNCTION aap_labeling_v1._quality_exact_keys(jsonb,text[]),
    aap_labeling_v1.mask_qc_command(jsonb),aap_labeling_v1.mask_repair_command(jsonb),
    aap_labeling_v1._raster_mask_reference_keys(),aap_labeling_v1.raster_mask_retention_query(jsonb)
    FROM PUBLIC;
