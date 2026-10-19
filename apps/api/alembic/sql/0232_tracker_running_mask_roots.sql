-- Keep frame Masks live for the duration of an authoritative tracker run.
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
AS $function$
DECLARE
    v_reply jsonb;
    v_input_id uuid;
    v_payload_live boolean;
    v_bundle_found boolean;
    v_job_running boolean;
BEGIN
    IF p_root_kind = 'batch_result_payloads' THEN
        BEGIN
            SELECT payload.complete
                       AND payload.roots_active
                       AND payload.body IS NOT NULL
                       AND bundle.state IN ('capturing', 'captured', 'ready'),
                   bundle.result_id IS NOT NULL
            INTO v_payload_live, v_bundle_found
            FROM public.batch_result_payloads AS payload
            LEFT JOIN public.batch_result_bundles AS bundle
              ON bundle.result_id = payload.result_id
             AND bundle.job_id = payload.job_id
             AND bundle.ordinal = payload.ordinal
            WHERE payload.payload_id = p_root_id::uuid;
            IF NOT FOUND OR NOT COALESCE(v_bundle_found, false) THEN
                RETURN p_fail_closed;
            END IF;
            RETURN COALESCE(v_payload_live, p_fail_closed);
        EXCEPTION WHEN OTHERS THEN
            RETURN p_fail_closed;
        END;
    END IF;

    IF p_root_kind = 'video_tracker_running_outputs' THEN
        BEGIN
            SELECT job.status = 'running'
            INTO v_job_running
            FROM public.video_tracker_jobs AS job
            WHERE job.id = p_root_id::uuid;
            IF NOT FOUND THEN RETURN p_fail_closed; END IF;
            RETURN COALESCE(v_job_running, p_fail_closed);
        EXCEPTION WHEN OTHERS THEN
            RETURN p_fail_closed;
        END;
    END IF;

    IF p_root_kind = 'video_tracker_input_scopes' THEN
        BEGIN
            SELECT scope.input_id INTO v_input_id
            FROM public.video_tracker_input_scopes AS scope
            WHERE scope.tracker_job_id = p_root_id::uuid;
            IF NOT FOUND THEN RETURN p_fail_closed; END IF;
            v_reply := aap_labeling_v1.video_tracker_input_referenced(
                v_input_id
            );
            IF v_reply ->> 'schema'
                   IS DISTINCT FROM 'aap-labeling-video-tracker-inputs/v1'
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
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_root_entry_live(
    text, text, timestamptz, boolean
) FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1.register_tracker_running_mask_root(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job_id uuid;
    v_task_id uuid;
    v_object_key text;
    v_job_task_id uuid;
    v_status text;
    v_cancel_requested_at timestamptz;
    v_epoch bigint;
    v_active_operation uuid;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR (p_payload - ARRAY['job_id', 'task_id', 'object_key'])
            IS DISTINCT FROM '{}'::jsonb
       OR pg_catalog.jsonb_object_length(p_payload) <> 3
       OR pg_catalog.jsonb_typeof(p_payload -> 'job_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_payload -> 'task_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_payload -> 'object_key') IS DISTINCT FROM 'string' THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;

    v_job_id := (p_payload ->> 'job_id')::uuid;
    v_task_id := (p_payload ->> 'task_id')::uuid;
    v_object_key := p_payload ->> 'object_key';
    IF v_job_id::text IS DISTINCT FROM p_payload ->> 'job_id'
       OR v_task_id::text IS DISTINCT FROM p_payload ->> 'task_id'
       OR NOT aap_labeling_v1._mask_gc_key_valid(v_object_key) THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;

    SELECT job.task_id, job.status, job.cancel_requested_at
    INTO v_job_task_id, v_status, v_cancel_requested_at
    FROM public.video_tracker_jobs AS job
    WHERE job.id = v_job_id
    -- Finalization locks Job before its staged Mask keys; NOWAIT avoids waiting
    -- while this transaction already owns the generated Mask key permit.
    FOR SHARE NOWAIT;
    IF NOT FOUND
       OR v_job_task_id IS DISTINCT FROM v_task_id
       OR v_status IS DISTINCT FROM 'running'
       OR v_cancel_requested_at IS NOT NULL
       OR NOT EXISTS (SELECT 1 FROM public.tasks AS task WHERE task.id = v_task_id) THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'lease_lost');
    END IF;

    PERFORM aap_labeling_v1._mask_gc_lock_keys(ARRAY[v_object_key]);
    SELECT epoch.epoch, epoch.active_operation_id
    INTO v_epoch, v_active_operation
    FROM public.mask_gc_key_epochs AS epoch
    WHERE epoch.object_key = v_object_key;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'content_epoch_changed');
    END IF;
    IF v_active_operation IS NOT NULL THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'content_deleting');
    END IF;
    IF NOT aap_labeling_v1._mask_gc_permit_is_current(v_object_key, v_epoch) THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'content_epoch_changed');
    END IF;

    INSERT INTO public.mask_gc_roots(root_kind, root_id, object_key)
    VALUES ('video_tracker_running_outputs', v_job_id::text, v_object_key)
    ON CONFLICT (root_kind, root_id, object_key) DO NOTHING;

    RETURN aap_labeling_v1._mask_gc_reply(
        pg_catalog.jsonb_build_object(
            'job_id', v_job_id,
            'task_id', v_task_id,
            'object_key', v_object_key,
            'registered', true
        ),
        NULL
    );
EXCEPTION
WHEN lock_not_available THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'storage_unavailable');
WHEN invalid_text_representation OR numeric_value_out_of_range THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.register_tracker_running_mask_root(jsonb)
FROM PUBLIC;
-- aap-statement-break

CREATE FUNCTION aap_labeling_v1._mask_gc_release_tracker_running_outputs()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_keys text[];
    v_job_id uuid;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.status = 'running' THEN
        RETURN NEW;
    END IF;
    v_job_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
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
REVOKE ALL ON FUNCTION aap_labeling_v1._mask_gc_release_tracker_running_outputs()
FROM PUBLIC;
-- aap-statement-break

CREATE TRIGGER trg_mask_gc_release_tracker_running_outputs
AFTER UPDATE OF status OR DELETE ON public.video_tracker_jobs
FOR EACH ROW EXECUTE FUNCTION
    aap_labeling_v1._mask_gc_release_tracker_running_outputs();
