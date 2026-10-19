-- Input copying is leased by Assets and always uses an immutable attempt prefix.
CREATE FUNCTION aap_assets_v1.claim_accepted_image_input(
    p_worker_id uuid, p_input_id uuid, p_lease_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
    v_attempt public.asset_accepted_image_input_attempts%ROWTYPE;
    v_candidate record;
    v_reply jsonb;
    v_job jsonb;
    v_source jsonb;
    v_prefix text;
    v_now timestamptz;
    v_expires timestamptz;
    v_token uuid;
    v_generation bigint;
    v_code text;
    v_claim jsonb;
BEGIN
    IF p_worker_id IS NULL OR p_lease_seconds NOT BETWEEN 60 AND 7200 THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    IF p_input_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.asset_accepted_image_inputs AS input
        WHERE input.input_id = p_input_id
    ) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_missing');
    END IF;
    FOR v_candidate IN
        SELECT input.input_id
        FROM public.asset_accepted_image_inputs AS input
        WHERE (p_input_id IS NULL OR input.input_id = p_input_id)
          AND (
              input.status = 'pending'
              OR (input.status = 'materializing'
                  AND input.lease_expires_at <= pg_catalog.clock_timestamp())
          )
        ORDER BY input.created_at, input.input_id
        LIMIT CASE WHEN p_input_id IS NULL THEN 100 ELSE 1 END
        FOR UPDATE OF input SKIP LOCKED
    LOOP
        SELECT input.* INTO v_input
        FROM public.asset_accepted_image_inputs AS input
        WHERE input.input_id = v_candidate.input_id;

        IF v_input.status = 'materializing' THEN
            SELECT attempt.* INTO v_attempt
            FROM public.asset_accepted_image_input_attempts AS attempt
            WHERE attempt.input_id = v_input.input_id
              AND attempt.generation = v_input.generation
              AND attempt.lease_token = v_input.lease_token
              AND attempt.status = 'leased';
            IF FOUND THEN
                UPDATE public.asset_accepted_image_input_attempts AS attempt
                SET status = 'abandoned', completed_at = pg_catalog.clock_timestamp()
                WHERE attempt.input_id = v_attempt.input_id
                  AND attempt.generation = v_attempt.generation;
                PERFORM aap_assets_v1._queue_accepted_image_input_garbage(
                    v_attempt.prefix, 'lease_expired', pg_catalog.clock_timestamp()
                );
                v_input.last_lease_token := v_attempt.lease_token;
                v_input.last_outcome := 'lease_expired';
            END IF;
            v_input.lease_owner := NULL;
            v_input.lease_token := NULL;
            v_input.lease_expires_at := NULL;
            v_input.status := 'pending';
        END IF;

        v_reply := aap_assets_v1._accepted_image_input_job_scope(
            v_input.job_id, v_input.task_id, v_input.actor_id, true
        );
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_reply #>> '{error,code}';
            IF v_code = 'job_not_active' THEN
                UPDATE public.asset_accepted_image_inputs AS input
                SET status = 'failed', error_code = 'job_not_active',
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    last_lease_token = COALESCE(v_input.last_lease_token, input.last_lease_token),
                    last_outcome = CASE
                        WHEN COALESCE(v_input.last_lease_token, input.last_lease_token) IS NULL
                            THEN NULL ELSE 'failed_terminal'
                    END,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE input.input_id = v_input.input_id;
                CONTINUE;
            END IF;
            RETURN v_reply;
        END IF;
        v_job := v_reply -> 'data';

        v_reply := aap_assets_v1._accepted_image_input_source_scope(
            NULL, v_input.task_id, false
        );
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_reply #>> '{error,code}';
            IF v_code IN ('source_missing', 'source_changed', 'source_not_ready', 'not_image') THEN
                UPDATE public.asset_accepted_image_inputs AS input
                SET status = 'failed', error_code = CASE
                        WHEN v_code = 'source_missing' THEN 'source_missing'
                        ELSE 'source_changed'
                    END,
                    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                    last_lease_token = COALESCE(v_input.last_lease_token, input.last_lease_token),
                    last_outcome = CASE
                        WHEN COALESCE(v_input.last_lease_token, input.last_lease_token) IS NULL
                            THEN NULL ELSE 'failed_terminal'
                    END,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE input.input_id = v_input.input_id;
                CONTINUE;
            END IF;
            RETURN v_reply;
        END IF;
        v_source := v_reply -> 'data';
        IF v_source ->> 'source_snapshot' IS DISTINCT FROM v_input.source_snapshot
           OR v_source ->> 'source_generation' IS DISTINCT FROM v_input.source_generation::text
           OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_input.source_owner_kind
           OR v_source #>> '{owner,id}' IS DISTINCT FROM v_input.source_owner_id::text
           OR v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
           OR v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
           OR aap_assets_v1._image_normalize_identity(v_source -> 'object_identity')
                IS DISTINCT FROM v_input.source_identity THEN
            UPDATE public.asset_accepted_image_inputs AS input
            SET status = 'failed', error_code = 'source_changed',
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = COALESCE(v_input.last_lease_token, input.last_lease_token),
                last_outcome = CASE
                    WHEN COALESCE(v_input.last_lease_token, input.last_lease_token) IS NULL
                        THEN NULL ELSE 'failed_terminal'
                END,
                updated_at = pg_catalog.clock_timestamp()
            WHERE input.input_id = v_input.input_id;
            CONTINUE;
        END IF;
        IF v_input.attempt_count >= 5
           OR v_input.generation = 9223372036854775807 THEN
            UPDATE public.asset_accepted_image_inputs AS input
            SET status = 'failed', error_code = 'resource_budget_exceeded',
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                updated_at = pg_catalog.clock_timestamp()
            WHERE input.input_id = v_input.input_id;
            CONTINUE;
        END IF;

        v_now := pg_catalog.clock_timestamp();
        v_expires := v_now + pg_catalog.make_interval(secs => p_lease_seconds);
        v_token := pg_catalog.gen_random_uuid();
        v_generation := v_input.generation + 1;
        v_prefix := 'accepted-inputs/' || v_input.input_id::text
            || '/g' || v_generation::text || '/a' || v_token::text || '/';
        INSERT INTO public.asset_accepted_image_input_attempts (
            input_id, generation, lease_token, prefix, status, lease_expires_at
        ) VALUES (
            v_input.input_id, v_generation, v_token, v_prefix, 'leased', v_expires
        );
        UPDATE public.asset_accepted_image_inputs AS input
        SET status = 'materializing',
            generation = v_generation,
            attempt_count = input.attempt_count + 1,
            lease_owner = p_worker_id,
            lease_token = v_token,
            lease_expires_at = v_expires,
            last_lease_token = COALESCE(v_input.last_lease_token, input.last_lease_token),
            last_outcome = COALESCE(v_input.last_outcome, input.last_outcome),
            error_code = NULL,
            updated_at = v_now
        WHERE input.input_id = v_input.input_id
        RETURNING * INTO v_input;
        v_claim := pg_catalog.jsonb_build_object(
            'input', aap_assets_v1._accepted_image_input_record(v_input),
            'source', v_input.source_facts,
            'generation', v_generation,
            'lease_token', v_token,
            'lease_expires_at', v_expires,
            'blob_key', v_prefix || 'source'
        );
        RETURN aap_assets_v1._accepted_image_input_reply(
            pg_catalog.jsonb_build_object('claim', v_claim), NULL
        );
    END LOOP;
    RETURN aap_assets_v1._accepted_image_input_reply(
        pg_catalog.jsonb_build_object('claim', NULL), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.claim_accepted_image_input(uuid, uuid, integer)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.heartbeat_accepted_image_input(
    p_input_id uuid, p_generation bigint, p_lease_token uuid, p_lease_seconds integer
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
    v_attempt public.asset_accepted_image_input_attempts%ROWTYPE;
    v_expires timestamptz;
BEGIN
    IF p_input_id IS NULL OR p_generation IS NULL OR p_generation <= 0
       OR p_lease_token IS NULL OR p_lease_seconds NOT BETWEEN 60 AND 7200 THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT input.* INTO v_input
        FROM public.asset_accepted_image_inputs AS input
        WHERE input.input_id = p_input_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_missing');
    END IF;
    IF v_input.status <> 'materializing'
       OR (v_input.generation, v_input.lease_token)
            IS DISTINCT FROM (p_generation, p_lease_token)
       OR v_input.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'lease_lost');
    END IF;
    SELECT attempt.* INTO v_attempt
    FROM public.asset_accepted_image_input_attempts AS attempt
    WHERE attempt.input_id = p_input_id
      AND attempt.generation = p_generation
      AND attempt.lease_token = p_lease_token
      AND attempt.status = 'leased'
    FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'lease_lost');
    END IF;
    v_expires := pg_catalog.clock_timestamp()
        + pg_catalog.make_interval(secs => p_lease_seconds);
    UPDATE public.asset_accepted_image_input_attempts AS attempt
    SET lease_expires_at = v_expires
    WHERE attempt.input_id = p_input_id AND attempt.generation = p_generation;
    UPDATE public.asset_accepted_image_inputs AS input
    SET lease_expires_at = v_expires,
        updated_at = pg_catalog.clock_timestamp()
    WHERE input.input_id = p_input_id;
    RETURN aap_assets_v1._accepted_image_input_reply(
        pg_catalog.jsonb_build_object(
            'input_id', p_input_id,
            'generation', p_generation,
            'lease_token', p_lease_token,
            'lease_expires_at', v_expires
        ), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.heartbeat_accepted_image_input(uuid, bigint, uuid, integer)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_accepted_image_input(
    p_input_id uuid, p_generation bigint, p_lease_token uuid, p_blob jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
    v_attempt public.asset_accepted_image_input_attempts%ROWTYPE;
    v_reply jsonb;
    v_source jsonb;
    v_prefix text;
    v_size bigint;
    v_version text;
    v_etag text;
    v_sha text;
    v_key text;
    v_code text;
BEGIN
    IF p_input_id IS NULL OR p_generation IS NULL OR p_generation <= 0
       OR p_lease_token IS NULL
       OR pg_catalog.jsonb_typeof(p_blob) IS DISTINCT FROM 'object'
       OR (p_blob - ARRAY['key', 'version_id', 'etag', 'byte_size', 'sha256'])
            IS DISTINCT FROM '{}'::jsonb
       OR NOT (p_blob ?& ARRAY['key', 'version_id', 'etag', 'byte_size', 'sha256'])
       OR pg_catalog.jsonb_typeof(p_blob -> 'key') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_blob -> 'version_id') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_blob -> 'etag') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_blob -> 'byte_size') IS DISTINCT FROM 'number'
       OR p_blob ->> 'byte_size' !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.char_length(p_blob ->> 'byte_size') > 19
       OR pg_catalog.jsonb_typeof(p_blob -> 'sha256') IS DISTINCT FROM 'string'
       OR p_blob ->> 'sha256' !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    v_key := p_blob ->> 'key';
    v_version := p_blob ->> 'version_id';
    v_etag := p_blob ->> 'etag';
    v_sha := p_blob ->> 'sha256';
    IF pg_catalog.char_length(v_version) NOT BETWEEN 1 AND 255
       OR pg_catalog.btrim(v_version) = '' OR v_version = 'null'
       OR pg_catalog.char_length(v_etag) NOT BETWEEN 1 AND 255 THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'versioning_required');
    END IF;
    BEGIN
        v_size := (p_blob ->> 'byte_size')::bigint;
    EXCEPTION WHEN numeric_value_out_of_range OR invalid_text_representation THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END;
    BEGIN
        SELECT input.* INTO v_input
        FROM public.asset_accepted_image_inputs AS input
        WHERE input.input_id = p_input_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_missing');
    END IF;
    IF v_input.status = 'ready' AND v_input.generation = p_generation
       AND v_input.last_lease_token = p_lease_token
       AND v_input.last_outcome = 'ready'
       AND (v_input.blob_key, v_input.blob_version_id, v_input.blob_etag,
            v_input.byte_size, v_input.sha256)
           IS NOT DISTINCT FROM (v_key, v_version, v_etag, v_size, v_sha) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(
            aap_assets_v1._accepted_image_input_record(v_input), NULL
        );
    END IF;
    IF v_input.status <> 'materializing'
       OR (v_input.generation, v_input.lease_token)
            IS DISTINCT FROM (p_generation, p_lease_token)
       OR v_input.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'lease_lost');
    END IF;
    SELECT attempt.* INTO v_attempt
    FROM public.asset_accepted_image_input_attempts AS attempt
    WHERE attempt.input_id = p_input_id
      AND attempt.generation = p_generation
      AND attempt.lease_token = p_lease_token
      AND attempt.status = 'leased'
    FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'lease_lost');
    END IF;
    v_prefix := v_attempt.prefix;
    IF NOT aap_assets_v1._accepted_image_input_prefix_valid(v_prefix)
       OR v_key IS DISTINCT FROM v_prefix || 'source' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    IF v_size IS DISTINCT FROM (v_input.source_identity ->> 'byte_size')::bigint THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_corrupt');
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_job_scope(
        v_input.job_id, v_input.task_id, v_input.actor_id, true
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code = 'job_not_active' THEN
            UPDATE public.asset_accepted_image_input_attempts AS attempt
            SET status = 'failed', completed_at = pg_catalog.clock_timestamp()
            WHERE attempt.input_id = p_input_id AND attempt.generation = p_generation;
            UPDATE public.asset_accepted_image_inputs AS input
            SET status = 'failed', error_code = 'job_not_active',
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = p_lease_token, last_outcome = 'failed_terminal',
                updated_at = pg_catalog.clock_timestamp()
            WHERE input.input_id = p_input_id;
            PERFORM aap_assets_v1._queue_accepted_image_input_garbage(
                v_prefix, 'job_inactive', pg_catalog.clock_timestamp()
            );
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'job_not_active');
        END IF;
        RETURN v_reply;
    END IF;
    v_reply := aap_assets_v1._accepted_image_input_source_scope(
        NULL, v_input.task_id, false
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN ('source_missing', 'source_changed', 'source_not_ready', 'not_image') THEN
            UPDATE public.asset_accepted_image_input_attempts AS attempt
            SET status = 'failed', completed_at = pg_catalog.clock_timestamp()
            WHERE attempt.input_id = p_input_id AND attempt.generation = p_generation;
            UPDATE public.asset_accepted_image_inputs AS input
            SET status = 'failed', error_code = CASE
                    WHEN v_code = 'source_missing' THEN 'source_missing'
                    ELSE 'source_changed'
                END,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = p_lease_token, last_outcome = 'failed_terminal',
                updated_at = pg_catalog.clock_timestamp()
            WHERE input.input_id = p_input_id;
            PERFORM aap_assets_v1._queue_accepted_image_input_garbage(
                v_prefix, 'source_changed', pg_catalog.clock_timestamp()
            );
            RETURN aap_assets_v1._accepted_image_input_reply(
                NULL, CASE WHEN v_code = 'source_missing'
                    THEN 'source_missing' ELSE 'source_changed' END
            );
        END IF;
        RETURN v_reply;
    END IF;
    v_source := v_reply -> 'data';
    IF v_source ->> 'source_snapshot' IS DISTINCT FROM v_input.source_snapshot
       OR v_source ->> 'source_generation' IS DISTINCT FROM v_input.source_generation::text
       OR v_source #>> '{owner,kind}' IS DISTINCT FROM v_input.source_owner_kind
       OR v_source #>> '{owner,id}' IS DISTINCT FROM v_input.source_owner_id::text
       OR v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
       OR v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb
       OR aap_assets_v1._image_normalize_identity(v_source -> 'object_identity')
            IS DISTINCT FROM v_input.source_identity THEN
        UPDATE public.asset_accepted_image_input_attempts AS attempt
        SET status = 'failed', completed_at = pg_catalog.clock_timestamp()
        WHERE attempt.input_id = p_input_id AND attempt.generation = p_generation;
        UPDATE public.asset_accepted_image_inputs AS input
        SET status = 'failed', error_code = 'source_changed',
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
            last_lease_token = p_lease_token, last_outcome = 'failed_terminal',
            updated_at = pg_catalog.clock_timestamp()
        WHERE input.input_id = p_input_id;
        PERFORM aap_assets_v1._queue_accepted_image_input_garbage(
            v_prefix, 'source_changed', pg_catalog.clock_timestamp()
        );
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'source_changed');
    END IF;
    UPDATE public.asset_accepted_image_inputs AS input
    SET status = 'ready',
        blob_key = v_key,
        blob_version_id = v_version,
        blob_etag = v_etag,
        byte_size = v_size,
        sha256 = v_sha,
        error_code = NULL,
        lease_owner = NULL,
        lease_token = NULL,
        lease_expires_at = NULL,
        last_lease_token = p_lease_token,
        last_outcome = 'ready',
        retire_after = NULL,
        retention_checked_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE input.input_id = p_input_id
    RETURNING * INTO v_input;
    UPDATE public.asset_accepted_image_input_attempts AS attempt
    SET status = 'ready', completed_at = pg_catalog.clock_timestamp()
    WHERE attempt.input_id = p_input_id AND attempt.generation = p_generation;
    RETURN aap_assets_v1._accepted_image_input_reply(
        aap_assets_v1._accepted_image_input_record(v_input), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_accepted_image_input(uuid, bigint, uuid, jsonb)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fail_accepted_image_input(
    p_input_id uuid, p_generation bigint, p_lease_token uuid, p_error_code text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
    v_attempt public.asset_accepted_image_input_attempts%ROWTYPE;
    v_retryable boolean;
    v_status text;
BEGIN
    IF p_input_id IS NULL OR p_generation IS NULL OR p_generation <= 0
       OR p_lease_token IS NULL OR p_error_code IS NULL
       OR p_error_code NOT IN (
            'invalid_request', 'invalid_response', 'storage_unavailable',
            'account_not_active', 'account_busy', 'invalid_account_role',
            'source_missing', 'source_changed', 'source_busy', 'source_not_ready',
            'task_not_visible', 'task_busy', 'permission_denied', 'not_image',
            'asset_missing', 'already_ready', 'not_eligible', 'retry_cooldown',
            'operation_conflict', 'operation_busy', 'lease_lost',
            'dependency_changed', 'resource_budget_exceeded', 'object_missing',
            'owner_changed', 'unsupported_dimensions', 'resource_limit',
            'invalid_coordinate', 'timeout', 'source_unavailable',
            'color_profile_unavailable', 'multi_page_unsupported',
            'dimension_mismatch', 'generator_unavailable', 'decode_failed',
            'encode_failed', 'tile_grid_mismatch', 'upload_verification_failed',
            'inconsistent_ready', 'input_missing', 'input_scope_mismatch',
            'input_not_ready', 'input_expired', 'input_corrupt', 'input_busy',
            'job_not_active', 'job_busy', 'reference_busy', 'versioning_required'
       ) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT input.* INTO v_input
        FROM public.asset_accepted_image_inputs AS input
        WHERE input.input_id = p_input_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_missing');
    END IF;
    IF v_input.generation = p_generation
       AND v_input.last_lease_token = p_lease_token
       AND v_input.last_outcome IN ('failed_retryable', 'failed_terminal')
       AND v_input.error_code = p_error_code THEN
        RETURN aap_assets_v1._accepted_image_input_reply(
            aap_assets_v1._accepted_image_input_record(v_input), NULL
        );
    END IF;
    IF v_input.status <> 'materializing'
       OR (v_input.generation, v_input.lease_token)
            IS DISTINCT FROM (p_generation, p_lease_token)
       OR v_input.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'lease_lost');
    END IF;
    SELECT attempt.* INTO v_attempt
    FROM public.asset_accepted_image_input_attempts AS attempt
    WHERE attempt.input_id = p_input_id
      AND attempt.generation = p_generation
      AND attempt.lease_token = p_lease_token
      AND attempt.status = 'leased'
    FOR UPDATE NOWAIT;
    IF NOT FOUND OR v_attempt.lease_expires_at <= pg_catalog.clock_timestamp() THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'lease_lost');
    END IF;
    v_retryable := p_error_code IN (
        'storage_unavailable', 'timeout', 'source_unavailable', 'source_busy',
        'task_busy', 'input_busy', 'job_busy', 'reference_busy'
    ) AND v_input.attempt_count < 5;
    v_status := CASE WHEN v_retryable THEN 'pending' ELSE 'failed' END;
    UPDATE public.asset_accepted_image_input_attempts AS attempt
    SET status = 'failed', completed_at = pg_catalog.clock_timestamp()
    WHERE attempt.input_id = p_input_id AND attempt.generation = p_generation;
    UPDATE public.asset_accepted_image_inputs AS input
    SET status = v_status,
        error_code = p_error_code,
        lease_owner = NULL,
        lease_token = NULL,
        lease_expires_at = NULL,
        last_lease_token = p_lease_token,
        last_outcome = CASE WHEN v_retryable
            THEN 'failed_retryable' ELSE 'failed_terminal' END,
        updated_at = pg_catalog.clock_timestamp()
    WHERE input.input_id = p_input_id
    RETURNING * INTO v_input;
    PERFORM aap_assets_v1._queue_accepted_image_input_garbage(
        v_attempt.prefix, 'attempt_failed', pg_catalog.clock_timestamp()
    );
    RETURN aap_assets_v1._accepted_image_input_reply(
        aap_assets_v1._accepted_image_input_record(v_input), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fail_accepted_image_input(uuid, bigint, uuid, text)
    FROM PUBLIC;
