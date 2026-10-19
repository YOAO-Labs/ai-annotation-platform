-- Retain one immutable full-video admission beside the canonical Labeling Job.
-- Source materialization reuses asset_accepted_video_inputs and its attempt,
-- timebase, and garbage ledgers; input_owner keeps batch and tracker authority
-- distinct inside those shared rows.
CREATE FUNCTION aap_assets_v1._video_tracker_input_reply(p_data jsonb, p_code text)
RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema','aap-assets-video-tracker-inputs/v1','ok',true,'data',p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema','aap-assets-video-tracker-inputs/v1','ok',false,
        'error',pg_catalog.jsonb_build_object('code',p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_tracker_input_reply(jsonb,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._video_tracker_input_reply(p_data jsonb, p_code text)
RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema','aap-labeling-video-tracker-inputs/v1','ok',true,'data',p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema','aap-labeling-video-tracker-inputs/v1','ok',false,
        'error',pg_catalog.jsonb_build_object('code',p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_tracker_input_reply(jsonb,text) FROM PUBLIC;
-- aap-statement-break
ALTER TABLE public.asset_accepted_video_inputs
    ADD COLUMN input_owner text NOT NULL DEFAULT 'manifest'
        CHECK (input_owner IN ('manifest','tracker')),
    ADD COLUMN tracker_job_id uuid,
    ADD CONSTRAINT ck_asset_accepted_video_input_owner CHECK (
        (input_owner='manifest' AND tracker_job_id IS NULL)
        OR (input_owner='tracker' AND tracker_job_id IS NOT NULL AND tracker_job_id=job_id)
    );
-- aap-statement-break
CREATE INDEX ix_asset_accepted_video_inputs_owner_claim
    ON public.asset_accepted_video_inputs(input_owner,status,lease_expires_at,created_at,input_id);
-- aap-statement-break
CREATE TABLE public.video_tracker_input_scopes (
    tracker_job_id uuid PRIMARY KEY,
    input_id uuid NOT NULL UNIQUE,
    task_id uuid NOT NULL,
    project_id uuid NOT NULL,
    origin_actor_id uuid NOT NULL,
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    prompt_digest text NOT NULL CHECK (prompt_digest ~ '^[0-9a-f]{64}$'),
    scope jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(scope)='object'),
    prepare_error text CHECK (prepare_error IS NULL OR prepare_error IN (
        'source_missing','source_changed','source_not_ready','not_video',
        'task_scope_changed','timebase_changed','input_expired','input_corrupt',
        'resource_budget_exceeded'
    )),
    accepted_evidence jsonb CHECK (accepted_evidence IS NULL OR pg_catalog.jsonb_typeof(accepted_evidence)='object'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (scope ->> 'tracker_job_id' = tracker_job_id::text
       AND scope ->> 'origin_actor_id' = origin_actor_id::text
       AND scope ->> 'request_digest' = request_digest
       AND scope ->> 'prompt_digest' = prompt_digest
       AND scope #>> '{task,task_id}' = task_id::text
       AND scope #>> '{task,project_id}' = project_id::text)
);
-- aap-statement-break
CREATE INDEX ix_video_tracker_input_scopes_task
    ON public.video_tracker_input_scopes(task_id,created_at,tracker_job_id);
-- aap-statement-break
REVOKE ALL ON public.video_tracker_input_scopes FROM PUBLIC;
-- aap-statement-break
CREATE TABLE public.annotation_video_tracker_input_sources (
    annotation_id uuid NOT NULL REFERENCES public.annotations(id) ON DELETE CASCADE,
    tracker_job_id uuid NOT NULL,
    input_id uuid NOT NULL,
    task_id uuid NOT NULL,
    project_id uuid NOT NULL,
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    prompt_digest text NOT NULL CHECK (prompt_digest ~ '^[0-9a-f]{64}$'),
    plan_digest text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    timebase_digest text NOT NULL CHECK (timebase_digest ~ '^[0-9a-f]{64}$'),
    source_version_id text NOT NULL CHECK (pg_catalog.char_length(source_version_id) BETWEEN 1 AND 255),
    source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    from_frame integer NOT NULL CHECK (from_frame >= 0),
    to_frame integer NOT NULL CHECK (to_frame >= from_frame),
    direction text NOT NULL CHECK (direction IN ('forward','backward','bidirectional')),
    grid_step integer NOT NULL CHECK (grid_step BETWEEN 1 AND 2147483647),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY(annotation_id,input_id),
    UNIQUE(annotation_id,tracker_job_id),
    CHECK (input_id IS NOT NULL AND pg_catalog.btrim(source_version_id)<>''
       AND source_version_id<>'null')
);
-- aap-statement-break
CREATE INDEX ix_annotation_video_tracker_input_sources_input
    ON public.annotation_video_tracker_input_sources(input_id,annotation_id);
-- aap-statement-break
REVOKE ALL ON public.annotation_video_tracker_input_sources FROM PUBLIC;
-- aap-statement-break

-- Assets reads current source facts only through this narrow projection. It
-- reuses Labeling's active-actor/task authorization and locks the source row;
-- no URL or external object I/O occurs under the transaction.
CREATE FUNCTION aap_assets_v1.read_video_tracker_task_source(
    p_actor_id uuid,p_task_id uuid,p_expected_task jsonb
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
    v_task jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_source jsonb;
    v_code text;
BEGIN
    IF p_actor_id IS NULL OR p_task_id IS NULL OR p_expected_task IS NULL THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    v_reply := aap_labeling_v1.lock_video_input_task_source(
        p_task_id,p_actor_id,p_expected_task
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-input-source/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN ('invalid_request','account_not_active','account_busy',
            'invalid_account_role','task_busy','task_not_visible','permission_denied',
            'source_not_ready','source_changed') THEN
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_code);
        END IF;
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    v_data := v_reply -> 'data';
    v_task := v_data -> 'binding';
    IF v_reply #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR v_reply #> '{data,upload_ready}' IS DISTINCT FROM 'true'::jsonb
       OR v_task ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR NOT aap_assets_v1._video_input_task_valid(v_task,false)
       OR v_task ->> 'file_type' IS DISTINCT FROM 'video' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'task_scope_changed');
    END IF;
    BEGIN
        SELECT item.* INTO v_item
        FROM public.dataset_items AS item
        WHERE item.id=(v_task ->> 'dataset_item_id')::uuid
        FOR SHARE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'source_busy');
    END;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'source_missing'); END IF;
    v_source := aap_assets_v1._video_input_source_json(v_item);
    IF NOT aap_assets_v1._video_input_source_valid(v_source,true) THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_corrupt');
    END IF;
    IF v_source ->> 'file_type' IS DISTINCT FROM 'video'
       OR v_source ->> 'source_drifted' IS DISTINCT FROM 'false' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'source_changed');
    END IF;
    RETURN aap_assets_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object('task',v_task,'source_facts',v_source),NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_video_tracker_task_source(uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break

-- The canonical projector never reads AsyncJob or creates a Jobs mirror. Its
-- record is keyed to video_tracker_jobs and rechecks the caller's current Task
-- authority on every control and acceptance operation.
CREATE FUNCTION aap_labeling_v1.project_video_tracker_input(
    p_actor_id uuid,p_tracker_job_id uuid,p_task_id uuid,p_input_id uuid,
    p_request_digest text,p_require_active boolean,p_allow_review boolean,p_lock boolean
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_scope public.video_tracker_input_scopes%ROWTYPE;
    v_job public.video_tracker_jobs%ROWTYPE;
    v_reply jsonb;
    v_task jsonb;
    v_task_id uuid;
    v_code text;
BEGIN
    IF p_actor_id IS NULL OR p_tracker_job_id IS NULL OR p_require_active IS NULL
       OR p_allow_review IS NULL OR p_lock IS NULL
       OR (p_request_digest IS NOT NULL AND p_request_digest !~ '^[0-9a-f]{64}$') THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    -- Prove current Task visibility before attempting NOWAIT locks on the
    -- canonical Job or retained scope. Recheck under the lock below.
    SELECT job.task_id INTO v_task_id FROM public.video_tracker_jobs AS job
    WHERE job.id=p_tracker_job_id;
    IF NOT FOUND THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    IF p_task_id IS NOT NULL AND v_task_id IS DISTINCT FROM p_task_id THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    v_reply := aap_labeling_v1.read_video_input_task_source(v_task_id,p_actor_id);
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-input-source/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN ('invalid_request','account_not_active','account_busy',
            'invalid_account_role','task_busy','task_not_visible','permission_denied',
            'source_missing','source_changed','source_not_ready','not_video') THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_code);
        END IF;
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR v_reply #>> '{data,binding,task_id}' IS DISTINCT FROM v_task_id::text THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF p_lock THEN
        SELECT job.id, job.task_id, job.dataset_item_id, job.annotation_id, job.segment_id,
            job.target_class_name, job.target_tool_unit_id, job.created_by, job.status, job.job_kind,
            job.track_id_snapshot, job.correction_frame, job.model_key, job.direction, job.from_frame,
            job.to_frame, job.prompt, job.staged_result, job.revision, job.cancel_requested_at,
            job.started_at, job.created_at, job.completed_at
        INTO v_job.id, v_job.task_id, v_job.dataset_item_id, v_job.annotation_id, v_job.segment_id,
            v_job.target_class_name, v_job.target_tool_unit_id, v_job.created_by, v_job.status, v_job.job_kind,
            v_job.track_id_snapshot, v_job.correction_frame, v_job.model_key, v_job.direction, v_job.from_frame,
            v_job.to_frame, v_job.prompt, v_job.staged_result, v_job.revision, v_job.cancel_requested_at,
            v_job.started_at, v_job.created_at, v_job.completed_at
        FROM public.video_tracker_jobs AS job
        WHERE job.id=p_tracker_job_id FOR UPDATE NOWAIT;
    ELSE
        SELECT job.id, job.task_id, job.dataset_item_id, job.annotation_id, job.segment_id,
            job.target_class_name, job.target_tool_unit_id, job.created_by, job.status, job.job_kind,
            job.track_id_snapshot, job.correction_frame, job.model_key, job.direction, job.from_frame,
            job.to_frame, job.prompt, job.staged_result, job.revision, job.cancel_requested_at,
            job.started_at, job.created_at, job.completed_at
        INTO v_job.id, v_job.task_id, v_job.dataset_item_id, v_job.annotation_id, v_job.segment_id,
            v_job.target_class_name, v_job.target_tool_unit_id, v_job.created_by, v_job.status, v_job.job_kind,
            v_job.track_id_snapshot, v_job.correction_frame, v_job.model_key, v_job.direction, v_job.from_frame,
            v_job.to_frame, v_job.prompt, v_job.staged_result, v_job.revision, v_job.cancel_requested_at,
            v_job.started_at, v_job.created_at, v_job.completed_at
        FROM public.video_tracker_jobs AS job
        WHERE job.id=p_tracker_job_id FOR SHARE NOWAIT;
    END IF;
    IF NOT FOUND THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    IF v_job.task_id IS DISTINCT FROM v_task_id THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'source_changed');
    END IF;
    IF p_lock THEN
        SELECT scope.* INTO v_scope FROM public.video_tracker_input_scopes AS scope
        WHERE scope.tracker_job_id=p_tracker_job_id FOR UPDATE NOWAIT;
    ELSE
        SELECT scope.* INTO v_scope FROM public.video_tracker_input_scopes AS scope
        WHERE scope.tracker_job_id=p_tracker_job_id FOR SHARE NOWAIT;
    END IF;
    IF NOT FOUND THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    IF (p_task_id IS NOT NULL AND v_scope.task_id<>p_task_id)
       OR (p_input_id IS NOT NULL AND v_scope.input_id<>p_input_id)
       OR (p_request_digest IS NOT NULL AND v_scope.request_digest<>p_request_digest) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    IF v_job.task_id IS DISTINCT FROM v_task_id
       OR v_job.task_id<>v_scope.task_id
       OR (v_job.created_by IS NOT NULL AND v_job.created_by IS DISTINCT FROM v_scope.origin_actor_id)
       OR v_job.dataset_item_id IS DISTINCT FROM
            (v_scope.scope #>> '{task,dataset_item_id}')::uuid
       OR NOT aap_labeling_v1._video_tracker_request_matches(v_scope.scope,v_job) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    IF p_require_active AND (
        v_job.status NOT IN ('queued','running') OR v_job.cancel_requested_at IS NOT NULL
        OR v_job.created_by IS DISTINCT FROM v_scope.origin_actor_id
    ) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'job_not_active');
    END IF;
    IF p_allow_review AND NOT (
        v_job.status IN ('pending_review','partially_reviewed')
        OR (v_job.status='cancelled' AND v_job.staged_result IS NOT NULL
            AND COALESCE(v_job.completed_at,v_job.created_at) + interval '24 hours' > pg_catalog.clock_timestamp())
    ) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'job_not_active');
    END IF;
    IF p_lock THEN
        v_reply := aap_labeling_v1.lock_video_input_task_source(
            v_scope.task_id,p_actor_id,v_scope.scope -> 'task'
        );
    ELSE
        v_reply := aap_labeling_v1.read_video_input_task_source(
            v_scope.task_id,p_actor_id
        );
    END IF;
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-input-source/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN ('invalid_request','account_not_active','account_busy',
            'invalid_account_role','task_busy','task_not_visible','permission_denied',
            'source_missing','source_changed','source_not_ready','not_video') THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_code);
        END IF;
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    v_task := v_reply #> '{data,binding}';
    IF v_reply #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR v_reply #>> '{data,binding,task_id}' IS DISTINCT FROM v_scope.task_id::text
       OR v_reply #>> '{data,binding,project_id}' IS DISTINCT FROM v_scope.project_id::text
       OR (v_task - 'task_version') IS DISTINCT FROM
            ((v_scope.scope -> 'task') - 'task_version') THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'source_changed');
    END IF;
    RETURN aap_labeling_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object('input_id',v_scope.input_id,'scope',v_scope.scope,
            'created_at',v_scope.created_at,'accepted_evidence',v_scope.accepted_evidence,
            'prepare_error',v_scope.prepare_error),NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'job_busy');
WHEN OTHERS THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.project_video_tracker_input(uuid,uuid,uuid,uuid,text,boolean,boolean,boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.capture_video_tracker_input(
    p_actor_id uuid,p_tracker_job_id uuid,p_grid_step integer
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_job public.video_tracker_jobs%ROWTYPE;
    v_preflight_task_id uuid;
    v_preflight_actor_id uuid;
    v_task jsonb;
    v_source jsonb;
    v_reply jsonb;
    v_data jsonb;
    v_prompt jsonb;
    v_expected jsonb;
    v_annotations jsonb := '[]'::jsonb;
    v_correction_mask jsonb := 'null'::jsonb;
    v_annotation_id uuid;
    v_annotation_version integer;
    v_annotation record;
    v_execution jsonb;
    v_scope jsonb;
    v_without_digest jsonb;
    v_request_digest text;
    v_prompt_digest text;
    v_input_id uuid;
    v_existing public.video_tracker_input_scopes%ROWTYPE;
    v_expected_entry record;
    v_expected_version integer;
    v_expected_count integer;
    v_mask jsonb;
    v_mask_digest text;
    v_error text;
BEGIN
    IF p_actor_id IS NULL OR p_tracker_job_id IS NULL
       OR p_grid_step IS NULL OR p_grid_step NOT BETWEEN 1 AND 2147483647 THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    -- Reject non-origin callers and prove current Task access before taking
    -- the canonical Job lock. The Task is rechecked after that lock.
    SELECT job.task_id,job.created_by
    INTO v_preflight_task_id,v_preflight_actor_id
    FROM public.video_tracker_jobs AS job
    WHERE job.id=p_tracker_job_id;
    IF NOT FOUND THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    IF v_preflight_actor_id IS DISTINCT FROM p_actor_id THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'permission_denied');
    END IF;
    v_reply := aap_labeling_v1.read_video_input_task_source(
        v_preflight_task_id,p_actor_id
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-input-source/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_error := v_reply #>> '{error,code}';
        IF v_error IN ('invalid_request','account_not_active','account_busy',
            'invalid_account_role','task_busy','task_not_visible','permission_denied',
            'source_missing','source_changed','source_not_ready','not_video') THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_error);
        END IF;
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR v_reply #>> '{data,binding,task_id}' IS DISTINCT FROM v_preflight_task_id::text THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    SELECT job.id, job.task_id, job.dataset_item_id, job.annotation_id, job.segment_id,
        job.target_class_name, job.target_tool_unit_id, job.created_by, job.status, job.job_kind,
        job.track_id_snapshot, job.correction_frame, job.model_key, job.direction, job.from_frame,
        job.to_frame, job.prompt, job.staged_result, job.revision, job.cancel_requested_at,
        job.started_at, job.created_at, job.completed_at
    INTO v_job.id, v_job.task_id, v_job.dataset_item_id, v_job.annotation_id, v_job.segment_id,
        v_job.target_class_name, v_job.target_tool_unit_id, v_job.created_by, v_job.status, v_job.job_kind,
        v_job.track_id_snapshot, v_job.correction_frame, v_job.model_key, v_job.direction, v_job.from_frame,
        v_job.to_frame, v_job.prompt, v_job.staged_result, v_job.revision, v_job.cancel_requested_at,
        v_job.started_at, v_job.created_at, v_job.completed_at
    FROM public.video_tracker_jobs AS job
    WHERE job.id=p_tracker_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    IF v_job.task_id IS DISTINCT FROM v_preflight_task_id THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_scope_changed');
    END IF;
    IF v_job.created_by IS DISTINCT FROM p_actor_id THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'permission_denied');
    END IF;
    SELECT scope.* INTO v_existing FROM public.video_tracker_input_scopes AS scope
    WHERE scope.tracker_job_id=v_job.id FOR UPDATE NOWAIT;
    IF FOUND THEN
        IF (v_existing.scope #>> '{execution,grid_step}')::integer IS DISTINCT FROM p_grid_step
           OR NOT aap_labeling_v1._video_tracker_request_matches(v_existing.scope,v_job) THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
        END IF;
        v_reply := aap_labeling_v1.read_video_input_task_source(v_job.task_id,p_actor_id);
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-input-source/v1'
           OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_error := COALESCE(v_reply #>> '{error,code}','invalid_response');
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_error);
        END IF;
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object('input_id',v_existing.input_id,'scope',v_existing.scope,
                'status',CASE WHEN v_existing.prepare_error IS NULL THEN 'pending' ELSE 'failed' END,
                'plan',NULL,'error',v_existing.prepare_error,
                'created_at',v_existing.created_at,'retire_after',NULL),NULL);
    END IF;
    IF v_job.status IS DISTINCT FROM 'queued' OR v_job.started_at IS NOT NULL
       OR v_job.cancel_requested_at IS NOT NULL THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'job_not_active');
    END IF;
    IF v_job.job_kind NOT IN ('tracking','correction')
       OR v_job.from_frame IS NULL OR v_job.to_frame IS NULL
       OR v_job.from_frame<0 OR v_job.to_frame<v_job.from_frame
       OR v_job.direction NOT IN ('forward','backward','bidirectional')
       OR v_job.model_key IS NULL OR pg_catalog.char_length(v_job.model_key) NOT BETWEEN 1 AND 80
       OR v_job.task_id IS NULL OR v_job.dataset_item_id IS NULL THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    v_reply := aap_labeling_v1.read_video_input_task_source(v_job.task_id,p_actor_id);
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-input-source/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_error := v_reply #>> '{error,code}';
        IF v_error IN ('invalid_request','account_not_active','account_busy',
            'invalid_account_role','task_busy','task_not_visible','permission_denied',
            'source_missing','source_changed','source_not_ready','not_video') THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_error);
        END IF;
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR v_reply #>> '{data,binding,task_id}' IS DISTINCT FROM v_job.task_id::text
       OR v_reply #>> '{data,binding,dataset_item_id}' IS DISTINCT FROM v_job.dataset_item_id::text
       OR v_reply #>> '{data,binding,file_type}' IS DISTINCT FROM 'video'
       OR v_reply #> '{data,upload_ready}' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_scope_changed');
    END IF;
    v_task := v_reply #> '{data,binding}';
    v_reply := aap_assets_v1.read_video_tracker_task_source(p_actor_id,v_job.task_id,v_task);
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-video-tracker-inputs/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_error := COALESCE(v_reply #>> '{error,code}','invalid_response');
        IF v_error IN ('invalid_request','account_not_active','account_busy',
            'invalid_account_role','task_busy','task_not_visible','permission_denied',
            'source_missing','source_changed','source_not_ready','not_video','source_busy') THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_error);
        END IF;
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    v_data := v_reply -> 'data';
    IF v_data #>> '{source_facts,dataset_item_id}' IS DISTINCT FROM v_job.dataset_item_id::text
       OR v_data #>> '{source_facts,file_type}' IS DISTINCT FROM 'video' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'source_changed');
    END IF;
    v_source := v_data -> 'source_facts';
    v_expected := v_job.prompt -> 'expected_source_versions';
    IF v_expected IS NULL THEN v_expected := '{}'::jsonb; END IF;
    IF pg_catalog.jsonb_typeof(v_expected) IS DISTINCT FROM 'object' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'source_changed');
    END IF;
    SELECT pg_catalog.count(*) INTO v_expected_count
    FROM pg_catalog.jsonb_object_keys(v_expected);
    IF v_expected_count>256 THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'resource_budget_exceeded');
    END IF;
    FOR v_expected_entry IN
        SELECT entry.key,entry.value FROM pg_catalog.jsonb_each(v_expected) AS entry
        ORDER BY entry.key
    LOOP
        IF v_expected_entry.key !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_expected_entry.value) IS DISTINCT FROM 'number'
           OR (v_expected_entry.value #>> '{}') !~ '^[1-9][0-9]*$' THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'source_changed');
        END IF;
        v_annotation_id := v_expected_entry.key::uuid;
        v_expected_version := (v_expected_entry.value #>> '{}')::integer;
        SELECT annotation.id,annotation.task_id,annotation.version,annotation.geometry,
            annotation.track_id,annotation.is_active,annotation.was_cancelled,
            annotation.video_segment_id
        INTO v_annotation
        FROM public.annotations AS annotation
        WHERE annotation.id=v_annotation_id FOR SHARE NOWAIT;
        IF NOT FOUND OR v_annotation.task_id<>v_job.task_id
           OR NOT v_annotation.is_active OR v_annotation.was_cancelled
           OR v_annotation.version IS DISTINCT FROM v_expected_version
           OR v_annotation.geometry IS NULL THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'source_changed');
        END IF;
        v_annotations := v_annotations || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'annotation_id',v_annotation.id,'version',v_annotation.version,
                'frame_index',v_job.from_frame,'geometry',v_annotation.geometry,
                'geometry_digest',pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                    v_annotation.geometry::text,'UTF8')),'hex')
            )
        );
    END LOOP;
    IF v_job.annotation_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_each(v_expected) AS entry
        WHERE entry.key=v_job.annotation_id::text
    ) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'source_changed');
    END IF;
    IF v_job.job_kind='correction' THEN
        IF v_job.annotation_id IS NULL OR v_job.correction_frame IS NULL
           OR (v_job.prompt #>> '{correction,source_annotation_id}') IS DISTINCT FROM v_job.annotation_id::text THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'source_changed');
        END IF;
        SELECT annotation.id,annotation.task_id,annotation.version,annotation.geometry,
            annotation.track_id,annotation.is_active,annotation.was_cancelled,
            annotation.video_segment_id
        INTO v_annotation
        FROM public.annotations AS annotation
        WHERE annotation.id=v_job.annotation_id FOR SHARE NOWAIT;
        IF NOT FOUND OR NOT v_annotation.is_active OR v_annotation.was_cancelled THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'source_changed');
        END IF;
        SELECT keyframe.value -> 'mask' INTO v_mask
        FROM pg_catalog.jsonb_array_elements(
            CASE WHEN pg_catalog.jsonb_typeof(v_annotation.geometry->'keyframes')='array'
                THEN v_annotation.geometry->'keyframes' ELSE '[]'::jsonb END
        ) AS keyframe(value)
        WHERE keyframe.value->>'frame_index'=v_job.correction_frame::text
          AND keyframe.value->>'source'='manual';
        v_mask_digest := v_mask->>'sha256';
        IF pg_catalog.jsonb_typeof(v_mask) IS DISTINCT FROM 'object'
           OR v_mask_digest IS NULL OR v_mask_digest !~ '^[0-9a-f]{64}$'
           OR v_mask_digest IS DISTINCT FROM v_job.prompt #>> '{correction,corrected_digest}' THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'source_changed');
        END IF;
        v_correction_mask := pg_catalog.jsonb_build_object(
            'annotation_id',v_annotation.id,'annotation_version',v_annotation.version,
            'frame_index',v_job.correction_frame,'mask',v_mask,'mask_digest',v_mask_digest
        );
    END IF;
    v_prompt := v_job.prompt - ARRAY['expected_source_versions','touched_annotation_ids','review_state'];
    IF pg_catalog.jsonb_typeof(v_prompt #> '{correction,routing}')='object' THEN
        v_prompt := pg_catalog.jsonb_set(v_prompt,'{correction,routing}',
            (v_prompt #> '{correction,routing}')-'execution_backend_id',true);
    END IF;
    IF pg_catalog.octet_length(pg_catalog.convert_to(v_prompt::text,'UTF8'))>65536 THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'resource_budget_exceeded');
    END IF;
    v_execution := pg_catalog.jsonb_build_object(
        'job_kind',v_job.job_kind,'model_key',v_job.model_key,'direction',v_job.direction,
        'from_frame',v_job.from_frame,'to_frame',v_job.to_frame,'grid_step',p_grid_step,
        'correction_frame',v_job.correction_frame,'segment_id',v_job.segment_id,
        'track_id_snapshot',v_job.track_id_snapshot,'target_class_name',v_job.target_class_name,
        'target_tool_unit_id',v_job.target_tool_unit_id,'prompt_snapshot',v_prompt,
        'source_annotations',v_annotations,'correction_mask',v_correction_mask
    );
    v_prompt_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object('prompt',v_prompt,'source_annotations',v_annotations,
            'correction_mask',v_correction_mask)::text,'UTF8')),'hex');
    v_scope := pg_catalog.jsonb_build_object(
        'tracker_job_id',v_job.id,'origin_actor_id',p_actor_id,'task',v_task,
        'source_facts',v_source,'execution',v_execution,'prompt_digest',v_prompt_digest
    );
    v_without_digest := v_scope;
    v_request_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(v_without_digest::text,'UTF8')),'hex');
    v_scope := v_scope || pg_catalog.jsonb_build_object('request_digest',v_request_digest);
    IF pg_catalog.octet_length(pg_catalog.convert_to(v_scope::text,'UTF8'))>2097152 THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'resource_budget_exceeded');
    END IF;
    v_input_id := pg_catalog.gen_random_uuid();
    INSERT INTO public.video_tracker_input_scopes(
        tracker_job_id,input_id,task_id,project_id,origin_actor_id,request_digest,prompt_digest,scope
    ) VALUES(v_job.id,v_input_id,v_job.task_id,(v_task->>'project_id')::uuid,p_actor_id,
        v_request_digest,v_prompt_digest,v_scope)
    RETURNING * INTO v_existing;
    RETURN aap_labeling_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object('input_id',v_existing.input_id,'scope',v_existing.scope,
            'status','pending','plan',NULL,'error',NULL,'created_at',v_existing.created_at,
            'retire_after',NULL),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'job_busy');
WHEN OTHERS THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.capture_video_tracker_input(uuid,uuid,integer) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.fail_video_tracker_input_preparation(
    p_actor_id uuid,p_tracker_job_id uuid,p_task_id uuid,p_input_id uuid,
    p_request_digest text,p_error_code text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_reply jsonb;
    v_code text;
    v_scope jsonb;
    v_created_at timestamptz;
BEGIN
    IF p_error_code IS NULL OR p_error_code NOT IN ('source_missing','source_changed','source_not_ready','not_video',
        'task_scope_changed','timebase_changed','input_expired','input_corrupt',
        'resource_budget_exceeded') THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    v_reply := aap_labeling_v1.project_video_tracker_input(
        p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,true,false,true
    );
    IF v_reply IS NULL OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_reply;
    END IF;
    v_scope := v_reply #> '{data,scope}';
    v_created_at := (v_reply #>> '{data,created_at}')::timestamptz;
    UPDATE public.video_tracker_input_scopes AS scope
    SET prepare_error=COALESCE(scope.prepare_error,p_error_code)
    WHERE scope.tracker_job_id=p_tracker_job_id AND scope.input_id=p_input_id
      AND scope.request_digest=p_request_digest
    RETURNING scope.scope,scope.created_at,scope.prepare_error
        INTO v_scope,v_created_at,v_code;
    IF NOT FOUND THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_scope_mismatch'); END IF;
    RETURN aap_labeling_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object('input_id',p_input_id,'scope',v_scope,'status','failed',
            'plan',NULL,'error',v_code,'created_at',v_created_at,'retire_after',NULL),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'job_busy');
WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS v_code=MESSAGE_TEXT;
    RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.fail_video_tracker_input_preparation(uuid,uuid,uuid,uuid,text,text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._video_tracker_input_record(
    p_input_id uuid,p_scope jsonb,p_scope_created_at timestamptz,p_prepare_error text,
    p_input public.asset_accepted_video_inputs
)
RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'input_id',p_input_id,
        'scope',p_scope,
        'status',CASE WHEN (p_input).input_id IS NOT NULL THEN (p_input).status
            WHEN p_prepare_error IS NOT NULL THEN 'failed' ELSE 'pending' END,
        'plan',CASE WHEN (p_input).input_id IS NOT NULL THEN (p_input).plan ELSE NULL END,
        'error',CASE WHEN (p_input).input_id IS NOT NULL THEN (p_input).error_code
            ELSE p_prepare_error END,
        'created_at',CASE WHEN (p_input).input_id IS NOT NULL THEN (p_input).created_at
            ELSE p_scope_created_at END,
        'retire_after',(p_input).retire_after
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_tracker_input_record(uuid,jsonb,timestamptz,text,public.asset_accepted_video_inputs) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.read_video_tracker_input(
    p_actor_id uuid,p_tracker_job_id uuid,p_task_id uuid
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_reply jsonb;
    v_code text;
    v_data jsonb;
    v_scope jsonb;
    v_source jsonb;
    v_created_at timestamptz;
    v_prepare_error text;
    v_input public.asset_accepted_video_inputs%ROWTYPE;
BEGIN
    IF p_actor_id IS NULL OR p_tracker_job_id IS NULL OR p_task_id IS NULL THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    v_reply := aap_labeling_v1.project_video_tracker_input(
        p_actor_id,p_tracker_job_id,p_task_id,NULL,NULL,false,false,false
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    v_code := CASE WHEN v_reply -> 'ok'='true'::jsonb THEN NULL ELSE v_reply #>> '{error,code}' END;
    IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_code); END IF;
    v_data := v_reply -> 'data';
    v_scope := v_data -> 'scope';
    v_created_at := (v_data ->> 'created_at')::timestamptz;
    v_prepare_error := v_data ->> 'prepare_error';
    v_reply := aap_assets_v1.read_video_tracker_task_source(
        p_actor_id,p_task_id,v_scope -> 'task'
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-video-tracker-inputs/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := COALESCE(v_reply #>> '{error,code}','invalid_response');
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_code);
    END IF;
    v_source := v_reply #> '{data,source_facts}';
    IF (v_source - 'object_identity') IS DISTINCT FROM
        ((v_scope -> 'source_facts') - 'object_identity')
       OR (v_scope #> '{source_facts,object_identity}'<>'null'::jsonb
           AND v_scope #> '{source_facts,object_identity}' IS DISTINCT FROM v_source -> 'object_identity') THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'source_changed');
    END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=(v_data ->> 'input_id')::uuid
    FOR SHARE NOWAIT;
    IF FOUND THEN
        IF v_input.input_owner IS DISTINCT FROM 'tracker'
           OR v_input.tracker_job_id IS DISTINCT FROM p_tracker_job_id
           OR v_input.job_id IS DISTINCT FROM p_tracker_job_id
           OR v_input.task_id IS DISTINCT FROM p_task_id
           OR v_input.task IS DISTINCT FROM v_scope -> 'task' THEN
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
        END IF;
    END IF;
    RETURN aap_assets_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object(
            'record',aap_assets_v1._video_tracker_input_record(
                (v_data ->> 'input_id')::uuid,v_scope,v_created_at,v_prepare_error,v_input
            ),
            'source_facts',v_source
        ),NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_video_tracker_input(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.prepare_video_tracker_input(
    p_actor_id uuid,p_tracker_job_id uuid,p_task_id uuid,p_input_id uuid,
    p_request_digest text,p_source_snapshot text,p_timetable_revision bigint,
    p_object_identity jsonb,p_terminal_error text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_reply jsonb;
    v_code text;
    v_data jsonb;
    v_scope jsonb;
    v_task jsonb;
    v_captured_source jsonb;
    v_source jsonb;
    v_identity jsonb;
    v_video_source jsonb;
    v_sampling jsonb;
    v_created_at timestamptz;
    v_prepare_error text;
    v_input_id uuid;
    v_item public.dataset_items%ROWTYPE;
    v_input public.asset_accepted_video_inputs%ROWTYPE;
BEGIN
    IF p_actor_id IS NULL OR p_tracker_job_id IS NULL OR p_task_id IS NULL
       OR p_input_id IS NULL OR p_request_digest IS NULL OR p_request_digest !~ '^[0-9a-f]{64}$'
       OR ((p_object_identity IS NULL) = (p_terminal_error IS NULL))
       OR (p_object_identity IS NOT NULL AND (
            NOT aap_assets_v1._video_input_identity_valid(p_object_identity)
            OR p_source_snapshot IS NULL OR p_source_snapshot !~ '^[0-9a-f]{64}$'
            OR p_timetable_revision IS NULL OR p_timetable_revision NOT BETWEEN 0 AND 9007199254740991
       ))
       OR (p_terminal_error IS NOT NULL AND p_terminal_error NOT IN (
            'source_missing','source_changed','source_not_ready','not_video',
            'task_scope_changed','timebase_changed','input_expired','input_corrupt',
            'resource_budget_exceeded'
       )) THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    v_reply := aap_labeling_v1.project_video_tracker_input(
        p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,true,false,true
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_reply #>> '{error,code}');
    END IF;
    v_data := v_reply -> 'data';
    v_scope := v_data -> 'scope';
    v_task := v_scope -> 'task';
    v_captured_source := v_scope -> 'source_facts';
    v_input_id := (v_data ->> 'input_id')::uuid;
    v_created_at := (v_data ->> 'created_at')::timestamptz;
    v_prepare_error := v_data ->> 'prepare_error';
    IF v_input_id<>p_input_id THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    IF v_prepare_error IS NOT NULL THEN
        SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_id=p_input_id AND input.input_owner='tracker';
        RETURN aap_assets_v1._video_tracker_input_reply(
            aap_assets_v1._video_tracker_input_record(
                p_input_id,v_scope,v_created_at,v_prepare_error,v_input
            ),NULL);
    END IF;
    IF p_terminal_error IS NOT NULL THEN
        v_reply := aap_labeling_v1.fail_video_tracker_input_preparation(
            p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,p_terminal_error
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
           OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_assets_v1._video_tracker_input_reply(
                NULL,COALESCE(v_reply #>> '{error,code}','invalid_response'));
        END IF;
        RETURN aap_assets_v1._video_tracker_input_reply(v_reply -> 'data',NULL);
    END IF;
    IF p_source_snapshot IS DISTINCT FROM v_captured_source ->> 'source_snapshot'
       OR p_timetable_revision::text IS DISTINCT FROM v_captured_source ->> 'timetable_revision'
       OR (p_object_identity ->> 'byte_size')::bigint=0 THEN
        v_code := CASE WHEN (p_object_identity ->> 'byte_size')::bigint=0
            THEN 'source_missing' ELSE 'source_changed' END;
        v_reply := aap_labeling_v1.fail_video_tracker_input_preparation(
            p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,v_code
        );
        IF v_reply -> 'ok'='true'::jsonb THEN
            RETURN aap_assets_v1._video_tracker_input_reply(v_reply -> 'data',NULL);
        END IF;
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,COALESCE(v_reply #>> '{error,code}','invalid_response'));
    END IF;
    v_reply := aap_assets_v1.read_video_tracker_task_source(p_actor_id,p_task_id,v_task);
    IF v_reply IS NULL OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := COALESCE(v_reply #>> '{error,code}','invalid_response');
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_code);
    END IF;
    v_source := v_reply #> '{data,source_facts}';
    IF (v_source - 'object_identity') IS DISTINCT FROM
        (v_captured_source - 'object_identity')
       OR (v_captured_source -> 'object_identity'<>'null'::jsonb
           AND v_source -> 'object_identity' IS DISTINCT FROM v_captured_source -> 'object_identity')
       OR (v_source -> 'object_identity'<>'null'::jsonb
           AND v_source -> 'object_identity' IS DISTINCT FROM p_object_identity)
       OR ((v_source -> 'file_size'<>'null'::jsonb)
           AND (v_source ->> 'file_size')::bigint<>(p_object_identity ->> 'byte_size')::bigint) THEN
        v_reply := aap_labeling_v1.fail_video_tracker_input_preparation(
            p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,'source_changed'
        );
        IF v_reply -> 'ok'='true'::jsonb THEN
            RETURN aap_assets_v1._video_tracker_input_reply(v_reply -> 'data',NULL);
        END IF;
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,COALESCE(v_reply #>> '{error,code}','invalid_response'));
    END IF;
    IF v_source -> 'object_identity'='null'::jsonb THEN
        v_reply := aap_assets_v1._media_reconcile_source(
            (v_source ->> 'dataset_item_id')::uuid,p_source_snapshot,p_object_identity,NULL
        );
        IF v_reply IS NULL OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-media/v1'
           OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := COALESCE(v_reply #>> '{error,code}','invalid_response');
            IF v_code IN ('source_missing','source_changed') THEN
                v_reply := aap_labeling_v1.fail_video_tracker_input_preparation(
                    p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,v_code
                );
                IF v_reply -> 'ok'='true'::jsonb THEN
                    RETURN aap_assets_v1._video_tracker_input_reply(v_reply -> 'data',NULL);
                END IF;
            END IF;
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_code);
        END IF;
        v_reply := aap_assets_v1.read_video_tracker_task_source(p_actor_id,p_task_id,v_task);
        IF v_reply IS NULL OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,COALESCE(v_reply #>> '{error,code}','invalid_response'));
        END IF;
        v_source := v_reply #> '{data,source_facts}';
        IF (v_source - 'object_identity') IS DISTINCT FROM
            (v_captured_source - 'object_identity')
           OR v_source -> 'object_identity' IS DISTINCT FROM p_object_identity THEN
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,'source_changed');
        END IF;
    END IF;
    IF NOT aap_assets_v1._video_input_source_valid(v_source,true)
       OR v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
       OR v_source -> 'object_identity' IS DISTINCT FROM p_object_identity THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'source_changed');
    END IF;
    v_video_source := v_source - ARRAY[
        'source_drifted','file_path','file_type','file_size','content_hash'
    ];
    v_identity := v_source -> 'object_identity';
    v_sampling := pg_catalog.jsonb_build_object(
        'max_frames',2147483647,
        'step',(v_scope #>> '{execution,grid_step}')::integer,
        'width',1280,'format','jpeg'
    );
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.job_id=p_tracker_job_id AND input.task_id=p_task_id
    FOR UPDATE NOWAIT;
    IF FOUND THEN
        IF v_input.input_id IS DISTINCT FROM p_input_id OR v_input.input_owner IS DISTINCT FROM 'tracker'
           OR v_input.tracker_job_id IS DISTINCT FROM p_tracker_job_id
           OR v_input.actor_id IS DISTINCT FROM (v_scope ->> 'origin_actor_id')::uuid
           OR v_input.task IS DISTINCT FROM v_task OR v_input.source_facts IS DISTINCT FROM v_source
           OR v_input.sampling IS DISTINCT FROM v_sampling THEN
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
        END IF;
        UPDATE public.asset_accepted_video_inputs AS input
        SET request_actor_id=p_actor_id,updated_at=pg_catalog.clock_timestamp()
        WHERE input.input_id=p_input_id RETURNING * INTO v_input;
    ELSE
        INSERT INTO public.asset_accepted_video_inputs(
            input_id,job_id,task_id,project_id,actor_id,request_actor_id,task,source,
            source_facts,sampling,input_owner,tracker_job_id
        ) VALUES(
            p_input_id,p_tracker_job_id,p_task_id,(v_task->>'project_id')::uuid,
            (v_scope->>'origin_actor_id')::uuid,p_actor_id,v_task,v_video_source,
            v_source,v_sampling,'tracker',p_tracker_job_id
        ) RETURNING * INTO v_input;
    END IF;
    RETURN aap_assets_v1._video_tracker_input_reply(
        aap_assets_v1._video_tracker_input_record(p_input_id,v_scope,v_created_at,NULL,v_input),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected OR unique_violation THEN
    RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_video_tracker_input(uuid,uuid,uuid,uuid,text,text,bigint,jsonb,text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1.reference_video_tracker_source(
    p_actor_id uuid,p_tracker_job_id uuid,p_task_id uuid,p_input_id uuid,
    p_request_digest text,p_plan_digest text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_reply jsonb;
    v_code text;
    v_data jsonb;
    v_scope jsonb;
    v_source jsonb;
    v_created_at timestamptz;
    v_prepare_error text;
    v_input public.asset_accepted_video_inputs%ROWTYPE;
BEGIN
    IF p_actor_id IS NULL OR p_tracker_job_id IS NULL OR p_task_id IS NULL
       OR p_input_id IS NULL OR p_request_digest IS NULL OR p_request_digest !~ '^[0-9a-f]{64}$'
       OR p_plan_digest IS NULL OR p_plan_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    v_reply := aap_labeling_v1.project_video_tracker_input(
        p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,true,false,true
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_reply #>> '{error,code}');
    END IF;
    v_data := v_reply -> 'data';
    v_scope := v_data -> 'scope';
    v_created_at := (v_data ->> 'created_at')::timestamptz;
    v_prepare_error := v_data ->> 'prepare_error';
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=p_input_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._video_tracker_input_reply(
            NULL,COALESCE(v_prepare_error,'input_not_ready'));
    END IF;
    IF v_input.input_owner IS DISTINCT FROM 'tracker'
       OR v_input.tracker_job_id IS DISTINCT FROM p_tracker_job_id
       OR v_input.job_id IS DISTINCT FROM p_tracker_job_id
       OR v_input.task_id IS DISTINCT FROM p_task_id
       OR v_input.request_actor_id IS DISTINCT FROM p_actor_id THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    IF v_input.status='expired' THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_expired'); END IF;
    IF v_input.status='failed' THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_input.error_code); END IF;
    IF v_input.status<>'ready' OR v_input.plan IS NULL THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_not_ready');
    END IF;
    IF v_input.plan ->> 'plan_digest' IS DISTINCT FROM p_plan_digest THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    v_reply := aap_assets_v1.read_video_tracker_task_source(p_actor_id,p_task_id,v_scope -> 'task');
    IF v_reply IS NULL OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,COALESCE(v_reply #>> '{error,code}','invalid_response'));
    END IF;
    v_source := v_reply #> '{data,source_facts}';
    IF (v_source - 'object_identity') IS DISTINCT FROM
        ((v_scope -> 'source_facts') - 'object_identity')
       OR v_source -> 'object_identity' IS DISTINCT FROM v_input.source #> '{object_identity}'
       OR v_source -> 'object_identity' IS DISTINCT FROM v_input.plan #> '{input,source,object_identity}'
       OR v_input.plan #>> '{input,job_id}' IS DISTINCT FROM p_tracker_job_id::text
       OR v_input.plan #>> '{input,actor_id}' IS DISTINCT FROM (v_scope ->> 'origin_actor_id')
       OR v_input.plan #> '{input,task}' IS DISTINCT FROM v_scope -> 'task'
       OR v_input.plan #> '{input,blob,version_id}' IS NULL
       OR v_input.plan #>> '{input,blob,version_id}' IN ('','null') THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'source_changed');
    END IF;
    RETURN aap_assets_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object(
            'input',aap_assets_v1._video_tracker_input_record(
                p_input_id,v_scope,v_created_at,NULL,v_input
            ),
            'source_facts',v_source
        ),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_tracker_input_reply(NULL,'reference_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.reference_video_tracker_source(uuid,uuid,uuid,uuid,text,text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._video_tracker_input_current_source(
    p_input public.asset_accepted_video_inputs,p_lock boolean,p_require_active boolean
)
RETURNS text LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_reply jsonb;
    v_scope jsonb;
    v_source jsonb;
    v_code text;
BEGIN
    IF (p_input).input_id IS NULL OR (p_input).input_owner IS DISTINCT FROM 'tracker'
       OR (p_input).tracker_job_id IS DISTINCT FROM (p_input).job_id
       OR p_lock IS NULL OR p_require_active IS NULL THEN RETURN 'input_scope_mismatch'; END IF;
    v_reply := aap_labeling_v1.project_video_tracker_input(
        (p_input).request_actor_id,(p_input).tracker_job_id,(p_input).task_id,
        (p_input).input_id,NULL,p_require_active,false,p_lock
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN 'invalid_response';
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        RETURN CASE WHEN v_code IN ('invalid_request','account_not_active','account_busy',
            'invalid_account_role','task_busy','task_not_visible','permission_denied',
            'source_missing','source_changed','source_not_ready','job_busy','job_not_active',
            'input_missing','input_scope_mismatch') THEN v_code ELSE 'invalid_response' END;
    END IF;
    v_scope := v_reply #> '{data,scope}';
    IF v_reply #>> '{data,input_id}' IS DISTINCT FROM (p_input).input_id::text
       OR v_scope ->> 'origin_actor_id' IS DISTINCT FROM (p_input).actor_id::text
       OR v_scope ->> 'request_digest' !~ '^[0-9a-f]{64}$'
       OR (v_scope -> 'task') IS DISTINCT FROM (p_input).task THEN
        RETURN 'input_scope_mismatch';
    END IF;
    v_reply := aap_assets_v1.read_video_tracker_task_source(
        (p_input).request_actor_id,(p_input).task_id,v_scope -> 'task'
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-video-tracker-inputs/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := COALESCE(v_reply #>> '{error,code}','invalid_response');
        RETURN CASE WHEN v_code IN ('invalid_request','account_not_active','account_busy',
            'invalid_account_role','task_busy','task_not_visible','permission_denied',
            'source_missing','source_changed','source_not_ready','source_busy',
            'task_scope_changed','not_video') THEN v_code ELSE 'invalid_response' END;
    END IF;
    v_source := v_reply #> '{data,source_facts}';
    IF (v_source - 'object_identity') IS DISTINCT FROM
        ((p_input).source_facts - 'object_identity')
       OR v_source -> 'object_identity' IS DISTINCT FROM (p_input).source_facts -> 'object_identity'
       OR v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
       OR v_source -> 'file_type' IS DISTINCT FROM '"video"'::jsonb THEN
        RETURN 'source_changed';
    END IF;
    IF (p_input).source -> 'object_identity' IS DISTINCT FROM v_source -> 'object_identity'
       OR (p_input).source -> 'source_snapshot' IS DISTINCT FROM v_source -> 'source_snapshot'
       OR (p_input).source -> 'timetable_revision' IS DISTINCT FROM v_source -> 'timetable_revision' THEN
        RETURN 'source_changed';
    END IF;
    RETURN NULL;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN 'source_busy';
WHEN OTHERS THEN RETURN 'invalid_response';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_tracker_input_current_source(public.asset_accepted_video_inputs,boolean,boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_assets_v1._lock_video_tracker_input_plan(
    p_actor_id uuid,p_tracker_job_id uuid,p_task_id uuid,p_input_id uuid,
    p_request_digest text,p_plan_digest text,p_require_active boolean
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_reply jsonb;
    v_code text;
    v_data jsonb;
    v_scope jsonb;
    v_source jsonb;
    v_created_at timestamptz;
    v_input public.asset_accepted_video_inputs%ROWTYPE;
BEGIN
    IF p_actor_id IS NULL OR p_tracker_job_id IS NULL OR p_task_id IS NULL
       OR p_input_id IS NULL OR p_request_digest IS NULL OR p_request_digest !~ '^[0-9a-f]{64}$'
       OR p_plan_digest IS NULL OR p_plan_digest !~ '^[0-9a-f]{64}$'
       OR p_require_active IS NULL THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    v_reply := aap_labeling_v1.project_video_tracker_input(
        p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,
        p_require_active,false,true
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_reply #>> '{error,code}');
    END IF;
    v_data := v_reply -> 'data';
    v_scope := v_data -> 'scope';
    v_created_at := (v_data ->> 'created_at')::timestamptz;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    IF v_input.input_owner IS DISTINCT FROM 'tracker'
       OR v_input.tracker_job_id IS DISTINCT FROM p_tracker_job_id
       OR v_input.task_id IS DISTINCT FROM p_task_id
       OR v_input.job_id IS DISTINCT FROM p_tracker_job_id THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    IF v_input.status='expired' THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_expired'); END IF;
    IF v_input.status='failed' THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_input.error_code); END IF;
    IF v_input.status<>'ready' OR v_input.plan IS NULL THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_not_ready');
    END IF;
    IF v_input.plan ->> 'plan_digest' IS DISTINCT FROM p_plan_digest THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    v_reply := aap_assets_v1.read_video_tracker_task_source(
        p_actor_id,p_task_id,v_scope -> 'task'
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-video-tracker-inputs/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN ('invalid_request','account_not_active','account_busy',
            'invalid_account_role','task_busy','task_not_visible','permission_denied',
            'source_missing','source_changed','source_not_ready','source_busy',
            'task_scope_changed','not_video') THEN
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_code);
        END IF;
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    v_source := v_reply #> '{data,source_facts}';
    IF v_source IS DISTINCT FROM v_input.source_facts
       OR NOT aap_assets_v1._video_input_source_valid(v_source,true)
       OR v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
       OR v_source -> 'object_identity' IS DISTINCT FROM v_input.source -> 'object_identity'
       OR v_input.source -> 'source_snapshot' IS DISTINCT FROM v_source -> 'source_snapshot'
       OR v_input.source -> 'timetable_revision' IS DISTINCT FROM v_source -> 'timetable_revision' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'source_changed');
    END IF;
    RETURN aap_assets_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object(
            'input',aap_assets_v1._video_tracker_input_record(
                p_input_id,v_scope,v_created_at,NULL,v_input
            ),
            'source_facts',v_source
        ),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._lock_video_tracker_input_plan(uuid,uuid,uuid,uuid,text,text,boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._video_tracker_input_evidence(
    p_scope jsonb,p_record jsonb
)
RETURNS jsonb LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema','aap-assets-video-tracker-input-evidence/v1',
        'tracker_job_id',p_scope->'tracker_job_id',
        'input_id',p_record->'input_id',
        'task_id',p_scope #> '{task,task_id}',
        'project_id',p_scope #> '{task,project_id}',
        'request_digest',p_scope->'request_digest',
        'prompt_digest',p_scope->'prompt_digest',
        'plan_digest',p_record #> '{plan,plan_digest}',
        'timebase_digest',p_record #> '{plan,timebase_digest}',
        'source_version_id',p_record #> '{plan,input,blob,version_id}',
        'source_sha256',p_record #> '{plan,input,blob,sha256}',
        'from_frame',p_scope #> '{execution,from_frame}',
        'to_frame',p_scope #> '{execution,to_frame}',
        'direction',p_scope #> '{execution,direction}',
        'grid_step',p_scope #> '{execution,grid_step}'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_tracker_input_evidence(jsonb,jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._video_tracker_request_matches(
    p_scope jsonb,p_job public.video_tracker_jobs
)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_prompt jsonb := (p_job).prompt - ARRAY['expected_source_versions','touched_annotation_ids','review_state'];
    v_execution jsonb := p_scope -> 'execution';
BEGIN
    IF pg_catalog.jsonb_typeof(v_execution) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(v_prompt) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    IF pg_catalog.jsonb_typeof(v_prompt #> '{correction,routing}')='object' THEN
        v_prompt := pg_catalog.jsonb_set(v_prompt,'{correction,routing}',
            (v_prompt #> '{correction,routing}')-'execution_backend_id',true);
    END IF;
    RETURN (p_job).job_kind IS NOT DISTINCT FROM v_execution->>'job_kind'
       AND (p_job).model_key IS NOT DISTINCT FROM v_execution->>'model_key'
       AND (p_job).direction IS NOT DISTINCT FROM v_execution->>'direction'
       AND (p_job).from_frame IS NOT DISTINCT FROM (v_execution->>'from_frame')::integer
       AND (p_job).to_frame IS NOT DISTINCT FROM (v_execution->>'to_frame')::integer
       AND (p_job).correction_frame IS NOT DISTINCT FROM
            CASE WHEN v_execution->'correction_frame'='null'::jsonb THEN NULL
                 ELSE (v_execution->>'correction_frame')::integer END
       AND (p_job).segment_id IS NOT DISTINCT FROM
            CASE WHEN v_execution->'segment_id'='null'::jsonb THEN NULL
                 ELSE (v_execution->>'segment_id')::uuid END
       AND (p_job).track_id_snapshot IS NOT DISTINCT FROM
            CASE WHEN v_execution->'track_id_snapshot'='null'::jsonb THEN NULL
                 ELSE v_execution->>'track_id_snapshot' END
       AND (p_job).target_class_name IS NOT DISTINCT FROM
            CASE WHEN v_execution->'target_class_name'='null'::jsonb THEN NULL
                 ELSE v_execution->>'target_class_name' END
       AND (p_job).target_tool_unit_id IS NOT DISTINCT FROM
            CASE WHEN v_execution->'target_tool_unit_id'='null'::jsonb THEN NULL
                 ELSE v_execution->>'target_tool_unit_id' END
       AND v_prompt IS NOT DISTINCT FROM v_execution->'prompt_snapshot';
EXCEPTION WHEN OTHERS THEN RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_tracker_request_matches(jsonb,public.video_tracker_jobs) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._video_tracker_source_annotations_current(
    p_scope jsonb,p_job public.video_tracker_jobs,p_review_mutated boolean
)
RETURNS text LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_expected jsonb;
    v_expected_count integer;
    v_source jsonb;
    v_annotation record;
    v_entry jsonb;
    v_output record;
    v_expected_version integer;
    v_count integer := 0;
BEGIN
    v_source := p_scope #> '{execution,source_annotations}';
    IF pg_catalog.jsonb_typeof(v_source) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(v_source)>256 THEN RETURN 'input_corrupt'; END IF;
    IF p_review_mutated THEN
        v_expected := (p_job).prompt -> 'expected_source_versions';
        IF v_expected IS NULL THEN v_expected := '{}'::jsonb; END IF;
        IF pg_catalog.jsonb_typeof(v_expected) IS DISTINCT FROM 'object' THEN
            RETURN 'source_changed';
        END IF;
        SELECT pg_catalog.count(*) INTO v_expected_count
        FROM pg_catalog.jsonb_object_keys(v_expected);
        IF v_expected_count<pg_catalog.jsonb_array_length(v_source) THEN
            RETURN 'source_changed';
        END IF;
        -- Partial review also versions discovered output tracks. Only outputs
        -- already pinned to this exact input may extend the mutable version map.
        FOR v_output IN SELECT entry.key,entry.value FROM pg_catalog.jsonb_each(v_expected) AS entry
            WHERE NOT EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(v_source) AS source(value)
                WHERE source.value->>'annotation_id'=entry.key
            )
        LOOP
            IF v_output.key !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(v_output.value) IS DISTINCT FROM 'number'
               OR v_output.value::text !~ '^[1-9][0-9]*$'
               OR v_output.value::numeric>2147483647 THEN RETURN 'source_changed'; END IF;
            SELECT annotation.id,annotation.task_id,annotation.version,
                annotation.is_active,annotation.was_cancelled
            INTO v_annotation FROM public.annotations AS annotation
            WHERE annotation.id=v_output.key::uuid FOR SHARE NOWAIT;
            IF NOT FOUND OR v_annotation.task_id<>(p_job).task_id
               OR v_annotation.version::numeric<>v_output.value::numeric
               OR NOT v_annotation.is_active OR v_annotation.was_cancelled
               OR NOT EXISTS (
                   SELECT 1 FROM public.annotation_video_tracker_input_sources AS pin
                   WHERE pin.annotation_id=v_annotation.id AND pin.tracker_job_id=(p_job).id
                     AND pin.input_id=(SELECT scope.input_id
                         FROM public.video_tracker_input_scopes AS scope
                         WHERE scope.tracker_job_id=(p_job).id
                           AND scope.request_digest=p_scope->>'request_digest')
               ) THEN RETURN 'source_changed'; END IF;
        END LOOP;
    END IF;
    FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(v_source) LOOP
        v_count := v_count+1;
        IF pg_catalog.jsonb_typeof(v_entry->'annotation_id') IS DISTINCT FROM 'string'
           OR v_entry->>'annotation_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_entry->'version') IS DISTINCT FROM 'number'
           OR (v_entry->>'version') !~ '^[1-9][0-9]*$'
           OR (v_entry->>'version')::numeric>2147483647
           OR pg_catalog.jsonb_typeof(v_entry->'frame_index') IS DISTINCT FROM 'number'
           OR (v_entry->>'frame_index') !~ '^(0|[1-9][0-9]*)$'
           OR (v_entry->>'frame_index')::numeric>2147483647
           OR pg_catalog.jsonb_typeof(v_entry->'geometry') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(v_entry->'geometry_digest') IS DISTINCT FROM 'string'
           OR v_entry->>'geometry_digest' !~ '^[0-9a-f]{64}$' THEN
            RETURN 'input_corrupt';
        END IF;
        IF p_review_mutated THEN
            IF NOT (v_expected ? (v_entry->>'annotation_id'))
               OR pg_catalog.jsonb_typeof(v_expected->(v_entry->>'annotation_id')) IS DISTINCT FROM 'number'
               OR (v_expected->>(v_entry->>'annotation_id')) !~ '^[1-9][0-9]*$' THEN
                RETURN 'source_changed';
            END IF;
            v_expected_version := (v_expected->>(v_entry->>'annotation_id'))::integer;
        ELSE
            v_expected_version := (v_entry->>'version')::integer;
        END IF;
        SELECT annotation.id,annotation.task_id,annotation.version,annotation.geometry,
            annotation.is_active,annotation.was_cancelled
        INTO v_annotation FROM public.annotations AS annotation
        WHERE annotation.id=(v_entry->>'annotation_id')::uuid FOR SHARE NOWAIT;
        IF NOT FOUND OR v_annotation.task_id<>(p_job).task_id
           OR v_annotation.version IS DISTINCT FROM v_expected_version
           OR NOT v_annotation.is_active OR v_annotation.was_cancelled THEN
            RETURN 'source_changed';
        END IF;
        IF NOT p_review_mutated AND pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            v_annotation.geometry::text,'UTF8')),'hex') IS DISTINCT FROM v_entry->>'geometry_digest' THEN
            RETURN 'source_changed';
        END IF;
    END LOOP;
    RETURN NULL;
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN 'task_busy';
WHEN OTHERS THEN RETURN 'invalid_response';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_tracker_source_annotations_current(jsonb,public.video_tracker_jobs,boolean) FROM PUBLIC;
-- aap-statement-break

-- The caller has already locked current project access in this transaction.
-- Keep the older media-access envelope unchanged for its frozen consumers.
CREATE FUNCTION aap_workspace_v1.read_media_video_collaboration_policy(p_project_id uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_config jsonb;
    v_enabled jsonb;
    v_overlap jsonb;
BEGIN
    SELECT project.video_collaboration INTO v_config FROM public.projects AS project
    WHERE project.id=p_project_id FOR SHARE NOWAIT;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-workspace-video-collaboration-policy/v1','ok',false,
            'error',pg_catalog.jsonb_build_object('code','project_missing'));
    END IF;
    IF v_config IS NULL OR v_config='null'::jsonb THEN v_config := '{}'::jsonb; END IF;
    v_enabled := COALESCE(v_config->'enabled','false'::jsonb);
    v_overlap := COALESCE(v_config->'overlap_frames','0'::jsonb);
    IF pg_catalog.jsonb_typeof(v_config) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(v_enabled) IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_overlap) IS DISTINCT FROM 'number'
       OR v_overlap::text !~ '^(0|[1-9][0-9]*)$'
       OR (v_enabled='true'::jsonb AND v_overlap::numeric<1) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-workspace-video-collaboration-policy/v1','ok',false,
            'error',pg_catalog.jsonb_build_object('code','invalid_response'));
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-workspace-video-collaboration-policy/v1','ok',true,
        'data',pg_catalog.jsonb_build_object(
            'project_id',p_project_id,'enabled',v_enabled,'overlap_frames',v_overlap));
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-workspace-video-collaboration-policy/v1','ok',false,
        'error',pg_catalog.jsonb_build_object('code','task_busy'));
WHEN OTHERS THEN
    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-workspace-video-collaboration-policy/v1','ok',false,
        'error',pg_catalog.jsonb_build_object('code','invalid_response'));
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.read_media_video_collaboration_policy(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.lock_video_tracker_input_for_stage(
    p_actor_id uuid,p_tracker_job_id uuid,p_task_id uuid,p_input_id uuid,
    p_request_digest text,p_plan_digest text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
    v_scope jsonb;
    v_job public.video_tracker_jobs%ROWTYPE;
    v_segment record;
    v_access jsonb;
    v_access_data jsonb;
    v_material jsonb;
    v_source_code text;
    v_evidence jsonb;
    v_privileged boolean;
    v_collaboration boolean;
    v_policy jsonb;
    v_overlap numeric;
    v_segment_count bigint;
    v_work_start bigint;
    v_work_end bigint;
    v_frame_count bigint;
BEGIN
    v_reply := aap_labeling_v1.project_video_tracker_input(
        p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,false,false,true
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_reply #>> '{error,code}');
    END IF;
    v_data := v_reply -> 'data';
    v_scope := v_data -> 'scope';
    SELECT job.id, job.task_id, job.dataset_item_id, job.annotation_id, job.segment_id,
        job.target_class_name, job.target_tool_unit_id, job.created_by, job.status, job.job_kind,
        job.track_id_snapshot, job.correction_frame, job.model_key, job.direction, job.from_frame,
        job.to_frame, job.prompt, job.staged_result, job.revision, job.cancel_requested_at,
        job.started_at, job.created_at, job.completed_at
    INTO v_job.id, v_job.task_id, v_job.dataset_item_id, v_job.annotation_id, v_job.segment_id,
        v_job.target_class_name, v_job.target_tool_unit_id, v_job.created_by, v_job.status, v_job.job_kind,
        v_job.track_id_snapshot, v_job.correction_frame, v_job.model_key, v_job.direction, v_job.from_frame,
        v_job.to_frame, v_job.prompt, v_job.staged_result, v_job.revision, v_job.cancel_requested_at,
        v_job.started_at, v_job.created_at, v_job.completed_at
    FROM public.video_tracker_jobs AS job
    WHERE job.id=p_tracker_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_job.created_by IS DISTINCT FROM p_actor_id
       OR NOT (
           v_job.status='running'
           OR (v_job.status='cancelled' AND v_job.job_kind='tracking' AND v_job.started_at IS NOT NULL)
       ) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'job_not_active');
    END IF;
    v_source_code := aap_labeling_v1._video_tracker_source_annotations_current(v_scope,v_job,false);
    IF v_source_code IS NOT NULL THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_source_code);
    END IF;
    v_access := aap_workspace_v1.lock_media_project_access(p_actor_id,(v_scope #>> '{task,project_id}')::uuid);
    IF v_access IS NULL OR v_access->>'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(v_access->'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_access->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_source_code := v_access #>> '{error,code}';
        IF v_source_code IN ('account_not_active','account_busy','invalid_account_role','task_busy') THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_source_code);
        END IF;
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'permission_denied');
    END IF;
    v_access_data := v_access->'data';
    IF v_access_data->>'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_access_data->>'project_id' IS DISTINCT FROM v_scope #>> '{task,project_id}'
       OR pg_catalog.jsonb_typeof(v_access_data->'is_manager') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data->'is_super_admin') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data->'video_collaboration_enabled') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    v_privileged := v_access_data->'is_manager'='true'::jsonb
        OR v_access_data->'is_super_admin'='true'::jsonb;
    v_collaboration := v_access_data->'video_collaboration_enabled'='true'::jsonb;
    v_material := aap_assets_v1._lock_video_tracker_input_plan(
        p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,p_plan_digest,false
    );
    IF v_material IS NULL OR v_material->>'schema' IS DISTINCT FROM 'aap-assets-video-tracker-inputs/v1'
       OR v_material->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            NULL,COALESCE(v_material #>> '{error,code}','invalid_response'));
    END IF;
    IF v_job.segment_id IS NOT NULL THEN
        SELECT segment.dataset_item_id,segment.start_frame,segment.end_frame,segment.segment_index,
            segment.assignee_id,segment.locked_by,segment.lock_expires_at
        INTO v_segment FROM public.video_segments AS segment
        WHERE segment.id=v_job.segment_id FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_segment.dataset_item_id<>(v_scope #>> '{source_facts,dataset_item_id}')::uuid THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_scope_changed');
        END IF;
        v_policy := aap_workspace_v1.read_media_video_collaboration_policy(
            (v_scope #>> '{task,project_id}')::uuid);
        IF v_policy IS NULL
           OR v_policy->>'schema' IS DISTINCT FROM 'aap-workspace-video-collaboration-policy/v1'
           OR pg_catalog.jsonb_typeof(v_policy->'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
        END IF;
        IF v_policy->'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,
                CASE WHEN v_policy #>> '{error,code}'='task_busy' THEN 'task_busy' ELSE 'invalid_response' END);
        END IF;
        v_policy := v_policy->'data';
        IF pg_catalog.jsonb_typeof(v_policy) IS DISTINCT FROM 'object'
           OR v_policy-ARRAY['project_id','enabled','overlap_frames']::text[]<>'{}'::jsonb
           OR v_policy->>'project_id' IS DISTINCT FROM v_scope #>> '{task,project_id}'
           OR v_policy->'enabled' IS DISTINCT FROM pg_catalog.to_jsonb(v_collaboration)
           OR pg_catalog.jsonb_typeof(v_policy->'overlap_frames') IS DISTINCT FROM 'number'
           OR v_policy->>'overlap_frames' !~ '^(0|[1-9][0-9]*)$' THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
        END IF;
        v_work_start := v_segment.start_frame;
        v_work_end := v_segment.end_frame;
        IF v_collaboration THEN
            v_overlap := (v_policy->>'overlap_frames')::numeric;
            v_frame_count := (v_material #>> '{data,input,plan,input,video,frame_count}')::bigint;
            SELECT pg_catalog.count(segment.dataset_item_id) INTO v_segment_count
            FROM public.video_segments AS segment
            WHERE segment.dataset_item_id=v_segment.dataset_item_id;
            IF v_overlap<1 OR v_frame_count IS NULL OR v_frame_count<1
               OR v_segment.segment_index<0 OR v_segment.segment_index>=v_segment_count THEN
                RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_scope_changed');
            END IF;
            v_work_start := GREATEST(0::numeric,v_segment.start_frame::numeric-
                CASE WHEN v_segment.segment_index=0 THEN 0 ELSE pg_catalog.floor(v_overlap/2) END)::bigint;
            v_work_end := LEAST(GREATEST(0,v_frame_count-1)::numeric,v_segment.end_frame::numeric+
                CASE WHEN v_segment.segment_index=v_segment_count-1 THEN 0
                    ELSE v_overlap-pg_catalog.floor(v_overlap/2) END)::bigint;
        END IF;
        IF v_job.from_frame<v_work_start OR v_job.to_frame>v_work_end THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_scope_changed');
        END IF;
        IF (v_collaboration OR NOT v_privileged)
           AND (v_segment.locked_by IS DISTINCT FROM p_actor_id
                OR v_segment.lock_expires_at IS NULL
                OR v_segment.lock_expires_at<=pg_catalog.clock_timestamp()) THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_busy');
        END IF;
        IF v_collaboration AND v_segment.assignee_id IS DISTINCT FROM p_actor_id THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'permission_denied');
        END IF;
    END IF;
    v_evidence := aap_labeling_v1._video_tracker_input_evidence(
        v_scope,v_material #> '{data,input}'
    );
    RETURN aap_labeling_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object('input',v_material #> '{data,input}','evidence',v_evidence),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_busy');
WHEN OTHERS THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_video_tracker_input_for_stage(uuid,uuid,uuid,uuid,text,text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.lock_video_tracker_input_for_accept(
    p_actor_id uuid,p_tracker_job_id uuid,p_task_id uuid,p_input_id uuid,
    p_request_digest text,p_plan_digest text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
    v_scope_row public.video_tracker_input_scopes%ROWTYPE;
    v_scope jsonb;
    v_job public.video_tracker_jobs%ROWTYPE;
    v_segment record;
    v_access jsonb;
    v_access_data jsonb;
    v_material jsonb;
    v_source_code text;
    v_evidence jsonb;
    v_privileged boolean;
    v_collaboration boolean;
    v_policy jsonb;
    v_overlap numeric;
    v_segment_count bigint;
    v_work_start bigint;
    v_work_end bigint;
    v_frame_count bigint;
BEGIN
    v_reply := aap_labeling_v1.project_video_tracker_input(
        p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,false,true,true
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_reply #>> '{error,code}');
    END IF;
    v_data := v_reply -> 'data';
    v_scope := v_data -> 'scope';
    SELECT scope.* INTO v_scope_row FROM public.video_tracker_input_scopes AS scope
    WHERE scope.tracker_job_id=p_tracker_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    SELECT job.id, job.task_id, job.dataset_item_id, job.annotation_id, job.segment_id,
        job.target_class_name, job.target_tool_unit_id, job.created_by, job.status, job.job_kind,
        job.track_id_snapshot, job.correction_frame, job.model_key, job.direction, job.from_frame,
        job.to_frame, job.prompt, job.staged_result, job.revision, job.cancel_requested_at,
        job.started_at, job.created_at, job.completed_at
    INTO v_job.id, v_job.task_id, v_job.dataset_item_id, v_job.annotation_id, v_job.segment_id,
        v_job.target_class_name, v_job.target_tool_unit_id, v_job.created_by, v_job.status, v_job.job_kind,
        v_job.track_id_snapshot, v_job.correction_frame, v_job.model_key, v_job.direction, v_job.from_frame,
        v_job.to_frame, v_job.prompt, v_job.staged_result, v_job.revision, v_job.cancel_requested_at,
        v_job.started_at, v_job.created_at, v_job.completed_at
    FROM public.video_tracker_jobs AS job
    WHERE job.id=p_tracker_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_job.status NOT IN ('pending_review','partially_reviewed')
       AND NOT (v_job.status='cancelled' AND v_job.job_kind='tracking'
           AND v_job.staged_result IS NOT NULL
           AND COALESCE(v_job.completed_at,v_job.created_at)+interval '24 hours'>pg_catalog.clock_timestamp())
       OR v_job.staged_result IS NULL
       OR COALESCE(v_job.completed_at,v_job.created_at)+interval '24 hours'<=pg_catalog.clock_timestamp() THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'job_not_active');
    END IF;
    IF NOT aap_labeling_v1._video_tracker_request_matches(v_scope,v_job) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    v_source_code := aap_labeling_v1._video_tracker_source_annotations_current(v_scope,v_job,true);
    IF v_source_code IS NOT NULL THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_source_code);
    END IF;
    v_access := aap_workspace_v1.lock_media_project_access(p_actor_id,(v_scope #>> '{task,project_id}')::uuid);
    IF v_access IS NULL OR v_access->>'schema' IS DISTINCT FROM 'aap-workspace-media-access/v1'
       OR pg_catalog.jsonb_typeof(v_access->'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    IF v_access->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_source_code := v_access #>> '{error,code}';
        IF v_source_code IN ('account_not_active','account_busy','invalid_account_role','task_busy') THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_source_code);
        END IF;
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'permission_denied');
    END IF;
    v_access_data := v_access->'data';
    IF v_access_data->>'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_access_data->>'project_id' IS DISTINCT FROM v_scope #>> '{task,project_id}'
       OR pg_catalog.jsonb_typeof(v_access_data->'is_manager') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data->'is_super_admin') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(v_access_data->'video_collaboration_enabled') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    END IF;
    v_privileged := v_access_data->'is_manager'='true'::jsonb
        OR v_access_data->'is_super_admin'='true'::jsonb;
    v_collaboration := v_access_data->'video_collaboration_enabled'='true'::jsonb;
    v_material := aap_assets_v1._lock_video_tracker_input_plan(
        p_actor_id,p_tracker_job_id,p_task_id,p_input_id,p_request_digest,p_plan_digest,false
    );
    IF v_material IS NULL OR v_material->>'schema' IS DISTINCT FROM 'aap-assets-video-tracker-inputs/v1'
       OR v_material->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            NULL,COALESCE(v_material #>> '{error,code}','invalid_response'));
    END IF;
    IF v_job.segment_id IS NOT NULL THEN
        SELECT segment.dataset_item_id,segment.start_frame,segment.end_frame,segment.segment_index,
            segment.assignee_id,segment.locked_by,segment.lock_expires_at
        INTO v_segment FROM public.video_segments AS segment
        WHERE segment.id=v_job.segment_id FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_segment.dataset_item_id<>(v_scope #>> '{source_facts,dataset_item_id}')::uuid THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_scope_changed');
        END IF;
        v_policy := aap_workspace_v1.read_media_video_collaboration_policy(
            (v_scope #>> '{task,project_id}')::uuid);
        IF v_policy IS NULL
           OR v_policy->>'schema' IS DISTINCT FROM 'aap-workspace-video-collaboration-policy/v1'
           OR pg_catalog.jsonb_typeof(v_policy->'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
        END IF;
        IF v_policy->'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,
                CASE WHEN v_policy #>> '{error,code}'='task_busy' THEN 'task_busy' ELSE 'invalid_response' END);
        END IF;
        v_policy := v_policy->'data';
        IF pg_catalog.jsonb_typeof(v_policy) IS DISTINCT FROM 'object'
           OR v_policy-ARRAY['project_id','enabled','overlap_frames']::text[]<>'{}'::jsonb
           OR v_policy->>'project_id' IS DISTINCT FROM v_scope #>> '{task,project_id}'
           OR v_policy->'enabled' IS DISTINCT FROM pg_catalog.to_jsonb(v_collaboration)
           OR pg_catalog.jsonb_typeof(v_policy->'overlap_frames') IS DISTINCT FROM 'number'
           OR v_policy->>'overlap_frames' !~ '^(0|[1-9][0-9]*)$' THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
        END IF;
        v_work_start := v_segment.start_frame;
        v_work_end := v_segment.end_frame;
        IF v_collaboration THEN
            v_overlap := (v_policy->>'overlap_frames')::numeric;
            v_frame_count := (v_material #>> '{data,input,plan,input,video,frame_count}')::bigint;
            SELECT pg_catalog.count(segment.dataset_item_id) INTO v_segment_count
            FROM public.video_segments AS segment
            WHERE segment.dataset_item_id=v_segment.dataset_item_id;
            IF v_overlap<1 OR v_frame_count IS NULL OR v_frame_count<1
               OR v_segment.segment_index<0 OR v_segment.segment_index>=v_segment_count THEN
                RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_scope_changed');
            END IF;
            v_work_start := GREATEST(0::numeric,v_segment.start_frame::numeric-
                CASE WHEN v_segment.segment_index=0 THEN 0 ELSE pg_catalog.floor(v_overlap/2) END)::bigint;
            v_work_end := LEAST(GREATEST(0,v_frame_count-1)::numeric,v_segment.end_frame::numeric+
                CASE WHEN v_segment.segment_index=v_segment_count-1 THEN 0
                    ELSE v_overlap-pg_catalog.floor(v_overlap/2) END)::bigint;
        END IF;
        IF v_job.from_frame<v_work_start OR v_job.to_frame>v_work_end THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_scope_changed');
        END IF;
        IF (v_collaboration OR NOT v_privileged)
           AND (v_segment.locked_by IS DISTINCT FROM p_actor_id
                OR v_segment.lock_expires_at IS NULL
                OR v_segment.lock_expires_at<=pg_catalog.clock_timestamp()) THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_busy');
        END IF;
        IF v_collaboration AND v_segment.assignee_id IS DISTINCT FROM p_actor_id THEN
            RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'permission_denied');
        END IF;
    END IF;
    v_evidence := aap_labeling_v1._video_tracker_input_evidence(
        v_scope,v_material #> '{data,input}'
    );
    IF v_scope_row.accepted_evidence IS NOT NULL
       AND v_scope_row.accepted_evidence IS DISTINCT FROM v_evidence THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    UPDATE public.video_tracker_input_scopes AS scope
    SET accepted_evidence=COALESCE(scope.accepted_evidence,v_evidence)
    WHERE scope.tracker_job_id=p_tracker_job_id;
    RETURN aap_labeling_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object('input',v_material #> '{data,input}','evidence',v_evidence),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_busy');
WHEN OTHERS THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_video_tracker_input_for_accept(uuid,uuid,uuid,uuid,text,text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._video_tracker_evidence_valid(p_evidence jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RETURN COALESCE(
        pg_catalog.jsonb_typeof(p_evidence)='object'
        AND (p_evidence-ARRAY['schema','tracker_job_id','input_id','task_id','project_id',
            'request_digest','prompt_digest','plan_digest','timebase_digest','source_version_id',
            'source_sha256','from_frame','to_frame','direction','grid_step'])='{}'::jsonb
        AND p_evidence ?& ARRAY['schema','tracker_job_id','input_id','task_id','project_id',
            'request_digest','prompt_digest','plan_digest','timebase_digest','source_version_id',
            'source_sha256','from_frame','to_frame','direction','grid_step']
        AND p_evidence->>'schema'='aap-assets-video-tracker-input-evidence/v1'
        AND pg_catalog.jsonb_typeof(p_evidence->'tracker_job_id')='string'
        AND p_evidence->>'tracker_job_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND pg_catalog.jsonb_typeof(p_evidence->'input_id')='string'
        AND p_evidence->>'input_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND pg_catalog.jsonb_typeof(p_evidence->'task_id')='string'
        AND p_evidence->>'task_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND pg_catalog.jsonb_typeof(p_evidence->'project_id')='string'
        AND p_evidence->>'project_id' ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        AND pg_catalog.jsonb_typeof(p_evidence->'request_digest')='string'
        AND p_evidence->>'request_digest' ~ '^[0-9a-f]{64}$'
        AND pg_catalog.jsonb_typeof(p_evidence->'prompt_digest')='string'
        AND p_evidence->>'prompt_digest' ~ '^[0-9a-f]{64}$'
        AND pg_catalog.jsonb_typeof(p_evidence->'plan_digest')='string'
        AND p_evidence->>'plan_digest' ~ '^[0-9a-f]{64}$'
        AND pg_catalog.jsonb_typeof(p_evidence->'timebase_digest')='string'
        AND p_evidence->>'timebase_digest' ~ '^[0-9a-f]{64}$'
        AND pg_catalog.jsonb_typeof(p_evidence->'source_sha256')='string'
        AND p_evidence->>'source_sha256' ~ '^[0-9a-f]{64}$'
        AND pg_catalog.jsonb_typeof(p_evidence->'source_version_id')='string'
        AND pg_catalog.char_length(p_evidence->>'source_version_id') BETWEEN 1 AND 255
        AND pg_catalog.btrim(p_evidence->>'source_version_id')<>''
        AND p_evidence->>'source_version_id'<>'null'
        AND pg_catalog.jsonb_typeof(p_evidence->'from_frame')='number'
        AND (p_evidence->>'from_frame') ~ '^(0|[1-9][0-9]*)$'
        AND (p_evidence->>'from_frame')::numeric<=2147483647
        AND pg_catalog.jsonb_typeof(p_evidence->'to_frame')='number'
        AND (p_evidence->>'to_frame') ~ '^(0|[1-9][0-9]*)$'
        AND (p_evidence->>'to_frame')::numeric<=2147483647
        AND (p_evidence->>'to_frame')::bigint>=(p_evidence->>'from_frame')::bigint
        AND pg_catalog.jsonb_typeof(p_evidence->'direction')='string'
        AND p_evidence->>'direction' IN ('forward','backward','bidirectional')
        AND pg_catalog.jsonb_typeof(p_evidence->'grid_step')='number'
        AND (p_evidence->>'grid_step') ~ '^[1-9][0-9]*$'
        AND (p_evidence->>'grid_step')::numeric<=2147483647,false);
EXCEPTION WHEN OTHERS THEN RETURN false;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_tracker_evidence_valid(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.attach_annotation_video_tracker_input_source(
    p_actor_id uuid,p_tracker_job_id uuid,p_annotation_id uuid,p_evidence jsonb
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_reply jsonb;
    v_code text;
    v_scope public.video_tracker_input_scopes%ROWTYPE;
    v_job public.video_tracker_jobs%ROWTYPE;
    v_annotation record;
    v_evidence jsonb;
    v_source jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_tracker_job_id IS NULL OR p_annotation_id IS NULL
       OR NOT aap_labeling_v1._video_tracker_evidence_valid(p_evidence) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    v_reply := aap_labeling_v1.project_video_tracker_input(
        p_actor_id,p_tracker_job_id,(p_evidence->>'task_id')::uuid,
        (p_evidence->>'input_id')::uuid,p_evidence->>'request_digest',false,false,true
    );
    IF v_reply IS NULL OR v_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,COALESCE(v_reply #>> '{error,code}','invalid_response'));
    END IF;
    SELECT scope.* INTO v_scope FROM public.video_tracker_input_scopes AS scope
    WHERE scope.tracker_job_id=p_tracker_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_scope.input_id<>(p_evidence->>'input_id')::uuid
       OR v_scope.accepted_evidence IS DISTINCT FROM p_evidence
       OR p_evidence->>'tracker_job_id' IS DISTINCT FROM p_tracker_job_id::text
       OR p_evidence->>'prompt_digest' IS DISTINCT FROM v_scope.prompt_digest THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    SELECT job.id, job.task_id, job.dataset_item_id, job.annotation_id, job.segment_id,
        job.target_class_name, job.target_tool_unit_id, job.created_by, job.status, job.job_kind,
        job.track_id_snapshot, job.correction_frame, job.model_key, job.direction, job.from_frame,
        job.to_frame, job.prompt, job.staged_result, job.revision, job.cancel_requested_at,
        job.started_at, job.created_at, job.completed_at
    INTO v_job.id, v_job.task_id, v_job.dataset_item_id, v_job.annotation_id, v_job.segment_id,
        v_job.target_class_name, v_job.target_tool_unit_id, v_job.created_by, v_job.status, v_job.job_kind,
        v_job.track_id_snapshot, v_job.correction_frame, v_job.model_key, v_job.direction, v_job.from_frame,
        v_job.to_frame, v_job.prompt, v_job.staged_result, v_job.revision, v_job.cancel_requested_at,
        v_job.started_at, v_job.created_at, v_job.completed_at
    FROM public.video_tracker_jobs AS job
    WHERE job.id=p_tracker_job_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_job.status NOT IN ('accepted','partially_reviewed')
       OR NOT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements_text(
                CASE WHEN pg_catalog.jsonb_typeof(v_job.prompt->'touched_annotation_ids')='array'
                    THEN v_job.prompt->'touched_annotation_ids' ELSE '[]'::jsonb END
            ) AS touched(value) WHERE touched.value=p_annotation_id::text
       ) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'permission_denied');
    END IF;
    v_reply := aap_labeling_v1.lock_video_input_task_source(
        v_scope.task_id,p_actor_id,v_scope.scope->'task'
    );
    IF v_reply IS NULL OR v_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := COALESCE(v_reply #>> '{error,code}','invalid_response');
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_code);
    END IF;
    SELECT annotation.id,annotation.task_id,annotation.project_id,annotation.is_active,annotation.was_cancelled
    INTO v_annotation FROM public.annotations AS annotation
    WHERE annotation.id=p_annotation_id FOR SHARE NOWAIT;
    IF NOT FOUND OR v_annotation.task_id<>v_scope.task_id
       OR v_annotation.project_id<>v_scope.project_id
       OR NOT v_annotation.is_active OR v_annotation.was_cancelled THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_scope_changed');
    END IF;
    INSERT INTO public.annotation_video_tracker_input_sources(
        annotation_id,tracker_job_id,input_id,task_id,project_id,request_digest,prompt_digest,
        plan_digest,timebase_digest,source_version_id,source_sha256,from_frame,to_frame,direction,grid_step
    ) VALUES(
        p_annotation_id,p_tracker_job_id,(p_evidence->>'input_id')::uuid,v_scope.task_id,v_scope.project_id,
        p_evidence->>'request_digest',p_evidence->>'prompt_digest',p_evidence->>'plan_digest',
        p_evidence->>'timebase_digest',p_evidence->>'source_version_id',p_evidence->>'source_sha256',
        (p_evidence->>'from_frame')::integer,(p_evidence->>'to_frame')::integer,
        p_evidence->>'direction',(p_evidence->>'grid_step')::integer
    ) ON CONFLICT(annotation_id,input_id) DO NOTHING;
    IF NOT EXISTS (
        SELECT 1 FROM public.annotation_video_tracker_input_sources AS pin
        WHERE pin.annotation_id=p_annotation_id AND pin.input_id=(p_evidence->>'input_id')::uuid
          AND pin.tracker_job_id=p_tracker_job_id AND pin.request_digest=p_evidence->>'request_digest'
          AND pin.plan_digest=p_evidence->>'plan_digest' AND pin.timebase_digest=p_evidence->>'timebase_digest'
          AND pin.source_version_id=p_evidence->>'source_version_id'
          AND pin.source_sha256=p_evidence->>'source_sha256'
    ) THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_corrupt'); END IF;
    RETURN aap_labeling_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object('annotation_id',p_annotation_id,'tracker_job_id',p_tracker_job_id,
            'input_id',p_evidence->'input_id','plan_digest',p_evidence->'plan_digest','attached',true),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_busy');
WHEN OTHERS THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.attach_annotation_video_tracker_input_source(uuid,uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.copy_annotation_video_tracker_input_sources(
    p_actor_id uuid,p_source_annotation_id uuid,p_target_annotation_id uuid
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_source record;
    v_target record;
    v_task_reply jsonb;
    v_lock_reply jsonb;
    v_code text;
    v_count integer;
BEGIN
    IF p_actor_id IS NULL OR p_source_annotation_id IS NULL OR p_target_annotation_id IS NULL THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    SELECT annotation.id,annotation.task_id,annotation.project_id,annotation.is_active,annotation.was_cancelled
    INTO v_source FROM public.annotations AS annotation
    WHERE annotation.id=p_source_annotation_id FOR SHARE NOWAIT;
    SELECT annotation.id,annotation.task_id,annotation.project_id,annotation.is_active,annotation.was_cancelled
    INTO v_target FROM public.annotations AS annotation
    WHERE annotation.id=p_target_annotation_id FOR SHARE NOWAIT;
    IF NOT FOUND OR v_source.id IS NULL OR v_target.id IS NULL
       OR v_source.task_id IS DISTINCT FROM v_target.task_id
       OR v_source.project_id IS DISTINCT FROM v_target.project_id
       OR NOT v_target.is_active OR v_target.was_cancelled THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_scope_changed');
    END IF;
    v_task_reply := aap_labeling_v1.read_video_input_task_source(v_source.task_id,p_actor_id);
    IF v_task_reply IS NULL OR v_task_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,COALESCE(v_task_reply #>> '{error,code}','invalid_response'));
    END IF;
    v_lock_reply := aap_labeling_v1.lock_video_input_task_source(
        v_source.task_id,p_actor_id,v_task_reply #> '{data,binding}'
    );
    IF v_lock_reply IS NULL OR v_lock_reply->'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := COALESCE(v_lock_reply #>> '{error,code}','invalid_response');
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,v_code);
    END IF;
    INSERT INTO public.annotation_video_tracker_input_sources(
        annotation_id,tracker_job_id,input_id,task_id,project_id,request_digest,prompt_digest,
        plan_digest,timebase_digest,source_version_id,source_sha256,from_frame,to_frame,direction,grid_step
    ) SELECT p_target_annotation_id,pin.tracker_job_id,pin.input_id,pin.task_id,pin.project_id,
        pin.request_digest,pin.prompt_digest,pin.plan_digest,pin.timebase_digest,pin.source_version_id,
        pin.source_sha256,pin.from_frame,pin.to_frame,pin.direction,pin.grid_step
    FROM public.annotation_video_tracker_input_sources AS pin
    WHERE pin.annotation_id=p_source_annotation_id
    ON CONFLICT(annotation_id,input_id) DO NOTHING;
    SELECT pg_catalog.count(*) INTO v_count
    FROM public.annotation_video_tracker_input_sources AS pin
    WHERE pin.annotation_id=p_source_annotation_id;
    IF EXISTS (
        SELECT 1 FROM public.annotation_video_tracker_input_sources AS source_pin
        JOIN public.annotation_video_tracker_input_sources AS target_pin
          ON target_pin.annotation_id=p_target_annotation_id AND target_pin.input_id=source_pin.input_id
        WHERE source_pin.annotation_id=p_source_annotation_id
          AND (source_pin.tracker_job_id,source_pin.request_digest,source_pin.plan_digest,
              source_pin.timebase_digest,source_pin.source_version_id,source_pin.source_sha256)
              IS DISTINCT FROM
              (target_pin.tracker_job_id,target_pin.request_digest,target_pin.plan_digest,
              target_pin.timebase_digest,target_pin.source_version_id,target_pin.source_sha256)
    ) THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'input_corrupt'); END IF;
    RETURN aap_labeling_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object('source_annotation_id',p_source_annotation_id,
            'target_annotation_id',p_target_annotation_id,'copied',v_count),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'task_busy');
WHEN OTHERS THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.copy_annotation_video_tracker_input_sources(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._video_tracker_input_source_immutable()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION USING ERRCODE='55000',
            MESSAGE='annotation tracker input evidence is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._video_tracker_input_source_immutable() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_annotation_video_tracker_input_source_immutable
BEFORE UPDATE ON public.annotation_video_tracker_input_sources
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._video_tracker_input_source_immutable();
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._propagate_video_tracker_input_pin(
    p_pin public.annotation_video_tracker_input_sources
)
RETURNS void LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_descendant record;
BEGIN
    FOR v_descendant IN
        WITH RECURSIVE descendants(annotation_id,project_id,path) AS (
            SELECT child.id,child.project_id,ARRAY[(p_pin).annotation_id,child.id]::uuid[]
            FROM public.annotations AS child
            WHERE child.parent_annotation_id=(p_pin).annotation_id
              AND child.project_id=(p_pin).project_id
            UNION ALL
            SELECT child.id,child.project_id,descendants.path||child.id
            FROM public.annotations AS child JOIN descendants
              ON child.parent_annotation_id=descendants.annotation_id
             AND child.project_id=descendants.project_id
            WHERE NOT child.id=ANY(descendants.path)
        ) SELECT annotation_id FROM descendants
    LOOP
        IF EXISTS (
            SELECT 1 FROM public.annotation_video_tracker_input_sources AS target
            WHERE target.annotation_id=v_descendant.annotation_id AND target.input_id=(p_pin).input_id
              AND (pg_catalog.to_jsonb(target)-ARRAY['annotation_id','created_at'])
                  IS DISTINCT FROM (pg_catalog.to_jsonb(p_pin)-ARRAY['annotation_id','created_at'])
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='annotation tracker evidence changed';
        END IF;
        INSERT INTO public.annotation_video_tracker_input_sources(
            annotation_id,tracker_job_id,input_id,task_id,project_id,request_digest,prompt_digest,
            plan_digest,timebase_digest,source_version_id,source_sha256,from_frame,to_frame,direction,grid_step
        ) VALUES (
            v_descendant.annotation_id,(p_pin).tracker_job_id,(p_pin).input_id,(p_pin).task_id,
            (p_pin).project_id,(p_pin).request_digest,(p_pin).prompt_digest,(p_pin).plan_digest,
            (p_pin).timebase_digest,(p_pin).source_version_id,(p_pin).source_sha256,
            (p_pin).from_frame,(p_pin).to_frame,(p_pin).direction,(p_pin).grid_step
        ) ON CONFLICT(annotation_id,input_id) DO NOTHING;
    END LOOP;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._propagate_video_tracker_input_pin(public.annotation_video_tracker_input_sources) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._inherit_annotation_video_tracker_input_sources()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_parent_project_id uuid;
    v_pin public.annotation_video_tracker_input_sources%ROWTYPE;
BEGIN
    IF NEW.parent_annotation_id IS NULL THEN RETURN NEW; END IF;
    SELECT parent.project_id INTO v_parent_project_id FROM public.annotations AS parent
    WHERE parent.id=NEW.parent_annotation_id;
    IF NOT FOUND OR NEW.task_id IS NULL OR NEW.project_id IS DISTINCT FROM v_parent_project_id THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='annotation tracker input lineage must stay within one project';
    END IF;
    FOR v_pin IN SELECT pin.* FROM public.annotation_video_tracker_input_sources AS pin
        WHERE pin.annotation_id=NEW.parent_annotation_id ORDER BY pin.input_id
    LOOP
        IF v_pin.project_id IS DISTINCT FROM NEW.project_id OR EXISTS (
            SELECT 1 FROM public.annotation_video_tracker_input_sources AS target
            WHERE target.annotation_id=NEW.id AND target.input_id=v_pin.input_id
              AND (pg_catalog.to_jsonb(target)-ARRAY['annotation_id','created_at'])
                  IS DISTINCT FROM (pg_catalog.to_jsonb(v_pin)-ARRAY['annotation_id','created_at'])
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='annotation tracker evidence changed';
        END IF;
        INSERT INTO public.annotation_video_tracker_input_sources(
            annotation_id,tracker_job_id,input_id,task_id,project_id,request_digest,prompt_digest,
            plan_digest,timebase_digest,source_version_id,source_sha256,from_frame,to_frame,direction,grid_step
        ) VALUES (
            NEW.id,v_pin.tracker_job_id,v_pin.input_id,v_pin.task_id,v_pin.project_id,
            v_pin.request_digest,v_pin.prompt_digest,v_pin.plan_digest,v_pin.timebase_digest,
            v_pin.source_version_id,v_pin.source_sha256,v_pin.from_frame,v_pin.to_frame,
            v_pin.direction,v_pin.grid_step
        ) ON CONFLICT(annotation_id,input_id) DO NOTHING;
        v_pin.annotation_id := NEW.id;
        PERFORM aap_labeling_v1._propagate_video_tracker_input_pin(v_pin);
    END LOOP;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._inherit_annotation_video_tracker_input_sources() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_inherit_annotation_video_tracker_input_sources
AFTER INSERT OR UPDATE OF parent_annotation_id ON public.annotations
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._inherit_annotation_video_tracker_input_sources();
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._propagate_annotation_video_tracker_input_source()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF pg_catalog.pg_trigger_depth()>1 THEN RETURN NEW; END IF;
    PERFORM aap_labeling_v1._propagate_video_tracker_input_pin(NEW);
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._propagate_annotation_video_tracker_input_source() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_propagate_annotation_video_tracker_input_source
AFTER INSERT ON public.annotation_video_tracker_input_sources
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._propagate_annotation_video_tracker_input_source();
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.video_tracker_input_referenced(p_input_id uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_scope public.video_tracker_input_scopes%ROWTYPE;
    v_job public.video_tracker_jobs%ROWTYPE;
    v_until timestamptz;
    v_referenced boolean := false;
BEGIN
    IF p_input_id IS NULL THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    SELECT scope.* INTO v_scope FROM public.video_tracker_input_scopes AS scope
    WHERE scope.input_id=p_input_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object('input_id',p_input_id,'referenced',false,'retained_until',NULL),NULL);
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.annotation_video_tracker_input_sources AS pin
        WHERE pin.input_id=p_input_id
    ) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object('input_id',p_input_id,'referenced',true,'retained_until',NULL),NULL);
    END IF;
    SELECT job.id, job.task_id, job.dataset_item_id, job.annotation_id, job.segment_id,
        job.target_class_name, job.target_tool_unit_id, job.created_by, job.status, job.job_kind,
        job.track_id_snapshot, job.correction_frame, job.model_key, job.direction, job.from_frame,
        job.to_frame, job.prompt, job.staged_result, job.revision, job.cancel_requested_at,
        job.started_at, job.created_at, job.completed_at
    INTO v_job.id, v_job.task_id, v_job.dataset_item_id, v_job.annotation_id, v_job.segment_id,
        v_job.target_class_name, v_job.target_tool_unit_id, v_job.created_by, v_job.status, v_job.job_kind,
        v_job.track_id_snapshot, v_job.correction_frame, v_job.model_key, v_job.direction, v_job.from_frame,
        v_job.to_frame, v_job.prompt, v_job.staged_result, v_job.revision, v_job.cancel_requested_at,
        v_job.started_at, v_job.created_at, v_job.completed_at
    FROM public.video_tracker_jobs AS job
    WHERE job.id=v_scope.tracker_job_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object('input_id',p_input_id,'referenced',false,'retained_until',NULL),NULL);
    END IF;
    v_until := COALESCE(v_job.completed_at,v_job.created_at)+interval '24 hours';
    IF v_job.status IN ('queued','running') THEN
        v_referenced := true;
        v_until := NULL;
    ELSIF v_job.status IN ('pending_review','partially_reviewed')
       AND v_job.staged_result IS NOT NULL AND v_until>pg_catalog.clock_timestamp() THEN
        v_referenced := true;
    ELSIF v_job.status='cancelled' AND v_job.job_kind='tracking'
       AND v_job.staged_result IS NOT NULL AND v_until>pg_catalog.clock_timestamp() THEN
        v_referenced := true;
    ELSE
        v_until := NULL;
    END IF;
    RETURN aap_labeling_v1._video_tracker_input_reply(
        pg_catalog.jsonb_build_object('input_id',p_input_id,'referenced',v_referenced,
            'retained_until',v_until),NULL);
EXCEPTION WHEN OTHERS THEN RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.video_tracker_input_referenced(uuid) FROM PUBLIC;
-- aap-statement-break

-- Tracker rows share the existing attempts/times/blob-GC ledger, but have an
-- owner-local retention predicate. This bounded sweep never advances the
-- manifest cursor or leaves tracker rows in the legacy batch candidate page.
CREATE FUNCTION aap_assets_v1.sweep_video_tracker_inputs(p_limit integer)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_candidate record;
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_reply jsonb;
    v_data jsonb;
    v_now timestamptz;
    v_not_before timestamptz;
    v_referenced boolean;
    v_retained_until timestamptz;
    v_expired integer := 0;
BEGIN
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    FOR v_candidate IN
        SELECT input.input_id
        FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_owner='tracker'
          AND (
              (input.status='ready' AND (
                  (input.retire_after IS NOT NULL AND input.retire_after<=pg_catalog.clock_timestamp())
                  OR input.retention_checked_at IS NULL
                  OR input.retention_checked_at<=pg_catalog.clock_timestamp()-interval '1 day'
              ))
              OR (input.status IN ('pending','materializing')
                  AND (input.lease_expires_at IS NULL OR input.lease_expires_at<=pg_catalog.clock_timestamp())
                  AND (input.retention_checked_at IS NULL
                       OR input.retention_checked_at<=pg_catalog.clock_timestamp()-interval '1 day'))
              OR (input.status='failed' AND (
                  input.retention_checked_at IS NULL OR (
                      input.retention_checked_at<=pg_catalog.clock_timestamp()-interval '1 day'
                      AND EXISTS (SELECT 1 FROM public.asset_accepted_video_input_attempts AS attempt
                          WHERE attempt.input_id=input.input_id AND attempt.status<>'expired')
                  )
              ))
          )
        ORDER BY input.retire_after NULLS FIRST,input.created_at,input.input_id
        LIMIT p_limit FOR UPDATE OF input SKIP LOCKED
    LOOP
        SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_id=v_candidate.input_id;
        v_now := pg_catalog.clock_timestamp();
        IF v_input.input_owner IS DISTINCT FROM 'tracker' THEN CONTINUE; END IF;
        IF v_input.lease_token IS NOT NULL AND v_input.lease_expires_at>v_now THEN CONTINUE; END IF;
        IF EXISTS (
            SELECT 1 FROM public.asset_accepted_video_input_attempts AS attempt
            WHERE attempt.input_id=v_input.input_id AND attempt.status='leased'
              AND attempt.lease_expires_at>v_now
        ) THEN CONTINUE; END IF;
        IF EXISTS (
            SELECT 1 FROM public.asset_accepted_input_garbage AS garbage
            WHERE garbage.lease_token IS NOT NULL AND garbage.lease_expires_at>v_now
              AND EXISTS (SELECT 1 FROM public.asset_accepted_video_input_attempts AS attempt
                  WHERE attempt.input_id=v_input.input_id AND attempt.prefix=garbage.prefix)
        ) THEN CONTINUE; END IF;
        v_reply := aap_labeling_v1.video_tracker_input_referenced(v_input.input_id);
        IF v_reply IS NULL
           OR v_reply->>'schema' IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
           OR v_reply->'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_reply #>> '{data,input_id}' IS DISTINCT FROM v_input.input_id::text
           OR pg_catalog.jsonb_typeof(v_reply #> '{data,referenced}') IS DISTINCT FROM 'boolean' THEN
            CONTINUE;
        END IF;
        v_data := v_reply->'data';
        v_referenced := v_data->'referenced'='true'::jsonb;
        v_retained_until := CASE WHEN v_data->'retained_until'='null'::jsonb
            THEN NULL ELSE (v_data->>'retained_until')::timestamptz END;
        IF v_referenced THEN
            UPDATE public.asset_accepted_video_inputs AS input
            SET retire_after=v_retained_until,retention_checked_at=v_now,updated_at=v_now
            WHERE input.input_id=v_input.input_id;
            CONTINUE;
        END IF;
        IF v_input.status='failed' THEN
            FOR v_attempt IN SELECT attempt.*
                FROM public.asset_accepted_video_input_attempts AS attempt
                WHERE attempt.input_id=v_input.input_id AND attempt.status<>'expired'
                ORDER BY attempt.generation
            LOOP
                v_not_before := GREATEST(v_now,v_attempt.lease_expires_at)+interval '24 hours';
                PERFORM aap_assets_v1._video_input_queue_prefix(v_attempt.prefix,'input_failed',v_not_before);
                UPDATE public.asset_accepted_video_input_attempts AS attempt
                SET status='expired',completed_at=COALESCE(attempt.completed_at,v_now)
                WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
            END LOOP;
            UPDATE public.asset_accepted_video_inputs AS input
            SET retention_checked_at=v_now,updated_at=v_now
            WHERE input.input_id=v_input.input_id;
            CONTINUE;
        END IF;
        IF v_input.status IN ('pending','materializing') THEN
            IF v_input.status='materializing' THEN
                SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
                WHERE attempt.input_id=v_input.input_id AND attempt.generation=v_input.generation
                  AND attempt.lease_token=v_input.lease_token AND attempt.status='leased'
                FOR UPDATE NOWAIT;
                IF FOUND THEN
                    PERFORM aap_assets_v1._video_input_queue_prefix(
                        v_attempt.prefix,'job_inactive',GREATEST(v_now,v_attempt.lease_expires_at)+interval '24 hours'
                    );
                    UPDATE public.asset_accepted_video_input_attempts AS attempt
                    SET status='abandoned',completed_at=v_now
                    WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
                END IF;
            END IF;
            UPDATE public.asset_accepted_video_inputs AS input
            SET status='failed',error_code='job_not_active',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                last_lease_token=COALESCE(v_input.lease_token,input.last_lease_token),
                last_outcome=CASE WHEN v_input.lease_token IS NULL THEN input.last_outcome ELSE 'lease_expired' END,
                retention_checked_at=v_now,updated_at=v_now
            WHERE input.input_id=v_input.input_id;
            CONTINUE;
        END IF;
        IF v_input.status<>'ready' OR v_input.plan IS NULL THEN CONTINUE; END IF;
        FOR v_attempt IN SELECT attempt.* FROM public.asset_accepted_video_input_attempts AS attempt
            WHERE attempt.input_id=v_input.input_id ORDER BY attempt.generation
        LOOP
            v_not_before := GREATEST(v_now,v_attempt.lease_expires_at)+interval '24 hours';
            PERFORM aap_assets_v1._video_input_queue_prefix(v_attempt.prefix,'input_expired',v_not_before);
            UPDATE public.asset_accepted_video_input_attempts AS attempt
            SET status='expired',completed_at=COALESCE(attempt.completed_at,v_now)
            WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
        END LOOP;
        FOR v_attempt IN SELECT attempt.* FROM public.asset_accepted_video_frame_attempts AS attempt
            WHERE attempt.input_id=v_input.input_id ORDER BY attempt.frame_id,attempt.generation
        LOOP
            v_not_before := GREATEST(v_now,v_attempt.lease_expires_at)+interval '24 hours';
            PERFORM aap_assets_v1._video_input_queue_prefix(v_attempt.prefix,'input_expired',v_not_before);
            UPDATE public.asset_accepted_video_frame_attempts AS attempt
            SET status='expired',completed_at=COALESCE(attempt.completed_at,v_now)
            WHERE attempt.frame_id=v_attempt.frame_id AND attempt.generation=v_attempt.generation;
        END LOOP;
        UPDATE public.asset_accepted_video_frames AS frame
        SET status='expired',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=v_now
        WHERE frame.input_id=v_input.input_id AND frame.status='ready';
        UPDATE public.asset_accepted_video_frames AS frame
        SET status='failed',error_code='input_expired',lease_owner=NULL,lease_token=NULL,
            lease_expires_at=NULL,updated_at=v_now
        WHERE frame.input_id=v_input.input_id AND frame.status IN ('pending','materializing');
        UPDATE public.asset_accepted_video_inputs AS input
        SET status='expired',retire_after=v_now,retention_checked_at=v_now,updated_at=v_now
        WHERE input.input_id=v_input.input_id;
        v_expired := v_expired+1;
    END LOOP;
    RETURN aap_assets_v1._video_tracker_input_reply(pg_catalog.jsonb_build_object('expired',v_expired),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.sweep_video_tracker_inputs(integer) FROM PUBLIC;
-- aap-statement-break

-- Shared attempt ledger, filtered to tracker-owned rows. Batch claims use only
-- manifest-owned rows; neither claimant can starve or consume the other's work.
CREATE FUNCTION aap_assets_v1.claim_video_tracker_input(
    p_worker_id uuid,p_input_id uuid,p_lease_seconds integer
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_candidate record;
    v_code text;
    v_now timestamptz;
    v_expires timestamptz;
    v_token uuid;
    v_generation bigint;
    v_prefix text;
    v_claim jsonb;
    v_expired boolean := false;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 60 AND 7200 THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    IF p_input_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=p_input_id
    ) THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    IF p_input_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_id=p_input_id AND input.input_owner IS DISTINCT FROM 'tracker'
    ) THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch'); END IF;
    FOR v_candidate IN
        SELECT input.input_id FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_owner='tracker'
          AND (p_input_id IS NULL OR input.input_id=p_input_id)
          AND (input.status='pending' OR (input.status='materializing'
              AND input.lease_expires_at<=pg_catalog.clock_timestamp()))
        ORDER BY input.created_at,input.input_id
        LIMIT CASE WHEN p_input_id IS NULL THEN 100 ELSE 1 END
        FOR UPDATE OF input SKIP LOCKED
    LOOP
        v_expired := false;
        v_attempt := NULL;
        SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_id=v_candidate.input_id;
        v_now := pg_catalog.clock_timestamp();
        IF v_input.status='materializing' THEN
            SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
            WHERE attempt.input_id=v_input.input_id AND attempt.generation=v_input.generation
              AND attempt.lease_token=v_input.lease_token AND attempt.status='leased'
            FOR UPDATE NOWAIT;
            IF NOT FOUND OR v_attempt.lease_expires_at>v_now THEN
                RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_busy');
            END IF;
            v_expired := true;
            v_input.last_lease_token := v_attempt.lease_token;
            v_input.last_outcome := 'lease_expired';
        END IF;
        v_code := aap_assets_v1._video_tracker_input_current_source(v_input,true,true);
        IF v_code IS NOT NULL THEN
            IF v_code IN ('account_busy','source_busy','task_busy','job_busy','input_busy') THEN
                RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_code);
            END IF;
            IF v_expired THEN
                UPDATE public.asset_accepted_video_input_attempts AS attempt
                SET status='abandoned',completed_at=v_now
                WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
                PERFORM aap_assets_v1._video_input_queue_prefix(
                    v_attempt.prefix,'lease_expired',GREATEST(v_now,v_attempt.lease_expires_at)+INTERVAL '24 hours'
                );
            END IF;
            UPDATE public.asset_accepted_video_inputs AS input
            SET status='failed',error_code=v_code,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                last_lease_token=CASE WHEN v_expired THEN v_attempt.lease_token ELSE input.last_lease_token END,
                last_outcome=CASE WHEN v_expired THEN 'lease_expired' ELSE input.last_outcome END,
                retention_checked_at=NULL,updated_at=v_now
            WHERE input.input_id=v_input.input_id;
            CONTINUE;
        END IF;
        IF v_expired THEN
            UPDATE public.asset_accepted_video_input_attempts AS attempt
            SET status='abandoned',completed_at=v_now
            WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
            PERFORM aap_assets_v1._video_input_queue_prefix(
                v_attempt.prefix,'lease_expired',GREATEST(v_now,v_attempt.lease_expires_at)+INTERVAL '24 hours'
            );
        END IF;
        IF v_input.attempt_count>=5 OR v_input.generation>=9007199254740991 THEN
            UPDATE public.asset_accepted_video_inputs AS input
            SET status='failed',error_code='resource_budget_exceeded',lease_owner=NULL,
                lease_token=NULL,lease_expires_at=NULL,retention_checked_at=NULL,updated_at=v_now
            WHERE input.input_id=v_input.input_id;
            CONTINUE;
        END IF;
        v_generation := v_input.generation+1;
        v_token := pg_catalog.gen_random_uuid();
        v_prefix := aap_assets_v1._video_input_attempt_prefix(v_input.input_id,v_generation,v_token);
        v_expires := v_now+pg_catalog.make_interval(secs=>p_lease_seconds);
        INSERT INTO public.asset_accepted_video_input_attempts(
            input_id,generation,lease_token,prefix,status,lease_expires_at
        ) VALUES(v_input.input_id,v_generation,v_token,v_prefix,'leased',v_expires);
        UPDATE public.asset_accepted_video_inputs AS input
        SET status='materializing',generation=v_generation,attempt_count=input.attempt_count+1,
            lease_owner=p_worker_id,lease_token=v_token,lease_expires_at=v_expires,error_code=NULL,
            staged_count=0,staged_bytes=0,
            last_lease_token=CASE WHEN v_expired THEN v_attempt.lease_token ELSE input.last_lease_token END,
            last_outcome=CASE WHEN v_expired THEN 'lease_expired' ELSE input.last_outcome END,
            retention_checked_at=NULL,updated_at=v_now
        WHERE input.input_id=v_input.input_id RETURNING * INTO v_input;
        v_claim := pg_catalog.jsonb_build_object(
            'input',aap_assets_v1._video_input_record(v_input),'source',v_input.source_facts,
            'generation',v_generation,'lease_token',v_token,'lease_expires_at',v_expires,
            'blob_key',v_prefix||'source'
        );
        RETURN aap_assets_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object('claim',v_claim),NULL);
    END LOOP;
    RETURN aap_assets_v1._video_tracker_input_reply(pg_catalog.jsonb_build_object('claim',NULL),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_video_tracker_input(uuid,uuid,integer) FROM PUBLIC;
-- aap-statement-break

-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.claim_accepted_video_input(
    p_worker_id uuid, p_input_id uuid, p_lease_seconds integer
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_manifest public.asset_accepted_video_manifests%ROWTYPE;
    v_candidate record;
    v_reply jsonb;
    v_source_code text;
    v_code text;
    v_now timestamptz;
    v_expires timestamptz;
    v_token uuid;
    v_generation bigint;
    v_prefix text;
    v_claim jsonb;
    v_expired boolean := false;
    v_message text;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 60 AND 7200 THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    IF p_input_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_id=p_input_id AND input.input_owner='manifest'
    ) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,CASE WHEN EXISTS (
            SELECT 1 FROM public.asset_accepted_video_inputs AS input WHERE input.input_id=p_input_id
        ) THEN 'input_scope_mismatch' ELSE 'input_missing' END);
    END IF;
    FOR v_candidate IN
        SELECT input.input_id FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_owner='manifest'
          AND (p_input_id IS NULL OR input.input_id=p_input_id)
          AND (input.status='pending' OR (input.status='materializing'
              AND input.lease_expires_at<=pg_catalog.clock_timestamp()))
        ORDER BY input.created_at,input.input_id
        LIMIT CASE WHEN p_input_id IS NULL THEN 100 ELSE 1 END
        FOR UPDATE OF input SKIP LOCKED
    LOOP
        v_expired := false;
        v_attempt := NULL;
        SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
        WHERE input.input_id=v_candidate.input_id;
        v_now := pg_catalog.clock_timestamp();
        IF v_input.status='materializing' THEN
            SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
            WHERE attempt.input_id=v_input.input_id AND attempt.generation=v_input.generation
              AND attempt.lease_token=v_input.lease_token AND attempt.status='leased'
            FOR UPDATE NOWAIT;
            IF NOT FOUND OR v_attempt.lease_expires_at>v_now THEN
                RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
            END IF;
            v_expired := true;
            v_input.last_lease_token := v_attempt.lease_token;
            v_input.last_outcome := 'lease_expired';
        END IF;
        SELECT manifest.* INTO v_manifest FROM public.asset_accepted_video_manifests AS manifest
        WHERE manifest.job_id=v_input.job_id;
        IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'manifest_missing'); END IF;
        IF v_manifest.status<>'preparing' THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'manifest_not_ready');
        END IF;
        v_reply := aap_assets_v1._video_input_job(v_manifest,v_input.task_id,true);
        v_code := aap_assets_v1._video_input_code(v_reply,'aap-assets-video-input-execution/v1');
        IF v_code='job_not_active' THEN
            IF v_expired THEN
                UPDATE public.asset_accepted_video_input_attempts AS attempt
                SET status='abandoned',completed_at=v_now
                WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
                PERFORM aap_assets_v1._video_input_queue_prefix(
                    v_attempt.prefix,'lease_expired',GREATEST(v_now,v_attempt.lease_expires_at)+INTERVAL '24 hours'
                );
            END IF;
            UPDATE public.asset_accepted_video_inputs AS input SET status='failed',error_code='job_not_active',
                lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                last_lease_token=COALESCE(v_input.last_lease_token,input.last_lease_token),
                last_outcome=CASE WHEN COALESCE(v_input.last_lease_token,input.last_lease_token) IS NULL
                    THEN NULL ELSE COALESCE(v_input.last_outcome,input.last_outcome) END,
                updated_at=v_now WHERE input.input_id=v_input.input_id RETURNING * INTO v_input;
            PERFORM aap_assets_v1._video_input_complete_member(v_input.input_id,'failed','job_not_active');
            CONTINUE;
        END IF;
        IF v_code IS NOT NULL THEN RETURN aap_assets_v1._video_input_reply(NULL,v_code); END IF;
        v_source_code := aap_assets_v1._video_input_current_source(
            v_input,v_input.request_actor_id,true,true
        );
        IF v_source_code IS NOT NULL THEN
            IF v_source_code IN ('account_not_active','invalid_account_role','source_missing','source_changed','source_not_ready','not_video',
                'task_scope_changed','timebase_changed','input_expired','input_corrupt','permission_denied',
                'task_not_visible') THEN
                IF v_expired THEN
                    UPDATE public.asset_accepted_video_input_attempts AS attempt
                    SET status='abandoned',completed_at=v_now
                    WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
                    PERFORM aap_assets_v1._video_input_queue_prefix(
                        v_attempt.prefix,'lease_expired',GREATEST(v_now,v_attempt.lease_expires_at)+INTERVAL '24 hours'
                    );
                END IF;
                UPDATE public.asset_accepted_video_inputs AS input
                SET status='failed',error_code=v_source_code,lease_owner=NULL,lease_token=NULL,
                    lease_expires_at=NULL,
                    last_lease_token=CASE WHEN v_expired THEN v_attempt.lease_token ELSE input.last_lease_token END,
                    last_outcome=CASE WHEN v_expired THEN 'lease_expired' ELSE input.last_outcome END,updated_at=v_now
                WHERE input.input_id=v_input.input_id RETURNING * INTO v_input;
                PERFORM aap_assets_v1._video_input_complete_member(v_input.input_id,'failed',v_source_code);
                CONTINUE;
            END IF;
            RETURN aap_assets_v1._video_input_reply(NULL,v_source_code);
        END IF;
        IF v_expired THEN
            UPDATE public.asset_accepted_video_input_attempts AS attempt
            SET status='abandoned',completed_at=v_now
            WHERE attempt.input_id=v_attempt.input_id AND attempt.generation=v_attempt.generation;
            PERFORM aap_assets_v1._video_input_queue_prefix(
                v_attempt.prefix,'lease_expired',GREATEST(v_now,v_attempt.lease_expires_at)+INTERVAL '24 hours'
            );
        END IF;
        IF v_input.attempt_count>=5 OR v_input.generation>=9007199254740991 THEN
            UPDATE public.asset_accepted_video_inputs AS input
            SET status='failed',error_code='resource_budget_exceeded',lease_owner=NULL,
                lease_token=NULL,lease_expires_at=NULL,
                last_lease_token=COALESCE(v_input.last_lease_token,input.last_lease_token),
                last_outcome=CASE WHEN COALESCE(v_input.last_lease_token,input.last_lease_token) IS NULL
                    THEN NULL ELSE COALESCE(v_input.last_outcome,input.last_outcome) END,updated_at=v_now
            WHERE input.input_id=v_input.input_id RETURNING * INTO v_input;
            PERFORM aap_assets_v1._video_input_complete_member(
                v_input.input_id,'failed','resource_budget_exceeded'
            );
            CONTINUE;
        END IF;
        v_generation := v_input.generation+1;
        v_token := pg_catalog.gen_random_uuid();
        v_prefix := aap_assets_v1._video_input_attempt_prefix(v_input.input_id,v_generation,v_token);
        v_expires := v_now+pg_catalog.make_interval(secs=>p_lease_seconds);
        INSERT INTO public.asset_accepted_video_input_attempts(
            input_id,generation,lease_token,prefix,status,lease_expires_at
        ) VALUES(v_input.input_id,v_generation,v_token,v_prefix,'leased',v_expires);
        UPDATE public.asset_accepted_video_inputs AS input
        SET status='materializing',generation=v_generation,attempt_count=input.attempt_count+1,
            lease_owner=p_worker_id,lease_token=v_token,lease_expires_at=v_expires,error_code=NULL,
            staged_count=0,staged_bytes=0,
            last_lease_token=COALESCE(v_input.last_lease_token,input.last_lease_token),
            last_outcome=COALESCE(v_input.last_outcome,input.last_outcome),updated_at=v_now
        WHERE input.input_id=v_input.input_id RETURNING * INTO v_input;
        v_claim := pg_catalog.jsonb_build_object(
            'input',aap_assets_v1._video_input_record(v_input),'source',v_input.source_facts,
            'generation',v_generation,'lease_token',v_token,'lease_expires_at',v_expires,
            'blob_key',v_prefix||'source'
        );
        RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('claim',v_claim),NULL);
    END LOOP;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('claim',NULL),NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
    WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS v_message=MESSAGE_TEXT;
        IF v_message LIKE 'video_input_abort:%' THEN
            RETURN aap_assets_v1._video_input_reply(NULL,pg_catalog.substr(v_message,19));
        END IF;
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_accepted_video_input(uuid,uuid,integer) FROM PUBLIC;
-- aap-statement-break

-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.sweep_accepted_video_inputs(p_limit integer)
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
        WHERE input.input_owner='manifest' AND ((
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
        ))
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
            WHERE frame.input_id=v_input.input_id AND frame.status <> 'expired'
            ORDER BY frame.ordinal
            FOR UPDATE OF frame NOWAIT
        LOOP
            IF v_frame.status='ready' THEN
                UPDATE public.asset_accepted_video_frames AS frame
                SET status='expired',updated_at=v_now
                WHERE frame.frame_id=v_frame.frame_id;
            ELSE
                UPDATE public.asset_accepted_video_frames AS frame
                SET status='failed',error_code='input_expired',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                    last_lease_token=COALESCE(v_frame.lease_token,frame.last_lease_token),
                    last_outcome=CASE WHEN v_frame.lease_token IS NULL THEN frame.last_outcome ELSE 'lease_expired' END,
                    updated_at=v_now WHERE frame.frame_id=v_frame.frame_id;
            END IF;
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
-- aap-statement-break

-- aap-statement-break
CREATE FUNCTION aap_assets_v1.heartbeat_video_tracker_input(
    p_input_id uuid,p_generation bigint,p_lease_token uuid,p_lease_seconds integer
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_reply jsonb; v_code text; v_source_code text; v_expires timestamptz;
BEGIN
    IF p_input_id IS NULL OR p_generation IS NULL OR p_generation<=0 OR p_lease_token IS NULL
       OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 60 AND 7200 THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    IF v_input.input_owner IS DISTINCT FROM 'tracker' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch'); END IF;
    IF v_input.status<>'materializing' OR (v_input.generation,v_input.lease_token)
        IS DISTINCT FROM (p_generation,p_lease_token)
       OR v_input.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'lease_lost');
    END IF;
    SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation
      AND attempt.lease_token=p_lease_token AND attempt.status='leased' FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'lease_lost');
    END IF;
    v_source_code := aap_assets_v1._video_tracker_input_current_source(v_input,true,true);
    IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_source_code); END IF;
    v_expires := pg_catalog.clock_timestamp()+pg_catalog.make_interval(secs=>p_lease_seconds);
    UPDATE public.asset_accepted_video_input_attempts AS attempt SET lease_expires_at=v_expires
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation;
    UPDATE public.asset_accepted_video_inputs AS input SET lease_expires_at=v_expires,updated_at=pg_catalog.clock_timestamp()
    WHERE input.input_id=p_input_id;
    RETURN aap_assets_v1._video_tracker_input_reply(pg_catalog.jsonb_build_object(
        'input_id',p_input_id,'generation',p_generation,'lease_token',p_lease_token,'lease_expires_at',v_expires
    ),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_busy');
WHEN OTHERS THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.heartbeat_video_tracker_input(uuid,bigint,uuid,integer) FROM PUBLIC;
-- aap-statement-break

-- aap-statement-break
CREATE FUNCTION aap_assets_v1.stage_video_tracker_times(
    p_input_id uuid,p_generation bigint,p_lease_token uuid,p_cursor bigint,p_entries jsonb
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_reply jsonb; v_code text; v_source_code text; v_entry jsonb; v_existing public.asset_accepted_video_times%ROWTYPE;
    v_ordinal bigint; v_index bigint; v_pts bigint; v_keyframe boolean; v_offset integer;
    v_count bigint; v_bytes bigint; v_next bigint; v_known public.video_frame_indices%ROWTYPE;
    v_projected_count bigint; v_projected_bytes bigint;
    v_message text;
BEGIN
    IF p_input_id IS NULL OR p_generation IS NULL OR p_generation<=0 OR p_lease_token IS NULL
       OR p_cursor IS NULL OR p_cursor<0 OR p_cursor>9007199254740991
       OR pg_catalog.jsonb_typeof(p_entries) IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    IF pg_catalog.jsonb_array_length(p_entries) NOT BETWEEN 1 AND 100
       OR pg_catalog.octet_length(pg_catalog.convert_to(p_entries::text,'UTF8'))>65536 THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    IF v_input.input_owner IS DISTINCT FROM 'tracker' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch'); END IF;
    IF v_input.status<>'materializing' OR (v_input.generation,v_input.lease_token)
        IS DISTINCT FROM (p_generation,p_lease_token) OR v_input.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'lease_lost');
    END IF;
    SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation
      AND attempt.lease_token=p_lease_token AND attempt.status='leased' FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'lease_lost');
    END IF;
    v_source_code := aap_assets_v1._video_tracker_input_current_source(v_input,true,true);
    IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_source_code); END IF;
    SELECT count(*),COALESCE(sum(pg_catalog.octet_length(pg_catalog.convert_to(
        aap_assets_v1._video_input_entry_text(times.ordinal,times.frame_index,times.pts_ms,times.is_keyframe),'UTF8'))),0)
    INTO v_projected_count,v_projected_bytes FROM public.asset_accepted_video_times AS times
    WHERE times.input_id=p_input_id AND times.generation=p_generation;
    IF v_projected_count<>v_input.staged_count OR v_projected_bytes<>v_input.staged_bytes THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_corrupt');
    END IF;
    IF v_projected_count>0 AND ((SELECT MIN(times.ordinal) FROM public.asset_accepted_video_times AS times
            WHERE times.input_id=p_input_id AND times.generation=p_generation)<>1
       OR (SELECT MAX(times.ordinal) FROM public.asset_accepted_video_times AS times
            WHERE times.input_id=p_input_id AND times.generation=p_generation)<>v_projected_count) THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_corrupt');
    END IF;
    v_offset := 0;
    FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_entries) LOOP
        v_offset := v_offset+1;
        IF NOT aap_assets_v1._video_input_keys(v_entry,ARRAY['ordinal','frame_index','pts_ms','is_keyframe'])
           OR NOT aap_assets_v1._video_input_int(v_entry->'ordinal',1,2147483647)
           OR NOT aap_assets_v1._video_input_int(v_entry->'frame_index',0,2147483647)
           OR NOT aap_assets_v1._video_input_int(v_entry->'pts_ms',-9007199254740991,9007199254740991)
           OR pg_catalog.jsonb_typeof(v_entry->'is_keyframe') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
        END IF;
        v_ordinal := (v_entry->>'ordinal')::bigint;
        v_index := (v_entry->>'frame_index')::bigint;
        v_pts := (v_entry->>'pts_ms')::bigint;
        v_keyframe := (v_entry->>'is_keyframe')::boolean;
        IF v_ordinal<>p_cursor+v_offset OR v_ordinal> (v_input.sampling->>'max_frames')::bigint
           OR v_index<>(v_ordinal-1)*(v_input.sampling->>'step')::bigint THEN
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,'timebase_incomplete');
        END IF;
        IF (v_input.source->>'timetable_revision')::bigint>0 THEN
            SELECT frame.* INTO v_known FROM public.video_frame_indices AS frame
            WHERE frame.dataset_item_id=(v_input.source->>'dataset_item_id')::uuid
              AND frame.frame_index=v_index;
            IF NOT FOUND OR (v_known.pts_ms::bigint,v_known.is_keyframe)
                IS DISTINCT FROM (v_pts,v_keyframe) THEN
                RETURN aap_assets_v1._video_tracker_input_reply(NULL,'timebase_changed');
            END IF;
        END IF;
        SELECT times.* INTO v_existing FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation AND times.ordinal=v_ordinal;
        IF FOUND THEN
            IF v_existing.lease_token IS DISTINCT FROM p_lease_token
               OR (v_existing.frame_index,v_existing.pts_ms,v_existing.is_keyframe)
                IS DISTINCT FROM (v_index::integer,v_pts,v_keyframe) THEN
                RETURN aap_assets_v1._video_tracker_input_reply(NULL,'timebase_changed');
            END IF;
        ELSE
            IF v_ordinal<>v_projected_count+1 THEN
                RETURN aap_assets_v1._video_tracker_input_reply(NULL,'timebase_incomplete');
            END IF;
            v_projected_count := v_projected_count+1;
            v_projected_bytes := v_projected_bytes+pg_catalog.octet_length(pg_catalog.convert_to(
                aap_assets_v1._video_input_entry_text(v_ordinal,v_index::integer,v_pts,v_keyframe),'UTF8'));
        END IF;
    END LOOP;
    IF v_projected_bytes>67108864 OR v_projected_count>2147483647 THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'resource_budget_exceeded');
    END IF;
    FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_entries) LOOP
        v_ordinal := (v_entry->>'ordinal')::bigint;
        v_index := (v_entry->>'frame_index')::bigint;
        v_pts := (v_entry->>'pts_ms')::bigint;
        v_keyframe := (v_entry->>'is_keyframe')::boolean;
        PERFORM 1 FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation AND times.ordinal=v_ordinal;
        IF NOT FOUND THEN
            INSERT INTO public.asset_accepted_video_times(input_id,generation,lease_token,ordinal,frame_index,pts_ms,is_keyframe)
            VALUES(p_input_id,p_generation,p_lease_token,v_ordinal,v_index::integer,v_pts,v_keyframe);
        END IF;
    END LOOP;
    SELECT count(*),COALESCE(sum(pg_catalog.octet_length(pg_catalog.convert_to(
        aap_assets_v1._video_input_entry_text(times.ordinal,times.frame_index,times.pts_ms,times.is_keyframe),'UTF8'))),0)
    INTO v_count,v_bytes FROM public.asset_accepted_video_times AS times
    WHERE times.input_id=p_input_id AND times.generation=p_generation;
    IF v_count IS DISTINCT FROM v_projected_count OR v_bytes IS DISTINCT FROM v_projected_bytes THEN
        PERFORM aap_assets_v1._video_input_abort('input_corrupt');
    END IF;
    UPDATE public.asset_accepted_video_inputs AS input SET staged_count=v_count,staged_bytes=v_bytes,
        updated_at=pg_catalog.clock_timestamp() WHERE input.input_id=p_input_id;
    v_next := p_cursor+pg_catalog.jsonb_array_length(p_entries);
    RETURN aap_assets_v1._video_tracker_input_reply(pg_catalog.jsonb_build_object(
        'input_id',p_input_id,'generation',p_generation,'lease_token',p_lease_token,'next_cursor',v_next
    ),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_busy');
WHEN OTHERS THEN
    GET STACKED DIAGNOSTICS v_message=MESSAGE_TEXT;
    IF v_message LIKE 'video_input_abort:%' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,pg_catalog.substr(v_message,19));
    END IF;
    RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.stage_video_tracker_times(uuid,bigint,uuid,bigint,jsonb) FROM PUBLIC;
-- aap-statement-break

-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_video_tracker_input(
    p_input_id uuid,p_generation bigint,p_lease_token uuid,p_plan jsonb
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_scope jsonb;
    v_reply jsonb; v_code text; v_source_code text;
    v_plan jsonb; v_blob jsonb; v_prefix text;
    v_frame_count bigint; v_max_frames bigint; v_step bigint; v_expected bigint;
    v_rows bigint; v_bytes bigint; v_seed text; v_entries text; v_timebase text; v_digest text;
    v_entry record; v_known public.video_frame_indices%ROWTYPE;
    v_message text;
BEGIN
    IF p_plan #> '{input,blob,version_id}' = 'null'::jsonb
       OR p_plan #>> '{input,blob,version_id}' IN ('','null')
       OR pg_catalog.btrim(p_plan #>> '{input,blob,version_id}')='' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'versioning_required');
    END IF;
    IF p_input_id IS NULL OR p_generation IS NULL OR p_generation<=0 OR p_lease_token IS NULL
       OR NOT aap_assets_v1._video_input_plan_shape_valid(p_plan)
       OR p_plan #>> '{input,input_id}' IS DISTINCT FROM p_input_id::text
       OR p_plan #>> '{input,blob,key}' IS DISTINCT FROM
            aap_assets_v1._video_input_attempt_prefix(p_input_id,p_generation,p_lease_token)||'source' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    IF v_input.input_owner IS DISTINCT FROM 'tracker' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch'); END IF;
    IF (p_plan #>> '{input,created_at}')::timestamptz IS DISTINCT FROM v_input.created_at THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch'); END IF;
    v_plan := pg_catalog.jsonb_set(p_plan,'{input,created_at}',pg_catalog.to_jsonb(v_input.created_at),false);
    IF v_plan #>> '{input,job_id}' IS DISTINCT FROM v_input.job_id::text
       OR v_plan #>> '{input,actor_id}' IS DISTINCT FROM v_input.actor_id::text
       OR v_plan #> '{input,task}' IS DISTINCT FROM v_input.task
       OR v_plan #> '{input,source}' IS DISTINCT FROM v_input.source
       OR v_plan -> 'sampling' IS DISTINCT FROM v_input.sampling
       OR NOT aap_assets_v1._video_input_blob_valid(v_plan #> '{input,blob}',false) THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    v_blob := v_plan #> '{input,blob}';
    IF pg_catalog.char_length(v_blob ->> 'version_id') NOT BETWEEN 1 AND 255
       OR pg_catalog.btrim(v_blob ->> 'version_id')='' OR v_blob ->> 'version_id'='null'
       OR (v_blob ->> 'byte_size')::bigint IS DISTINCT FROM
            (v_input.source #>> '{object_identity,byte_size}')::bigint THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_corrupt');
    END IF;
    SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation
      AND attempt.lease_token=p_lease_token AND attempt.status='leased' FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at<=pg_catalog.clock_timestamp()
       OR v_input.status<>'materializing' OR (v_input.generation,v_input.lease_token)
            IS DISTINCT FROM (p_generation,p_lease_token)
       OR v_input.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        IF v_input.status='ready' AND v_input.generation=p_generation
           AND v_input.last_lease_token=p_lease_token AND v_input.last_outcome='ready'
           AND v_input.plan IS NOT DISTINCT FROM v_plan THEN
            SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
            WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation
              AND attempt.lease_token=p_lease_token AND attempt.status='ready';
            IF NOT FOUND OR v_attempt.prefix IS DISTINCT FROM
                aap_assets_v1._video_input_attempt_prefix(p_input_id,p_generation,p_lease_token) THEN
                RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_corrupt');
            END IF;
            v_source_code := aap_assets_v1._video_tracker_input_current_source(v_input,true,true);
            IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_source_code); END IF;
            RETURN aap_assets_v1._video_tracker_input_reply(aap_assets_v1._video_input_record(v_input),NULL);
        END IF;
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'lease_lost');
    END IF;
    v_frame_count := (v_plan #>> '{input,video,frame_count}')::bigint;
    v_max_frames := (v_plan #>> '{sampling,max_frames}')::bigint;
    v_step := (v_plan #>> '{sampling,step}')::bigint;
    v_expected := LEAST(v_max_frames,(v_frame_count+v_step-1)/v_step);
    IF v_expected<1 OR v_expected>2147483647
       OR (v_expected-1)*v_step>2147483647
       OR (v_plan ->> 'selected_count')::bigint IS DISTINCT FROM v_expected THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'timebase_incomplete');
    END IF;
    SELECT count(*),COALESCE(sum(pg_catalog.octet_length(pg_catalog.convert_to(
        aap_assets_v1._video_input_entry_text(times.ordinal,times.frame_index,times.pts_ms,times.is_keyframe),'UTF8'))),0)
    INTO v_rows,v_bytes FROM public.asset_accepted_video_times AS times
    WHERE times.input_id=p_input_id AND times.generation=p_generation;
    IF v_bytes>67108864 OR v_bytes<>v_input.staged_bytes OR v_rows<>v_input.staged_count THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'resource_budget_exceeded');
    END IF;
    IF EXISTS (SELECT 1 FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation AND times.lease_token<>p_lease_token) THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'timebase_changed');
    END IF;
    IF v_rows<>v_expected OR NOT EXISTS (
        SELECT 1 FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation AND times.ordinal=1
    ) OR NOT EXISTS (
        SELECT 1 FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation AND times.ordinal=v_expected
    ) THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'timebase_incomplete'); END IF;
    IF EXISTS (
        SELECT 1 FROM public.asset_accepted_video_times AS times
        WHERE times.input_id=p_input_id AND times.generation=p_generation
          AND (times.frame_index::bigint<>(times.ordinal-1)*v_step OR times.ordinal>v_expected)
    ) THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'timebase_incomplete'); END IF;
    SELECT pg_catalog.string_agg(
        aap_assets_v1._video_input_entry_text(times.ordinal,times.frame_index,times.pts_ms,times.is_keyframe),''
        ORDER BY times.ordinal
    ) INTO v_entries FROM public.asset_accepted_video_times AS times
    WHERE times.input_id=p_input_id AND times.generation=p_generation;
    v_seed := pg_catalog.array_to_string(ARRAY[
        'aap-assets-video-inputs/v1',v_input.input_id::text,v_input.job_id::text,v_input.actor_id::text,
        v_input.task->>'task_id',v_input.task->>'project_id',v_input.task->>'task_version',
        v_input.task->>'task_source_generation',v_input.task->>'task_source_snapshot',
        v_input.source->>'dataset_id',v_input.source->>'dataset_item_id',v_input.source->>'source_generation',
        v_input.source->>'source_snapshot',v_input.source->>'timetable_revision',v_blob->>'sha256',
        v_blob->>'byte_size',v_plan #>> '{input,video,frame_count}',v_plan #>> '{input,video,width}',
        v_plan #>> '{input,video,height}',v_plan #>> '{input,video,duration_ms}',
        v_plan #>> '{input,video,video_stream}',v_plan #>> '{sampling,max_frames}',
        v_plan #>> '{sampling,step}',v_plan #>> '{sampling,width}',v_plan #>> '{sampling,format}'
    ],E'\n')||E'\n';
    v_timebase := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to('timebase'||E'\n'||v_seed||COALESCE(v_entries,''),'UTF8')),'hex');
    v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to('plan'||E'\n'||v_seed||v_timebase||E'\n','UTF8')),'hex');
    IF v_timebase IS DISTINCT FROM v_plan->>'timebase_digest'
       OR v_digest IS DISTINCT FROM v_plan->>'plan_digest' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_corrupt');
    END IF;
    IF (v_input.source->>'timetable_revision')::bigint>0 THEN
        FOR v_entry IN SELECT times.frame_index,times.pts_ms,times.is_keyframe
            FROM public.asset_accepted_video_times AS times
            WHERE times.input_id=p_input_id AND times.generation=p_generation ORDER BY times.ordinal
        LOOP
            SELECT frame.* INTO v_known FROM public.video_frame_indices AS frame
            WHERE frame.dataset_item_id=(v_input.source->>'dataset_item_id')::uuid
              AND frame.frame_index=v_entry.frame_index;
            IF NOT FOUND OR (v_known.pts_ms::bigint,v_known.is_keyframe)
                IS DISTINCT FROM (v_entry.pts_ms,v_entry.is_keyframe) THEN
                RETURN aap_assets_v1._video_tracker_input_reply(NULL,'timebase_changed');
            END IF;
        END LOOP;
    END IF;
    v_reply := aap_labeling_v1.project_video_tracker_input(
        v_input.request_actor_id,v_input.tracker_job_id,v_input.task_id,v_input.input_id,
        NULL,true,false,true
    );
    IF v_reply IS NULL OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,COALESCE(v_reply #>> '{error,code}','invalid_response'));
    END IF;
    v_scope := v_reply #> '{data,scope}';
    IF v_frame_count <= (v_scope #>> '{execution,to_frame}')::bigint
       OR ((v_scope #>> '{execution,correction_frame}') IS NOT NULL
           AND (v_scope #>> '{execution,correction_frame}')::bigint>=v_frame_count) THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_frame_range');
    END IF;
    v_source_code := aap_assets_v1._video_tracker_input_current_source(v_input,true,true);
    IF v_source_code IS NOT NULL THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_source_code); END IF;
    v_prefix := v_attempt.prefix;
    IF NOT aap_assets_v1._video_input_prefix_valid(v_prefix)
       OR v_blob->>'key' IS DISTINCT FROM v_prefix||'source' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch');
    END IF;
    UPDATE public.asset_accepted_video_inputs AS input SET status='ready',plan=v_plan,error_code=NULL,
        lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_lease_token=p_lease_token,
        last_outcome='ready',retire_after=NULL,retention_checked_at=NULL,updated_at=pg_catalog.clock_timestamp()
    WHERE input.input_id=p_input_id RETURNING * INTO v_input;
    UPDATE public.asset_accepted_video_input_attempts AS attempt SET status='ready',completed_at=pg_catalog.clock_timestamp()
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation;
    RETURN aap_assets_v1._video_tracker_input_reply(aap_assets_v1._video_input_record(v_input),NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_busy');
    WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS v_message=MESSAGE_TEXT;
        IF v_message LIKE 'video_input_abort:%' THEN
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,pg_catalog.substr(v_message,19));
        END IF;
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_video_tracker_input(uuid,bigint,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break

-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fail_video_tracker_input(
    p_input_id uuid,p_generation bigint,p_lease_token uuid,p_error_code text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_attempt public.asset_accepted_video_input_attempts%ROWTYPE;
    v_reply jsonb; v_code text; v_source_code text; v_message text;
BEGIN
    IF p_input_id IS NULL OR p_generation IS NULL OR p_generation<=0 OR p_lease_token IS NULL
       OR NOT aap_assets_v1._video_input_error_valid(p_error_code)
       OR p_error_code IN ('input_busy','source_busy','job_busy','manifest_busy','task_busy','account_busy',
           'operation_busy','lease_lost','reference_busy') THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    SELECT input.* INTO v_input FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id=p_input_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_missing'); END IF;
    IF v_input.input_owner IS DISTINCT FROM 'tracker' THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_scope_mismatch'); END IF;
    IF v_input.status='failed' AND v_input.generation=p_generation AND v_input.last_lease_token=p_lease_token
       AND v_input.last_outcome='failed' AND v_input.error_code=p_error_code THEN
        SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
        WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation AND attempt.lease_token=p_lease_token
          AND attempt.status='failed';
        IF NOT FOUND OR v_attempt.prefix IS DISTINCT FROM
            aap_assets_v1._video_input_attempt_prefix(p_input_id,p_generation,p_lease_token) THEN
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_corrupt');
        END IF;
        RETURN aap_assets_v1._video_tracker_input_reply(aap_assets_v1._video_input_record(v_input),NULL);
    END IF;
    IF v_input.status<>'materializing' OR (v_input.generation,v_input.lease_token)
       IS DISTINCT FROM (p_generation,p_lease_token) OR v_input.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'lease_lost');
    END IF;
    SELECT attempt.* INTO v_attempt FROM public.asset_accepted_video_input_attempts AS attempt
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation AND attempt.lease_token=p_lease_token
      AND attempt.status='leased' FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at<=pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'lease_lost');
    END IF;
    v_source_code := aap_assets_v1._video_tracker_input_current_source(v_input,true,true);
    IF v_source_code IS NOT NULL AND v_source_code NOT IN (
        'source_missing','source_changed','source_not_ready','not_video','task_scope_changed'
    ) THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,v_source_code);
    END IF;
    PERFORM aap_assets_v1._video_input_queue_prefix(
        v_attempt.prefix,'input_failed',GREATEST(pg_catalog.clock_timestamp(),v_attempt.lease_expires_at)+INTERVAL '24 hours'
    );
    UPDATE public.asset_accepted_video_input_attempts AS attempt SET status='failed',completed_at=pg_catalog.clock_timestamp()
    WHERE attempt.input_id=p_input_id AND attempt.generation=p_generation;
    UPDATE public.asset_accepted_video_inputs AS input SET status='failed',error_code=p_error_code,
        lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_lease_token=p_lease_token,
        last_outcome='failed',retire_after=NULL,updated_at=pg_catalog.clock_timestamp()
    WHERE input.input_id=p_input_id RETURNING * INTO v_input;
    RETURN aap_assets_v1._video_tracker_input_reply(aap_assets_v1._video_input_record(v_input),NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'input_busy');
    WHEN OTHERS THEN
        GET STACKED DIAGNOSTICS v_message=MESSAGE_TEXT;
        IF v_message LIKE 'video_input_abort:%' THEN
            RETURN aap_assets_v1._video_tracker_input_reply(NULL,pg_catalog.substr(v_message,19));
        END IF;
        RETURN aap_assets_v1._video_tracker_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fail_video_tracker_input(uuid,bigint,uuid,text) FROM PUBLIC;
-- aap-statement-break
