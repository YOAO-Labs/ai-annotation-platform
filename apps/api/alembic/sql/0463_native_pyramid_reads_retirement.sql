-- Public native-pyramid reads and retirement routing for saved outputs.
-- Forward-only body replacements; historical migration bodies stay untouched.
-- One exact proof for a saved pyramid output: the immutable receipt still
-- binds this output/asset/generation in a live state, with a usable media-cache
-- location and its own allocated prefix.  Retirement belongs to that receipt;
-- the bucket-root garbage lane records neither location nor inventory.
CREATE OR REPLACE FUNCTION aap_assets_v1._image_saved_output_prefix(
    p_asset_id uuid, p_generation integer, p_output_id uuid, p_ready_only boolean
) RETURNS text
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_prefix text;
BEGIN
    IF p_asset_id IS NULL OR p_generation IS NULL OR p_generation < 1
       OR p_output_id IS NULL OR p_ready_only IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid saved image output scope';
    END IF;
    SELECT output.storage_ref ->> 'prefix'
    INTO v_prefix
    FROM public.image_pyramid_outputs AS output
    WHERE output.id = p_output_id
      AND output.asset_id = p_asset_id
      AND output.generation = p_generation
      -- A live saved output (or one whose exact retirement already completed)
      -- is its own durable obligation; only the ready read needs live bytes.
      AND ((p_ready_only AND output.state = 'ready')
           OR (NOT p_ready_only AND output.state IN (
                'building', 'ready', 'retiring', 'deleted')))
      AND pg_catalog.jsonb_typeof(output.storage_ref) = 'object'
      AND output.storage_ref ?& ARRAY[
            'schema', 'purpose', 'location_id', 'bucket', 'prefix', 'storage_identity'
          ]
      AND output.storage_ref -> 'schema' IN ('1'::jsonb, '2'::jsonb)
      AND output.storage_ref ->> 'purpose' = 'media-cache'
      AND output.storage_ref ->> 'location_id'
            ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
      AND COALESCE(output.storage_ref ->> 'bucket', '') <> ''
      AND output.storage_ref ->> 'storage_identity' ~ '^[0-9a-f]{64}$'
      AND COALESCE(output.storage_ref ->> 'prefix', '') <> ''
      AND output.storage_ref ->> 'prefix'
            = COALESCE(output.storage_ref ->> 'key_prefix', '')
              || 'image-pyramids/objects/' || output.id::text || '/'
    FOR SHARE NOWAIT;
    RETURN v_prefix;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_saved_output_prefix(uuid,integer,uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.read_image_generation(
    p_actor_id uuid, p_task_id uuid, p_dataset_item_id uuid,
    p_generation integer, p_profile_version text
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source_reply jsonb;
    v_source jsonb;
    v_projection jsonb;
    v_selected_profile text;
    v_asset_id uuid;
    v_output_id uuid;
    v_output_prefix text;
    v_active_generation integer;
    v_last_accessed_at timestamptz;
    v_generation_policy jsonb;
    v_policy jsonb;
    v_accessed_at timestamptz;
    v_retention_until timestamptz;
    v_asset_prefix text;
    v_generation_prefix text;
    v_attempt_prefix text;
    v_prefix text;
    v_prefixes text[];
    v_garbage public.asset_image_garbage%ROWTYPE;
    v_garbage_error text;
    v_touch_access boolean;
BEGIN
    IF p_actor_id IS NULL OR ((p_task_id IS NULL) = (p_dataset_item_id IS NULL))
       OR (p_generation IS NOT NULL AND p_generation < 1)
       OR (p_profile_version IS NOT NULL
           AND pg_catalog.char_length(p_profile_version) NOT BETWEEN 1 AND 64) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_selected_profile := COALESCE(p_profile_version, 'pyramid-v1');
    v_source_reply := aap_assets_v1._image_owner_source(
        p_actor_id, p_task_id, p_dataset_item_id, false
    );
    IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_source_reply; END IF;
    v_source := v_source_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_source) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_projection := aap_assets_v1._image_pyramid_projection(
        v_source, p_generation, v_selected_profile
    );
    IF v_projection IS NULL THEN
        RETURN aap_assets_v1._image_reply(
            NULL, CASE WHEN p_generation IS NOT NULL THEN 'asset_missing' ELSE 'invalid_response' END
        );
    END IF;
    IF p_generation IS NOT NULL
       AND v_projection ->> 'status' = 'ready'
       AND v_projection ->> 'generation' = p_generation::text THEN
        BEGIN
            v_asset_id := (v_projection ->> 'asset_id')::uuid;
            SELECT asset.active_generation, generation.last_accessed_at,
                   generation.policy, generation.output_id
            INTO v_active_generation, v_last_accessed_at, v_generation_policy,
                 v_output_id
            FROM public.image_pyramid_assets AS asset
            JOIN public.image_pyramid_generations AS generation
              ON generation.asset_id = asset.id
            WHERE asset.id = v_asset_id
              AND asset.profile_version = v_selected_profile
              AND generation.generation = p_generation
              AND generation.status = 'ready'
              AND generation.source_generation = (v_source ->> 'source_generation')::bigint
              AND generation.source_snapshot = v_source ->> 'source_snapshot'
              AND generation.source_identity = aap_assets_v1._image_exec_legacy_identity(
                    v_source -> 'object_identity'
              )
              AND (v_source -> 'width' = 'null'::jsonb
                   OR generation.width = (v_source ->> 'width')::integer)
              AND (v_source -> 'height' = 'null'::jsonb
                   OR generation.height = (v_source ->> 'height')::integer)
            FOR UPDATE OF asset, generation NOWAIT;
            IF NOT FOUND THEN
                RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
            END IF;

            v_policy := aap_assets_v1._image_policy(
                COALESCE(v_generation_policy, '{}'::jsonb)
            );
            IF v_policy IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            v_asset_prefix := 'image-pyramids/' || v_asset_id::text || '/';
            v_generation_prefix := v_asset_prefix || 'g' || p_generation::text || '/';
            v_attempt_prefix := pg_catalog.regexp_replace(
                v_projection ->> 'manifest_key', 'manifest\.json$', ''
            );
            -- A saved output owns its bytes and its prefix is the allocation's
            -- full key prefix, so the scope is proven by that immutable receipt
            -- (output id, asset, generation, state, location identity), never by
            -- a bucket-root pattern.  Rows without a saved output keep their
            -- legacy attempt scopes.
            IF v_output_id IS NOT NULL THEN
                v_output_prefix := aap_assets_v1._image_saved_output_prefix(
                    v_asset_id, p_generation, v_output_id, true
                );
                IF v_output_prefix IS NULL
                   OR v_attempt_prefix IS DISTINCT FROM v_output_prefix THEN
                    RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
                END IF;
            ELSIF v_attempt_prefix IS NULL
               OR NOT (
                   v_attempt_prefix = v_generation_prefix
                   OR (pg_catalog.left(v_attempt_prefix, pg_catalog.char_length(v_generation_prefix))
                           = v_generation_prefix
                       AND aap_assets_v1._image_garbage_prefix_valid(v_attempt_prefix))
               ) THEN
                RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
            END IF;
            v_prefixes := ARRAY[v_asset_prefix, v_generation_prefix, v_attempt_prefix];
            v_touch_access := v_last_accessed_at IS NULL
                OR v_last_accessed_at < pg_catalog.clock_timestamp() - interval '10 minutes';
            IF v_touch_access THEN
                v_accessed_at := pg_catalog.clock_timestamp();
            ELSE
                v_accessed_at := v_last_accessed_at;
            END IF;
            v_retention_until := v_accessed_at + pg_catalog.make_interval(
                hours => (v_policy ->> 'gc_grace_hours')::integer
            );
            BEGIN
                FOR v_prefix IN
                    SELECT DISTINCT candidate.prefix
                    FROM pg_catalog.unnest(v_prefixes) AS candidate(prefix)
                    ORDER BY candidate.prefix
                LOOP
                    IF NOT pg_catalog.pg_try_advisory_xact_lock(
                        pg_catalog.hashtextextended('aap-image-garbage:' || v_prefix, 0)
                    ) THEN
                        v_garbage_error := 'operation_busy';
                        RAISE lock_not_available USING MESSAGE = 'image garbage scope is busy';
                    END IF;
                END LOOP;
                FOR v_garbage IN
                    SELECT garbage.*
                    FROM public.asset_image_garbage AS garbage
                    WHERE garbage.prefix = ANY(v_prefixes)
                    ORDER BY garbage.prefix
                    FOR UPDATE NOWAIT
                LOOP
                    IF v_garbage.completed_at IS NOT NULL
                       OR v_garbage.last_ack_token IS NOT NULL THEN
                        v_garbage_error := 'asset_missing';
                        RAISE lock_not_available USING MESSAGE = 'image generation was collected';
                    ELSIF v_garbage.lease_token IS NOT NULL THEN
                        v_garbage_error := 'source_busy';
                        RAISE lock_not_available USING MESSAGE = 'image generation cleanup is active';
                    END IF;
                END LOOP;
                IF v_touch_access THEN
                    UPDATE public.image_pyramid_generations AS generation
                    SET last_accessed_at = v_accessed_at
                    WHERE generation.asset_id = v_asset_id
                      AND generation.generation = p_generation;
                END IF;
                -- A saved output retires through its immutable owner once no
                -- generation references it; the bucket-root prefix never holds
                -- its bytes.
                IF v_active_generation IS DISTINCT FROM p_generation
                   AND v_output_id IS NULL THEN
                    PERFORM aap_assets_v1._queue_image_garbage(
                        v_generation_prefix, 'generation_retired', v_retention_until
                    );
                END IF;
                UPDATE public.asset_image_garbage AS garbage
                SET not_before = GREATEST(garbage.not_before, v_retention_until)
                WHERE garbage.prefix = ANY(v_prefixes)
                  AND garbage.completed_at IS NULL
                  AND garbage.lease_token IS NULL
                  AND garbage.not_before < v_retention_until;
            EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
                RETURN aap_assets_v1._image_reply(
                    NULL, COALESCE(v_garbage_error, 'operation_busy')
                );
            END;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        WHEN invalid_text_representation OR numeric_value_out_of_range THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
    END IF;
    RETURN aap_assets_v1._image_reply(
        pg_catalog.jsonb_build_object('source', v_source, 'pyramid', v_projection), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1._cancel_image_source_work(
    p_owner_kind text, p_owner_id uuid, p_reason text
)
RETURNS timestamptz
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_deadline timestamptz := pg_catalog.statement_timestamp();
    v_prefix text;
    v_grace integer;
BEGIN
    IF p_owner_kind IS NULL OR p_owner_kind NOT IN ('dataset_item', 'task')
       OR p_owner_id IS NULL OR p_reason IS NULL
       OR pg_catalog.char_length(p_reason) NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid image cancellation scope';
    END IF;
    FOR v_job IN
        SELECT job.* FROM public.asset_image_jobs AS job
        WHERE job.owner_kind = p_owner_kind AND job.owner_id = p_owner_id
        ORDER BY job.job_id FOR UPDATE NOWAIT
    LOOP
        v_deadline := GREATEST(v_deadline, COALESCE(v_job.lease_expires_at, v_deadline));
        IF v_job.output_prefix IS NOT NULL THEN
            PERFORM aap_assets_v1._queue_image_garbage(
                v_job.output_prefix, p_reason,
                GREATEST(pg_catalog.statement_timestamp(), v_job.lease_expires_at)
            );
        END IF;
        IF v_job.status IN ('pending', 'running') THEN
            UPDATE public.asset_image_jobs AS job
            SET status = 'canceled', error_code = 'source_changed',
                error_detail = p_reason, completed_at = pg_catalog.clock_timestamp(),
                updated_at = pg_catalog.clock_timestamp(),
                last_lease_token = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN v_job.lease_token ELSE job.last_lease_token END,
                last_outcome = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN 'source_changed' ELSE job.last_outcome END,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
            WHERE job.job_id = v_job.job_id;
        END IF;
    END LOOP;
    FOR v_asset IN
        SELECT asset.* FROM public.image_pyramid_assets AS asset
        WHERE (p_owner_kind = 'dataset_item' AND asset.dataset_item_id = p_owner_id)
           OR (p_owner_kind = 'task' AND asset.task_id = p_owner_id)
        ORDER BY asset.id FOR UPDATE NOWAIT
    LOOP
        FOR v_generation IN
            SELECT generation.* FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_asset.id
            ORDER BY generation.generation FOR UPDATE NOWAIT
        LOOP
            IF v_generation.status = 'building'
               AND v_generation.lease_expires_at IS NULL THEN
                RAISE lock_not_available USING MESSAGE = 'pyramid writer lease is unproven';
            END IF;
            v_deadline := GREATEST(v_deadline, COALESCE(v_generation.lease_expires_at, v_deadline));
            IF v_generation.output_id IS NOT NULL THEN
                -- The saved output receipt owns these bytes and this exact
                -- asset/generation.  asset_image_garbage records no location, so
                -- the bucket-root lane must never take a media-cache prefix.  The
                -- output row survives business cleanup and the Images owner
                -- retires it by exact inventory once no generation references it;
                -- a sealed fixture owner retires its own outputs.
                IF aap_assets_v1._image_saved_output_prefix(
                        v_asset.id, v_generation.generation, v_generation.output_id,
                        false) IS NULL THEN
                    RAISE EXCEPTION USING ERRCODE = '23514',
                        MESSAGE = 'pyramid output retirement receipt unproven';
                END IF;
            ELSE
                v_prefix := COALESCE(v_generation.output_prefix,
                    'image-pyramids/' || v_asset.id::text || '/g' || v_generation.generation::text || '/');
                v_grace := COALESCE((v_generation.policy ->> 'gc_grace_hours')::integer, 24);
                PERFORM aap_assets_v1._queue_image_garbage(v_prefix, p_reason,
                    GREATEST(v_deadline,
                        pg_catalog.statement_timestamp() + pg_catalog.make_interval(hours => v_grace))
                );
            END IF;
            IF v_generation.status IN ('pending', 'building') THEN
                UPDATE public.image_pyramid_generations AS generation
                SET status = 'failed', error_code = 'source_changed',
                    updated_at = pg_catalog.clock_timestamp()
                WHERE generation.asset_id = v_asset.id
                  AND generation.generation = v_generation.generation;
            END IF;
        END LOOP;
        UPDATE public.image_pyramid_assets AS asset
        SET active_generation = NULL, building_generation = NULL,
            updated_at = pg_catalog.clock_timestamp()
        WHERE asset.id = v_asset.id;
    END LOOP;
    RETURN v_deadline;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.cancel_image_media(
    p_operation_id uuid, p_actor_id uuid, p_job_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_access jsonb;
    v_job public.asset_image_jobs%ROWTYPE;
    v_generation public.image_pyramid_generations%ROWTYPE;
    v_policy jsonb;
    v_scope jsonb;
    v_intent jsonb;
    v_saved jsonb;
    v_result jsonb;
    v_data jsonb;
    v_canceled boolean := false;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_prefix text;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_job_id IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    v_access := aap_assets_v1._image_job_access(p_job_id, p_actor_id, true);
    IF v_access -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_access; END IF;
    v_scope := pg_catalog.jsonb_build_object('job_id', p_job_id);
    v_intent := pg_catalog.jsonb_build_object('job_id', p_job_id);
    v_saved := aap_assets_v1._image_read_operation(
        p_operation_id, p_actor_id, 'cancel', v_scope, v_intent
    );
    IF v_saved IS NOT NULL THEN
        IF v_saved ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1'
           OR pg_catalog.jsonb_typeof(v_saved -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        IF v_saved -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_saved; END IF;
        v_data := v_saved -> 'data';
        IF v_data ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
           OR v_data ->> 'job_id' IS DISTINCT FROM p_job_id::text
           OR pg_catalog.jsonb_typeof(v_data -> 'canceled') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        RETURN v_saved;
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_image_jobs AS job
        WHERE job.job_id = p_job_id
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'source_missing'); END IF;
        IF v_job.status IN ('pending', 'running') THEN
            v_canceled := true;
            v_policy := aap_assets_v1._image_policy(COALESCE(v_job.spec -> 'policy', '{}'::jsonb));
            IF v_policy IS NULL THEN RAISE EXCEPTION 'invalid captured image policy'; END IF;
            IF v_job.output_prefix IS NOT NULL THEN
                PERFORM aap_assets_v1._queue_image_garbage(
                    v_job.output_prefix,
                    'attempt_abandoned',
                    GREATEST(v_now, COALESCE(v_job.lease_expires_at, v_now))
                        + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
                );
            END IF;
            IF v_job.kind = 'pyramid'
               AND v_job.pyramid_asset_id IS NOT NULL
               AND v_job.pyramid_generation IS NOT NULL THEN
                SELECT generation.* INTO v_generation
                FROM public.image_pyramid_generations AS generation
                WHERE generation.asset_id = v_job.pyramid_asset_id
                  AND generation.generation = v_job.pyramid_generation
                FOR UPDATE NOWAIT;
                IF FOUND AND v_generation.status IN ('pending', 'building') THEN
                    IF v_generation.output_id IS NOT NULL THEN
                        -- Retirement is owned by the immutable output receipt.
                        IF aap_assets_v1._image_saved_output_prefix(
                                v_generation.asset_id, v_generation.generation,
                                v_generation.output_id, false) IS NULL THEN
                            RAISE EXCEPTION USING ERRCODE = '23514',
                                MESSAGE = 'pyramid output retirement receipt unproven';
                        END IF;
                    ELSE
                        v_prefix := COALESCE(v_generation.output_prefix, v_job.output_prefix);
                        IF v_prefix IS NOT NULL AND v_prefix IS DISTINCT FROM v_job.output_prefix THEN
                            PERFORM aap_assets_v1._queue_image_garbage(
                                v_prefix,
                                'attempt_abandoned',
                                GREATEST(v_now, COALESCE(v_generation.lease_expires_at, v_now))
                                    + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
                            );
                        END IF;
                    END IF;
                    UPDATE public.image_pyramid_generations AS generation
                    SET status = 'failed', error_code = 'owner_changed',
                        lease_token = NULL, lease_expires_at = NULL,
                        updated_at = v_now
                    WHERE generation.asset_id = v_generation.asset_id
                      AND generation.generation = v_generation.generation;
                    UPDATE public.image_pyramid_assets AS asset
                    SET building_generation = CASE
                            WHEN asset.building_generation = v_job.pyramid_generation THEN NULL
                            ELSE asset.building_generation END,
                        updated_at = v_now
                    WHERE asset.id = v_job.pyramid_asset_id;
                END IF;
            END IF;
            UPDATE public.asset_image_jobs AS job
            SET status = 'canceled',
                error_code = 'canceled',
                error_detail = NULL,
                last_lease_token = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN v_job.lease_token ELSE job.last_lease_token END,
                last_outcome = CASE WHEN v_job.lease_token IS NOT NULL
                    THEN 'lease_lost' ELSE job.last_outcome END,
                lease_owner = NULL,
                lease_token = NULL,
                lease_expires_at = NULL,
                completed_at = v_now,
                updated_at = v_now
            WHERE job.job_id = p_job_id
            RETURNING job.* INTO v_job;
        END IF;
        v_result := aap_assets_v1._image_reply(
            pg_catalog.jsonb_build_object(
                'operation_id', p_operation_id,
                'job_id', p_job_id,
                'canceled', v_canceled
            ), NULL
        );
        v_saved := aap_assets_v1._image_write_operation(
            p_operation_id, p_actor_id, 'cancel', v_scope, v_intent, v_result
        );
        IF v_saved IS NULL OR v_saved IS DISTINCT FROM v_result THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'image cancellation receipt mismatch';
        END IF;
        RETURN v_saved;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    WHEN OTHERS THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1._image_enqueue(
    p_operation_id uuid,
    p_actor_id uuid,
    p_source jsonb,
    p_spec jsonb,
    p_force boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_spec jsonb;
    v_policy jsonb;
    v_kind text;
    v_owner_kind text;
    v_owner_id uuid;
    v_source_generation bigint;
    v_source_snapshot text;
    v_slot text;
    v_job public.asset_image_jobs%ROWTYPE;
    v_asset public.image_pyramid_assets%ROWTYPE;
    v_old_generation public.image_pyramid_generations%ROWTYPE;
    v_pyramid_generation integer;
    v_job_generation bigint;
    v_job_id uuid;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_same boolean := false;
    v_reuse boolean := false;
    v_attempt_prefix text;
    v_data jsonb;
    v_retained_publication jsonb;
    v_existing boolean := false;
BEGIN
    IF p_force IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    v_spec := aap_assets_v1._image_spec(p_spec);
    IF v_spec IS NULL THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_request'); END IF;
    v_kind := v_spec ->> 'kind';
    v_policy := v_spec -> 'policy';
    IF v_kind = 'image_backfill' THEN
        IF p_source IS NOT NULL OR p_actor_id IS NOT NULL THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        IF v_spec #> '{selection,accepted_before}' = 'null'::jsonb THEN
            v_spec := pg_catalog.jsonb_set(
                v_spec, '{selection,accepted_before}', pg_catalog.to_jsonb(v_now), true
            );
        END IF;
        BEGIN
            v_job_id := pg_catalog.gen_random_uuid();
            INSERT INTO public.asset_image_jobs(
                job_id, kind, owner_kind, owner_id, slot_key, generation, spec,
                status, source_generation, source_snapshot, source_facts, actor_id,
                progress, attempt_count, accepted_at, updated_at, backfill_cutoff
            ) VALUES (
                v_job_id, 'image_backfill', NULL, NULL,
                'backfill:' || COALESCE(p_operation_id::text, v_job_id::text), 1,
                v_spec, 'pending', 0, NULL, NULL, NULL, '{}'::jsonb, 0,
                v_now, v_now, COALESCE((v_spec #>> '{selection,accepted_before}')::timestamptz, v_now)
            ) RETURNING * INTO v_job;
            v_data := pg_catalog.jsonb_build_object(
                'operation_id', p_operation_id,
                'source', NULL,
                'work', aap_assets_v1._image_work_reference(v_job),
                'spec', v_spec
            );
            RETURN aap_assets_v1._image_reply(v_data, NULL);
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        WHEN unique_violation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        WHEN OTHERS THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END;
    END IF;
    IF p_source IS NULL OR NOT aap_assets_v1._image_source_data_valid(p_source) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF p_source -> 'source_drifted' = 'true'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    IF p_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
    END IF;
    IF p_source -> 'object_identity' = 'null'::jsonb THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_not_ready');
    END IF;
    v_owner_kind := p_source #>> '{owner,kind}';
    BEGIN
        v_owner_id := (p_source #>> '{owner,id}')::uuid;
        v_source_generation := (p_source ->> 'source_generation')::bigint;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END;
    v_source_snapshot := p_source ->> 'source_snapshot';
    IF v_spec ->> 'expected_source_snapshot' IS DISTINCT FROM v_source_snapshot THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    v_slot := CASE
        WHEN v_kind = 'pyramid' THEN 'pyramid:' || (v_policy ->> 'profile_version')
        WHEN v_kind = 'dimensions' AND v_spec -> 'generate_preview' = 'true'::jsonb
            THEN 'dimensions:preview'
        ELSE v_kind END;
    IF v_kind = 'pyramid' THEN
        BEGIN
            IF v_owner_kind = 'dataset_item' THEN
                INSERT INTO public.image_pyramid_assets(id, dataset_item_id, profile_version)
                VALUES (pg_catalog.gen_random_uuid(), v_owner_id, v_policy ->> 'profile_version')
                ON CONFLICT (dataset_item_id, profile_version)
                    WHERE dataset_item_id IS NOT NULL DO NOTHING;
                SELECT asset.* INTO v_asset
                FROM public.image_pyramid_assets AS asset
                WHERE asset.dataset_item_id = v_owner_id
                  AND asset.profile_version = v_policy ->> 'profile_version'
                FOR UPDATE NOWAIT;
            ELSE
                INSERT INTO public.image_pyramid_assets(id, task_id, profile_version)
                VALUES (pg_catalog.gen_random_uuid(), v_owner_id, v_policy ->> 'profile_version')
                ON CONFLICT (task_id, profile_version)
                    WHERE task_id IS NOT NULL DO NOTHING;
                SELECT asset.* INTO v_asset
                FROM public.image_pyramid_assets AS asset
                WHERE asset.task_id = v_owner_id
                  AND asset.profile_version = v_policy ->> 'profile_version'
                FOR UPDATE NOWAIT;
            END IF;
        EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        WHEN unique_violation THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
        END;
        IF NOT FOUND THEN RETURN aap_assets_v1._image_reply(NULL, 'invalid_response'); END IF;
    END IF;
    BEGIN
        SELECT job.* INTO v_job
        FROM public.asset_image_jobs AS job
        WHERE job.owner_kind = v_owner_kind
          AND job.owner_id = v_owner_id
          AND job.slot_key = v_slot
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    v_existing := FOUND;
    IF v_existing THEN
        v_same := v_job.source_generation = v_source_generation
            AND v_job.source_snapshot = v_source_snapshot
            AND v_job.spec = v_spec;
        v_reuse := v_same AND v_job.status IN ('pending', 'running');
        IF v_same AND v_job.status = 'ready' AND NOT p_force THEN v_reuse := true; END IF;
        IF v_same AND v_job.status = 'failed' AND (
            NOT p_force OR v_now < COALESCE(v_job.completed_at, v_job.updated_at)
                + pg_catalog.make_interval(secs => (v_policy ->> 'retry_cooldown_seconds')::integer)
        ) THEN v_reuse := true; END IF;
        IF v_reuse THEN
            v_data := pg_catalog.jsonb_build_object(
                'operation_id', p_operation_id,
                'source', p_source,
                'work', aap_assets_v1._image_work_reference(v_job),
                'spec', v_job.spec
            );
            RETURN aap_assets_v1._image_reply(v_data, NULL);
        END IF;
        IF v_job.status IN ('pending', 'running') THEN
            RETURN aap_assets_v1._image_reply(NULL, 'operation_conflict');
        END IF;
        IF v_kind = 'thumbnail' THEN
            IF v_job.status = 'ready' THEN
                IF v_job.kind = 'thumbnail'
                   AND v_job.owner_kind = v_owner_kind
                   AND v_job.owner_id = v_owner_id
                   AND v_job.source_generation = v_source_generation
                   AND v_job.source_snapshot = v_source_snapshot
                   AND v_job.spec ->> 'kind' = 'thumbnail'
                   AND v_job.spec ->> 'expected_source_snapshot' = v_source_snapshot
                   AND v_job.result ->> 'kind' = 'thumbnail'
                   AND v_job.output_prefix IS NOT NULL
                   AND v_job.result #>> '{artifact,key}' = v_job.output_prefix || 'thumbnail.webp'
                   AND p_source -> 'thumbnail_path' = v_job.result #> '{artifact,key}'
                   AND p_source -> 'thumbnail_source_generation' = p_source -> 'source_generation'
                   AND aap_assets_v1._media_object_identity_valid(
                        v_job.source_facts -> 'object_identity'
                   )
                   AND aap_assets_v1._image_exec_identity_equal(
                        v_job.source_facts -> 'object_identity', p_source -> 'object_identity'
                   )
                   AND aap_assets_v1._image_exec_identity_equal(
                        v_job.result -> 'source_identity', p_source -> 'object_identity'
                   ) THEN
                    v_retained_publication := pg_catalog.jsonb_build_object(
                        'schema', 'aap-image-thumbnail-publication/v1',
                        'job_id', v_job.job_id,
                        'generation', v_job.generation,
                        'owner', pg_catalog.jsonb_build_object(
                            'kind', v_job.owner_kind, 'id', v_job.owner_id
                        ),
                        'source_generation', v_job.source_generation,
                        'source_snapshot', v_job.source_snapshot,
                        'source_identity', aap_assets_v1._image_normalize_identity(
                            v_job.source_facts -> 'object_identity'
                        ),
                        'spec', v_job.spec,
                        'result', v_job.result
                    );
                END IF;
            ELSIF v_job.status IN ('failed', 'canceled') THEN
                v_retained_publication := v_job.progress -> 'retained_publication';
                IF pg_catalog.jsonb_typeof(v_retained_publication) IS DISTINCT FROM 'object'
                   OR (v_retained_publication - ARRAY[
                        'schema', 'job_id', 'generation', 'owner', 'source_generation',
                        'source_snapshot', 'source_identity', 'spec', 'result'
                   ]) IS DISTINCT FROM '{}'::jsonb
                   OR NOT (v_retained_publication ?& ARRAY[
                        'schema', 'job_id', 'generation', 'owner', 'source_generation',
                        'source_snapshot', 'source_identity', 'spec', 'result'
                   ])
                   OR v_retained_publication ->> 'schema'
                        IS DISTINCT FROM 'aap-image-thumbnail-publication/v1'
                   OR v_retained_publication ->> 'job_id' IS DISTINCT FROM v_job.job_id::text
                   OR COALESCE(v_retained_publication ->> 'generation', '') !~ '^[1-9][0-9]*$'
                   OR (v_retained_publication #>> '{owner,kind}') IS DISTINCT FROM v_owner_kind
                   OR (v_retained_publication #>> '{owner,id}') IS DISTINCT FROM v_owner_id::text
                   OR v_job.source_generation IS DISTINCT FROM v_source_generation
                   OR v_job.source_snapshot IS DISTINCT FROM v_source_snapshot
                   OR v_retained_publication ->> 'source_generation'
                        IS DISTINCT FROM v_source_generation::text
                   OR v_retained_publication ->> 'source_snapshot'
                        IS DISTINCT FROM v_source_snapshot
                   OR NOT aap_assets_v1._media_object_identity_valid(
                        v_retained_publication -> 'source_identity'
                   )
                   OR NOT aap_assets_v1._image_exec_identity_equal(
                        v_retained_publication -> 'source_identity', p_source -> 'object_identity'
                   )
                   OR aap_assets_v1._image_spec(v_retained_publication -> 'spec')
                        IS DISTINCT FROM v_retained_publication -> 'spec'
                   OR v_retained_publication #>> '{spec,kind}' IS DISTINCT FROM 'thumbnail'
                   OR v_retained_publication #>> '{spec,expected_source_snapshot}'
                        IS DISTINCT FROM v_source_snapshot
                   OR pg_catalog.jsonb_typeof(v_retained_publication -> 'result')
                        IS DISTINCT FROM 'object'
                   OR v_retained_publication #>> '{result,kind}' IS DISTINCT FROM 'thumbnail'
                   OR v_retained_publication #>> '{result,artifact,key}'
                        IS DISTINCT FROM p_source ->> 'thumbnail_path'
                   OR p_source -> 'thumbnail_source_generation'
                        IS DISTINCT FROM p_source -> 'source_generation'
                   OR NOT aap_assets_v1._media_object_identity_valid(
                        v_retained_publication #> '{result,source_identity}'
                   )
                   OR NOT aap_assets_v1._image_exec_identity_equal(
                        v_retained_publication #> '{result,source_identity}',
                        p_source -> 'object_identity'
                   ) THEN
                    v_retained_publication := NULL;
                END IF;
            END IF;
        END IF;
        IF v_job.generation = 9223372036854775807 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'resource_budget_exceeded');
        END IF;
        IF v_kind = 'pyramid' THEN
            v_pyramid_generation := aap_assets_v1._image_next_generation(v_asset.id);
            IF v_pyramid_generation IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'resource_budget_exceeded');
            END IF;
        END IF;
        v_job_generation := v_job.generation + 1;
        IF v_job.output_prefix IS NOT NULL THEN
            PERFORM aap_assets_v1._queue_image_garbage(
                v_job.output_prefix, 'attempt_abandoned',
                GREATEST(v_now, COALESCE(v_job.lease_expires_at, v_now))
                    + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
            );
        END IF;
        IF v_kind = 'pyramid' AND v_job.pyramid_asset_id IS NOT NULL
           AND v_job.pyramid_generation IS NOT NULL THEN
            SELECT generation.* INTO v_old_generation
            FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_job.pyramid_asset_id
              AND generation.generation = v_job.pyramid_generation
            FOR UPDATE NOWAIT;
            IF FOUND AND v_old_generation.status IN ('pending', 'building') THEN
                IF v_old_generation.output_id IS NOT NULL THEN
                    -- The saved output receipt owns retirement; keep its row and
                    -- inventory intact for the Images owner.
                    IF aap_assets_v1._image_saved_output_prefix(
                            v_old_generation.asset_id, v_old_generation.generation,
                            v_old_generation.output_id, false) IS NULL THEN
                        RAISE EXCEPTION USING ERRCODE = '23514',
                            MESSAGE = 'pyramid output retirement receipt unproven';
                    END IF;
                ELSIF v_old_generation.output_prefix IS NOT NULL THEN
                    PERFORM aap_assets_v1._queue_image_garbage(
                        v_old_generation.output_prefix, 'attempt_abandoned',
                        GREATEST(v_now, COALESCE(v_old_generation.lease_expires_at, v_now))
                            + pg_catalog.make_interval(hours => (v_policy ->> 'gc_grace_hours')::integer)
                    );
                END IF;
                UPDATE public.image_pyramid_generations AS generation
                SET status = 'failed', error_code = 'source_changed',
                    updated_at = v_now
                WHERE generation.asset_id = v_old_generation.asset_id
                  AND generation.generation = v_old_generation.generation;
            END IF;
        END IF;
    ELSE
        v_job_generation := 1;
        v_job_id := pg_catalog.gen_random_uuid();
        IF v_kind = 'pyramid' THEN
            v_pyramid_generation := aap_assets_v1._image_next_generation(v_asset.id);
            IF v_pyramid_generation IS NULL THEN
                RETURN aap_assets_v1._image_reply(NULL, 'resource_budget_exceeded');
            END IF;
        END IF;
    END IF;
    IF v_existing THEN
        UPDATE public.asset_image_jobs AS job
        SET generation = v_job_generation,
            spec = v_spec,
            status = 'pending',
            source_generation = v_source_generation,
            source_snapshot = v_source_snapshot,
            source_facts = p_source,
            actor_id = p_actor_id,
            progress = CASE WHEN v_retained_publication IS NULL THEN '{}'::jsonb
                ELSE pg_catalog.jsonb_build_object(
                    'retained_publication', v_retained_publication
                ) END,
            result = NULL,
            error_code = NULL,
            error_detail = NULL,
            attempt_count = 0,
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            output_prefix = NULL,
            pyramid_asset_id = CASE WHEN v_kind = 'pyramid' THEN v_asset.id END,
            pyramid_generation = v_pyramid_generation,
            last_lease_token = NULL,
            last_outcome = NULL,
            accepted_at = v_now,
            updated_at = v_now,
            completed_at = NULL
        WHERE job.job_id = v_job.job_id
        RETURNING job.* INTO v_job;
    ELSE
        INSERT INTO public.asset_image_jobs(
            job_id, kind, owner_kind, owner_id, slot_key, generation, spec, status,
            source_generation, source_snapshot, source_facts, actor_id, progress,
            attempt_count, pyramid_asset_id, pyramid_generation, accepted_at, updated_at
        ) VALUES (
            v_job_id, v_kind, v_owner_kind, v_owner_id, v_slot, v_job_generation,
            v_spec, 'pending', v_source_generation, v_source_snapshot, p_source,
            p_actor_id, '{}'::jsonb, 0,
            CASE WHEN v_kind = 'pyramid' THEN v_asset.id END,
            v_pyramid_generation, v_now, v_now
        ) RETURNING * INTO v_job;
    END IF;
    IF v_kind = 'pyramid' THEN
        IF EXISTS (
            SELECT 1 FROM public.image_pyramid_generations AS generation
            WHERE generation.asset_id = v_asset.id
              AND generation.generation = v_pyramid_generation
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23505', MESSAGE = 'image generation was already reserved';
        END IF;
        INSERT INTO public.image_pyramid_generations(
            asset_id, generation, job_id, job_generation, source_generation,
            source_snapshot, output_prefix, manifest, policy, source_identity,
            source_fingerprint, status, lease_token, lease_expires_at,
            width, height, tile_size, overlap, normalization_version,
            tile_count, retained_bytes, created_at, updated_at
        ) VALUES (
            v_asset.id, v_pyramid_generation, v_job.job_id, v_job.generation,
            v_source_generation, v_source_snapshot, NULL, NULL, v_policy,
            v_source_snapshot, NULL, 'pending', NULL, NULL, NULL, NULL, 512, 1,
            v_policy ->> 'normalization_version', 0, 0, v_now, v_now
        );
        UPDATE public.image_pyramid_assets AS asset
        SET building_generation = v_pyramid_generation,
            updated_at = v_now
        WHERE asset.id = v_asset.id;
    END IF;
    v_data := pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id,
        'source', p_source,
        'work', aap_assets_v1._image_work_reference(v_job),
        'spec', v_job.spec
    );
    RETURN aap_assets_v1._image_reply(v_data, NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN unique_violation THEN
    RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
DO $owner$
DECLARE
    v_owner name;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO STRICT v_owner
    FROM pg_catalog.pg_proc
    WHERE oid = 'aap_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text)'::regprocedure;
    EXECUTE pg_catalog.format(
        'ALTER FUNCTION aap_assets_v1._image_saved_output_prefix(uuid,integer,uuid,boolean) OWNER TO %I',
        v_owner
    );
END;
$owner$;
