CREATE FUNCTION aap_labeling_v1.inspect_mask_gc_page(p_payload jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone TO 'UTC'
AS $function$
DECLARE
    v_input_after_key text := NULL;
    v_page jsonb;
    v_items jsonb;
    v_page_after_key text;
    v_complete boolean;
    v_include_reference_count boolean := false;
    v_page_count integer;
    v_scanned integer := 0;
    v_eligible integer := 0;
    v_retained integer := 0;
    v_referenced bigint := NULL;
    v_item jsonb;
    v_object_key text;
    v_previous_key text;
    v_last_modified timestamptz;
    v_cutoff timestamptz;
    v_last_write_at timestamptz;
    v_active_operation uuid;
    v_has_recent_upload boolean;
    v_has_active_intent boolean;
BEGIN
    IF p_payload IS NULL
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR pg_catalog.octet_length(p_payload::text) > 1048576 THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;
    IF (p_payload - ARRAY[
            'after_key', 'page', 'include_reference_count'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_payload ? 'page') THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;

    IF p_payload ? 'after_key' THEN
        IF p_payload -> 'after_key' = 'null'::jsonb THEN
            v_input_after_key := NULL;
        ELSIF pg_catalog.jsonb_typeof(p_payload -> 'after_key')
                  IS DISTINCT FROM 'string' THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        ELSE
            v_input_after_key := p_payload ->> 'after_key';
            IF pg_catalog.octet_length(v_input_after_key) NOT BETWEEN 1 AND 1024
               OR v_input_after_key NOT LIKE 'raster-masks/sha256/%' THEN
                RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
            END IF;
        END IF;
    END IF;

    IF p_payload ? 'include_reference_count' THEN
        IF pg_catalog.jsonb_typeof(p_payload -> 'include_reference_count')
               IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        v_include_reference_count :=
            (p_payload ->> 'include_reference_count')::boolean;
    END IF;

    v_page := p_payload -> 'page';
    IF pg_catalog.jsonb_typeof(v_page) IS DISTINCT FROM 'object' THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;
    IF (v_page - ARRAY['items', 'after_key', 'complete'])
           IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_page ?& ARRAY['items', 'after_key', 'complete']) THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_typeof(v_page -> 'items')
           IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_page -> 'complete')
           IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;
    v_items := v_page -> 'items';
    v_page_count := pg_catalog.jsonb_array_length(v_items);
    IF v_page_count > 1000 THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    END IF;

    IF v_page -> 'after_key' = 'null'::jsonb THEN
        v_page_after_key := NULL;
    ELSIF pg_catalog.jsonb_typeof(v_page -> 'after_key')
              IS DISTINCT FROM 'string' THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
    ELSE
        v_page_after_key := v_page ->> 'after_key';
        IF pg_catalog.octet_length(v_page_after_key) NOT BETWEEN 1 AND 1024
           OR v_page_after_key NOT LIKE 'raster-masks/sha256/%' THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
    END IF;
    v_complete := (v_page ->> 'complete')::boolean;

    IF v_page_after_key IS NULL THEN
        IF NOT v_complete OR v_page_count > 0 THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'cursor_conflict');
        END IF;
    ELSIF NOT v_complete
          AND v_input_after_key IS NOT NULL
          AND v_page_after_key COLLATE "C"
                <= v_input_after_key COLLATE "C" THEN
        RETURN aap_labeling_v1._mask_gc_reply(NULL, 'cursor_conflict');
    END IF;

    -- Validate every candidate and the complete physical page boundary first.
    v_previous_key := NULL;
    FOR v_item IN
        SELECT entry.value
        FROM pg_catalog.jsonb_array_elements(v_items)
            WITH ORDINALITY AS entry(value, ordinal)
        ORDER BY entry.ordinal
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object' THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        IF (v_item - ARRAY[
                'object_key', 'etag', 'last_modified', 'byte_size'
           ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_item ?& ARRAY[
                'object_key', 'etag', 'last_modified', 'byte_size'
           ]) THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        IF pg_catalog.jsonb_typeof(v_item -> 'object_key')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_item -> 'etag')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_item -> 'last_modified')
               IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_item -> 'byte_size')
               IS DISTINCT FROM 'number' THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;

        v_object_key := v_item ->> 'object_key';
        IF NOT aap_labeling_v1._mask_gc_key_valid(v_object_key) THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        IF pg_catalog.octet_length(v_item ->> 'etag') NOT BETWEEN 1 AND 255
           OR (v_item ->> 'etag') !~ '^[!-#%-~]+$' THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        IF (v_item ->> 'byte_size') !~ '^(0|[1-9][0-9]*)$'
           OR pg_catalog.char_length(v_item ->> 'byte_size') > 19 THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        IF (v_item ->> 'byte_size')::numeric > 9223372036854775807 THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        IF (v_item ->> 'last_modified') !~
             '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+]00:00)$' THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END IF;
        BEGIN
            v_last_modified := (v_item ->> 'last_modified')::timestamptz;
        EXCEPTION WHEN OTHERS THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_request');
        END;

        IF (v_input_after_key IS NOT NULL
                AND v_object_key COLLATE "C"
                    <= v_input_after_key COLLATE "C")
           OR v_page_after_key IS NULL
           OR v_object_key COLLATE "C" > v_page_after_key COLLATE "C"
           OR (v_previous_key IS NOT NULL
                AND v_object_key COLLATE "C"
                    <= v_previous_key COLLATE "C") THEN
            RETURN aap_labeling_v1._mask_gc_reply(NULL, 'cursor_conflict');
        END IF;
        v_previous_key := v_object_key;
    END LOOP;

    v_cutoff := pg_catalog.clock_timestamp() - interval '24 hours';
    v_scanned := v_page_count;
    FOR v_item IN
        SELECT entry.value
        FROM pg_catalog.jsonb_array_elements(v_items) AS entry(value)
        ORDER BY entry.value ->> 'object_key' COLLATE "C"
    LOOP
        v_object_key := v_item ->> 'object_key';
        v_last_modified := (v_item ->> 'last_modified')::timestamptz;
        IF v_last_modified >= v_cutoff THEN
            v_retained := v_retained + 1;
            CONTINUE;
        END IF;

        v_last_write_at := NULL;
        v_active_operation := NULL;
        SELECT epoch.last_write_at, epoch.active_operation_id
        INTO v_last_write_at, v_active_operation
        FROM public.mask_gc_key_epochs AS epoch
        WHERE epoch.object_key = v_object_key;
        IF v_active_operation IS NOT NULL
           OR (v_last_write_at IS NOT NULL AND v_last_write_at >= v_cutoff) THEN
            v_retained := v_retained + 1;
            CONTINUE;
        END IF;

        SELECT EXISTS (
            SELECT 1
            FROM public.mask_gc_intents AS intent
            WHERE intent.object_key = v_object_key
              AND intent.state <> 'completed'
        ) INTO v_has_active_intent;
        IF v_has_active_intent THEN
            v_retained := v_retained + 1;
            CONTINUE;
        END IF;

        SELECT EXISTS (
            SELECT 1
            FROM public.raster_mask_uploads AS upload
            WHERE upload.object_key = v_object_key
              AND upload.created_at >= v_cutoff
        ) INTO v_has_recent_upload;
        IF v_has_recent_upload THEN
            v_retained := v_retained + 1;
            CONTINUE;
        END IF;

        IF aap_labeling_v1._mask_gc_has_live_root(v_object_key, true) THEN
            v_retained := v_retained + 1;
            CONTINUE;
        END IF;
        v_eligible := v_eligible + 1;
    END LOOP;

    IF v_include_reference_count THEN
        SELECT count(DISTINCT root.object_key)
        INTO v_referenced
        FROM public.mask_gc_roots AS root
        WHERE aap_labeling_v1._mask_gc_root_entry_live(
            root.root_kind, root.root_id, root.valid_until, true
        );
    END IF;

    RETURN aap_labeling_v1._mask_gc_reply(
        pg_catalog.jsonb_build_object(
            'scanned', v_scanned,
            'eligible', v_eligible,
            'retained', v_retained,
            'referenced', v_referenced
        ),
        NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_labeling_v1._mask_gc_reply(NULL, 'invalid_response');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.inspect_mask_gc_page(jsonb) FROM PUBLIC;
