ALTER TABLE public.dataset_source_ingest_operations ADD COLUMN admission_authority jsonb;
-- aap-statement-break
-- Owner-side publication fence for the original admitted ingest credential.
CREATE FUNCTION aap_assets_v1._dataset_ingest_publication_authority(
    p_actor_id uuid, p_dataset_id uuid, p_authority jsonb, p_kind text
) RETURNS text
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_credential_id uuid;
    v_kind text;
    v_generation bigint;
    v_expires_at timestamptz;
    v_revision text;
    v_scopes text[];
    v_key_expires_at timestamptz;
    v_fence jsonb;
    v_role text;
    v_dataset public.datasets%ROWTYPE;
BEGIN
    IF pg_catalog.jsonb_typeof(p_authority) IS DISTINCT FROM 'object'
       OR NOT (p_authority ?& ARRAY['credential_type', 'credential_id', 'generation',
            'expires_at', 'api_key_revision', 'api_key_scopes',
            'dataset_lifecycle_revision', 'directory_protected'])
       OR (p_authority - ARRAY['credential_type', 'credential_id', 'generation',
            'expires_at', 'api_key_revision', 'api_key_scopes',
            'dataset_lifecycle_revision', 'directory_protected']) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_authority -> 'directory_protected') IS DISTINCT FROM 'boolean' THEN
        RETURN 'invalid_request';
    END IF;
    BEGIN
        v_kind := p_authority ->> 'credential_type';
        v_credential_id := (p_authority ->> 'credential_id')::uuid;
        v_generation := (p_authority ->> 'generation')::bigint;
        v_expires_at := (p_authority ->> 'expires_at')::timestamptz;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
        OR invalid_datetime_format OR datetime_field_overflow THEN
        RETURN 'invalid_request';
    END;
    IF v_kind NOT IN ('jwt', 'api_key') OR v_credential_id IS NULL
       OR NOT aap_assets_v1._dataset_source_ingest_integer_valid(
            p_authority -> 'dataset_lifecycle_revision', 1, 2147483647)
       OR (v_expires_at IS NOT NULL AND NOT pg_catalog.isfinite(v_expires_at))
       OR (v_kind = 'jwt' AND (v_generation IS NULL OR v_generation < 0
            OR v_expires_at IS NULL OR p_authority ->> 'api_key_revision' IS NOT NULL
            OR p_authority -> 'api_key_scopes' IS DISTINCT FROM 'null'::jsonb))
       OR (v_kind = 'api_key' AND (v_generation IS NOT NULL
            OR pg_catalog.jsonb_typeof(p_authority -> 'api_key_scopes') IS DISTINCT FROM 'array'
            OR COALESCE(p_authority ->> 'api_key_revision', '') !~ '^[0-9a-f]{64}$')) THEN
        RETURN 'invalid_request';
    END IF;
    v_fence := aap_identity_v1.lock_current_principal(
        p_actor_id, v_kind, v_credential_id, v_generation,
        CASE WHEN v_kind = 'jwt' THEN v_expires_at ELSE NULL END,
        CASE WHEN v_kind = 'api_key' THEN ARRAY['datasets:write']::text[] ELSE NULL END
    );
    IF v_fence ->> 'schema' IS DISTINCT FROM 'aap-identity/v1'
       OR pg_catalog.jsonb_typeof(v_fence -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Identity fence';
    END IF;
    IF v_fence -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_fence #>> '{error,code}' = 'credential_busy' THEN
            RETURN 'account_busy';
        END IF;
        RETURN 'access_revoked';
    END IF;
    v_role := v_fence #>> '{data,role}';
    IF p_kind = 'scan' AND v_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN 'catalog_manager_required';
    END IF;
    IF v_kind = 'api_key' THEN
        SELECT facts.revision, facts.scopes, facts.expires_at
        INTO v_revision, v_scopes, v_key_expires_at
        FROM aap_identity_v1.read_accepted_api_key_facts(p_actor_id, v_credential_id) AS facts;
        IF NOT FOUND OR v_revision IS DISTINCT FROM p_authority ->> 'api_key_revision'
           OR pg_catalog.to_jsonb(v_scopes) IS DISTINCT FROM p_authority -> 'api_key_scopes'
           OR v_key_expires_at IS DISTINCT FROM v_expires_at
           OR (v_expires_at IS NOT NULL AND v_expires_at <= pg_catalog.clock_timestamp()) THEN
            RETURN 'api_key_authority_revoked';
        END IF;
    END IF;
    BEGIN
        SELECT dataset.* INTO v_dataset FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN 'dataset_busy';
    END;
    IF NOT FOUND THEN RETURN 'dataset_missing'; END IF;
    IF v_dataset.archived_at IS NOT NULL THEN RETURN 'dataset_archived'; END IF;
    IF v_dataset.lifecycle_revision IS DISTINCT FROM
        (p_authority ->> 'dataset_lifecycle_revision')::integer THEN
        RETURN 'dataset_lifecycle_changed';
    END IF;
    IF ((p_authority ->> 'directory_protected')::boolean OR EXISTS (
               SELECT 1 FROM public.dataset_sources AS source
               WHERE source.dataset_id = p_dataset_id))
       AND (v_role NOT IN ('super_admin', 'project_admin')
            OR (v_role <> 'super_admin' AND v_dataset.created_by <> p_actor_id)) THEN
        RETURN 'access_revoked';
    END IF;
    RETURN NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._dataset_ingest_publication_authority(uuid, uuid, jsonb, text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.begin_dataset_ingest(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_descriptor jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_code text;
    v_role text;
    v_descriptor jsonb;
    v_saved public.dataset_source_ingest_operations%ROWTYPE;
    v_dataset_name text;
    v_is_temporal boolean;
    v_authority jsonb;
    v_input_descriptor jsonb;
BEGIN
    v_authority := p_descriptor -> 'authority';
    v_input_descriptor := p_descriptor - 'authority';
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR pg_catalog.jsonb_typeof(v_authority) IS DISTINCT FROM 'object'
       OR NOT aap_assets_v1._dataset_source_ingest_descriptor_valid(v_input_descriptor) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;

    v_descriptor := v_input_descriptor || pg_catalog.jsonb_build_object(
        'hidden_count', COALESCE(v_input_descriptor -> 'hidden_count', '0'::jsonb),
        'unsafe_count', COALESCE(v_input_descriptor -> 'unsafe_count', '0'::jsonb),
        'top_level_dirs', COALESCE(v_input_descriptor -> 'top_level_dirs', '[]'::jsonb)
    );
    IF v_descriptor ->> 'kind' = 'scan'
       AND v_descriptor ->> 'request_digest' IS DISTINCT FROM
           'f5f47f290df98f65d1879eb9896f8bc6e16b4b0c805a834cca38c44a44faf065' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    SELECT CASE WHEN facts.is_active THEN
        pg_catalog.jsonb_build_object('schema', 'aap-active-actor/v1', 'ok', true,
            'data', pg_catalog.jsonb_build_object('actor_id', facts.actor_id,
                'platform_role', facts.platform_role))
        ELSE NULL END INTO v_actor
    FROM aap_identity_v1.read_actor_facts(p_actor_id) AS facts;
    IF v_actor IS NULL THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'account_not_active');
    END IF;
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1'
       OR pg_catalog.jsonb_typeof(v_actor -> 'ok') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Assets source ingest Identity contract mismatch';
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        v_code := v_actor #>> '{error,code}';
        IF v_code IN ('account_not_active', 'account_busy') THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, v_code);
        END IF;
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Assets source ingest Identity failure mismatch';
    END IF;
    IF pg_catalog.jsonb_typeof(v_actor -> 'data') IS DISTINCT FROM 'object'
       OR ((v_actor -> 'data') - ARRAY['actor_id', 'platform_role']) <> '{}'::jsonb
       OR NOT ((v_actor -> 'data') ?& ARRAY['actor_id', 'platform_role'])
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,actor_id}') IS DISTINCT FROM 'string'
       OR v_actor #>> '{data,actor_id}' IS DISTINCT FROM p_actor_id::text
       OR pg_catalog.jsonb_typeof(v_actor #> '{data,platform_role}') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Assets source ingest Identity facts mismatch';
    END IF;
    v_role := v_actor #>> '{data,platform_role}';
    IF v_role NOT IN ('super_admin', 'project_admin', 'employee', 'viewer') THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_account_role');
    END IF;
    IF v_descriptor ->> 'kind' = 'scan'
       AND v_role NOT IN ('super_admin', 'project_admin') THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'catalog_manager_required');
    END IF;

    IF NOT pg_catalog.pg_try_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'aap-dataset-source-ingest:' || p_operation_id::text, 0
        )
    ) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END IF;

    BEGIN
        SELECT operation.* INTO v_saved
        FROM public.dataset_source_ingest_operations AS operation
        WHERE operation.operation_id = p_operation_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END;
    IF FOUND THEN
        IF (v_saved.actor_id, v_saved.dataset_id, v_saved.kind,
            v_saved.request_digest)
           IS DISTINCT FROM
           (p_actor_id, p_dataset_id, v_descriptor ->> 'kind',
            v_descriptor ->> 'request_digest') THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        IF v_saved.state = 'completed' THEN
            v_code := aap_assets_v1._dataset_ingest_publication_authority(
                p_actor_id, p_dataset_id, v_authority, v_descriptor ->> 'kind');
            IF v_code IS NOT NULL THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, v_code);
            END IF;
            RETURN aap_assets_v1._dataset_source_ingest_reply(
                aap_assets_v1._dataset_source_ingest_context(p_operation_id), NULL
            );
        END IF;
        IF v_saved.creator_transaction_id <> pg_catalog.txid_current() THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        RETURN aap_assets_v1._dataset_source_ingest_reply(
            aap_assets_v1._dataset_source_ingest_context(p_operation_id), NULL
        );
    END IF;

    BEGIN
        SELECT dataset.name, dataset.is_temporal
        INTO v_dataset_name, v_is_temporal
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_missing');
    END IF;
    IF pg_catalog.left(
        pg_catalog.split_part(pg_catalog.btrim(v_dataset_name, '/'), '/', 1),
        pg_catalog.char_length('__aap_trusted_nuscenes__')
    ) = '__aap_trusted_nuscenes__' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'reserved_namespace');
    END IF;
    IF v_descriptor ->> 'kind' = 'scan'
       AND pg_catalog.split_part(pg_catalog.btrim(v_dataset_name, '/'), '/', 1)
           IN ('dataset-sources', 'dataset-uploads') THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'reserved_namespace');
    END IF;

    INSERT INTO public.dataset_source_ingest_operations (
        operation_id, actor_id, dataset_id, kind, request_digest, descriptor,
        state, creator_transaction_id, dataset_name, is_temporal, source_prefix, admission_authority
    ) VALUES (
        p_operation_id, p_actor_id, p_dataset_id,
        v_descriptor ->> 'kind', v_descriptor ->> 'request_digest', v_descriptor,
        'preparing', pg_catalog.txid_current(), v_dataset_name, v_is_temporal,
        v_dataset_name || '/', v_authority
    );
    RETURN aap_assets_v1._dataset_source_ingest_reply(
        aap_assets_v1._dataset_source_ingest_context(p_operation_id), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_busy');
END
$function$;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.stage_dataset_ingest_batch(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid,
    p_batch_id uuid, p_completions jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation public.dataset_source_ingest_operations%ROWTYPE;
    v_entry public.dataset_source_ingest_entries%ROWTYPE;
    v_input jsonb;
    v_seen uuid[] := ARRAY[]::uuid[];
    v_pending integer;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_batch_id IS NULL OR pg_catalog.jsonb_typeof(p_completions) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_completions) > 200 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.dataset_source_ingest_operations AS operation
    WHERE operation.operation_id = p_operation_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_missing');
    END IF;
    IF v_operation.actor_id IS DISTINCT FROM p_actor_id
       OR v_operation.dataset_id IS DISTINCT FROM p_dataset_id
       OR v_operation.kind <> 'zip' OR v_operation.state <> 'preparing'
       OR v_operation.creator_transaction_id <> pg_catalog.txid_current() THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    SELECT pg_catalog.count(*)::integer INTO v_pending
    FROM public.dataset_source_ingest_entries AS entry
    WHERE entry.operation_id = p_operation_id
      AND entry.batch_id = p_batch_id AND entry.planned_status = 'pending';
    IF v_pending <> pg_catalog.jsonb_array_length(p_completions) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    FOR v_input IN SELECT value FROM pg_catalog.jsonb_array_elements(p_completions)
    LOOP
        IF NOT aap_assets_v1._dataset_source_ingest_completion_valid(v_input)
           OR (v_input ->> 'entry_id')::uuid = ANY(v_seen) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
        END IF;
        v_seen := pg_catalog.array_append(v_seen, (v_input ->> 'entry_id')::uuid);
        SELECT entry.* INTO v_entry
        FROM public.dataset_source_ingest_entries AS entry
        WHERE entry.operation_id = p_operation_id AND entry.batch_id = p_batch_id
          AND entry.entry_id = (v_input ->> 'entry_id')::uuid;
        IF NOT FOUND OR v_entry.planned_status <> 'pending'
           OR v_entry.completion IS NOT NULL AND v_entry.completion IS DISTINCT FROM v_input
           OR (v_input ->> 'publication_id')::uuid IS DISTINCT FROM v_entry.publication_id THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        IF v_input ->> 'status' = 'ready' AND (
            (v_input ->> 'file_size')::bigint IS DISTINCT FROM (v_entry.source ->> 'file_size')::bigint
            OR v_input ->> 'content_hash' IS DISTINCT FROM v_entry.source ->> 'content_hash'
            OR (v_entry.source ->> 'width') IS NOT NULL
               AND v_input ->> 'width' IS DISTINCT FROM v_entry.source ->> 'width'
            OR (v_entry.source ->> 'height') IS NOT NULL
               AND v_input ->> 'height' IS DISTINCT FROM v_entry.source ->> 'height'
            OR v_entry.source ->> 'file_type' <> 'image'
               AND (v_input -> 'width' <> 'null'::jsonb
                    OR v_input -> 'height' <> 'null'::jsonb)
            OR NOT EXISTS (
                SELECT 1 FROM public.managed_original_creations AS creation
                WHERE creation.dataset_id = p_dataset_id
                  AND creation.owner_kind = 'dataset' AND creation.origin = 'zip'
                  AND creation.status = 'verified' AND creation.verification IS NOT NULL
                  AND creation.requested_by = p_actor_id
                  AND creation.authority ->> 'kind' = 'dataset_source_ingest'
                  AND creation.authority ->> 'operation_id' = p_operation_id::text
                  AND creation.authority ->> 'batch_id' = p_batch_id::text
                  AND creation.authority ->> 'entry_index' = v_entry.entry_index::text
                  AND creation.authority ->> 'item_id' = v_entry.item_id::text
                  AND creation.authority ->> 'publication_id' = v_entry.publication_id::text
                  AND creation.size_bytes = (v_input ->> 'file_size')::bigint
                  AND creation.md5 = v_input ->> 'content_hash'
                  AND creation.logical_path = v_entry.source ->> 'relpath'
                  AND creation.object_key =
                      COALESCE(creation.configuration -> 'original_ref' ->> 'key_prefix', '')
                      || v_entry.file_path
            )
        ) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
    END LOOP;
    UPDATE public.dataset_source_ingest_entries AS entry
    SET completion = submitted.value
    FROM pg_catalog.jsonb_array_elements(p_completions) AS submitted(value)
    WHERE entry.operation_id = p_operation_id AND entry.batch_id = p_batch_id
      AND entry.entry_id = (submitted.value ->> 'entry_id')::uuid;
    RETURN aap_assets_v1._dataset_source_ingest_reply(
        pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id, 'actor_id', p_actor_id,
            'dataset_id', p_dataset_id, 'batch_id', p_batch_id,
            'progress', aap_assets_v1._dataset_source_ingest_progress(
                p_operation_id, p_actor_id, p_dataset_id)
        ), NULL);
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.stage_dataset_ingest_batch(uuid,uuid,uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.complete_dataset_ingest_batch(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_batch_id uuid,
    p_completions jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation public.dataset_source_ingest_operations%ROWTYPE;
    v_entry public.dataset_source_ingest_entries%ROWTYPE;
    v_input jsonb;
    v_completion_set jsonb := '[]'::jsonb;
    v_expected_set jsonb;
    v_settlements jsonb := '[]'::jsonb;
    v_entry_count integer;
    v_pending_count integer;
    v_input_count integer;
    v_added_count integer := 0;
    v_deduped_count integer := 0;
    v_error_count integer := 0;
    v_linked_tasks integer := 0;
    v_batch_hashes text[] := ARRAY[]::text[];
    v_dataset_file_count integer;
    v_effective_status text;
    v_error_code text;
    v_added_ids uuid[] := ARRAY[]::uuid[];
    v_link_reply jsonb;
    v_project_entry jsonb;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_authority_error text;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_batch_id IS NULL OR p_completions IS NULL
       OR pg_catalog.jsonb_typeof(p_completions) IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_array_length(p_completions) > 200 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT operation.* INTO v_operation
        FROM public.dataset_source_ingest_operations AS operation
        WHERE operation.operation_id = p_operation_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_missing');
    END IF;
    IF (v_operation.actor_id, v_operation.dataset_id)
       IS DISTINCT FROM (p_actor_id, p_dataset_id) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    IF v_operation.state <> 'preparing'
       OR v_operation.creator_transaction_id <> pg_catalog.txid_current() THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    IF v_operation.kind = 'zip' AND (
        v_operation.next_index IS DISTINCT FROM
            (v_operation.descriptor ->> 'planned_entries')::integer
        OR EXISTS (
            SELECT 1 FROM public.dataset_source_ingest_entries AS entry
            WHERE entry.operation_id = p_operation_id
              AND entry.status = 'pending' AND entry.completion IS NULL
        )
    ) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;


    v_authority_error := aap_assets_v1._dataset_ingest_publication_authority(
        p_actor_id, p_dataset_id, v_operation.admission_authority, v_operation.kind);
    IF v_authority_error IS NOT NULL THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, v_authority_error);
    END IF;

    FOR v_input IN SELECT value FROM pg_catalog.jsonb_array_elements(p_completions)
    LOOP
        IF NOT aap_assets_v1._dataset_source_ingest_completion_valid(v_input) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
        END IF;
        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(v_completion_set) AS prior(value)
            WHERE prior.value ->> 'entry_id' = v_input ->> 'entry_id'
        ) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
        END IF;
        SELECT entry.* INTO v_entry
        FROM public.dataset_source_ingest_entries AS entry
        WHERE entry.operation_id = p_operation_id
          AND entry.batch_id = p_batch_id
          AND entry.entry_id = (v_input ->> 'entry_id')::uuid;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        IF v_operation.kind = 'zip'
           AND v_entry.completion IS DISTINCT FROM v_input THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        IF v_entry.planned_status = 'pending' THEN
            IF (v_operation.kind = 'zip' AND
                (v_input ->> 'publication_id')::uuid IS DISTINCT FROM v_entry.publication_id)
               OR (v_operation.kind = 'scan' AND
                pg_catalog.jsonb_typeof(v_input -> 'publication_id') IS DISTINCT FROM 'null') THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
            END IF;
            IF v_input ->> 'status' = 'ready' THEN
                IF (v_input ->> 'file_size')::bigint IS DISTINCT FROM
                        (v_entry.source ->> 'file_size')::bigint
                   OR v_input ->> 'content_hash' IS DISTINCT FROM
                        v_entry.source ->> 'content_hash'
                   OR (v_entry.source ->> 'width') IS NOT NULL
                      AND v_input ->> 'width' IS DISTINCT FROM v_entry.source ->> 'width'
                   OR (v_entry.source ->> 'height') IS NOT NULL
                      AND v_input ->> 'height' IS DISTINCT FROM v_entry.source ->> 'height'
                   OR v_entry.source ->> 'file_type' <> 'image'
                      AND (v_input -> 'width' <> 'null'::jsonb
                           OR v_input -> 'height' <> 'null'::jsonb) THEN
                    RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
                END IF;
            END IF;
        ELSIF v_entry.completion IS NULL
           OR v_entry.completion IS DISTINCT FROM v_input THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        v_completion_set := v_completion_set || pg_catalog.jsonb_build_array(v_input);
    END LOOP;

    SELECT pg_catalog.count(*)::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'pending')::integer,
        COALESCE(
            pg_catalog.jsonb_agg(entry.completion ORDER BY entry.entry_id)
                FILTER (WHERE entry.completion IS NOT NULL),
            '[]'::jsonb
        )
    INTO v_entry_count, v_pending_count, v_expected_set
    FROM public.dataset_source_ingest_entries AS entry
    WHERE entry.operation_id = p_operation_id AND entry.batch_id = p_batch_id;
    v_input_count := pg_catalog.jsonb_array_length(v_completion_set);
    IF v_entry_count = 0 THEN
        IF v_input_count <> 0 THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        RETURN aap_assets_v1._dataset_source_ingest_reply(
            pg_catalog.jsonb_build_object(
                'operation_id', p_operation_id,
                'actor_id', p_actor_id,
                'dataset_id', p_dataset_id,
                'batch_id', p_batch_id,
                'progress', aap_assets_v1._dataset_source_ingest_progress(
                    p_operation_id, p_actor_id, p_dataset_id
                )
            ), NULL
        );
    END IF;
    IF v_pending_count = 0 THEN
        SELECT COALESCE(pg_catalog.jsonb_agg(value ORDER BY value ->> 'entry_id'), '[]'::jsonb)
        INTO v_completion_set
        FROM pg_catalog.jsonb_array_elements(v_completion_set) AS incoming(value);
        SELECT COALESCE(pg_catalog.jsonb_agg(value ORDER BY value ->> 'entry_id'), '[]'::jsonb)
        INTO v_expected_set
        FROM pg_catalog.jsonb_array_elements(v_expected_set) AS saved(value);
        IF v_completion_set IS DISTINCT FROM v_expected_set THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        RETURN aap_assets_v1._dataset_source_ingest_reply(
            pg_catalog.jsonb_build_object(
                'operation_id', p_operation_id,
                'actor_id', p_actor_id,
                'dataset_id', p_dataset_id,
                'batch_id', p_batch_id,
                'progress', aap_assets_v1._dataset_source_ingest_progress(
                    p_operation_id, p_actor_id, p_dataset_id
                )
            ), NULL
        );
    END IF;
    IF v_input_count <> v_pending_count THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;

    FOR v_input IN
        SELECT incoming.value
        FROM public.dataset_source_ingest_entries AS entry
        JOIN pg_catalog.jsonb_array_elements(v_completion_set) AS incoming(value)
          ON (incoming.value ->> 'entry_id')::uuid = entry.entry_id
        WHERE entry.operation_id = p_operation_id
          AND entry.batch_id = p_batch_id
          AND entry.status = 'pending'
        ORDER BY entry.entry_index
    LOOP
        SELECT entry.* INTO v_entry
        FROM public.dataset_source_ingest_entries AS entry
        WHERE entry.operation_id = p_operation_id
          AND entry.batch_id = p_batch_id
          AND entry.entry_id = (v_input ->> 'entry_id')::uuid;
        v_effective_status := v_input ->> 'status';
        v_error_code := v_input ->> 'error_code';
        IF v_effective_status = 'ready' THEN
            IF v_input ->> 'content_hash' IS NOT NULL AND (
                EXISTS (
                    SELECT 1 FROM public.dataset_items AS item
                    WHERE item.dataset_id = p_dataset_id
                      AND item.content_hash = v_input ->> 'content_hash'
                ) OR pg_catalog.array_position(
                    v_batch_hashes, v_input ->> 'content_hash'
                ) IS NOT NULL
            ) THEN
                v_effective_status := 'deduped';
                v_deduped_count := v_deduped_count + 1;
            ELSE
                v_effective_status := 'added';
                v_added_count := v_added_count + 1;
                v_added_ids := pg_catalog.array_append(v_added_ids, v_entry.item_id);
                IF v_input ->> 'content_hash' IS NOT NULL THEN
                    v_batch_hashes := pg_catalog.array_append(
                        v_batch_hashes, v_input ->> 'content_hash'
                    );
                END IF;
            END IF;
            v_error_code := NULL;
        ELSE
            v_error_count := v_error_count + 1;
        END IF;
        v_settlements := v_settlements || pg_catalog.jsonb_build_array(
            pg_catalog.jsonb_build_object(
                'entry_id', v_entry.entry_id,
                'status', v_effective_status,
                'publication_id', v_input -> 'publication_id',
                'file_size', v_input -> 'file_size',
                'content_hash', v_input -> 'content_hash',
                'width', v_input -> 'width',
                'height', v_input -> 'height',
                'error_code', v_error_code,
                'completion', v_input
            )
        );
    END LOOP;

    IF v_added_count::bigint + v_operation.added > 2147483647
       OR v_deduped_count::bigint + v_operation.deduped > 2147483647
       OR v_error_count::bigint + v_operation.error_count > 2147483647 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'resource_budget_exceeded');
    END IF;
    SELECT dataset.file_count INTO v_dataset_file_count
    FROM public.datasets AS dataset
    WHERE dataset.id = p_dataset_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_missing');
    END IF;
    IF COALESCE(v_dataset_file_count, 0)::bigint + v_added_count > 2147483647 THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'resource_budget_exceeded');
    END IF;
    IF v_added_count > 0 AND EXISTS (
        SELECT 1
        FROM pg_catalog.jsonb_array_elements(v_settlements) AS settlement(value)
        JOIN public.dataset_source_ingest_entries AS entry
          ON entry.entry_id = (settlement.value ->> 'entry_id')::uuid
        JOIN public.dataset_items AS item
          ON item.id = entry.item_id OR item.file_path = entry.file_path
        WHERE entry.operation_id = p_operation_id
          AND settlement.value ->> 'status' = 'added'
    ) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END IF;

    IF v_added_count > 0 THEN
        UPDATE public.datasets AS dataset
        SET file_count = COALESCE(dataset.file_count, 0) + v_added_count,
            updated_at = v_now
        WHERE dataset.id = p_dataset_id;
        INSERT INTO public.dataset_items (
            id, dataset_id, file_name, file_path, file_type, file_size,
            content_hash, width, height, metadata
        )
        SELECT entry.item_id,
            p_dataset_id,
            entry.file_name,
            entry.file_path,
            entry.source ->> 'file_type',
            (settlement.value ->> 'file_size')::bigint,
            settlement.value ->> 'content_hash',
            (settlement.value ->> 'width')::integer,
            (settlement.value ->> 'height')::integer,
            CASE WHEN v_operation.kind = 'zip' THEN
                pg_catalog.jsonb_build_object(
                    'source_ingest', pg_catalog.jsonb_build_object(
                        'operation_id', p_operation_id,
                        'relpath', entry.source ->> 'relpath'
                    )
                )
            ELSE '{}'::jsonb END
        FROM pg_catalog.jsonb_array_elements(v_settlements) AS settlement(value)
        JOIN public.dataset_source_ingest_entries AS entry
          ON entry.operation_id = p_operation_id
         AND entry.entry_id = (settlement.value ->> 'entry_id')::uuid
        WHERE settlement.value ->> 'status' = 'added'
        ORDER BY entry.entry_index;
    END IF;

    v_link_reply := aap_labeling_v1.append_dataset_items(
        p_dataset_id, v_added_ids, p_batch_id
    );
    IF pg_catalog.jsonb_typeof(v_link_reply) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Labeling append capability returned a non-object';
    END IF;
    IF (v_link_reply - ARRAY[
            'schema', 'operation_id', 'dataset_id', 'created',
            'project_totals', 'associations'
       ]) <> '{}'::jsonb
       OR NOT (v_link_reply ?& ARRAY[
            'schema', 'operation_id', 'dataset_id', 'created',
            'project_totals', 'associations'
       ])
       OR v_link_reply ->> 'schema' IS DISTINCT FROM 'aap-task-append/v1'
       OR v_link_reply ->> 'operation_id' IS DISTINCT FROM p_batch_id::text
       OR v_link_reply ->> 'dataset_id' IS DISTINCT FROM p_dataset_id::text
       OR NOT aap_assets_v1._dataset_source_ingest_integer_valid(
            v_link_reply -> 'created', 0, 2147483647
       )
       OR pg_catalog.jsonb_typeof(v_link_reply -> 'project_totals') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(v_link_reply -> 'associations') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'Labeling append capability contract mismatch';
    END IF;
    v_linked_tasks := (v_link_reply ->> 'created')::integer;
    FOR v_project_entry IN
        SELECT value FROM pg_catalog.jsonb_array_elements(v_link_reply -> 'project_totals')
    LOOP
        IF pg_catalog.jsonb_typeof(v_project_entry) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Labeling append project projection is not an object';
        END IF;
        IF (v_project_entry - ARRAY['project_id', 'total_tasks']) <> '{}'::jsonb
           OR NOT (v_project_entry ?& ARRAY['project_id', 'total_tasks'])
           OR pg_catalog.jsonb_typeof(v_project_entry -> 'project_id') IS DISTINCT FROM 'string'
           OR v_project_entry ->> 'project_id' !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
           OR NOT aap_assets_v1._dataset_source_ingest_integer_valid(
                v_project_entry -> 'total_tasks', 0, 2147483647
           ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Labeling append project projection mismatch';
        END IF;
    END LOOP;
    FOR v_project_entry IN
        SELECT value FROM pg_catalog.jsonb_array_elements(v_link_reply -> 'associations')
    LOOP
        IF pg_catalog.jsonb_typeof(v_project_entry) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Labeling append association projection is not an object';
        END IF;
        IF (v_project_entry - ARRAY['id', 'project_id']) <> '{}'::jsonb
           OR NOT (v_project_entry ?& ARRAY['id', 'project_id'])
           OR pg_catalog.jsonb_typeof(v_project_entry -> 'id') IS DISTINCT FROM 'string'
           OR v_project_entry ->> 'id' !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(v_project_entry -> 'project_id') IS DISTINCT FROM 'string'
           OR v_project_entry ->> 'project_id' !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'Labeling append association projection mismatch';
        END IF;
    END LOOP;
    IF v_operation.linked_tasks::bigint + v_linked_tasks > 2147483647 THEN
        RAISE EXCEPTION USING ERRCODE = '22003',
            MESSAGE = 'Assets source ingest linked task count exceeds the wire contract';
    END IF;

    IF v_added_count > 0 THEN
        INSERT INTO public.dataset_source_ingest_deliveries (
            operation_id, kind, target_id
        )
        SELECT p_operation_id,
            CASE entry.source ->> 'file_type'
                WHEN 'image' THEN 'thumbnail'
                WHEN 'video' THEN 'video_metadata'
            END,
            entry.item_id
        FROM pg_catalog.jsonb_array_elements(v_settlements) AS settlement(value)
        JOIN public.dataset_source_ingest_entries AS entry
          ON entry.operation_id = p_operation_id
         AND entry.entry_id = (settlement.value ->> 'entry_id')::uuid
        WHERE settlement.value ->> 'status' = 'added'
          AND entry.source ->> 'file_type' IN ('image', 'video');
    END IF;

    UPDATE public.dataset_source_ingest_entries AS entry
    SET status = settlement.value ->> 'status',
        error_code = settlement.value ->> 'error_code',
        completion = settlement.value -> 'completion',
        file_size = CASE WHEN settlement.value ->> 'status' IN ('added', 'deduped')
            THEN (settlement.value ->> 'file_size')::bigint ELSE NULL END,
        content_hash = CASE WHEN settlement.value ->> 'status' IN ('added', 'deduped')
            THEN settlement.value ->> 'content_hash' ELSE NULL END,
        width = CASE WHEN settlement.value ->> 'status' IN ('added', 'deduped')
            THEN (settlement.value ->> 'width')::integer ELSE NULL END,
        height = CASE WHEN settlement.value ->> 'status' IN ('added', 'deduped')
            THEN (settlement.value ->> 'height')::integer ELSE NULL END,
        settled_at = v_now
    FROM pg_catalog.jsonb_array_elements(v_settlements) AS settlement(value)
    WHERE entry.operation_id = p_operation_id
      AND entry.entry_id = (settlement.value ->> 'entry_id')::uuid;

    UPDATE public.dataset_source_ingest_operations AS operation
    SET added = operation.added + v_added_count,
        deduped = operation.deduped + v_deduped_count,
        error_count = operation.error_count + v_error_count,
        linked_tasks = operation.linked_tasks + v_linked_tasks
    WHERE operation.operation_id = p_operation_id;

    RETURN aap_assets_v1._dataset_source_ingest_reply(
        pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'actor_id', p_actor_id,
            'dataset_id', p_dataset_id,
            'batch_id', p_batch_id,
            'progress', aap_assets_v1._dataset_source_ingest_progress(
                p_operation_id, p_actor_id, p_dataset_id
            )
        ), NULL
    );
EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
    RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
END
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.finish_dataset_ingest(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_finish jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation public.dataset_source_ingest_operations%ROWTYPE;
    v_dataset_name text;
    v_is_temporal boolean;
    v_finish_notes jsonb;
    v_scene_plan jsonb;
    v_layout_digest text;
    v_snapshot jsonb;
    v_errors jsonb := '[]'::jsonb;
    v_receipt jsonb;
    v_pre_scene_count integer;
    v_entry_count integer;
    v_pending_count integer;
    v_actual_added integer;
    v_actual_deduped integer;
    v_actual_skipped integer;
    v_actual_errors integer;
    v_skipped_count bigint;
    v_expected_entries integer;
    v_assignment_count integer;
    v_updated_count integer;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_authority_error text;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_finish IS NULL OR pg_catalog.jsonb_typeof(p_finish) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    IF (p_finish - ARRAY[
            'layout_snapshot_digest', 'scene_plan', 'scene_inference_notes'
       ]) <> '{}'::jsonb
       OR NOT (p_finish ?& ARRAY['layout_snapshot_digest', 'scene_plan']) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    IF pg_catalog.jsonb_typeof(p_finish -> 'layout_snapshot_digest') NOT IN ('string', 'null')
       OR (pg_catalog.jsonb_typeof(p_finish -> 'layout_snapshot_digest') = 'string'
           AND p_finish ->> 'layout_snapshot_digest' !~ '^[0-9a-f]{64}$')
       OR pg_catalog.jsonb_typeof(p_finish -> 'scene_plan') NOT IN ('object', 'null') THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    IF p_finish ? 'scene_inference_notes'
       AND pg_catalog.jsonb_typeof(p_finish -> 'scene_inference_notes') IS DISTINCT FROM 'array' THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    v_finish_notes := COALESCE(p_finish -> 'scene_inference_notes', '[]'::jsonb);
    IF pg_catalog.jsonb_array_length(v_finish_notes) > 1024 OR EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(v_finish_notes) AS note(value)
        WHERE pg_catalog.jsonb_typeof(note.value) IS DISTINCT FROM 'string'
    ) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;
    v_scene_plan := CASE
        WHEN pg_catalog.jsonb_typeof(p_finish -> 'scene_plan') = 'object'
            THEN p_finish -> 'scene_plan'
        ELSE NULL
    END;
    v_layout_digest := p_finish ->> 'layout_snapshot_digest';
    IF v_scene_plan IS NOT NULL AND NOT aap_assets_v1._dataset_source_ingest_scene_plan_valid(v_scene_plan) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
    END IF;

    BEGIN
        SELECT operation.* INTO v_operation
        FROM public.dataset_source_ingest_operations AS operation
        WHERE operation.operation_id = p_operation_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'source_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_missing');
    END IF;
    IF (v_operation.actor_id, v_operation.dataset_id)
       IS DISTINCT FROM (p_actor_id, p_dataset_id) THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    v_authority_error := aap_assets_v1._dataset_ingest_publication_authority(
        p_actor_id, p_dataset_id, v_operation.admission_authority, v_operation.kind);
    IF v_authority_error IS NOT NULL THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, v_authority_error);
    END IF;
    IF v_operation.state <> 'preparing'
       OR v_operation.creator_transaction_id <> pg_catalog.txid_current() THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;

    BEGIN
        SELECT dataset.name, dataset.is_temporal
        INTO v_dataset_name, v_is_temporal
        FROM public.datasets AS dataset
        WHERE dataset.id = p_dataset_id
        FOR UPDATE NOWAIT;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_missing');
    END IF;
    IF v_dataset_name IS DISTINCT FROM v_operation.dataset_name
       OR v_is_temporal IS DISTINCT FROM v_operation.is_temporal THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;

    SELECT
        pg_catalog.count(*)::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'pending')::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'added')::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'deduped')::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'skipped')::integer,
        pg_catalog.count(*) FILTER (WHERE entry.status = 'error')::integer
    INTO v_entry_count, v_pending_count, v_actual_added, v_actual_deduped,
        v_actual_skipped, v_actual_errors
    FROM public.dataset_source_ingest_entries AS entry
    WHERE entry.operation_id = p_operation_id;
    IF v_pending_count <> 0 OR v_entry_count <> v_operation.next_index
       OR v_operation.added <> v_actual_added
       OR v_operation.deduped <> v_actual_deduped
       OR v_operation.skipped <> v_actual_skipped
       OR v_operation.error_count <> v_actual_errors THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;
    IF v_operation.kind = 'zip' THEN
        v_expected_entries := (v_operation.descriptor ->> 'planned_entries')::integer;
        IF v_operation.next_index <> v_expected_entries THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        v_skipped_count := v_operation.skipped::bigint
            + (v_operation.descriptor ->> 'hidden_count')::bigint
            + (v_operation.descriptor ->> 'unsafe_count')::bigint;
        IF v_skipped_count > 2147483647 THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'resource_budget_exceeded');
        END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'name', pg_catalog.left(entry.source ->> 'source_name', 512),
                    'error', aap_assets_v1._dataset_source_ingest_error_message(entry.error_code)
                ) ORDER BY entry.entry_index
            ), '[]'::jsonb)
        INTO v_errors
        FROM public.dataset_source_ingest_entries AS entry
        WHERE entry.operation_id = p_operation_id AND entry.status = 'error';
        IF pg_catalog.jsonb_array_length(v_errors) <> v_operation.error_count
           OR pg_catalog.jsonb_array_length(v_errors) > 5000
           OR EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(v_errors) AS error(value)
                WHERE pg_catalog.jsonb_typeof(error.value -> 'name') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(error.value -> 'error') IS DISTINCT FROM 'string'
                   OR error.value ->> 'error' = ''
           ) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_response');
        END IF;
    ELSE
        IF v_scene_plan IS NOT NULL OR v_layout_digest IS NOT NULL
           OR pg_catalog.jsonb_array_length(v_finish_notes) <> 0 THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
        END IF;
        v_skipped_count := v_operation.skipped;
        v_errors := '[]'::jsonb;
    END IF;

    IF v_operation.kind = 'zip' AND v_operation.added > 0 THEN
        IF v_operation.layout_snapshot_digest IS NULL
           OR v_layout_digest IS DISTINCT FROM v_operation.layout_snapshot_digest THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
        v_snapshot := aap_assets_v1._dataset_source_ingest_dataset_snapshot(p_dataset_id);
        IF (v_operation.layout_snapshot_digest,
            v_operation.layout_scene_count,
            v_operation.layout_assigned_count,
            v_operation.layout_total_items)
           IS DISTINCT FROM
           (v_snapshot ->> 'snapshot_digest',
            (v_snapshot ->> 'scene_count')::integer,
            (v_snapshot ->> 'assigned_count')::integer,
            (v_snapshot ->> 'total_items')::integer) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
        END IF;
    ELSIF v_layout_digest IS NOT NULL OR v_scene_plan IS NOT NULL THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
    END IF;

    IF v_operation.kind = 'zip' THEN
        IF v_scene_plan IS NOT NULL THEN
            IF v_scene_plan ->> 'mode' <> 'auto'
               OR v_scene_plan -> 'dry_run' IS DISTINCT FROM 'false'::jsonb THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'invalid_request');
            END IF;
            IF v_operation.added = 0 THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
            END IF;
            v_assignment_count := pg_catalog.jsonb_array_length(v_scene_plan -> 'assignments');
            IF (v_scene_plan ->> 'assigned_items')::integer >
                    (v_operation.layout_total_items)::integer THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
            END IF;
            IF v_operation.layout_scene_count > 0
               AND (pg_catalog.jsonb_array_length(v_scene_plan -> 'scenes') > 0
                    OR v_assignment_count > 0) THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'scene_conflict');
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(v_scene_plan -> 'scenes') AS planned(value)
                JOIN public.scenes AS scene
                  ON scene.id = (planned.value ->> 'id')::uuid
                     OR (scene.dataset_id = p_dataset_id
                         AND scene.name = planned.value ->> 'name')
            ) THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'scene_conflict');
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_catalog.jsonb_array_elements(v_scene_plan -> 'assignments') AS assignment(value)
                LEFT JOIN public.dataset_items AS item
                  ON item.id = (assignment.value ->> 'item_id')::uuid
                WHERE item.id IS NULL OR item.dataset_id <> p_dataset_id
                   OR item.scene_id IS NOT NULL
            ) THEN
                RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'scene_conflict');
            END IF;
        END IF;

        SELECT pg_catalog.count(*)::integer INTO v_pre_scene_count
        FROM public.scenes AS scene WHERE scene.dataset_id = p_dataset_id;
        IF v_is_temporal AND v_pre_scene_count = 0
           AND (v_scene_plan IS NULL
                OR pg_catalog.jsonb_array_length(v_scene_plan -> 'scenes') = 0) THEN
            RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'temporal_scenes_missing');
        END IF;
    END IF;

    v_receipt := pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id,
        'actor_id', p_actor_id,
        'dataset_id', p_dataset_id,
        'kind', v_operation.kind,
        'request_digest', v_operation.request_digest,
        'added', v_operation.added,
        'deduped', v_operation.deduped,
        'skipped', v_skipped_count,
        'errors', v_errors,
        'error_count', v_operation.error_count,
        'total_in_zip', CASE WHEN v_operation.kind = 'zip'
            THEN (v_operation.descriptor -> 'total_in_zip') ELSE 'null'::jsonb END,
        'linked_tasks', v_operation.linked_tasks,
        'scene_inference_notes', CASE WHEN v_operation.kind = 'zip'
            THEN v_finish_notes ELSE '[]'::jsonb END
    );

    IF v_scene_plan IS NOT NULL THEN
        INSERT INTO public.scenes (
            id, display_id, dataset_id, name, source_format, source_metadata, created_by
        )
        SELECT (planned.value ->> 'id')::uuid,
            'SCN-' || pg_catalog.nextval('public.display_seq_scenes'::pg_catalog.regclass)::text,
            p_dataset_id,
            planned.value ->> 'name',
            planned.value ->> 'source_format',
            planned.value -> 'source_metadata',
            p_actor_id
        FROM pg_catalog.jsonb_array_elements(v_scene_plan -> 'scenes') AS planned(value)
        ORDER BY planned.value ->> 'id';

        UPDATE public.dataset_items AS item
        SET scene_id = (assignment.value ->> 'scene_id')::uuid,
            frame_index = (assignment.value ->> 'frame_index')::integer,
            updated_at = v_now
        FROM pg_catalog.jsonb_array_elements(v_scene_plan -> 'assignments') AS assignment(value)
        WHERE item.id = (assignment.value ->> 'item_id')::uuid
          AND item.dataset_id = p_dataset_id
          AND item.scene_id IS NULL;
        GET DIAGNOSTICS v_updated_count = ROW_COUNT;
        IF v_updated_count <> (v_scene_plan ->> 'assigned_items')::integer THEN
            RAISE EXCEPTION USING ERRCODE = '40001',
                MESSAGE = 'Assets source ingest scene snapshot changed';
        END IF;
    END IF;

    UPDATE public.dataset_source_ingest_operations AS operation
    SET state = 'completed',
        result = v_receipt,
        completed_at = v_now
    WHERE operation.operation_id = p_operation_id;
    RETURN aap_assets_v1._dataset_source_ingest_reply(v_receipt, NULL);
EXCEPTION
    WHEN unique_violation THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'scene_conflict');
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'dataset_busy');
END
$function$;
