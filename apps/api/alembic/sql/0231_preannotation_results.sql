-- Durable ordinary batch prediction state. Jobs owns selection and progress;
-- Labeling owns per-request bytes and immutable Prediction receipts.

CREATE TABLE public.batch_result_executions (
    job_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    project_id uuid NOT NULL,
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    execution_id text,
    state text NOT NULL CHECK (state IN ('preparing', 'running', 'completed', 'failed', 'cancelled')),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    worker_id uuid,
    lease_token uuid,
    lease_expires_at timestamptz,
    total bigint CHECK (total IS NULL OR total >= 0),
    selection_cursor bigint NOT NULL DEFAULT 0 CHECK (selection_cursor >= 0),
    selection_complete boolean NOT NULL DEFAULT false,
    next_ordinal bigint NOT NULL DEFAULT 1 CHECK (next_ordinal >= 1),
    bulk_overwrite_done boolean NOT NULL DEFAULT false,
    success_count bigint NOT NULL DEFAULT 0 CHECK (success_count >= 0),
    failed_count bigint NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    skipped_count bigint NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    total_cost text NOT NULL DEFAULT '0' CHECK (total_cost ~ '^-?(0|[1-9][0-9]*)([.][0-9]+)?$'),
    pipeline_stages jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (pg_catalog.jsonb_typeof(pipeline_stages) = 'object'),
    gpu_arbiter_failures jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (pg_catalog.jsonb_typeof(gpu_arbiter_failures) = 'array'),
    result_extra jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (pg_catalog.jsonb_typeof(result_extra) = 'object'),
    error_code text,
    last_page_after_ordinal bigint,
    last_page_digest text CHECK (last_page_digest IS NULL OR last_page_digest ~ '^[0-9a-f]{64}$'),
    last_page_complete boolean,
    last_page_result jsonb CHECK (last_page_result IS NULL OR pg_catalog.jsonb_typeof(last_page_result) = 'object'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    CHECK (
        (worker_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR (worker_id IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK (
        (last_page_after_ordinal IS NULL AND last_page_digest IS NULL
            AND last_page_complete IS NULL AND last_page_result IS NULL)
        OR (last_page_after_ordinal IS NOT NULL AND last_page_digest IS NOT NULL
            AND last_page_complete IS NOT NULL AND last_page_result IS NOT NULL)
    ),
    CHECK ((selection_complete AND total IS NOT NULL) OR (NOT selection_complete AND total IS NULL)),
    CHECK (total IS NULL OR selection_cursor <= total)
);
-- aap-statement-break
CREATE TABLE public.batch_result_members (
    job_id uuid NOT NULL REFERENCES public.batch_result_executions(job_id) ON DELETE CASCADE,
    ordinal bigint NOT NULL CHECK (ordinal >= 1),
    task_id uuid NOT NULL,
    write_scope jsonb CHECK (write_scope IS NULL OR pg_catalog.jsonb_typeof(write_scope) = 'object'),
    manifest_member jsonb CHECK (manifest_member IS NULL OR pg_catalog.jsonb_typeof(manifest_member) = 'object'),
    failure_id uuid NOT NULL,
    overwrite_done boolean NOT NULL DEFAULT false,
    state text NOT NULL DEFAULT 'planned' CHECK (state IN ('planned', 'prepared', 'capturing', 'captured', 'ready', 'applied', 'failed', 'skipped')),
    result_id uuid,
    failed_prediction_id uuid,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    PRIMARY KEY (job_id, ordinal),
    UNIQUE (job_id, task_id),
    UNIQUE (failure_id)
);
-- aap-statement-break
CREATE INDEX ix_batch_result_members_state ON public.batch_result_members(job_id, state, ordinal);
-- aap-statement-break
CREATE TABLE public.batch_result_bundles (
    result_id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    ordinal bigint NOT NULL CHECK (ordinal >= 1),
    task_id uuid NOT NULL,
    result_count bigint NOT NULL CHECK (result_count >= 0),
    raw_digest text NOT NULL CHECK (raw_digest ~ '^[0-9a-f]{64}$'),
    prepared_digest text CHECK (prepared_digest IS NULL OR prepared_digest ~ '^[0-9a-f]{64}$'),
    raw_complete boolean NOT NULL DEFAULT false,
    prepared_complete boolean NOT NULL DEFAULT false,
    state text NOT NULL CHECK (state IN ('capturing', 'captured', 'ready', 'applied', 'failed', 'abandoned')),
    context jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (pg_catalog.jsonb_typeof(context) = 'object'),
    receipt_count bigint NOT NULL DEFAULT 0 CHECK (receipt_count >= 0),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    applied_at timestamptz,
    FOREIGN KEY (job_id, ordinal)
        REFERENCES public.batch_result_members(job_id, ordinal) ON DELETE RESTRICT,
    UNIQUE (job_id, ordinal),
    UNIQUE (result_id, job_id, ordinal),
    CHECK (NOT prepared_complete OR raw_complete),
    CHECK (
        (state = 'capturing' AND NOT prepared_complete AND applied_at IS NULL)
        OR (state = 'captured' AND raw_complete AND applied_at IS NULL)
        OR (state = 'ready' AND raw_complete AND prepared_complete AND applied_at IS NULL)
        OR (state = 'applied' AND raw_complete AND prepared_complete AND applied_at IS NOT NULL)
        OR (state IN ('failed', 'abandoned') AND applied_at IS NULL)
    ),
    CHECK (receipt_count = 0 OR state = 'applied')
);
-- aap-statement-break
CREATE INDEX ix_batch_result_bundles_pending ON public.batch_result_bundles(job_id, state, ordinal)
    WHERE state NOT IN ('applied', 'failed', 'abandoned');
-- aap-statement-break
CREATE TABLE public.batch_result_payloads (
    payload_id uuid PRIMARY KEY,
    result_id uuid NOT NULL,
    job_id uuid NOT NULL,
    ordinal bigint NOT NULL CHECK (ordinal >= 1),
    request_index bigint NOT NULL CHECK (request_index >= 0),
    prediction_id uuid NOT NULL,
    prediction_created_at timestamptz NOT NULL,
    stage text NOT NULL CHECK (stage IN ('raw', 'prepared')),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    part_count bigint NOT NULL CHECK (part_count >= 1),
    complete boolean NOT NULL DEFAULT false,
    roots_active boolean NOT NULL DEFAULT false,
    tool_unit_id text,
    body jsonb CHECK (body IS NULL OR pg_catalog.jsonb_typeof(body) = 'object'),
    receipt jsonb CHECK (receipt IS NULL OR pg_catalog.jsonb_typeof(receipt) = 'object'),
    created_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp(),
    FOREIGN KEY (result_id, job_id, ordinal)
        REFERENCES public.batch_result_bundles(result_id, job_id, ordinal) ON DELETE CASCADE,
    UNIQUE (result_id, stage, request_index),
    UNIQUE (payload_id, result_id),
    CHECK (complete OR body IS NULL),
    CHECK (NOT roots_active OR (complete AND body IS NOT NULL)),
    CHECK (receipt IS NULL OR stage IN ('raw', 'prepared')),
    CHECK (tool_unit_id IS NULL OR pg_catalog.octet_length(tool_unit_id) <= 30)
);
-- aap-statement-break
CREATE UNIQUE INDEX uq_batch_result_raw_prediction_id
    ON public.batch_result_payloads(prediction_id) WHERE stage='raw';
-- aap-statement-break
CREATE INDEX ix_batch_result_payloads_job ON public.batch_result_payloads(job_id, ordinal, stage, request_index);
-- aap-statement-break
CREATE TABLE public.batch_result_parts (
    payload_id uuid NOT NULL REFERENCES public.batch_result_payloads(payload_id) ON DELETE CASCADE,
    part_index bigint NOT NULL CHECK (part_index >= 0),
    content bytea NOT NULL CHECK (pg_catalog.octet_length(content) <= 524288),
    PRIMARY KEY (payload_id, part_index)
);
-- aap-statement-break
REVOKE ALL ON public.batch_result_executions, public.batch_result_members,
    public.batch_result_bundles, public.batch_result_payloads,
    public.batch_result_parts FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._batch_result_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT CASE
      WHEN p_code IS NULL AND pg_catalog.octet_length(
          pg_catalog.jsonb_build_object(
              'schema', 'aap-preannotation-results/v1', 'ok', true, 'data', p_data
          )::text
      ) <= 2097152 THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-preannotation-results/v1', 'ok', true, 'data', p_data
      )
      WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-preannotation-results/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
      ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-preannotation-results/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._batch_result_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._batch_result_reply(p_data jsonb, p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT CASE
      WHEN p_code IS NULL AND pg_catalog.octet_length(
          pg_catalog.jsonb_build_object(
              'schema', 'aap-preannotation-results/v1', 'ok', true, 'data', p_data
          )::text
      ) <= 2097152 THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-preannotation-results/v1', 'ok', true, 'data', p_data
      )
      WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-preannotation-results/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', 'invalid_response')
      ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-preannotation-results/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._batch_result_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._batch_result_scope_valid(p_payload jsonb, p_lease boolean)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_keys text[] := ARRAY['job_id', 'actor_id', 'project_id', 'request_digest'];
    v_uuid text;
BEGIN
    IF p_lease THEN
        v_keys := v_keys || ARRAY['generation', 'lease_token'];
    END IF;
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text) > 2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR NOT (p_payload ?& v_keys) THEN
        RETURN false;
    END IF;
    FOREACH v_uuid IN ARRAY ARRAY[
        p_payload ->> 'job_id', p_payload ->> 'actor_id', p_payload ->> 'project_id'
    ] LOOP
        IF v_uuid IS NULL OR v_uuid !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN false;
        END IF;
    END LOOP;
    IF pg_catalog.jsonb_typeof(p_payload -> 'request_digest') IS DISTINCT FROM 'string'
       OR p_payload ->> 'request_digest' !~ '^[0-9a-f]{64}$' THEN
        RETURN false;
    END IF;
    IF p_lease AND (
        pg_catalog.jsonb_typeof(p_payload -> 'generation') IS DISTINCT FROM 'number'
        OR p_payload ->> 'generation' !~ '^[1-9][0-9]*$'
        OR pg_catalog.char_length(p_payload ->> 'generation') > 19
        OR (p_payload ->> 'generation')::numeric > 9223372036854775807
        OR pg_catalog.jsonb_typeof(p_payload -> 'lease_token') IS DISTINCT FROM 'string'
        OR p_payload ->> 'lease_token' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    ) THEN
        RETURN false;
    END IF;
    RETURN true;
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._batch_result_scope_valid(jsonb, boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._batch_result_member_json(p_member public.batch_result_members)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'ordinal', p_member.ordinal, 'task_id', p_member.task_id,
        'write_scope', p_member.write_scope,
        'manifest_member', p_member.manifest_member,
        'failure_id', p_member.failure_id,
        'overwrite_done', p_member.overwrite_done,
        'state', p_member.state,
        'result_id', p_member.result_id,
        'failed_prediction_id', p_member.failed_prediction_id,
        'error_code', p_member.error_code
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._batch_result_member_json(public.batch_result_members) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._batch_result_progress(p_job_id uuid)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_execution public.batch_result_executions%ROWTYPE;
    v_job public.async_jobs%ROWTYPE;
    v_state text;
BEGIN
    SELECT * INTO v_execution FROM public.batch_result_executions WHERE job_id = p_job_id;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = p_job_id;
    v_state := CASE
        WHEN v_execution.state = 'preparing' THEN 'preparing'
        WHEN v_execution.state IN ('completed', 'failed', 'cancelled') THEN v_execution.state
        WHEN v_job.status = 'cancelled' THEN 'cancelled'
        WHEN v_job.status = 'failed' THEN 'failed'
        WHEN v_job.status = 'completed' THEN 'completed'
        ELSE 'running'
    END;
    RETURN pg_catalog.jsonb_build_object(
        'job_id', p_job_id, 'state', v_state,
        'total', v_execution.total,
        'selection_cursor', v_execution.selection_cursor,
        'selection_complete', v_execution.selection_complete,
        'next_ordinal', v_execution.next_ordinal,
        'bulk_overwrite_done', v_execution.bulk_overwrite_done,
        'success_count', v_execution.success_count,
        'failed_count', v_execution.failed_count,
        'skipped_count', v_execution.skipped_count,
        'total_cost', v_execution.total_cost,
        'pipeline_stages', v_execution.pipeline_stages,
        'gpu_arbiter_failures', v_execution.gpu_arbiter_failures
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._batch_result_progress(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._lock_batch_result_scope(
    p_scope jsonb,
    p_require_lease boolean,
    p_require_current boolean DEFAULT false
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_cancel_requested boolean := false;
BEGIN
    IF NOT aap_jobs_v1._batch_result_scope_valid(p_scope, p_require_lease) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT * INTO v_job FROM public.async_jobs
        WHERE id = (p_scope ->> 'job_id')::uuid
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'execution_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing');
    END IF;
    IF v_job.kind IS DISTINCT FROM 'batch_predict'
       OR (v_job.user_id IS DISTINCT FROM (p_scope ->> 'actor_id')::uuid
           AND (p_require_current OR v_job.user_id IS NOT NULL))
       OR COALESCE(v_job.project_id, v_job.retained_project_id)
            IS DISTINCT FROM (p_scope ->> 'project_id')::uuid THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
    END IF;
    IF pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object'
       OR (v_job.payload ? 'cancel_requested'
           AND pg_catalog.jsonb_typeof(v_job.payload -> 'cancel_requested') IS DISTINCT FROM 'boolean')
       OR (v_job.payload ? 'preannotation_request_digest'
           AND (pg_catalog.jsonb_typeof(v_job.payload -> 'preannotation_request_digest') IS DISTINCT FROM 'string'
                OR v_job.payload ->> 'preannotation_request_digest' !~ '^[0-9a-f]{64}$')) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
    END IF;
    IF v_job.payload ? 'preannotation_request_digest'
       AND v_job.payload ->> 'preannotation_request_digest' IS DISTINCT FROM p_scope ->> 'request_digest' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    v_cancel_requested := COALESCE((v_job.payload ->> 'cancel_requested')::boolean, false);
    IF v_job.status NOT IN ('pending', 'running') AND p_require_current THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    BEGIN
        SELECT * INTO v_execution FROM public.batch_result_executions
        WHERE job_id = v_job.id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'execution_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing');
    END IF;
    IF v_execution.actor_id IS DISTINCT FROM (p_scope ->> 'actor_id')::uuid
       OR v_execution.project_id IS DISTINCT FROM (p_scope ->> 'project_id')::uuid
       OR v_execution.request_digest IS DISTINCT FROM p_scope ->> 'request_digest'
       OR (v_job.payload ? 'preannotation_request_digest'
           AND v_job.payload ->> 'preannotation_request_digest' IS DISTINCT FROM v_execution.request_digest) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
    END IF;
    IF p_require_lease THEN
        IF v_execution.generation IS DISTINCT FROM (p_scope ->> 'generation')::bigint
           OR v_execution.lease_token IS DISTINCT FROM (p_scope ->> 'lease_token')::uuid
           OR v_execution.lease_token IS NULL
           OR v_execution.lease_expires_at IS NULL
           OR v_execution.lease_expires_at <= pg_catalog.clock_timestamp() THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'lease_lost');
        END IF;
    END IF;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'job_status', v_job.status,
            'cancel_requested', v_cancel_requested,
            'progress', aap_jobs_v1._batch_result_progress(v_job.id)
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._lock_batch_result_scope(jsonb, boolean, boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._batch_result_scope_valid(p_payload jsonb, p_lease boolean)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT aap_jobs_v1._batch_result_scope_valid(p_payload, p_lease)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._batch_result_scope_valid(jsonb, boolean) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._batch_result_mask_roots_trigger()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_old_id text;
    v_new_id text;
    v_old_body jsonb;
    v_new_body jsonb;
    v_old_live text[] := ARRAY[]::text[];
    v_keys text[] := ARRAY[]::text[];
    v_ref record;
    v_epoch bigint;
    v_active_operation uuid;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        v_old_id := OLD.payload_id::text;
        v_old_body := OLD.body;
        SELECT COALESCE(pg_catalog.array_agg(root.object_key ORDER BY root.object_key COLLATE "C"), ARRAY[]::text[])
        INTO v_old_live
        FROM public.mask_gc_roots AS root
        WHERE root.root_kind = 'batch_result_payloads'
          AND root.root_id = v_old_id
          AND aap_labeling_v1._mask_gc_root_entry_live(
              root.root_kind, root.root_id, root.valid_until, false
          );
    END IF;
    IF TG_OP <> 'DELETE' THEN
        v_new_id := NEW.payload_id::text;
        IF NEW.complete AND NEW.roots_active AND NEW.body IS NOT NULL THEN
            SELECT COALESCE(pg_catalog.array_agg(DISTINCT keys.object_key COLLATE "C" ORDER BY keys.object_key COLLATE "C"), ARRAY[]::text[])
            INTO v_keys
            FROM aap_labeling_v1._mask_gc_json_keys(NEW.body) AS keys
            WHERE aap_labeling_v1._mask_gc_key_valid(keys.object_key);
            v_new_body := NEW.body;
        END IF;
    END IF;
    IF v_old_body IS NOT NULL THEN
        SELECT COALESCE(pg_catalog.array_agg(DISTINCT keys.object_key COLLATE "C" ORDER BY keys.object_key COLLATE "C"), ARRAY[]::text[])
        INTO v_keys
        FROM (
            SELECT unnest(v_keys) AS object_key
            UNION ALL
            SELECT refs.object_key
            FROM aap_labeling_v1._mask_gc_json_keys(v_old_body) AS refs
            WHERE aap_labeling_v1._mask_gc_key_valid(refs.object_key)
        ) AS all_keys;
    END IF;
    PERFORM aap_labeling_v1._mask_gc_lock_keys(v_keys);
    IF v_old_id IS NOT NULL THEN
        DELETE FROM public.mask_gc_roots
        WHERE root_kind = 'batch_result_payloads' AND root_id = v_old_id;
    END IF;
    IF v_new_id IS NULL OR v_new_body IS NULL THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    FOR v_ref IN
        SELECT DISTINCT refs.object_key COLLATE "C" AS object_key
        FROM aap_labeling_v1._mask_gc_json_keys(v_new_body) AS refs
        WHERE aap_labeling_v1._mask_gc_key_valid(refs.object_key)
        ORDER BY refs.object_key COLLATE "C"
    LOOP
        v_epoch := 0;
        v_active_operation := NULL;
        SELECT epoch.epoch, epoch.active_operation_id
        INTO v_epoch, v_active_operation
        FROM public.mask_gc_key_epochs AS epoch
        WHERE epoch.object_key = v_ref.object_key;
        IF v_active_operation IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'AM409', MESSAGE = 'content_deleting';
        END IF;
        IF COALESCE(v_epoch, 0) > 0
           AND NOT aap_labeling_v1._mask_gc_permit_is_current(v_ref.object_key, v_epoch)
           AND NOT (v_ref.object_key = ANY(v_old_live))
           AND NOT aap_labeling_v1._mask_gc_has_live_root(v_ref.object_key, false) THEN
            RAISE EXCEPTION USING ERRCODE = 'AM409', MESSAGE = 'content_epoch_changed';
        END IF;
        INSERT INTO public.mask_gc_roots(root_kind, root_id, object_key)
        VALUES ('batch_result_payloads', v_new_id, v_ref.object_key)
        ON CONFLICT (root_kind, root_id, object_key) DO NOTHING;
    END LOOP;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._batch_result_mask_roots_trigger() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_batch_result_payload_mask_roots
AFTER INSERT OR UPDATE OF complete, roots_active, body OR DELETE ON public.batch_result_payloads
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._batch_result_mask_roots_trigger();
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._batch_result_lease(p_execution public.batch_result_executions)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'job_id', p_execution.job_id,
        'actor_id', p_execution.actor_id,
        'project_id', p_execution.project_id,
        'request_digest', p_execution.request_digest,
        'generation', p_execution.generation,
        'lease_token', p_execution.lease_token,
        'worker_id', p_execution.worker_id,
        'execution_id', p_execution.execution_id,
        'lease_expires_at', p_execution.lease_expires_at
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._batch_result_lease(public.batch_result_executions) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._batch_result_scope_matches_member(
    p_scope jsonb,
    p_job_id uuid,
    p_actor_id uuid,
    p_project_id uuid,
    p_task_id uuid
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_allowed text[] := ARRAY[
        'task_id', 'project_id', 'actor_id', 'actor_known', 'operation',
        'execution_scope', 'execution_scope_known', 'task_version', 'task_status',
        'assignee_id', 'review_round_id', 'source_annotation_id',
        'source_annotation_version', 'expected_batch_id', 'expected_batch_id_known',
        'job_id', 'job_id_known', 'source_binding_known', 'task_source_generation',
        'source_dataset_item_id', 'source_file_path', 'source_file_type',
        'accepted_input_id'
    ];
    v_key text;
    v_value jsonb;
BEGIN
    IF p_scope IS NULL OR p_scope = 'null'::jsonb THEN RETURN true; END IF;
    IF pg_catalog.jsonb_typeof(p_scope) IS DISTINCT FROM 'object'
       OR (p_scope - v_allowed) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_scope ?& ARRAY[
            'task_id', 'project_id', 'actor_id', 'actor_known', 'operation',
            'execution_scope', 'execution_scope_known', 'task_version', 'task_status',
            'assignee_id', 'review_round_id', 'source_annotation_id',
            'source_annotation_version', 'expected_batch_id', 'expected_batch_id_known',
            'job_id', 'job_id_known', 'source_binding_known', 'task_source_generation',
            'source_dataset_item_id', 'source_file_path', 'source_file_type',
            'accepted_input_id'
       ]) THEN
        RETURN false;
    END IF;
    IF p_scope ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR p_scope ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR p_scope ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR p_scope ->> 'job_id' IS DISTINCT FROM p_job_id::text
       OR p_scope ->> 'operation' IS DISTINCT FROM 'preannotation'
       OR p_scope -> 'actor_known' IS DISTINCT FROM 'true'::jsonb
       OR p_scope -> 'execution_scope_known' IS DISTINCT FROM 'true'::jsonb
       OR p_scope -> 'job_id_known' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN false;
    END IF;
    FOREACH v_key IN ARRAY ARRAY['task_id', 'project_id', 'actor_id', 'job_id'] LOOP
        IF pg_catalog.jsonb_typeof(p_scope -> v_key) IS DISTINCT FROM 'string'
           OR p_scope ->> v_key !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN false;
        END IF;
    END LOOP;
    FOREACH v_key IN ARRAY ARRAY['actor_known', 'execution_scope_known', 'expected_batch_id_known', 'job_id_known', 'source_binding_known'] LOOP
        IF pg_catalog.jsonb_typeof(p_scope -> v_key) IS DISTINCT FROM 'boolean' THEN
            RETURN false;
        END IF;
    END LOOP;
    IF pg_catalog.jsonb_typeof(p_scope -> 'operation') IS DISTINCT FROM 'string'
       OR (p_scope -> 'execution_scope' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_scope -> 'execution_scope') IS DISTINCT FROM 'string')
       OR (p_scope -> 'task_status' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_scope -> 'task_status') IS DISTINCT FROM 'string')
       OR (p_scope -> 'source_file_path' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_scope -> 'source_file_path') IS DISTINCT FROM 'string')
       OR (p_scope -> 'source_file_type' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_scope -> 'source_file_type') IS DISTINCT FROM 'string') THEN
        RETURN false;
    END IF;
    FOREACH v_key IN ARRAY ARRAY[
        'assignee_id', 'review_round_id', 'source_annotation_id',
        'expected_batch_id', 'source_dataset_item_id', 'accepted_input_id'
    ] LOOP
        v_value := p_scope -> v_key;
        IF v_value <> 'null'::jsonb AND (
            pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'string'
            OR v_value #>> '{}' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ) THEN
            RETURN false;
        END IF;
    END LOOP;
    FOREACH v_key IN ARRAY ARRAY['task_version', 'source_annotation_version', 'task_source_generation'] LOOP
        v_value := p_scope -> v_key;
        IF v_value <> 'null'::jsonb AND (
            pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'number'
            OR v_value #>> '{}' !~ '^(0|[1-9][0-9]*)$'
        ) THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._batch_result_scope_matches_member(jsonb, uuid, uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.claim_batch_result_execution(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_worker uuid;
    v_execution_id text;
    v_seconds integer;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_lease jsonb;
BEGIN
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text) > 2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id', 'actor_id', 'project_id', 'request_digest', 'worker_id', 'execution_id', 'lease_seconds']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id', 'actor_id', 'project_id', 'request_digest', 'worker_id', 'execution_id', 'lease_seconds'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(
            p_payload - ARRAY['worker_id', 'execution_id', 'lease_seconds'], false
       )
       OR pg_catalog.jsonb_typeof(p_payload -> 'worker_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'worker_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR (p_payload -> 'execution_id' <> 'null'::jsonb
           AND pg_catalog.jsonb_typeof(p_payload -> 'execution_id') IS DISTINCT FROM 'string')
       OR pg_catalog.jsonb_typeof(p_payload -> 'lease_seconds') IS DISTINCT FROM 'number'
       OR p_payload ->> 'lease_seconds' !~ '^[0-9]+$' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_worker := (p_payload ->> 'worker_id')::uuid;
    v_execution_id := CASE WHEN p_payload -> 'execution_id' = 'null'::jsonb
        THEN NULL ELSE p_payload ->> 'execution_id' END;
    v_seconds := (p_payload ->> 'lease_seconds')::integer;
    IF v_seconds < 5 OR v_seconds > 300 THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT * INTO v_job FROM public.async_jobs
        WHERE id = (p_payload ->> 'job_id')::uuid
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'execution_busy');
    END;
    IF NOT FOUND THEN RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing'); END IF;
    IF v_job.kind IS DISTINCT FROM 'batch_predict'
       OR v_job.user_id IS DISTINCT FROM (p_payload ->> 'actor_id')::uuid
       OR v_job.project_id IS DISTINCT FROM (p_payload ->> 'project_id')::uuid
       OR (v_job.retained_project_id IS NOT NULL AND v_job.retained_project_id IS DISTINCT FROM v_job.project_id) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
    END IF;
    IF pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object'
       OR (v_job.payload ? 'cancel_requested'
           AND pg_catalog.jsonb_typeof(v_job.payload -> 'cancel_requested') IS DISTINCT FROM 'boolean')
       OR (v_job.payload ? 'preannotation_request_digest'
           AND (pg_catalog.jsonb_typeof(v_job.payload -> 'preannotation_request_digest') IS DISTINCT FROM 'string'
                OR v_job.payload ->> 'preannotation_request_digest' !~ '^[0-9a-f]{64}$')) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
    END IF;
    IF v_job.payload ? 'preannotation_request_digest'
       AND v_job.payload ->> 'preannotation_request_digest' IS DISTINCT FROM p_payload ->> 'request_digest' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    IF v_job.status NOT IN ('pending', 'running')
       OR COALESCE(v_job.payload -> 'cancel_requested', 'false'::jsonb) = 'true'::jsonb THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    BEGIN
        SELECT * INTO v_execution FROM public.batch_result_executions
        WHERE job_id = v_job.id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'execution_busy');
    END;
    IF NOT FOUND THEN
        IF v_job.status <> 'pending' THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
        END IF;
        INSERT INTO public.batch_result_executions(
            job_id, actor_id, project_id, request_digest, execution_id,
            state, generation, worker_id, lease_token, lease_expires_at
        ) VALUES (
            v_job.id, v_job.user_id, v_job.project_id,
            p_payload ->> 'request_digest', v_execution_id,
            'preparing', 1, v_worker, pg_catalog.gen_random_uuid(),
            v_now + pg_catalog.make_interval(secs => v_seconds)
        ) RETURNING * INTO v_execution;
    ELSE
        IF v_execution.actor_id IS DISTINCT FROM v_job.user_id
           OR v_execution.project_id IS DISTINCT FROM v_job.project_id
           OR v_execution.request_digest IS DISTINCT FROM p_payload ->> 'request_digest' THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
        END IF;
        IF v_execution.state IN ('completed', 'failed', 'cancelled') THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
        END IF;
        IF v_execution.lease_token IS NOT NULL
           AND v_execution.lease_expires_at > v_now THEN
            IF v_execution.worker_id = v_worker
               AND v_execution.execution_id IS NOT DISTINCT FROM v_execution_id THEN
                v_lease := aap_jobs_v1._batch_result_lease(v_execution);
                RETURN aap_jobs_v1._batch_result_reply(
                    pg_catalog.jsonb_build_object('lease', v_lease, 'progress', aap_jobs_v1._batch_result_progress(v_job.id)), NULL
                );
            END IF;
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'execution_busy');
        END IF;
        IF v_execution.generation >= 9223372036854775807 THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
        END IF;
        UPDATE public.batch_result_executions
        SET generation = generation + 1,
            worker_id = v_worker,
            lease_token = pg_catalog.gen_random_uuid(),
            execution_id = v_execution_id,
            lease_expires_at = v_now + pg_catalog.make_interval(secs => v_seconds),
            updated_at = v_now
        WHERE job_id = v_job.id
        RETURNING * INTO v_execution;
    END IF;
    v_lease := aap_jobs_v1._batch_result_lease(v_execution);
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'lease', v_lease, 'progress', aap_jobs_v1._batch_result_progress(v_job.id)
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.claim_batch_result_execution(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.heartbeat_batch_result_execution(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_seconds integer;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','lease_seconds']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','lease_seconds'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - 'lease_seconds', true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'lease_seconds') IS DISTINCT FROM 'number'
       OR p_payload ->> 'lease_seconds' !~ '^[0-9]+$' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_seconds := (p_payload ->> 'lease_seconds')::integer;
    IF v_seconds < 5 OR v_seconds > 300 THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload - 'lease_seconds', true, true);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_locked; END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = (p_payload ->> 'job_id')::uuid;
    UPDATE public.batch_result_executions
    SET lease_expires_at = v_now + pg_catalog.make_interval(secs => v_seconds),
        updated_at = v_now
    WHERE job_id = v_execution.job_id
    RETURNING * INTO v_execution;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'lease', aap_jobs_v1._batch_result_lease(v_execution),
            'progress', aap_jobs_v1._batch_result_progress(v_execution.job_id)
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.heartbeat_batch_result_execution(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.release_batch_result_execution(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
BEGIN
    IF NOT aap_jobs_v1._batch_result_scope_valid(p_payload, true) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload, false, false);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_locked; END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = (p_payload ->> 'job_id')::uuid;
    IF v_execution.generation IS DISTINCT FROM (p_payload ->> 'generation')::bigint
       OR v_execution.lease_token IS DISTINCT FROM (p_payload ->> 'lease_token')::uuid
       OR v_execution.lease_token IS NULL THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'lease_lost');
    END IF;
    UPDATE public.batch_result_executions
    SET worker_id = NULL, lease_token = NULL, lease_expires_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_execution.job_id;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('progress', aap_jobs_v1._batch_result_progress(v_execution.job_id)), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.release_batch_result_execution(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.stage_batch_result_members(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_item jsonb;
    v_ordinal bigint;
    v_task uuid;
    v_after bigint;
    v_complete boolean;
    v_count bigint;
    v_seen bigint := 0;
    v_digest text;
    v_progress jsonb;
    v_manifest jsonb;
    v_manifest_item jsonb;
    v_input jsonb;
    v_job public.async_jobs%ROWTYPE;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'after_ordinal','members','complete'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'after_ordinal','members','complete'
       ])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - ARRAY['after_ordinal','members','complete'], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'after_ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'after_ordinal' !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_payload ->> 'after_ordinal') > 19
       OR (p_payload ->> 'after_ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'members') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_payload -> 'members') > 100
       OR pg_catalog.jsonb_typeof(p_payload -> 'complete') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_after := (p_payload ->> 'after_ordinal')::bigint;
    v_complete := (p_payload -> 'complete') = 'true'::jsonb;
    v_count := pg_catalog.jsonb_array_length(p_payload -> 'members');
    v_digest := pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                pg_catalog.jsonb_build_object(
                    'after_ordinal', v_after,
                    'members', p_payload -> 'members',
                    'complete', v_complete
                )::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload - ARRAY['after_ordinal','members','complete'], false, false);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_locked; END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = (p_payload ->> 'job_id')::uuid;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = v_execution.job_id;
    IF v_execution.generation IS DISTINCT FROM (p_payload ->> 'generation')::bigint
       OR v_execution.lease_token IS DISTINCT FROM (p_payload ->> 'lease_token')::uuid
       OR v_execution.lease_token IS NULL THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'lease_lost');
    END IF;

    -- A byte-identical retry of the last accepted page is read-only. Check it
    -- after the fencing token but before cursor/completion/expiry checks.
    IF v_execution.last_page_digest = v_digest
       AND v_execution.last_page_after_ordinal = v_after
       AND v_execution.last_page_complete = v_complete THEN
        RETURN aap_jobs_v1._batch_result_reply(
            pg_catalog.jsonb_build_object('progress', v_execution.last_page_result), NULL
        );
    END IF;
    IF v_execution.lease_expires_at IS NULL
       OR v_execution.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'lease_lost');
    END IF;
    IF v_execution.selection_complete OR v_after <> v_execution.selection_cursor THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    IF v_execution.state <> 'preparing'
       OR (SELECT status FROM public.async_jobs WHERE id = v_execution.job_id) NOT IN ('pending', 'running') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    IF v_count = 0 AND NOT v_complete THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;

    FOR v_item IN
        SELECT entry.value FROM pg_catalog.jsonb_array_elements(p_payload -> 'members') AS entry(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object'
           OR (v_item - ARRAY['ordinal','task_id','write_scope','manifest_member']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_item ?& ARRAY['ordinal','task_id','write_scope','manifest_member'])
           OR pg_catalog.jsonb_typeof(v_item -> 'ordinal') IS DISTINCT FROM 'number'
           OR v_item ->> 'ordinal' !~ '^[1-9][0-9]*$'
           OR pg_catalog.char_length(v_item ->> 'ordinal') > 19
           OR (v_item ->> 'ordinal')::numeric > 9223372036854775807
           OR pg_catalog.jsonb_typeof(v_item -> 'task_id') IS DISTINCT FROM 'string'
           OR v_item ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR (v_item -> 'write_scope' <> 'null'::jsonb
               AND pg_catalog.jsonb_typeof(v_item -> 'write_scope') IS DISTINCT FROM 'object')
           OR (v_item -> 'manifest_member' <> 'null'::jsonb
               AND pg_catalog.jsonb_typeof(v_item -> 'manifest_member') IS DISTINCT FROM 'object') THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
        END IF;
        v_ordinal := (v_item ->> 'ordinal')::bigint;
        v_task := (v_item ->> 'task_id')::uuid;
        IF v_ordinal <> v_after + 1 + v_seen
           OR NOT aap_jobs_v1._batch_result_scope_matches_member(
                v_item -> 'write_scope', v_execution.job_id, v_execution.actor_id,
                v_execution.project_id, v_task
           ) THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
        END IF;
        v_manifest := v_item -> 'manifest_member';
        v_manifest_item := v_item -> 'write_scope';
        IF v_manifest <> 'null'::jsonb THEN
            IF pg_catalog.jsonb_typeof(v_manifest) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_manifest -> 'ordinal') IS DISTINCT FROM 'number'
               OR v_manifest ->> 'ordinal' IS DISTINCT FROM v_ordinal::text
               OR pg_catalog.jsonb_typeof(v_manifest -> 'task_id') IS DISTINCT FROM 'string'
               OR v_manifest ->> 'task_id' IS DISTINCT FROM v_task::text
               OR pg_catalog.jsonb_typeof(v_manifest -> 'project_id') IS DISTINCT FROM 'string'
               OR v_manifest ->> 'project_id' IS DISTINCT FROM v_execution.project_id::text
               OR NOT (v_manifest ? 'input')
               OR pg_catalog.jsonb_typeof(v_manifest -> 'input') NOT IN ('object','null') THEN
                RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
            END IF;
            v_input := v_manifest -> 'input';
            IF v_manifest ->> 'kind' = 'workbench' THEN
                IF (v_manifest - ARRAY['kind','ordinal','task_id','project_id','state','input']) IS DISTINCT FROM '{}'::jsonb
                   OR v_manifest ->> 'kind' IS DISTINCT FROM 'workbench'
                   OR v_manifest ->> 'state' IS DISTINCT FROM 'ready'
                   OR pg_catalog.jsonb_typeof(v_input) IS DISTINCT FROM 'object'
                   OR v_manifest_item = 'null'::jsonb THEN
                    RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
                END IF;
            ELSE
                IF (v_manifest - ARRAY[
                        'manifest_id','selection_digest','sealed_digest','ordinal','task_id','project_id',
                        'task_version','task_source_generation','dataset_item_id','task_source_snapshot',
                        'file_path','file_type','source','state','input','error'
                    ]) IS DISTINCT FROM '{}'::jsonb
                   OR NOT (v_manifest ?& ARRAY[
                        'manifest_id','selection_digest','sealed_digest','ordinal','task_id','project_id',
                        'task_version','task_source_generation','dataset_item_id','task_source_snapshot',
                        'file_path','file_type','source','state','input','error'
                    ])
                   OR pg_catalog.jsonb_typeof(v_manifest -> 'manifest_id') IS DISTINCT FROM 'string'
                   OR v_manifest ->> 'manifest_id' IS DISTINCT FROM v_execution.job_id::text
                   OR pg_catalog.jsonb_typeof(v_manifest -> 'selection_digest') IS DISTINCT FROM 'string'
                   OR v_manifest ->> 'selection_digest' !~ '^[0-9a-f]{64}$'
                   OR pg_catalog.jsonb_typeof(v_manifest -> 'sealed_digest') IS DISTINCT FROM 'string'
                   OR v_manifest ->> 'sealed_digest' !~ '^[0-9a-f]{64}$'
                   OR v_manifest ->> 'state' NOT IN ('ready','failed')
                   OR pg_catalog.jsonb_typeof(v_manifest -> 'task_version') IS DISTINCT FROM 'number'
                   OR v_manifest ->> 'task_version' !~ '^[1-9][0-9]*$'
                   OR pg_catalog.jsonb_typeof(v_manifest -> 'task_source_generation') IS DISTINCT FROM 'number'
                   OR v_manifest ->> 'task_source_generation' !~ '^[1-9][0-9]*$'
                   OR pg_catalog.jsonb_typeof(v_manifest -> 'task_source_snapshot') IS DISTINCT FROM 'string'
                   OR v_manifest ->> 'task_source_snapshot' !~ '^[0-9a-f]{64}$'
                   OR pg_catalog.jsonb_typeof(v_manifest -> 'file_path') IS DISTINCT FROM 'string'
                   OR pg_catalog.octet_length(v_manifest ->> 'file_path') > 1024
                   OR v_manifest ->> 'file_type' IS DISTINCT FROM 'image'
                   OR pg_catalog.jsonb_typeof(v_manifest -> 'source') NOT IN ('object','null')
                   OR pg_catalog.jsonb_typeof(v_manifest -> 'error') NOT IN ('string','null')
                   OR (v_manifest_item <> 'null'::jsonb AND (
                       v_manifest_item -> 'source_binding_known' IS DISTINCT FROM 'true'::jsonb
                       OR v_manifest_item ->> 'task_version' IS DISTINCT FROM v_manifest ->> 'task_version'
                       OR v_manifest_item ->> 'task_source_generation' IS DISTINCT FROM v_manifest ->> 'task_source_generation'
                       OR v_manifest_item -> 'source_dataset_item_id' IS DISTINCT FROM v_manifest -> 'dataset_item_id'
                       OR v_manifest_item ->> 'source_file_path' IS DISTINCT FROM v_manifest ->> 'file_path'
                       OR v_manifest_item ->> 'source_file_type' IS DISTINCT FROM v_manifest ->> 'file_type'
                   ))
                   OR v_job.payload ->> 'accepted_input_manifest_protocol' IS DISTINCT FROM 'aap-assets-input-manifest/v1'
                   OR v_job.payload ->> 'accepted_input_manifest_id' IS DISTINCT FROM v_manifest ->> 'manifest_id'
                   OR v_job.payload ->> 'accepted_input_manifest_digest' IS DISTINCT FROM v_manifest ->> 'selection_digest'
                   OR v_job.payload ->> 'accepted_input_manifest_sealed_digest' IS DISTINCT FROM v_manifest ->> 'sealed_digest' THEN
                    RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
                END IF;
                IF v_manifest ->> 'state' = 'ready'
                   AND (pg_catalog.jsonb_typeof(v_input) IS DISTINCT FROM 'object'
                        OR pg_catalog.jsonb_typeof(v_input -> 'sha256') IS DISTINCT FROM 'string'
                        OR v_input ->> 'sha256' !~ '^[0-9a-f]{64}$'
                        OR pg_catalog.jsonb_typeof(v_input -> 'byte_size') IS DISTINCT FROM 'number'
                        OR v_input ->> 'byte_size' !~ '^(0|[1-9][0-9]*)$'
                        OR v_input ->> 'task_version' IS DISTINCT FROM v_manifest ->> 'task_version'
                        OR v_input ->> 'task_source_generation' IS DISTINCT FROM v_manifest ->> 'task_source_generation'
                        OR v_input -> 'dataset_item_id' IS DISTINCT FROM v_manifest -> 'dataset_item_id'
                        OR v_input ->> 'task_source_snapshot' IS DISTINCT FROM v_manifest ->> 'task_source_snapshot') THEN
                    RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
                END IF;
            END IF;
            IF pg_catalog.jsonb_typeof(v_input) = 'object'
               AND (pg_catalog.jsonb_typeof(v_input -> 'input_id') IS DISTINCT FROM 'string'
                    OR v_input ->> 'input_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                    OR v_input ->> 'task_id' IS DISTINCT FROM v_task::text
                    OR v_input ->> 'project_id' IS DISTINCT FROM v_execution.project_id::text
                    OR (v_manifest_item <> 'null'::jsonb
                        AND v_manifest_item ->> 'accepted_input_id' IS DISTINCT FROM v_input ->> 'input_id')) THEN
                RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
            END IF;
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.batch_result_members AS existing
            WHERE existing.job_id = v_execution.job_id
              AND (existing.ordinal = v_ordinal OR existing.task_id = v_task)
        ) THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
        END IF;
        v_seen := v_seen + 1;
    END LOOP;
    IF v_count > 0 THEN
        INSERT INTO public.batch_result_members(
            job_id, ordinal, task_id, write_scope, manifest_member, failure_id
        )
        SELECT v_execution.job_id,
               (member.value ->> 'ordinal')::bigint,
               (member.value ->> 'task_id')::uuid,
               NULLIF(member.value -> 'write_scope', 'null'::jsonb),
               NULLIF(member.value -> 'manifest_member', 'null'::jsonb),
               pg_catalog.gen_random_uuid()
        FROM pg_catalog.jsonb_array_elements(p_payload -> 'members') AS member(value)
        ORDER BY (member.value ->> 'ordinal')::bigint;
    END IF;
    UPDATE public.batch_result_executions
    SET selection_cursor = selection_cursor + v_count,
        selection_complete = v_complete,
        total = CASE WHEN v_complete THEN selection_cursor + v_count ELSE NULL END,
        updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_execution.job_id
    RETURNING * INTO v_execution;
    v_progress := aap_jobs_v1._batch_result_progress(v_execution.job_id);
    UPDATE public.batch_result_executions
    SET last_page_after_ordinal = v_after,
        last_page_digest = v_digest,
        last_page_complete = v_complete,
        last_page_result = v_progress,
        updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_execution.job_id;
    RETURN aap_jobs_v1._batch_result_reply(pg_catalog.jsonb_build_object('progress', v_progress), NULL);
EXCEPTION WHEN unique_violation THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.stage_batch_result_members(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.start_batch_result_execution(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_job public.async_jobs%ROWTYPE;
BEGIN
    IF NOT aap_jobs_v1._batch_result_scope_valid(p_payload, true) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload, true, true);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_locked; END IF;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = (p_payload ->> 'job_id')::uuid;
    SELECT * INTO v_execution FROM public.batch_result_executions WHERE job_id = v_job.id;
    IF v_execution.selection_complete IS DISTINCT FROM true
       OR v_execution.total IS NULL THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    IF COALESCE(v_job.payload -> 'cancel_requested', 'false'::jsonb) = 'true'::jsonb THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    IF v_execution.state = 'running' AND v_job.status = 'running' THEN
        RETURN aap_jobs_v1._batch_result_reply(
            pg_catalog.jsonb_build_object('progress', aap_jobs_v1._batch_result_progress(v_job.id)), NULL
        );
    END IF;
    IF v_execution.state <> 'preparing' OR v_job.status NOT IN ('pending', 'running') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    UPDATE public.batch_result_executions
    SET state = 'running', updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_job.id;
    UPDATE public.async_jobs
    SET status = 'running',
        started_at = COALESCE(started_at, pg_catalog.clock_timestamp()),
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = v_job.id AND status IN ('pending','running');
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('progress', aap_jobs_v1._batch_result_progress(v_job.id)), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.start_batch_result_execution(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.read_batch_result_execution(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_scope jsonb;
    v_job public.async_jobs%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_after bigint;
    v_limit integer;
    v_members jsonb;
    v_next bigint;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','after_ordinal','limit']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','after_ordinal','limit'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - ARRAY['after_ordinal','limit'], false)
       OR pg_catalog.jsonb_typeof(p_payload -> 'after_ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'after_ordinal' !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_payload ->> 'after_ordinal') > 19
       OR (p_payload ->> 'after_ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'limit') IS DISTINCT FROM 'number'
       OR p_payload ->> 'limit' !~ '^[0-9]+$'
       OR pg_catalog.char_length(p_payload ->> 'limit') > 3
       OR (p_payload ->> 'limit')::numeric > 100 THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_after := (p_payload ->> 'after_ordinal')::bigint;
    v_limit := (p_payload ->> 'limit')::integer;
    IF v_after < 0 OR v_limit < 1 OR v_limit > 100 THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_scope := p_payload - ARRAY['after_ordinal','limit'];
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = (v_scope ->> 'job_id')::uuid;
    IF NOT FOUND THEN RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing'); END IF;
    IF v_execution.actor_id IS DISTINCT FROM (v_scope ->> 'actor_id')::uuid
       OR v_execution.project_id IS DISTINCT FROM (v_scope ->> 'project_id')::uuid
       OR v_execution.request_digest IS DISTINCT FROM v_scope ->> 'request_digest' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
    END IF;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = v_execution.job_id;
    IF FOUND THEN
        IF v_job.kind IS DISTINCT FROM 'batch_predict'
           OR (v_job.user_id IS DISTINCT FROM (v_scope ->> 'actor_id')::uuid AND v_job.user_id IS NOT NULL)
           OR COALESCE(v_job.project_id, v_job.retained_project_id) IS DISTINCT FROM (v_scope ->> 'project_id')::uuid THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
        END IF;
    ELSIF v_execution.state NOT IN ('completed','failed','cancelled') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing');
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(aap_jobs_v1._batch_result_member_json(member) ORDER BY member.ordinal), '[]'::jsonb)
    INTO v_members
    FROM (
        SELECT * FROM public.batch_result_members
        WHERE job_id = v_execution.job_id AND ordinal > v_after
        ORDER BY ordinal LIMIT v_limit
    ) AS member;
    SELECT max(member.ordinal) INTO v_next
    FROM (
        SELECT ordinal FROM public.batch_result_members
        WHERE job_id = v_execution.job_id AND ordinal > v_after
        ORDER BY ordinal LIMIT v_limit
    ) AS member;
    IF v_next IS NOT NULL AND EXISTS (
        SELECT 1 FROM public.batch_result_members
        WHERE job_id = v_execution.job_id AND ordinal > v_next
    ) THEN
        v_next := v_next;
    ELSE
        v_next := NULL;
    END IF;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'progress', aap_jobs_v1._batch_result_progress(v_job.id),
            'members', v_members, 'next_ordinal', v_next
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.read_batch_result_execution(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.mark_batch_result_overwrite(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_member public.batch_result_members%ROWTYPE;
    v_ordinal bigint;
    v_task uuid;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - 'ordinal', true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') NOT IN ('number','null') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload - 'ordinal', true, true);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_locked; END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = (p_payload ->> 'job_id')::uuid;
    IF v_execution.state <> 'running' THEN RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable'); END IF;
    IF v_locked #> '{data,cancel_requested}' IS DISTINCT FROM 'false'::jsonb THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    IF p_payload -> 'ordinal' = 'null'::jsonb THEN
        IF COALESCE(
            (SELECT job.payload ->> 'predict_mode' FROM public.async_jobs AS job WHERE job.id = v_execution.job_id),
            (SELECT job.payload #>> '{input_selection,predict_mode}' FROM public.async_jobs AS job WHERE job.id = v_execution.job_id),
            'append'
        ) <> 'overwrite' THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
        END IF;
        IF COALESCE((SELECT status FROM public.async_jobs WHERE id = v_execution.job_id), '') NOT IN ('pending','running') THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
        END IF;
        IF v_execution.bulk_overwrite_done THEN
            RETURN aap_jobs_v1._batch_result_reply(
                pg_catalog.jsonb_build_object('progress', aap_jobs_v1._batch_result_progress(v_execution.job_id), 'member', NULL), NULL
            );
        END IF;
        UPDATE public.batch_result_executions SET bulk_overwrite_done = true, updated_at = pg_catalog.clock_timestamp()
        WHERE job_id = v_execution.job_id;
        RETURN aap_jobs_v1._batch_result_reply(
            pg_catalog.jsonb_build_object('progress', aap_jobs_v1._batch_result_progress(v_execution.job_id), 'member', NULL), NULL
        );
    END IF;
    IF p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807 THEN RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request'); END IF;
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    SELECT * INTO v_member FROM public.batch_result_members
    WHERE job_id = v_execution.job_id AND ordinal = v_ordinal
    FOR UPDATE;
    IF NOT FOUND OR v_ordinal <> v_execution.next_ordinal THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    IF v_member.overwrite_done THEN
        RETURN aap_jobs_v1._batch_result_reply(
            pg_catalog.jsonb_build_object('progress', aap_jobs_v1._batch_result_progress(v_execution.job_id), 'member', aap_jobs_v1._batch_result_member_json(v_member)), NULL
        );
    END IF;
    IF v_member.state <> 'planned'
       OR COALESCE(
            (SELECT job.payload ->> 'predict_mode' FROM public.async_jobs AS job WHERE job.id = v_execution.job_id),
            (SELECT job.payload #>> '{input_selection,predict_mode}' FROM public.async_jobs AS job WHERE job.id = v_execution.job_id),
            'append'
       ) <> 'overwrite' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    UPDATE public.batch_result_members
    SET overwrite_done = true, updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_member.job_id AND ordinal = v_member.ordinal
    RETURNING * INTO v_member;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('progress', aap_jobs_v1._batch_result_progress(v_execution.job_id), 'member', aap_jobs_v1._batch_result_member_json(v_member)), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.mark_batch_result_overwrite(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.start_batch_result_member(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_member public.batch_result_members%ROWTYPE;
    v_ordinal bigint;
    v_job public.async_jobs%ROWTYPE;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - 'ordinal', true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807 THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload - 'ordinal', true, true);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_locked; END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = (p_payload ->> 'job_id')::uuid;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = v_execution.job_id;
    SELECT * INTO v_member FROM public.batch_result_members
    WHERE job_id = v_execution.job_id AND ordinal = v_ordinal
    FOR UPDATE;
    IF NOT FOUND OR v_ordinal <> v_execution.next_ordinal
       OR v_execution.state <> 'running' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    IF v_member.state IN ('prepared','capturing','captured','ready') THEN
        RETURN aap_jobs_v1._batch_result_reply(
            pg_catalog.jsonb_build_object(
                'progress', aap_jobs_v1._batch_result_progress(v_execution.job_id),
                'member', aap_jobs_v1._batch_result_member_json(v_member)
            ), NULL
        );
    END IF;
    IF v_member.state <> 'planned'
       OR v_member.write_scope IS NULL
       OR COALESCE(v_job.payload -> 'cancel_requested', 'false'::jsonb) = 'true'::jsonb THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    UPDATE public.batch_result_members
    SET state = 'prepared', updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_member.job_id AND ordinal = v_member.ordinal
    RETURNING * INTO v_member;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'progress', aap_jobs_v1._batch_result_progress(v_execution.job_id),
            'member', aap_jobs_v1._batch_result_member_json(v_member)
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.start_batch_result_member(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.skip_batch_result_member(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_member public.batch_result_members%ROWTYPE;
    v_reference jsonb;
    v_ordinal bigint;
    v_reason text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal','reason']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal','reason'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - ARRAY['ordinal','reason'], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'reason') IS DISTINCT FROM 'string'
       OR pg_catalog.octet_length(p_payload ->> 'reason') NOT BETWEEN 1 AND 256 THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_reason := p_payload ->> 'reason';
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload - ARRAY['ordinal','reason'], true, true);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_locked; END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = (p_payload ->> 'job_id')::uuid;
    SELECT * INTO v_member FROM public.batch_result_members
    WHERE job_id = v_execution.job_id AND ordinal = v_ordinal
    FOR UPDATE;
    IF FOUND AND v_member.state = 'skipped' THEN
        IF v_member.error_code IS DISTINCT FROM v_reason THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
        END IF;
        RETURN aap_jobs_v1._batch_result_reply(
            pg_catalog.jsonb_build_object('progress', aap_jobs_v1._batch_result_progress(v_execution.job_id)), NULL
        );
    END IF;
    IF NOT FOUND OR v_ordinal <> v_execution.next_ordinal THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    IF v_member.state NOT IN ('planned','prepared') OR v_member.result_id IS NOT NULL THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'result_conflict');
    END IF;
    v_reference := aap_labeling_v1.batch_result_referenced(
        pg_catalog.jsonb_build_object(
            'job_id', v_execution.job_id, 'ordinal', v_member.ordinal
        )
    );
    IF v_reference -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'result_not_ready');
    END IF;
    IF v_reference #> '{data,referenced}' IS DISTINCT FROM 'false'::jsonb THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'result_conflict');
    END IF;
    UPDATE public.batch_result_members
    SET state = 'skipped', error_code = v_reason, updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_member.job_id AND ordinal = v_member.ordinal;
    UPDATE public.batch_result_executions
    SET next_ordinal = next_ordinal + 1,
        skipped_count = skipped_count + 1,
        updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_execution.job_id
    RETURNING * INTO v_execution;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('progress', aap_jobs_v1._batch_result_progress(v_execution.job_id)), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.skip_batch_result_member(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.lock_batch_result_execution(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_member public.batch_result_members%ROWTYPE;
    v_ordinal bigint;
    v_task uuid;
    v_cancel boolean;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal','task_id']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal','task_id'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - ARRAY['ordinal','task_id'], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'task_id') NOT IN ('string','null')
       OR (p_payload -> 'task_id' <> 'null'::jsonb
           AND p_payload ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_task := (p_payload ->> 'task_id')::uuid;
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload - ARRAY['ordinal','task_id'], true, true);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_locked; END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = (p_payload ->> 'job_id')::uuid;
    SELECT * INTO v_member FROM public.batch_result_members
    WHERE job_id = v_execution.job_id AND ordinal = v_ordinal
    FOR UPDATE;
    IF NOT FOUND OR v_ordinal <> v_execution.next_ordinal
       OR (v_task IS NOT NULL AND v_member.task_id IS DISTINCT FROM v_task) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    v_cancel := COALESCE((v_locked #>> '{data,cancel_requested}')::boolean, false);
    IF v_member.state = 'planned' AND v_cancel THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'progress', aap_jobs_v1._batch_result_progress(v_execution.job_id),
            'member', aap_jobs_v1._batch_result_member_json(v_member)
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.lock_batch_result_execution(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.record_batch_result_member(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_member public.batch_result_members%ROWTYPE;
    v_ordinal bigint;
    v_task uuid;
    v_result uuid;
    v_state text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal','task_id','result_id','state']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal','task_id','result_id','state'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - ARRAY['ordinal','task_id','result_id','state'], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'task_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'result_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'result_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'state') IS DISTINCT FROM 'string'
       OR p_payload ->> 'state' NOT IN ('capturing','captured','ready') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_task := (p_payload ->> 'task_id')::uuid;
    v_result := (p_payload ->> 'result_id')::uuid;
    v_state := p_payload ->> 'state';
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload - ARRAY['ordinal','task_id','result_id','state'], true, true);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_locked; END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions WHERE job_id = (p_payload ->> 'job_id')::uuid;
    SELECT * INTO v_member FROM public.batch_result_members
    WHERE job_id = v_execution.job_id AND ordinal = v_ordinal FOR UPDATE;
    IF NOT FOUND OR v_ordinal <> v_execution.next_ordinal
       OR v_member.task_id IS DISTINCT FROM v_task
       OR v_member.state NOT IN ('prepared','capturing','captured','ready') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    IF v_member.result_id IS NOT NULL AND v_member.result_id IS DISTINCT FROM v_result THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'result_conflict');
    END IF;
    IF NOT (
        (v_member.state = 'prepared' AND v_state = 'capturing')
        OR (v_member.state = 'capturing' AND v_state IN ('capturing','captured'))
        OR (v_member.state = 'captured' AND v_state IN ('captured','ready'))
        OR (v_member.state = 'ready' AND v_state = 'ready')
    ) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'result_conflict');
    END IF;
    UPDATE public.batch_result_members
    SET result_id = v_result, state = v_state, updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_member.job_id AND ordinal = v_member.ordinal
    RETURNING * INTO v_member;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'progress', aap_jobs_v1._batch_result_progress(v_execution.job_id),
            'member', aap_jobs_v1._batch_result_member_json(v_member)
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.record_batch_result_member(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._batch_result_merge_stage_counts(p_old jsonb, p_add jsonb)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_result jsonb := COALESCE(p_old, '{}'::jsonb);
    v_stage record;
    v_count record;
    v_value numeric;
    v_prior numeric;
BEGIN
    IF pg_catalog.jsonb_typeof(v_result) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_add) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid stage counters';
    END IF;
    FOR v_stage IN SELECT key, value FROM pg_catalog.jsonb_each(p_add)
    LOOP
        IF v_stage.key !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.jsonb_typeof(v_stage.value) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid stage counters';
        END IF;
        FOR v_count IN SELECT key, value FROM pg_catalog.jsonb_each(v_stage.value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_count.value) IS DISTINCT FROM 'number'
               OR v_count.value #>> '{}' !~ '^(0|[1-9][0-9]*)$' THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid stage counters';
            END IF;
            v_value := (v_count.value #>> '{}')::numeric;
            IF v_value > 9223372036854775807 THEN
                RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'stage counter overflow';
            END IF;
            IF pg_catalog.jsonb_typeof(v_result -> v_stage.key) = 'object'
               AND pg_catalog.jsonb_typeof(v_result -> v_stage.key -> v_count.key) = 'number'
               AND (v_result -> v_stage.key ->> v_count.key) ~ '^(0|[1-9][0-9]*)$' THEN
                v_prior := (v_result -> v_stage.key ->> v_count.key)::numeric;
            ELSE
                v_prior := 0;
            END IF;
            IF v_prior + v_value > 9223372036854775807 THEN
                RAISE EXCEPTION USING ERRCODE = '22003', MESSAGE = 'stage counter overflow';
            END IF;
            v_result := v_result || pg_catalog.jsonb_build_object(
                v_stage.key,
                COALESCE(v_result -> v_stage.key, '{}'::jsonb)
                    || pg_catalog.jsonb_build_object(v_count.key, (v_prior + v_value)::bigint)
            );
        END LOOP;
    END LOOP;
    RETURN v_result;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._batch_result_merge_stage_counts(jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._batch_result_merge_gpu_failures(p_old jsonb, p_add jsonb)
RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_map jsonb := '{}'::jsonb;
    v_item jsonb;
    v_code text;
    v_count bigint;
    v_retry bigint;
    v_prior jsonb;
    v_status jsonb;
    v_existing_retry jsonb;
    v_item_count bigint;
    v_item_retry bigint;
    v_array jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(COALESCE(p_old,'[]'::jsonb)) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_add) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='invalid GPU failure summary';
    END IF;
    FOREACH v_array SLICE 0 IN ARRAY ARRAY[COALESCE(p_old,'[]'::jsonb),p_add]
    LOOP
        FOR v_item IN SELECT entry.value FROM pg_catalog.jsonb_array_elements(v_array) AS entry(value)
        LOOP
            v_code := v_item ->> 'error_code';
            IF v_code IS NULL OR v_code = '' THEN CONTINUE; END IF;
            IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_item -> 'error_code') IS DISTINCT FROM 'string' THEN
                CONTINUE;
            END IF;
            IF v_code NOT IN (
                'gpu_arbiter_not_ready', 'gpu_capacity_unavailable',
                'gpu_backend_concurrency_saturated', 'gpu_drain_timeout',
                'gpu_arbiter_unavailable', 'gpu_config_invalid',
                'gpu_backend_retirement_required'
            ) THEN
                RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='unknown GPU arbitration error code';
            END IF;
            v_item_count := CASE
                WHEN pg_catalog.jsonb_typeof(v_item -> 'count')='number'
                     AND (v_item ->> 'count') ~ '^[1-9][0-9]*$'
                     AND pg_catalog.char_length(v_item ->> 'count') < 19
                    THEN (v_item ->> 'count')::bigint
                ELSE 1 END;
            v_item_retry := CASE
                WHEN pg_catalog.jsonb_typeof(v_item -> 'retry_after_s')='number'
                     AND (v_item ->> 'retry_after_s') ~ '^(0|[1-9][0-9]*)$'
                     AND pg_catalog.char_length(v_item ->> 'retry_after_s') < 19
                    THEN (v_item ->> 'retry_after_s')::bigint
                ELSE NULL END;
            v_prior := v_map -> v_code;
            IF v_prior IS NULL THEN
                v_status := CASE
                    WHEN pg_catalog.jsonb_typeof(v_item -> 'status_code') IN ('number','null')
                        THEN v_item -> 'status_code' ELSE 'null'::jsonb END;
                v_retry := v_item_retry;
                v_count := v_item_count;
            ELSE
                v_status := v_prior -> 'status_code';
                v_count := (v_prior ->> 'count')::bigint + v_item_count;
                v_existing_retry := v_prior -> 'retry_after_s';
                v_retry := CASE
                    WHEN v_item_retry IS NULL THEN
                        CASE WHEN v_existing_retry IS NULL OR v_existing_retry='null'::jsonb
                            THEN NULL ELSE (v_existing_retry #>> '{}')::bigint END
                    WHEN v_existing_retry IS NULL OR v_existing_retry='null'::jsonb
                        THEN v_item_retry
                    ELSE GREATEST((v_existing_retry #>> '{}')::bigint,v_item_retry)
                END;
            END IF;
            v_map := v_map || pg_catalog.jsonb_build_object(
                v_code,
                pg_catalog.jsonb_build_object(
                    'error_code',v_code,
                    'status_code',v_status,
                    'retry_after_s',v_retry,
                    'count',v_count
                )
            );
        END LOOP;
    END LOOP;
    SELECT COALESCE(pg_catalog.jsonb_agg(entry.value ORDER BY entry.key), '[]'::jsonb)
    INTO v_array
    FROM pg_catalog.jsonb_each(v_map) AS entry;
    RETURN v_array;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._batch_result_merge_gpu_failures(jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.advance_batch_result_member(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_member public.batch_result_members%ROWTYPE;
    v_ordinal bigint;
    v_task uuid;
    v_result uuid;
    v_failed_prediction uuid;
    v_error text;
    v_outcome text;
    v_context jsonb;
    v_cost numeric;
    v_stages jsonb;
    v_gpu jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','task_id','result_id','failed_prediction_id','error_code','outcome','context'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','task_id','result_id','failed_prediction_id','error_code','outcome','context'
       ])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - ARRAY['ordinal','task_id','result_id','failed_prediction_id','error_code','outcome','context'], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'task_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'result_id') NOT IN ('string','null')
       OR (p_payload -> 'result_id' <> 'null'::jsonb
           AND p_payload ->> 'result_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR pg_catalog.jsonb_typeof(p_payload -> 'failed_prediction_id') NOT IN ('string','null')
       OR (p_payload -> 'failed_prediction_id' <> 'null'::jsonb
           AND p_payload ->> 'failed_prediction_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR pg_catalog.jsonb_typeof(p_payload -> 'error_code') NOT IN ('string','null')
       OR pg_catalog.jsonb_typeof(p_payload -> 'outcome') IS DISTINCT FROM 'string'
       OR p_payload ->> 'outcome' NOT IN ('applied','failed')
       OR pg_catalog.jsonb_typeof(p_payload -> 'context') IS DISTINCT FROM 'object' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_task := (p_payload ->> 'task_id')::uuid;
    v_result := CASE WHEN p_payload -> 'result_id'='null'::jsonb THEN NULL ELSE (p_payload ->> 'result_id')::uuid END;
    v_failed_prediction := CASE WHEN p_payload -> 'failed_prediction_id'='null'::jsonb THEN NULL ELSE (p_payload ->> 'failed_prediction_id')::uuid END;
    v_error := CASE WHEN p_payload -> 'error_code'='null'::jsonb THEN NULL ELSE p_payload ->> 'error_code' END;
    v_outcome := p_payload ->> 'outcome';
    v_context := p_payload -> 'context';
    IF (v_outcome = 'applied' AND (v_result IS NULL OR v_failed_prediction IS NOT NULL OR v_error IS NOT NULL))
       OR (v_outcome = 'failed' AND v_error IS NULL) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload - ARRAY['ordinal','task_id','result_id','failed_prediction_id','error_code','outcome','context'], true, true);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_locked; END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = (p_payload ->> 'job_id')::uuid;
    SELECT * INTO v_member FROM public.batch_result_members
    WHERE job_id = v_execution.job_id AND ordinal = v_ordinal FOR UPDATE;
    IF NOT FOUND OR v_ordinal <> v_execution.next_ordinal
       OR v_member.task_id IS DISTINCT FROM v_task
       OR v_member.result_id IS DISTINCT FROM v_result THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'selection_conflict');
    END IF;
    IF v_outcome = 'applied' AND v_member.state <> 'ready' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'result_not_ready');
    END IF;
    IF v_outcome = 'failed' AND v_member.state NOT IN ('planned','prepared','capturing','captured','ready') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'result_conflict');
    END IF;
    IF v_outcome = 'failed' AND v_member.state = 'planned'
       AND (v_failed_prediction IS NOT NULL OR v_error NOT IN ('task_missing','scope_abandoned')) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'result_conflict');
    END IF;
    v_stages := COALESCE(v_context -> 'pipeline_stages', '{}'::jsonb);
    v_gpu := COALESCE(v_context -> 'gpu_arbiter_failures', '[]'::jsonb);
    IF pg_catalog.jsonb_typeof(v_stages) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(v_gpu) IS DISTINCT FROM 'array' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    IF v_context ? 'total_cost' THEN
        IF pg_catalog.jsonb_typeof(v_context -> 'total_cost') IS DISTINCT FROM 'string'
           OR v_context ->> 'total_cost' !~ '^-?(0|[1-9][0-9]*)([.][0-9]+)?$' THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
        END IF;
        v_cost := (v_context ->> 'total_cost')::numeric;
    ELSE
        v_cost := 0;
    END IF;
    UPDATE public.batch_result_members
    SET state = CASE WHEN v_outcome = 'applied' THEN 'applied' ELSE 'failed' END,
        failed_prediction_id = CASE WHEN v_outcome='failed' THEN v_failed_prediction ELSE NULL END,
        error_code = CASE WHEN v_outcome='failed' THEN v_error ELSE NULL END,
        updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_member.job_id AND ordinal = v_member.ordinal;
    UPDATE public.batch_result_executions
    SET next_ordinal = next_ordinal + 1,
        success_count = success_count + CASE WHEN v_outcome = 'applied' THEN 1 ELSE 0 END,
        failed_count = failed_count + CASE WHEN v_outcome = 'failed' THEN 1 ELSE 0 END,
        total_cost = (total_cost::numeric + v_cost)::text,
        pipeline_stages = aap_jobs_v1._batch_result_merge_stage_counts(pipeline_stages, v_stages),
        gpu_arbiter_failures = aap_jobs_v1._batch_result_merge_gpu_failures(gpu_arbiter_failures, v_gpu),
        updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_execution.job_id
    RETURNING * INTO v_execution;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('progress', aap_jobs_v1._batch_result_progress(v_execution.job_id)), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.advance_batch_result_member(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.batch_result_execution_referenced(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_scope jsonb;
    v_active boolean := false;
BEGIN
    IF NOT aap_jobs_v1._batch_result_scope_valid(p_payload, false) THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = (p_payload ->> 'job_id')::uuid;
    IF NOT FOUND THEN
        SELECT * INTO v_execution FROM public.batch_result_executions
        WHERE job_id = (p_payload ->> 'job_id')::uuid;
        IF NOT FOUND THEN
            RETURN aap_jobs_v1._batch_result_reply(
                pg_catalog.jsonb_build_object(
                    'job_id', p_payload ->> 'job_id', 'referenced', false,
                    'terminal', false, 'job_status', NULL
                ), NULL
            );
        END IF;
        IF v_execution.actor_id IS DISTINCT FROM (p_payload ->> 'actor_id')::uuid
           OR v_execution.project_id IS DISTINCT FROM (p_payload ->> 'project_id')::uuid
           OR v_execution.request_digest IS DISTINCT FROM p_payload ->> 'request_digest' THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
        END IF;
        IF v_execution.state NOT IN ('completed','failed','cancelled') THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_missing');
        END IF;
        RETURN aap_jobs_v1._batch_result_reply(
            pg_catalog.jsonb_build_object(
                'job_id', v_execution.job_id, 'referenced', false,
                'terminal', true, 'job_status', v_execution.state
            ), NULL
        );
    END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions WHERE job_id = v_job.id;
    IF NOT FOUND THEN
        RETURN aap_jobs_v1._batch_result_reply(
            pg_catalog.jsonb_build_object(
                'job_id', v_job.id, 'referenced', false,
                'terminal', v_job.status IN ('completed','failed','cancelled'),
                'job_status', v_job.status
            ), NULL
        );
    END IF;
    IF v_execution.actor_id IS DISTINCT FROM (p_payload ->> 'actor_id')::uuid
       OR v_execution.project_id IS DISTINCT FROM (p_payload ->> 'project_id')::uuid
       OR v_execution.request_digest IS DISTINCT FROM p_payload ->> 'request_digest' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'scope_mismatch');
    END IF;
    v_active := v_job.status IN ('pending','running')
        AND v_execution.state IN ('preparing','running');
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'job_id', v_job.id, 'referenced', v_active,
            'terminal', v_job.status IN ('completed','failed','cancelled'),
            'job_status', v_job.status
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.batch_result_execution_referenced(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.retire_batch_results(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_status jsonb;
    v_job_id uuid;
    v_retired bigint := 0;
BEGIN
    IF NOT aap_labeling_v1._batch_result_scope_valid(p_payload, false) THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_job_id := (p_payload ->> 'job_id')::uuid;
    v_status := aap_jobs_v1.batch_result_execution_referenced(p_payload);
    IF v_status -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, COALESCE(v_status #>> '{error,code}', 'scope_mismatch'));
    END IF;
    IF v_status #> '{data,terminal}' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    WITH retired AS (
        UPDATE public.batch_result_bundles AS bundle
        SET state='abandoned', updated_at=pg_catalog.clock_timestamp()
        WHERE bundle.job_id=v_job_id
          AND bundle.state IN ('capturing','captured','ready')
          AND bundle.receipt_count=0
        RETURNING bundle.result_id,bundle.ordinal
    ), payload_rows AS (
        SELECT payload.payload_id
        FROM public.batch_result_payloads AS payload
        JOIN retired USING (result_id)
    ), deleted_parts AS (
        DELETE FROM public.batch_result_parts AS part
        USING payload_rows AS payload
        WHERE part.payload_id=payload.payload_id
        RETURNING part.payload_id
    ), deleted_payloads AS (
        DELETE FROM public.batch_result_payloads AS payload
        USING retired
        WHERE payload.result_id=retired.result_id
        RETURNING payload.payload_id
    ), marked_members AS (
        UPDATE public.batch_result_members AS member
        SET error_code='job_terminal', updated_at=pg_catalog.clock_timestamp()
        FROM retired
        WHERE member.job_id=v_job_id AND member.ordinal=retired.ordinal
          AND member.state IN ('planned','prepared','capturing','captured','ready')
        RETURNING member.ordinal
    )
    SELECT count(*)::bigint INTO v_retired FROM retired;
    RETURN aap_labeling_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('job_id',v_job_id,'retired_count',v_retired), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.retire_batch_results(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._batch_result_terminal_job_trigger()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_execution public.batch_result_executions%ROWTYPE;
    v_reply jsonb;
BEGIN
    IF NEW.kind IS DISTINCT FROM 'batch_predict'
       OR NEW.status NOT IN ('completed','failed','cancelled')
       OR OLD.status IS NOT DISTINCT FROM NEW.status THEN
        RETURN NEW;
    END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id=NEW.id FOR UPDATE;
    IF NOT FOUND THEN RETURN NEW; END IF;
    UPDATE public.batch_result_executions
    SET state=NEW.status, worker_id=NULL, lease_token=NULL,
        lease_expires_at=NULL, error_code=COALESCE(error_code, NEW.error_message),
        updated_at=pg_catalog.clock_timestamp()
    WHERE job_id=NEW.id;
    v_reply := aap_labeling_v1.retire_batch_results(
        pg_catalog.jsonb_build_object(
            'job_id',v_execution.job_id,'actor_id',v_execution.actor_id,
            'project_id',v_execution.project_id,'request_digest',v_execution.request_digest
        )
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE=COALESCE(v_reply #>> '{error,code}','invalid_response');
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._batch_result_terminal_job_trigger() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_async_jobs_batch_result_terminal
AFTER UPDATE OF status ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1._batch_result_terminal_job_trigger();
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1._batch_result_job_delete_guard()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_execution public.batch_result_executions%ROWTYPE;
BEGIN
    IF OLD.kind IS DISTINCT FROM 'batch_predict' THEN RETURN OLD; END IF;
    SELECT * INTO v_execution FROM public.batch_result_executions
    WHERE job_id = OLD.id FOR UPDATE;
    IF NOT FOUND THEN RETURN OLD; END IF;
    IF OLD.status NOT IN ('completed','failed','cancelled')
       OR v_execution.state NOT IN ('completed','failed','cancelled')
       OR v_execution.worker_id IS NOT NULL
       OR v_execution.lease_token IS NOT NULL
       OR v_execution.lease_expires_at IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='job_not_writable';
    END IF;
    RETURN OLD;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1._batch_result_job_delete_guard() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_async_jobs_batch_result_delete_guard
BEFORE DELETE ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1._batch_result_job_delete_guard();
-- aap-statement-break

CREATE FUNCTION aap_jobs_v1.finish_batch_result_execution(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_locked jsonb;
    v_execution public.batch_result_executions%ROWTYPE;
    v_job public.async_jobs%ROWTYPE;
    v_status text;
    v_error text;
    v_extra jsonb;
    v_terminal_count bigint;
    v_member_count bigint;
    v_result jsonb;
    v_failed_ids jsonb;
    v_duration_ms bigint;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','status','error_code','result_extra']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','status','error_code','result_extra'])
       OR NOT aap_jobs_v1._batch_result_scope_valid(p_payload - ARRAY['status','error_code','result_extra'], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'status') IS DISTINCT FROM 'string'
       OR p_payload ->> 'status' NOT IN ('completed','failed','cancelled')
       OR pg_catalog.jsonb_typeof(p_payload -> 'error_code') NOT IN ('string','null')
       OR pg_catalog.jsonb_typeof(p_payload -> 'result_extra') IS DISTINCT FROM 'object' THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_status := p_payload ->> 'status';
    v_error := CASE WHEN p_payload -> 'error_code' = 'null'::jsonb THEN NULL ELSE p_payload ->> 'error_code' END;
    v_extra := p_payload -> 'result_extra';
    IF v_extra ?| ARRAY['raw_result','results','predictions','write_requests','output'] THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_path_exists(v_extra, '$.**.raw_result')
       OR pg_catalog.jsonb_path_exists(v_extra, '$.**.results')
       OR pg_catalog.jsonb_path_exists(v_extra, '$.**.predictions')
       OR pg_catalog.jsonb_path_exists(v_extra, '$.**.write_requests')
       OR pg_catalog.jsonb_path_exists(v_extra, '$.**.output') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_locked := aap_jobs_v1._lock_batch_result_scope(p_payload - ARRAY['status','error_code','result_extra'], true, true);
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_locked; END IF;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = (p_payload ->> 'job_id')::uuid;
    SELECT * INTO v_execution FROM public.batch_result_executions WHERE job_id = v_job.id;
    IF v_execution.state NOT IN ('preparing','running') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'result_not_ready');
    END IF;
    IF v_status = 'completed' THEN
        IF NOT v_execution.selection_complete
           OR v_execution.total IS NULL
           OR v_execution.next_ordinal <= v_execution.total THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'result_not_ready');
        END IF;
        SELECT count(*) FILTER (WHERE member.state IN ('applied','failed','skipped'))::bigint,
               count(*)::bigint
        INTO v_terminal_count, v_member_count
        FROM public.batch_result_members AS member
        WHERE member.job_id = v_job.id;
        IF v_member_count <> v_execution.total OR v_terminal_count <> v_execution.total THEN
            RETURN aap_jobs_v1._batch_result_reply(NULL, 'result_not_ready');
        END IF;
    END IF;
    IF v_job.status NOT IN ('pending','running') THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    IF v_status = 'completed'
       AND COALESCE(v_job.payload -> 'cancel_requested', 'false'::jsonb) = 'true'::jsonb THEN
        RETURN aap_jobs_v1._batch_result_reply(NULL, 'job_not_writable');
    END IF;
    SELECT COALESCE(
        pg_catalog.jsonb_agg(member.failed_prediction_id ORDER BY member.ordinal),
        '[]'::jsonb
    ) INTO v_failed_ids
    FROM public.batch_result_members AS member
    WHERE member.job_id = v_job.id AND member.failed_prediction_id IS NOT NULL;
    v_duration_ms := GREATEST(
        0,
        pg_catalog.floor(
            EXTRACT(epoch FROM (
                pg_catalog.clock_timestamp() - COALESCE(v_job.started_at, v_job.created_at)
            )) * 1000
        )::bigint
    );
    v_result := COALESCE(v_job.result, '{}'::jsonb) || v_extra || pg_catalog.jsonb_build_object(
        'success_count', v_execution.success_count,
        'failed_count', v_execution.failed_count,
        'skipped_count', v_execution.skipped_count,
        'failed_prediction_ids', v_failed_ids,
        'duration_ms', v_duration_ms,
        'total_cost', COALESCE(
            v_extra -> 'total_cost', v_job.result -> 'total_cost',
            pg_catalog.to_jsonb(v_execution.total_cost)
        ),
        'pipeline_stages', COALESCE(
            v_extra -> 'pipeline_stages', v_job.result -> 'pipeline_stages',
            v_execution.pipeline_stages
        ),
        'gpu_arbiter_failures', COALESCE(
            v_extra -> 'gpu_arbiter_failures', v_job.result -> 'gpu_arbiter_failures',
            v_execution.gpu_arbiter_failures
        )
    );
    UPDATE public.batch_result_executions
    SET state = v_status, error_code = v_error,
        worker_id = NULL, lease_token = NULL, lease_expires_at = NULL,
        result_extra = v_extra, updated_at = pg_catalog.clock_timestamp()
    WHERE job_id = v_job.id;
    UPDATE public.async_jobs
    SET status = v_status,
        result = v_result,
        error_message = COALESCE(v_error, error_message),
        progress_pct = 100,
        completed_at = pg_catalog.clock_timestamp(),
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = v_job.id;
    RETURN aap_jobs_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('progress', aap_jobs_v1._batch_result_progress(v_job.id)), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_jobs_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.finish_batch_result_execution(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.batch_result_referenced(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_referenced boolean := false;
    v_job_id uuid;
    v_ordinal bigint;
    v_id uuid;
BEGIN
    IF p_payload IS NULL OR pg_catalog.octet_length(p_payload::text) > 2097152
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    IF p_payload ? 'job_id' OR p_payload ? 'ordinal' THEN
        IF (p_payload - ARRAY['job_id','ordinal']) IS DISTINCT FROM '{}'::jsonb
           OR NOT (p_payload ?& ARRAY['job_id','ordinal'])
           OR pg_catalog.jsonb_typeof(p_payload -> 'job_id') IS DISTINCT FROM 'string'
           OR p_payload ->> 'job_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
           OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807 THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
        END IF;
        v_job_id := (p_payload ->> 'job_id')::uuid;
        v_ordinal := (p_payload ->> 'ordinal')::bigint;
        SELECT EXISTS (
            SELECT 1 FROM public.batch_result_bundles AS bundle
            WHERE bundle.job_id = v_job_id AND bundle.ordinal = v_ordinal
              AND bundle.state <> 'abandoned'
        ) INTO v_referenced;
        RETURN aap_labeling_v1._batch_result_reply(
            pg_catalog.jsonb_build_object('referenced', v_referenced), NULL
        );
    END IF;
    IF p_payload ? 'result_id' THEN
        IF (p_payload - 'result_id') IS DISTINCT FROM '{}'::jsonb
           OR pg_catalog.jsonb_typeof(p_payload -> 'result_id') IS DISTINCT FROM 'string'
           OR p_payload ->> 'result_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
        END IF;
        v_id := (p_payload ->> 'result_id')::uuid;
        SELECT EXISTS (
            SELECT 1 FROM public.batch_result_bundles AS bundle
            WHERE bundle.result_id = v_id AND bundle.state <> 'abandoned'
        ) INTO v_referenced;
    ELSIF p_payload ? 'payload_id' THEN
        IF (p_payload - 'payload_id') IS DISTINCT FROM '{}'::jsonb
           OR pg_catalog.jsonb_typeof(p_payload -> 'payload_id') IS DISTINCT FROM 'string'
           OR p_payload ->> 'payload_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
        END IF;
        v_id := (p_payload ->> 'payload_id')::uuid;
        SELECT EXISTS (
            SELECT 1 FROM public.batch_result_payloads AS payload
            JOIN public.batch_result_bundles AS bundle USING (result_id)
            WHERE payload.payload_id = v_id AND bundle.state <> 'abandoned'
        ) INTO v_referenced;
    ELSE
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    RETURN aap_labeling_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('referenced', v_referenced), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.batch_result_referenced(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.batch_result_commit_proof(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_bundle public.batch_result_bundles%ROWTYPE;
    v_result_id uuid;
    v_payload_id uuid;
    v_applied boolean := false;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR ((p_payload ? 'result_id') = (p_payload ? 'payload_id'))
       OR (p_payload - ARRAY['result_id','payload_id']) IS DISTINCT FROM '{}'::jsonb
       OR (p_payload ? 'result_id'
           AND (pg_catalog.jsonb_typeof(p_payload -> 'result_id') IS DISTINCT FROM 'string'
                OR p_payload ->> 'result_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'))
       OR (p_payload ? 'payload_id'
           AND (pg_catalog.jsonb_typeof(p_payload -> 'payload_id') IS DISTINCT FROM 'string'
                OR p_payload ->> 'payload_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')) THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    IF p_payload ? 'result_id' THEN
        v_result_id := (p_payload ->> 'result_id')::uuid;
        SELECT * INTO v_bundle FROM public.batch_result_bundles WHERE result_id = v_result_id;
        v_applied := FOUND AND v_bundle.state='applied'
            AND v_bundle.receipt_count=v_bundle.result_count;
        RETURN aap_labeling_v1._batch_result_reply(
            pg_catalog.jsonb_build_object('result_id',v_result_id,'applied',v_applied), NULL
        );
    END IF;
    v_payload_id := (p_payload ->> 'payload_id')::uuid;
    SELECT payload.result_id INTO v_result_id
    FROM public.batch_result_payloads AS payload
    WHERE payload.payload_id=v_payload_id AND payload.stage='prepared'
      AND payload.receipt IS NOT NULL;
    IF FOUND THEN
        SELECT * INTO v_bundle FROM public.batch_result_bundles WHERE result_id=v_result_id;
        v_applied := v_bundle.state='applied' AND v_bundle.receipt_count=v_bundle.result_count;
    END IF;
    RETURN aap_labeling_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('payload_id',v_payload_id,'applied',v_applied), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.batch_result_commit_proof(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._batch_result_bundle_json(p_bundle public.batch_result_bundles)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'result_id', p_bundle.result_id,
        'job_id', p_bundle.job_id,
        'ordinal', p_bundle.ordinal,
        'task_id', p_bundle.task_id,
        'result_count', p_bundle.result_count,
        'raw_digest', p_bundle.raw_digest,
        'prepared_digest', p_bundle.prepared_digest,
        'raw_complete', p_bundle.raw_complete,
        'prepared_complete', p_bundle.prepared_complete,
        'state', p_bundle.state,
        'context', p_bundle.context,
        'receipt_count', p_bundle.receipt_count
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._batch_result_bundle_json(public.batch_result_bundles) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._batch_result_payload_json(p_payload public.batch_result_payloads)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'payload_id', p_payload.payload_id,
        'request_index', p_payload.request_index,
        'prediction_id', p_payload.prediction_id,
        'prediction_created_at', p_payload.prediction_created_at,
        'stage', p_payload.stage,
        'sha256', p_payload.sha256,
        'byte_size', p_payload.byte_size,
        'part_count', p_payload.part_count,
        'complete', p_payload.complete,
        'tool_unit_id', p_payload.tool_unit_id,
        'receipt', p_payload.receipt
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._batch_result_payload_json(public.batch_result_payloads) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._batch_result_assembled_bytes(p_payload_id uuid)
RETURNS bytea
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_payload public.batch_result_payloads%ROWTYPE;
    v_bytes bytea;
    v_count bigint;
    v_min bigint;
    v_max bigint;
BEGIN
    SELECT * INTO v_payload FROM public.batch_result_payloads
    WHERE payload_id = p_payload_id;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'result_incomplete'; END IF;
    SELECT count(*)::bigint, min(part_index), max(part_index),
           COALESCE(pg_catalog.string_agg(content, ''::bytea ORDER BY part_index), ''::bytea)
    INTO v_count, v_min, v_max, v_bytes
    FROM public.batch_result_parts WHERE payload_id = p_payload_id;
    IF v_count <> v_payload.part_count OR v_min <> 0 OR v_max <> v_payload.part_count - 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'result_incomplete';
    END IF;
    IF pg_catalog.octet_length(v_bytes) <> v_payload.byte_size
       OR pg_catalog.encode(public.digest(v_bytes, 'sha256'), 'hex') IS DISTINCT FROM v_payload.sha256 THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'result_incomplete';
    END IF;
    RETURN v_bytes;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._batch_result_assembled_bytes(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._batch_result_bundle_digest(
    p_job_id uuid,
    p_ordinal bigint,
    p_result_count bigint,
    p_request_digest text,
    p_stage text,
    p_result_id uuid
)
RETURNS text
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_hash bytea;
    v_payload record;
    v_count bigint := 0;
BEGIN
    v_hash := public.digest(
        pg_catalog.convert_to('aap-preannotation-results/v1', 'UTF8')
        || pg_catalog.decode('00', 'hex')
        || pg_catalog.convert_to(p_stage, 'UTF8')
        || pg_catalog.decode('00', 'hex')
        || pg_catalog.uuid_send(p_job_id)
        || pg_catalog.int8send(p_ordinal::bigint)
        || pg_catalog.int8send(p_result_count::bigint)
        || pg_catalog.decode(p_request_digest, 'hex'),
        'sha256'
    );
    FOR v_payload IN
        SELECT request_index, sha256
        FROM public.batch_result_payloads
        WHERE result_id = p_result_id AND stage = p_stage
        ORDER BY request_index
    LOOP
        IF v_payload.request_index <> v_count THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'result_incomplete';
        END IF;
        v_hash := public.digest(
            v_hash || pg_catalog.int8send(v_payload.request_index::bigint)
                || pg_catalog.decode(v_payload.sha256, 'hex'),
            'sha256'
        );
        v_count := v_count + 1;
    END LOOP;
    IF v_count <> p_result_count THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'result_incomplete';
    END IF;
    RETURN pg_catalog.encode(v_hash, 'hex');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._batch_result_bundle_digest(uuid, bigint, bigint, text, text, uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.begin_batch_result(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_scope jsonb;
    v_locked jsonb;
    v_member jsonb;
    v_bundle public.batch_result_bundles%ROWTYPE;
    v_result_id uuid;
    v_task uuid;
    v_ordinal bigint;
    v_count bigint;
    v_stage text;
    v_restart boolean := false;
    v_digest text;
    v_context jsonb;
    v_record jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','task_id','result_count','bundle_digest','stage','context','restart_incomplete'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','task_id','result_count','bundle_digest','context'
       ])
       OR NOT aap_labeling_v1._batch_result_scope_valid(p_payload - ARRAY[
            'ordinal','task_id','result_count','bundle_digest','stage','context','restart_incomplete'
       ], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'task_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'task_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'result_count') IS DISTINCT FROM 'number'
       OR p_payload ->> 'result_count' !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_payload ->> 'result_count') > 19
       OR (p_payload ->> 'result_count')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'bundle_digest') IS DISTINCT FROM 'string'
       OR p_payload ->> 'bundle_digest' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'context') IS DISTINCT FROM 'object'
       OR (p_payload ? 'stage' AND pg_catalog.jsonb_typeof(p_payload -> 'stage') IS DISTINCT FROM 'string')
       OR (p_payload ? 'restart_incomplete' AND pg_catalog.jsonb_typeof(p_payload -> 'restart_incomplete') IS DISTINCT FROM 'boolean') THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_path_exists(p_payload -> 'context', '$.**.raw_result')
       OR pg_catalog.jsonb_path_exists(p_payload -> 'context', '$.**.results')
       OR pg_catalog.jsonb_path_exists(p_payload -> 'context', '$.**.predictions')
       OR pg_catalog.jsonb_path_exists(p_payload -> 'context', '$.**.write_requests')
       OR pg_catalog.jsonb_path_exists(p_payload -> 'context', '$.**.output') THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_scope := p_payload - ARRAY[
        'ordinal','task_id','result_count','bundle_digest','stage','context','restart_incomplete'
    ];
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_task := (p_payload ->> 'task_id')::uuid;
    v_count := (p_payload ->> 'result_count')::bigint;
    v_digest := p_payload ->> 'bundle_digest';
    v_context := p_payload -> 'context';
    v_stage := COALESCE(p_payload ->> 'stage', 'raw');
    v_restart := COALESCE((p_payload ->> 'restart_incomplete')::boolean, false);
    IF v_stage NOT IN ('raw','prepared') OR (v_restart AND v_stage <> 'raw') THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_locked := aap_jobs_v1.lock_batch_result_execution(
        v_scope || pg_catalog.jsonb_build_object('ordinal',v_ordinal,'task_id',v_task)
    );
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, COALESCE(v_locked #>> '{error,code}', 'lease_lost'));
    END IF;
    v_member := v_locked #> '{data,member}';
    IF v_member IS NULL OR v_member = 'null'::jsonb
       OR v_member ->> 'task_id' IS DISTINCT FROM v_task::text THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'scope_mismatch');
    END IF;
    SELECT * INTO v_bundle FROM public.batch_result_bundles
    WHERE job_id = (v_scope ->> 'job_id')::uuid AND ordinal = v_ordinal
    FOR UPDATE;
    IF v_stage = 'raw' THEN
        IF FOUND THEN
            IF v_bundle.task_id IS DISTINCT FROM v_task
               OR v_bundle.state IN ('failed','abandoned') THEN
                RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_conflict');
            END IF;
            IF v_restart THEN
                IF v_bundle.raw_complete
                   OR EXISTS (
                        SELECT 1 FROM public.batch_result_payloads AS payload
                        WHERE payload.result_id = v_bundle.result_id AND payload.complete
                   )
                   OR EXISTS (
                        SELECT 1 FROM public.batch_result_payloads AS payload
                        WHERE payload.result_id = v_bundle.result_id AND payload.receipt IS NOT NULL
                   ) THEN
                    IF v_bundle.result_count IS DISTINCT FROM v_count
                       OR v_bundle.raw_digest IS DISTINCT FROM v_digest
                       OR v_bundle.context IS DISTINCT FROM v_context THEN
                        RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_conflict');
                    END IF;
                    RETURN aap_labeling_v1._batch_result_reply(
                        pg_catalog.jsonb_build_object('result',aap_labeling_v1._batch_result_bundle_json(v_bundle)), NULL
                    );
                END IF;
                DELETE FROM public.batch_result_payloads WHERE result_id = v_bundle.result_id;
                UPDATE public.batch_result_bundles
                SET raw_digest = v_digest, result_count = v_count,
                    prepared_digest = NULL, raw_complete = (v_count = 0), prepared_complete = false,
                    state = 'capturing', context = v_context, receipt_count = 0,
                    applied_at = NULL, updated_at = pg_catalog.clock_timestamp()
                WHERE result_id = v_bundle.result_id
                RETURNING * INTO v_bundle;
                v_record := aap_jobs_v1.record_batch_result_member(
                    v_scope || pg_catalog.jsonb_build_object(
                        'ordinal',v_ordinal,'task_id',v_task,'result_id',v_bundle.result_id,'state','capturing'
                    )
                );
                IF v_record -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = COALESCE(v_record #>> '{error,code}','lease_lost');
                END IF;
            ELSIF v_bundle.result_count IS DISTINCT FROM v_count
               OR v_bundle.raw_digest IS DISTINCT FROM v_digest
               OR v_bundle.context IS DISTINCT FROM v_context THEN
                RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_conflict');
            END IF;
        ELSE
            v_result_id := pg_catalog.gen_random_uuid();
            v_record := aap_jobs_v1.record_batch_result_member(
                v_scope || pg_catalog.jsonb_build_object(
                    'ordinal',v_ordinal,'task_id',v_task,'result_id',v_result_id,'state','capturing'
                )
            );
            IF v_record -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN aap_labeling_v1._batch_result_reply(NULL, COALESCE(v_record #>> '{error,code}', 'lease_lost'));
            END IF;
            INSERT INTO public.batch_result_bundles(
                result_id, job_id, ordinal, task_id, result_count,
                raw_digest, raw_complete, prepared_complete, state, context
            ) VALUES (
                v_result_id, (v_scope ->> 'job_id')::uuid, v_ordinal, v_task, v_count,
                v_digest, (v_count = 0), false, 'capturing', v_context
            ) RETURNING * INTO v_bundle;
        END IF;
    ELSE
        IF NOT FOUND OR v_bundle.task_id IS DISTINCT FROM v_task
           OR v_bundle.result_count IS DISTINCT FROM v_count
           OR v_bundle.context IS DISTINCT FROM v_context
           OR NOT v_bundle.raw_complete
           OR v_bundle.state NOT IN ('captured','ready','applied') THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_not_ready');
        END IF;
        IF v_bundle.prepared_digest IS NOT NULL
           AND v_bundle.prepared_digest IS DISTINCT FROM v_digest THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_conflict');
        END IF;
        IF v_bundle.state = 'applied' THEN
            RETURN aap_labeling_v1._batch_result_reply(
                pg_catalog.jsonb_build_object('result', aap_labeling_v1._batch_result_bundle_json(v_bundle)), NULL
            );
        END IF;
        UPDATE public.batch_result_bundles
        SET prepared_digest = v_digest,
            prepared_complete = CASE WHEN result_count = 0 THEN true ELSE prepared_complete END,
            updated_at = pg_catalog.clock_timestamp()
        WHERE result_id = v_bundle.result_id
        RETURNING * INTO v_bundle;
    END IF;
    RETURN aap_labeling_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('result', aap_labeling_v1._batch_result_bundle_json(v_bundle)), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.begin_batch_result(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.put_batch_result_part(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_scope jsonb;
    v_locked jsonb;
    v_member jsonb;
    v_bundle public.batch_result_bundles%ROWTYPE;
    v_payload_row public.batch_result_payloads%ROWTYPE;
    v_raw public.batch_result_payloads%ROWTYPE;
    v_stage text;
    v_ordinal bigint;
    v_task uuid;
    v_index bigint;
    v_part_index bigint;
    v_byte_size bigint;
    v_part_count bigint;
    v_content text;
    v_bytes bytea;
    v_body jsonb;
    v_id uuid;
    v_created timestamptz;
    v_tool text;
    v_seen bytea;
    v_complete_count bigint;
    v_row_count bigint;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','stage','request_index','sha256','byte_size','part_count','part_index','content','tool_unit_id'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','stage','request_index','sha256','byte_size','part_count','part_index','content','tool_unit_id'
       ])
       OR NOT aap_labeling_v1._batch_result_scope_valid(p_payload - ARRAY[
            'ordinal','stage','request_index','sha256','byte_size','part_count','part_index','content','tool_unit_id'
       ], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'stage') IS DISTINCT FROM 'string'
       OR p_payload ->> 'stage' NOT IN ('raw','prepared')
       OR pg_catalog.jsonb_typeof(p_payload -> 'request_index') IS DISTINCT FROM 'number'
       OR p_payload ->> 'request_index' !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_payload ->> 'request_index') > 19
       OR (p_payload ->> 'request_index')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'sha256') IS DISTINCT FROM 'string'
       OR p_payload ->> 'sha256' !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'byte_size') IS DISTINCT FROM 'number'
       OR p_payload ->> 'byte_size' !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_payload ->> 'byte_size') > 19
       OR (p_payload ->> 'byte_size')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'part_count') IS DISTINCT FROM 'number'
       OR p_payload ->> 'part_count' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'part_count') > 19
       OR (p_payload ->> 'part_count')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'part_index') IS DISTINCT FROM 'number'
       OR p_payload ->> 'part_index' !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_payload ->> 'part_index') > 19
       OR (p_payload ->> 'part_index')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'content') IS DISTINCT FROM 'string'
       OR pg_catalog.octet_length(p_payload ->> 'content') > 699052
       OR (p_payload ->> 'content') !~ '^[A-Za-z0-9+/]*={0,2}$'
       OR (pg_catalog.jsonb_typeof(p_payload -> 'tool_unit_id') NOT IN ('string','null')) THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_scope := p_payload - ARRAY[
        'ordinal','stage','request_index','sha256','byte_size','part_count','part_index','content','tool_unit_id'
    ];
    v_stage := p_payload ->> 'stage';
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_task := NULL;
    v_index := (p_payload ->> 'request_index')::bigint;
    v_part_index := (p_payload ->> 'part_index')::bigint;
    v_byte_size := (p_payload ->> 'byte_size')::bigint;
    v_part_count := (p_payload ->> 'part_count')::bigint;
    v_content := p_payload ->> 'content';
    v_tool := CASE WHEN p_payload -> 'tool_unit_id' = 'null'::jsonb THEN NULL ELSE p_payload ->> 'tool_unit_id' END;
    IF v_part_index >= v_part_count
       OR (v_tool IS NOT NULL AND pg_catalog.octet_length(v_tool) > 30)
       OR (v_stage = 'raw' AND v_tool IS NOT NULL)
       OR pg_catalog.length(v_content) % 4 <> 0 THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_bytes := pg_catalog.decode(v_content, 'base64');
    IF pg_catalog.octet_length(v_bytes) > 524288
       OR pg_catalog.replace(pg_catalog.encode(v_bytes, 'base64'), E'\n', '') IS DISTINCT FROM v_content THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_locked := aap_jobs_v1.lock_batch_result_execution(
        v_scope || pg_catalog.jsonb_build_object('ordinal',v_ordinal,'task_id',NULL)
    );
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, COALESCE(v_locked #>> '{error,code}', 'lease_lost'));
    END IF;
    v_member := v_locked #> '{data,member}';
    v_task := (v_member ->> 'task_id')::uuid;
    SELECT * INTO v_bundle FROM public.batch_result_bundles
    WHERE job_id = (v_scope ->> 'job_id')::uuid AND ordinal = v_ordinal
    FOR UPDATE;
    IF NOT FOUND OR v_bundle.task_id IS DISTINCT FROM v_task
       OR v_bundle.result_id IS DISTINCT FROM (v_member ->> 'result_id')::uuid
       OR v_index >= v_bundle.result_count THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_not_ready');
    END IF;
    IF v_stage = 'raw' THEN
        IF v_bundle.state <> 'capturing' OR v_bundle.raw_complete THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_conflict');
        END IF;
    ELSE
        IF v_bundle.state NOT IN ('captured','ready') OR NOT v_bundle.raw_complete
           OR v_bundle.prepared_digest IS NULL OR v_bundle.prepared_complete THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_not_ready');
        END IF;
        SELECT * INTO v_raw FROM public.batch_result_payloads
        WHERE result_id = v_bundle.result_id AND stage = 'raw' AND request_index = v_index
        FOR UPDATE;
        IF NOT FOUND OR NOT v_raw.complete OR v_raw.body IS NULL THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_not_ready');
        END IF;
        v_id := v_raw.prediction_id;
        v_created := v_raw.prediction_created_at;
    END IF;
    SELECT * INTO v_payload_row FROM public.batch_result_payloads
    WHERE result_id = v_bundle.result_id AND stage = v_stage AND request_index = v_index
    FOR UPDATE;
    IF FOUND THEN
        IF v_payload_row.sha256 IS DISTINCT FROM p_payload ->> 'sha256'
           OR v_payload_row.byte_size IS DISTINCT FROM v_byte_size
           OR v_payload_row.part_count IS DISTINCT FROM v_part_count
           OR v_payload_row.tool_unit_id IS DISTINCT FROM v_tool THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_conflict');
        END IF;
        SELECT content INTO v_seen FROM public.batch_result_parts
        WHERE payload_id = v_payload_row.payload_id AND part_index = v_part_index;
        IF FOUND THEN
            IF v_seen IS DISTINCT FROM v_bytes THEN
                RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_conflict');
            END IF;
        ELSE
            INSERT INTO public.batch_result_parts(payload_id, part_index, content)
            VALUES (v_payload_row.payload_id, v_part_index, v_bytes);
        END IF;
    ELSE
        IF v_stage = 'raw' THEN
            v_id := pg_catalog.gen_random_uuid();
            v_created := pg_catalog.clock_timestamp();
        END IF;
        INSERT INTO public.batch_result_payloads(
            payload_id, result_id, job_id, ordinal, request_index,
            prediction_id, prediction_created_at, stage, sha256,
            byte_size, part_count, complete, tool_unit_id
        ) VALUES (
            pg_catalog.gen_random_uuid(), v_bundle.result_id, v_bundle.job_id,
            v_bundle.ordinal, v_index, v_id, v_created, v_stage,
            p_payload ->> 'sha256', v_byte_size, v_part_count, false, v_tool
        ) RETURNING * INTO v_payload_row;
        INSERT INTO public.batch_result_parts(payload_id, part_index, content)
        VALUES (v_payload_row.payload_id, v_part_index, v_bytes);
    END IF;
    SELECT count(*)::bigint INTO v_complete_count
    FROM public.batch_result_parts WHERE payload_id = v_payload_row.payload_id;
    IF v_complete_count = v_payload_row.part_count THEN
        BEGIN
            v_bytes := aap_labeling_v1._batch_result_assembled_bytes(v_payload_row.payload_id);
        EXCEPTION WHEN SQLSTATE 'P0001' THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_incomplete');
        END;
        v_body := NULL;
        BEGIN
            v_body := pg_catalog.convert_from(v_bytes, 'UTF8')::jsonb;
            IF pg_catalog.jsonb_typeof(v_body) IS DISTINCT FROM 'object' THEN
                v_body := NULL;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            v_body := NULL;
        END;
        UPDATE public.batch_result_payloads
        SET complete = true, body = v_body, roots_active = false,
            updated_at = pg_catalog.clock_timestamp()
        WHERE payload_id = v_payload_row.payload_id
        RETURNING * INTO v_payload_row;
        IF v_payload_row.body IS NOT NULL THEN
            BEGIN
                UPDATE public.batch_result_payloads
                SET roots_active = true, updated_at = pg_catalog.clock_timestamp()
                WHERE payload_id = v_payload_row.payload_id
                RETURNING * INTO v_payload_row;
            EXCEPTION WHEN SQLSTATE 'AM409' THEN
                -- Keep the complete bytes and parsed body durable. Seal retries
                -- root publication after the transient Mask fence changes.
                NULL;
            END;
        END IF;
        IF v_stage = 'raw' THEN
            SELECT count(*)::bigint,
                   count(*) FILTER (WHERE payload.complete)::bigint
            INTO v_row_count, v_complete_count
            FROM public.batch_result_payloads AS payload
            WHERE payload.result_id = v_bundle.result_id AND payload.stage = 'raw';
            IF v_row_count = v_bundle.result_count AND v_complete_count = v_bundle.result_count THEN
                UPDATE public.batch_result_bundles SET raw_complete = true, updated_at = pg_catalog.clock_timestamp()
                WHERE result_id = v_bundle.result_id RETURNING * INTO v_bundle;
            END IF;
        ELSE
            SELECT count(*)::bigint,
                   count(*) FILTER (WHERE payload.complete)::bigint
            INTO v_row_count, v_complete_count
            FROM public.batch_result_payloads AS payload
            WHERE payload.result_id = v_bundle.result_id AND payload.stage = 'prepared';
            IF v_row_count = v_bundle.result_count AND v_complete_count = v_bundle.result_count THEN
                UPDATE public.batch_result_bundles SET prepared_complete = true, updated_at = pg_catalog.clock_timestamp()
                WHERE result_id = v_bundle.result_id RETURNING * INTO v_bundle;
            END IF;
        END IF;
    END IF;
    RETURN aap_labeling_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'result', aap_labeling_v1._batch_result_bundle_json(v_bundle),
            'payload', aap_labeling_v1._batch_result_payload_json(v_payload_row)
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.put_batch_result_part(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._batch_result_body_valid(
    p_body jsonb,
    p_write_scope jsonb,
    p_task_id uuid,
    p_project_id uuid
)
RETURNS boolean
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_key text;
    v_value jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_body) IS DISTINCT FROM 'object'
       OR p_body ?| ARRAY['result_id','payload_id','prediction_id','prediction_created_at','write_identity','tool_unit_id']
       OR pg_catalog.jsonb_typeof(p_body -> 'task_id') IS DISTINCT FROM 'string'
       OR p_body ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR pg_catalog.jsonb_typeof(p_body -> 'project_id') IS DISTINCT FROM 'string'
       OR p_body ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR pg_catalog.jsonb_typeof(p_body -> 'write_scope') IS DISTINCT FROM 'object'
       OR p_body -> 'write_scope' IS DISTINCT FROM p_write_scope
       OR pg_catalog.jsonb_typeof(p_body -> 'result') IS DISTINCT FROM 'array'
       OR (p_body ? 'source' AND pg_catalog.jsonb_typeof(p_body -> 'source') IS DISTINCT FROM 'string')
       OR (p_body ? 'model_version' AND pg_catalog.jsonb_typeof(p_body -> 'model_version') NOT IN ('string','null'))
       OR (p_body ? 'score' AND pg_catalog.jsonb_typeof(p_body -> 'score') NOT IN ('number','null'))
       OR (p_body ? 'inference_time_ms' AND pg_catalog.jsonb_typeof(p_body -> 'inference_time_ms') NOT IN ('number','null'))
       OR (p_body ? 'token_meta' AND pg_catalog.jsonb_typeof(p_body -> 'token_meta') NOT IN ('object','null'))
       OR (p_body ? 'pipeline_extra' AND pg_catalog.jsonb_typeof(p_body -> 'pipeline_extra') NOT IN ('object','null'))
       OR (p_body ? 'model_provenance' AND pg_catalog.jsonb_typeof(p_body -> 'model_provenance') NOT IN ('object','null'))
       OR (p_body ? 'accepted_video_input' AND pg_catalog.jsonb_typeof(p_body -> 'accepted_video_input') NOT IN ('object','null')) THEN
        RETURN false;
    END IF;
    FOREACH v_key IN ARRAY ARRAY['ml_backend_id','ml_backend_pool_id','accepted_input_job_id'] LOOP
        IF p_body ? v_key THEN
            v_value := p_body -> v_key;
            IF v_value <> 'null'::jsonb AND (
                pg_catalog.jsonb_typeof(v_value) IS DISTINCT FROM 'string'
                OR v_value #>> '{}' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            ) THEN
                RETURN false;
            END IF;
        END IF;
    END LOOP;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._batch_result_body_valid(jsonb, jsonb, uuid, uuid) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.seal_batch_result(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_scope jsonb;
    v_locked jsonb;
    v_member jsonb;
    v_bundle public.batch_result_bundles%ROWTYPE;
    v_payload_row public.batch_result_payloads%ROWTYPE;
    v_raw public.batch_result_payloads%ROWTYPE;
    v_stage text;
    v_ordinal bigint;
    v_digest text;
    v_bytes bytea;
    v_body jsonb;
    v_raw_body jsonb;
    v_record jsonb;
    v_next_state text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal','stage']) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','generation','lease_token','ordinal','stage'])
       OR NOT aap_labeling_v1._batch_result_scope_valid(p_payload - ARRAY['ordinal','stage'], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'stage') IS DISTINCT FROM 'string'
       OR p_payload ->> 'stage' NOT IN ('raw','prepared') THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_scope := p_payload - ARRAY['ordinal','stage'];
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_stage := p_payload ->> 'stage';
    v_locked := aap_jobs_v1.lock_batch_result_execution(
        v_scope || pg_catalog.jsonb_build_object('ordinal',v_ordinal,'task_id',NULL)
    );
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, COALESCE(v_locked #>> '{error,code}', 'lease_lost'));
    END IF;
    v_member := v_locked #> '{data,member}';
    SELECT * INTO v_bundle FROM public.batch_result_bundles
    WHERE job_id = (v_scope ->> 'job_id')::uuid AND ordinal = v_ordinal
    FOR UPDATE;
    IF NOT FOUND OR v_bundle.task_id IS DISTINCT FROM (v_member ->> 'task_id')::uuid
       OR v_bundle.result_id IS DISTINCT FROM (v_member ->> 'result_id')::uuid THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_not_ready');
    END IF;
    IF v_stage = 'raw' THEN
        IF v_bundle.state IN ('captured','ready','applied') AND v_bundle.raw_complete THEN
            RETURN aap_labeling_v1._batch_result_reply(
                pg_catalog.jsonb_build_object('result',aap_labeling_v1._batch_result_bundle_json(v_bundle)), NULL
            );
        END IF;
        IF v_bundle.state <> 'capturing' OR NOT v_bundle.raw_complete
           OR v_bundle.result_count IS DISTINCT FROM (
                SELECT count(*)::bigint FROM public.batch_result_payloads
                WHERE result_id=v_bundle.result_id AND stage='raw' AND complete
           ) THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_incomplete');
        END IF;
        v_digest := aap_labeling_v1._batch_result_bundle_digest(
            v_bundle.job_id, v_bundle.ordinal, v_bundle.result_count,
            (v_scope ->> 'request_digest'), 'raw', v_bundle.result_id
        );
        IF v_digest IS DISTINCT FROM v_bundle.raw_digest THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
        END IF;
        FOR v_payload_row IN
            SELECT * FROM public.batch_result_payloads
            WHERE result_id = v_bundle.result_id AND stage = 'raw'
            ORDER BY request_index FOR UPDATE
        LOOP
            v_bytes := aap_labeling_v1._batch_result_assembled_bytes(v_payload_row.payload_id);
            v_body := pg_catalog.convert_from(v_bytes, 'UTF8')::jsonb;
            IF NOT aap_labeling_v1._batch_result_body_valid(
                v_body, v_member -> 'write_scope', v_bundle.task_id,
                (v_scope ->> 'project_id')::uuid
            ) THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
            END IF;
            UPDATE public.batch_result_payloads
            SET body = v_body, roots_active = true, updated_at = pg_catalog.clock_timestamp()
            WHERE payload_id = v_payload_row.payload_id;
        END LOOP;
        UPDATE public.batch_result_bundles
        SET state = 'captured', updated_at = pg_catalog.clock_timestamp()
        WHERE result_id = v_bundle.result_id
        RETURNING * INTO v_bundle;
        v_next_state := 'captured';
    ELSE
        IF v_bundle.state IN ('ready','applied') AND v_bundle.prepared_complete THEN
            RETURN aap_labeling_v1._batch_result_reply(
                pg_catalog.jsonb_build_object('result',aap_labeling_v1._batch_result_bundle_json(v_bundle)), NULL
            );
        END IF;
        IF v_bundle.state <> 'captured' OR NOT v_bundle.raw_complete
           OR NOT v_bundle.prepared_complete
           OR v_bundle.prepared_digest IS NULL
           OR v_bundle.result_count IS DISTINCT FROM (
                SELECT count(*)::bigint FROM public.batch_result_payloads
                WHERE result_id=v_bundle.result_id AND stage='prepared' AND complete
           ) THEN
            RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_incomplete');
        END IF;
        v_digest := aap_labeling_v1._batch_result_bundle_digest(
            v_bundle.job_id, v_bundle.ordinal, v_bundle.result_count,
            (v_scope ->> 'request_digest'), 'prepared', v_bundle.result_id
        );
        IF v_digest IS DISTINCT FROM v_bundle.prepared_digest THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
        END IF;
        FOR v_payload_row IN
            SELECT * FROM public.batch_result_payloads
            WHERE result_id = v_bundle.result_id AND stage = 'prepared'
            ORDER BY request_index FOR UPDATE
        LOOP
            v_raw := NULL;
            SELECT * INTO v_raw FROM public.batch_result_payloads
            WHERE result_id=v_bundle.result_id AND stage='raw'
              AND request_index=v_payload_row.request_index;
            IF NOT FOUND OR NOT v_raw.complete OR v_raw.body IS NULL
               OR v_raw.prediction_id IS DISTINCT FROM v_payload_row.prediction_id
               OR v_raw.prediction_created_at IS DISTINCT FROM v_payload_row.prediction_created_at THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
            END IF;
            v_bytes := aap_labeling_v1._batch_result_assembled_bytes(v_payload_row.payload_id);
            v_body := pg_catalog.convert_from(v_bytes, 'UTF8')::jsonb;
            IF NOT aap_labeling_v1._batch_result_body_valid(
                v_body, v_member -> 'write_scope', v_bundle.task_id,
                (v_scope ->> 'project_id')::uuid
            ) THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
            END IF;
            v_raw_body := v_raw.body;
            IF (v_body - 'result') IS DISTINCT FROM (v_raw_body - 'result') THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
            END IF;
            UPDATE public.batch_result_payloads
            SET body = v_body, roots_active = true, updated_at = pg_catalog.clock_timestamp()
            WHERE payload_id = v_payload_row.payload_id;
        END LOOP;
        UPDATE public.batch_result_bundles
        SET state = 'ready', updated_at = pg_catalog.clock_timestamp()
        WHERE result_id = v_bundle.result_id
        RETURNING * INTO v_bundle;
        v_next_state := 'ready';
    END IF;
    v_record := aap_jobs_v1.record_batch_result_member(
        v_scope || pg_catalog.jsonb_build_object(
            'ordinal',v_ordinal,'task_id',v_bundle.task_id,
            'result_id',v_bundle.result_id,'state',v_next_state
        )
    );
    IF v_record -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE=COALESCE(v_record #>> '{error,code}','lease_lost');
    END IF;
    RETURN aap_labeling_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('result',aap_labeling_v1._batch_result_bundle_json(v_bundle)), NULL
    );
EXCEPTION WHEN SQLSTATE 'AM409' THEN
    RETURN aap_labeling_v1._batch_result_reply(
        NULL, CASE WHEN SQLERRM='content_epoch_changed' THEN 'content_epoch_changed' ELSE 'content_deleting' END
    );
WHEN SQLSTATE 'P0001' THEN
    RETURN aap_labeling_v1._batch_result_reply(
        NULL, CASE WHEN SQLERRM IN ('result_conflict','result_incomplete','lease_lost','selection_conflict') THEN SQLERRM ELSE 'invalid_response' END
    );
WHEN invalid_text_representation OR character_not_in_repertoire THEN
    RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_incomplete');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.seal_batch_result(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.read_batch_result(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_scope jsonb;
    v_scope_reply jsonb;
    v_bundle public.batch_result_bundles%ROWTYPE;
    v_payload_row public.batch_result_payloads%ROWTYPE;
    v_ordinal bigint;
    v_index bigint;
    v_part bigint;
    v_stage text;
    v_content bytea;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'job_id','actor_id','project_id','request_digest','ordinal','stage','request_index','part_index'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY['job_id','actor_id','project_id','request_digest','ordinal'])
       OR NOT aap_labeling_v1._batch_result_scope_valid(p_payload - ARRAY['ordinal','stage','request_index','part_index'], false)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807
       OR (p_payload ? 'stage' AND pg_catalog.jsonb_typeof(p_payload -> 'stage') IS DISTINCT FROM 'string')
       OR (p_payload ? 'request_index' AND pg_catalog.jsonb_typeof(p_payload -> 'request_index') IS DISTINCT FROM 'number')
       OR (p_payload ? 'request_index' AND p_payload ->> 'request_index' !~ '^(0|[1-9][0-9]*)$')
       OR (p_payload ? 'part_index' AND pg_catalog.jsonb_typeof(p_payload -> 'part_index') NOT IN ('number','null'))
       OR (p_payload -> 'part_index' <> 'null'::jsonb AND p_payload ? 'part_index'
           AND p_payload ->> 'part_index' !~ '^(0|[1-9][0-9]*)$')
       OR (p_payload -> 'part_index' <> 'null'::jsonb AND p_payload ? 'part_index'
           AND pg_catalog.char_length(p_payload ->> 'part_index') > 19)
       OR (p_payload -> 'part_index' <> 'null'::jsonb AND p_payload ? 'part_index'
           AND (p_payload ->> 'part_index')::numeric > 9223372036854775807) THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_scope := p_payload - ARRAY['ordinal','stage','request_index','part_index'];
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_stage := COALESCE(p_payload ->> 'stage', 'raw');
    v_index := COALESCE((p_payload ->> 'request_index')::bigint, 0);
    v_part := CASE WHEN p_payload -> 'part_index' IS NULL OR p_payload -> 'part_index' = 'null'::jsonb
        THEN NULL ELSE (p_payload ->> 'part_index')::bigint END;
    IF v_stage NOT IN ('raw','prepared') OR (v_part IS NOT NULL AND v_part < 0) THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_scope_reply := aap_jobs_v1.batch_result_execution_referenced(v_scope);
    IF v_scope_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, COALESCE(v_scope_reply #>> '{error,code}', 'scope_mismatch'));
    END IF;
    SELECT * INTO v_bundle FROM public.batch_result_bundles
    WHERE job_id = (v_scope ->> 'job_id')::uuid AND ordinal = v_ordinal;
    IF NOT FOUND OR v_bundle.task_id IS NULL THEN
        RETURN aap_labeling_v1._batch_result_reply(
            pg_catalog.jsonb_build_object('result',NULL,'payload',NULL,'content',NULL), NULL
        );
    END IF;
    SELECT * INTO v_payload_row FROM public.batch_result_payloads
    WHERE result_id = v_bundle.result_id AND stage = v_stage AND request_index = v_index;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._batch_result_reply(
            pg_catalog.jsonb_build_object('result',aap_labeling_v1._batch_result_bundle_json(v_bundle),'payload',NULL,'content',NULL), NULL
        );
    END IF;
    IF v_part IS NOT NULL THEN
        SELECT content INTO v_content FROM public.batch_result_parts
        WHERE payload_id = v_payload_row.payload_id AND part_index = v_part;
    END IF;
    RETURN aap_labeling_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'result',aap_labeling_v1._batch_result_bundle_json(v_bundle),
            'payload',aap_labeling_v1._batch_result_payload_json(v_payload_row),
            'content',CASE WHEN v_content IS NULL THEN NULL
                ELSE pg_catalog.replace(pg_catalog.encode(v_content,'base64'), E'\n', '') END
        ), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_batch_result(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.lock_batch_result_write(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_scope jsonb;
    v_progress jsonb;
    v_member jsonb;
    v_locked jsonb;
    v_read jsonb;
    v_bundle public.batch_result_bundles%ROWTYPE;
    v_result uuid;
    v_ordinal bigint;
    v_digest text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','result_id','prepared_digest'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','result_id','prepared_digest'
       ])
       OR NOT aap_labeling_v1._batch_result_scope_valid(p_payload - ARRAY['ordinal','result_id','prepared_digest'], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'result_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'result_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'prepared_digest') IS DISTINCT FROM 'string'
       OR p_payload ->> 'prepared_digest' !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_request');
    END IF;
    v_scope := p_payload - ARRAY['ordinal','result_id','prepared_digest','generation','lease_token'];
    v_result := (p_payload ->> 'result_id')::uuid;
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_digest := p_payload ->> 'prepared_digest';
    v_read := aap_jobs_v1.read_batch_result_execution(
        v_scope || pg_catalog.jsonb_build_object('after_ordinal',v_ordinal - 1,'limit',1)
    );
    IF v_read -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, COALESCE(v_read #>> '{error,code}', 'scope_mismatch'));
    END IF;
    SELECT item.value INTO v_member
    FROM pg_catalog.jsonb_array_elements(v_read #> '{data,members}') AS item(value)
    WHERE (item.value ->> 'ordinal')::bigint = v_ordinal;
    IF v_member IS NOT NULL THEN
        SELECT * INTO v_bundle FROM public.batch_result_bundles
        WHERE result_id = v_result AND job_id = (v_scope ->> 'job_id')::uuid
          AND ordinal = v_ordinal;
        IF FOUND AND v_bundle.state = 'applied'
           AND v_bundle.prepared_digest = v_digest THEN
            RETURN aap_labeling_v1._batch_result_reply(
                pg_catalog.jsonb_build_object('result',aap_labeling_v1._batch_result_bundle_json(v_bundle)), NULL
            );
        END IF;
    END IF;
    v_locked := aap_jobs_v1.lock_batch_result_execution(
        (p_payload - ARRAY['result_id','prepared_digest'])
            || pg_catalog.jsonb_build_object('task_id',NULL)
    );
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, COALESCE(v_locked #>> '{error,code}', 'lease_lost'));
    END IF;
    v_member := v_locked #> '{data,member}';
    SELECT * INTO v_bundle FROM public.batch_result_bundles
    WHERE result_id = v_result AND job_id = (v_scope ->> 'job_id')::uuid
      AND ordinal = v_ordinal
    FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_bundle.task_id IS DISTINCT FROM (v_member ->> 'task_id')::uuid
       OR v_bundle.state <> 'ready'
       OR NOT v_bundle.raw_complete OR NOT v_bundle.prepared_complete
       OR v_bundle.prepared_digest IS DISTINCT FROM v_digest
       OR v_member ->> 'result_id' IS DISTINCT FROM v_bundle.result_id::text THEN
        RETURN aap_labeling_v1._batch_result_reply(NULL, 'result_not_ready');
    END IF;
    RETURN aap_labeling_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('result',aap_labeling_v1._batch_result_bundle_json(v_bundle)), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._batch_result_reply(NULL, 'execution_busy');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._batch_result_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.lock_batch_result_write(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._batch_result_prediction_matches(
    p_payload public.batch_result_payloads,
    p_body jsonb
)
RETURNS boolean
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_prediction public.predictions%ROWTYPE;
    v_meta public.prediction_metas%ROWTYPE;
    v_token jsonb;
    v_expected_extra jsonb;
BEGIN
    SELECT * INTO v_prediction
    FROM public.predictions AS prediction
    WHERE prediction.id = p_payload.prediction_id
      AND prediction.created_at = p_payload.prediction_created_at;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO v_meta
    FROM public.prediction_metas AS meta
    WHERE meta.prediction_id = p_payload.prediction_id
      AND meta.prediction_created_at = p_payload.prediction_created_at;
    IF NOT FOUND THEN RETURN false; END IF;
    v_token := CASE WHEN p_body -> 'token_meta' IS NULL OR p_body -> 'token_meta' = 'null'::jsonb
        THEN '{}'::jsonb ELSE p_body -> 'token_meta' END;
    IF pg_catalog.jsonb_typeof(v_token) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    v_expected_extra := CASE
        WHEN p_body -> 'pipeline_extra' IS NULL OR p_body -> 'pipeline_extra' = 'null'::jsonb
            THEN '{}'::jsonb ELSE p_body -> 'pipeline_extra' END;
    IF p_body -> 'model_provenance' IS NOT NULL
       AND p_body -> 'model_provenance' <> 'null'::jsonb
       AND p_body -> 'model_provenance' <> '{}'::jsonb THEN
        v_expected_extra := v_expected_extra || pg_catalog.jsonb_build_object(
            'trained_model', p_body -> 'model_provenance'
        );
    END IF;
    IF p_body -> 'accepted_video_input' IS NOT NULL AND p_body -> 'accepted_video_input' <> 'null'::jsonb THEN
        v_expected_extra := v_expected_extra || pg_catalog.jsonb_build_object(
            'accepted_video_input',p_body -> 'accepted_video_input',
            'accepted_video_execution_job_id',p_body #> '{write_scope,job_id}'
        );
    END IF;
    RETURN v_prediction.task_id IS NOT DISTINCT FROM (p_body ->> 'task_id')::uuid
       AND v_prediction.project_id IS NOT DISTINCT FROM (p_body ->> 'project_id')::uuid
       AND v_prediction.ml_backend_id IS NOT DISTINCT FROM NULLIF(p_body ->> 'ml_backend_id','')::uuid
       AND v_prediction.ml_backend_pool_id IS NOT DISTINCT FROM NULLIF(p_body ->> 'ml_backend_pool_id','')::uuid
       AND v_prediction.model_version IS NOT DISTINCT FROM p_body ->> 'model_version'
       AND v_prediction.score IS NOT DISTINCT FROM NULLIF(p_body ->> 'score','')::double precision
       AND v_prediction.tool_unit_id IS NOT DISTINCT FROM p_payload.tool_unit_id
       AND v_prediction.result IS NOT DISTINCT FROM p_body -> 'result'
       AND v_prediction.source IS NOT DISTINCT FROM COALESCE(p_body ->> 'source','ml_backend')
       AND v_prediction.cluster IS NULL
       AND v_prediction.mislabeling IS NULL
       AND v_prediction.rejected_shape_indexes IS NOT DISTINCT FROM '[]'::jsonb
       AND v_meta.failed_prediction_id IS NULL
       AND v_meta.inference_time_ms IS NOT DISTINCT FROM NULLIF(p_body ->> 'inference_time_ms','')::integer
       AND v_meta.prompt_tokens IS NOT DISTINCT FROM NULLIF(v_token ->> 'prompt_tokens','')::integer
       AND v_meta.completion_tokens IS NOT DISTINCT FROM NULLIF(v_token ->> 'completion_tokens','')::integer
       AND v_meta.total_tokens IS NOT DISTINCT FROM NULLIF(v_token ->> 'total_tokens','')::integer
       AND v_meta.prompt_cost IS NOT DISTINCT FROM NULLIF(v_token ->> 'prompt_cost','')::double precision
       AND v_meta.completion_cost IS NOT DISTINCT FROM NULLIF(v_token ->> 'completion_cost','')::double precision
       AND v_meta.total_cost IS NOT DISTINCT FROM NULLIF(v_token ->> 'total_cost','')::double precision
       AND v_meta.extra IS NOT DISTINCT FROM v_expected_extra;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._batch_result_prediction_matches(public.batch_result_payloads, jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.finish_batch_result_write(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_scope jsonb;
    v_scope_reply jsonb;
    v_locked jsonb;
    v_member jsonb;
    v_read jsonb;
    v_bundle public.batch_result_bundles%ROWTYPE;
    v_payload_row public.batch_result_payloads%ROWTYPE;
    v_raw public.batch_result_payloads%ROWTYPE;
    v_result uuid;
    v_ordinal bigint;
    v_digest text;
    v_receipt jsonb;
    v_progress jsonb;
    v_advance jsonb;
    v_count bigint;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','result_id','prepared_digest'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','result_id','prepared_digest'
       ])
       OR NOT aap_labeling_v1._batch_result_scope_valid(p_payload - ARRAY['ordinal','result_id','prepared_digest'], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'result_id') IS DISTINCT FROM 'string'
       OR p_payload ->> 'result_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR pg_catalog.jsonb_typeof(p_payload -> 'prepared_digest') IS DISTINCT FROM 'string'
       OR p_payload ->> 'prepared_digest' !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='invalid_request';
    END IF;
    v_scope := p_payload - ARRAY['ordinal','result_id','prepared_digest','generation','lease_token'];
    v_result := (p_payload ->> 'result_id')::uuid;
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_digest := p_payload ->> 'prepared_digest';
    v_read := aap_jobs_v1.read_batch_result_execution(
        v_scope || pg_catalog.jsonb_build_object('after_ordinal',v_ordinal - 1,'limit',1)
    );
    IF v_read -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE=COALESCE(v_read #>> '{error,code}','scope_mismatch');
    END IF;
    SELECT item.value INTO v_member
    FROM pg_catalog.jsonb_array_elements(v_read #> '{data,members}') AS item(value)
    WHERE (item.value ->> 'ordinal')::bigint = v_ordinal;
    SELECT * INTO v_bundle FROM public.batch_result_bundles
    WHERE result_id = v_result AND job_id = (v_scope ->> 'job_id')::uuid
      AND ordinal = v_ordinal;
    IF FOUND AND v_bundle.state = 'applied'
       AND v_bundle.prepared_digest = v_digest
       AND v_bundle.receipt_count = v_bundle.result_count THEN
        RETURN aap_labeling_v1._batch_result_reply(
            pg_catalog.jsonb_build_object(
                'result',aap_labeling_v1._batch_result_bundle_json(v_bundle),
                'progress',v_read #> '{data,progress}'
            ), NULL
        );
    END IF;
    v_locked := aap_jobs_v1.lock_batch_result_execution(
        p_payload - ARRAY['result_id','prepared_digest']
            || pg_catalog.jsonb_build_object('task_id',NULL)
    );
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE=COALESCE(v_locked #>> '{error,code}','lease_lost');
    END IF;
    v_member := v_locked #> '{data,member}';
    SELECT * INTO v_bundle FROM public.batch_result_bundles
    WHERE result_id = v_result AND job_id = (v_scope ->> 'job_id')::uuid
      AND ordinal = v_ordinal
    FOR UPDATE;
    IF NOT FOUND OR v_member IS NULL
       OR v_bundle.task_id IS DISTINCT FROM (v_member ->> 'task_id')::uuid
       OR v_bundle.state <> 'ready'
       OR v_bundle.prepared_digest IS DISTINCT FROM v_digest
       OR NOT v_bundle.raw_complete OR NOT v_bundle.prepared_complete THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_not_ready';
    END IF;
    SELECT count(*)::bigint INTO v_count
    FROM public.batch_result_payloads AS payload
    WHERE payload.result_id = v_result AND payload.stage = 'prepared'
      AND payload.complete AND payload.body IS NOT NULL;
    IF v_count <> v_bundle.result_count THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_incomplete';
    END IF;
    FOR v_payload_row IN
        SELECT * FROM public.batch_result_payloads
        WHERE result_id = v_result AND stage = 'prepared'
        ORDER BY request_index FOR UPDATE
    LOOP
        IF v_payload_row.request_index < 0 OR v_payload_row.request_index >= v_bundle.result_count
           OR NOT aap_labeling_v1._batch_result_prediction_matches(v_payload_row, v_payload_row.body) THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
        END IF;
        SELECT * INTO v_raw FROM public.batch_result_payloads
        WHERE result_id = v_result AND stage = 'raw'
          AND request_index = v_payload_row.request_index
        FOR UPDATE;
        IF NOT FOUND OR v_raw.prediction_id IS DISTINCT FROM v_payload_row.prediction_id
           OR v_raw.prediction_created_at IS DISTINCT FROM v_payload_row.prediction_created_at
           OR v_raw.body IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
        END IF;
        v_receipt := pg_catalog.jsonb_build_object(
            'id',v_payload_row.prediction_id,
            'created_at',v_payload_row.prediction_created_at,
            'tool_unit_id',v_payload_row.tool_unit_id
        );
        UPDATE public.batch_result_payloads
        SET receipt = v_receipt, body = NULL, roots_active = false,
            updated_at = pg_catalog.clock_timestamp()
        WHERE payload_id IN (v_payload_row.payload_id, v_raw.payload_id);
    END LOOP;
    DELETE FROM public.batch_result_parts
    WHERE payload_id IN (
        SELECT payload.payload_id FROM public.batch_result_payloads AS payload
        WHERE payload.result_id = v_result
    );
    UPDATE public.batch_result_bundles
    SET state = 'applied', receipt_count = result_count,
        applied_at = pg_catalog.clock_timestamp(), updated_at = pg_catalog.clock_timestamp()
    WHERE result_id = v_result
    RETURNING * INTO v_bundle;
    v_advance := aap_jobs_v1.advance_batch_result_member(
        v_scope || pg_catalog.jsonb_build_object(
            'generation',(p_payload ->> 'generation')::bigint,
            'lease_token',p_payload ->> 'lease_token',
            'ordinal',v_ordinal,
            'task_id',v_bundle.task_id,
            'result_id',v_result,
            'outcome','applied',
            'context',v_bundle.context
        )
    );
    IF v_advance -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE=COALESCE(v_advance #>> '{error,code}','lease_lost');
    END IF;
    RETURN aap_labeling_v1._batch_result_reply(
        pg_catalog.jsonb_build_object(
            'result',aap_labeling_v1._batch_result_bundle_json(v_bundle),
            'progress',v_advance #> '{data,progress}'
        ), NULL
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.finish_batch_result_write(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.finish_batch_result_failure(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_scope jsonb;
    v_read jsonb;
    v_locked jsonb;
    v_member jsonb;
    v_bundle public.batch_result_bundles%ROWTYPE;
    v_failed public.failed_predictions%ROWTYPE;
    v_job_id uuid;
    v_ordinal bigint;
    v_failure_id uuid;
    v_failed_id uuid;
    v_result uuid;
    v_error text;
    v_context jsonb;
    v_advance jsonb;
    v_task_id uuid;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','failed_prediction_id','error_code','context'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ?& ARRAY[
            'job_id','actor_id','project_id','request_digest','generation','lease_token',
            'ordinal','failed_prediction_id','error_code','context'
       ])
       OR NOT aap_labeling_v1._batch_result_scope_valid(p_payload - ARRAY[
            'ordinal','failed_prediction_id','error_code','context'
       ], true)
       OR pg_catalog.jsonb_typeof(p_payload -> 'ordinal') IS DISTINCT FROM 'number'
       OR p_payload ->> 'ordinal' !~ '^[1-9][0-9]*$'
       OR pg_catalog.char_length(p_payload ->> 'ordinal') > 19
       OR (p_payload ->> 'ordinal')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(p_payload -> 'failed_prediction_id') NOT IN ('string','null')
       OR (p_payload -> 'failed_prediction_id' <> 'null'::jsonb
           AND p_payload ->> 'failed_prediction_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
       OR pg_catalog.jsonb_typeof(p_payload -> 'error_code') IS DISTINCT FROM 'string'
       OR pg_catalog.octet_length(p_payload ->> 'error_code') NOT BETWEEN 1 AND 256
       OR pg_catalog.jsonb_typeof(p_payload -> 'context') IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='invalid_request';
    END IF;
    IF pg_catalog.jsonb_path_exists(p_payload -> 'context', '$.**.raw_result')
       OR pg_catalog.jsonb_path_exists(p_payload -> 'context', '$.**.results')
       OR pg_catalog.jsonb_path_exists(p_payload -> 'context', '$.**.predictions')
       OR pg_catalog.jsonb_path_exists(p_payload -> 'context', '$.**.write_requests')
       OR pg_catalog.jsonb_path_exists(p_payload -> 'context', '$.**.output') THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='invalid_request';
    END IF;
    v_scope := p_payload - ARRAY[
        'ordinal','failed_prediction_id','error_code','context','generation','lease_token'
    ];
    v_job_id := (p_payload ->> 'job_id')::uuid;
    v_ordinal := (p_payload ->> 'ordinal')::bigint;
    v_failure_id := CASE WHEN p_payload -> 'failed_prediction_id'='null'::jsonb
        THEN NULL ELSE (p_payload ->> 'failed_prediction_id')::uuid END;
    v_error := p_payload ->> 'error_code';
    v_context := p_payload -> 'context';
    v_read := aap_jobs_v1.read_batch_result_execution(
        v_scope || pg_catalog.jsonb_build_object('after_ordinal',v_ordinal - 1,'limit',1)
    );
    IF v_read -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE=COALESCE(v_read #>> '{error,code}','scope_mismatch');
    END IF;
    SELECT item.value INTO v_member
    FROM pg_catalog.jsonb_array_elements(v_read #> '{data,members}') AS item(value)
    WHERE (item.value ->> 'ordinal')::bigint = v_ordinal;
    IF v_member IS NOT NULL AND v_member ->> 'state' = 'failed' THEN
        IF v_member ->> 'error_code' IS DISTINCT FROM v_error
           OR v_member -> 'failed_prediction_id' IS DISTINCT FROM p_payload -> 'failed_prediction_id' THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
        END IF;
        RETURN aap_labeling_v1._batch_result_reply(
            pg_catalog.jsonb_build_object('progress',v_read #> '{data,progress}'), NULL
        );
    END IF;
    IF v_member IS NULL OR (v_read #>> '{data,progress,next_ordinal}')::bigint <> v_ordinal THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='selection_conflict';
    END IF;
    v_task_id := (v_member ->> 'task_id')::uuid;
    v_result := CASE WHEN v_member -> 'result_id'='null'::jsonb
        THEN NULL ELSE (v_member ->> 'result_id')::uuid END;
    v_locked := aap_jobs_v1.lock_batch_result_execution(
        (p_payload - ARRAY['failed_prediction_id','error_code','context'])
            || pg_catalog.jsonb_build_object('task_id',v_task_id)
    );
    IF v_locked -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE=COALESCE(v_locked #>> '{error,code}','lease_lost');
    END IF;
    v_member := v_locked #> '{data,member}';
    IF v_member ->> 'failure_id' IS NULL
       OR v_member ->> 'failure_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR (v_failure_id IS NOT NULL AND v_failure_id IS DISTINCT FROM (v_member ->> 'failure_id')::uuid) THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='scope_mismatch';
    END IF;
    IF v_failure_id IS NULL THEN
        IF v_error = 'task_missing' THEN
            IF EXISTS (SELECT 1 FROM public.tasks WHERE id = v_task_id) THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='scope_mismatch';
            END IF;
        ELSIF v_error = 'scope_abandoned' THEN
            IF v_member -> 'write_scope' IS DISTINCT FROM 'null'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='scope_mismatch';
            END IF;
        ELSE
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='invalid_request';
        END IF;
    ELSE
        SELECT * INTO v_failed FROM public.failed_predictions
        WHERE id = v_failure_id FOR KEY SHARE;
        IF NOT FOUND
           OR v_failed.project_id IS DISTINCT FROM (v_scope ->> 'project_id')::uuid
           OR (v_failed.task_id IS DISTINCT FROM v_task_id
               AND NOT (v_error = 'task_missing' AND v_failed.task_id IS NULL
                        AND NOT EXISTS (SELECT 1 FROM public.tasks WHERE id = v_task_id))) THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='scope_mismatch';
        END IF;
    END IF;
    IF v_member ->> 'state' = 'planned'
       AND NOT (v_error IN ('task_missing','scope_abandoned') AND v_failure_id IS NULL) THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='selection_conflict';
    END IF;
    SELECT * INTO v_bundle FROM public.batch_result_bundles
    WHERE job_id = v_job_id AND ordinal = v_ordinal FOR UPDATE;
    IF FOUND THEN
        IF v_bundle.result_id IS DISTINCT FROM v_result
           OR v_bundle.state = 'applied' OR v_bundle.receipt_count <> 0 THEN
            RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
        END IF;
        UPDATE public.batch_result_payloads
        SET body = NULL, roots_active = false,
            updated_at = pg_catalog.clock_timestamp()
        WHERE result_id = v_bundle.result_id;
        DELETE FROM public.batch_result_parts
        WHERE payload_id IN (
            SELECT payload_id FROM public.batch_result_payloads WHERE result_id = v_bundle.result_id
        );
        UPDATE public.batch_result_bundles
        SET state = 'failed', updated_at = pg_catalog.clock_timestamp()
        WHERE result_id = v_bundle.result_id;
    ELSIF v_result IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='result_conflict';
    END IF;
    v_advance := aap_jobs_v1.advance_batch_result_member(
        v_scope || pg_catalog.jsonb_build_object(
            'generation',(p_payload ->> 'generation')::bigint,
            'lease_token',p_payload ->> 'lease_token',
            'ordinal',v_ordinal,
            'task_id',v_task_id,
            'result_id',v_result,
            'failed_prediction_id',v_failure_id,
            'error_code',v_error,
            'outcome','failed',
            'context',v_context
        )
    );
    IF v_advance -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE=COALESCE(v_advance #>> '{error,code}','lease_lost');
    END IF;
    RETURN aap_labeling_v1._batch_result_reply(
        pg_catalog.jsonb_build_object('progress',v_advance #> '{data,progress}'), NULL
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.finish_batch_result_failure(jsonb) FROM PUBLIC;
-- aap-statement-break
