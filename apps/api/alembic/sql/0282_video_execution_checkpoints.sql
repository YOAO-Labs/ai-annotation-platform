-- Durable, proof-bound video window plans and immutable result checkpoints.
-- All mutation paths enter through Jobs after locking canonical Job -> execution;
-- checkpoint tables remain private to the Jobs owner.
CREATE TABLE aap_jobs_v1.video_execution_checkpoint_restore (
    signature text PRIMARY KEY,
    function_definition text NOT NULL
);
-- aap-statement-break
INSERT INTO aap_jobs_v1.video_execution_checkpoint_restore(signature,function_definition)
SELECT function.oid::pg_catalog.regprocedure::text,
       pg_catalog.pg_get_functiondef(function.oid)
FROM pg_catalog.pg_proc AS function
WHERE function.oid IN (
    pg_catalog.to_regprocedure('aap_labeling_v1.video_tracker_input_referenced(uuid)')::oid,
    pg_catalog.to_regprocedure(
        'aap_labeling_v1._mask_gc_root_entry_live(text,text,timestamptz,boolean)'
    )::oid,
    pg_catalog.to_regprocedure(
        'aap_labeling_v1._mask_gc_release_tracker_running_outputs()'
    )::oid,
    pg_catalog.to_regprocedure('aap_labeling_v1.guard_dataset_source_deletion(uuid,uuid)')::oid
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.video_execution_checkpoint_restore FROM PUBLIC;
-- aap-statement-break
DO $restore_guard$
BEGIN
    IF (SELECT pg_catalog.count(*)
        FROM aap_jobs_v1.video_execution_checkpoint_restore) <> 4 THEN
        RAISE EXCEPTION USING
            ERRCODE='55000',
            MESSAGE='required Labeling functions for checkpoint downgrade are unavailable';
    END IF;
END
$restore_guard$;
-- aap-statement-break
CREATE TABLE aap_jobs_v1.video_execution_journals (
    job_id uuid PRIMARY KEY REFERENCES aap_jobs_v1.video_executions(job_id) ON DELETE RESTRICT,
    run_id uuid NOT NULL,
    input_id uuid,
    task_id uuid,
    project_id uuid,
    source_item_id uuid,
    request_digest text CHECK (
        request_digest IS NULL OR request_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    prompt_digest text CHECK (
        prompt_digest IS NULL OR prompt_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    source_plan_digest text CHECK (
        source_plan_digest IS NULL OR source_plan_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    timebase_digest text CHECK (
        timebase_digest IS NULL OR timebase_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    plan_digest text CHECK (
        plan_digest IS NULL OR plan_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    plan_document jsonb CHECK (
        plan_document IS NULL OR pg_catalog.jsonb_typeof(plan_document) = 'object'
    ),
    writer_terminal boolean NOT NULL DEFAULT false,
    writer_terminal_outcome text CHECK (
        writer_terminal_outcome IS NULL
        OR writer_terminal_outcome IN ('completed','cancelled','failed')
    ),
    execution_retired boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (job_id, run_id),
    CHECK ((plan_document IS NULL) = (plan_digest IS NULL)),
    CHECK (writer_terminal OR writer_terminal_outcome IS NULL),
    CHECK (NOT execution_retired OR writer_terminal)
);
-- aap-statement-break
CREATE INDEX ix_aap_video_execution_journal_input
    ON aap_jobs_v1.video_execution_journals(input_id, execution_retired);
-- aap-statement-break
CREATE INDEX ix_aap_video_execution_journal_source
    ON aap_jobs_v1.video_execution_journals(source_item_id, execution_retired);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.video_execution_journals FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_jobs_v1.video_execution_window_attempts (
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 1),
    window_ordinal integer NOT NULL CHECK (window_ordinal BETWEEN 0 AND 8191),
    window_digest text NOT NULL CHECK (window_digest COLLATE "C" ~ '^[0-9a-f]{64}$'),
    mode text NOT NULL CHECK (mode IN ('execute','replay')),
    state text NOT NULL CHECK (
        state IN ('started','capturing','captured','replay_verified','abandoned')
    ),
    capture_id uuid NOT NULL UNIQUE,
    seed_input_digest text CHECK (
        seed_input_digest IS NULL OR seed_input_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    predecessor_checkpoint_digest text CHECK (
        predecessor_checkpoint_digest IS NULL
        OR predecessor_checkpoint_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    raw_response_digest text CHECK (
        raw_response_digest IS NULL OR raw_response_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (job_id, run_id, generation, window_ordinal),
    FOREIGN KEY (job_id, run_id)
        REFERENCES aap_jobs_v1.video_execution_journals(job_id, run_id) ON DELETE RESTRICT,
    CHECK (
        (mode = 'replay' AND state IN ('started','replay_verified','abandoned'))
        OR (mode = 'execute' AND state IN ('started','capturing','captured','abandoned'))
    )
);
-- aap-statement-break
CREATE INDEX ix_aap_video_execution_attempt_capture
    ON aap_jobs_v1.video_execution_window_attempts(capture_id, state);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.video_execution_window_attempts FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_jobs_v1.video_execution_captures (
    capture_id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 1),
    window_ordinal integer NOT NULL CHECK (window_ordinal BETWEEN 0 AND 8191),
    manifest_digest text NOT NULL CHECK (manifest_digest COLLATE "C" ~ '^[0-9a-f]{64}$'),
    manifest jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(manifest) = 'object'),
    state text NOT NULL CHECK (state IN ('receiving','sealed','abandoned')),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    UNIQUE (job_id, run_id, generation, window_ordinal, capture_id),
    UNIQUE (job_id, run_id, capture_id),
    FOREIGN KEY (job_id, run_id, generation, window_ordinal)
        REFERENCES aap_jobs_v1.video_execution_window_attempts(
            job_id, run_id, generation, window_ordinal
        ) ON DELETE RESTRICT
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.video_execution_captures FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_jobs_v1.video_execution_capture_parts (
    capture_id uuid NOT NULL REFERENCES aap_jobs_v1.video_execution_captures(capture_id)
        ON DELETE RESTRICT,
    part_index integer NOT NULL CHECK (part_index BETWEEN 0 AND 127),
    part_digest text NOT NULL CHECK (part_digest COLLATE "C" ~ '^[0-9a-f]{64}$'),
    part_bytes bytea NOT NULL CHECK (
        pg_catalog.octet_length(part_bytes) BETWEEN 1 AND 524288
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (capture_id, part_index)
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.video_execution_capture_parts FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_jobs_v1.video_execution_windows (
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    window_ordinal integer NOT NULL CHECK (window_ordinal BETWEEN 0 AND 8191),
    checkpoint_id text NOT NULL CHECK (checkpoint_id COLLATE "C" ~ '^[0-9a-f]{64}$'),
    manifest_digest text NOT NULL CHECK (manifest_digest COLLATE "C" ~ '^[0-9a-f]{64}$'),
    manifest jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(manifest) = 'object'),
    capture_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (job_id, run_id, window_ordinal),
    UNIQUE (job_id, run_id, checkpoint_id),
    FOREIGN KEY (job_id, run_id)
        REFERENCES aap_jobs_v1.video_execution_journals(job_id, run_id) ON DELETE RESTRICT,
    FOREIGN KEY (job_id, run_id, capture_id)
        REFERENCES aap_jobs_v1.video_execution_captures(job_id, run_id, capture_id)
        ON DELETE RESTRICT
);
-- aap-statement-break
CREATE INDEX ix_aap_video_execution_window_checkpoint
    ON aap_jobs_v1.video_execution_windows(checkpoint_id);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.video_execution_windows FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_jobs_v1.video_execution_window_parts (
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    window_ordinal integer NOT NULL CHECK (window_ordinal BETWEEN 0 AND 8191),
    checkpoint_id text NOT NULL CHECK (checkpoint_id COLLATE "C" ~ '^[0-9a-f]{64}$'),
    part_index integer NOT NULL CHECK (part_index BETWEEN 0 AND 127),
    part_digest text NOT NULL CHECK (part_digest COLLATE "C" ~ '^[0-9a-f]{64}$'),
    part_bytes bytea NOT NULL CHECK (
        pg_catalog.octet_length(part_bytes) BETWEEN 1 AND 524288
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (job_id, run_id, window_ordinal, part_index),
    FOREIGN KEY (job_id, run_id, window_ordinal)
        REFERENCES aap_jobs_v1.video_execution_windows(job_id, run_id, window_ordinal)
        ON DELETE RESTRICT
);
-- aap-statement-break
CREATE INDEX ix_aap_video_execution_window_part_checkpoint
    ON aap_jobs_v1.video_execution_window_parts(checkpoint_id, part_index);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.video_execution_window_parts FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_jobs_v1.video_execution_span_closures (
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    plan_digest text NOT NULL CHECK (plan_digest COLLATE "C" ~ '^[0-9a-f]{64}$'),
    context_span_digest text NOT NULL CHECK (
        context_span_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    closed_generation bigint NOT NULL CHECK (closed_generation >= 1),
    closed_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (job_id, run_id, context_span_digest),
    FOREIGN KEY (job_id, run_id)
        REFERENCES aap_jobs_v1.video_execution_journals(job_id, run_id) ON DELETE RESTRICT
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.video_execution_span_closures FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_checkpoint_reply(
    p_data jsonb,
    p_reason text DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT CASE WHEN p_reason IS NULL THEN
        pg_catalog.jsonb_build_object(
            'schema','aap-video-checkpoint-data/v1','status','ok'
        ) || COALESCE(p_data,'{}'::jsonb)
    ELSE pg_catalog.jsonb_build_object(
        'schema','aap-video-checkpoint-data/v1','status','rejected','reason',p_reason
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_checkpoint_reply(jsonb,text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.video_execution_input_retained(p_input_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT COALESCE(EXISTS (
        SELECT 1 FROM aap_jobs_v1.video_execution_journals AS journal
        WHERE journal.input_id = p_input_id AND NOT journal.execution_retired
    ),false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.video_execution_input_retained(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.video_execution_source_item_retained(
    p_item_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT COALESCE(EXISTS (
        SELECT 1 FROM aap_jobs_v1.video_execution_journals AS journal
        WHERE journal.source_item_id = p_item_id AND NOT journal.execution_retired
    ),false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.video_execution_source_item_retained(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.video_execution_running_mask_roots_live(
    p_job_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT COALESCE(EXISTS (
        SELECT 1 FROM aap_jobs_v1.video_execution_journals AS journal
        WHERE journal.job_id = p_job_id
          AND NOT journal.writer_terminal
          AND NOT journal.execution_retired
    ),false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.video_execution_running_mask_roots_live(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.video_execution_checkpoint_mask_retained(
    p_checkpoint_id text
)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT COALESCE(EXISTS (
        SELECT 1
        FROM aap_jobs_v1.video_execution_windows AS checkpoint
        JOIN aap_jobs_v1.video_execution_journals AS journal
          ON journal.job_id = checkpoint.job_id AND journal.run_id = checkpoint.run_id
        WHERE checkpoint.checkpoint_id = p_checkpoint_id
          AND NOT journal.writer_terminal
          AND NOT journal.execution_retired
    ),false)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.video_execution_checkpoint_mask_retained(text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1.video_execution_scope_retired(
    p_scope_id uuid
)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT p_scope_id IS NOT NULL AND EXISTS (
        SELECT 1
        FROM aap_inference_routing_v1.scope_retirements AS retirement
        JOIN aap_inference_routing_v1.scopes AS scope
          ON scope.id = retirement.scope_id
        WHERE retirement.scope_id = p_scope_id
          AND retirement.phase = 'completed'
          AND retirement.realm = aap_inference_routing_v1._routing_realm()
          AND scope.state = 'reconciled'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.video_execution_scope_retired(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_inference_routing_v1.video_execution_scope_facts(p_scope_id uuid)
RETURNS jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id',scope.id,'registry_id',scope.registry_id,'owner',scope.owner,
        'operation',scope.operation,'state',scope.state,'closed',scope.closed
    )
    FROM aap_inference_routing_v1.scopes AS scope
    WHERE scope.id=p_scope_id
      AND scope.realm=aap_inference_routing_v1._routing_realm()
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_inference_routing_v1.video_execution_scope_facts(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.video_execution_input_facts(p_input_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_input public.asset_accepted_video_inputs%ROWTYPE;
    v_plan jsonb;
BEGIN
    IF p_input_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-assets-video-execution-input-facts/v1',
            'ok',false,'error',pg_catalog.jsonb_build_object('code','invalid_request')
        );
    END IF;
    SELECT input.* INTO v_input
    FROM public.asset_accepted_video_inputs AS input
    WHERE input.input_id = p_input_id AND input.input_owner = 'tracker'
    FOR SHARE NOWAIT;
    IF NOT FOUND OR v_input.status <> 'ready' OR v_input.plan IS NULL
       OR v_input.tracker_job_id IS NULL OR v_input.job_id IS DISTINCT FROM v_input.tracker_job_id
       OR NOT aap_assets_v1._video_input_plan_shape_valid(v_input.plan) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-assets-video-execution-input-facts/v1',
            'ok',false,'error',pg_catalog.jsonb_build_object('code','input_unavailable')
        );
    END IF;
    v_plan := v_input.plan;
    IF v_plan #>> '{input,input_id}' IS DISTINCT FROM p_input_id::text
       OR v_plan #>> '{input,job_id}' IS DISTINCT FROM v_input.tracker_job_id::text
       OR v_plan #>> '{input,task,task_id}' IS DISTINCT FROM v_input.task_id::text
       OR v_plan #>> '{input,task,project_id}' IS DISTINCT FROM v_input.project_id::text
       OR v_plan #> '{input,source}' IS DISTINCT FROM v_input.source
       OR v_plan #> '{input,task}' IS DISTINCT FROM v_input.task
       OR (v_plan #>> '{input,blob,byte_size}')::bigint IS DISTINCT FROM
            (v_input.source_facts #>> '{object_identity,byte_size}')::bigint THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-assets-video-execution-input-facts/v1',
            'ok',false,'error',pg_catalog.jsonb_build_object('code','input_unavailable')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-assets-video-execution-input-facts/v1','ok',true,
        'data',pg_catalog.jsonb_build_object(
            'input_id',p_input_id,
            'request_id',v_input.tracker_job_id,
            'job_id',v_input.tracker_job_id,
            'task_id',v_input.task_id,
            'project_id',v_input.project_id,
            'plan_digest',v_plan -> 'plan_digest',
            'timebase_digest',v_plan -> 'timebase_digest',
            'sampling_step',v_plan #> '{sampling,step}',
            'selected_frame_count',v_plan -> 'selected_count',
            'source_version_id',v_plan #> '{input,blob,version_id}',
            'source_sha256',v_plan #> '{input,blob,sha256}',
            'byte_size',v_plan #> '{input,blob,byte_size}',
            'frame_count',v_plan #> '{input,video,frame_count}',
            'width',v_plan #> '{input,video,width}',
            'height',v_plan #> '{input,video,height}',
            'duration_ms',v_plan #> '{input,video,duration_ms}',
            'video_stream',v_plan #> '{input,video,video_stream}'
        )
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-assets-video-execution-input-facts/v1',
            'ok',false,'error',pg_catalog.jsonb_build_object('code','input_busy')
        );
    WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-assets-video-execution-input-facts/v1',
            'ok',false,'error',pg_catalog.jsonb_build_object('code','invalid_response')
        );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.video_execution_input_facts(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.video_execution_request_facts(p_job_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_scope public.video_tracker_input_scopes%ROWTYPE;
    v_execution jsonb;
BEGIN
    IF p_job_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-labeling-video-execution-request-facts/v1',
            'ok',false,'error',pg_catalog.jsonb_build_object('code','invalid_request')
        );
    END IF;
    SELECT scope.* INTO v_scope
    FROM public.video_tracker_input_scopes AS scope
    WHERE scope.tracker_job_id = p_job_id AND scope.prepare_error IS NULL
    FOR SHARE NOWAIT;
    IF NOT FOUND OR pg_catalog.jsonb_typeof(v_scope.scope #> '{execution}') IS DISTINCT FROM 'object'
       OR (v_scope.scope #>> '{task,task_id}') IS DISTINCT FROM v_scope.task_id::text
       OR (v_scope.scope #>> '{task,project_id}') IS DISTINCT FROM v_scope.project_id::text THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-labeling-video-execution-request-facts/v1',
            'ok',false,'error',pg_catalog.jsonb_build_object('code','input_unavailable')
        );
    END IF;
    v_execution := v_scope.scope -> 'execution';
    IF pg_catalog.jsonb_typeof(v_execution) IS DISTINCT FROM 'object'
       OR (v_execution - ARRAY[
            'job_kind','model_key','direction','from_frame','to_frame','grid_step',
            'correction_frame','segment_id','track_id_snapshot','target_class_name',
            'target_tool_unit_id','prompt_snapshot','source_annotations','correction_mask'
        ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_execution ?& ARRAY[
            'job_kind','model_key','direction','from_frame','to_frame','grid_step',
            'correction_frame','segment_id','track_id_snapshot','target_class_name',
            'target_tool_unit_id','prompt_snapshot','source_annotations','correction_mask'
        ])
       OR pg_catalog.jsonb_typeof(v_execution -> 'model_key') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_execution -> 'direction') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_execution -> 'job_kind') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(v_execution -> 'from_frame') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_execution -> 'to_frame') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_execution -> 'grid_step') IS DISTINCT FROM 'number' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-labeling-video-execution-request-facts/v1',
            'ok',false,'error',pg_catalog.jsonb_build_object('code','invalid_response')
        );
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'schema','aap-labeling-video-execution-request-facts/v1','ok',true,
        'data',pg_catalog.jsonb_build_object(
            'job_id',p_job_id,
            'input_id',v_scope.input_id,
            'task_id',v_scope.task_id,
            'project_id',v_scope.project_id,
            'source_item_id',v_scope.scope #> '{task,dataset_item_id}',
            'request_digest',v_scope.request_digest,
            'prompt_digest',v_scope.prompt_digest,
            'requested_model_key',v_execution -> 'model_key',
            'job_kind',v_execution -> 'job_kind',
            'direction',v_execution -> 'direction',
            'from_frame',v_execution -> 'from_frame',
            'to_frame',v_execution -> 'to_frame',
            'grid_step',v_execution -> 'grid_step',
            'correction_frame',v_execution -> 'correction_frame',
            'segment_id',v_execution -> 'segment_id'
        )
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-labeling-video-execution-request-facts/v1',
            'ok',false,'error',pg_catalog.jsonb_build_object('code','input_busy')
        );
    WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema','aap-labeling-video-execution-request-facts/v1',
            'ok',false,'error',pg_catalog.jsonb_build_object('code','invalid_response')
        );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.video_execution_request_facts(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_checkpoint_canonical_json(p_value jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT aap_inference_routing_v1._video_execution_canonical_json(p_value)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_checkpoint_canonical_json(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_checkpoint_digest(p_value jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT pg_catalog.encode(public.digest(
        pg_catalog.convert_to(aap_jobs_v1._video_checkpoint_canonical_json(p_value),'UTF8'),
        'sha256'
    ),'hex')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_checkpoint_digest(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_checkpoint_input_facts(p_job_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_request jsonb;
    v_input jsonb;
    v_request_data jsonb;
    v_input_data jsonb;
    v_source_item uuid;
    v_input_id uuid;
    v_job_id uuid;
    v_task_id uuid;
    v_project_id uuid;
    v_request_id uuid;
    v_facts jsonb;
BEGIN
    IF p_job_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_request');
    END IF;
    v_request := aap_labeling_v1.video_execution_request_facts(p_job_id);
    IF v_request IS NULL
       OR v_request ->> 'schema' IS DISTINCT FROM
            'aap-labeling-video-execution-request-facts/v1'
       OR v_request -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok',false,'reason',COALESCE(v_request #>> '{error,code}','input_unavailable')
        );
    END IF;
    v_request_data := v_request -> 'data';
    IF NOT aap_jobs_v1._video_execution_exact_keys(
        v_request_data,ARRAY[
            'job_id','input_id','task_id','project_id','source_item_id',
            'request_digest','prompt_digest','requested_model_key','job_kind',
            'direction','from_frame','to_frame','grid_step','correction_frame','segment_id'
        ]
    ) THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_response');
    END IF;
    BEGIN
        v_job_id := aap_jobs_v1._video_execution_uuid(v_request_data -> 'job_id');
        v_input_id := aap_jobs_v1._video_execution_uuid(v_request_data -> 'input_id');
        v_task_id := aap_jobs_v1._video_execution_uuid(v_request_data -> 'task_id');
        v_project_id := aap_jobs_v1._video_execution_uuid(v_request_data -> 'project_id');
        IF v_request_data -> 'source_item_id' <> 'null'::jsonb THEN
            v_source_item := aap_jobs_v1._video_execution_uuid(
                v_request_data -> 'source_item_id'
            );
        END IF;
        v_request_id := v_job_id;
    EXCEPTION WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_response');
    END;
    IF v_job_id IS DISTINCT FROM p_job_id
       OR pg_catalog.jsonb_typeof(v_request_data -> 'request_digest') IS DISTINCT FROM 'string'
       OR (v_request_data ->> 'request_digest') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(v_request_data -> 'prompt_digest') IS DISTINCT FROM 'string'
       OR (v_request_data ->> 'prompt_digest') !~ '^[0-9a-f]{64}$' THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_response');
    END IF;
    v_input := aap_assets_v1.video_execution_input_facts(v_input_id);
    IF v_input IS NULL
       OR v_input ->> 'schema' IS DISTINCT FROM
            'aap-assets-video-execution-input-facts/v1'
       OR v_input -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'ok',false,'reason',COALESCE(v_input #>> '{error,code}','input_unavailable')
        );
    END IF;
    v_input_data := v_input -> 'data';
    IF NOT aap_jobs_v1._video_execution_exact_keys(
        v_input_data,ARRAY[
            'input_id','request_id','job_id','task_id','project_id','plan_digest',
            'timebase_digest','sampling_step','selected_frame_count','source_version_id',
            'source_sha256','byte_size','frame_count','width','height','duration_ms',
            'video_stream'
        ]
    ) THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_response');
    END IF;
    BEGIN
        IF aap_jobs_v1._video_execution_uuid(v_input_data -> 'input_id') IS DISTINCT FROM v_input_id
           OR aap_jobs_v1._video_execution_uuid(v_input_data -> 'request_id') IS DISTINCT FROM v_request_id
           OR aap_jobs_v1._video_execution_uuid(v_input_data -> 'job_id') IS DISTINCT FROM v_job_id
           OR aap_jobs_v1._video_execution_uuid(v_input_data -> 'task_id') IS DISTINCT FROM v_task_id
           OR aap_jobs_v1._video_execution_uuid(v_input_data -> 'project_id') IS DISTINCT FROM v_project_id THEN
            RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_response');
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_response');
    END;
    IF pg_catalog.jsonb_typeof(v_input_data -> 'plan_digest') IS DISTINCT FROM 'string'
       OR (v_input_data ->> 'plan_digest') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(v_input_data -> 'timebase_digest') IS DISTINCT FROM 'string'
       OR (v_input_data ->> 'timebase_digest') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(v_input_data -> 'source_sha256') IS DISTINCT FROM 'string'
       OR (v_input_data ->> 'source_sha256') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(v_input_data -> 'source_version_id') IS DISTINCT FROM 'string'
       OR pg_catalog.btrim(v_input_data ->> 'source_version_id') = ''
       OR (v_input_data ->> 'source_version_id') = 'null' THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_response');
    END IF;
    v_facts := pg_catalog.jsonb_build_object(
        'job_id',v_job_id,
        'input_id',v_input_id,
        'task_id',v_task_id,
        'project_id',v_project_id,
        'source_item_id',CASE WHEN v_source_item IS NULL THEN NULL ELSE to_jsonb(v_source_item) END,
        'request_digest',v_request_data -> 'request_digest',
        'prompt_digest',v_request_data -> 'prompt_digest',
        'source_plan_digest',v_input_data -> 'plan_digest',
        'requested_model_key',v_request_data -> 'requested_model_key',
        'job_kind',v_request_data -> 'job_kind',
        'direction',v_request_data -> 'direction',
        'from_frame',v_request_data -> 'from_frame',
        'to_frame',v_request_data -> 'to_frame',
        'grid_step',v_request_data -> 'grid_step',
        'correction_frame',v_request_data -> 'correction_frame',
        'segment_id',v_request_data -> 'segment_id',
        'source',pg_catalog.jsonb_build_object(
            'source_version_id',v_input_data -> 'source_version_id',
            'source_sha256',v_input_data -> 'source_sha256',
            'byte_size',v_input_data -> 'byte_size',
            'frame_count',v_input_data -> 'frame_count',
            'width',v_input_data -> 'width',
            'height',v_input_data -> 'height',
            'duration_ms',v_input_data -> 'duration_ms',
            'video_stream',v_input_data -> 'video_stream'
        ),
        'timebase',pg_catalog.jsonb_build_object(
            'timebase_digest',v_input_data -> 'timebase_digest',
            'selected_frame_count',v_input_data -> 'selected_frame_count',
            'sampling_step',v_input_data -> 'sampling_step',
            'grid_step',v_request_data -> 'grid_step'
        )
    );
    RETURN pg_catalog.jsonb_build_object('ok',true,'facts',v_facts);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','execution_busy');
    WHEN OTHERS THEN
        RETURN pg_catalog.jsonb_build_object('ok',false,'reason','invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_checkpoint_input_facts(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_checkpoint_integer(
    p_value jsonb,
    p_min bigint,
    p_max bigint
)
RETURNS bigint
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_number numeric;
BEGIN
    IF p_min IS NULL OR p_max IS NULL OR p_min > p_max
       OR pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'number'
       OR (p_value #>> '{}') !~ '^(0|[1-9][0-9]*)$' THEN
        RETURN NULL;
    END IF;
    v_number := (p_value #>> '{}')::numeric;
    IF v_number < p_min OR v_number > p_max THEN RETURN NULL; END IF;
    RETURN v_number::bigint;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_checkpoint_integer(jsonb,bigint,bigint)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_checkpoint_digest_valid(p_value text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT p_value IS NOT NULL AND p_value COLLATE "C" ~ '^[0-9a-f]{64}$'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_checkpoint_digest_valid(text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_checkpoint_plan_identities_complete(
    p_plan jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_backend jsonb;
    v_provider text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_plan -> 'backend_compatibility') IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    FOR v_backend IN SELECT element.value
        FROM pg_catalog.jsonb_array_elements(p_plan -> 'backend_compatibility') AS element(value)
    LOOP
        v_provider := v_backend ->> 'provider_kind';
        IF v_provider = 'builtin' THEN
            IF NOT aap_jobs_v1._video_checkpoint_digest_valid(
                    v_backend ->> 'builtin_implementation_digest'
                ) OR NOT aap_jobs_v1._video_checkpoint_digest_valid(
                    v_backend ->> 'capability_digest'
                ) THEN
                RETURN false;
            END IF;
        ELSIF v_provider = 'registry' THEN
            IF v_backend ->> 'backend_incarnation' IS NULL
               OR v_backend ->> 'model_id' IS NULL
               OR v_backend ->> 'weights_release_id' IS NULL
               OR NOT aap_jobs_v1._video_checkpoint_digest_valid(
                    v_backend ->> 'weights_sha256'
               ) OR NOT aap_jobs_v1._video_checkpoint_digest_valid(
                    v_backend ->> 'capability_digest'
               ) THEN
                RETURN false;
            END IF;
        ELSE
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_checkpoint_plan_identities_complete(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_checkpoint_manifest_valid(
    p_manifest jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_keys text[] := ARRAY[
        'schema','checkpoint_id','manifest_digest','request_id','input_id','plan_digest',
        'window_ordinal','window_digest','context_span_digest','phase','seed_input_digest',
        'predecessor_checkpoint_digest','raw_response_digest','result_digest',
        'result_byte_size','payload_digest','payload_byte_size','payload_part_count',
        'continuation_digest','discovery_output_digest','discovery_seeds_digest'
    ];
    v_core jsonb;
    v_expected_id text;
    v_size bigint;
    v_parts bigint;
BEGIN
    IF NOT aap_jobs_v1._video_execution_exact_keys(p_manifest,v_keys)
       OR p_manifest ->> 'schema' IS DISTINCT FROM 'aap-video-window-result/v1'
       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(p_manifest ->> 'checkpoint_id')
       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(p_manifest ->> 'manifest_digest')
       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(p_manifest ->> 'plan_digest')
       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(p_manifest ->> 'window_digest')
       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(p_manifest ->> 'context_span_digest')
       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(p_manifest ->> 'raw_response_digest')
       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(p_manifest ->> 'result_digest')
       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(p_manifest ->> 'payload_digest')
       OR pg_catalog.jsonb_typeof(p_manifest -> 'request_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_manifest -> 'input_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_manifest -> 'phase') IS DISTINCT FROM 'string'
       OR p_manifest ->> 'phase' NOT IN ('discovery','track') THEN
        RETURN false;
    END IF;
    IF p_manifest -> 'seed_input_digest' <> 'null'::jsonb
       AND NOT aap_jobs_v1._video_checkpoint_digest_valid(
            p_manifest ->> 'seed_input_digest'
       ) THEN RETURN false; END IF;
    IF p_manifest -> 'predecessor_checkpoint_digest' <> 'null'::jsonb
       AND NOT aap_jobs_v1._video_checkpoint_digest_valid(
            p_manifest ->> 'predecessor_checkpoint_digest'
       ) THEN RETURN false; END IF;
    IF p_manifest -> 'continuation_digest' <> 'null'::jsonb
       AND NOT aap_jobs_v1._video_checkpoint_digest_valid(
            p_manifest ->> 'continuation_digest'
       ) THEN RETURN false; END IF;
    IF p_manifest -> 'discovery_output_digest' <> 'null'::jsonb
       AND NOT aap_jobs_v1._video_checkpoint_digest_valid(
            p_manifest ->> 'discovery_output_digest'
       ) THEN RETURN false; END IF;
    IF p_manifest -> 'discovery_seeds_digest' <> 'null'::jsonb
       AND NOT aap_jobs_v1._video_checkpoint_digest_valid(
            p_manifest ->> 'discovery_seeds_digest'
       ) THEN RETURN false; END IF;
    IF aap_jobs_v1._video_execution_uuid(p_manifest -> 'request_id') IS NULL
       OR aap_jobs_v1._video_execution_uuid(p_manifest -> 'input_id') IS NULL THEN
        RETURN false;
    END IF;
    v_size := aap_jobs_v1._video_checkpoint_integer(
        p_manifest -> 'payload_byte_size',1,67108864
    );
    v_parts := aap_jobs_v1._video_checkpoint_integer(
        p_manifest -> 'payload_part_count',1,128
    );
    IF v_size IS NULL OR v_parts IS NULL
       OR v_parts <> GREATEST(1,((v_size + 524287) / 524288))
       OR aap_jobs_v1._video_checkpoint_integer(
            p_manifest -> 'result_byte_size',0,67108864
       ) IS NULL
       OR aap_jobs_v1._video_checkpoint_integer(
            p_manifest -> 'window_ordinal',0,8191
       ) IS NULL THEN
        RETURN false;
    END IF;
    v_expected_id := aap_jobs_v1._video_checkpoint_digest(
        pg_catalog.jsonb_build_object(
            'plan_digest',p_manifest -> 'plan_digest',
            'window_ordinal',p_manifest -> 'window_ordinal'
        )
    );
    v_core := p_manifest - ARRAY['checkpoint_id','manifest_digest'];
    RETURN p_manifest ->> 'checkpoint_id' IS NOT DISTINCT FROM v_expected_id
       AND p_manifest ->> 'manifest_digest' IS NOT DISTINCT FROM
            aap_jobs_v1._video_checkpoint_digest(v_core);
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_checkpoint_manifest_valid(jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_checkpoint_plan_valid(
    p_plan jsonb,
    p_facts jsonb,
    p_job_id uuid,
    p_scope jsonb
)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_context jsonb;
    v_context_member jsonb;
    v_window jsonb;
    v_backend jsonb;
    v_track jsonb;
    v_discovery jsonb;
    v_branch_window jsonb;
    v_previous_window jsonb;
    v_first_track jsonb;
    v_windows bigint;
    v_ordinal bigint := 0;
    v_from bigint;
    v_to bigint;
    v_plan_from bigint;
    v_plan_to bigint;
    v_correction bigint;
    v_frame_count bigint;
    v_span_start bigint;
    v_span_end bigint;
    v_span_from bigint;
    v_span_to bigint;
    v_context_min_from bigint;
    v_context_max_to bigint;
    v_member_from bigint;
    v_member_to bigint;
    v_platform_default bigint;
    v_platform_sam3 bigint;
    v_effective_track bigint;
    v_track_backend_limit bigint;
    v_track_max_window bigint;
    v_track_max_context bigint;
    v_discovery_platform bigint;
    v_discovery_backend bigint;
    v_discovery_max_window bigint;
    v_effective_discovery bigint;
    v_sampling_step bigint;
    v_selected_count bigint;
    v_grid_step bigint;
    v_backend_context_limit bigint;
    v_phase_limit bigint;
    v_track_count bigint := 0;
    v_discovery_count bigint := 0;
    v_branch_count bigint;
    v_branch_index integer;
    v_span_ordinal integer;
    v_roles text[] := ARRAY[]::text[];
    v_expected_branches text[];
    v_provider text;
    v_role text;
    v_branch text;
    v_first_branch text;
    v_seed_source text;
    v_group_phase text;
    v_group_branch text;
    v_group_role text;
    v_correction_seed_digest text;
    v_job_kind text;
    v_direction text;
    v_requested_model_key text;
    v_combo boolean;
BEGIN
    IF pg_catalog.jsonb_typeof(p_plan) IS DISTINCT FROM 'object'
       OR NOT aap_jobs_v1._video_execution_exact_keys(p_plan,ARRAY[
            'schema','request_id','input_id','task_id','project_id','request_digest',
            'prompt_digest','source_plan_digest','source','timebase','requested_model_key',
            'job_kind','direction','from_frame','to_frame','correction_frame','segment_id',
            'window_settings','backend_compatibility','output','windows','plan_digest'
       ]) OR p_plan ->> 'schema' IS DISTINCT FROM 'aap-video-execution-plan/v1'
       OR aap_jobs_v1._video_execution_uuid(p_plan -> 'request_id') IS DISTINCT FROM p_job_id
       OR aap_jobs_v1._video_execution_uuid(p_plan -> 'input_id')
            IS DISTINCT FROM aap_jobs_v1._video_execution_uuid(p_facts -> 'input_id')
       OR aap_jobs_v1._video_execution_uuid(p_plan -> 'task_id')
            IS DISTINCT FROM aap_jobs_v1._video_execution_uuid(p_facts -> 'task_id')
       OR aap_jobs_v1._video_execution_uuid(p_plan -> 'project_id')
            IS DISTINCT FROM aap_jobs_v1._video_execution_uuid(p_facts -> 'project_id')
       OR p_plan -> 'request_digest' IS DISTINCT FROM p_facts -> 'request_digest'
       OR p_plan -> 'prompt_digest' IS DISTINCT FROM p_facts -> 'prompt_digest'
       OR p_plan -> 'source_plan_digest' IS DISTINCT FROM p_facts -> 'source_plan_digest'
       OR p_plan -> 'requested_model_key' IS DISTINCT FROM p_facts -> 'requested_model_key'
       OR p_plan -> 'job_kind' IS DISTINCT FROM p_facts -> 'job_kind'
       OR p_plan -> 'direction' IS DISTINCT FROM p_facts -> 'direction'
       OR p_plan -> 'from_frame' IS DISTINCT FROM p_facts -> 'from_frame'
       OR p_plan -> 'to_frame' IS DISTINCT FROM p_facts -> 'to_frame'
       OR p_plan -> 'correction_frame' IS DISTINCT FROM p_facts -> 'correction_frame'
       OR p_plan -> 'segment_id' IS DISTINCT FROM p_facts -> 'segment_id'
       OR p_plan #> '{timebase,timebase_digest}' IS DISTINCT FROM p_facts #> '{timebase,timebase_digest}'
       OR p_plan #> '{timebase,selected_frame_count}' IS DISTINCT FROM p_facts #> '{timebase,selected_frame_count}'
       OR p_plan #> '{timebase,sampling_step}' IS DISTINCT FROM p_facts #> '{timebase,sampling_step}'
       OR p_plan #> '{timebase,grid_step}' IS DISTINCT FROM p_facts #> '{timebase,grid_step}'
       OR (p_plan -> 'source') IS DISTINCT FROM (p_facts -> 'source')
       OR pg_catalog.jsonb_typeof(p_plan -> 'source') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_plan -> 'timebase') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_plan -> 'window_settings') IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_plan -> 'output') IS DISTINCT FROM 'object'
       OR NOT aap_jobs_v1._video_execution_exact_keys(p_plan -> 'source',ARRAY[
            'source_version_id','source_sha256','byte_size','frame_count','width',
            'height','duration_ms','video_stream'
       ]) OR NOT aap_jobs_v1._video_execution_exact_keys(p_plan -> 'timebase',ARRAY[
            'timebase_digest','selected_frame_count','sampling_step','grid_step'
       ]) OR NOT aap_jobs_v1._video_execution_exact_keys(p_plan -> 'output',ARRAY[
            'geometry','grid_step','result_schema'
       ]) THEN
        RETURN false;
    END IF;
    v_requested_model_key := p_plan ->> 'requested_model_key';
    v_job_kind := p_plan ->> 'job_kind';
    v_direction := p_plan ->> 'direction';
    v_combo := v_requested_model_key = 'sam3_video_combo';
    IF pg_catalog.jsonb_typeof(p_facts) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_plan -> 'requested_model_key') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_plan -> 'job_kind') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_plan -> 'direction') IS DISTINCT FROM 'string'
       OR pg_catalog.length(v_requested_model_key) NOT BETWEEN 1 AND 80
       OR v_job_kind NOT IN ('tracking','correction')
       OR v_direction NOT IN ('forward','backward','bidirectional') THEN
        RETURN false;
    END IF;
    IF NOT aap_jobs_v1._video_execution_exact_keys(p_plan -> 'window_settings',ARRAY[
            'platform_default_frames','platform_sam3_frames','track_backend_max_frames',
            'effective_track_max_frames','combo_discovery_window_frames',
            'discovery_backend_max_frames','effective_discovery_max_frames'
       ]) OR p_plan #> '{output,grid_step}' IS DISTINCT FROM p_facts #> '{timebase,grid_step}'
       OR p_plan #>> '{output,result_schema}' IS DISTINCT FROM
            'aap-video-tracker-frame-results/v1' THEN
        RETURN false;
    END IF;
    v_from := aap_jobs_v1._video_checkpoint_integer(p_plan -> 'from_frame',0,2147483647);
    v_to := aap_jobs_v1._video_checkpoint_integer(p_plan -> 'to_frame',0,2147483647);
    v_correction := CASE
        WHEN p_plan -> 'correction_frame'='null'::jsonb THEN NULL
        ELSE aap_jobs_v1._video_checkpoint_integer(
            p_plan -> 'correction_frame',0,2147483647
        )
    END;
    v_frame_count := aap_jobs_v1._video_checkpoint_integer(
        p_plan #> '{source,frame_count}',1,9007199254740991
    );
    v_sampling_step := aap_jobs_v1._video_checkpoint_integer(
        p_plan #> '{timebase,sampling_step}',1,9007199254740991
    );
    v_selected_count := aap_jobs_v1._video_checkpoint_integer(
        p_plan #> '{timebase,selected_frame_count}',1,9007199254740991
    );
    v_grid_step := aap_jobs_v1._video_checkpoint_integer(
        p_plan #> '{timebase,grid_step}',1,9007199254740991
    );
    v_platform_default := aap_jobs_v1._video_checkpoint_integer(
        p_plan #> '{window_settings,platform_default_frames}',1,9007199254740991
    );
    v_platform_sam3 := aap_jobs_v1._video_checkpoint_integer(
        p_plan #> '{window_settings,platform_sam3_frames}',1,9007199254740991
    );
    v_effective_track := aap_jobs_v1._video_checkpoint_integer(
        p_plan #> '{window_settings,effective_track_max_frames}',1,9007199254740991
    );
    v_track_backend_limit := CASE
        WHEN p_plan #> '{window_settings,track_backend_max_frames}'='null'::jsonb
        THEN NULL
        ELSE aap_jobs_v1._video_checkpoint_integer(
            p_plan #> '{window_settings,track_backend_max_frames}',1,9007199254740991
        )
    END;
    v_discovery_platform := CASE
        WHEN p_plan #> '{window_settings,combo_discovery_window_frames}'='null'::jsonb
        THEN NULL
        ELSE aap_jobs_v1._video_checkpoint_integer(
            p_plan #> '{window_settings,combo_discovery_window_frames}',1,9007199254740991
        )
    END;
    v_discovery_backend := CASE
        WHEN p_plan #> '{window_settings,discovery_backend_max_frames}'='null'::jsonb
        THEN NULL
        ELSE aap_jobs_v1._video_checkpoint_integer(
            p_plan #> '{window_settings,discovery_backend_max_frames}',1,9007199254740991
        )
    END;
    v_effective_discovery := CASE
        WHEN p_plan #> '{window_settings,effective_discovery_max_frames}'='null'::jsonb
        THEN NULL
        ELSE aap_jobs_v1._video_checkpoint_integer(
            p_plan #> '{window_settings,effective_discovery_max_frames}',1,9007199254740991
        )
    END;
    IF v_from IS NULL OR v_to IS NULL OR v_from > v_to
       OR v_frame_count IS NULL OR v_to >= v_frame_count
       OR v_sampling_step IS NULL OR v_selected_count IS NULL OR v_grid_step IS NULL
       OR v_selected_count > ((v_frame_count + v_sampling_step - 1) / v_sampling_step)
       OR v_platform_default IS NULL OR v_platform_sam3 IS NULL
       OR v_effective_track IS NULL
       OR (p_plan #> '{window_settings,track_backend_max_frames}'<>'null'::jsonb
           AND v_track_backend_limit IS NULL)
       OR (p_plan #> '{window_settings,combo_discovery_window_frames}'<>'null'::jsonb
           AND v_discovery_platform IS NULL)
       OR (p_plan #> '{window_settings,discovery_backend_max_frames}'<>'null'::jsonb
           AND v_discovery_backend IS NULL)
       OR (p_plan #> '{window_settings,effective_discovery_max_frames}'<>'null'::jsonb
           AND v_effective_discovery IS NULL)
       OR (v_combo AND (
            v_discovery_platform IS NULL OR v_effective_discovery IS NULL
       ))
       OR (NOT v_combo AND (
            v_discovery_platform IS NOT NULL OR v_discovery_backend IS NOT NULL
            OR v_effective_discovery IS NOT NULL
       ))
       OR p_plan #> '{output,grid_step}' IS DISTINCT FROM p_plan #> '{timebase,grid_step}'
       OR p_plan #>> '{output,geometry}' NOT IN ('bbox','polygon','mask')
       OR pg_catalog.jsonb_typeof(p_plan #> '{output,geometry}') IS DISTINCT FROM 'string'
       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(p_plan ->> 'plan_digest')
       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(
            p_plan #>> '{source,source_sha256}'
       )
       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(
            p_plan #>> '{timebase,timebase_digest}'
       )
       OR p_plan ->> 'plan_digest' IS DISTINCT FROM
            aap_jobs_v1._video_checkpoint_digest(p_plan - 'plan_digest') THEN
        RETURN false;
    END IF;
    v_plan_from := v_from;
    v_plan_to := v_to;
    IF (v_job_kind='tracking' AND (
            v_correction IS NOT NULL OR v_direction='bidirectional'
       )) OR (v_job_kind='correction' AND (
            v_correction IS NULL OR v_correction<v_from OR v_correction>v_to
       )) THEN
        RETURN false;
    END IF;
    v_expected_branches := CASE
        WHEN v_direction='bidirectional' THEN ARRAY['backward','forward']::text[]
        ELSE ARRAY[v_direction]::text[]
    END;
    IF pg_catalog.jsonb_typeof(p_plan #> '{source,source_version_id}') IS DISTINCT FROM 'string'
       OR pg_catalog.btrim(p_plan #>> '{source,source_version_id}')=''
       OR p_plan #>> '{source,source_version_id}'='null'
       OR aap_jobs_v1._video_checkpoint_integer(
            p_plan #> '{source,byte_size}',1,9007199254740991
       ) IS NULL
       OR aap_jobs_v1._video_checkpoint_integer(
        p_plan #> '{source,width}',1,9007199254740991
       ) IS NULL
       OR aap_jobs_v1._video_checkpoint_integer(
        p_plan #> '{source,height}',1,9007199254740991
       ) IS NULL
       OR aap_jobs_v1._video_checkpoint_integer(
            p_plan #> '{source,duration_ms}',0,9007199254740991
       ) IS NULL
       OR aap_jobs_v1._video_checkpoint_integer(
        p_plan #> '{source,video_stream}',0,9007199254740991
       ) IS NULL THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_plan -> 'backend_compatibility') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_plan -> 'backend_compatibility') NOT BETWEEN 1 AND 2 THEN
        RETURN false;
    END IF;
    FOR v_backend IN SELECT element.value
        FROM pg_catalog.jsonb_array_elements(p_plan -> 'backend_compatibility') AS element(value)
    LOOP
        IF NOT aap_jobs_v1._video_execution_exact_keys(v_backend,ARRAY[
            'role','provider_kind','registry_id','backend_incarnation',
            'builtin_implementation_id','builtin_implementation_digest','model_key',
            'model_id','weights_release_id','weights_sha256','tracker_context_mode',
            'max_context_frames','max_window_frames','capability_digest'
        ]) THEN RETURN false; END IF;
        v_roles := array_append(v_roles,v_backend ->> 'role');
        v_provider := v_backend ->> 'provider_kind';
        IF pg_catalog.jsonb_typeof(v_backend -> 'role') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_backend -> 'provider_kind') IS DISTINCT FROM 'string'
           OR v_backend ->> 'role' NOT IN ('track','discovery')
           OR pg_catalog.jsonb_typeof(v_backend -> 'model_key') IS DISTINCT FROM 'string'
           OR pg_catalog.length(v_backend ->> 'model_key') NOT BETWEEN 1 AND 80
           OR pg_catalog.jsonb_typeof(v_backend -> 'tracker_context_mode')
                IS DISTINCT FROM 'string'
           OR v_provider NOT IN ('registry','builtin')
           OR v_backend ->> 'tracker_context_mode' NOT IN ('none','session') THEN
            RETURN false;
        END IF;
        v_role := v_backend ->> 'role';
        v_backend_context_limit := aap_jobs_v1._video_checkpoint_integer(
            v_backend -> 'max_context_frames',0,9007199254740991
        );
        IF v_backend_context_limit IS NULL
           OR (v_backend ->> 'tracker_context_mode'='none'
               AND v_backend_context_limit<>0)
           OR (v_backend ->> 'tracker_context_mode'='session'
               AND v_backend_context_limit<1)
           OR (v_backend -> 'max_window_frames'<>'null'::jsonb
               AND aap_jobs_v1._video_checkpoint_integer(
                    v_backend -> 'max_window_frames',1,9007199254740991
               ) IS NULL)
           OR (v_backend -> 'capability_digest'<>'null'::jsonb
               AND NOT aap_jobs_v1._video_checkpoint_digest_valid(
                    v_backend ->> 'capability_digest'
               ))
           OR (v_backend -> 'weights_sha256'<>'null'::jsonb
               AND NOT aap_jobs_v1._video_checkpoint_digest_valid(
                    v_backend ->> 'weights_sha256'
               ))
           OR ((v_backend -> 'weights_release_id'='null'::jsonb)
               IS DISTINCT FROM (v_backend -> 'weights_sha256'='null'::jsonb))
           OR (v_backend -> 'model_id'<>'null'::jsonb AND (
                pg_catalog.jsonb_typeof(v_backend -> 'model_id') IS DISTINCT FROM 'string'
                OR pg_catalog.length(v_backend ->> 'model_id') NOT BETWEEN 1 AND 255
           ))
           OR (v_backend -> 'weights_release_id'<>'null'::jsonb AND (
                pg_catalog.jsonb_typeof(v_backend -> 'weights_release_id') IS DISTINCT FROM 'string'
                OR pg_catalog.length(v_backend ->> 'weights_release_id') NOT BETWEEN 1 AND 255
           )) THEN
            RETURN false;
        END IF;
        IF v_role='track' THEN
            v_track := v_backend;
            v_track_max_window := CASE
                WHEN v_backend -> 'max_window_frames'='null'::jsonb THEN NULL
                ELSE aap_jobs_v1._video_checkpoint_integer(
                    v_backend -> 'max_window_frames',1,9007199254740991
                )
            END;
            v_track_max_context := v_backend_context_limit;
        ELSE
            v_discovery := v_backend;
            v_discovery_max_window := CASE
                WHEN v_backend -> 'max_window_frames'='null'::jsonb THEN NULL
                ELSE aap_jobs_v1._video_checkpoint_integer(
                    v_backend -> 'max_window_frames',1,9007199254740991
                )
            END;
        END IF;
        IF v_provider = 'builtin' THEN
            IF v_role IS DISTINCT FROM 'track'
               OR v_backend ->> 'model_key' IS DISTINCT FROM 'mock_bbox'
               OR v_backend ->> 'model_id' IS DISTINCT FROM 'mock_bbox'
               OR v_backend -> 'registry_id' <> 'null'::jsonb
               OR v_backend -> 'backend_incarnation' <> 'null'::jsonb
               OR v_backend ->> 'builtin_implementation_id' IS DISTINCT FROM 'aap-mock-bbox/v1'
               OR (v_backend -> 'builtin_implementation_digest'<>'null'::jsonb
                   AND NOT aap_jobs_v1._video_checkpoint_digest_valid(
                       v_backend ->> 'builtin_implementation_digest'
                   ))
               OR v_backend -> 'weights_release_id' <> 'null'::jsonb
               OR v_backend -> 'weights_sha256' <> 'null'::jsonb
               OR v_backend ->> 'tracker_context_mode' IS DISTINCT FROM 'none'
               OR v_backend_context_limit<>0 THEN
                RETURN false;
            END IF;
        ELSE
            BEGIN
                IF aap_jobs_v1._video_execution_uuid(v_backend -> 'registry_id') IS NULL THEN
                    RETURN false;
                END IF;
            EXCEPTION WHEN OTHERS THEN
                RETURN false;
            END;
            IF v_backend -> 'builtin_implementation_id' <> 'null'::jsonb
               OR v_backend -> 'builtin_implementation_digest' <> 'null'::jsonb THEN
                RETURN false;
            END IF;
            IF v_backend -> 'registry_id'='null'::jsonb
               OR (v_backend -> 'backend_incarnation'<>'null'::jsonb AND
                   aap_jobs_v1._video_execution_uuid(v_backend -> 'backend_incarnation') IS NULL) THEN
                RETURN false;
            END IF;
        END IF;
    END LOOP;
    IF v_roles[1] IS DISTINCT FROM 'track'
       OR (pg_catalog.cardinality(v_roles) = 2 AND v_roles[2] IS DISTINCT FROM 'discovery')
       OR v_track IS NULL
       OR (v_combo IS DISTINCT FROM (pg_catalog.cardinality(v_roles)=2))
       OR (v_track ->> 'model_key' IS DISTINCT FROM (
            CASE WHEN v_combo
            THEN 'sam3_video_interactive' ELSE v_requested_model_key END
       ))
       OR (v_combo AND (
            v_discovery IS NULL
            OR v_discovery ->> 'model_key' IS DISTINCT FROM 'sam3_video'
            OR v_discovery ->> 'provider_kind' IS DISTINCT FROM 'registry'
       )) THEN
        RETURN false;
    END IF;
    IF v_track ->> 'provider_kind'='builtin' THEN
        IF p_scope IS NOT NULL AND p_scope <> 'null'::jsonb THEN RETURN false; END IF;
    ELSE
        IF pg_catalog.jsonb_typeof(p_scope) IS DISTINCT FROM 'object'
           OR p_scope ->> 'owner' IS DISTINCT FROM 'tracker:' || p_job_id::text
           OR p_scope ->> 'operation' IS DISTINCT FROM 'tracker'
           OR p_scope -> 'closed' IS DISTINCT FROM 'false'::jsonb
           OR p_scope ->> 'state' IS NULL
           OR p_scope ->> 'state' NOT IN ('reserved','active')
           OR (p_scope ->> 'registry_id') IS DISTINCT FROM (v_track ->> 'registry_id') THEN
            RETURN false;
        END IF;
    END IF;
    IF (p_plan #> '{window_settings,track_backend_max_frames}')
            IS DISTINCT FROM (v_track -> 'max_window_frames')
       OR v_effective_track > (CASE WHEN v_requested_model_key LIKE 'sam3%'
            THEN v_platform_sam3 ELSE v_platform_default END)
       OR (v_track_max_window IS NOT NULL
           AND v_effective_track>v_track_max_window)
       OR (NOT v_combo AND v_track ->> 'tracker_context_mode'='session'
           AND v_effective_track>v_track_max_context)
       OR (v_combo AND (
            (p_plan #> '{window_settings,discovery_backend_max_frames}')
                IS DISTINCT FROM (v_discovery -> 'max_window_frames')
            OR v_effective_discovery>v_discovery_platform
            OR v_effective_discovery>v_platform_sam3
            OR (v_discovery_max_window IS NOT NULL
                AND v_effective_discovery>v_discovery_max_window)
       )) THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(p_plan -> 'windows') IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    v_windows := pg_catalog.jsonb_array_length(p_plan -> 'windows');
    IF v_windows NOT BETWEEN 1 AND 8192 THEN RETURN false; END IF;
    FOR v_window IN
        SELECT element.value FROM pg_catalog.jsonb_array_elements(p_plan -> 'windows')
            WITH ORDINALITY AS element(value,ordinality) ORDER BY element.ordinality
    LOOP
        IF NOT aap_jobs_v1._video_execution_exact_keys(v_window,ARRAY[
            'phase','backend_role','branch','ordinal','from_frame','to_frame',
            'is_seed_window','seed_required','seed_source','seed_digest',
            'context_span','window_digest'
        ]) THEN RETURN false; END IF;
        IF aap_jobs_v1._video_checkpoint_integer(v_window -> 'ordinal',0,8191)
                IS DISTINCT FROM v_ordinal
           OR NOT aap_jobs_v1._video_checkpoint_digest_valid(v_window ->> 'window_digest')
           OR v_window ->> 'window_digest' IS DISTINCT FROM
                aap_jobs_v1._video_checkpoint_digest(v_window - 'window_digest')
           OR pg_catalog.jsonb_typeof(v_window -> 'is_seed_window') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_window -> 'seed_required') IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_window -> 'phase') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_window -> 'branch') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_window -> 'backend_role') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_window -> 'seed_source') IS DISTINCT FROM 'string'
           OR v_window ->> 'phase' NOT IN ('discovery','track')
           OR v_window ->> 'branch' NOT IN ('forward','backward')
           OR v_window ->> 'seed_source' NOT IN (
                'none','prompt','source_geometry','correction','discovery','continuation','session'
           ) THEN
            RETURN false;
        END IF;
        v_from := aap_jobs_v1._video_checkpoint_integer(v_window -> 'from_frame',0,2147483647);
        v_to := aap_jobs_v1._video_checkpoint_integer(v_window -> 'to_frame',0,2147483647);
        IF v_from IS NULL OR v_to IS NULL OR v_from > v_to
           OR v_from < aap_jobs_v1._video_checkpoint_integer(p_plan -> 'from_frame',0,2147483647)
           OR v_to > aap_jobs_v1._video_checkpoint_integer(p_plan -> 'to_frame',0,2147483647)
           OR NOT aap_jobs_v1._video_execution_exact_keys(v_window -> 'context_span',ARRAY[
                'start_ordinal','end_ordinal','from_frame','to_frame','mode','span_digest'
           ]) THEN
            RETURN false;
        END IF;
        IF v_window ->> 'phase'='discovery' THEN
            v_discovery_count := v_discovery_count + 1;
            v_phase_limit := v_effective_discovery;
            v_backend := v_discovery;
        ELSE
            v_track_count := v_track_count + 1;
            v_phase_limit := v_effective_track;
            v_backend := v_track;
            IF NOT (v_window ->> 'branch'=ANY(v_expected_branches)) THEN
                RETURN false;
            END IF;
        END IF;
        IF v_phase_limit IS NULL OR v_to-v_from+1>v_phase_limit
           OR v_window ->> 'backend_role' IS DISTINCT FROM v_backend ->> 'role' THEN
            RETURN false;
        END IF;
        v_context := v_window -> 'context_span';
        v_span_start := aap_jobs_v1._video_checkpoint_integer(
            v_context -> 'start_ordinal',0,8191
        );
        v_span_end := aap_jobs_v1._video_checkpoint_integer(
            v_context -> 'end_ordinal',0,8191
        );
        v_span_from := aap_jobs_v1._video_checkpoint_integer(
            v_context -> 'from_frame',0,2147483647
        );
        v_span_to := aap_jobs_v1._video_checkpoint_integer(
            v_context -> 'to_frame',0,2147483647
        );
        IF pg_catalog.jsonb_typeof(v_context -> 'mode') IS DISTINCT FROM 'string'
           OR v_span_start IS NULL OR v_span_end IS NULL
           OR v_span_from IS NULL OR v_span_to IS NULL
           OR v_span_start > v_span_end OR v_span_end >= v_windows
           OR v_span_from < aap_jobs_v1._video_checkpoint_integer(
                p_plan -> 'from_frame',0,2147483647
           )
           OR v_span_to > aap_jobs_v1._video_checkpoint_integer(
                p_plan -> 'to_frame',0,2147483647
           )
           OR v_span_from > v_span_to OR v_span_from>v_from OR v_span_to<v_to
           OR v_span_start > v_ordinal
           OR v_span_end < v_ordinal
           OR v_context ->> 'mode' NOT IN ('none','session')
           OR NOT aap_jobs_v1._video_checkpoint_digest_valid(v_context ->> 'span_digest')
           OR v_context ->> 'span_digest' IS DISTINCT FROM
                aap_jobs_v1._video_checkpoint_digest(v_context - 'span_digest')
           OR (v_context ->> 'mode' = 'none' AND (
                v_span_start <> v_ordinal
                OR v_span_end <> v_ordinal
                OR v_span_from <> v_from
                OR v_span_to <> v_to
           )) THEN
            RETURN false;
        END IF;
        IF v_span_start=v_ordinal THEN
            v_context_min_from := NULL;
            v_context_max_to := NULL;
            v_group_phase := v_window ->> 'phase';
            v_group_branch := v_window ->> 'branch';
            v_group_role := v_window ->> 'backend_role';
            FOR v_span_ordinal IN v_span_start..v_span_end LOOP
                v_context_member := p_plan -> 'windows' -> v_span_ordinal;
                IF v_context_member IS NULL
                   OR v_context_member -> 'context_span' IS DISTINCT FROM v_context
                   OR v_context_member ->> 'phase' IS DISTINCT FROM v_group_phase
                   OR v_context_member ->> 'branch' IS DISTINCT FROM v_group_branch
                   OR v_context_member ->> 'backend_role' IS DISTINCT FROM v_group_role THEN
                    RETURN false;
                END IF;
                v_member_from := aap_jobs_v1._video_checkpoint_integer(
                    v_context_member -> 'from_frame',0,2147483647
                );
                v_member_to := aap_jobs_v1._video_checkpoint_integer(
                    v_context_member -> 'to_frame',0,2147483647
                );
                IF v_member_from IS NULL OR v_member_to IS NULL THEN
                    RETURN false;
                END IF;
                v_context_min_from := CASE
                    WHEN v_context_min_from IS NULL THEN v_member_from
                    ELSE LEAST(v_context_min_from,v_member_from)
                END;
                v_context_max_to := CASE
                    WHEN v_context_max_to IS NULL THEN v_member_to
                    ELSE GREATEST(v_context_max_to,v_member_to)
                END;
            END LOOP;
            IF v_context_min_from IS DISTINCT FROM v_span_from
               OR v_context_max_to IS DISTINCT FROM v_span_to THEN
                RETURN false;
            END IF;
        END IF;
        IF v_context ->> 'mode'='session' AND (
            v_window ->> 'phase' IS DISTINCT FROM 'track'
            OR v_backend ->> 'tracker_context_mode' IS DISTINCT FROM 'session'
            OR v_span_to-v_span_from+1>v_track_max_context
        ) THEN
            RETURN false;
        END IF;
        IF v_window ->> 'phase'='track' AND NOT v_combo
           AND v_window ->> 'seed_source'='discovery' THEN
            RETURN false;
        END IF;
        IF v_window ->> 'phase' = 'discovery'
           AND (v_window ->> 'backend_role' IS DISTINCT FROM 'discovery'
                OR v_window ->> 'branch' IS DISTINCT FROM 'forward'
                OR v_window ->> 'seed_source' IS DISTINCT FROM 'none'
                OR v_window -> 'is_seed_window' IS DISTINCT FROM 'false'::jsonb
                OR v_window -> 'seed_digest' IS DISTINCT FROM 'null'::jsonb
                OR v_window -> 'seed_required' IS DISTINCT FROM 'false'::jsonb
                OR v_context ->> 'mode' IS DISTINCT FROM 'none') THEN
            RETURN false;
        ELSIF v_window ->> 'phase' = 'track'
           AND v_window ->> 'backend_role' IS DISTINCT FROM 'track' THEN
            RETURN false;
        END IF;
        IF v_window ->> 'seed_source' IN ('prompt','source_geometry','correction') THEN
            IF NOT aap_jobs_v1._video_checkpoint_digest_valid(v_window ->> 'seed_digest')
               OR v_window -> 'seed_required' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN false;
            END IF;
        ELSIF v_window -> 'seed_digest' <> 'null'::jsonb
           OR (v_window ->> 'seed_source' NOT IN ('none','session')
               AND v_window -> 'seed_required' IS DISTINCT FROM 'true'::jsonb)
           OR (v_window ->> 'seed_source' IN ('none','session')
               AND v_window -> 'seed_required' IS DISTINCT FROM 'false'::jsonb) THEN
            RETURN false;
        END IF;
        v_ordinal := v_ordinal + 1;
    END LOOP;
    IF v_track_count<1
       OR (v_combo AND v_discovery_count<>1)
       OR (NOT v_combo AND v_discovery_count<>0)
       OR (v_combo AND v_job_kind='correction') THEN
        RETURN false;
    END IF;
    IF v_combo THEN
        v_window := p_plan -> 'windows' -> 0;
        v_from := aap_jobs_v1._video_checkpoint_integer(
            v_window -> 'from_frame',0,2147483647
        );
        v_to := aap_jobs_v1._video_checkpoint_integer(
            v_window -> 'to_frame',0,2147483647
        );
        IF v_window ->> 'phase' IS DISTINCT FROM 'discovery'
           OR (v_direction='backward' AND v_to IS DISTINCT FROM v_plan_to)
           OR (v_direction<>'backward' AND v_from IS DISTINCT FROM v_plan_from) THEN
            RETURN false;
        END IF;
    ELSE
        v_window := p_plan -> 'windows' -> 0;
        IF v_window ->> 'phase' IS DISTINCT FROM 'track' THEN
            RETURN false;
        END IF;
    END IF;
    SELECT element.value ->> 'branch'
    INTO v_first_branch
    FROM pg_catalog.jsonb_array_elements(p_plan -> 'windows')
        WITH ORDINALITY AS element(value,ordinality)
    WHERE element.value ->> 'phase'='track'
    ORDER BY element.ordinality
    LIMIT 1;
    IF v_first_branch IS DISTINCT FROM v_expected_branches[1] THEN
        RETURN false;
    END IF;
    IF v_direction='bidirectional' THEN
        SELECT max((element.value ->> 'ordinal')::bigint)
        INTO v_span_end
        FROM pg_catalog.jsonb_array_elements(p_plan -> 'windows') AS element(value)
        WHERE element.value ->> 'phase'='track'
          AND element.value ->> 'branch'='backward';
        SELECT min((element.value ->> 'ordinal')::bigint)
        INTO v_span_start
        FROM pg_catalog.jsonb_array_elements(p_plan -> 'windows') AS element(value)
        WHERE element.value ->> 'phase'='track'
          AND element.value ->> 'branch'='forward';
        IF v_span_end IS NULL OR v_span_start IS NULL OR v_span_end>=v_span_start THEN
            RETURN false;
        END IF;
    END IF;
    FOR v_branch_index IN 1..pg_catalog.cardinality(v_expected_branches) LOOP
        v_branch := v_expected_branches[v_branch_index];
        v_branch_count := 0;
        v_previous_window := NULL;
        FOR v_branch_window IN
            SELECT element.value
            FROM pg_catalog.jsonb_array_elements(p_plan -> 'windows')
                WITH ORDINALITY AS element(value,ordinality)
            WHERE element.value ->> 'phase'='track'
              AND element.value ->> 'branch'=v_branch
            ORDER BY element.ordinality
        LOOP
            v_branch_count := v_branch_count + 1;
            v_from := aap_jobs_v1._video_checkpoint_integer(
                v_branch_window -> 'from_frame',0,2147483647
            );
            v_to := aap_jobs_v1._video_checkpoint_integer(
                v_branch_window -> 'to_frame',0,2147483647
            );
            v_seed_source := v_branch_window ->> 'seed_source';
            IF v_branch_count=1 THEN
                IF v_branch_window -> 'is_seed_window' IS DISTINCT FROM 'true'::jsonb
                   OR v_seed_source IN ('continuation','session') THEN
                    RETURN false;
                END IF;
                IF v_branch='backward' THEN
                    IF v_to IS DISTINCT FROM (CASE
                        WHEN v_job_kind='correction' THEN v_correction
                        ELSE v_plan_to
                    END) THEN
                        RETURN false;
                    END IF;
                ELSIF v_from IS DISTINCT FROM (CASE
                    WHEN v_job_kind='correction' THEN v_correction
                    ELSE v_plan_from
                END) THEN
                    RETURN false;
                END IF;
                IF v_job_kind='correction' THEN
                    IF v_seed_source IS DISTINCT FROM 'correction'
                       OR NOT aap_jobs_v1._video_checkpoint_digest_valid(
                            v_branch_window ->> 'seed_digest'
                       ) THEN
                        RETURN false;
                    END IF;
                    IF v_correction_seed_digest IS NULL THEN
                        v_correction_seed_digest := v_branch_window ->> 'seed_digest';
                    ELSIF v_correction_seed_digest IS DISTINCT FROM
                        v_branch_window ->> 'seed_digest' THEN
                        RETURN false;
                    END IF;
                ELSIF v_combo THEN
                    IF v_seed_source IS DISTINCT FROM 'discovery'
                       OR v_branch_window -> 'seed_required'
                            IS DISTINCT FROM 'true'::jsonb
                       OR v_branch_window -> 'seed_digest' IS DISTINCT FROM 'null'::jsonb THEN
                        RETURN false;
                    END IF;
                END IF;
            ELSE
                IF v_branch_window -> 'is_seed_window' IS DISTINCT FROM 'false'::jsonb THEN
                    RETURN false;
                END IF;
                IF v_branch='forward'
                   AND v_from <> (v_previous_window ->> 'to_frame')::bigint + 1 THEN
                    RETURN false;
                ELSIF v_branch='backward'
                   AND v_to <> (v_previous_window ->> 'from_frame')::bigint - 1 THEN
                    RETURN false;
                END IF;
                IF (v_branch_window #>> '{context_span,start_ordinal}')::bigint
                        = (v_branch_window ->> 'ordinal')::bigint THEN
                    IF v_seed_source IS DISTINCT FROM 'continuation'
                       OR v_branch_window -> 'seed_required'
                            IS DISTINCT FROM 'true'::jsonb THEN
                        RETURN false;
                    END IF;
                ELSIF v_branch_window #>> '{context_span,mode}' IS DISTINCT FROM 'session'
                   OR v_seed_source IS DISTINCT FROM 'session'
                   OR v_branch_window -> 'seed_required' IS DISTINCT FROM 'false'::jsonb THEN
                    RETURN false;
                END IF;
            END IF;
            v_previous_window := v_branch_window;
        END LOOP;
        IF v_branch_count=0 THEN
            RETURN false;
        END IF;
        SELECT element.value INTO v_first_track
        FROM pg_catalog.jsonb_array_elements(p_plan -> 'windows')
            WITH ORDINALITY AS element(value,ordinality)
        WHERE element.value ->> 'phase'='track'
          AND element.value ->> 'branch'=v_branch
        ORDER BY element.ordinality
        LIMIT 1;
        IF v_job_kind='tracking' AND v_direction='forward' THEN
            IF (v_branch_window ->> 'to_frame')::bigint<>v_plan_to
               OR (v_first_track ->> 'from_frame')::bigint<>v_plan_from THEN
                RETURN false;
            END IF;
        ELSIF v_job_kind='tracking' AND v_direction='backward' THEN
            IF (v_branch_window ->> 'from_frame')::bigint<>v_plan_from
               OR (v_first_track ->> 'to_frame')::bigint<>v_plan_to THEN
                RETURN false;
            END IF;
        ELSIF v_job_kind='correction' AND v_branch='backward' THEN
            IF (v_branch_window ->> 'from_frame')::bigint<>v_plan_from
               OR (v_first_track ->> 'to_frame')::bigint<>v_correction THEN
                RETURN false;
            END IF;
        ELSE
            IF (v_branch_window ->> 'to_frame')::bigint<>v_plan_to
               OR (v_first_track ->> 'from_frame')::bigint<>v_correction THEN
                RETURN false;
            END IF;
        END IF;
    END LOOP;
    RETURN true;
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_checkpoint_plan_valid(jsonb,jsonb,uuid,jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_checkpoint_results_complete(p_job_id uuid,p_run_id uuid)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_jobs_v1.video_execution_journals AS journal
        WHERE journal.job_id=p_job_id AND journal.run_id=p_run_id
          AND journal.plan_document IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.jsonb_array_elements(journal.plan_document -> 'windows') AS planned(value)
              WHERE NOT EXISTS (
                  SELECT 1 FROM aap_jobs_v1.video_execution_windows AS result
                  WHERE result.job_id=p_job_id AND result.run_id=p_run_id
                    AND result.window_ordinal=(planned.value ->> 'ordinal')::integer
                    AND result.manifest ->> 'window_digest'=planned.value ->> 'window_digest'
              ) OR (planned.value #>> '{context_span,mode}'='session' AND NOT EXISTS (
                  SELECT 1 FROM aap_jobs_v1.video_execution_span_closures AS closure
                  WHERE closure.job_id=p_job_id AND closure.run_id=p_run_id
                    AND closure.plan_digest=journal.plan_digest
                    AND closure.context_span_digest=planned.value #>> '{context_span,span_digest}'
              ))
          )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_checkpoint_results_complete(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.video_execution_checkpoint_step(
    p_action text,
    p_arguments text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
SET lock_timeout = '250ms'
AS $function$
DECLARE
    v_args jsonb;
    v_keys text[];
    v_proof jsonb;
    v_reply jsonb;
    v_route jsonb;
    v_scope_reply jsonb;
    v_scope jsonb;
    v_facts_reply jsonb;
    v_facts jsonb;
    v_guard jsonb;
    v_plan jsonb;
    v_manifest jsonb;
    v_payload jsonb;
    v_payload_json json;
    v_payload_text text;
    v_bytes bytea;
    v_part bytea;
    v_part_digest text;
    v_base64 text;
    v_attempt aap_jobs_v1.video_execution_window_attempts%ROWTYPE;
    v_capture aap_jobs_v1.video_execution_captures%ROWTYPE;
    v_window aap_jobs_v1.video_execution_windows%ROWTYPE;
    v_journal aap_jobs_v1.video_execution_journals%ROWTYPE;
    v_execution aap_jobs_v1.video_executions%ROWTYPE;
    v_job record;
    v_found boolean;
    v_job_missing boolean := false;
    v_has_journal boolean := false;
    v_reason text;
    v_worker_id uuid;
    v_job_id uuid;
    v_run_id uuid;
    v_token uuid;
    v_generation bigint;
    v_window_ordinal integer;
    v_after integer;
    v_limit integer;
    v_last integer;
    v_has_more boolean := false;
    v_index integer;
    v_part_count bigint;
    v_total_bytes bigint;
    v_expected_bytes bigint;
    v_payload_byte_size bigint;
    v_payload_part_count bigint;
    v_payload_key_count bigint;
    v_payload_unique_keys bigint;
    v_expected_seed text;
    v_expected_predecessor text;
    v_previous_ordinal integer;
    v_previous_manifest jsonb;
    v_span_digest text;
    v_span_mode text;
    v_span_closed boolean;
    v_outcome text;
    v_plan_digest text;
    v_checkpoint_id text;
    v_manifest_digest text;
    v_raw_response_digest text;
    v_read_resume_windows jsonb := '[]'::jsonb;
    v_closed_spans jsonb := '[]'::jsonb;
    v_seen_spans jsonb := '{}'::jsonb;
    v_attempt_view jsonb;
    v_checkpoint_view jsonb;
    v_window_view jsonb;
    v_part_view jsonb;
    v_data jsonb;
    v_window_json jsonb;
    v_scope_status jsonb;
    v_identity_complete boolean;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 2097152 THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    END IF;
    BEGIN
        v_args := p_arguments::jsonb;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    END;
    IF pg_catalog.jsonb_typeof(v_args) IS DISTINCT FROM 'object' THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    END IF;
    IF p_action = 'policy' THEN
        IF NOT aap_jobs_v1._video_execution_exact_keys(v_args,ARRAY[]::text[]) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END IF;
        RETURN aap_jobs_v1._video_checkpoint_reply(pg_catalog.jsonb_build_object(
            'ready',aap_jobs_v1._video_execution_secret() IS NOT NULL,
            'functions',pg_catalog.jsonb_build_array(
                'aap_jobs_v1.video_execution_step(text,text)',
                'aap_jobs_v1.video_execution_checkpoint_step(text,text)'
            )
        ));
    END IF;
    IF p_action = 'input_facts' OR p_action = 'read_plan'
       OR p_action = 'recovery_ready' OR p_action = 'retire' THEN
        v_keys := ARRAY[]::text[];
    ELSIF p_action = 'freeze_plan' THEN
        v_keys := ARRAY['plan'];
    ELSIF p_action = 'read_resume' THEN
        v_keys := ARRAY['after_ordinal','limit'];
    ELSIF p_action = 'begin_window' THEN
        v_keys := ARRAY[
            'plan_digest','window_ordinal','window_digest','seed_input_digest',
            'predecessor_checkpoint_digest'
        ];
    ELSIF p_action = 'capture_window' THEN
        v_keys := ARRAY['plan_digest','window_ordinal','capture_id','manifest'];
    ELSIF p_action = 'put_part' THEN
        v_keys := ARRAY['capture_id','index','sha256','data_base64'];
    ELSIF p_action = 'seal_window' THEN
        v_keys := ARRAY['capture_id'];
    ELSIF p_action = 'read_part' THEN
        v_keys := ARRAY['checkpoint_id','index'];
    ELSIF p_action = 'verify_replay' THEN
        v_keys := ARRAY[
            'plan_digest','window_ordinal','capture_id','checkpoint_id','result_digest',
            'continuation_digest','discovery_seeds_digest','seed_input_digest',
            'predecessor_checkpoint_digest','raw_response_digest'
        ];
    ELSIF p_action = 'close_span' THEN
        v_keys := ARRAY['plan_digest','context_span_digest'];
    ELSIF p_action = 'finish' THEN
        v_keys := ARRAY['outcome'];
    ELSE
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    END IF;
    v_keys := ARRAY[
        'job_id','run_id','worker_id','generation','lease_token'
    ] || v_keys;
    IF NOT aap_jobs_v1._video_execution_exact_keys(v_args,v_keys) THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    END IF;
    BEGIN
        v_job_id := aap_jobs_v1._video_execution_uuid(v_args -> 'job_id');
        v_run_id := aap_jobs_v1._video_execution_uuid(v_args -> 'run_id');
        v_worker_id := aap_jobs_v1._video_execution_uuid(v_args -> 'worker_id');
        v_token := aap_jobs_v1._video_execution_uuid(v_args -> 'lease_token');
        v_generation := aap_jobs_v1._video_checkpoint_integer(
            v_args -> 'generation',1,9223372036854775807
        );
        IF v_generation IS NULL THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END IF;
        IF p_action IN ('begin_window','capture_window','verify_replay','close_span') THEN
            v_plan_digest := v_args ->> 'plan_digest';
            IF NOT aap_jobs_v1._video_checkpoint_digest_valid(v_plan_digest) THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
            END IF;
        END IF;
        IF p_action IN ('begin_window','capture_window','verify_replay') THEN
            v_window_ordinal := aap_jobs_v1._video_checkpoint_integer(
                v_args -> 'window_ordinal',0,8191
            );
            IF v_window_ordinal IS NULL THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
            END IF;
        END IF;
    EXCEPTION WHEN OTHERS THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    END;
    v_proof := pg_catalog.jsonb_build_object(
        'job_id',v_job_id,'run_id',v_run_id,'worker_id',v_worker_id,
        'generation',v_generation,'lease_token',v_token
    );
    -- This exact status probe holds the canonical Job -> execution locks and
    -- accepts the current cleanup proof while recovery_required. It never grants
    -- write authority; writer actions below additionally call the core guard.
    v_route := aap_jobs_v1._video_execution_route_step('status',v_proof::text);
    IF v_route IS NULL OR v_route -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(
            NULL,COALESCE(v_route ->> 'reason','lease_lost')
        );
    END IF;
    SELECT execution.* INTO v_execution
    FROM aap_jobs_v1.video_executions AS execution
    WHERE execution.job_id = v_job_id;
    IF NOT FOUND OR v_execution.run_id IS DISTINCT FROM v_run_id
       OR v_execution.generation IS DISTINCT FROM v_generation THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'lease_lost');
    END IF;
    SELECT job.status,job.cancel_requested_at INTO v_job
    FROM public.video_tracker_jobs AS job WHERE job.id = v_job_id;
    v_job_missing := NOT FOUND;
    IF p_action IN (
        'freeze_plan','begin_window','put_part'
    ) THEN
        v_guard := aap_jobs_v1.video_execution_step(
            'guard', (v_proof || pg_catalog.jsonb_build_object(
                'purpose',CASE WHEN p_action IN ('freeze_plan','begin_window')
                               THEN 'start' ELSE 'progress' END
            ))::text
        );
        IF v_guard IS NULL
           OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-video-execution-data/v1'
           OR v_guard ->> 'status' IS DISTINCT FROM 'ok' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(
                NULL,COALESCE(v_guard ->> 'reason','execution_busy')
            );
        END IF;
    END IF;

    IF p_action = 'input_facts' THEN
        v_facts_reply := aap_jobs_v1._video_checkpoint_input_facts(v_job_id);
        IF v_facts_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(
                NULL,COALESCE(v_facts_reply ->> 'reason','input_unavailable')
            );
        END IF;
        RETURN aap_jobs_v1._video_checkpoint_reply(
            pg_catalog.jsonb_build_object('facts',v_facts_reply -> 'facts')
        );
    END IF;

    IF p_action IN ('input_facts','read_plan','read_resume','read_part') THEN
        SELECT journal.* INTO v_journal
        FROM aap_jobs_v1.video_execution_journals AS journal
        WHERE journal.job_id=v_job_id;
    ELSE
        SELECT journal.* INTO v_journal
        FROM aap_jobs_v1.video_execution_journals AS journal
        WHERE journal.job_id=v_job_id
        FOR UPDATE NOWAIT;
    END IF;
    v_has_journal := FOUND;
    IF v_has_journal AND v_journal.run_id IS DISTINCT FROM v_run_id THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'lease_lost');
    END IF;
    IF p_action = 'freeze_plan' THEN
        IF v_job_missing OR v_job.status IS DISTINCT FROM 'running'
           OR v_job.cancel_requested_at IS NOT NULL THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'job_not_active');
        END IF;
        IF v_execution.state <> 'ready' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'recovery_required');
        END IF;
        IF pg_catalog.jsonb_typeof(v_args -> 'plan') IS DISTINCT FROM 'object'
           OR pg_catalog.octet_length((v_args -> 'plan')::text) > 2097152 THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END IF;
        v_facts_reply := aap_jobs_v1._video_checkpoint_input_facts(v_job_id);
        IF v_facts_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(
                NULL,COALESCE(v_facts_reply ->> 'reason','input_unavailable')
            );
        END IF;
        v_facts := v_facts_reply -> 'facts';
        v_scope := NULL;
        IF v_route -> 'intent' IS NOT NULL AND v_route -> 'intent' <> 'null'::jsonb THEN
            v_scope := aap_inference_routing_v1.video_execution_scope_facts(
                aap_jobs_v1._video_execution_uuid(v_route #> '{intent,scope_id}')
            );
            IF v_scope IS NULL THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'scope_unavailable');
            END IF;
        END IF;
        IF NOT aap_jobs_v1._video_checkpoint_plan_valid(
            v_args -> 'plan',v_facts,v_job_id,v_scope
        ) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_mismatch');
        END IF;
        IF v_has_journal THEN
            IF v_journal.plan_document IS NOT NULL THEN
                IF v_journal.plan_digest IS DISTINCT FROM v_args #>> '{plan,plan_digest}'
                   OR v_journal.plan_document IS DISTINCT FROM v_args -> 'plan' THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_conflict');
                END IF;
                RETURN aap_jobs_v1._video_checkpoint_reply(
                    pg_catalog.jsonb_build_object('plan',v_journal.plan_document)
                );
            END IF;
            IF v_journal.writer_terminal OR v_journal.execution_retired
               OR v_journal.run_id IS DISTINCT FROM v_run_id THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'job_not_active');
            END IF;
            IF (v_journal.input_id IS NOT NULL AND v_journal.input_id IS DISTINCT FROM
                    aap_jobs_v1._video_execution_uuid(v_facts -> 'input_id'))
               OR (v_journal.request_digest IS NOT NULL AND v_journal.request_digest
                    IS DISTINCT FROM v_facts ->> 'request_digest') THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_conflict');
            END IF;
            UPDATE aap_jobs_v1.video_execution_journals AS journal
            SET input_id=aap_jobs_v1._video_execution_uuid(v_facts -> 'input_id'),
                task_id=aap_jobs_v1._video_execution_uuid(v_facts -> 'task_id'),
                project_id=aap_jobs_v1._video_execution_uuid(v_facts -> 'project_id'),
                source_item_id=CASE WHEN v_facts -> 'source_item_id'='null'::jsonb
                    THEN NULL ELSE aap_jobs_v1._video_execution_uuid(v_facts -> 'source_item_id') END,
                request_digest=v_facts ->> 'request_digest',
                prompt_digest=v_facts ->> 'prompt_digest',
                source_plan_digest=v_facts ->> 'source_plan_digest',
                timebase_digest=v_facts #>> '{timebase,timebase_digest}',
                plan_digest=v_args #>> '{plan,plan_digest}',
                plan_document=v_args -> 'plan',updated_at=pg_catalog.clock_timestamp()
            WHERE journal.job_id=v_job_id AND journal.run_id=v_run_id;
        ELSE
            INSERT INTO aap_jobs_v1.video_execution_journals(
                job_id,run_id,input_id,task_id,project_id,source_item_id,
                request_digest,prompt_digest,source_plan_digest,timebase_digest,
                plan_digest,plan_document
            ) VALUES (
                v_job_id,v_run_id,
                aap_jobs_v1._video_execution_uuid(v_facts -> 'input_id'),
                aap_jobs_v1._video_execution_uuid(v_facts -> 'task_id'),
                aap_jobs_v1._video_execution_uuid(v_facts -> 'project_id'),
                CASE WHEN v_facts -> 'source_item_id'='null'::jsonb THEN NULL
                     ELSE aap_jobs_v1._video_execution_uuid(v_facts -> 'source_item_id') END,
                v_facts ->> 'request_digest',v_facts ->> 'prompt_digest',
                v_facts ->> 'source_plan_digest',v_facts #>> '{timebase,timebase_digest}',
                v_args #>> '{plan,plan_digest}',v_args -> 'plan'
            );
        END IF;
        RETURN aap_jobs_v1._video_checkpoint_reply(
            pg_catalog.jsonb_build_object('plan',v_args -> 'plan')
        );
    END IF;
    IF p_action = 'read_plan' THEN
        IF v_has_journal THEN v_plan := v_journal.plan_document; END IF;
        RETURN aap_jobs_v1._video_checkpoint_reply(
            pg_catalog.jsonb_build_object('plan',v_plan)
        );
    END IF;
    IF p_action = 'read_resume' THEN
        IF NOT v_has_journal OR v_journal.plan_document IS NULL THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_not_frozen');
        END IF;
        IF pg_catalog.jsonb_typeof(v_args -> 'after_ordinal') IS DISTINCT FROM 'number'
           OR (v_args ->> 'after_ordinal') !~ '^(-1|0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(v_args -> 'limit') IS DISTINCT FROM 'number' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END IF;
        v_after := (v_args ->> 'after_ordinal')::integer;
        v_limit := aap_jobs_v1._video_checkpoint_integer(v_args -> 'limit',1,100);
        IF v_after NOT BETWEEN -1 AND 8191 OR v_limit IS NULL THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END IF;
        FOR v_window_json IN
            SELECT item.value
            FROM pg_catalog.jsonb_array_elements(v_journal.plan_document -> 'windows')
                WITH ORDINALITY AS item(value,ordinality)
            WHERE (item.ordinality - 1) > v_after
            ORDER BY item.ordinality
            LIMIT v_limit
        LOOP
            v_window_ordinal := (v_window_json ->> 'ordinal')::integer;
            v_checkpoint_view := NULL;
            SELECT result.manifest INTO v_checkpoint_view
            FROM aap_jobs_v1.video_execution_windows AS result
            WHERE result.job_id=v_job_id AND result.run_id=v_run_id
              AND result.window_ordinal=v_window_ordinal;
            v_attempt_view := NULL;
            SELECT pg_catalog.jsonb_build_object(
                'generation',attempt.generation,'mode',attempt.mode,'state',attempt.state,
                'capture_id',attempt.capture_id,
                'seed_input_digest',attempt.seed_input_digest,
                'predecessor_checkpoint_digest',attempt.predecessor_checkpoint_digest
            ) INTO v_attempt_view
            FROM aap_jobs_v1.video_execution_window_attempts AS attempt
            WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
              AND attempt.generation=v_generation
              AND attempt.window_ordinal=v_window_ordinal;
            v_read_resume_windows := v_read_resume_windows || pg_catalog.jsonb_build_array(
                pg_catalog.jsonb_build_object(
                    'ordinal',v_window_ordinal,
                    'window_digest',v_window_json -> 'window_digest',
                    'context_span_digest',v_window_json #> '{context_span,span_digest}',
                    'phase',v_window_json -> 'phase','branch',v_window_json -> 'branch',
                    'seed_source',v_window_json -> 'seed_source',
                    'seed_digest',v_window_json -> 'seed_digest',
                    'completed',v_checkpoint_view IS NOT NULL,
                    'checkpoint',v_checkpoint_view,'attempt',v_attempt_view
                )
            );
            v_span_digest := v_window_json #>> '{context_span,span_digest}';
            IF NOT (v_seen_spans ? v_span_digest) THEN
                v_seen_spans := v_seen_spans || pg_catalog.jsonb_build_object(v_span_digest,true);
                v_span_mode := v_window_json #>> '{context_span,mode}';
                IF v_span_mode = 'none' THEN
                    v_span_closed := v_checkpoint_view IS NOT NULL;
                ELSE
                    SELECT EXISTS (
                        SELECT 1 FROM aap_jobs_v1.video_execution_span_closures AS closure
                        WHERE closure.job_id=v_job_id AND closure.run_id=v_run_id
                          AND closure.plan_digest=v_journal.plan_digest
                          AND closure.context_span_digest=v_span_digest
                    ) INTO v_span_closed;
                END IF;
                v_closed_spans := v_closed_spans || pg_catalog.jsonb_build_array(
                    pg_catalog.jsonb_build_object(
                        'context_span_digest',v_span_digest,'closed',v_span_closed
                    )
                );
            END IF;
            v_last := v_window_ordinal;
        END LOOP;
        IF v_last IS NOT NULL THEN
            SELECT EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(
                    v_journal.plan_document -> 'windows'
                ) AS item(value)
                WHERE (item.value ->> 'ordinal')::integer > v_last
            ) INTO v_has_more;
        END IF;
        RETURN aap_jobs_v1._video_checkpoint_reply(pg_catalog.jsonb_build_object(
            'windows',v_read_resume_windows,'closed_spans',v_closed_spans,
            'next_ordinal',CASE WHEN v_has_more THEN to_jsonb(v_last) ELSE NULL END
        ));
    END IF;

    -- Remaining actions below share the locked proof and private plan row.
    IF p_action IN ('begin_window','capture_window','put_part','seal_window',
                    'read_part','verify_replay','close_span') THEN
        IF NOT v_has_journal THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_not_frozen');
        END IF;
        v_plan := v_journal.plan_document;
        IF v_plan IS NULL THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_not_frozen');
        END IF;
    END IF;
    IF p_action = 'begin_window' THEN
        IF v_job_missing OR v_job.status IS DISTINCT FROM 'running'
           OR v_job.cancel_requested_at IS NOT NULL THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'job_not_active');
        END IF;
        IF v_journal.writer_terminal OR v_journal.execution_retired
           OR v_execution.state <> 'ready' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'recovery_required');
        END IF;
        IF v_journal.plan_digest IS DISTINCT FROM v_plan_digest THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_mismatch');
        END IF;
        v_window_json := v_plan -> 'windows' -> v_window_ordinal;
        IF v_window_json IS NULL
           OR v_window_json ->> 'ordinal' IS DISTINCT FROM v_window_ordinal::text
           OR v_window_json ->> 'window_digest' IS DISTINCT FROM v_args ->> 'window_digest' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_mismatch');
        END IF;
        IF NOT aap_jobs_v1._video_checkpoint_digest_valid(v_args ->> 'window_digest') THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_plan -> 'windows') AS item(value)
            WHERE (item.value ->> 'ordinal')::integer < v_window_ordinal
              AND NOT EXISTS (
                  SELECT 1 FROM aap_jobs_v1.video_execution_windows AS result
                  WHERE result.job_id=v_job_id AND result.run_id=v_run_id
                    AND result.window_ordinal=(item.value ->> 'ordinal')::integer
              )
        ) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_out_of_order');
        END IF;
        v_expected_seed := NULL;
        v_expected_predecessor := NULL;
        v_previous_manifest := NULL;
        v_previous_ordinal := NULL;
        CASE v_window_json ->> 'seed_source'
            WHEN 'prompt' THEN
                v_expected_seed := v_window_json ->> 'seed_digest';
            WHEN 'source_geometry' THEN
                v_expected_seed := v_window_json ->> 'seed_digest';
            WHEN 'correction' THEN
                v_expected_seed := v_window_json ->> 'seed_digest';
            WHEN 'discovery' THEN
                SELECT (item.value ->> 'ordinal')::integer INTO v_previous_ordinal
                FROM pg_catalog.jsonb_array_elements(v_plan -> 'windows') AS item(value)
                WHERE item.value ->> 'phase'='discovery'
                  AND (item.value ->> 'ordinal')::integer < v_window_ordinal
                ORDER BY (item.value ->> 'ordinal')::integer DESC LIMIT 1;
                IF v_previous_ordinal IS NULL THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_out_of_order');
                END IF;
                SELECT result.manifest INTO v_previous_manifest
                FROM aap_jobs_v1.video_execution_windows AS result
                WHERE result.job_id=v_job_id AND result.run_id=v_run_id
                  AND result.window_ordinal=v_previous_ordinal;
                IF v_previous_manifest IS NULL
                   OR v_previous_manifest ->> 'phase' IS DISTINCT FROM 'discovery'
                   OR NOT aap_jobs_v1._video_checkpoint_digest_valid(
                        v_previous_manifest ->> 'discovery_seeds_digest'
                   ) THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_out_of_order');
                END IF;
                v_expected_seed := v_previous_manifest ->> 'discovery_seeds_digest';
                v_expected_predecessor := v_previous_manifest ->> 'manifest_digest';
            WHEN 'continuation' THEN
                SELECT (item.value ->> 'ordinal')::integer INTO v_previous_ordinal
                FROM pg_catalog.jsonb_array_elements(v_plan -> 'windows') AS item(value)
                WHERE item.value ->> 'phase'='track'
                  AND item.value ->> 'branch'=v_window_json ->> 'branch'
                  AND (item.value ->> 'ordinal')::integer < v_window_ordinal
                ORDER BY (item.value ->> 'ordinal')::integer DESC LIMIT 1;
                IF v_previous_ordinal IS NULL THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_out_of_order');
                END IF;
                SELECT result.manifest INTO v_previous_manifest
                FROM aap_jobs_v1.video_execution_windows AS result
                WHERE result.job_id=v_job_id AND result.run_id=v_run_id
                  AND result.window_ordinal=v_previous_ordinal;
                IF v_previous_manifest IS NULL
                   OR NOT aap_jobs_v1._video_checkpoint_digest_valid(
                        v_previous_manifest ->> 'continuation_digest'
                   ) THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_out_of_order');
                END IF;
                v_expected_seed := v_previous_manifest ->> 'continuation_digest';
                v_expected_predecessor := v_previous_manifest ->> 'manifest_digest';
            WHEN 'session' THEN
                IF (v_window_json #>> '{context_span,start_ordinal}')::integer
                    = v_window_ordinal THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_mismatch');
                END IF;
                SELECT (item.value ->> 'ordinal')::integer INTO v_previous_ordinal
                FROM pg_catalog.jsonb_array_elements(v_plan -> 'windows') AS item(value)
                WHERE item.value ->> 'phase'='track'
                  AND item.value ->> 'branch'=v_window_json ->> 'branch'
                  AND item.value #>> '{context_span,span_digest}'
                        = v_window_json #>> '{context_span,span_digest}'
                  AND (item.value ->> 'ordinal')::integer < v_window_ordinal
                ORDER BY (item.value ->> 'ordinal')::integer DESC LIMIT 1;
                IF v_previous_ordinal IS NULL THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_out_of_order');
                END IF;
                SELECT result.manifest INTO v_previous_manifest
                FROM aap_jobs_v1.video_execution_windows AS result
                WHERE result.job_id=v_job_id AND result.run_id=v_run_id
                  AND result.window_ordinal=v_previous_ordinal;
                IF v_previous_manifest IS NULL THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_out_of_order');
                END IF;
                v_expected_predecessor := v_previous_manifest ->> 'manifest_digest';
            WHEN 'none' THEN
                IF v_window_json ->> 'phase'='track' THEN
                    SELECT (item.value ->> 'ordinal')::integer INTO v_previous_ordinal
                    FROM pg_catalog.jsonb_array_elements(v_plan -> 'windows') AS item(value)
                    WHERE item.value ->> 'phase'='track'
                      AND item.value ->> 'branch'=v_window_json ->> 'branch'
                      AND (item.value ->> 'ordinal')::integer < v_window_ordinal
                    ORDER BY (item.value ->> 'ordinal')::integer DESC LIMIT 1;
                    IF v_previous_ordinal IS NOT NULL THEN
                        SELECT result.manifest INTO v_previous_manifest
                        FROM aap_jobs_v1.video_execution_windows AS result
                        WHERE result.job_id=v_job_id AND result.run_id=v_run_id
                          AND result.window_ordinal=v_previous_ordinal;
                        IF v_previous_manifest IS NULL THEN
                            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_out_of_order');
                        END IF;
                        v_expected_predecessor := v_previous_manifest ->> 'manifest_digest';
                    END IF;
                END IF;
            ELSE
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_mismatch');
        END CASE;
        IF (v_args -> 'seed_input_digest' = 'null'::jsonb) IS DISTINCT FROM
                (v_expected_seed IS NULL)
           OR (v_expected_seed IS NOT NULL AND
                v_args ->> 'seed_input_digest' IS DISTINCT FROM v_expected_seed)
           OR (v_args -> 'predecessor_checkpoint_digest' = 'null'::jsonb) IS DISTINCT FROM
                (v_expected_predecessor IS NULL)
           OR (v_expected_predecessor IS NOT NULL AND
                v_args ->> 'predecessor_checkpoint_digest' IS DISTINCT FROM v_expected_predecessor) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_mismatch');
        END IF;
        IF (v_window_json #>> '{context_span,mode}')='session'
           AND NOT EXISTS (
                SELECT 1 FROM aap_jobs_v1.video_execution_span_closures AS closure
                WHERE closure.job_id=v_job_id AND closure.run_id=v_run_id
                  AND closure.plan_digest=v_plan_digest
                  AND closure.context_span_digest=
                        v_window_json #>> '{context_span,span_digest}'
           ) AND EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(v_plan -> 'windows') AS item(value)
                WHERE item.value #>> '{context_span,span_digest}'=
                        v_window_json #>> '{context_span,span_digest}'
                  AND (item.value ->> 'ordinal')::integer < v_window_ordinal
                  AND NOT EXISTS (
                      SELECT 1 FROM aap_jobs_v1.video_execution_window_attempts AS attempt
                      WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
                        AND attempt.generation=v_generation
                        AND attempt.window_ordinal=(item.value ->> 'ordinal')::integer
                        AND attempt.state IN ('captured','replay_verified')
                  )
           ) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_out_of_order');
        END IF;
        v_checkpoint_view := NULL;
        SELECT result.* INTO v_window
        FROM aap_jobs_v1.video_execution_windows AS result
        WHERE result.job_id=v_job_id AND result.run_id=v_run_id
          AND result.window_ordinal=v_window_ordinal;
        v_found := FOUND;
        IF v_found THEN v_checkpoint_view := v_window.manifest; END IF;
        SELECT attempt.* INTO v_attempt
        FROM aap_jobs_v1.video_execution_window_attempts AS attempt
        WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
          AND attempt.generation=v_generation AND attempt.window_ordinal=v_window_ordinal
        FOR UPDATE NOWAIT;
        v_found := FOUND;
        IF v_found THEN
            IF v_attempt.window_digest IS DISTINCT FROM v_args ->> 'window_digest'
               OR v_attempt.seed_input_digest IS DISTINCT FROM v_expected_seed
               OR v_attempt.predecessor_checkpoint_digest IS DISTINCT FROM v_expected_predecessor
               OR (v_checkpoint_view IS NOT NULL AND v_attempt.mode='execute'
                   AND v_attempt.state<>'captured')
               OR (v_checkpoint_view IS NULL AND v_attempt.mode='replay')
               OR v_attempt.state='abandoned' THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'window_mismatch');
            END IF;
        ELSE
            IF v_checkpoint_view IS NOT NULL THEN
                IF NOT aap_jobs_v1._video_checkpoint_plan_identities_complete(v_plan) THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(
                        NULL,'checkpoint_identity_incomplete'
                    );
                END IF;
                v_reason := 'replay';
            ELSE
                v_reason := 'execute';
            END IF;
            INSERT INTO aap_jobs_v1.video_execution_window_attempts(
                job_id,run_id,generation,window_ordinal,window_digest,mode,state,
                capture_id,seed_input_digest,predecessor_checkpoint_digest
            ) VALUES (
                v_job_id,v_run_id,v_generation,v_window_ordinal,
                v_args ->> 'window_digest',v_reason,'started',public.gen_random_uuid(),
                v_expected_seed,v_expected_predecessor
            ) RETURNING * INTO v_attempt;
        END IF;
        RETURN aap_jobs_v1._video_checkpoint_reply(pg_catalog.jsonb_build_object(
            'attempt',pg_catalog.jsonb_build_object(
                'job_id',v_job_id,'run_id',v_run_id,'generation',v_generation,
                'window_ordinal',v_window_ordinal,'window_digest',v_attempt.window_digest,
                'mode',v_attempt.mode,'capture_id',v_attempt.capture_id,
                'seed_input_digest',v_attempt.seed_input_digest,
                'predecessor_checkpoint_digest',v_attempt.predecessor_checkpoint_digest,
                'state',v_attempt.state,'checkpoint',v_checkpoint_view
            )
        ));
    END IF;
    IF p_action = 'capture_window' THEN
        BEGIN
            v_capture.capture_id := aap_jobs_v1._video_execution_uuid(
                v_args -> 'capture_id'
            );
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END;
        v_manifest := v_args -> 'manifest';
        IF NOT aap_jobs_v1._video_checkpoint_manifest_valid(v_manifest) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'manifest_invalid');
        END IF;
        v_window_json := v_plan -> 'windows' -> v_window_ordinal;
        IF v_args ->> 'plan_digest' IS DISTINCT FROM v_journal.plan_digest
           OR v_manifest ->> 'request_id' IS DISTINCT FROM v_job_id::text
           OR v_manifest ->> 'input_id' IS DISTINCT FROM v_journal.input_id::text
           OR v_manifest ->> 'plan_digest' IS DISTINCT FROM v_journal.plan_digest
           OR (v_manifest ->> 'window_ordinal')::integer IS DISTINCT FROM v_window_ordinal
           OR v_manifest ->> 'window_digest' IS DISTINCT FROM v_window_json ->> 'window_digest'
           OR v_manifest ->> 'context_span_digest' IS DISTINCT FROM
                v_window_json #>> '{context_span,span_digest}'
           OR v_manifest ->> 'phase' IS DISTINCT FROM v_window_json ->> 'phase' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'manifest_mismatch');
        END IF;
        SELECT attempt.* INTO v_attempt
        FROM aap_jobs_v1.video_execution_window_attempts AS attempt
        WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
          AND attempt.generation=v_generation
          AND attempt.window_ordinal=v_window_ordinal
          AND attempt.capture_id=v_capture.capture_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_attempt.mode <> 'execute'
           OR v_attempt.window_digest IS DISTINCT FROM v_manifest ->> 'window_digest'
           OR v_attempt.seed_input_digest IS DISTINCT FROM
                (CASE WHEN v_manifest -> 'seed_input_digest'='null'::jsonb
                     THEN NULL ELSE v_manifest ->> 'seed_input_digest' END)
           OR v_attempt.predecessor_checkpoint_digest IS DISTINCT FROM
                (CASE WHEN v_manifest -> 'predecessor_checkpoint_digest'='null'::jsonb
                     THEN NULL ELSE v_manifest ->> 'predecessor_checkpoint_digest' END) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'capture_mismatch');
        END IF;
        IF v_attempt.state = 'captured' THEN
            SELECT result.* INTO v_window
            FROM aap_jobs_v1.video_execution_windows AS result
            WHERE result.job_id=v_job_id AND result.run_id=v_run_id
              AND result.window_ordinal=v_window_ordinal;
            IF NOT FOUND OR v_window.capture_id IS DISTINCT FROM v_capture.capture_id
               OR v_window.manifest IS DISTINCT FROM v_manifest THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'capture_conflict');
            END IF;
            RETURN aap_jobs_v1._video_checkpoint_reply(pg_catalog.jsonb_build_object(
                'capture',pg_catalog.jsonb_build_object(
                    'capture_id',v_capture.capture_id,'window_ordinal',v_window_ordinal,
                    'state','sealed','manifest',v_window.manifest
                )
            ));
        ELSIF v_attempt.state NOT IN ('started','capturing') THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'capture_conflict');
        END IF;
        IF v_execution.state <> 'ready' OR v_journal.writer_terminal
           OR v_journal.execution_retired
           OR v_journal.plan_digest IS DISTINCT FROM v_plan_digest THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'recovery_required');
        END IF;
        v_guard := aap_jobs_v1.video_execution_step(
            'guard',(v_proof || pg_catalog.jsonb_build_object('purpose','progress'))::text
        );
        IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-video-execution-data/v1'
           OR v_guard ->> 'status' IS DISTINCT FROM 'ok' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(
                NULL,COALESCE(v_guard ->> 'reason','execution_busy')
            );
        END IF;
        SELECT result.* INTO v_window
        FROM aap_jobs_v1.video_execution_windows AS result
        WHERE result.job_id=v_job_id AND result.run_id=v_run_id
          AND result.window_ordinal=v_window_ordinal;
        IF FOUND THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_conflict');
        END IF;
        SELECT capture.* INTO v_capture
        FROM aap_jobs_v1.video_execution_captures AS capture
        WHERE capture.capture_id=aap_jobs_v1._video_execution_uuid(
            v_args -> 'capture_id'
        )
        FOR UPDATE NOWAIT;
        IF FOUND THEN
            IF v_capture.job_id IS DISTINCT FROM v_job_id
               OR v_capture.run_id IS DISTINCT FROM v_run_id
               OR v_capture.generation IS DISTINCT FROM v_generation
               OR v_capture.window_ordinal IS DISTINCT FROM v_window_ordinal
               OR v_capture.manifest_digest IS DISTINCT FROM v_manifest ->> 'manifest_digest'
               OR v_capture.manifest IS DISTINCT FROM v_manifest
               OR v_capture.state <> 'receiving' THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'capture_conflict');
            END IF;
        ELSE
            INSERT INTO aap_jobs_v1.video_execution_captures(
                capture_id,job_id,run_id,generation,window_ordinal,
                manifest_digest,manifest,state
            ) VALUES (
                v_attempt.capture_id,v_job_id,v_run_id,v_generation,v_window_ordinal,
                v_manifest ->> 'manifest_digest',v_manifest,'receiving'
            );
            UPDATE aap_jobs_v1.video_execution_window_attempts AS attempt
            SET state='capturing',updated_at=pg_catalog.clock_timestamp()
            WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
              AND attempt.generation=v_generation
              AND attempt.window_ordinal=v_window_ordinal;
        END IF;
        RETURN aap_jobs_v1._video_checkpoint_reply(pg_catalog.jsonb_build_object(
            'capture',pg_catalog.jsonb_build_object(
                'capture_id',v_attempt.capture_id,'window_ordinal',v_window_ordinal,
                'state','receiving','manifest',v_manifest
            )
        ));
    END IF;
    IF p_action = 'put_part' THEN
        IF v_execution.state <> 'ready' OR v_journal.writer_terminal
           OR v_journal.execution_retired THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'recovery_required');
        END IF;
        BEGIN
            v_capture.capture_id := aap_jobs_v1._video_execution_uuid(
                v_args -> 'capture_id'
            );
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END;
        v_index := aap_jobs_v1._video_checkpoint_integer(
            v_args -> 'index',0,127
        );
        v_base64 := v_args ->> 'data_base64';
        IF v_index IS NULL
           OR pg_catalog.jsonb_typeof(v_args -> 'sha256') IS DISTINCT FROM 'string'
           OR NOT aap_jobs_v1._video_checkpoint_digest_valid(v_args ->> 'sha256')
           OR pg_catalog.jsonb_typeof(v_args -> 'data_base64') IS DISTINCT FROM 'string'
           OR v_base64 IS NULL OR pg_catalog.char_length(v_base64) NOT BETWEEN 4 AND 699052
           OR v_base64 !~ '^([A-Za-z0-9+/]{4})*([A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'part_invalid');
        END IF;
        BEGIN
            v_part := pg_catalog.decode(v_base64,'base64');
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'part_invalid');
        END;
        IF pg_catalog.translate(pg_catalog.encode(v_part,'base64'),E'\n\r','')
                IS DISTINCT FROM v_base64
           OR pg_catalog.octet_length(v_part) NOT BETWEEN 1 AND 524288
           OR pg_catalog.encode(public.digest(v_part,'sha256'),'hex')
                IS DISTINCT FROM v_args ->> 'sha256' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'part_invalid');
        END IF;
        SELECT capture.* INTO v_capture
        FROM aap_jobs_v1.video_execution_captures AS capture
        WHERE capture.capture_id=v_capture.capture_id
          AND capture.job_id=v_job_id AND capture.run_id=v_run_id
          AND capture.generation=v_generation
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'capture_mismatch');
        END IF;
        IF v_capture.state='sealed' THEN
            SELECT result.* INTO v_window
            FROM aap_jobs_v1.video_execution_windows AS result
            WHERE result.job_id=v_job_id AND result.run_id=v_run_id
              AND result.window_ordinal=v_capture.window_ordinal
              AND result.capture_id=v_capture.capture_id;
            SELECT part.part_digest,part.part_bytes
            INTO v_part_digest,v_bytes
            FROM aap_jobs_v1.video_execution_window_parts AS part
            WHERE part.job_id=v_job_id AND part.run_id=v_run_id
              AND part.window_ordinal=v_capture.window_ordinal
              AND part.part_index=v_index;
            IF NOT FOUND OR v_part_digest IS DISTINCT FROM v_args ->> 'sha256'
               OR v_bytes IS DISTINCT FROM v_part THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'part_conflict');
            END IF;
            v_manifest := v_window.manifest;
            RETURN aap_jobs_v1._video_checkpoint_reply(pg_catalog.jsonb_build_object(
                'part',pg_catalog.jsonb_build_object(
                    'capture_id',v_capture.capture_id,
                    'checkpoint_id',v_manifest -> 'checkpoint_id',
                    'manifest_digest',v_manifest -> 'manifest_digest',
                    'part_index',v_index,
                    'part_count',v_manifest -> 'payload_part_count',
                    'payload_digest',v_manifest -> 'payload_digest',
                    'payload_byte_size',v_manifest -> 'payload_byte_size',
                    'part_digest',v_part_digest
                )
            ));
        ELSIF v_capture.state <> 'receiving' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'capture_closed');
        END IF;
        SELECT attempt.* INTO v_attempt
        FROM aap_jobs_v1.video_execution_window_attempts AS attempt
        WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
          AND attempt.generation=v_generation
          AND attempt.window_ordinal=v_capture.window_ordinal
          AND attempt.capture_id=v_capture.capture_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_attempt.state NOT IN ('capturing') THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'capture_mismatch');
        END IF;
        v_manifest := v_capture.manifest;
        v_payload_byte_size := (v_manifest ->> 'payload_byte_size')::bigint;
        v_payload_part_count := (v_manifest ->> 'payload_part_count')::bigint;
        IF v_index >= v_payload_part_count THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'part_invalid');
        END IF;
        v_expected_bytes := LEAST(524288,v_payload_byte_size-v_index::bigint*524288);
        IF pg_catalog.octet_length(v_part) <> v_expected_bytes THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'part_invalid');
        END IF;
        SELECT part.part_digest,part.part_bytes INTO v_reason,v_bytes
        FROM aap_jobs_v1.video_execution_capture_parts AS part
        WHERE part.capture_id=v_capture.capture_id AND part.part_index=v_index
        FOR UPDATE NOWAIT;
        IF FOUND THEN
            IF v_reason IS DISTINCT FROM v_args ->> 'sha256'
               OR v_bytes IS DISTINCT FROM v_part THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'part_conflict');
            END IF;
        ELSE
            INSERT INTO aap_jobs_v1.video_execution_capture_parts(
                capture_id,part_index,part_digest,part_bytes
            ) VALUES(v_capture.capture_id,v_index,v_args ->> 'sha256',v_part);
        END IF;
        RETURN aap_jobs_v1._video_checkpoint_reply(pg_catalog.jsonb_build_object(
            'part',pg_catalog.jsonb_build_object(
                'capture_id',v_capture.capture_id,
                'checkpoint_id',v_manifest -> 'checkpoint_id',
                'manifest_digest',v_manifest -> 'manifest_digest',
                'part_index',v_index,
                'part_count',v_manifest -> 'payload_part_count',
                'payload_digest',v_manifest -> 'payload_digest',
                'payload_byte_size',v_manifest -> 'payload_byte_size',
                'part_digest',v_args -> 'sha256'
            )
        ));
    END IF;
    IF p_action = 'seal_window' THEN
        BEGIN
            v_capture.capture_id := aap_jobs_v1._video_execution_uuid(
                v_args -> 'capture_id'
            );
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END;
        SELECT capture.* INTO v_capture
        FROM aap_jobs_v1.video_execution_captures AS capture
        WHERE capture.capture_id=v_capture.capture_id
          AND capture.job_id=v_job_id AND capture.run_id=v_run_id
          AND capture.generation=v_generation
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'capture_mismatch');
        END IF;
        SELECT attempt.* INTO v_attempt
        FROM aap_jobs_v1.video_execution_window_attempts AS attempt
        WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
          AND attempt.generation=v_generation
          AND attempt.window_ordinal=v_capture.window_ordinal
          AND attempt.capture_id=v_capture.capture_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'capture_mismatch');
        END IF;
        IF v_capture.state='sealed' THEN
            SELECT result.* INTO v_window
            FROM aap_jobs_v1.video_execution_windows AS result
            WHERE result.job_id=v_job_id AND result.run_id=v_run_id
              AND result.window_ordinal=v_capture.window_ordinal;
            IF NOT FOUND OR v_window.capture_id IS DISTINCT FROM v_capture.capture_id
               OR v_window.manifest IS DISTINCT FROM v_capture.manifest THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_conflict');
            END IF;
            RETURN aap_jobs_v1._video_checkpoint_reply(pg_catalog.jsonb_build_object(
                'window',pg_catalog.jsonb_build_object(
                    'capture_id',v_capture.capture_id,
                    'ordinal',v_capture.window_ordinal,
                    'checkpoint_id',v_window.checkpoint_id,
                    'manifest',v_window.manifest
                )
            ));
        END IF;
        IF v_execution.state <> 'ready' OR v_journal.writer_terminal
           OR v_journal.execution_retired THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'recovery_required');
        END IF;
        IF v_capture.state <> 'receiving' OR v_attempt.state <> 'capturing' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'capture_closed');
        END IF;
        v_guard := aap_jobs_v1.video_execution_step(
            'guard',(v_proof || pg_catalog.jsonb_build_object('purpose','progress'))::text
        );
        IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-video-execution-data/v1'
           OR v_guard ->> 'status' IS DISTINCT FROM 'ok' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(
                NULL,COALESCE(v_guard ->> 'reason','execution_busy')
            );
        END IF;
        v_manifest := v_capture.manifest;
        SELECT result.* INTO v_window
        FROM aap_jobs_v1.video_execution_windows AS result
        WHERE result.job_id=v_job_id AND result.run_id=v_run_id
          AND result.window_ordinal=v_capture.window_ordinal;
        IF FOUND THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_conflict');
        END IF;
        v_payload_byte_size := (v_manifest ->> 'payload_byte_size')::bigint;
        v_payload_part_count := (v_manifest ->> 'payload_part_count')::bigint;
        SELECT pg_catalog.count(*),COALESCE(pg_catalog.sum(
                   pg_catalog.octet_length(part.part_bytes)
               ),0)
        INTO v_part_count,v_total_bytes
        FROM aap_jobs_v1.video_execution_capture_parts AS part
        WHERE part.capture_id=v_capture.capture_id;
        IF v_part_count IS DISTINCT FROM v_payload_part_count
           OR v_total_bytes IS DISTINCT FROM v_payload_byte_size THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
        END IF;
        SELECT pg_catalog.string_agg(part.part_bytes,pg_catalog.decode('','hex')
                   ORDER BY part.part_index)
        INTO v_bytes
        FROM aap_jobs_v1.video_execution_capture_parts AS part
        WHERE part.capture_id=v_capture.capture_id;
        IF v_bytes IS NULL OR pg_catalog.octet_length(v_bytes) IS DISTINCT FROM v_payload_byte_size
           OR pg_catalog.encode(public.digest(v_bytes,'sha256'),'hex')
                IS DISTINCT FROM v_manifest ->> 'payload_digest' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
        END IF;
        BEGIN
            v_payload_text := pg_catalog.convert_from(v_bytes,'UTF8');
            v_payload_json := v_payload_text::json;
            v_payload := v_payload_json::jsonb;
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_invalid');
        END;
        SELECT pg_catalog.count(*),pg_catalog.count(DISTINCT field.key)
        INTO v_payload_key_count,v_payload_unique_keys
        FROM pg_catalog.json_each(v_payload_json) AS field;
        IF v_payload_key_count <> 19 OR v_payload_unique_keys <> 19
           OR NOT aap_jobs_v1._video_execution_exact_keys(v_payload,ARRAY[
                'schema','checkpoint_id','request_id','input_id','plan_digest',
                'window_ordinal','window_digest','context_span_digest','phase','branch',
                'seed_input_digest','predecessor_checkpoint_digest','raw_response_digest',
                'frame_results','continuation','discovery_output_digest',
                'discovery_seeds_digest','discovery_seeds','result_digest'
           ]) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_invalid');
        END IF;
        v_window_json := v_plan -> 'windows' -> v_capture.window_ordinal;
        IF v_payload ->> 'schema' IS DISTINCT FROM 'aap-video-window-result-content/v1'
           OR v_payload ->> 'checkpoint_id' IS DISTINCT FROM v_manifest ->> 'checkpoint_id'
           OR v_payload ->> 'request_id' IS DISTINCT FROM v_job_id::text
           OR v_payload ->> 'input_id' IS DISTINCT FROM v_journal.input_id::text
           OR v_payload ->> 'plan_digest' IS DISTINCT FROM v_journal.plan_digest
           OR (v_payload ->> 'window_ordinal')::integer IS DISTINCT FROM v_capture.window_ordinal
           OR v_payload ->> 'window_digest' IS DISTINCT FROM v_window_json ->> 'window_digest'
           OR v_payload ->> 'context_span_digest' IS DISTINCT FROM
                v_window_json #>> '{context_span,span_digest}'
           OR v_payload ->> 'phase' IS DISTINCT FROM v_window_json ->> 'phase'
           OR v_payload ->> 'branch' IS DISTINCT FROM v_window_json ->> 'branch'
           OR v_payload -> 'seed_input_digest' IS DISTINCT FROM COALESCE(to_jsonb(v_attempt.seed_input_digest),'null'::jsonb)
           OR v_payload -> 'predecessor_checkpoint_digest' IS DISTINCT FROM
                COALESCE(to_jsonb(v_attempt.predecessor_checkpoint_digest),'null'::jsonb)
           OR v_payload ->> 'raw_response_digest' IS DISTINCT FROM v_manifest ->> 'raw_response_digest'
           OR v_payload -> 'result_digest' IS DISTINCT FROM v_manifest -> 'result_digest'
           OR v_payload -> 'discovery_output_digest' IS DISTINCT FROM v_manifest -> 'discovery_output_digest'
           OR v_payload -> 'discovery_seeds_digest' IS DISTINCT FROM v_manifest -> 'discovery_seeds_digest'
           OR pg_catalog.jsonb_typeof(v_payload -> 'frame_results') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(v_payload -> 'discovery_seeds') IS DISTINCT FROM 'array'
           OR (v_window_json ->> 'phase'='track'
               AND pg_catalog.jsonb_typeof(v_payload -> 'continuation') IS DISTINCT FROM 'object')
           OR (v_window_json ->> 'phase'='discovery'
               AND v_payload -> 'continuation' IS DISTINCT FROM 'null'::jsonb) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_invalid');
        END IF;
        v_reply := aap_labeling_v1.retain_video_execution_checkpoint_masks(
            v_job_id,v_manifest ->> 'checkpoint_id',
            pg_catalog.jsonb_build_object('frame_results',v_payload -> 'frame_results')
        );
        IF v_reply IS NULL OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-mask-gc/v1'
           OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_reply #>> '{data,job_id}' IS DISTINCT FROM v_job_id::text
           OR v_reply #>> '{data,checkpoint_id}' IS DISTINCT FROM v_manifest ->> 'checkpoint_id' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'mask_retention_unavailable');
        END IF;
        INSERT INTO aap_jobs_v1.video_execution_windows(
            job_id,run_id,window_ordinal,checkpoint_id,manifest_digest,manifest,capture_id
        ) VALUES (
            v_job_id,v_run_id,v_capture.window_ordinal,
            v_manifest ->> 'checkpoint_id',v_manifest ->> 'manifest_digest',
            v_manifest,v_capture.capture_id
        );
        INSERT INTO aap_jobs_v1.video_execution_window_parts(
            job_id,run_id,window_ordinal,checkpoint_id,part_index,part_digest,part_bytes
        )
        SELECT v_job_id,v_run_id,v_capture.window_ordinal,v_manifest ->> 'checkpoint_id',
               part.part_index,part.part_digest,part.part_bytes
        FROM aap_jobs_v1.video_execution_capture_parts AS part
        WHERE part.capture_id=v_capture.capture_id
        ORDER BY part.part_index;
        DELETE FROM aap_jobs_v1.video_execution_capture_parts AS part
        WHERE part.capture_id=v_capture.capture_id;
        UPDATE aap_jobs_v1.video_execution_captures AS capture
        SET state='sealed',updated_at=pg_catalog.clock_timestamp()
        WHERE capture.capture_id=v_capture.capture_id;
        UPDATE aap_jobs_v1.video_execution_window_attempts AS attempt
        SET state='captured',updated_at=pg_catalog.clock_timestamp()
        WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
          AND attempt.generation=v_generation
          AND attempt.window_ordinal=v_capture.window_ordinal;
        RETURN aap_jobs_v1._video_checkpoint_reply(pg_catalog.jsonb_build_object(
            'window',pg_catalog.jsonb_build_object(
                'capture_id',v_capture.capture_id,
                'ordinal',v_capture.window_ordinal,
                'checkpoint_id',v_manifest -> 'checkpoint_id',
                'manifest',v_manifest
            )
        ));
    END IF;
    IF p_action = 'read_part' THEN
        BEGIN
            v_checkpoint_id := v_args ->> 'checkpoint_id';
            v_index := aap_jobs_v1._video_checkpoint_integer(v_args -> 'index',0,127);
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END;
        IF NOT aap_jobs_v1._video_checkpoint_digest_valid(v_checkpoint_id)
           OR v_index IS NULL THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END IF;
        SELECT result.* INTO v_window
        FROM aap_jobs_v1.video_execution_windows AS result
        WHERE result.job_id=v_job_id AND result.run_id=v_run_id
          AND result.checkpoint_id=v_checkpoint_id;
        IF NOT FOUND THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_missing');
        END IF;
        v_manifest := v_window.manifest;
        IF v_index >= (v_manifest ->> 'payload_part_count')::integer THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'part_missing');
        END IF;
        SELECT part.part_digest,part.part_bytes INTO v_part_digest,v_part
        FROM aap_jobs_v1.video_execution_window_parts AS part
        WHERE part.job_id=v_job_id AND part.run_id=v_run_id
          AND part.window_ordinal=v_window.window_ordinal
          AND part.part_index=v_index;
        IF NOT FOUND THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
        END IF;
        v_base64 := pg_catalog.translate(
            pg_catalog.encode(v_part,'base64'),E'\n\r',''
        );
        RETURN aap_jobs_v1._video_checkpoint_reply(pg_catalog.jsonb_build_object(
            'part',pg_catalog.jsonb_build_object(
                'schema','aap-video-window-result-part/v1',
                'checkpoint_id',v_checkpoint_id,
                'manifest_digest',v_manifest -> 'manifest_digest',
                'part_index',v_index,
                'part_count',v_manifest -> 'payload_part_count',
                'payload_digest',v_manifest -> 'payload_digest',
                'payload_byte_size',v_manifest -> 'payload_byte_size',
                'part_digest',v_part_digest,
                'data_base64',v_base64
            )
        ));
    END IF;
    IF p_action = 'verify_replay' THEN
        IF v_journal.plan_digest IS DISTINCT FROM v_plan_digest THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_mismatch');
        END IF;
        BEGIN
            v_capture.capture_id := aap_jobs_v1._video_execution_uuid(
                v_args -> 'capture_id'
            );
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END;
        v_manifest := NULL;
        SELECT result.manifest INTO v_manifest
        FROM aap_jobs_v1.video_execution_windows AS result
        WHERE result.job_id=v_job_id AND result.run_id=v_run_id
          AND result.window_ordinal=v_window_ordinal
          AND result.checkpoint_id=v_args ->> 'checkpoint_id';
        IF NOT FOUND THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_missing');
        END IF;
        IF v_args ->> 'checkpoint_id' IS DISTINCT FROM v_manifest ->> 'checkpoint_id'
           OR v_args ->> 'plan_digest' IS DISTINCT FROM v_manifest ->> 'plan_digest'
           OR pg_catalog.jsonb_typeof(v_args -> 'result_digest') IS DISTINCT FROM 'string'
           OR NOT aap_jobs_v1._video_checkpoint_digest_valid(v_args ->> 'result_digest')
           OR pg_catalog.jsonb_typeof(v_args -> 'raw_response_digest') IS DISTINCT FROM 'string'
           OR NOT aap_jobs_v1._video_checkpoint_digest_valid(v_args ->> 'raw_response_digest') THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END IF;
        FOREACH v_reason IN ARRAY ARRAY[
            'continuation_digest','discovery_seeds_digest','seed_input_digest',
            'predecessor_checkpoint_digest'
        ] LOOP
            IF v_args -> v_reason <> 'null'::jsonb
               AND (pg_catalog.jsonb_typeof(v_args -> v_reason) IS DISTINCT FROM 'string'
                    OR NOT aap_jobs_v1._video_checkpoint_digest_valid(v_args ->> v_reason)) THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
            END IF;
        END LOOP;
        SELECT attempt.* INTO v_attempt
        FROM aap_jobs_v1.video_execution_window_attempts AS attempt
        WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
          AND attempt.generation=v_generation AND attempt.window_ordinal=v_window_ordinal
          AND attempt.capture_id=v_capture.capture_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND OR v_attempt.mode <> 'replay'
           OR v_attempt.window_digest IS DISTINCT FROM v_manifest ->> 'window_digest'
           OR v_attempt.seed_input_digest IS DISTINCT FROM
                (CASE WHEN v_args -> 'seed_input_digest'='null'::jsonb
                     THEN NULL ELSE v_args ->> 'seed_input_digest' END)
           OR v_attempt.predecessor_checkpoint_digest IS DISTINCT FROM
                (CASE WHEN v_args -> 'predecessor_checkpoint_digest'='null'::jsonb
                     THEN NULL ELSE v_args ->> 'predecessor_checkpoint_digest' END)
           OR v_args ->> 'result_digest' IS DISTINCT FROM v_manifest ->> 'result_digest'
           OR v_args -> 'continuation_digest' IS DISTINCT FROM v_manifest -> 'continuation_digest'
           OR v_args -> 'discovery_seeds_digest' IS DISTINCT FROM v_manifest -> 'discovery_seeds_digest' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'replay_mismatch');
        END IF;
        IF v_attempt.state='replay_verified' THEN
            IF v_attempt.raw_response_digest IS DISTINCT FROM v_args ->> 'raw_response_digest' THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'replay_conflict');
            END IF;
        ELSIF v_attempt.state='started' THEN
            IF v_execution.state <> 'ready' OR v_journal.writer_terminal
               OR v_journal.execution_retired THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'recovery_required');
            END IF;
            v_guard := aap_jobs_v1.video_execution_step(
                'guard',(v_proof || pg_catalog.jsonb_build_object('purpose','progress'))::text
            );
            IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-video-execution-data/v1'
               OR v_guard ->> 'status' IS DISTINCT FROM 'ok' THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(
                    NULL,COALESCE(v_guard ->> 'reason','execution_busy')
                );
            END IF;
            UPDATE aap_jobs_v1.video_execution_window_attempts AS attempt
            SET state='replay_verified',raw_response_digest=v_args ->> 'raw_response_digest',
                updated_at=pg_catalog.clock_timestamp()
            WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
              AND attempt.generation=v_generation
              AND attempt.window_ordinal=v_window_ordinal;
        ELSE
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'replay_conflict');
        END IF;
        RETURN aap_jobs_v1._video_checkpoint_reply(
            pg_catalog.jsonb_build_object('verified',true)
        );
    END IF;
    IF p_action = 'close_span' THEN
        IF v_journal.plan_digest IS DISTINCT FROM v_plan_digest
           OR NOT aap_jobs_v1._video_checkpoint_digest_valid(
                v_args ->> 'context_span_digest'
           ) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'plan_mismatch');
        END IF;
        SELECT element.value #>> '{context_span,mode}' INTO v_span_mode
        FROM pg_catalog.jsonb_array_elements(v_plan -> 'windows') AS element(value)
        WHERE element.value #>> '{context_span,span_digest}'
            = v_args ->> 'context_span_digest'
        LIMIT 1;
        IF NOT FOUND THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'span_mismatch');
        END IF;
        IF v_span_mode='session' AND EXISTS (
            SELECT 1 FROM aap_jobs_v1.video_execution_span_closures AS closure
            WHERE closure.job_id=v_job_id AND closure.run_id=v_run_id
              AND closure.plan_digest=v_plan_digest
              AND closure.context_span_digest=v_args ->> 'context_span_digest'
        ) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(
                pg_catalog.jsonb_build_object('closed',true)
            );
        END IF;
        SELECT pg_catalog.count(*),pg_catalog.count(result.window_ordinal),
               pg_catalog.count(*) FILTER (
                   WHERE attempt.state IN ('captured','replay_verified')
               )
        INTO v_part_count,v_total_bytes,v_payload_part_count
        FROM pg_catalog.jsonb_array_elements(v_plan -> 'windows') AS element(value)
        LEFT JOIN aap_jobs_v1.video_execution_windows AS result
          ON result.job_id=v_job_id AND result.run_id=v_run_id
         AND result.window_ordinal=(element.value ->> 'ordinal')::integer
        LEFT JOIN aap_jobs_v1.video_execution_window_attempts AS attempt
          ON attempt.job_id=v_job_id AND attempt.run_id=v_run_id
         AND attempt.generation=v_generation
         AND attempt.window_ordinal=(element.value ->> 'ordinal')::integer
        WHERE element.value #>> '{context_span,span_digest}'
            = v_args ->> 'context_span_digest';
        IF v_part_count IS NULL OR v_part_count=0
           OR v_total_bytes IS DISTINCT FROM v_part_count
           OR (v_span_mode='session' AND v_payload_part_count IS DISTINCT FROM v_part_count) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'span_incomplete');
        END IF;
        IF v_span_mode='session' THEN
            v_guard := aap_jobs_v1.video_execution_step(
                'guard',(v_proof || pg_catalog.jsonb_build_object('purpose','progress'))::text
            );
            IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-video-execution-data/v1'
               OR v_guard ->> 'status' IS DISTINCT FROM 'ok' THEN
                RETURN aap_jobs_v1._video_checkpoint_reply(
                    NULL,COALESCE(v_guard ->> 'reason','execution_busy')
                );
            END IF;
            INSERT INTO aap_jobs_v1.video_execution_span_closures(
                job_id,run_id,plan_digest,context_span_digest,closed_generation
            ) VALUES (
                v_job_id,v_run_id,v_plan_digest,v_args ->> 'context_span_digest',v_generation
            );
        END IF;
        RETURN aap_jobs_v1._video_checkpoint_reply(
            pg_catalog.jsonb_build_object('closed',true)
        );
    END IF;
    IF p_action = 'recovery_ready' THEN
        IF v_job_missing OR v_job.status NOT IN ('queued','running','cancelled') THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'job_not_active');
        END IF;
        IF NOT aap_jobs_v1._video_checkpoint_all_routes_retired(v_job_id,v_run_id) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'recovery_required');
        END IF;
        IF v_execution.state='ready' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(
                pg_catalog.jsonb_build_object('ready',true)
            );
        END IF;
        IF v_execution.state IS DISTINCT FROM 'recovery_required' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'recovery_required');
        END IF;
        DELETE FROM aap_jobs_v1.video_execution_capture_parts AS part
        USING aap_jobs_v1.video_execution_captures AS capture
        WHERE capture.capture_id=part.capture_id
          AND capture.job_id=v_job_id AND capture.run_id=v_run_id
          AND capture.generation<>v_generation AND capture.state='receiving';
        UPDATE aap_jobs_v1.video_execution_captures AS capture
        SET state='abandoned',updated_at=pg_catalog.clock_timestamp()
        WHERE capture.job_id=v_job_id AND capture.run_id=v_run_id
          AND capture.generation<>v_generation AND capture.state='receiving';
        UPDATE aap_jobs_v1.video_execution_window_attempts AS attempt
        SET state='abandoned',updated_at=pg_catalog.clock_timestamp()
        WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
          AND attempt.generation<>v_generation
          AND attempt.state IN ('started','capturing');
        UPDATE aap_jobs_v1.video_executions AS execution
        SET state='ready',route_request_id=NULL,updated_at=pg_catalog.clock_timestamp()
        WHERE execution.job_id=v_job_id AND execution.run_id=v_run_id
          AND execution.generation=v_generation;
        RETURN aap_jobs_v1._video_checkpoint_reply(
            pg_catalog.jsonb_build_object('ready',true)
        );
    END IF;
    IF p_action = 'finish' THEN
        v_outcome := v_args ->> 'outcome';
        IF v_outcome='completed' AND NOT aap_jobs_v1._video_checkpoint_results_complete(v_job_id,v_run_id) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'checkpoint_incomplete');
        END IF;
        IF pg_catalog.jsonb_typeof(v_args -> 'outcome') IS DISTINCT FROM 'string'
           OR v_outcome NOT IN ('completed','cancelled','failed') THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
        END IF;
        IF v_job_missing THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'job_missing');
        END IF;
        IF (v_outcome='cancelled' AND v_job.status IS DISTINCT FROM 'cancelled')
           OR (v_outcome='failed' AND v_job.status IS DISTINCT FROM 'failed')
           OR (v_outcome='completed' AND v_job.status NOT IN (
                'completed','pending_review','partially_reviewed','accepted','discarded'
           )) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'job_not_active');
        END IF;
        IF v_execution.state='recovery_required' THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'recovery_required');
        END IF;
        IF v_has_journal THEN
            IF v_journal.writer_terminal THEN
                IF v_journal.writer_terminal_outcome IS DISTINCT FROM v_outcome THEN
                    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'finish_conflict');
                END IF;
            ELSE
                UPDATE aap_jobs_v1.video_execution_journals AS journal
                SET writer_terminal=true,writer_terminal_outcome=v_outcome,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE journal.job_id=v_job_id AND journal.run_id=v_run_id;
            END IF;
        ELSE
            INSERT INTO aap_jobs_v1.video_execution_journals(
                job_id,run_id,writer_terminal,writer_terminal_outcome
            ) VALUES(v_job_id,v_run_id,true,v_outcome);
        END IF;
        DELETE FROM aap_jobs_v1.video_execution_capture_parts AS part
        USING aap_jobs_v1.video_execution_captures AS capture
        WHERE capture.capture_id=part.capture_id
          AND capture.job_id=v_job_id AND capture.run_id=v_run_id
          AND capture.state='receiving';
        UPDATE aap_jobs_v1.video_execution_captures AS capture
        SET state='abandoned',updated_at=pg_catalog.clock_timestamp()
        WHERE capture.job_id=v_job_id AND capture.run_id=v_run_id
          AND capture.state='receiving';
        UPDATE aap_jobs_v1.video_execution_window_attempts AS attempt
        SET state='abandoned',updated_at=pg_catalog.clock_timestamp()
        WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
          AND attempt.state IN ('started','capturing');
        RETURN aap_jobs_v1._video_checkpoint_reply(
            pg_catalog.jsonb_build_object('finished',true)
        );
    END IF;
    IF p_action = 'retire' THEN
        IF NOT v_job_missing AND v_job.status NOT IN (
            'completed','failed','cancelled','pending_review','partially_reviewed',
            'accepted','discarded'
        ) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'job_not_active');
        END IF;
        IF NOT aap_jobs_v1._video_checkpoint_all_routes_retired(v_job_id,v_run_id) THEN
            RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'recovery_required');
        END IF;
        IF NOT v_has_journal THEN
            IF v_job_missing THEN
                v_outcome := NULL;
            ELSIF v_job.status='cancelled' THEN
                v_outcome := 'cancelled';
            ELSIF v_job.status='failed' THEN
                v_outcome := 'failed';
            ELSE
                v_outcome := 'completed';
            END IF;
            INSERT INTO aap_jobs_v1.video_execution_journals(
                job_id,run_id,writer_terminal,writer_terminal_outcome,execution_retired
            ) VALUES(v_job_id,v_run_id,true,v_outcome,true);
        ELSE
            IF NOT v_journal.writer_terminal THEN
                IF v_job_missing THEN
                    v_outcome := NULL;
                ELSIF v_job.status='cancelled' THEN
                    v_outcome := 'cancelled';
                ELSIF v_job.status='failed' THEN
                    v_outcome := 'failed';
                ELSE
                    v_outcome := 'completed';
                END IF;
                UPDATE aap_jobs_v1.video_execution_journals AS journal
                SET writer_terminal=true,writer_terminal_outcome=v_outcome,
                    updated_at=pg_catalog.clock_timestamp()
                WHERE journal.job_id=v_job_id AND journal.run_id=v_run_id;
            END IF;
            UPDATE aap_jobs_v1.video_execution_journals AS journal
            SET execution_retired=true,updated_at=pg_catalog.clock_timestamp()
            WHERE journal.job_id=v_job_id AND journal.run_id=v_run_id;
        END IF;
        DELETE FROM aap_jobs_v1.video_execution_capture_parts AS part
        USING aap_jobs_v1.video_execution_captures AS capture
        WHERE capture.capture_id=part.capture_id
          AND capture.job_id=v_job_id AND capture.run_id=v_run_id
          AND capture.state='receiving';
        UPDATE aap_jobs_v1.video_execution_captures AS capture
        SET state='abandoned',updated_at=pg_catalog.clock_timestamp()
        WHERE capture.job_id=v_job_id AND capture.run_id=v_run_id
          AND capture.state='receiving';
        UPDATE aap_jobs_v1.video_execution_window_attempts AS attempt
        SET state='abandoned',updated_at=pg_catalog.clock_timestamp()
        WHERE attempt.job_id=v_job_id AND attempt.run_id=v_run_id
          AND attempt.state IN ('started','capturing');
        RETURN aap_jobs_v1._video_checkpoint_reply(
            pg_catalog.jsonb_build_object('retired',true)
        );
    END IF;
    RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'execution_busy');
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'invalid_request');
    WHEN OTHERS THEN
        RETURN aap_jobs_v1._video_checkpoint_reply(NULL,'storage_unavailable');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.video_execution_checkpoint_step(text,text)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_checkpoint_all_routes_retired(
    p_job_id uuid,
    p_run_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_intent record;
BEGIN
    IF p_job_id IS NULL OR p_run_id IS NULL THEN RETURN false; END IF;
    FOR v_intent IN
        SELECT intent.request_id,intent.scope_id,intent.command_sha256,intent.state
        FROM aap_jobs_v1.video_execution_route_intents AS intent
        WHERE intent.job_id=p_job_id AND intent.run_id=p_run_id
    LOOP
        IF v_intent.state = 'bound' THEN
            IF aap_inference_routing_v1.video_execution_scope_retired(
                    v_intent.scope_id
               ) IS DISTINCT FROM true THEN
                RETURN false;
            END IF;
        ELSIF v_intent.state = 'prepared' THEN
            IF aap_inference_routing_v1._video_execution_no_scope_rejection(
                    v_intent.scope_id,v_intent.request_id,v_intent.command_sha256
               ) IS DISTINCT FROM true THEN
                RETURN false;
            END IF;
        ELSE
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_checkpoint_all_routes_retired(uuid,uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1._video_execution_checkpoint_terminal()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_execution aap_jobs_v1.video_executions%ROWTYPE;
    v_journal aap_jobs_v1.video_execution_journals%ROWTYPE;
    v_outcome text;
    v_all_retired boolean;
BEGIN
    IF NEW.status NOT IN (
        'completed','failed','cancelled','pending_review','partially_reviewed',
        'accepted','discarded'
    ) OR OLD.status NOT IN ('queued','running') OR NEW.status='cancelled'
      OR OLD.status IS NOT DISTINCT FROM NEW.status THEN
        RETURN NEW;
    END IF;
    SELECT execution.* INTO v_execution
    FROM aap_jobs_v1.video_executions AS execution
    WHERE execution.job_id=NEW.id
    FOR UPDATE;
    IF NOT FOUND OR v_execution.realm IS DISTINCT FROM aap_jobs_v1._video_execution_realm() THEN
        RETURN NEW;
    END IF;
    IF NEW.status IN ('completed','pending_review') AND EXISTS (
        SELECT 1 FROM aap_jobs_v1.video_execution_journals AS journal
        WHERE journal.job_id=NEW.id AND journal.plan_document IS NOT NULL
    ) AND NOT aap_jobs_v1._video_checkpoint_results_complete(NEW.id,v_execution.run_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='video execution checkpoint incomplete';
    END IF;
    v_all_retired := aap_jobs_v1._video_checkpoint_all_routes_retired(
        NEW.id,v_execution.run_id
    );
    -- Success/failure close atomically only after exact physical retirement.
    -- Cancellation always keeps held-result pins until explicit finish.
    IF v_all_retired IS DISTINCT FROM true THEN
        RETURN NEW;
    END IF;
    IF NEW.status='cancelled' THEN
        v_outcome := 'cancelled';
    ELSIF NEW.status='failed' THEN
        v_outcome := 'failed';
    ELSE
        v_outcome := 'completed';
    END IF;
    INSERT INTO aap_jobs_v1.video_execution_journals(
        job_id,run_id,writer_terminal,writer_terminal_outcome
    ) VALUES(NEW.id,v_execution.run_id,true,v_outcome)
    ON CONFLICT(job_id) DO NOTHING;
    SELECT journal.* INTO v_journal
    FROM aap_jobs_v1.video_execution_journals AS journal
    WHERE journal.job_id=NEW.id
    FOR UPDATE;
    IF NOT FOUND OR v_journal.run_id IS DISTINCT FROM v_execution.run_id THEN
        RAISE EXCEPTION USING
            ERRCODE='55000',MESSAGE='video execution journal identity mismatch';
    END IF;
    IF NOT v_journal.writer_terminal THEN
        UPDATE aap_jobs_v1.video_execution_journals AS journal
        SET writer_terminal=true,writer_terminal_outcome=v_outcome,
            updated_at=pg_catalog.clock_timestamp()
        WHERE journal.job_id=NEW.id AND journal.run_id=v_execution.run_id;
    ELSIF v_journal.writer_terminal_outcome IS NOT NULL
       AND v_journal.writer_terminal_outcome IS DISTINCT FROM v_outcome THEN
        RAISE EXCEPTION USING
            ERRCODE='55000',MESSAGE='video execution terminal outcome conflict';
    END IF;
    UPDATE aap_jobs_v1.video_execution_journals AS journal
    SET execution_retired=true,updated_at=pg_catalog.clock_timestamp()
    WHERE journal.job_id=NEW.id AND journal.run_id=v_execution.run_id;
    DELETE FROM aap_jobs_v1.video_execution_capture_parts AS part
    USING aap_jobs_v1.video_execution_captures AS capture
    WHERE capture.capture_id=part.capture_id
      AND capture.job_id=NEW.id AND capture.run_id=v_execution.run_id
      AND capture.state='receiving';
    UPDATE aap_jobs_v1.video_execution_captures AS capture
    SET state='abandoned',updated_at=pg_catalog.clock_timestamp()
    WHERE capture.job_id=NEW.id AND capture.run_id=v_execution.run_id
      AND capture.state='receiving';
    UPDATE aap_jobs_v1.video_execution_window_attempts AS attempt
    SET state='abandoned',updated_at=pg_catalog.clock_timestamp()
    WHERE attempt.job_id=NEW.id AND attempt.run_id=v_execution.run_id
      AND attempt.state IN ('started','capturing');
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._video_execution_checkpoint_terminal()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_video_execution_checkpoint_terminal
AFTER UPDATE OF status ON public.video_tracker_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1._video_execution_checkpoint_terminal();
-- aap-statement-break
CREATE TABLE aap_labeling_v1.video_execution_checkpoint_mask_sets (
    checkpoint_id text PRIMARY KEY CHECK (checkpoint_id COLLATE "C" ~ '^[0-9a-f]{64}$'),
    job_id uuid NOT NULL,
    object_keys_digest text NOT NULL CHECK (
        object_keys_digest COLLATE "C" ~ '^[0-9a-f]{64}$'
    ),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()
);
-- aap-statement-break
REVOKE ALL ON aap_labeling_v1.video_execution_checkpoint_mask_sets FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.retain_video_execution_checkpoint_masks(
    p_job_id uuid,
    p_checkpoint_id text,
    p_body jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_keys text[];
    v_digest text;
    v_existing text;
    v_existing_job uuid;
    v_key text;
BEGIN
    IF p_job_id IS NULL OR p_checkpoint_id IS NULL
       OR p_checkpoint_id !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_body) IS DISTINCT FROM 'object'
       OR (p_body - ARRAY['frame_results']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_body ?& ARRAY['frame_results'])
       OR pg_catalog.jsonb_typeof(p_body -> 'frame_results') IS DISTINCT FROM 'array'
       OR pg_catalog.octet_length(p_body::text) > 67108864
       OR aap_jobs_v1.video_execution_running_mask_roots_live(p_job_id) IS DISTINCT FROM true THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL,'invalid_request');
    END IF;
    SELECT COALESCE(
        pg_catalog.array_agg(ref.object_key ORDER BY ref.object_key COLLATE "C"),
        ARRAY[]::text[]
    ) INTO v_keys
    FROM (
        SELECT DISTINCT key.object_key
        FROM aap_labeling_v1._mask_gc_json_keys(p_body -> 'frame_results') AS key
    ) AS ref;
    IF pg_catalog.array_position(v_keys,NULL) IS NOT NULL
       OR EXISTS (
            SELECT 1 FROM pg_catalog.unnest(v_keys) AS item(object_key)
            WHERE NOT aap_labeling_v1._mask_gc_key_valid(item.object_key)
       ) THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL,'invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.unnest(v_keys) AS item(object_key)
        WHERE NOT EXISTS (
            SELECT 1 FROM public.mask_gc_roots AS root
            WHERE root.root_kind='video_tracker_running_outputs'
              AND root.root_id=p_job_id::text
              AND root.object_key=item.object_key
        )
    ) THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL,'mask_root_missing');
    END IF;
    v_digest := pg_catalog.encode(public.digest(
        pg_catalog.convert_to(pg_catalog.array_to_string(v_keys,E'\n'),'UTF8'),
        'sha256'
    ),'hex');
    SELECT receipt.object_keys_digest,receipt.job_id
    INTO v_existing,v_existing_job
    FROM aap_labeling_v1.video_execution_checkpoint_mask_sets AS receipt
    WHERE receipt.checkpoint_id = p_checkpoint_id
    FOR UPDATE;
    IF FOUND THEN
        IF v_existing IS DISTINCT FROM v_digest OR v_existing_job IS DISTINCT FROM p_job_id THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL,'checkpoint_conflict');
        END IF;
    ELSE
        INSERT INTO aap_labeling_v1.video_execution_checkpoint_mask_sets(
            checkpoint_id,job_id,object_keys_digest
        ) VALUES(p_checkpoint_id,p_job_id,v_digest);
    END IF;
    FOREACH v_key IN ARRAY v_keys LOOP
        INSERT INTO public.mask_gc_roots(root_kind,root_id,object_key)
        VALUES('video_execution_checkpoints',p_checkpoint_id,v_key)
        ON CONFLICT(root_kind,root_id,object_key) DO NOTHING;
    END LOOP;
    RETURN aap_labeling_v1._mask_gc_reply(
        pg_catalog.jsonb_build_object(
            'job_id',p_job_id,'checkpoint_id',p_checkpoint_id,
            'mask_count',pg_catalog.cardinality(v_keys),'registered',true
        ),NULL
    );
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL,'storage_unavailable');
    WHEN OTHERS THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_labeling_v1.retain_video_execution_checkpoint_masks(uuid,text,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1._mask_gc_release_tracker_running_outputs()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_keys text[];
    v_job_id uuid;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.status = 'running' THEN
        RETURN NEW;
    END IF;
    v_job_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    IF aap_jobs_v1.video_execution_running_mask_roots_live(v_job_id) IS TRUE THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    SELECT COALESCE(
        pg_catalog.array_agg(root.object_key ORDER BY root.object_key COLLATE "C"),
        ARRAY[]::text[]
    ) INTO v_keys
    FROM public.mask_gc_roots AS root
    WHERE root.root_kind = 'video_tracker_running_outputs'
      AND root.root_id = v_job_id::text;
    PERFORM aap_labeling_v1._mask_gc_lock_keys(v_keys);
    DELETE FROM public.mask_gc_roots AS root
    WHERE root.root_kind = 'video_tracker_running_outputs'
      AND root.root_id = v_job_id::text;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1._mask_gc_root_entry_live(
    p_root_kind text,
    p_root_id text,
    p_valid_until timestamptz,
    p_fail_closed boolean
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_reply jsonb;
    v_input_id uuid;
    v_job_id uuid;
    v_job_running boolean;
    v_job_found boolean;
    v_checkpoint_live boolean;
    v_payload_live boolean;
    v_bundle_found boolean;
BEGIN
    IF p_root_kind = 'batch_result_payloads' THEN
        BEGIN
            SELECT payload.complete AND payload.roots_active AND payload.body IS NOT NULL
                       AND bundle.state IN ('capturing','captured','ready'),
                   bundle.result_id IS NOT NULL
            INTO v_payload_live,v_bundle_found
            FROM public.batch_result_payloads AS payload
            LEFT JOIN public.batch_result_bundles AS bundle
              ON bundle.result_id=payload.result_id
             AND bundle.job_id=payload.job_id AND bundle.ordinal=payload.ordinal
            WHERE payload.payload_id=p_root_id::uuid;
            IF NOT FOUND OR NOT COALESCE(v_bundle_found,false) THEN
                RETURN p_fail_closed;
            END IF;
            RETURN COALESCE(v_payload_live,p_fail_closed);
        EXCEPTION WHEN OTHERS THEN
            RETURN p_fail_closed;
        END;
    END IF;
    IF p_root_kind = 'video_tracker_running_outputs' THEN
        BEGIN
            v_job_id := p_root_id::uuid;
            SELECT job.status='running' INTO v_job_running
            FROM public.video_tracker_jobs AS job WHERE job.id=v_job_id;
            v_job_found := FOUND;
            IF v_job_running IS TRUE THEN RETURN true; END IF;
            v_checkpoint_live := aap_jobs_v1.video_execution_running_mask_roots_live(v_job_id);
            IF v_checkpoint_live IS NULL THEN RETURN p_fail_closed; END IF;
            IF NOT v_job_found AND NOT v_checkpoint_live THEN RETURN p_fail_closed; END IF;
            RETURN v_checkpoint_live;
        EXCEPTION WHEN OTHERS THEN
            RETURN p_fail_closed;
        END;
    END IF;
    IF p_root_kind = 'video_execution_checkpoints' THEN
        BEGIN
            v_checkpoint_live := aap_jobs_v1.video_execution_checkpoint_mask_retained(
                p_root_id
            );
            RETURN COALESCE(v_checkpoint_live,p_fail_closed);
        EXCEPTION WHEN OTHERS THEN
            RETURN p_fail_closed;
        END;
    END IF;
    IF p_root_kind = 'video_tracker_input_scopes' THEN
        BEGIN
            v_job_id := p_root_id::uuid;
            v_checkpoint_live :=
                aap_jobs_v1.video_execution_running_mask_roots_live(v_job_id);
            IF v_checkpoint_live IS NULL THEN RETURN p_fail_closed; END IF;
            IF v_checkpoint_live IS TRUE THEN RETURN true; END IF;
            SELECT scope.input_id INTO v_input_id
            FROM public.video_tracker_input_scopes AS scope
            WHERE scope.tracker_job_id=v_job_id;
            IF NOT FOUND THEN RETURN p_fail_closed; END IF;
            v_reply := aap_labeling_v1.video_tracker_input_referenced(
                v_input_id
            );
            IF v_reply IS NULL
               OR v_reply ->> 'schema' IS DISTINCT FROM
                    'aap-labeling-video-tracker-inputs/v1'
               OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
               OR v_reply #>> '{data,input_id}' IS DISTINCT FROM v_input_id::text
               OR pg_catalog.jsonb_typeof(v_reply #> '{data,referenced}')
                    IS DISTINCT FROM 'boolean' THEN
                RETURN p_fail_closed;
            END IF;
            RETURN v_reply #> '{data,referenced}' = 'true'::jsonb;
        EXCEPTION WHEN OTHERS THEN
            RETURN p_fail_closed;
        END;
    END IF;
    RETURN p_valid_until IS NULL OR p_valid_until > pg_catalog.clock_timestamp();
END
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.video_tracker_input_referenced(p_input_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone TO 'UTC'
SET DateStyle TO 'ISO, YMD'
SET extra_float_digits TO 3
AS $function$
DECLARE
    v_scope public.video_tracker_input_scopes%ROWTYPE;
    v_job public.video_tracker_jobs%ROWTYPE;
    v_until timestamptz;
    v_referenced boolean := false;
    v_checkpoint_retained boolean;
BEGIN
    IF p_input_id IS NULL THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_request');
    END IF;
    v_checkpoint_retained := aap_jobs_v1.video_execution_input_retained(p_input_id);
    IF v_checkpoint_retained IS NULL THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
    ELSIF v_checkpoint_retained IS TRUE THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object(
                'input_id',p_input_id,'referenced',true,'retained_until',NULL
            ),NULL
        );
    END IF;
    SELECT scope.* INTO v_scope FROM public.video_tracker_input_scopes AS scope
    WHERE scope.input_id=p_input_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object(
                'input_id',p_input_id,'referenced',false,'retained_until',NULL
            ),NULL
        );
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.annotation_video_tracker_input_sources AS pin
        WHERE pin.input_id=p_input_id
    ) THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object(
                'input_id',p_input_id,'referenced',true,'retained_until',NULL
            ),NULL
        );
    END IF;
    SELECT job.id,job.task_id,job.dataset_item_id,job.annotation_id,job.segment_id,
        job.target_class_name,job.target_tool_unit_id,job.created_by,job.status,job.job_kind,
        job.track_id_snapshot,job.correction_frame,job.model_key,job.direction,job.from_frame,
        job.to_frame,job.prompt,job.staged_result,job.revision,job.cancel_requested_at,
        job.started_at,job.created_at,job.completed_at
    INTO v_job.id,v_job.task_id,v_job.dataset_item_id,v_job.annotation_id,v_job.segment_id,
        v_job.target_class_name,v_job.target_tool_unit_id,v_job.created_by,v_job.status,v_job.job_kind,
        v_job.track_id_snapshot,v_job.correction_frame,v_job.model_key,v_job.direction,v_job.from_frame,
        v_job.to_frame,v_job.prompt,v_job.staged_result,v_job.revision,v_job.cancel_requested_at,
        v_job.started_at,v_job.created_at,v_job.completed_at
    FROM public.video_tracker_jobs AS job WHERE job.id=v_scope.tracker_job_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._video_tracker_input_reply(
            pg_catalog.jsonb_build_object(
                'input_id',p_input_id,'referenced',false,'retained_until',NULL
            ),NULL
        );
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
        pg_catalog.jsonb_build_object(
            'input_id',p_input_id,'referenced',v_referenced,'retained_until',v_until
        ),NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._video_tracker_input_reply(NULL,'invalid_response');
END
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_labeling_v1.guard_dataset_source_deletion(
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_dataset_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    -- A durable execution retains its exact source until physical retirement.
    IF EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        WHERE aap_jobs_v1.video_execution_source_item_retained(source_item.item_id)
            IS DISTINCT FROM false
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
        );
    END IF;

    -- A direct primary source and every multimodal task link retain the item,
    -- including links whose role is not primary_lidar.
    IF EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.tasks AS task
          ON task.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.task_dataset_item_links AS item_link
          ON item_link.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.annotations AS annotation
          ON annotation.sensor_dataset_item_id = source_item.item_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
        );
    END IF;

    -- Deleting an Item may proceed when it has no item-owned history. Dataset
    -- deletion also removes Scenes, so retain all Scene/track/quality history.
    IF p_item_id IS NULL THEN
        IF EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.scene_tracks AS scene_track
              ON scene_track.scene_id = source_scene.scene_id
            LIMIT 1
        ) OR EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.scene_track_operations AS operation
              ON operation.scene_id = source_scene.scene_id
            LIMIT 1
        ) OR EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.point_cloud_quality_issues AS issue
              ON issue.scene_id = source_scene.scene_id
            LIMIT 1
        ) OR EXISTS (
            SELECT 1
            FROM aap_assets_v1.dataset_scene_references(p_dataset_id)
                AS source_scene
            JOIN public.point_cloud_quality_runs AS quality_run
              ON (quality_run.scope_json -> 'scene_ids') ? source_scene.scene_id::text
            LIMIT 1
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
            );
        END IF;
    END IF;

    -- A selected Item also retains history for its containing Scene. Resolve
    -- that relationship through Assets' narrow projection rather than reading
    -- dataset_items from the Labeling owner.
    IF p_item_id IS NOT NULL AND EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_scene_references(p_dataset_id, p_item_id)
            AS source_scene
        WHERE EXISTS (
            SELECT 1 FROM public.scene_tracks AS scene_track
            WHERE scene_track.scene_id = source_scene.scene_id
        ) OR EXISTS (
            SELECT 1 FROM public.scene_track_operations AS operation
            WHERE operation.scene_id = source_scene.scene_id
        ) OR EXISTS (
            SELECT 1 FROM public.point_cloud_quality_issues AS issue
            WHERE issue.scene_id = source_scene.scene_id
        ) OR EXISTS (
            SELECT 1 FROM public.point_cloud_quality_runs AS quality_run
            WHERE (quality_run.scope_json -> 'scene_ids') ? source_scene.scene_id::text
        )
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
        );
    END IF;

    -- Video segment/tracker records are retained evidence. DatasetItem FKs
    -- cascade, so reject deletion before those rows could disappear silently.
    IF EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.video_segments AS segment
          ON segment.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.video_tracker_jobs AS tracker_job
          ON tracker_job.dataset_item_id = source_item.item_id
        LIMIT 1
    ) OR EXISTS (
        SELECT 1
        FROM aap_assets_v1.dataset_item_references(p_dataset_id, p_item_id)
            AS source_item
        JOIN public.video_chapters AS chapter
          ON chapter.dataset_item_id = source_item.item_id
        LIMIT 1
    ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-source-retention/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'source_in_use')
        );
    END IF;

    IF p_item_id IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM public.task_creation_operations AS operation
            WHERE operation.dataset_id = p_dataset_id
              AND operation.state IN ('accepted', 'running')
              AND EXISTS (
                  SELECT 1
                  FROM aap_assets_v1.dataset_association_references(
                      p_dataset_id
                  ) AS association
                  WHERE association.association_id = operation.association_id
                    AND association.project_id = operation.project_id
              )
              AND aap_jobs_v1.task_creation_execution_active(
                  operation.id, operation.job_id
              ) IS DISTINCT FROM false
            LIMIT 1
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-source-retention/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'code', 'creation_in_progress'
                )
            );
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-source-retention/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object()
    );
END;
$function$;
-- aap-statement-break
