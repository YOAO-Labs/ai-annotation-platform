CREATE OR REPLACE FUNCTION aap_assets_v1.advance_image_backfill(
    p_job_id uuid, p_generation bigint, p_lease_token uuid,
    p_batch_size integer, p_observations jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_candidate public.asset_image_jobs%ROWTYPE;
    v_job public.asset_image_jobs%ROWTYPE;
    v_progress jsonb;
    v_pending jsonb;
    v_existing_pending jsonb;
    v_spec jsonb;
    v_selection jsonb;
    v_page_reply jsonb;
    v_page jsonb;
    v_page_items jsonb;
    v_observation jsonb;
    v_candidate_source jsonb;
    v_owner_reply jsonb;
    v_owner_data jsonb;
    v_owner_id uuid;
    v_owner_kind text;
    v_source_snapshot text;
    v_identity jsonb;
    v_error_code text;
    v_child_spec jsonb;
    v_enqueue jsonb;
    v_enqueue_data jsonb;
    v_source_error text;
    v_snapshot_digest text;
    v_observations_digest text;
    v_total bigint;
    v_scheduled bigint;
    v_skipped bigint;
    v_page_scheduled bigint := 0;
    v_page_skipped bigint := 0;
    v_batch_token uuid;
    v_next_kind text;
    v_next_id uuid;
    v_complete boolean;
    v_only_missing boolean;
    v_purpose text;
    v_child_kind text;
    v_need_work boolean;
    v_asset_id uuid;
    v_active_generation integer;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_data jsonb;
    v_saved_result jsonb;
    v_failure text;
    v_cursor_kind text;
    v_cursor_id uuid;
    v_accepted_before timestamptz;
    v_item_count integer;
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL OR p_batch_size IS NULL OR p_batch_size NOT BETWEEN 1 AND 500
       OR (p_observations IS NOT NULL AND (
            pg_catalog.jsonb_typeof(p_observations) IS DISTINCT FROM 'object'
            OR pg_catalog.octet_length(pg_catalog.convert_to(p_observations::text, 'UTF8')) > 8388608
       )) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT job.* INTO v_candidate
    FROM public.asset_image_jobs AS job
    WHERE job.job_id = p_job_id;
    IF NOT FOUND OR v_candidate.kind <> 'image_backfill' THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    v_progress := COALESCE(v_candidate.progress, '{}'::jsonb);
    IF p_observations IS NOT NULL THEN
        v_observations_digest := pg_catalog.encode(
            pg_catalog.sha256(pg_catalog.convert_to(p_observations::text, 'UTF8')),
            'hex'
        );
        IF v_progress ->> 'last_advance_lease_token' = p_lease_token::text
           AND v_progress ->> 'last_advance_batch_token' = p_observations ->> 'batch_token'
           AND pg_catalog.jsonb_typeof(v_progress -> 'last_advance_result') = 'object' THEN
            IF v_progress ->> 'last_advance_observations_digest'
                    IS DISTINCT FROM v_observations_digest THEN
                RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
            END IF;
            RETURN aap_assets_v1._image_reply(v_progress -> 'last_advance_result', NULL);
        END IF;
    END IF;
    IF p_observations IS NULL
       AND v_progress ->> 'last_advance_lease_token' = p_lease_token::text
       AND v_progress ->> 'last_advance_batch_size' = p_batch_size::text
       AND pg_catalog.jsonb_typeof(v_progress -> 'last_advance_result') = 'object'
       AND v_candidate.status = 'ready' THEN
        RETURN aap_assets_v1._image_reply(v_progress -> 'last_advance_result', NULL);
    END IF;
    IF v_candidate.status <> 'running'
       OR v_candidate.generation <> p_generation
       OR v_candidate.lease_token IS DISTINCT FROM p_lease_token
       OR v_candidate.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    v_spec := aap_assets_v1._image_spec(v_candidate.spec);
    IF v_spec IS NULL OR v_spec IS DISTINCT FROM v_candidate.spec
       OR v_candidate.backfill_cutoff IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_selection := pg_catalog.jsonb_set(
        v_spec -> 'selection', '{accepted_before}',
        pg_catalog.to_jsonb(v_candidate.backfill_cutoff), true
    );
    v_purpose := v_selection ->> 'purpose';
    v_only_missing := COALESCE(v_selection -> 'only_missing' = 'true'::jsonb, true);
    IF v_purpose NOT IN ('dimensions', 'thumbnail', 'pyramid') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;

    IF p_observations IS NULL THEN
        IF pg_catalog.jsonb_typeof(v_progress -> 'pending_batch') = 'object'
           AND v_progress #>> '{pending_batch,lease_token}' = p_lease_token::text
           AND v_progress #>> '{pending_batch,batch_size}' = p_batch_size::text THEN
            v_pending := v_progress -> 'pending_batch';
            RETURN aap_assets_v1._image_reply(
                pg_catalog.jsonb_build_object(
                    'job_id', p_job_id,
                    'generation', p_generation,
                    'lease_token', p_lease_token,
                    'scheduled', 0,
                    'skipped', 0,
                    'complete', false,
                    'next_owner_kind', v_pending -> 'next_owner_kind',
                    'next_owner_id', v_pending -> 'next_owner_id',
                    'batch_token', v_pending -> 'batch_token',
                    'candidates', v_pending -> 'items',
                    'needs_observation', true
                ), NULL
            );
        END IF;
        v_page_reply := aap_assets_v1.scan_image_backfill(
            v_selection,
            v_candidate.backfill_cursor_kind,
            v_candidate.backfill_cursor_id,
            p_batch_size
        );
        IF v_page_reply IS NULL
           OR v_page_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_page_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_page_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN v_page_reply;
        END IF;
        v_page := v_page_reply -> 'data';
        IF NOT aap_assets_v1._media_object_has_only(
            v_page, ARRAY[
                'items', 'snapshot_digest', 'total', 'accepted_before',
                'after_owner_kind', 'after_owner_id', 'next_owner_kind', 'next_owner_id', 'complete'
            ]
        ) OR NOT (v_page ?& ARRAY[
            'items', 'snapshot_digest', 'total', 'accepted_before',
            'after_owner_kind', 'after_owner_id', 'next_owner_kind', 'next_owner_id', 'complete'
        ]) OR pg_catalog.jsonb_typeof(v_page -> 'items') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(v_page -> 'items') > p_batch_size
           OR pg_catalog.jsonb_typeof(v_page -> 'snapshot_digest') IS DISTINCT FROM 'string'
           OR v_page ->> 'snapshot_digest' !~ '^[0-9a-f]{64}$'
           OR NOT aap_assets_v1._image_exec_integer_valid(v_page -> 'total', 0, 9223372036854775807)
           OR pg_catalog.jsonb_typeof(v_page -> 'accepted_before') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(v_page -> 'complete') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        BEGIN
            v_accepted_before := (v_page ->> 'accepted_before')::timestamptz;
        EXCEPTION WHEN invalid_text_representation OR datetime_field_overflow THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
        IF v_accepted_before IS DISTINCT FROM v_candidate.backfill_cutoff
           OR ((v_page -> 'after_owner_kind' = 'null'::jsonb)
                <> (v_page -> 'after_owner_id' = 'null'::jsonb))
           OR ((v_page -> 'next_owner_kind' = 'null'::jsonb)
                <> (v_page -> 'next_owner_id' = 'null'::jsonb))
           OR (v_page -> 'after_owner_kind' <> 'null'::jsonb
               AND (v_page ->> 'after_owner_kind' IS DISTINCT FROM v_candidate.backfill_cursor_kind
                    OR (v_page ->> 'after_owner_id')::uuid IS DISTINCT FROM v_candidate.backfill_cursor_id)) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_page_items := v_page -> 'items';
        v_item_count := pg_catalog.jsonb_array_length(v_page_items);
        IF v_item_count = 0 AND v_page -> 'complete' = 'false'::jsonb THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_item_count > 0 AND v_page -> 'next_owner_kind' <> 'null'::jsonb
           AND v_page ->> 'next_owner_kind' IS DISTINCT FROM v_selection ->> 'owner_kind' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        FOR v_candidate_source IN
            SELECT item.value FROM pg_catalog.jsonb_array_elements(v_page_items) AS item(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_candidate_source) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_candidate_source -> 'owner') IS DISTINCT FROM 'object'
               OR v_candidate_source #>> '{owner,kind}' IS DISTINCT FROM v_selection ->> 'owner_kind'
               OR pg_catalog.jsonb_typeof(v_candidate_source #> '{owner,id}') IS DISTINCT FROM 'string'
               OR pg_catalog.jsonb_typeof(v_candidate_source -> 'source_snapshot') IS DISTINCT FROM 'string'
               OR v_candidate_source ->> 'source_snapshot' !~ '^[0-9a-f]{64}$'
               OR pg_catalog.jsonb_typeof(v_candidate_source -> 'file_type') IS DISTINCT FROM 'string'
               OR NOT aap_assets_v1._image_exec_integer_valid(
                    v_candidate_source -> 'source_generation', 1, 9223372036854775807
               ) THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            BEGIN
                v_owner_id := (v_candidate_source #>> '{owner,id}')::uuid;
                PERFORM (v_candidate_source ->> 'source_generation')::bigint;
            EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END;
        END LOOP;
        v_snapshot_digest := v_page ->> 'snapshot_digest';
        v_total := (v_page ->> 'total')::bigint;
        v_scheduled := COALESCE((v_progress ->> 'scheduled')::bigint, 0);
        v_skipped := COALESCE((v_progress ->> 'skipped')::bigint, 0);
        IF v_item_count = 0 AND v_page -> 'complete' = 'true'::jsonb THEN
            v_data := pg_catalog.jsonb_build_object(
                'job_id', p_job_id,
                'generation', p_generation,
                'lease_token', p_lease_token,
                'scheduled', v_scheduled,
                'skipped', v_skipped,
                'complete', true,
                'next_owner_kind', NULL,
                'next_owner_id', NULL,
                'batch_token', NULL,
                'candidates', '[]'::jsonb,
                'needs_observation', false
            );
            v_progress := v_progress || pg_catalog.jsonb_build_object(
                'snapshot_digest', v_snapshot_digest, 'total', v_total,
                'scheduled', v_scheduled, 'skipped', v_skipped,
                'last_advance_lease_token', p_lease_token,
                'last_advance_batch_size', p_batch_size,
                'last_advance_result', v_data
            );
            BEGIN
                SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
                WHERE job.job_id = p_job_id FOR UPDATE NOWAIT;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
            END;
            IF NOT FOUND OR v_job.status <> 'running'
               OR v_job.generation <> p_generation
               OR v_job.lease_token IS DISTINCT FROM p_lease_token
               OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
                RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
            END IF;
            UPDATE public.asset_image_jobs AS job
            SET status = 'ready', progress = v_progress,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = p_lease_token, last_outcome = 'published',
                completed_at = pg_catalog.clock_timestamp(), updated_at = pg_catalog.clock_timestamp()
            WHERE job.job_id = p_job_id;
            RETURN aap_assets_v1._image_reply(v_data, NULL);
        END IF;
        v_batch_token := pg_catalog.gen_random_uuid();
        v_pending := pg_catalog.jsonb_build_object(
            'batch_token', v_batch_token,
            'lease_token', p_lease_token,
            'batch_size', p_batch_size,
            'items', v_page_items,
            'after_owner_kind', v_page -> 'after_owner_kind',
            'after_owner_id', v_page -> 'after_owner_id',
            'next_owner_kind', v_page -> 'next_owner_kind',
            'next_owner_id', v_page -> 'next_owner_id',
            'complete', v_page -> 'complete',
            'snapshot_digest', v_snapshot_digest,
            'total', v_total
        );
        v_progress := v_progress || pg_catalog.jsonb_build_object(
            'snapshot_digest', v_snapshot_digest,
            'total', v_total,
            'scheduled', v_scheduled,
            'skipped', v_skipped,
            'pending_batch', v_pending
        );
        BEGIN
            SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
            WHERE job.job_id = p_job_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        IF NOT FOUND OR v_job.status <> 'running'
           OR v_job.generation <> p_generation
           OR v_job.lease_token IS DISTINCT FROM p_lease_token
           OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
            IF FOUND AND v_job.status = 'ready'
               AND v_job.generation = p_generation
               AND v_job.progress ->> 'last_advance_lease_token' = p_lease_token::text
               AND v_job.progress ->> 'last_advance_batch_size' = p_batch_size::text
               AND pg_catalog.jsonb_typeof(v_job.progress -> 'last_advance_result') = 'object' THEN
                RETURN aap_assets_v1._image_reply(
                    v_job.progress -> 'last_advance_result', NULL
                );
            END IF;
            RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
        END IF;
        v_existing_pending := v_job.progress -> 'pending_batch';
        IF pg_catalog.jsonb_typeof(v_existing_pending) = 'object'
           AND v_existing_pending ->> 'lease_token' = p_lease_token::text THEN
            IF v_existing_pending ->> 'batch_size' IS DISTINCT FROM p_batch_size::text THEN
                RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
            END IF;
            v_data := pg_catalog.jsonb_build_object(
                'job_id', p_job_id,
                'generation', p_generation,
                'lease_token', p_lease_token,
                'scheduled', COALESCE((v_job.progress ->> 'scheduled')::bigint, 0),
                'skipped', COALESCE((v_job.progress ->> 'skipped')::bigint, 0),
                'complete', false,
                'next_owner_kind', v_existing_pending -> 'next_owner_kind',
                'next_owner_id', v_existing_pending -> 'next_owner_id',
                'batch_token', v_existing_pending -> 'batch_token',
                'candidates', v_existing_pending -> 'items',
                'needs_observation', true
            );
            RETURN aap_assets_v1._image_reply(v_data, NULL);
        END IF;
        v_data := pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'generation', p_generation,
            'lease_token', p_lease_token,
            'scheduled', 0,
            'skipped', 0,
            'complete', false,
            'next_owner_kind', v_pending -> 'next_owner_kind',
            'next_owner_id', v_pending -> 'next_owner_id',
            'batch_token', v_batch_token,
            'candidates', v_page_items,
            'needs_observation', true
        );
        IF pg_catalog.octet_length(pg_catalog.convert_to(v_data::text, 'UTF8')) > 15728640 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'resource_budget_exceeded');
        END IF;
        UPDATE public.asset_image_jobs AS job
        SET progress = v_progress, updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
        RETURN aap_assets_v1._image_reply(v_data, NULL);
    END IF;

    -- Validate the observation batch and its one-to-one relation with the staged page.
    IF NOT aap_assets_v1._media_object_has_only(
        p_observations, ARRAY['batch_token', 'items']
    ) OR NOT (p_observations ?& ARRAY['batch_token', 'items'])
       OR pg_catalog.jsonb_typeof(p_observations -> 'batch_token') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_observations -> 'items') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_observations -> 'items') NOT BETWEEN 1 AND p_batch_size THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        v_batch_token := (p_observations ->> 'batch_token')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END;
    v_pending := v_progress -> 'pending_batch';
    IF pg_catalog.jsonb_typeof(v_pending) IS DISTINCT FROM 'object'
       OR v_pending ->> 'batch_token' IS DISTINCT FROM v_batch_token::text
       OR v_pending ->> 'lease_token' IS DISTINCT FROM p_lease_token::text
       OR v_pending ->> 'batch_size' IS DISTINCT FROM p_batch_size::text THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
    END IF;
    v_page_items := v_pending -> 'items';
    IF pg_catalog.jsonb_array_length(v_page_items)
       <> pg_catalog.jsonb_array_length(p_observations -> 'items') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(p_observations -> 'items') AS entry(value)
        WHERE NOT aap_assets_v1._media_object_has_only(
            entry.value, ARRAY['owner', 'source_snapshot', 'object_identity', 'error_code']
        ) OR NOT (entry.value ?& ARRAY['owner', 'source_snapshot', 'object_identity', 'error_code'])
          OR pg_catalog.jsonb_typeof(entry.value -> 'owner') IS DISTINCT FROM 'object'
          OR NOT aap_assets_v1._media_object_has_only(
                entry.value -> 'owner', ARRAY['kind', 'id']
          ) OR NOT (entry.value -> 'owner' ?& ARRAY['kind', 'id'])
          OR entry.value #>> '{owner,kind}' IS DISTINCT FROM v_selection ->> 'owner_kind'
          OR pg_catalog.jsonb_typeof(entry.value #> '{owner,id}') IS DISTINCT FROM 'string'
          OR pg_catalog.jsonb_typeof(entry.value -> 'source_snapshot') IS DISTINCT FROM 'string'
          OR entry.value ->> 'source_snapshot' !~ '^[0-9a-f]{64}$'
          OR (entry.value -> 'object_identity' = 'null'::jsonb)
                = (entry.value -> 'error_code' = 'null'::jsonb)
          OR (entry.value -> 'object_identity' <> 'null'::jsonb
              AND NOT aap_assets_v1._media_object_identity_valid(entry.value -> 'object_identity'))
          OR (entry.value -> 'error_code' <> 'null'::jsonb
              AND (pg_catalog.jsonb_typeof(entry.value -> 'error_code') IS DISTINCT FROM 'string'
                   OR entry.value ->> 'error_code' NOT IN ('source_missing', 'source_changed')))
    ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(v_page_items) AS candidate(value)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(p_observations -> 'items') AS observed(value)
            WHERE observed.value #>> '{owner,id}' = candidate.value #>> '{owner,id}'
              AND observed.value #>> '{owner,kind}' = candidate.value #>> '{owner,kind}'
              AND observed.value ->> 'source_snapshot' = candidate.value ->> 'source_snapshot'
        )
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(p_observations -> 'items') AS observed(value)
        WHERE NOT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(v_page_items) AS candidate(value)
            WHERE candidate.value #>> '{owner,id}' = observed.value #>> '{owner,id}'
              AND candidate.value #>> '{owner,kind}' = observed.value #>> '{owner,kind}'
              AND candidate.value ->> 'source_snapshot' = observed.value ->> 'source_snapshot'
        )
    ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;

    v_snapshot_digest := v_pending ->> 'snapshot_digest';
    v_total := (v_pending ->> 'total')::bigint;
    v_scheduled := COALESCE((v_progress ->> 'scheduled')::bigint, 0);
    v_skipped := COALESCE((v_progress ->> 'skipped')::bigint, 0);
    v_accepted_before := v_candidate.backfill_cutoff;
    v_cursor_kind := v_pending ->> 'after_owner_kind';
    IF v_pending -> 'after_owner_id' <> 'null'::jsonb THEN
        BEGIN v_cursor_id := (v_pending ->> 'after_owner_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
    END IF;
    v_next_kind := v_pending ->> 'next_owner_kind';
    IF v_pending -> 'next_owner_id' <> 'null'::jsonb THEN
        BEGIN v_next_id := (v_pending ->> 'next_owner_id')::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
    END IF;
    v_complete := v_pending -> 'complete' = 'true'::jsonb;
    v_owner_kind := v_selection ->> 'owner_kind';
    v_purpose := v_selection ->> 'purpose';
    v_only_missing := COALESCE(v_selection -> 'only_missing' = 'true'::jsonb, true);

    BEGIN
        -- Observe and lock the complete staged batch before admitting children.
        -- Drifted, removed and relinked items are skipped in the second pass;
        -- source contention rolls this whole savepoint back for a retry.
        FOR v_observation IN
            SELECT entry.value
            FROM pg_catalog.jsonb_array_elements(p_observations -> 'items') AS entry(value)
            ORDER BY entry.value #>> '{owner,kind}', (entry.value #>> '{owner,id}')::uuid
        LOOP
            v_owner_kind := v_observation #>> '{owner,kind}';
            v_owner_id := (v_observation #>> '{owner,id}')::uuid;
            v_source_snapshot := v_observation ->> 'source_snapshot';
            v_identity := v_observation -> 'object_identity';
            IF v_identity IS NOT NULL AND v_identity <> 'null'::jsonb THEN
                v_owner_reply := aap_assets_v1._image_observe_owner(
                    v_owner_kind, v_owner_id, v_source_snapshot, v_identity
                );
            ELSE
                v_owner_reply := aap_assets_v1._image_resolve_owner(
                    v_owner_kind, v_owner_id, true
                );
            END IF;
            IF v_owner_reply IS NULL
               OR v_owner_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
               OR pg_catalog.jsonb_typeof(v_owner_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            IF v_owner_reply -> 'ok' = 'false'::jsonb THEN
                v_source_error := v_owner_reply #>> '{error,code}';
                IF v_source_error IN ('source_busy', 'task_busy') THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_source_busy';
                ELSIF v_source_error IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                ELSIF v_source_error NOT IN (
                    'source_missing', 'source_changed', 'owner_changed',
                    'source_not_ready', 'not_image'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                END IF;
                CONTINUE;
            END IF;
            v_owner_data := v_owner_reply -> 'data';
            IF NOT aap_assets_v1._image_source_data_valid(v_owner_data)
               OR v_owner_data #>> '{owner,kind}' IS DISTINCT FROM v_owner_kind
               OR v_owner_data #>> '{owner,id}' IS DISTINCT FROM v_owner_id::text THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
        END LOOP;

        FOR v_observation IN
            SELECT entry.value
            FROM pg_catalog.jsonb_array_elements(p_observations -> 'items') AS entry(value)
            ORDER BY entry.value #>> '{owner,kind}', (entry.value #>> '{owner,id}')::uuid
        LOOP
            v_source_error := NULL;
            v_owner_kind := v_observation #>> '{owner,kind}';
            v_owner_id := (v_observation #>> '{owner,id}')::uuid;
            v_source_snapshot := v_observation ->> 'source_snapshot';
            v_identity := v_observation -> 'object_identity';
            v_error_code := v_observation ->> 'error_code';
            v_owner_data := NULL;
            IF v_identity IS NOT NULL AND v_identity <> 'null'::jsonb THEN
                v_owner_reply := aap_assets_v1._image_observe_owner(
                    v_owner_kind, v_owner_id, v_source_snapshot, v_identity
                );
            ELSE
                v_owner_reply := aap_assets_v1._image_resolve_owner(
                    v_owner_kind, v_owner_id, true
                );
            END IF;
            IF v_owner_reply IS NULL
               OR v_owner_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
               OR pg_catalog.jsonb_typeof(v_owner_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            IF v_owner_reply -> 'ok' = 'true'::jsonb THEN
                v_owner_data := v_owner_reply -> 'data';
                IF pg_catalog.jsonb_typeof(v_owner_data) IS DISTINCT FROM 'object'
                   OR v_owner_data #>> '{owner,kind}' IS DISTINCT FROM v_owner_kind
                   OR v_owner_data #>> '{owner,id}' IS DISTINCT FROM v_owner_id::text THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                END IF;
            ELSE
                v_source_error := v_owner_reply #>> '{error,code}';
                IF v_source_error IN ('source_busy', 'task_busy') THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_source_busy';
                ELSIF v_source_error IS NULL OR v_source_error NOT IN (
                    'source_missing', 'source_changed', 'owner_changed',
                    'source_not_ready', 'not_image'
                ) THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
                END IF;
            END IF;
            IF v_owner_data IS NULL
               OR v_source_error IN ('source_missing', 'source_changed', 'owner_changed', 'not_image')
               OR v_observation -> 'error_code' <> 'null'::jsonb
               OR v_owner_data -> 'source_drifted' = 'true'::jsonb
               OR v_owner_data -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
               OR v_owner_data -> 'file_type' IS DISTINCT FROM '"image"'::jsonb
               OR v_owner_data ->> 'source_snapshot' IS DISTINCT FROM v_source_snapshot
               OR (v_identity IS NOT NULL AND v_identity <> 'null'::jsonb
                   AND NOT aap_assets_v1._image_exec_identity_equal(
                        v_owner_data -> 'object_identity', v_identity
                   )) THEN
                v_page_skipped := v_page_skipped + 1;
                v_source_error := NULL;
                CONTINUE;
            END IF;
            v_child_kind := v_purpose;
            v_need_work := true;
            IF v_only_missing AND v_need_work THEN
                IF v_child_kind = 'dimensions' THEN
                    v_need_work := v_owner_data -> 'width' = 'null'::jsonb
                        OR v_owner_data -> 'height' = 'null'::jsonb;
                ELSIF v_child_kind = 'thumbnail' THEN
                    v_need_work := v_owner_data -> 'thumbnail_path' = 'null'::jsonb
                        OR v_owner_data -> 'thumbnail_source_generation' = 'null'::jsonb
                        OR (v_owner_data ->> 'thumbnail_source_generation')::bigint
                            <> (v_owner_data ->> 'source_generation')::bigint;
                ELSIF v_child_kind = 'pyramid' THEN
                    IF v_owner_kind = 'dataset_item' THEN
                        SELECT asset.id, asset.active_generation INTO v_asset_id, v_active_generation
                        FROM public.image_pyramid_assets AS asset
                        WHERE asset.dataset_item_id = v_owner_id
                          AND asset.profile_version = v_spec #>> '{policy,profile_version}';
                    ELSE
                        SELECT asset.id, asset.active_generation INTO v_asset_id, v_active_generation
                        FROM public.image_pyramid_assets AS asset
                        WHERE asset.task_id = v_owner_id
                          AND asset.profile_version = v_spec #>> '{policy,profile_version}';
                    END IF;
                    IF v_asset_id IS NOT NULL AND v_active_generation IS NOT NULL THEN
                        SELECT generation.* INTO v_generation
                        FROM public.image_pyramid_generations AS generation
                        WHERE generation.asset_id = v_asset_id
                          AND generation.generation = v_active_generation
                          AND generation.status = 'ready'
                          AND generation.source_generation = (v_owner_data ->> 'source_generation')::bigint
                          AND generation.source_snapshot = v_owner_data ->> 'source_snapshot';
                        v_need_work := NOT FOUND;
                    END IF;
                END IF;
            END IF;
            IF NOT v_need_work THEN
                v_page_skipped := v_page_skipped + 1;
                CONTINUE;
            END IF;
            v_child_spec := pg_catalog.jsonb_build_object(
                'kind', v_child_kind,
                'expected_source_snapshot', v_owner_data -> 'source_snapshot',
                'policy', v_spec -> 'policy'
            );
            v_enqueue := aap_assets_v1._image_enqueue(
                NULL, NULL, v_owner_data, v_child_spec, NOT v_only_missing
            );
            IF v_enqueue IS NULL
               OR v_enqueue ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
               OR pg_catalog.jsonb_typeof(v_enqueue -> 'ok') IS DISTINCT FROM 'boolean' THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            IF v_enqueue -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                v_source_error := v_enqueue #>> '{error,code}';
                IF v_source_error IN ('source_busy', 'operation_busy') THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_source_busy';
                ELSIF v_source_error = 'operation_conflict' THEN
                    v_page_skipped := v_page_skipped + 1;
                    CONTINUE;
                ELSIF v_source_error IN (
                    'source_changed', 'source_not_ready', 'source_missing',
                    'owner_changed', 'not_image'
                ) THEN
                    v_page_skipped := v_page_skipped + 1;
                    CONTINUE;
                END IF;
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            v_enqueue_data := v_enqueue -> 'data';
            IF pg_catalog.jsonb_typeof(v_enqueue_data) IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_enqueue_data -> 'work') IS DISTINCT FROM 'object'
               OR v_enqueue_data #>> '{work,job_id}' IS NULL
               OR v_enqueue_data #>> '{work,status}' NOT IN ('pending', 'running', 'ready', 'failed', 'canceled') THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_invalid_response';
            END IF;
            IF v_enqueue_data #>> '{work,status}' IN ('pending', 'running') THEN
                v_page_scheduled := v_page_scheduled + 1;
            ELSE
                v_page_skipped := v_page_skipped + 1;
            END IF;
        END LOOP;

        BEGIN
            SELECT job.* INTO v_job FROM public.asset_image_jobs AS job
            WHERE job.job_id = p_job_id FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_operation_busy';
        END;
        IF NOT FOUND OR v_job.status <> 'running'
           OR v_job.generation <> p_generation
           OR v_job.lease_token IS DISTINCT FROM p_lease_token
           OR v_job.lease_expires_at <= pg_catalog.clock_timestamp()
           OR v_job.progress #>> '{pending_batch,batch_token}' IS DISTINCT FROM v_batch_token::text THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'image_lease_lost';
        END IF;
        v_scheduled := v_scheduled + v_page_scheduled;
        v_skipped := v_skipped + v_page_skipped;
        v_data := pg_catalog.jsonb_build_object(
            'job_id', p_job_id,
            'generation', p_generation,
            'lease_token', p_lease_token,
            'scheduled', v_scheduled,
            'skipped', v_skipped,
            'complete', v_complete,
            'next_owner_kind', CASE WHEN v_complete THEN NULL ELSE v_next_kind END,
            'next_owner_id', CASE WHEN v_complete THEN NULL ELSE v_next_id END,
            'batch_token', v_batch_token,
            'candidates', '[]'::jsonb,
            'needs_observation', false
        );
        v_progress := (v_job.progress - 'pending_batch') || pg_catalog.jsonb_build_object(
            'snapshot_digest', v_snapshot_digest,
            'total', v_total,
            'scheduled', v_scheduled,
            'skipped', v_skipped,
            'last_advance_lease_token', p_lease_token,
            'last_advance_batch_token', v_batch_token,
            'last_advance_observations_digest', v_observations_digest,
            'last_advance_result', v_data
        );
        UPDATE public.asset_image_jobs AS job
        SET status = CASE WHEN v_complete THEN 'ready' ELSE 'running' END,
            backfill_cursor_kind = CASE WHEN v_complete THEN NULL ELSE v_next_kind END,
            backfill_cursor_id = CASE WHEN v_complete THEN NULL ELSE v_next_id END,
            progress = v_progress,
            lease_owner = CASE WHEN v_complete THEN NULL ELSE job.lease_owner END,
            lease_token = CASE WHEN v_complete THEN NULL ELSE job.lease_token END,
            lease_expires_at = CASE WHEN v_complete THEN NULL ELSE job.lease_expires_at END,
            last_lease_token = CASE WHEN v_complete THEN p_lease_token ELSE NULL END,
            last_outcome = CASE WHEN v_complete THEN 'published' ELSE NULL END,
            completed_at = CASE WHEN v_complete THEN pg_catalog.clock_timestamp() ELSE NULL END,
            updated_at = pg_catalog.clock_timestamp()
        WHERE job.job_id = p_job_id;
    EXCEPTION WHEN SQLSTATE 'P0001' THEN
        v_failure := SQLERRM;
    END;
    IF v_failure = 'image_source_busy' OR v_failure = 'image_operation_busy' THEN
        RETURN aap_assets_v1._image_reply(NULL,
            CASE WHEN v_failure = 'image_source_busy' THEN 'source_busy' ELSE 'operation_busy' END);
    ELSIF v_failure = 'image_lease_lost' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    ELSIF v_failure IS NOT NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    RETURN aap_assets_v1._image_reply(v_data, NULL);
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.fail_image_media(
    p_job_id uuid, p_generation bigint, p_lease_token uuid,
    p_error_code text, p_error_detail text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
#variable_conflict use_variable
DECLARE
    v_candidate public.asset_image_jobs%ROWTYPE;
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_source_reply jsonb;
    v_source jsonb;
    v_source_code text;
    v_spec jsonb;
    v_prefix text;
    v_reason text := 'attempt_abandoned';
    v_outcome text := 'failed';
BEGIN
    IF p_job_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_lease_token IS NULL
       OR p_error_code IS NULL OR pg_catalog.char_length(p_error_code) NOT BETWEEN 1 AND 64
       OR p_error_code NOT IN (
            'invalid_request', 'invalid_response', 'storage_unavailable',
            'account_not_active', 'account_busy', 'invalid_account_role',
            'source_missing', 'source_changed', 'source_busy', 'source_not_ready',
            'task_not_visible', 'task_busy', 'permission_denied', 'not_image',
            'asset_missing', 'operation_conflict', 'operation_busy', 'lease_lost',
            'dependency_changed', 'resource_budget_exceeded', 'object_missing',
            'owner_changed', 'not_eligible', 'unsupported_dimensions', 'resource_limit',
            'invalid_coordinate', 'timeout', 'source_unavailable',
            'color_profile_unavailable', 'multi_page_unsupported',
            'dimension_mismatch', 'generator_unavailable', 'decode_failed',
            'encode_failed', 'tile_grid_mismatch', 'upload_verification_failed',
            'inconsistent_ready'
       ) OR p_error_detail IS NULL OR pg_catalog.char_length(p_error_detail) > 2000 THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    IF p_error_code IN ('source_busy', 'task_busy') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
    ELSIF p_error_code = 'operation_busy' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    ELSIF p_error_code = 'lease_lost' THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    SELECT job.* INTO v_candidate
    FROM public.asset_image_jobs AS job
    WHERE job.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_candidate.generation = p_generation
       AND v_candidate.last_lease_token = p_lease_token
       AND v_candidate.last_outcome IS NOT NULL THEN
        RETURN aap_assets_v1._image_exec_outcome(
            p_job_id, p_generation,
            CASE WHEN v_candidate.last_outcome IN ('source_changed', 'dependency_changed')
                 THEN v_candidate.last_outcome ELSE 'replayed' END
        );
    END IF;
    IF v_candidate.status <> 'running'
       OR v_candidate.generation <> p_generation
       OR v_candidate.lease_token IS DISTINCT FROM p_lease_token THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_candidate.kind <> 'image_backfill' THEN
        v_source_reply := aap_assets_v1._image_resolve_owner(
            v_candidate.owner_kind, v_candidate.owner_id, true
        );
        IF v_source_reply IS NULL
           OR v_source_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_source_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_source_reply -> 'ok' = 'true'::jsonb THEN
            v_source := v_source_reply -> 'data';
            IF pg_catalog.jsonb_typeof(v_source) IS DISTINCT FROM 'object'
               OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_candidate.owner_kind
               OR v_source #>> '{owner,id}' IS DISTINCT FROM v_candidate.owner_id::text THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            IF v_source -> 'source_drifted' = 'true'::jsonb
               OR v_source ->> 'source_snapshot' IS DISTINCT FROM v_candidate.source_snapshot
               OR (v_source ->> 'source_generation')::bigint IS DISTINCT FROM v_candidate.source_generation
               OR NOT aap_assets_v1._image_exec_identity_equal(
                    v_source -> 'object_identity', v_candidate.source_facts -> 'object_identity'
               ) THEN
                v_source_code := 'source_changed';
            END IF;
        ELSE
            v_source_code := v_source_reply #>> '{error,code}';
            IF v_source_code IN ('source_busy', 'task_busy') THEN
                RETURN aap_assets_v1._image_reply(NULL, 'source_busy');
            ELSIF v_source_code IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
        END IF;
        IF v_source_code IS NULL AND (
            v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
            OR v_source -> 'file_type' IS DISTINCT FROM '"image"'::jsonb
            OR v_source -> 'object_identity' IS NULL
            OR v_source -> 'object_identity' = 'null'::jsonb
        ) THEN
            v_source_code := 'source_changed';
        END IF;
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_image_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_job.status = 'running'
       AND v_job.generation = p_generation
       AND v_job.lease_token = p_lease_token
       AND v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        IF v_job.output_prefix IS NOT NULL THEN
            v_spec := aap_assets_v1._image_spec(v_job.spec);
            IF v_spec IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            PERFORM aap_assets_v1._queue_image_garbage(
                v_job.output_prefix, 'attempt_abandoned',
                GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                    + pg_catalog.make_interval(hours => (v_spec #>> '{policy,gc_grace_hours}')::integer)
            );
        END IF;
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    IF v_job.status <> 'running'
       OR v_job.generation <> p_generation
       OR v_job.lease_token IS DISTINCT FROM p_lease_token
       OR v_job.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, 'lease_lost');
    END IF;
    v_spec := aap_assets_v1._image_spec(v_job.spec);
    IF v_spec IS NULL OR v_spec IS DISTINCT FROM v_job.spec THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_source_code IS NOT NULL OR p_error_code = 'source_changed'
       OR p_error_code = 'source_missing' OR p_error_code = 'owner_changed' THEN
        v_outcome := 'source_changed';
        v_reason := 'source_changed';
    END IF;
    IF v_job.kind = 'pyramid' THEN
        BEGIN
            SELECT asset.* INTO v_asset
            FROM public.image_pyramid_assets AS asset
            WHERE asset.id = v_job.pyramid_asset_id
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed'); END IF;
        BEGIN
            SELECT generation.* INTO v_generation
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_job.pyramid_asset_id
              AND generation.generation = v_job.pyramid_generation
            FOR UPDATE NOWAIT;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        IF NOT FOUND OR v_generation.job_id IS DISTINCT FROM p_job_id
           OR v_generation.job_generation IS DISTINCT FROM p_generation
           OR v_generation.lease_token IS DISTINCT FROM p_lease_token
           OR v_generation.status <> 'building'
           OR v_asset.building_generation IS DISTINCT FROM v_job.pyramid_generation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'dependency_changed');
        END IF;
    END IF;
    v_prefix := v_job.output_prefix;
    IF v_prefix IS NOT NULL THEN
        PERFORM aap_assets_v1._queue_image_garbage(
            v_prefix, v_reason,
            GREATEST(pg_catalog.clock_timestamp(), v_job.lease_expires_at)
                + pg_catalog.make_interval(hours => (v_spec #>> '{policy,gc_grace_hours}')::integer)
        );
    END IF;
    IF v_job.kind = 'pyramid' THEN
        UPDATE public.image_pyramid_generations AS generation
        SET status = 'failed', error_code = CASE WHEN v_outcome = 'failed'
                THEN p_error_code ELSE 'source_changed' END,
            lease_token = NULL, lease_expires_at = NULL,
            updated_at = pg_catalog.clock_timestamp()
        WHERE generation.asset_id = v_job.pyramid_asset_id
          AND generation.generation = v_job.pyramid_generation
          AND generation.status IN ('pending', 'building');
        UPDATE public.image_pyramid_assets AS asset
        SET building_generation = NULL, updated_at = pg_catalog.clock_timestamp()
        WHERE asset.id = v_job.pyramid_asset_id
          AND asset.building_generation = v_job.pyramid_generation;
    END IF;
    UPDATE public.asset_image_jobs AS job
    SET status = CASE WHEN v_outcome = 'failed' THEN 'failed' ELSE 'canceled' END,
        error_code = CASE WHEN v_outcome = 'failed' THEN p_error_code ELSE 'source_changed' END,
        error_detail = CASE WHEN v_outcome = 'failed' THEN p_error_detail
            ELSE COALESCE(v_source_code, p_error_code) END,
        lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
        last_lease_token = p_lease_token, last_outcome = v_outcome,
        completed_at = pg_catalog.clock_timestamp(),
        updated_at = pg_catalog.clock_timestamp()
    WHERE job.job_id = p_job_id;
    RETURN aap_assets_v1._image_exec_outcome(p_job_id, p_generation, v_outcome);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
END;
$function$;
