ALTER TABLE public.asset_accepted_image_input_manifest_members
    DROP CONSTRAINT ck_asset_accepted_image_input_manifest_member_state,
    ADD CONSTRAINT ck_asset_accepted_image_input_manifest_member_state CHECK (
        (state = 'unprepared' AND source IS NOT NULL
            AND input_id IS NULL AND error_code IS NULL
            AND outcome_byte_size IS NULL AND outcome_sha256 IS NULL)
        OR (state = 'preparing' AND source IS NOT NULL
            AND input_id IS NOT NULL AND error_code IS NULL
            AND outcome_byte_size IS NULL AND outcome_sha256 IS NULL)
        OR (state = 'ready' AND source IS NOT NULL
            AND input_id IS NOT NULL AND error_code IS NULL
            AND outcome_byte_size IS NOT NULL AND outcome_byte_size >= 0
            AND outcome_sha256 IS NOT NULL AND outcome_sha256 ~ '^[0-9a-f]{64}$')
        OR (state = 'reference' AND dataset_item_id IS NOT NULL
            AND file_path ~ ('^__aap_reference__/' || dataset_item_id::text || '/[0-9a-f]{64}$')
            AND source IS NULL AND input_id IS NULL AND error_code IS NULL
            AND outcome_byte_size IS NULL AND outcome_sha256 IS NULL)
        OR (state = 'failed' AND error_code IS NOT NULL
            AND (input_id IS NULL OR source IS NOT NULL)
            AND outcome_byte_size IS NULL AND outcome_sha256 IS NULL)
    );
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._image_manifest_reference_matches(
    p_binding jsonb, p_member jsonb, p_task_source jsonb
)
RETURNS boolean LANGUAGE sql VOLATILE STRICT PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM yoao_assets_v1.directory_reference_binding(
            (p_member ->> 'dataset_item_id')::uuid, p_member ->> 'file_path', false
        ) AS registered
        LEFT JOIN LATERAL yoao_assets_storage_v1.gate_facts(registered.location_id, NULL) AS gate ON true
        WHERE registered.error_code IS NULL AND registered.sha256 ~ '^[0-9a-f]{64}$'
          AND p_member ->> 'file_path' = '__aap_reference__/' || (p_member ->> 'dataset_item_id') || '/' || registered.sha256
          AND COALESCE(gate.state, 'active') = 'active'
          AND p_binding = pg_catalog.jsonb_build_object(
              'task_id', p_member -> 'task_id', 'batch_id', p_member -> 'batch_id',
              'item_id', p_member -> 'dataset_item_id', 'file_path', p_member -> 'file_path',
              'file_name', p_task_source -> 'file_name', 'version', p_member -> 'task_version',
              'reference', pg_catalog.jsonb_build_object(
                  'dataset_id', registered.dataset_id, 'asset_id', registered.asset_id,
                  'sha256', registered.sha256, 'size', registered.size_bytes,
                  'width', registered.width, 'height', registered.height,
                  'run_id', registered.run_id, 'accepted_revision_id', registered.accepted_revision_id,
                  'source_id', registered.source_id, 'source_revision', registered.source_revision,
                  'location_id', registered.location_id, 'location_revision', registered.location_revision,
                  'maintenance_revision', COALESCE(gate.revision, 1),
                  'connection_id', registered.connection_id, 'connection_revision', registered.connection_revision
              )
          )
    )
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_assets_v1.capture_accepted_image_input_manifest(
    p_job_id uuid, p_actor_id uuid, p_project_id uuid, p_selection jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_reply jsonb;
    v_authority jsonb;
    v_reference_bindings jsonb;
    v_binding jsonb;
    v_reference jsonb;
    v_state text;
    v_existing public.asset_accepted_image_input_manifests%ROWTYPE;
    v_cursor_name text;
    v_cursor refcursor;
    v_row jsonb;
    v_source_reply jsonb;
    v_task_reply jsonb;
    v_source jsonb;
    v_task_source jsonb;
    v_kind text;
    v_error text;
    v_expected_ordinal bigint := 1;
    v_candidate_count bigint;
    v_selected_count bigint;
    v_prepared bigint := 0;
    v_digest text;
    v_hash_input jsonb;
    v_task_id uuid;
    v_item_id uuid;
    v_source_error text;
    v_task_scope_changed boolean;
    v_message text;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL
       OR NOT yoao_assets_v1._accepted_image_input_manifest_selection_valid(p_selection) THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_request');
    END IF;
    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-accepted-image-manifest:' || p_job_id::text, 0)
    ) THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
    END IF;
    v_reply := yoao_assets_v1._accepted_image_input_manifest_current_scope(
        p_actor_id, p_project_id
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    SELECT manifest.* INTO v_existing
    FROM public.asset_accepted_image_input_manifests AS manifest
    WHERE manifest.job_id = p_job_id;
    IF FOUND THEN
        IF (v_existing.actor_id, v_existing.project_id, v_existing.selection)
            IS DISTINCT FROM (p_actor_id, p_project_id, p_selection) THEN
            RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
        END IF;
        v_reply := yoao_assets_v1._accepted_image_input_manifest_job_scope(
            p_job_id, p_actor_id, p_project_id, NULL,
            v_existing.selection, v_existing.selection_digest, false
        );
        IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(
            yoao_assets_v1._accepted_image_input_manifest_header(v_existing), NULL
        );
    END IF;
    v_reply := yoao_assets_v1._accepted_image_input_manifest_job_scope(
        p_job_id, p_actor_id, p_project_id, NULL, p_selection, NULL, true
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    -- Only the active Job locked above supplies bindings. The selector and the
    -- caller cannot assert that an opaque path is a registered reference.
    SELECT job.payload -> 'prediction_authority' INTO v_authority
    FROM yoao_jobs_v1.read_job(p_job_id) AS job;
    IF v_authority @> pg_catalog.jsonb_build_object(
           'schema', 2, 'job_kind', 'batch_predict', 'execution_scope', 'bulk',
           'actor_id', p_actor_id, 'project_id', p_project_id,
           'batch_id', p_selection -> 'batch_id', 'predict_mode', p_selection -> 'predict_mode',
           'credentials', pg_catalog.jsonb_build_object('schema', 1, 'actor_id', p_actor_id)
       )
       AND v_authority ->> 'snapshot_id' = yoao_jobs_v1.read_job_execution_snapshot(p_job_id)::text
       AND (v_authority ->> 'snapshot_digest') ~ '^[0-9a-f]{64}$'
       AND v_authority -> 'execution_unit' IN ('null'::jsonb, '"image"'::jsonb)
       AND v_authority #> '{credentials,api_key_id}' = v_authority -> 'api_key_id'
       AND ((v_authority -> 'api_key_id' = 'null'::jsonb
             AND pg_catalog.jsonb_typeof(v_authority #> '{credentials,jwt}') = 'object')
            OR (pg_catalog.jsonb_typeof(v_authority -> 'api_key_id') = 'string'
                AND v_authority #> '{credentials,jwt}' = 'null'::jsonb
                AND (v_authority ->> 'key_proof') ~ '^[0-9a-f]{64}$'))
       AND pg_catalog.jsonb_typeof(v_authority -> 'tasks') = 'array' THEN
        SELECT pg_catalog.jsonb_object_agg(binding ->> 'task_id', binding) INTO v_reference_bindings
        FROM (
            SELECT task AS binding, count(*) OVER (PARTITION BY task ->> 'task_id') AS occurrences
            FROM pg_catalog.jsonb_array_elements(v_authority -> 'tasks') AS tasks(task)
        ) AS bindings
        WHERE occurrences = 1 AND pg_catalog.jsonb_typeof(binding -> 'task_id') = 'string'
          AND pg_catalog.jsonb_typeof(binding -> 'reference') = 'object' AND NOT (binding ? 'original');
    END IF;

    v_reply := yoao_labeling_v1.open_accepted_image_input_manifest_members(
        p_actor_id, p_project_id, p_selection
    );
    IF v_reply IS NULL
       OR v_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-accepted-image-input-manifest/v1'
       OR pg_catalog.jsonb_typeof(v_reply -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_error := v_reply #>> '{error,code}';
        IF v_error IN (
            'invalid_request', 'account_not_active', 'account_busy', 'invalid_account_role',
            'task_not_visible', 'source_busy', 'source_missing', 'unsupported_media'
        ) THEN
            RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, v_error);
        END IF;
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    IF pg_catalog.jsonb_typeof(v_reply #> '{data,cursor}') IS DISTINCT FROM 'string' THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;
    v_cursor_name := v_reply #>> '{data,cursor}';
    v_cursor := v_cursor_name::refcursor;
    FETCH v_cursor INTO v_row;
    IF v_row IS NULL OR pg_catalog.jsonb_typeof(v_row) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END IF;
    v_kind := v_row ->> 'kind';
    IF v_kind = 'error' THEN
        v_error := v_row ->> 'code';
        CLOSE v_cursor;
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, v_error);
    END IF;
    IF v_kind IS DISTINCT FROM 'header'
       OR pg_catalog.jsonb_typeof(v_row -> 'candidate_count') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_row -> 'selected_count') IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END IF;
    BEGIN
        v_candidate_count := (v_row ->> 'candidate_count')::bigint;
        v_selected_count := (v_row ->> 'selected_count')::bigint;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END;
    IF v_candidate_count < 0 OR v_selected_count < 0
       OR v_selected_count > v_candidate_count THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END IF;
    v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-input-manifest/v1',
            'job_id', p_job_id,
            'actor_id', p_actor_id,
            'project_id', p_project_id,
            'selection', p_selection,
            'candidate_count', v_candidate_count,
            'selected_count', v_selected_count
        )::text, 'UTF8'
    )), 'hex');

    LOOP
        FETCH v_cursor INTO v_row;
        EXIT WHEN NOT FOUND;
        IF pg_catalog.jsonb_typeof(v_row) IS DISTINCT FROM 'object'
           OR v_row ->> 'kind' IS DISTINCT FROM 'member'
           OR pg_catalog.jsonb_typeof(v_row -> 'ordinal') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(v_row -> 'task_id') IS DISTINCT FROM 'string'
           OR v_row ->> 'project_id' IS DISTINCT FROM p_project_id::text
           OR v_row ->> 'file_type' IS DISTINCT FROM 'image'
           OR pg_catalog.jsonb_typeof(v_row -> 'file_path') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_row ->> 'file_path') > 1024 THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
        END IF;
        BEGIN
            v_task_id := (v_row ->> 'task_id')::uuid;
            v_item_id := CASE WHEN v_row -> 'dataset_item_id' = 'null'::jsonb
                THEN NULL ELSE (v_row ->> 'dataset_item_id')::uuid END;
        EXCEPTION WHEN invalid_text_representation THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
        END;
        IF (v_row ->> 'ordinal')::bigint IS DISTINCT FROM v_expected_ordinal THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
        END IF;
        v_binding := v_reference_bindings -> v_task_id::text;
        v_reference := NULL;
        v_source := NULL;
        v_source_error := NULL;
        v_task_scope_changed := false;
        v_task_source := NULL;
        IF v_item_id IS NULL
           AND NOT yoao_assets_v1._accepted_image_input_manifest_path_valid(v_row ->> 'file_path') THEN
            v_source_error := 'source_missing';
        ELSE
            v_task_reply := yoao_labeling_v1.read_image_task_source(v_task_id, p_actor_id, false);
        END IF;
        IF v_source_error IS NOT NULL THEN
            NULL;
        ELSIF v_task_reply IS NULL
           OR v_task_reply ->> 'schema' IS DISTINCT FROM 'aap-labeling-image-source/v1'
           OR v_task_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_error := COALESCE(v_task_reply #>> '{error,code}', 'invalid_response');
            IF v_error IN ('source_missing', 'source_changed', 'not_image') THEN
                v_source_error := CASE WHEN v_error = 'source_missing'
                    THEN 'source_missing' ELSE 'source_changed' END;
            ELSIF v_error = 'source_not_ready' THEN
                v_source_error := 'source_not_ready';
            ELSE
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:' || v_error;
            END IF;
        ELSE
            v_task_source := v_task_reply -> 'data';
            IF pg_catalog.jsonb_typeof(v_task_source) IS DISTINCT FROM 'object'
               OR v_task_source ->> 'task_id' IS DISTINCT FROM v_task_id::text
               OR v_task_source ->> 'project_id' IS DISTINCT FROM p_project_id::text THEN
                RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
            END IF;
            IF v_task_source ->> 'source_snapshot' IS DISTINCT FROM v_row ->> 'task_source_snapshot'
               OR v_task_source ->> 'source_generation' IS DISTINCT FROM v_row ->> 'task_source_generation'
               OR v_task_source -> 'dataset_item_id' IS DISTINCT FROM v_row -> 'dataset_item_id'
               OR v_task_source ->> 'file_path' IS DISTINCT FROM v_row ->> 'file_path'
               OR v_task_source ->> 'file_type' IS DISTINCT FROM v_row ->> 'file_type' THEN
                v_source_error := 'task_scope_changed';
                v_task_scope_changed := true;
            ELSIF v_task_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb THEN
                v_source_error := 'source_not_ready';
            END IF;
            IF v_binding IS NOT NULL OR pg_catalog.starts_with(v_row ->> 'file_path', '__aap_reference__/') THEN
                IF v_source_error IS NULL THEN
                    IF v_binding IS NULL OR v_item_id IS NULL THEN
                        v_source_error := 'source_missing';
                    ELSIF v_task_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb
                       OR yoao_assets_v1._image_manifest_reference_matches(v_binding, v_row, v_task_source)
                          IS NOT TRUE THEN
                        v_source_error := 'source_changed';
                    ELSE
                        v_reference := v_binding -> 'reference';
                    END IF;
                END IF;
            ELSIF NOT v_task_scope_changed THEN
                v_source_reply := yoao_assets_v1._image_owner_source(
                    p_actor_id, v_task_id, NULL, false
                );
                IF v_source_reply IS NULL
                   OR v_source_reply ->> 'schema' IS DISTINCT FROM 'aap-assets-images/v1' THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
                END IF;
                IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                    v_error := v_source_reply #>> '{error,code}';
                    IF v_error IN ('source_missing', 'source_changed', 'not_image', 'owner_changed') THEN
                        v_source_error := CASE WHEN v_error = 'source_missing'
                            THEN 'source_missing' ELSE 'source_changed' END;
                    ELSIF v_error = 'source_not_ready' THEN
                        v_source_error := 'source_not_ready';
                    ELSIF v_error = 'source_busy' THEN
                        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:source_busy';
                    ELSE
                        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:' || COALESCE(v_error, 'invalid_response');
                    END IF;
                ELSE
                    v_source := v_source_reply -> 'data';
                    IF NOT yoao_assets_v1._image_source_data_valid(v_source)
                       OR v_source ->> 'task_id' IS DISTINCT FROM v_task_id::text
                       OR v_source ->> 'project_id' IS DISTINCT FROM p_project_id::text
                       OR v_source -> 'item_id' IS DISTINCT FROM v_row -> 'dataset_item_id'
                       OR (v_item_id IS NULL
                           AND v_source ->> 'file_path' IS DISTINCT FROM v_row ->> 'file_path')
                       OR v_source ->> 'file_type' IS DISTINCT FROM 'image' THEN
                        v_source_error := 'task_scope_changed';
                        v_source := NULL;
                    ELSIF v_source -> 'source_drifted' IS DISTINCT FROM 'false'::jsonb THEN
                        v_source_error := 'source_changed';
                    ELSIF v_source -> 'upload_ready' IS DISTINCT FROM 'true'::jsonb THEN
                        v_source_error := 'source_not_ready';
                    END IF;
                END IF;
            END IF;
        END IF;
        IF v_source IS NULL AND v_reference IS NULL AND v_source_error IS NULL THEN
            v_source_error := CASE WHEN v_task_source -> 'dataset_item_id' <> 'null'::jsonb
                THEN 'source_missing' ELSE 'source_changed' END;
        END IF;
        v_state := CASE WHEN v_source_error IS NOT NULL THEN 'failed'
                        WHEN v_reference IS NOT NULL THEN 'reference'
                        ELSE 'unprepared' END;
        IF v_state <> 'unprepared' THEN
            v_prepared := v_prepared + 1;
        END IF;
        v_hash_input := pg_catalog.jsonb_build_object(
            'ordinal', v_expected_ordinal,
            'task_id', v_task_id,
            'project_id', p_project_id,
            'batch_id', v_row -> 'batch_id',
            'task_version', v_row -> 'task_version',
            'task_source_generation', v_row -> 'task_source_generation',
            'dataset_item_id', v_row -> 'dataset_item_id',
            'task_source_snapshot', v_row ->> 'task_source_snapshot',
            'file_path', v_row ->> 'file_path',
            'file_type', v_row ->> 'file_type',
            'source', v_source,
            'error', v_source_error
        );
        IF v_reference IS NOT NULL THEN
            v_hash_input := v_hash_input || pg_catalog.jsonb_build_object('reference', v_reference);
        END IF;
        v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            v_digest || ':' || v_hash_input::text, 'UTF8'
        )), 'hex');
        INSERT INTO public.asset_accepted_image_input_manifest_members (
            job_id, ordinal, task_id, project_id, batch_id, task_version,
            task_source_generation, dataset_item_id, task_source_snapshot,
            file_path, file_type, source, state, error_code
        ) VALUES (
            p_job_id, v_expected_ordinal, v_task_id, p_project_id,
            CASE WHEN v_row -> 'batch_id' = 'null'::jsonb THEN NULL
                 ELSE (v_row ->> 'batch_id')::uuid END,
            (v_row ->> 'task_version')::integer,
            (v_row ->> 'task_source_generation')::bigint,
            v_item_id,
            v_row ->> 'task_source_snapshot',
            v_row ->> 'file_path', 'image', v_source,
            v_state, v_source_error
        );
        v_expected_ordinal := v_expected_ordinal + 1;
    END LOOP;
    CLOSE v_cursor;
    IF v_expected_ordinal - 1 <> v_selected_count THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END IF;
    INSERT INTO public.asset_accepted_image_input_manifests (
        job_id, actor_id, project_id, selection, selection_digest,
        candidate_count, selected_count, prepared_count
    ) VALUES (
        p_job_id, p_actor_id, p_project_id, p_selection, v_digest,
        v_candidate_count, v_selected_count, v_prepared
    ) RETURNING * INTO v_existing;
    RETURN yoao_assets_v1._accepted_image_input_manifest_reply(
        yoao_assets_v1._accepted_image_input_manifest_header(v_existing), NULL
    );
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS v_message = MESSAGE_TEXT;
    IF v_cursor IS NOT NULL THEN
        BEGIN
            CLOSE v_cursor;
        EXCEPTION WHEN invalid_cursor_name THEN
            NULL;
        END;
    END IF;
    RETURN yoao_assets_v1._accepted_image_input_manifest_reply(
        NULL, COALESCE(NULLIF(pg_catalog.split_part(v_message, ':', 2), ''), 'invalid_response')
    );
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
WHEN OTHERS THEN
    RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_assets_v1.seal_accepted_image_input_manifest(
    p_job_id uuid, p_actor_id uuid, p_project_id uuid, p_selection_digest text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_manifest public.asset_accepted_image_input_manifests%ROWTYPE;
    v_member public.asset_accepted_image_input_manifest_members%ROWTYPE;
    v_input public.asset_accepted_image_inputs%ROWTYPE;
    v_reply jsonb;
    v_job jsonb;
    v_ready bigint := 0;
    v_failed bigint := 0;
    v_count bigint := 0;
    v_expected bigint := 1;
    v_digest text;
    v_state text;
    v_error text;
    v_byte_size bigint;
    v_sha text;
    v_sealed_at timestamptz;
BEGIN
    IF p_job_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL
       OR p_selection_digest IS NULL OR p_selection_digest !~ '^[0-9a-f]{64}$' THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_request');
    END IF;
    v_reply := yoao_assets_v1._accepted_image_input_manifest_current_scope(
        p_actor_id, p_project_id
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    SELECT manifest.* INTO v_manifest
    FROM public.asset_accepted_image_input_manifests AS manifest
    WHERE manifest.job_id = p_job_id;
    IF NOT FOUND THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_missing');
    END IF;
    IF (v_manifest.actor_id, v_manifest.project_id, v_manifest.selection_digest)
        IS DISTINCT FROM (p_actor_id, p_project_id, p_selection_digest) THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    v_reply := yoao_assets_v1._accepted_image_input_manifest_job_scope(
        p_job_id, p_actor_id, p_project_id, NULL,
        v_manifest.selection, v_manifest.selection_digest, false
    );
    IF v_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN RETURN v_reply; END IF;
    v_job := v_reply -> 'data';
    IF v_job -> 'consistent' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    IF v_manifest.status = 'sealed' THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(
            yoao_assets_v1._accepted_image_input_manifest_header(v_manifest), NULL
        );
    END IF;
    IF v_job -> 'active' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'job_not_active');
    END IF;
    BEGIN
        SELECT manifest.* INTO v_manifest
        FROM public.asset_accepted_image_input_manifests AS manifest
        WHERE manifest.job_id = p_job_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
    END;
    IF NOT FOUND OR v_manifest.status <> 'preparing'
       OR (v_manifest.actor_id, v_manifest.project_id, v_manifest.selection_digest)
            IS DISTINCT FROM (p_actor_id, p_project_id, p_selection_digest) THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_scope_mismatch');
    END IF;
    IF v_manifest.prepared_count <> v_manifest.selected_count THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_not_ready');
    END IF;

    -- First pass is read-only. A not-ready member cannot leave a partially sealed
    -- outcome or partially published counters behind.
    FOR v_member IN
        SELECT member.*
        FROM public.asset_accepted_image_input_manifest_members AS member
        WHERE member.job_id = p_job_id
        ORDER BY member.ordinal
    LOOP
        IF v_member.ordinal IS DISTINCT FROM v_expected THEN
            RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
        END IF;
        v_expected := v_expected + 1;
        v_count := v_count + 1;
        IF v_member.state = 'unprepared' THEN
            RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_not_ready');
        ELSIF v_member.state = 'failed' THEN
            v_failed := v_failed + 1;
        ELSIF v_member.state = 'reference' THEN
            v_ready := v_ready + 1;
        ELSIF v_member.state = 'ready' THEN
            IF v_member.outcome_byte_size IS NULL OR v_member.outcome_sha256 IS NULL THEN
                RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
            END IF;
            v_ready := v_ready + 1;
        ELSIF v_member.state = 'preparing' THEN
            SELECT input.* INTO v_input
            FROM public.asset_accepted_image_inputs AS input
            WHERE input.input_id = v_member.input_id;
            IF NOT FOUND THEN
                v_failed := v_failed + 1;
            ELSIF v_input.status = 'ready' THEN
                IF v_input.byte_size IS NULL OR v_input.sha256 IS NULL THEN
                    RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
                END IF;
                v_ready := v_ready + 1;
            ELSIF v_input.status IN ('failed', 'expired') THEN
                v_failed := v_failed + 1;
            ELSE
                RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_not_ready');
            END IF;
        ELSE
            RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
        END IF;
    END LOOP;
    IF v_count <> v_manifest.selected_count THEN
        RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
    END IF;

    v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-input-manifest/v1',
            'job_id', p_job_id,
            'actor_id', p_actor_id,
            'project_id', p_project_id,
            'selection_digest', p_selection_digest,
            'selected_count', v_manifest.selected_count
        )::text, 'UTF8'
    )), 'hex');
    v_ready := 0;
    v_failed := 0;
    v_expected := 1;
    FOR v_member IN
        SELECT member.*
        FROM public.asset_accepted_image_input_manifest_members AS member
        WHERE member.job_id = p_job_id
        ORDER BY member.ordinal
    LOOP
        v_state := v_member.state;
        v_error := v_member.error_code;
        v_byte_size := NULL;
        v_sha := NULL;
        IF v_state = 'preparing' THEN
            SELECT input.* INTO v_input
            FROM public.asset_accepted_image_inputs AS input
            WHERE input.input_id = v_member.input_id;
            IF NOT FOUND THEN
                v_state := 'failed';
                v_error := 'input_expired';
                UPDATE public.asset_accepted_image_input_manifest_members AS member
                SET state = 'failed', input_id = NULL, error_code = v_error,
                    outcome_byte_size = NULL, outcome_sha256 = NULL,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE member.job_id = p_job_id AND member.ordinal = v_member.ordinal;
            ELSIF v_input.status = 'ready' THEN
                v_state := 'ready';
                v_error := NULL;
                v_byte_size := v_input.byte_size;
                v_sha := v_input.sha256;
                UPDATE public.asset_accepted_image_input_manifest_members AS member
                SET state = 'ready', error_code = NULL,
                    outcome_byte_size = v_byte_size, outcome_sha256 = v_sha,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE member.job_id = p_job_id AND member.ordinal = v_member.ordinal;
            ELSE
                v_state := 'failed';
                v_error := COALESCE(v_input.error_code, 'input_expired');
                UPDATE public.asset_accepted_image_input_manifest_members AS member
                SET state = 'failed', error_code = v_error,
                    outcome_byte_size = NULL, outcome_sha256 = NULL,
                    updated_at = pg_catalog.clock_timestamp()
                WHERE member.job_id = p_job_id AND member.ordinal = v_member.ordinal;
            END IF;
        ELSIF v_state = 'ready' THEN
            v_byte_size := v_member.outcome_byte_size;
            v_sha := v_member.outcome_sha256;
        END IF;
        IF v_state IN ('ready', 'reference') THEN
            v_ready := v_ready + 1;
        ELSIF v_state = 'failed' THEN
            v_failed := v_failed + 1;
        ELSE
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
        END IF;
        v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
            v_digest || ':' || pg_catalog.jsonb_build_object(
                'ordinal', v_member.ordinal,
                'task_id', v_member.task_id,
                'state', v_state,
                'input_id', CASE WHEN v_state = 'failed' AND v_error = 'input_expired'
                    AND v_member.input_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM public.asset_accepted_image_inputs AS input
                        WHERE input.input_id = v_member.input_id
                    ) THEN NULL ELSE v_member.input_id END,
                'byte_size', v_byte_size,
                'sha256', v_sha,
                'error', v_error
            )::text, 'UTF8'
        )), 'hex');
        v_expected := v_expected + 1;
    END LOOP;
    IF v_ready + v_failed <> v_manifest.selected_count THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:invalid_response';
    END IF;
    v_sealed_at := GREATEST(pg_catalog.clock_timestamp(), v_manifest.created_at);
    UPDATE public.asset_accepted_image_input_manifests AS manifest
    SET status = 'sealed', ready_count = v_ready, failed_count = v_failed,
        sealed_digest = v_digest, sealed_at = v_sealed_at
    WHERE manifest.job_id = p_job_id
      AND manifest.status = 'preparing'
      AND manifest.selection_digest = p_selection_digest
    RETURNING * INTO v_manifest;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'manifest_abort:manifest_scope_mismatch';
    END IF;
    RETURN yoao_assets_v1._accepted_image_input_manifest_reply(
        yoao_assets_v1._accepted_image_input_manifest_header(v_manifest), NULL
    );
EXCEPTION WHEN SQLSTATE 'P0001' THEN
    GET STACKED DIAGNOSTICS v_error = MESSAGE_TEXT;
    RETURN yoao_assets_v1._accepted_image_input_manifest_reply(
        NULL, COALESCE(NULLIF(pg_catalog.split_part(v_error, ':', 2), ''), 'invalid_response')
    );
WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'manifest_busy');
WHEN OTHERS THEN
    RETURN yoao_assets_v1._accepted_image_input_manifest_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.is_sealed_image_reference_member(
    p_job_id uuid, p_actor_id uuid, p_project_id uuid, p_task_id uuid,
    p_selection_digest text, p_item_id uuid, p_file_path text,
    p_task_version integer, p_task_source_generation bigint
)
RETURNS boolean LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1
        FROM public.asset_accepted_image_input_manifests AS manifest
        JOIN public.asset_accepted_image_input_manifest_members AS member ON member.job_id = manifest.job_id
        WHERE manifest.job_id = p_job_id AND manifest.actor_id = p_actor_id
          AND manifest.project_id = p_project_id AND manifest.selection_digest = p_selection_digest
          AND manifest.status = 'sealed' AND member.state = 'reference'
          AND member.project_id = p_project_id AND member.task_id = p_task_id
          AND member.dataset_item_id = p_item_id AND member.file_path = p_file_path
          AND member.task_version = p_task_version AND member.task_source_generation = p_task_source_generation
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._image_manifest_reference_matches(jsonb,jsonb,jsonb),
    yoao_assets_v1.is_sealed_image_reference_member(uuid,uuid,uuid,uuid,text,uuid,text,integer,bigint)
FROM PUBLIC;
