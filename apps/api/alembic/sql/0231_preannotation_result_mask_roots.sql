-- Keep complete pending prediction snapshots live until their native bundle is
-- applied or retired. The payload and bundle tables are both Labeling-owned.
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
BEGIN
    IF p_root_kind = 'batch_result_payloads' THEN
        BEGIN
            -- GC serializes these checks under sorted object-key advisory locks;
            -- taking payload or bundle row locks here would invert writer order.
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
