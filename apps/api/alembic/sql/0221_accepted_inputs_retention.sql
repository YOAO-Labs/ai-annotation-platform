-- Labeling is the source of accepted-input retention evidence.
CREATE FUNCTION aap_labeling_v1.accepted_image_input_referenced(p_input_id uuid)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_referenced boolean;
BEGIN
    IF p_input_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-labeling-accepted-input-retention/v1',
            'ok', false, 'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    SELECT EXISTS (
        SELECT 1
        FROM public.annotation_input_sources AS source
        WHERE source.input_id = p_input_id
        UNION ALL
        SELECT 1
        FROM public.prediction_metas AS meta
        JOIN public.predictions AS prediction
          ON prediction.id = meta.prediction_id
        WHERE meta.prediction_id IS NOT NULL
          AND meta.extra #>> '{accepted_input,input_id}' = p_input_id::text
        UNION ALL
        SELECT 1
        FROM public.failed_predictions AS failed
        WHERE failed.extra #>> '{accepted_input,input_id}' = p_input_id::text
          AND failed.dismissed_at IS NULL
          AND (
              failed.retry_count < 3
              OR COALESCE(failed.extra -> 'retry_pending' = 'true'::jsonb, false)
          )
    ) INTO v_referenced;
    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-accepted-input-retention/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'input_id', p_input_id,
            'referenced', v_referenced
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.accepted_image_input_referenced(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._lock_accepted_image_input_receipt(
    p_input_id uuid, p_task_id uuid, p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
BEGIN
    IF p_input_id IS NULL OR p_task_id IS NULL OR p_project_id IS NULL THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT input.* INTO v_input
        FROM public.asset_accepted_image_inputs AS input
        WHERE input.input_id = p_input_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'reference_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_missing');
    END IF;
    IF (v_input.task_id, v_input.project_id) IS DISTINCT FROM (p_task_id, p_project_id) THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    IF v_input.status = 'expired' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_expired');
    ELSIF v_input.status <> 'ready' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_not_ready');
    END IF;
    UPDATE public.asset_accepted_image_inputs AS input
    SET retire_after = NULL,
        retention_checked_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE input.input_id = p_input_id;
    v_input.retire_after := NULL;
    v_input.retention_checked_at := NULL;
    RETURN aap_assets_v1._accepted_image_input_reply(
        aap_assets_v1._accepted_image_input_record(v_input), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._lock_accepted_image_input_receipt(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._lock_accepted_image_input_pin(
    p_input_id uuid, p_project_id uuid, p_sha256 text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_input public.asset_accepted_image_inputs%ROWTYPE;
BEGIN
    IF p_input_id IS NULL OR p_project_id IS NULL
       OR p_sha256 IS NULL OR p_sha256 !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT input.* INTO v_input
        FROM public.asset_accepted_image_inputs AS input
        WHERE input.input_id = p_input_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'reference_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_missing');
    END IF;
    IF v_input.project_id IS DISTINCT FROM p_project_id
       OR v_input.sha256 IS DISTINCT FROM p_sha256 THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_scope_mismatch');
    END IF;
    IF v_input.status = 'expired' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_expired');
    ELSIF v_input.status <> 'ready' THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_not_ready');
    END IF;
    UPDATE public.asset_accepted_image_inputs AS input
    SET retire_after = NULL,
        retention_checked_at = NULL,
        updated_at = pg_catalog.clock_timestamp()
    WHERE input.input_id = p_input_id;
    v_input.retire_after := NULL;
    v_input.retention_checked_at := NULL;
    RETURN aap_assets_v1._accepted_image_input_reply(
        aap_assets_v1._accepted_image_input_record(v_input), NULL
    );
EXCEPTION WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._lock_accepted_image_input_pin(uuid, uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._annotation_input_sources_reply(
    p_data jsonb, p_code text
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL THEN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-input-sources/v1', 'ok', true, 'data', p_data
    ) ELSE pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-input-sources/v1', 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._annotation_input_sources_reply(jsonb, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.attach_annotation_input_source(
    p_annotation_id uuid, p_input_id uuid, p_sha256 text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_data jsonb;
    v_task_id uuid;
    v_project_id uuid;
    v_code text;
    v_attached integer;
BEGIN
    IF p_annotation_id IS NULL OR p_input_id IS NULL
       OR p_sha256 IS NULL OR p_sha256 !~ '^[0-9a-f]{64}$' THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT annotation.task_id, annotation.project_id
        INTO v_task_id, v_project_id
        FROM public.annotations AS annotation
        WHERE annotation.id = p_annotation_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'reference_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'source_missing');
    END IF;
    IF v_task_id IS NULL OR v_project_id IS NULL THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'input_scope_mismatch');
    END IF;
    v_reply := aap_assets_v1._lock_accepted_image_input_receipt(
        p_input_id, v_task_id, v_project_id
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-inputs/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_reply #>> '{error,code}';
        IF v_code IN (
            'reference_busy', 'input_missing', 'input_expired', 'input_not_ready',
            'input_scope_mismatch'
        ) THEN
            RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, v_code);
        END IF;
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'invalid_response');
    END IF;
    v_data := v_reply -> 'data';
    IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY[
            'input_id', 'job_id', 'task_id', 'project_id', 'actor_id', 'status',
            'task_version', 'task_source_generation', 'dataset_item_id',
            'task_source_snapshot', 'source_owner', 'source_generation',
            'source_snapshot', 'byte_size', 'sha256', 'error', 'created_at',
            'retire_after'
       ]) IS DISTINCT FROM '{}'::jsonb
       OR NOT (v_data ?& ARRAY[
            'input_id', 'job_id', 'task_id', 'project_id', 'actor_id', 'status',
            'task_version', 'task_source_generation', 'dataset_item_id',
            'task_source_snapshot', 'source_owner', 'source_generation',
            'source_snapshot', 'byte_size', 'sha256', 'error', 'created_at',
            'retire_after'
       ])
       OR v_data ->> 'input_id' IS DISTINCT FROM p_input_id::text
       OR v_data ->> 'task_id' IS DISTINCT FROM v_task_id::text
       OR v_data ->> 'project_id' IS DISTINCT FROM v_project_id::text
       OR v_data ->> 'status' IS DISTINCT FROM 'ready'
       OR v_data ->> 'sha256' IS DISTINCT FROM p_sha256
       OR v_data -> 'retire_after' IS DISTINCT FROM 'null'::jsonb THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'input_scope_mismatch');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.annotation_input_sources AS source
        WHERE source.annotation_id = p_annotation_id
          AND source.input_id = p_input_id
          AND source.sha256 IS DISTINCT FROM p_sha256
    ) THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'input_scope_mismatch');
    END IF;
    INSERT INTO public.annotation_input_sources(annotation_id, input_id, sha256)
    VALUES (p_annotation_id, p_input_id, p_sha256)
    ON CONFLICT (annotation_id, input_id) DO NOTHING;
    GET DIAGNOSTICS v_attached = ROW_COUNT;
    RETURN aap_labeling_v1._annotation_input_sources_reply(
        pg_catalog.jsonb_build_object(
            'annotation_id', p_annotation_id,
            'input_id', p_input_id,
            'sha256', p_sha256,
            'attached', v_attached = 1
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.attach_annotation_input_source(uuid, uuid, text)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.copy_annotation_input_sources(
    p_source_annotation_id uuid, p_target_annotation_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_locked_id uuid;
    v_source_project_id uuid;
    v_target_project_id uuid;
    v_input record;
    v_reply jsonb;
    v_data jsonb;
    v_code text;
    v_added integer;
    v_copied integer := 0;
    v_index integer;
    v_input_ids uuid[] := ARRAY[]::uuid[];
    v_input_shas text[] := ARRAY[]::text[];
BEGIN
    IF p_source_annotation_id IS NULL OR p_target_annotation_id IS NULL THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        FOR v_locked_id IN
            SELECT annotation.id
            FROM public.annotations AS annotation
            WHERE annotation.id = ANY(ARRAY[
                p_source_annotation_id, p_target_annotation_id
            ]::uuid[])
            ORDER BY annotation.id
            FOR UPDATE NOWAIT
        LOOP
            NULL;
        END LOOP;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'reference_busy');
    END;
    SELECT annotation.project_id INTO v_source_project_id
    FROM public.annotations AS annotation
    WHERE annotation.id = p_source_annotation_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'source_missing');
    END IF;
    SELECT annotation.project_id INTO v_target_project_id
    FROM public.annotations AS annotation
    WHERE annotation.id = p_target_annotation_id;
    IF NOT FOUND THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'source_missing');
    END IF;
    IF v_source_project_id IS NULL OR v_target_project_id IS NULL
       OR v_source_project_id IS DISTINCT FROM v_target_project_id THEN
        RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'input_scope_mismatch');
    END IF;
    FOR v_input IN
        SELECT source.input_id, source.sha256
        FROM public.annotation_input_sources AS source
        WHERE source.annotation_id = p_source_annotation_id
        ORDER BY source.input_id
    LOOP
        IF EXISTS (
            SELECT 1 FROM public.annotation_input_sources AS target_source
            WHERE target_source.annotation_id = p_target_annotation_id
              AND target_source.input_id = v_input.input_id
              AND target_source.sha256 IS DISTINCT FROM v_input.sha256
        ) THEN
            RETURN aap_labeling_v1._annotation_input_sources_reply(
                NULL, 'input_scope_mismatch'
            );
        END IF;
        v_reply := aap_assets_v1._lock_accepted_image_input_pin(
            v_input.input_id, v_source_project_id, v_input.sha256
        );
        IF v_reply IS NULL
           OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-inputs/v1'
           OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
            RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'invalid_response');
        END IF;
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_code := v_reply #>> '{error,code}';
            IF v_code IN (
                'reference_busy', 'input_missing', 'input_expired',
                'input_not_ready', 'input_scope_mismatch'
            ) THEN
                RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, v_code);
            END IF;
            RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'invalid_response');
        END IF;
        v_data := v_reply -> 'data';
        IF pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
           OR (v_data - ARRAY[
                'input_id', 'job_id', 'task_id', 'project_id', 'actor_id', 'status',
                'task_version', 'task_source_generation', 'dataset_item_id',
                'task_source_snapshot', 'source_owner', 'source_generation',
                'source_snapshot', 'byte_size', 'sha256', 'error', 'created_at',
                'retire_after'
           ]) IS DISTINCT FROM '{}'::jsonb
           OR NOT (v_data ?& ARRAY[
                'input_id', 'job_id', 'task_id', 'project_id', 'actor_id', 'status',
                'task_version', 'task_source_generation', 'dataset_item_id',
                'task_source_snapshot', 'source_owner', 'source_generation',
                'source_snapshot', 'byte_size', 'sha256', 'error', 'created_at',
                'retire_after'
           ])
           OR v_data ->> 'input_id' IS DISTINCT FROM v_input.input_id::text
           OR v_data ->> 'project_id' IS DISTINCT FROM v_source_project_id::text
           OR v_data ->> 'status' IS DISTINCT FROM 'ready'
           OR v_data ->> 'sha256' IS DISTINCT FROM v_input.sha256
           OR v_data -> 'retire_after' IS DISTINCT FROM 'null'::jsonb THEN
            RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'invalid_response');
        END IF;
        v_input_ids := pg_catalog.array_append(v_input_ids, v_input.input_id);
        v_input_shas := pg_catalog.array_append(v_input_shas, v_input.sha256);
    END LOOP;
    IF COALESCE(pg_catalog.array_length(v_input_ids, 1), 0) > 0 THEN
        FOR v_index IN 1..pg_catalog.array_length(v_input_ids, 1) LOOP
        INSERT INTO public.annotation_input_sources(annotation_id, input_id, sha256)
        VALUES (
            p_target_annotation_id, v_input_ids[v_index], v_input_shas[v_index]
        )
        ON CONFLICT (annotation_id, input_id) DO NOTHING;
        GET DIAGNOSTICS v_added = ROW_COUNT;
        v_copied := v_copied + v_added;
        END LOOP;
    END IF;
    RETURN aap_labeling_v1._annotation_input_sources_reply(
        pg_catalog.jsonb_build_object(
            'source_annotation_id', p_source_annotation_id,
            'target_annotation_id', p_target_annotation_id,
            'copied', v_copied
        ), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'reference_busy');
WHEN OTHERS THEN
    RETURN aap_labeling_v1._annotation_input_sources_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.copy_annotation_input_sources(uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._inherit_annotation_input_sources()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_parent_project_id uuid;
BEGIN
    IF NEW.parent_annotation_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT parent.project_id INTO v_parent_project_id
    FROM public.annotations AS parent
    WHERE parent.id = NEW.parent_annotation_id;
    IF NOT FOUND OR NEW.project_id IS NULL
       OR v_parent_project_id IS NULL
       OR NEW.project_id IS DISTINCT FROM v_parent_project_id THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'annotation parent input lineage must stay within one project';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM public.annotation_input_sources AS parent_source
        JOIN public.annotation_input_sources AS child_source
          ON child_source.annotation_id = NEW.id
         AND child_source.input_id = parent_source.input_id
        WHERE parent_source.annotation_id = NEW.parent_annotation_id
          AND child_source.sha256 IS DISTINCT FROM parent_source.sha256
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'annotation input evidence digest changed';
    END IF;
    INSERT INTO public.annotation_input_sources(annotation_id, input_id, sha256)
    SELECT NEW.id, source.input_id, source.sha256
    FROM public.annotation_input_sources AS source
    WHERE source.annotation_id = NEW.parent_annotation_id
    ON CONFLICT (annotation_id, input_id) DO NOTHING;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._inherit_annotation_input_sources()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_inherit_annotation_input_sources
AFTER INSERT OR UPDATE OF parent_annotation_id ON public.annotations
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._inherit_annotation_input_sources();
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._propagate_annotation_input_source()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.pg_trigger_depth() > 1 THEN
        RETURN NEW;
    END IF;
    WITH RECURSIVE descendants(annotation_id, project_id, path) AS (
        SELECT child.id, child.project_id,
               ARRAY[NEW.annotation_id, child.id]::uuid[]
        FROM public.annotations AS parent
        JOIN public.annotations AS child
          ON child.parent_annotation_id = parent.id
         AND child.project_id = parent.project_id
        WHERE parent.id = NEW.annotation_id
          AND parent.project_id IS NOT NULL
        UNION ALL
        SELECT child.id, child.project_id, descendants.path || child.id
        FROM public.annotations AS child
        JOIN descendants ON child.parent_annotation_id = descendants.annotation_id
            AND child.project_id = descendants.project_id
        WHERE NOT child.id = ANY(descendants.path)
    )
    INSERT INTO public.annotation_input_sources(annotation_id, input_id, sha256)
    SELECT descendants.annotation_id, NEW.input_id, NEW.sha256
    FROM descendants
    ON CONFLICT (annotation_id, input_id) DO NOTHING;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._propagate_annotation_input_source()
    FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_propagate_annotation_input_source
AFTER INSERT ON public.annotation_input_sources
FOR EACH ROW EXECUTE FUNCTION aap_labeling_v1._propagate_annotation_input_source();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.sweep_accepted_image_inputs(p_page_size integer)
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
    v_refs jsonb;
    v_prefix text;
    v_retired integer := 0;
    v_now timestamptz;
BEGIN
    IF p_page_size IS NULL OR p_page_size NOT BETWEEN 1 AND 100 THEN
        RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_request');
    END IF;
    FOR v_candidate IN
        SELECT input.input_id
        FROM public.asset_accepted_image_inputs AS input
        WHERE (
            input.status = 'ready'
            AND (
                (input.retire_after IS NOT NULL
                 AND input.retire_after <= pg_catalog.clock_timestamp())
                OR input.retention_checked_at IS NULL
                OR input.retention_checked_at
                    <= pg_catalog.clock_timestamp() - interval '1 day'
            )
        ) OR (
            input.status = 'materializing'
            AND input.lease_expires_at <= pg_catalog.clock_timestamp()
            AND (input.retention_checked_at IS NULL
                 OR input.retention_checked_at
                    <= pg_catalog.clock_timestamp() - interval '1 day')
        )
        ORDER BY input.retire_after NULLS FIRST, input.created_at, input.input_id
        LIMIT p_page_size
        FOR UPDATE OF input SKIP LOCKED
    LOOP
        SELECT input.* INTO v_input
        FROM public.asset_accepted_image_inputs AS input
        WHERE input.input_id = v_candidate.input_id;
        v_now := pg_catalog.clock_timestamp();
        IF v_input.status = 'materializing' THEN
            v_reply := aap_assets_v1._accepted_image_input_job_consistency(
                v_input.job_id, v_input.task_id, v_input.actor_id, v_input.project_id
            );
            IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                CONTINUE;
            END IF;
            v_job := v_reply -> 'data';
            IF v_job -> 'consistent' IS DISTINCT FROM 'true'::jsonb THEN
                CONTINUE;
            END IF;
            IF v_job -> 'active' = 'true'::jsonb THEN
                UPDATE public.asset_accepted_image_inputs AS input
                SET retention_checked_at = v_now,
                    updated_at = v_now
                WHERE input.input_id = v_input.input_id;
                CONTINUE;
            END IF;
            SELECT attempt.* INTO v_attempt
            FROM public.asset_accepted_image_input_attempts AS attempt
            WHERE attempt.input_id = v_input.input_id
              AND attempt.generation = v_input.generation
              AND attempt.lease_token = v_input.lease_token
              AND attempt.status = 'leased';
            IF FOUND THEN
                UPDATE public.asset_accepted_image_input_attempts AS attempt
                SET status = 'abandoned', completed_at = v_now
                WHERE attempt.input_id = v_attempt.input_id
                  AND attempt.generation = v_attempt.generation;
                PERFORM aap_assets_v1._queue_accepted_image_input_garbage(
                    v_attempt.prefix, 'job_inactive', v_now
                );
            END IF;
            UPDATE public.asset_accepted_image_inputs AS input
            SET status = 'failed', error_code = 'job_not_active',
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_lease_token = v_input.lease_token,
                last_outcome = 'failed_terminal',
                retention_checked_at = v_now,
                updated_at = v_now
            WHERE input.input_id = v_input.input_id;
            CONTINUE;
        END IF;

        v_reply := aap_assets_v1._accepted_image_input_job_consistency(
            v_input.job_id, v_input.task_id, v_input.actor_id, v_input.project_id
        );
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            CONTINUE;
        END IF;
        v_job := v_reply -> 'data';
        IF v_job -> 'consistent' IS DISTINCT FROM 'true'::jsonb THEN
            CONTINUE;
        END IF;
        v_refs := aap_labeling_v1.accepted_image_input_referenced(v_input.input_id);
        IF v_refs IS NULL
           OR v_refs ->> 'schema' IS DISTINCT FROM 'aap-labeling-accepted-input-retention/v1'
           OR v_refs -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_refs #>> '{data,input_id}' IS DISTINCT FROM v_input.input_id::text
           OR pg_catalog.jsonb_typeof(v_refs #> '{data,referenced}') IS DISTINCT FROM 'boolean' THEN
            CONTINUE;
        END IF;
        IF v_job -> 'active' = 'true'::jsonb
           OR v_refs #> '{data,referenced}' = 'true'::jsonb THEN
            UPDATE public.asset_accepted_image_inputs AS input
            SET retire_after = NULL,
                retention_checked_at = v_now,
                updated_at = v_now
            WHERE input.input_id = v_input.input_id;
            CONTINUE;
        END IF;
        IF v_input.retire_after IS NULL OR v_input.retire_after > v_now THEN
            UPDATE public.asset_accepted_image_inputs AS input
            SET retire_after = COALESCE(
                    input.retire_after, v_now + interval '30 days'
                ),
                retention_checked_at = v_now,
                updated_at = v_now
            WHERE input.input_id = v_input.input_id;
            CONTINUE;
        END IF;
        v_prefix := pg_catalog.regexp_replace(v_input.blob_key, 'source$', '');
        IF NOT aap_assets_v1._accepted_image_input_prefix_valid(v_prefix) THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
        END IF;
        SELECT attempt.* INTO v_attempt
        FROM public.asset_accepted_image_input_attempts AS attempt
        WHERE attempt.input_id = v_input.input_id
          AND attempt.prefix = v_prefix
          AND attempt.status = 'ready';
        IF NOT FOUND THEN
            RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
        END IF;
        UPDATE public.asset_accepted_image_input_attempts AS attempt
        SET status = 'expired', completed_at = v_now
        WHERE attempt.input_id = v_attempt.input_id
          AND attempt.generation = v_attempt.generation;
        PERFORM aap_assets_v1._queue_accepted_image_input_garbage(
            v_prefix, 'input_expired', v_now
        );
        UPDATE public.asset_accepted_image_inputs AS input
        SET status = 'expired',
            blob_key = NULL,
            blob_version_id = NULL,
            blob_etag = NULL,
            error_code = NULL,
            retention_checked_at = v_now,
            updated_at = v_now
        WHERE input.input_id = v_input.input_id;
        v_retired := v_retired + 1;
    END LOOP;
    RETURN aap_assets_v1._accepted_image_input_reply(
        pg_catalog.jsonb_build_object('retired', v_retired), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'input_busy');
WHEN OTHERS THEN
    RETURN aap_assets_v1._accepted_image_input_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.sweep_accepted_image_inputs(integer)
    FROM PUBLIC;
