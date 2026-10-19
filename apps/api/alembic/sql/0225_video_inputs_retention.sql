-- Video evidence is a Labeling-owned projection; it never points at Assets by FK.
CREATE FUNCTION aap_assets_v1._video_input_evidence_valid(p_data jsonb, p_ready boolean)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_frame_id jsonb;
    v_frame_sha jsonb;
    v_entry jsonb;
BEGIN
    IF p_ready IS NULL
       OR NOT aap_assets_v1._video_input_keys(p_data, ARRAY[
            'schema','input_id','job_id','task_id','project_id','plan_digest',
            'timebase_digest','frame_id','entry','source_sha256','frame_sha256'
       ])
       OR p_data ->> 'schema' IS DISTINCT FROM CASE WHEN p_ready
            THEN 'aap-assets-video-input-evidence/v1'
            ELSE 'aap-assets-video-input-failure/v1' END
       OR NOT aap_assets_v1._video_input_uuid(p_data -> 'input_id')
       OR NOT aap_assets_v1._video_input_uuid(p_data -> 'job_id')
       OR NOT aap_assets_v1._video_input_uuid(p_data -> 'task_id')
       OR NOT aap_assets_v1._video_input_uuid(p_data -> 'project_id')
       OR pg_catalog.jsonb_typeof(p_data -> 'plan_digest') IS DISTINCT FROM 'string'
       OR p_data ->> 'plan_digest' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_data -> 'timebase_digest') IS DISTINCT FROM 'string'
       OR p_data ->> 'timebase_digest' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_data -> 'source_sha256') IS DISTINCT FROM 'string'
       OR p_data ->> 'source_sha256' !~ '^[0-9a-f]{64}$'
       OR NOT aap_assets_v1._video_input_keys(p_data -> 'entry',
            ARRAY['ordinal','frame_index','pts_ms','is_keyframe']) THEN
        RETURN false;
    END IF;
    v_frame_id := p_data -> 'frame_id';
    v_frame_sha := p_data -> 'frame_sha256';
    v_entry := p_data -> 'entry';
    IF NOT aap_assets_v1._video_input_int(v_entry -> 'ordinal', 1, 2147483647)
       OR NOT aap_assets_v1._video_input_int(v_entry -> 'frame_index', 0, 2147483647)
       OR NOT aap_assets_v1._video_input_int(v_entry -> 'pts_ms', -9007199254740991, 9007199254740991)
       OR pg_catalog.jsonb_typeof(v_entry -> 'is_keyframe') IS DISTINCT FROM 'boolean' THEN
        RETURN false;
    END IF;
    IF p_ready THEN
        RETURN aap_assets_v1._video_input_uuid(v_frame_id)
           AND pg_catalog.jsonb_typeof(v_frame_sha) = 'string'
           AND v_frame_sha #>> '{}' ~ '^[0-9a-f]{64}$';
    END IF;
    RETURN (v_frame_id = 'null'::jsonb OR aap_assets_v1._video_input_uuid(v_frame_id))
       AND (v_frame_sha = 'null'::jsonb OR (
            pg_catalog.jsonb_typeof(v_frame_sha) = 'string'
            AND v_frame_sha #>> '{}' ~ '^[0-9a-f]{64}$'
       ))
       AND (v_frame_sha = 'null'::jsonb OR v_frame_id <> 'null'::jsonb);
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_evidence_valid(jsonb, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_input_sources_keys(p_data jsonb,p_keys text[])
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_data) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    RETURN (p_data - p_keys)='{}'::jsonb AND p_data ?& p_keys;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_sources_keys(jsonb,text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_input_sources_int(p_data jsonb,p_min bigint,p_max bigint)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_data) IS DISTINCT FROM 'number'
       OR (p_data #>> '{}') !~ '^-?(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_data #>> '{}') > 17 THEN RETURN false; END IF;
    RETURN (p_data #>> '{}')::numeric BETWEEN p_min AND p_max;
EXCEPTION WHEN OTHERS THEN RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_sources_int(jsonb,bigint,bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_input_sources_uuid(p_data jsonb)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT COALESCE(pg_catalog.jsonb_typeof(p_data)='string'
        AND p_data #>> '{}' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_sources_uuid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_input_evidence_valid(p_data jsonb,p_ready boolean)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_frame_id jsonb; v_frame_sha jsonb; v_entry jsonb;
BEGIN
    IF p_ready IS NULL
       OR NOT aap_labeling_v1._video_input_sources_keys(p_data,ARRAY[
            'schema','input_id','job_id','task_id','project_id','plan_digest',
            'timebase_digest','frame_id','entry','source_sha256','frame_sha256'
       ])
       OR p_data ->> 'schema' IS DISTINCT FROM CASE WHEN p_ready
            THEN 'aap-assets-video-input-evidence/v1'
            ELSE 'aap-assets-video-input-failure/v1' END
       OR NOT aap_labeling_v1._video_input_sources_uuid(p_data -> 'input_id')
       OR NOT aap_labeling_v1._video_input_sources_uuid(p_data -> 'job_id')
       OR NOT aap_labeling_v1._video_input_sources_uuid(p_data -> 'task_id')
       OR NOT aap_labeling_v1._video_input_sources_uuid(p_data -> 'project_id')
       OR pg_catalog.jsonb_typeof(p_data -> 'plan_digest') IS DISTINCT FROM 'string'
       OR p_data ->> 'plan_digest' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_data -> 'timebase_digest') IS DISTINCT FROM 'string'
       OR p_data ->> 'timebase_digest' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_data -> 'source_sha256') IS DISTINCT FROM 'string'
       OR p_data ->> 'source_sha256' !~ '^[0-9a-f]{64}$'
       OR NOT aap_labeling_v1._video_input_sources_keys(p_data -> 'entry',
            ARRAY['ordinal','frame_index','pts_ms','is_keyframe']) THEN RETURN false; END IF;
    v_frame_id := p_data -> 'frame_id'; v_frame_sha := p_data -> 'frame_sha256';
    v_entry := p_data -> 'entry';
    IF NOT aap_labeling_v1._video_input_sources_int(v_entry -> 'ordinal',1,2147483647)
       OR NOT aap_labeling_v1._video_input_sources_int(v_entry -> 'frame_index',0,2147483647)
       OR NOT aap_labeling_v1._video_input_sources_int(v_entry -> 'pts_ms',-9007199254740991,9007199254740991)
       OR pg_catalog.jsonb_typeof(v_entry -> 'is_keyframe') IS DISTINCT FROM 'boolean' THEN RETURN false; END IF;
    IF p_ready THEN
        RETURN aap_labeling_v1._video_input_sources_uuid(v_frame_id)
           AND pg_catalog.jsonb_typeof(v_frame_sha)='string'
           AND v_frame_sha #>> '{}' ~ '^[0-9a-f]{64}$';
    END IF;
    RETURN (v_frame_id='null'::jsonb OR aap_labeling_v1._video_input_sources_uuid(v_frame_id))
       AND (v_frame_sha='null'::jsonb OR (
            pg_catalog.jsonb_typeof(v_frame_sha)='string' AND v_frame_sha #>> '{}' ~ '^[0-9a-f]{64}$'
       )) AND (v_frame_sha='null'::jsonb OR v_frame_id<>'null'::jsonb);
EXCEPTION WHEN OTHERS THEN RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_evidence_valid(jsonb,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_input_sources_code(p_reply jsonb,p_schema text)
RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_code text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_reply) IS DISTINCT FROM 'object'
       OR p_reply ->> 'schema' IS DISTINCT FROM p_schema
       OR pg_catalog.jsonb_typeof(p_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN RETURN 'invalid_response'; END IF;
    IF p_reply -> 'ok'='true'::jsonb THEN
        IF NOT aap_labeling_v1._video_input_sources_keys(p_reply,ARRAY['schema','ok','data'])
           OR pg_catalog.jsonb_typeof(p_reply -> 'data') IS DISTINCT FROM 'object' THEN RETURN 'invalid_response'; END IF;
        RETURN NULL;
    END IF;
    IF NOT aap_labeling_v1._video_input_sources_keys(p_reply,ARRAY['schema','ok','error'])
       OR NOT aap_labeling_v1._video_input_sources_keys(p_reply -> 'error',ARRAY['code']) THEN RETURN 'invalid_response'; END IF;
    v_code := p_reply #>> '{error,code}';
    IF v_code='project_missing' THEN RETURN 'task_not_visible'; END IF;
    RETURN CASE WHEN v_code=ANY(ARRAY[
        'invalid_request','invalid_response','storage_unavailable','account_not_active','account_busy',
        'invalid_account_role','task_not_visible','task_busy','permission_denied','source_missing',
        'source_changed','source_not_ready','not_video','job_busy','job_not_active','manifest_scope_mismatch',
        'source_busy','metadata_not_ready','invalid_frame_range','asset_missing','operation_conflict',
        'operation_busy','lease_lost','dependency_changed','resource_budget_exceeded','object_missing',
        'input_missing','input_scope_mismatch','input_not_ready','input_expired','input_corrupt','input_busy',
        'manifest_missing','manifest_busy','manifest_not_ready','manifest_cursor_invalid','task_scope_changed',
        'unsupported_media','timebase_changed','timebase_incomplete','frame_missing','frame_not_ready',
        'frame_busy','versioning_required','resource_limit','timeout','processing_failed','reference_busy'
    ]) THEN v_code ELSE 'invalid_response' END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_sources_code(jsonb,text) FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.annotation_video_input_sources (
    annotation_id uuid NOT NULL REFERENCES public.annotations(id) ON DELETE CASCADE,
    input_id uuid NOT NULL,
    plan_digest varchar(64) NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    task_id uuid NOT NULL,
    project_id uuid NOT NULL,
    frame_id uuid NOT NULL,
    ordinal bigint NOT NULL CHECK (ordinal BETWEEN 1 AND 2147483647),
    frame_index integer NOT NULL CHECK (frame_index BETWEEN 0 AND 2147483647),
    timebase_digest varchar(64) NOT NULL CHECK (timebase_digest ~ '^[0-9a-f]{64}$'),
    source_sha256 varchar(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    frame_sha256 varchar(64) NOT NULL CHECK (frame_sha256 ~ '^[0-9a-f]{64}$'),
    evidence jsonb NOT NULL CHECK (aap_labeling_v1._video_input_evidence_valid(evidence, true)),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (annotation_id, input_id, plan_digest, ordinal),
    CHECK (
        evidence ->> 'input_id' = input_id::text
        AND evidence ->> 'task_id' = task_id::text
        AND evidence ->> 'project_id' = project_id::text
        AND evidence ->> 'frame_id' = frame_id::text
        AND evidence ->> 'plan_digest' = plan_digest
        AND evidence ->> 'timebase_digest' = timebase_digest
        AND evidence ->> 'source_sha256' = source_sha256
        AND evidence ->> 'frame_sha256' = frame_sha256
        AND evidence #>> '{entry,ordinal}' = ordinal::text
        AND evidence #>> '{entry,frame_index}' = frame_index::text
    )
);
-- aap-statement-break
CREATE INDEX ix_annotation_video_input_sources_input
    ON public.annotation_video_input_sources(input_id, annotation_id, plan_digest, ordinal);
-- aap-statement-break
REVOKE ALL ON public.annotation_video_input_sources FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_input_source_immutable()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF (NEW.annotation_id, NEW.input_id, NEW.plan_digest, NEW.task_id, NEW.project_id,
        NEW.frame_id, NEW.ordinal, NEW.frame_index, NEW.timebase_digest,
        NEW.source_sha256, NEW.frame_sha256, NEW.evidence, NEW.created_at)
       IS DISTINCT FROM
       (OLD.annotation_id, OLD.input_id, OLD.plan_digest, OLD.task_id, OLD.project_id,
        OLD.frame_id, OLD.ordinal, OLD.frame_index, OLD.timebase_digest,
        OLD.source_sha256, OLD.frame_sha256, OLD.evidence, OLD.created_at) THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'annotation video input evidence is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_source_immutable() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_annotation_video_input_source_immutable
BEFORE UPDATE ON public.annotation_video_input_sources
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._video_input_source_immutable();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._lock_accepted_video_input_pin(
    p_task_id uuid, p_project_id uuid, p_evidence jsonb, p_actor_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_frame public.asset_accepted_video_frames%ROWTYPE;
    v_code text;
    v_plan jsonb;
    v_reply jsonb;
BEGIN
    IF p_task_id IS NULL OR p_project_id IS NULL
       OR NOT aap_assets_v1._video_input_evidence_valid(p_evidence, true)
       OR p_evidence ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR p_evidence ->> 'project_id' IS DISTINCT FROM p_project_id::text THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT input.* INTO v_input
        FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_id = (p_evidence ->> 'input_id')::uuid
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'reference_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'input_missing');
    END IF;
    IF v_input.task_id IS DISTINCT FROM p_task_id
       OR v_input.project_id IS DISTINCT FROM p_project_id
       OR v_input.job_id::text IS DISTINCT FROM p_evidence ->> 'job_id'
       OR v_input.plan IS NULL
       OR v_input.plan ->> 'plan_digest' IS DISTINCT FROM p_evidence ->> 'plan_digest'
       OR v_input.plan ->> 'timebase_digest' IS DISTINCT FROM p_evidence ->> 'timebase_digest'
       OR v_input.plan #>> '{input,blob,sha256}' IS DISTINCT FROM p_evidence ->> 'source_sha256' THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    IF v_input.status = 'expired' THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'input_expired');
    ELSIF v_input.status <> 'ready' THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'input_not_ready');
    END IF;
    BEGIN
        SELECT frame.* INTO v_frame
        FROM public.asset_accepted_video_frames AS frame
        WHERE frame.frame_id = (p_evidence ->> 'frame_id')::uuid
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'reference_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'frame_missing');
    END IF;
    v_plan := v_input.plan;
    IF v_frame.input_id IS DISTINCT FROM v_input.input_id
       OR v_frame.plan_digest IS DISTINCT FROM p_evidence ->> 'plan_digest'
       OR v_frame.status = 'expired' THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'input_expired');
    ELSIF v_frame.status <> 'ready' OR v_frame.blob IS NULL THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'frame_not_ready');
    END IF;
    IF v_frame.ordinal::text IS DISTINCT FROM p_evidence #>> '{entry,ordinal}'
       OR v_frame.frame_index::text IS DISTINCT FROM p_evidence #>> '{entry,frame_index}'
       OR v_frame.pts_ms::text IS DISTINCT FROM p_evidence #>> '{entry,pts_ms}'
       OR pg_catalog.to_jsonb(v_frame.is_keyframe) IS DISTINCT FROM p_evidence #> '{entry,is_keyframe}'
       OR v_frame.blob ->> 'sha256' IS DISTINCT FROM p_evidence ->> 'frame_sha256'
       OR v_frame.frame_index::numeric <>
          (v_frame.ordinal::numeric - 1) * (v_plan #>> '{sampling,step}')::numeric
       OR v_frame.ordinal > (v_plan ->> 'selected_count')::bigint THEN
        RETURN aap_assets_v1._video_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    IF p_actor_id IS NOT NULL THEN
        v_code := aap_assets_v1._video_input_current_source(v_input, p_actor_id, true, false);
        IF v_code IS NOT NULL THEN
            RETURN aap_assets_v1._video_input_reply(NULL, v_code);
        END IF;
    END IF;
    UPDATE public.asset_accepted_video_inputs AS input
    SET retire_after = NULL,
        retention_checked_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE input.input_id = v_input.input_id;
    v_reply := aap_assets_v1._video_input_reply(
        pg_catalog.jsonb_build_object('frame', aap_assets_v1._video_input_frame(v_frame)), NULL
    );
    RETURN v_reply;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_input_reply(NULL, 'reference_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._video_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._lock_accepted_video_input_pin(uuid, uuid, jsonb, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_input_sources_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-video-input-sources/v1', 'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-video-input-sources/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_sources_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_input_abort(p_code text)
RETURNS void
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'video_evidence_abort:' || p_code;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_input_abort(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.accepted_video_input_referenced(p_input_id uuid)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_referenced boolean;
BEGIN
    IF p_input_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-video-input-retention/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT EXISTS (
        SELECT 1
        FROM public.annotation_video_input_sources AS source
        WHERE source.input_id = p_input_id
        UNION ALL
        SELECT 1
        FROM public.prediction_metas AS meta
        JOIN public.predictions AS prediction ON prediction.id = meta.prediction_id
        WHERE meta.prediction_id IS NOT NULL
          AND aap_labeling_v1._video_input_evidence_valid(meta.extra -> 'accepted_video_input', true)
          AND meta.extra #>> '{accepted_video_input,input_id}' = p_input_id::text
        UNION ALL
        SELECT 1
        FROM public.failed_predictions AS failed
        WHERE failed.extra #>> '{accepted_video_input,input_id}' = p_input_id::text
          AND failed.dismissed_at IS NULL
          AND (failed.retry_count < 3 OR failed.extra -> 'retry_pending' = 'true'::jsonb)
          AND aap_labeling_v1._video_input_evidence_valid(
                failed.extra -> 'accepted_video_input', true
          )
        UNION ALL
        SELECT 1
        FROM public.failed_predictions AS failed
        WHERE failed.extra #>> '{accepted_video_input,input_id}' = p_input_id::text
          AND failed.dismissed_at IS NULL
          AND (failed.retry_count < 3 OR failed.extra -> 'retry_pending' = 'true'::jsonb)
          AND aap_labeling_v1._video_input_evidence_valid(
                failed.extra -> 'accepted_video_input', false
          )
    ) INTO v_referenced;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-video-input-retention/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object('input_id', p_input_id, 'referenced', v_referenced)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.accepted_video_input_referenced(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.attach_annotation_video_input_source(
    p_annotation_id uuid, p_actor_id uuid, p_evidence jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_task_id uuid;
    v_project_id uuid;
    v_pin jsonb;
    v_frame jsonb;
    v_code text;
    v_attached integer;
BEGIN
    IF p_annotation_id IS NULL OR p_actor_id IS NULL
       OR NOT aap_labeling_v1._video_input_evidence_valid(p_evidence, true) THEN
        RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT annotation.task_id, annotation.project_id INTO v_task_id, v_project_id
        FROM public.annotations AS annotation
        WHERE annotation.id = p_annotation_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'reference_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'source_missing');
    END IF;
    IF v_task_id IS NULL OR v_project_id IS NULL
       OR p_evidence ->> 'task_id' IS DISTINCT FROM v_task_id::text
       OR p_evidence ->> 'project_id' IS DISTINCT FROM v_project_id::text THEN
        RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'input_scope_mismatch');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.annotation_video_input_sources AS source
        WHERE source.annotation_id = p_annotation_id
          AND source.input_id = (p_evidence ->> 'input_id')::uuid
          AND source.plan_digest = p_evidence ->> 'plan_digest'
          AND source.ordinal = (p_evidence #>> '{entry,ordinal}')::bigint
          AND source.evidence IS DISTINCT FROM p_evidence
    ) THEN
        RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'input_scope_mismatch');
    END IF;
    v_pin := aap_assets_v1._lock_accepted_video_input_pin(
        v_task_id, v_project_id, p_evidence, p_actor_id
    );
    v_code := aap_labeling_v1._video_input_sources_code(v_pin, 'aap-assets-video-input-execution/v1');
    IF v_code IS NOT NULL THEN
        RETURN aap_labeling_v1._video_input_sources_reply(NULL, v_code);
    END IF;
    IF NOT aap_labeling_v1._video_input_sources_keys(v_pin -> 'data', ARRAY['frame']) THEN
        RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'invalid_response');
    END IF;
    v_frame := v_pin #> '{data,frame}';
    IF NOT aap_labeling_v1._video_input_sources_keys(v_frame, ARRAY[
            'frame_id','plan','entry','status','blob','error','created_at'
       ])
       OR v_frame ->> 'frame_id' IS DISTINCT FROM p_evidence ->> 'frame_id'
       OR v_frame ->> 'status' IS DISTINCT FROM 'ready'
       OR v_frame -> 'entry' IS DISTINCT FROM p_evidence -> 'entry'
       OR v_frame #>> '{plan,plan_digest}' IS DISTINCT FROM p_evidence ->> 'plan_digest'
       OR v_frame #>> '{plan,timebase_digest}' IS DISTINCT FROM p_evidence ->> 'timebase_digest'
       OR v_frame #>> '{plan,input,input_id}' IS DISTINCT FROM p_evidence ->> 'input_id'
       OR v_frame #>> '{plan,input,job_id}' IS DISTINCT FROM p_evidence ->> 'job_id'
       OR v_frame #>> '{plan,input,blob,sha256}' IS DISTINCT FROM p_evidence ->> 'source_sha256'
       OR v_frame #>> '{blob,sha256}' IS DISTINCT FROM p_evidence ->> 'frame_sha256' THEN
        RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'input_scope_mismatch');
    END IF;
    INSERT INTO public.annotation_video_input_sources(
        annotation_id,input_id,plan_digest,task_id,project_id,frame_id,ordinal,frame_index,
        timebase_digest,source_sha256,frame_sha256,evidence
    ) VALUES (
        p_annotation_id,(p_evidence ->> 'input_id')::uuid,p_evidence ->> 'plan_digest',
        v_task_id,v_project_id,(p_evidence ->> 'frame_id')::uuid,
        (p_evidence #>> '{entry,ordinal}')::bigint,
        (p_evidence #>> '{entry,frame_index}')::integer,p_evidence ->> 'timebase_digest',
        p_evidence ->> 'source_sha256',p_evidence ->> 'frame_sha256',p_evidence
    ) ON CONFLICT (annotation_id,input_id,plan_digest,ordinal) DO NOTHING;
    GET DIAGNOSTICS v_attached = ROW_COUNT;
    RETURN aap_labeling_v1._video_input_sources_reply(
        pg_catalog.jsonb_build_object(
            'annotation_id',p_annotation_id,'input_id',(p_evidence ->> 'input_id')::uuid,
            'plan_digest',p_evidence ->> 'plan_digest','ordinal',(p_evidence #>> '{entry,ordinal}')::bigint,
            'attached',v_attached = 1
        ),NULL
    );
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'input_scope_mismatch');
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'reference_busy');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.attach_annotation_video_input_source(uuid, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.copy_annotation_video_input_sources(
    p_source_annotation_id uuid, p_target_annotation_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_locked_id uuid;
    v_source_task_id uuid;
    v_target_task_id uuid;
    v_source_project_id uuid;
    v_target_project_id uuid;
    v_source public.annotation_video_input_sources%ROWTYPE;
    v_pin jsonb;
    v_frame jsonb;
    v_code text;
    v_abort_message text;
    v_added integer;
    v_copied integer := 0;
BEGIN
    IF p_source_annotation_id IS NULL OR p_target_annotation_id IS NULL THEN
        RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        FOR v_locked_id IN
            SELECT annotation.id FROM public.annotations AS annotation
            WHERE annotation.id = ANY(ARRAY[p_source_annotation_id,p_target_annotation_id]::uuid[])
            ORDER BY annotation.id FOR UPDATE NOWAIT
        LOOP NULL; END LOOP;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'reference_busy');
    END;
    SELECT annotation.task_id,annotation.project_id INTO v_source_task_id,v_source_project_id
    FROM public.annotations AS annotation WHERE annotation.id=p_source_annotation_id;
    IF NOT FOUND THEN RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'source_missing'); END IF;
    SELECT annotation.task_id,annotation.project_id INTO v_target_task_id,v_target_project_id
    FROM public.annotations AS annotation WHERE annotation.id=p_target_annotation_id;
    IF NOT FOUND THEN RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'source_missing'); END IF;
    IF v_source_task_id IS NULL OR v_target_task_id IS NULL OR v_source_project_id IS NULL
       OR v_target_project_id IS NULL
       OR (v_source_task_id,v_source_project_id) IS DISTINCT FROM (v_target_task_id,v_target_project_id) THEN
        RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'input_scope_mismatch');
    END IF;
    FOR v_source IN
        SELECT source.* FROM public.annotation_video_input_sources AS source
        WHERE source.annotation_id=p_source_annotation_id
        ORDER BY source.input_id,source.plan_digest,source.ordinal
    LOOP
        IF NOT aap_labeling_v1._video_input_evidence_valid(v_source.evidence,true)
           OR v_source.task_id IS DISTINCT FROM v_source_task_id
           OR v_source.project_id IS DISTINCT FROM v_source_project_id THEN
            PERFORM aap_labeling_v1._video_input_abort('input_scope_mismatch');
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.annotation_video_input_sources AS target
            WHERE target.annotation_id=p_target_annotation_id
              AND target.input_id=v_source.input_id
              AND target.plan_digest=v_source.plan_digest
              AND target.ordinal=v_source.ordinal
              AND target.evidence IS DISTINCT FROM v_source.evidence
        ) THEN
            PERFORM aap_labeling_v1._video_input_abort('input_scope_mismatch');
        END IF;
        v_pin := aap_assets_v1._lock_accepted_video_input_pin(
            v_source_task_id,v_source_project_id,v_source.evidence,NULL
        );
        v_code := aap_labeling_v1._video_input_sources_code(v_pin,'aap-assets-video-input-execution/v1');
        IF v_code IS NOT NULL THEN PERFORM aap_labeling_v1._video_input_abort(v_code); END IF;
        IF NOT aap_labeling_v1._video_input_sources_keys(v_pin -> 'data',ARRAY['frame']) THEN
            PERFORM aap_labeling_v1._video_input_abort('invalid_response');
        END IF;
        v_frame := v_pin #> '{data,frame}';
        IF v_frame ->> 'status' IS DISTINCT FROM 'ready'
           OR v_frame ->> 'frame_id' IS DISTINCT FROM v_source.frame_id::text
           OR v_frame -> 'entry' IS DISTINCT FROM v_source.evidence -> 'entry'
           OR v_frame #>> '{plan,plan_digest}' IS DISTINCT FROM v_source.plan_digest
           OR v_frame #>> '{plan,input,blob,sha256}' IS DISTINCT FROM v_source.source_sha256
           OR v_frame #>> '{blob,sha256}' IS DISTINCT FROM v_source.frame_sha256 THEN
            PERFORM aap_labeling_v1._video_input_abort('input_scope_mismatch');
        END IF;
        INSERT INTO public.annotation_video_input_sources(
            annotation_id,input_id,plan_digest,task_id,project_id,frame_id,ordinal,frame_index,
            timebase_digest,source_sha256,frame_sha256,evidence
        ) VALUES (
            p_target_annotation_id,v_source.input_id,v_source.plan_digest,v_source_task_id,
            v_source_project_id,v_source.frame_id,v_source.ordinal,v_source.frame_index,
            v_source.timebase_digest,v_source.source_sha256,v_source.frame_sha256,v_source.evidence
        ) ON CONFLICT (annotation_id,input_id,plan_digest,ordinal) DO NOTHING;
        GET DIAGNOSTICS v_added = ROW_COUNT;
        v_copied := v_copied + v_added;
    END LOOP;
    RETURN aap_labeling_v1._video_input_sources_reply(
        pg_catalog.jsonb_build_object(
            'source_annotation_id',p_source_annotation_id,
            'target_annotation_id',p_target_annotation_id,'copied',v_copied
        ),NULL
    );
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS v_abort_message = MESSAGE_TEXT;
    IF pg_catalog.left(v_abort_message,pg_catalog.char_length('video_evidence_abort:'))
       = 'video_evidence_abort:' THEN
        v_code := pg_catalog.substr(
            v_abort_message,pg_catalog.char_length('video_evidence_abort:')+1
        );
        RETURN aap_labeling_v1._video_input_sources_reply(NULL,v_code);
    END IF;
    RETURN aap_labeling_v1._video_input_sources_reply(NULL,'invalid_response');
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'reference_busy');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._video_input_sources_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.copy_annotation_video_input_sources(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._inherit_annotation_video_input_sources()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_parent_task_id uuid;
    v_parent_project_id uuid;
    v_source public.annotation_video_input_sources%ROWTYPE;
    v_pin jsonb;
    v_frame jsonb;
    v_code text;
BEGIN
    IF NEW.parent_annotation_id IS NULL THEN RETURN NEW; END IF;
    SELECT parent.task_id,parent.project_id INTO v_parent_task_id,v_parent_project_id
    FROM public.annotations AS parent WHERE parent.id=NEW.parent_annotation_id;
    IF NOT FOUND OR NEW.task_id IS NULL OR NEW.project_id IS NULL
       OR (NEW.task_id,NEW.project_id) IS DISTINCT FROM (v_parent_task_id,v_parent_project_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='annotation video input lineage must stay within one Task and project';
    END IF;
    FOR v_source IN
        SELECT source.* FROM public.annotation_video_input_sources AS source
        WHERE source.annotation_id=NEW.parent_annotation_id
        ORDER BY source.input_id,source.plan_digest,source.ordinal
    LOOP
        IF NOT aap_labeling_v1._video_input_evidence_valid(v_source.evidence,true)
           OR v_source.task_id IS DISTINCT FROM NEW.task_id
           OR v_source.project_id IS DISTINCT FROM NEW.project_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='annotation video evidence scope changed';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.annotation_video_input_sources AS target
            WHERE target.annotation_id=NEW.id AND target.input_id=v_source.input_id
              AND target.plan_digest=v_source.plan_digest AND target.ordinal=v_source.ordinal
              AND target.evidence IS DISTINCT FROM v_source.evidence
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='annotation video evidence changed';
        END IF;
        v_pin := aap_assets_v1._lock_accepted_video_input_pin(
            NEW.task_id,NEW.project_id,v_source.evidence,NULL
        );
        v_code := aap_labeling_v1._video_input_sources_code(v_pin,'aap-assets-video-input-execution/v1');
        IF v_code IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='annotation video input is no longer retained';
        END IF;
        v_frame := v_pin #> '{data,frame}';
        IF v_frame ->> 'status' IS DISTINCT FROM 'ready'
           OR v_frame ->> 'frame_id' IS DISTINCT FROM v_source.frame_id::text
           OR v_frame -> 'entry' IS DISTINCT FROM v_source.evidence -> 'entry'
           OR v_frame #>> '{plan,plan_digest}' IS DISTINCT FROM v_source.plan_digest
           OR v_frame #>> '{blob,sha256}' IS DISTINCT FROM v_source.frame_sha256 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='annotation video input evidence changed';
        END IF;
        INSERT INTO public.annotation_video_input_sources(
            annotation_id,input_id,plan_digest,task_id,project_id,frame_id,ordinal,frame_index,
            timebase_digest,source_sha256,frame_sha256,evidence
        ) VALUES (
            NEW.id,v_source.input_id,v_source.plan_digest,NEW.task_id,NEW.project_id,
            v_source.frame_id,v_source.ordinal,v_source.frame_index,v_source.timebase_digest,
            v_source.source_sha256,v_source.frame_sha256,v_source.evidence
        ) ON CONFLICT (annotation_id,input_id,plan_digest,ordinal) DO NOTHING;
    END LOOP;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._inherit_annotation_video_input_sources() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_inherit_annotation_video_input_sources
AFTER INSERT OR UPDATE OF parent_annotation_id ON public.annotations
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._inherit_annotation_video_input_sources();
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._propagate_annotation_video_input_source()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.pg_trigger_depth() > 1 THEN RETURN NEW; END IF;
    IF EXISTS (
        WITH RECURSIVE descendants(annotation_id,task_id,project_id,path) AS (
            SELECT child.id,child.task_id,child.project_id,
                ARRAY[NEW.annotation_id,child.id]::uuid[]
            FROM public.annotations AS parent
            JOIN public.annotations AS child
              ON child.parent_annotation_id=parent.id
             AND child.task_id=parent.task_id AND child.project_id=parent.project_id
            WHERE parent.id=NEW.annotation_id AND parent.task_id=NEW.task_id
              AND parent.project_id=NEW.project_id
            UNION ALL
            SELECT child.id,child.task_id,child.project_id,descendants.path || child.id
            FROM public.annotations AS child
            JOIN descendants ON child.parent_annotation_id=descendants.annotation_id
                AND child.task_id=descendants.task_id AND child.project_id=descendants.project_id
            WHERE NOT child.id=ANY(descendants.path)
        )
        SELECT 1 FROM descendants
        JOIN public.annotation_video_input_sources AS target
          ON target.annotation_id=descendants.annotation_id
         AND target.input_id=NEW.input_id AND target.plan_digest=NEW.plan_digest
         AND target.ordinal=NEW.ordinal
        WHERE target.evidence IS DISTINCT FROM NEW.evidence
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='annotation video evidence changed';
    END IF;
    WITH RECURSIVE descendants(annotation_id,task_id,project_id,path) AS (
        SELECT child.id,child.task_id,child.project_id,
            ARRAY[NEW.annotation_id,child.id]::uuid[]
        FROM public.annotations AS parent
        JOIN public.annotations AS child
          ON child.parent_annotation_id=parent.id
         AND child.task_id=parent.task_id AND child.project_id=parent.project_id
        WHERE parent.id=NEW.annotation_id AND parent.task_id=NEW.task_id
          AND parent.project_id=NEW.project_id
        UNION ALL
        SELECT child.id,child.task_id,child.project_id,descendants.path || child.id
        FROM public.annotations AS child
        JOIN descendants ON child.parent_annotation_id=descendants.annotation_id
            AND child.task_id=descendants.task_id AND child.project_id=descendants.project_id
        WHERE NOT child.id=ANY(descendants.path)
    )
    INSERT INTO public.annotation_video_input_sources(
        annotation_id,input_id,plan_digest,task_id,project_id,frame_id,ordinal,frame_index,
        timebase_digest,source_sha256,frame_sha256,evidence
    )
    SELECT descendants.annotation_id,NEW.input_id,NEW.plan_digest,descendants.task_id,
        descendants.project_id,NEW.frame_id,NEW.ordinal,NEW.frame_index,NEW.timebase_digest,
        NEW.source_sha256,NEW.frame_sha256,NEW.evidence
    FROM descendants
    ON CONFLICT (annotation_id,input_id,plan_digest,ordinal) DO NOTHING;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._propagate_annotation_video_input_source() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_propagate_annotation_video_input_source
AFTER INSERT ON public.annotation_video_input_sources
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._propagate_annotation_video_input_source();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.sweep_accepted_video_inputs(p_limit integer)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_candidate record;
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_member public.asset_accepted_video_members%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_frame public.asset_accepted_video_frames%ROWTYPE;
    v_frame_attempt public.asset_accepted_video_frame_attempts%ROWTYPE;
    v_reply jsonb;
    v_job jsonb;
    v_refs jsonb;
    v_code text;
    v_now timestamptz;
    v_not_before timestamptz;
    v_prefix text;
    v_expired integer := 0;
    v_page integer;
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    v_page := p_limit;
    FOR v_candidate IN
        SELECT input.input_id
        FROM public.asset_accepted_video_inputs AS input
        WHERE (
            input.status = 'ready'
            AND ((input.retire_after IS NOT NULL AND input.retire_after <= pg_catalog.clock_timestamp())
                 OR input.retention_checked_at IS NULL
                 OR input.retention_checked_at <= pg_catalog.clock_timestamp() - interval '1 day')
        ) OR (
            input.status IN ('pending','materializing')
            AND (input.lease_expires_at IS NULL OR input.lease_expires_at <= pg_catalog.clock_timestamp())
            AND (input.retention_checked_at IS NULL
                 OR input.retention_checked_at <= pg_catalog.clock_timestamp() - interval '1 day')
        ) OR (
            input.status = 'failed'
            AND (input.retention_checked_at IS NULL OR (
                input.retention_checked_at <= pg_catalog.clock_timestamp() - interval '1 day'
                AND EXISTS (
                    SELECT 1 FROM public.asset_accepted_video_input_attempts AS attempt
                    WHERE attempt.input_id=input.input_id AND attempt.status <> 'expired'
                )
            ))
        )
        ORDER BY input.retire_after NULLS FIRST,input.created_at,input.input_id
        LIMIT v_page FOR UPDATE OF input SKIP LOCKED
    LOOP
        SELECT input.* INTO v_input
        FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_id=v_candidate.input_id;
        v_now := pg_catalog.clock_timestamp();
        IF v_input.lease_token IS NOT NULL AND v_input.lease_expires_at > v_now THEN
            CONTINUE;
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.asset_accepted_video_frames AS frame
            WHERE frame.input_id=v_input.input_id AND frame.status='materializing'
              AND frame.lease_expires_at > v_now
        ) THEN
            CONTINUE;
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.asset_accepted_video_input_attempts AS attempt
            WHERE attempt.input_id=v_input.input_id AND attempt.status='leased'
              AND attempt.lease_expires_at > v_now
        ) OR EXISTS (
            SELECT 1 FROM public.asset_accepted_video_frame_attempts AS attempt
            WHERE attempt.input_id=v_input.input_id AND attempt.status='leased'
              AND attempt.lease_expires_at > v_now
        ) THEN
            CONTINUE;
        END IF;
        IF EXISTS (
            SELECT 1
            FROM public.asset_accepted_video_input_attempts AS attempt
            JOIN public.asset_accepted_input_garbage AS garbage ON garbage.prefix=attempt.prefix
            WHERE attempt.input_id=v_input.input_id AND garbage.lease_token IS NOT NULL
              AND garbage.lease_expires_at > v_now
        ) OR EXISTS (
            SELECT 1
            FROM public.asset_accepted_video_frame_attempts AS attempt
            JOIN public.asset_accepted_input_garbage AS garbage ON garbage.prefix=attempt.prefix
            WHERE attempt.input_id=v_input.input_id AND garbage.lease_token IS NOT NULL
              AND garbage.lease_expires_at > v_now
        ) THEN
            CONTINUE;
        END IF;
        SELECT manifest.* INTO v_manifest
        FROM public.asset_accepted_video_manifests AS manifest
        WHERE manifest.job_id=v_input.job_id;
        IF NOT FOUND THEN CONTINUE; END IF;
        v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,false);
        v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
        IF v_code IS NOT NULL OR NOT aap_assets_v1._video_input_keys(v_reply -> 'data',ARRAY['job']) THEN
            CONTINUE;
        END IF;
        v_job := v_reply #> '{data,job}';
        IF v_job <> 'null'::jsonb AND v_job -> 'active' = 'true'::jsonb THEN
            UPDATE public.asset_accepted_video_inputs AS input
            SET retire_after=NULL,retention_checked_at=v_now,updated_at=v_now
            WHERE input.input_id=v_input.input_id;
            CONTINUE;
        END IF;
        IF v_input.status='failed' THEN
            FOR v_attempt IN
                SELECT attempt.* FROM public.asset_accepted_video_input_attempts AS attempt
                WHERE attempt.input_id=v_input.input_id
                ORDER BY attempt.generation
            LOOP
                v_not_before := GREATEST(v_now,v_attempt.lease_expires_at)+interval '24 hours';
                PERFORM aap_assets_v1._video_input_queue_prefix(v_attempt.prefix,'input_failed',v_not_before);
                UPDATE public.asset_accepted_video_input_attempts AS attempt
                SET status='expired',completed_at=COALESCE(attempt.completed_at,v_now)
                WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
            END LOOP;
            FOR v_frame_attempt IN
                SELECT attempt.* FROM public.asset_accepted_video_frame_attempts AS attempt
                WHERE attempt.input_id=v_input.input_id
                ORDER BY attempt.frame_id,attempt.generation
            LOOP
                v_not_before := GREATEST(v_now,v_frame_attempt.lease_expires_at)+interval '24 hours';
                PERFORM aap_assets_v1._video_input_queue_prefix(v_frame_attempt.prefix,'input_failed',v_not_before);
                UPDATE public.asset_accepted_video_frame_attempts AS attempt
                SET status='expired',completed_at=COALESCE(attempt.completed_at,v_now)
                WHERE attempt.frame_id=v_frame_attempt.frame_id AND attempt.generation=v_frame_attempt.generation;
            END LOOP;
            UPDATE public.asset_accepted_video_inputs AS input
            SET retention_checked_at=v_now,updated_at=v_now
            WHERE input.input_id=v_input.input_id;
            CONTINUE;
        END IF;
        IF v_input.status IN ('pending','materializing') THEN
            SELECT member.* INTO v_member
            FROM public.asset_accepted_video_members AS member
            WHERE member.job_id=v_input.job_id AND member.task_id=v_input.task_id
              AND member.input_id=v_input.input_id;
            IF NOT FOUND OR v_manifest.status <> 'preparing' OR v_member.state <> 'preparing' THEN
                CONTINUE;
            END IF;
            FOR v_attempt IN
                SELECT attempt.* FROM public.asset_accepted_video_input_attempts AS attempt
                WHERE attempt.input_id=v_input.input_id
                  AND attempt.status IN ('leased','ready','failed','abandoned')
                ORDER BY attempt.generation
            LOOP
                v_not_before := GREATEST(v_now,v_attempt.lease_expires_at)+interval '24 hours';
                PERFORM aap_assets_v1._video_input_queue_prefix(v_attempt.prefix,'job_inactive',v_not_before);
                UPDATE public.asset_accepted_video_input_attempts AS attempt
                SET status='expired',completed_at=COALESCE(attempt.completed_at,v_now)
                WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
            END LOOP;
            FOR v_frame_attempt IN
                SELECT attempt.* FROM public.asset_accepted_video_frame_attempts AS attempt
                WHERE attempt.input_id=v_input.input_id
                ORDER BY attempt.frame_id,attempt.generation
            LOOP
                v_not_before := GREATEST(v_now,v_frame_attempt.lease_expires_at)+interval '24 hours';
                PERFORM aap_assets_v1._video_input_queue_prefix(v_frame_attempt.prefix,'job_inactive',v_not_before);
                UPDATE public.asset_accepted_video_frame_attempts AS attempt
                SET status='expired',completed_at=COALESCE(attempt.completed_at,v_now)
                WHERE attempt.frame_id=v_frame_attempt.frame_id AND attempt.generation=v_frame_attempt.generation;
            END LOOP;
            UPDATE public.asset_accepted_video_inputs AS input
            SET status='failed',error_code='job_not_active',
                lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                last_lease_token=COALESCE(v_input.lease_token,input.last_lease_token),
                last_outcome=CASE WHEN v_input.lease_token IS NULL THEN input.last_outcome ELSE 'lease_expired' END,
                retention_checked_at=v_now,updated_at=v_now
            WHERE input.input_id=v_input.input_id;
            PERFORM aap_assets_v1._video_input_complete_member(v_input.input_id,'failed','job_not_active');
            CONTINUE;
        END IF;
        IF v_input.status <> 'ready' OR v_input.plan IS NULL THEN CONTINUE; END IF;
        v_refs := aap_labeling_v1.accepted_video_input_referenced(v_input.input_id);
        IF v_refs IS NULL
           OR v_refs ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-input-retention/v1'
           OR v_refs -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_refs #>> '{data,input_id}' IS DISTINCT FROM v_input.input_id::text
           OR pg_catalog.jsonb_typeof(v_refs #> '{data,referenced}') IS DISTINCT FROM 'boolean' THEN
            CONTINUE;
        END IF;
        IF v_refs #> '{data,referenced}' = 'true'::jsonb THEN
            UPDATE public.asset_accepted_video_inputs AS input
            SET retire_after=NULL,retention_checked_at=v_now,updated_at=v_now
            WHERE input.input_id=v_input.input_id;
            CONTINUE;
        END IF;
        IF v_input.retire_after IS NULL OR v_input.retire_after > v_now THEN
            UPDATE public.asset_accepted_video_inputs AS input
            SET retire_after=COALESCE(input.retire_after,v_now+interval '30 days'),
                retention_checked_at=v_now,updated_at=v_now
            WHERE input.input_id=v_input.input_id;
            CONTINUE;
        END IF;
        FOR v_attempt IN
            SELECT attempt.* FROM public.asset_accepted_video_input_attempts AS attempt
            WHERE attempt.input_id=v_input.input_id
            ORDER BY attempt.generation
        LOOP
            v_not_before := GREATEST(v_now,v_attempt.lease_expires_at)+interval '24 hours';
            PERFORM aap_assets_v1._video_input_queue_prefix(v_attempt.prefix,'input_expired',v_not_before);
            UPDATE public.asset_accepted_video_input_attempts AS attempt
            SET status='expired',completed_at=COALESCE(attempt.completed_at,v_now)
            WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
        END LOOP;
        FOR v_frame IN
            SELECT frame.* FROM public.asset_accepted_video_frames AS frame
            WHERE frame.input_id=v_input.input_id AND frame.status='ready'
            ORDER BY frame.ordinal
            FOR UPDATE OF frame NOWAIT
        LOOP
            UPDATE public.asset_accepted_video_frames AS frame
            SET status='expired',updated_at=v_now
            WHERE frame.frame_id=v_frame.frame_id;
        END LOOP;
        FOR v_frame_attempt IN
            SELECT attempt.* FROM public.asset_accepted_video_frame_attempts AS attempt
            WHERE attempt.input_id=v_input.input_id
            ORDER BY attempt.frame_id,attempt.generation
        LOOP
            v_not_before := GREATEST(v_now,v_frame_attempt.lease_expires_at)+interval '24 hours';
            PERFORM aap_assets_v1._video_input_queue_prefix(v_frame_attempt.prefix,'input_expired',v_not_before);
            UPDATE public.asset_accepted_video_frame_attempts AS attempt
            SET status='expired',completed_at=COALESCE(attempt.completed_at,v_now)
            WHERE attempt.frame_id=v_frame_attempt.frame_id AND attempt.generation=v_frame_attempt.generation;
        END LOOP;
        UPDATE public.asset_accepted_video_inputs AS input
        SET status='expired',retention_checked_at=v_now,updated_at=v_now
        WHERE input.input_id=v_input.input_id;
        v_expired := v_expired + 1;
    END LOOP;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('expired',v_expired),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.sweep_accepted_video_inputs(integer) FROM PUBLIC;
