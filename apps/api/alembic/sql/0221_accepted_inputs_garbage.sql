-- Cleanup sees only durable attempt prefixes; tombstones rescan for late versions.
CREATE FUNCTION aap_assets_v1.claim_accepted_input_garbage(
    p_worker_id uuid, p_lease_seconds integer, p_page_size integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.asset_accepted_input_garbage%ROWTYPE;
    v_candidate record;
    v_reply jsonb;
    v_now timestamptz;
    v_expires timestamptz;
    v_token uuid;
    v_items jsonb := '[]'::jsonb;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds NOT BETWEEN 60 AND 7200
       OR p_page_size NOT BETWEEN 1 AND 100 THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    v_reply := aap_assets_v1.sweep_accepted_image_inputs(p_page_size);
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-inputs/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_reply #> '{data,retired}') IS DISTINCT FROM 'number' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
    END IF;
    FOR v_candidate IN
        SELECT garbage.garbage_id
        FROM public.asset_accepted_input_garbage AS garbage
        WHERE garbage.not_before <= pg_catalog.clock_timestamp()
          AND (garbage.lease_token IS NULL
               OR garbage.lease_expires_at <= pg_catalog.clock_timestamp())
        ORDER BY garbage.not_before, garbage.garbage_id
        LIMIT p_page_size
        FOR UPDATE OF garbage SKIP LOCKED
    LOOP
        SELECT garbage.* INTO v_item
        FROM public.asset_accepted_input_garbage AS garbage
        WHERE garbage.garbage_id = v_candidate.garbage_id;
        IF NOT aap_assets_v1._accepted_image_input_prefix_valid(v_item.prefix) THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
        END IF;
        v_now := pg_catalog.clock_timestamp();
        v_expires := v_now + pg_catalog.make_interval(secs => p_lease_seconds);
        v_token := pg_catalog.gen_random_uuid();
        UPDATE public.asset_accepted_input_garbage AS garbage
        SET lease_owner = p_worker_id,
            lease_token = v_token,
            lease_expires_at = v_expires
        WHERE garbage.garbage_id = v_item.garbage_id
        RETURNING * INTO v_item;
        v_items := v_items || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'garbage_id', v_item.garbage_id,
                'prefix', v_item.prefix,
                'lease_token', v_token
            )
        );
    END LOOP;
    RETURN aap_assets_v1._accepted_image_input_reply(
        pg_catalog.jsonb_build_object('items', v_items), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_accepted_input_garbage(uuid, integer, integer)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_accepted_input_garbage(
    p_garbage_id uuid, p_lease_token uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.asset_accepted_input_garbage%ROWTYPE;
BEGIN
    IF p_garbage_id IS NULL OR p_lease_token IS NULL THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT garbage.* INTO v_item
        FROM public.asset_accepted_input_garbage AS garbage
        WHERE garbage.garbage_id = p_garbage_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_missing');
    END IF;
    IF v_item.lease_token IS NULL
       AND v_item.last_ack_token = p_lease_token
       AND v_item.completed_at IS NOT NULL THEN
        RETURN aap_assets_v1._accepted_image_input_reply(
            pg_catalog.jsonb_build_object(
                'garbage_id', p_garbage_id, 'completed', true
            ), NULL
        );
    END IF;
    IF v_item.lease_token IS DISTINCT FROM p_lease_token
       OR v_item.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'lease_lost');
    END IF;
    UPDATE public.asset_accepted_input_garbage AS garbage
    SET lease_owner = NULL,
        lease_token = NULL,
        lease_expires_at = NULL,
        last_ack_token = p_lease_token,
        completed_at = pg_catalog.clock_timestamp(),
        not_before = pg_catalog.clock_timestamp() + interval '30 days'
    WHERE garbage.garbage_id = p_garbage_id;
    RETURN aap_assets_v1._accepted_image_input_reply(
        pg_catalog.jsonb_build_object(
            'garbage_id', p_garbage_id, 'completed', true
        ), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_accepted_input_garbage(uuid, uuid)
    FROM PUBLIC;
