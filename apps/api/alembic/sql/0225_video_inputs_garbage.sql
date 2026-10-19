-- The existing tombstone ledger is shared, while each client owns one prefix partition.
ALTER TABLE public.asset_accepted_input_garbage
    DROP CONSTRAINT IF EXISTS ck_asset_accepted_input_garbage_prefix;
-- aap-statement-break
ALTER TABLE public.asset_accepted_input_garbage
    ADD CONSTRAINT ck_asset_accepted_input_garbage_prefix CHECK (
        aap_assets_v1._accepted_image_input_prefix_valid(prefix)
        OR aap_assets_v1._video_input_prefix_valid(prefix)
    );
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._video_input_queue_prefix(
    p_prefix text, p_reason text, p_not_before timestamptz
)
RETURNS void
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_now timestamptz;
    v_effective_not_before timestamptz;
BEGIN
    IF NOT aap_assets_v1._video_input_prefix_valid(p_prefix)
       OR p_reason IS NULL OR pg_catalog.char_length(p_reason) NOT BETWEEN 1 AND 40
       OR p_not_before IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='22023',
            MESSAGE='invalid accepted video input garbage scope';
    END IF;
    v_now := pg_catalog.clock_timestamp();
    v_effective_not_before := GREATEST(p_not_before,v_now+interval '24 hours');
    INSERT INTO public.asset_accepted_input_garbage(prefix,reason,not_before)
    VALUES (p_prefix,p_reason,v_effective_not_before)
    ON CONFLICT (prefix) DO UPDATE SET not_before=LEAST(
        public.asset_accepted_input_garbage.not_before,EXCLUDED.not_before
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._video_input_queue_prefix(text,text,timestamptz)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.claim_accepted_input_garbage(
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
        RETURN aap_assets_v1._accepted_image_input_reply(NULL,'invalid_request');
    END IF;
    v_reply := aap_assets_v1.sweep_accepted_image_inputs(p_page_size);
    IF v_reply IS NULL OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-inputs/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_reply #> '{data,retired}') IS DISTINCT FROM 'number' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL,'input_busy');
    END IF;
    FOR v_candidate IN
        SELECT garbage.garbage_id
        FROM public.asset_accepted_input_garbage AS garbage
        WHERE aap_assets_v1._accepted_image_input_prefix_valid(garbage.prefix)
          AND garbage.not_before <= pg_catalog.clock_timestamp()
          AND (garbage.lease_token IS NULL OR garbage.lease_expires_at <= pg_catalog.clock_timestamp())
        ORDER BY garbage.not_before,garbage.garbage_id
        LIMIT p_page_size FOR UPDATE OF garbage SKIP LOCKED
    LOOP
        SELECT garbage.* INTO v_item FROM public.asset_accepted_input_garbage AS garbage
        WHERE garbage.garbage_id=v_candidate.garbage_id;
        IF NOT aap_assets_v1._accepted_image_input_prefix_valid(v_item.prefix) THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL,'invalid_response');
        END IF;
        v_now := pg_catalog.clock_timestamp();
        v_expires := v_now + pg_catalog.make_interval(secs=>p_lease_seconds);
        v_token := pg_catalog.gen_random_uuid();
        UPDATE public.asset_accepted_input_garbage AS garbage
        SET lease_owner=p_worker_id,lease_token=v_token,lease_expires_at=v_expires
        WHERE garbage.garbage_id=v_item.garbage_id RETURNING * INTO v_item;
        v_items := v_items || pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
            'garbage_id',v_item.garbage_id,'prefix',v_item.prefix,'lease_token',v_token
        ));
    END LOOP;
    RETURN aap_assets_v1._accepted_image_input_reply(pg_catalog.jsonb_build_object('items',v_items),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL,'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_accepted_input_garbage(uuid,integer,integer)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.complete_accepted_input_garbage(
    p_garbage_id uuid,p_lease_token uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.asset_accepted_input_garbage%ROWTYPE;
BEGIN
    IF p_garbage_id IS NULL OR p_lease_token IS NULL THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL,'invalid_request');
    END IF;
    BEGIN
        SELECT garbage.* INTO v_item FROM public.asset_accepted_input_garbage AS garbage
        WHERE garbage.garbage_id=p_garbage_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL,'input_busy');
    END;
    IF NOT FOUND THEN RETURN aap_assets_v1._accepted_image_input_reply(NULL,'input_missing'); END IF;
    IF NOT aap_assets_v1._accepted_image_input_prefix_valid(v_item.prefix) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL,'input_scope_mismatch');
    END IF;
    IF v_item.lease_token IS NULL AND v_item.last_ack_token=p_lease_token
       AND v_item.completed_at IS NOT NULL THEN
        RETURN aap_assets_v1._accepted_image_input_reply(
            pg_catalog.jsonb_build_object('garbage_id',p_garbage_id,'completed',true),NULL
        );
    END IF;
    IF v_item.lease_token IS DISTINCT FROM p_lease_token
       OR v_item.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL,'lease_lost');
    END IF;
    UPDATE public.asset_accepted_input_garbage AS garbage
    SET lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_ack_token=p_lease_token,
        completed_at=pg_catalog.clock_timestamp(),
        not_before=pg_catalog.clock_timestamp()+interval '30 days'
    WHERE garbage.garbage_id=p_garbage_id
      AND garbage.lease_token=p_lease_token
      AND aap_assets_v1._accepted_image_input_prefix_valid(garbage.prefix);
    IF NOT FOUND THEN RETURN aap_assets_v1._accepted_image_input_reply(NULL,'lease_lost'); END IF;
    RETURN aap_assets_v1._accepted_image_input_reply(
        pg_catalog.jsonb_build_object('garbage_id',p_garbage_id,'completed',true),NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL,'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_accepted_input_garbage(uuid,uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.claim_accepted_video_input_garbage(
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
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    v_reply := aap_assets_v1.sweep_accepted_video_inputs(p_page_size);
    IF v_reply IS NULL OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-video-input-execution/v1'
       OR v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_reply #> '{data,expired}') IS DISTINCT FROM 'number' THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
    END IF;
    FOR v_candidate IN
        SELECT garbage.garbage_id
        FROM public.asset_accepted_input_garbage AS garbage
        WHERE aap_assets_v1._video_input_prefix_valid(garbage.prefix)
          AND garbage.not_before <= pg_catalog.clock_timestamp()
          AND (garbage.lease_token IS NULL OR garbage.lease_expires_at <= pg_catalog.clock_timestamp())
        ORDER BY garbage.not_before,garbage.garbage_id
        LIMIT p_page_size FOR UPDATE OF garbage SKIP LOCKED
    LOOP
        SELECT garbage.* INTO v_item FROM public.asset_accepted_input_garbage AS garbage
        WHERE garbage.garbage_id=v_candidate.garbage_id;
        IF NOT aap_assets_v1._video_input_prefix_valid(v_item.prefix) THEN
            RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
        END IF;
        v_now := pg_catalog.clock_timestamp();
        v_expires := v_now + pg_catalog.make_interval(secs=>p_lease_seconds);
        v_token := pg_catalog.gen_random_uuid();
        UPDATE public.asset_accepted_input_garbage AS garbage
        SET lease_owner=p_worker_id,lease_token=v_token,lease_expires_at=v_expires
        WHERE garbage.garbage_id=v_item.garbage_id RETURNING * INTO v_item;
        v_items := v_items || pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
            'garbage_id',v_item.garbage_id,'prefix',v_item.prefix,'lease_token',v_token
        ));
    END LOOP;
    RETURN aap_assets_v1._video_input_reply(pg_catalog.jsonb_build_object('items',v_items),NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_accepted_video_input_garbage(uuid,integer,integer)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.complete_accepted_video_input_garbage(
    p_garbage_id uuid,p_lease_token uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.asset_accepted_input_garbage%ROWTYPE;
BEGIN
    IF p_garbage_id IS NULL OR p_lease_token IS NULL THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'invalid_request');
    END IF;
    BEGIN
        SELECT garbage.* INTO v_item FROM public.asset_accepted_input_garbage AS garbage
        WHERE garbage.garbage_id=p_garbage_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
    END;
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'input_missing'); END IF;
    IF NOT aap_assets_v1._video_input_prefix_valid(v_item.prefix) THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'input_scope_mismatch');
    END IF;
    IF v_item.lease_token IS NULL AND v_item.last_ack_token=p_lease_token
       AND v_item.completed_at IS NOT NULL THEN
        RETURN aap_assets_v1._video_input_reply(
            pg_catalog.jsonb_build_object('garbage_id',p_garbage_id,'completed',true),NULL
        );
    END IF;
    IF v_item.lease_token IS DISTINCT FROM p_lease_token
       OR v_item.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost');
    END IF;
    UPDATE public.asset_accepted_input_garbage AS garbage
    SET lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_ack_token=p_lease_token,
        completed_at=pg_catalog.clock_timestamp(),
        not_before=pg_catalog.clock_timestamp()+interval '30 days'
    WHERE garbage.garbage_id=p_garbage_id
      AND garbage.lease_token=p_lease_token
      AND aap_assets_v1._video_input_prefix_valid(garbage.prefix);
    IF NOT FOUND THEN RETURN aap_assets_v1._video_input_reply(NULL,'lease_lost'); END IF;
    RETURN aap_assets_v1._video_input_reply(
        pg_catalog.jsonb_build_object('garbage_id',p_garbage_id,'completed',true),NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._video_input_reply(NULL,'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.complete_accepted_video_input_garbage(uuid,uuid)
    FROM PUBLIC;
