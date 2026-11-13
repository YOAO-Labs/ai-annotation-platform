-- Full-read scan identity and immutable proof-gated original retry.
ALTER TABLE public.managed_original_creations ADD COLUMN failure_evidence jsonb;
-- aap-statement-break
ALTER TABLE public.managed_original_creations
    ADD CONSTRAINT ck_managed_creation_failure_evidence CHECK (
        failure_evidence IS NULL OR COALESCE((
            status IN ('failed','conflict','unknown')
            AND pg_catalog.jsonb_typeof(failure_evidence) = 'object'
            AND pg_catalog.jsonb_typeof(failure_evidence -> 'physical_settled') = 'boolean'
            AND failure_evidence ->> 'physical_outcome' IN ('not_written','written','unknown')
            AND ((failure_evidence ->> 'physical_settled')::boolean
                 OR failure_evidence ->> 'physical_outcome' = 'unknown')
            AND (failure_evidence ->> 'physical_outcome' <> 'not_written'
                 OR (receipt IS NULL AND COALESCE(
                     failure_evidence -> 'receipt', 'null'::jsonb) = 'null'::jsonb))
        ), false)
    );
-- aap-statement-break
DROP INDEX public.uq_managed_creation_reserved_key_active;
-- aap-statement-break
CREATE UNIQUE INDEX uq_managed_creation_reserved_key_active
    ON public.managed_original_creations (storage_identity, bucket, object_key)
    WHERE status NOT IN ('failed','conflict') OR receipt IS NOT NULL
       OR NOT COALESCE(failure_evidence @>
           '{"physical_settled":true,"physical_outcome":"not_written"}'::jsonb, false);
-- aap-statement-break
DROP INDEX public.uq_managed_creation_staging_key_active;
-- aap-statement-break
CREATE UNIQUE INDEX uq_managed_creation_staging_key_active
    ON public.managed_original_creations (staging_key)
    WHERE staging_key IS NOT NULL AND (
        status NOT IN ('failed','conflict') OR receipt IS NOT NULL
        OR NOT COALESCE(failure_evidence @>
            '{"physical_settled":true,"physical_outcome":"not_written"}'::jsonb, false)
    );
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.begin_operator_auxiliary(
    p_request jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_kind text;
    v_contract jsonb;
    v_max_bytes bigint;
    v_content_type text;
    v_requested_by uuid;
    v_authority jsonb;
    v_existing public.managed_original_creations%ROWTYPE;
    v_binding public.storage_bindings%ROWTYPE;
    v_location public.storage_locations%ROWTYPE;
    v_connection public.storage_connections%ROWTYPE;
    v_endpoint text;
    v_prefix text;
    v_bucket text;
    v_identity text;
    v_ref jsonb;
    v_config jsonb;
    v_fingerprint text;
    v_revision integer;
    v_creation_id uuid;
    v_object_key text;
    v_object_prefix text;
    v_owner_kind text;
    v_item_id uuid;
    v_publication_id uuid;
    v_logical_path text;
    v_file_type text;
    v_origin text;
    v_mode text;
    v_business_path text;
    v_request_authority jsonb;
    v_destination jsonb;
    v_creation public.managed_original_creations%ROWTYPE;
    v_staging jsonb;
    v_staging_key text;
    v_staging_bucket text;
    v_staging_request_bucket text;
    v_staging_endpoint text;
    v_staging_location_id uuid;
    v_staging_config_revision integer;
    v_staging_etag text;
    v_staging_version text;
    v_staging_source jsonb;
    v_import_binding public.storage_bindings%ROWTYPE;
    v_import_location public.storage_locations%ROWTYPE;
    v_import_connection public.storage_connections%ROWTYPE;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR NOT (p_request ?& ARRAY[
           'dataset_id','kind','max_bytes','content_type'
       ])
       OR (p_request - ARRAY[
           'dataset_id','kind','source_contract','max_bytes','content_type',
           'requested_by','item_id','publication_id','logical_path','file_type',
           'authority','origin','mode','file_path','staging'
       ]) <> '{}'::jsonb THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        v_dataset_id := (p_request ->> 'dataset_id')::uuid;
        v_max_bytes := (p_request ->> 'max_bytes')::bigint;
        v_item_id := NULLIF(p_request ->> 'item_id', '')::uuid;
        v_publication_id := NULLIF(p_request ->> 'publication_id', '')::uuid;
    EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END;
    v_kind := p_request ->> 'kind';
    v_contract := p_request -> 'source_contract';
    v_content_type := p_request ->> 'content_type';
    v_logical_path := p_request ->> 'logical_path';
    v_file_type := p_request ->> 'file_type';
    v_origin := COALESCE(p_request ->> 'origin', 'connector');
    v_mode := COALESCE(p_request ->> 'mode', 'write');
    v_business_path := p_request ->> 'file_path';
    v_request_authority := p_request -> 'authority';
    v_requested_by := CASE
        WHEN p_request ? 'requested_by'
         AND (p_request -> 'requested_by') <> 'null'::jsonb
        THEN (p_request ->> 'requested_by')::uuid
        ELSE NULL
    END;
    IF v_kind NOT IN ('raw_lidar','map','item_media','role_media')
       OR (v_kind NOT IN ('item_media','role_media')
           AND pg_catalog.jsonb_typeof(v_contract) IS DISTINCT FROM 'object')
       OR v_max_bytes IS NULL OR v_max_bytes < 0
       OR v_content_type IS NULL OR v_content_type = '' THEN
        -- dataset_id stays an opaque scope: the physical ledger is
        -- deliberately FK-free, so a caller may allocate before its Dataset is
        -- published. Publication and item/scene binding validate the business
        -- scope in the caller's own unit of work.
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    IF v_kind = 'role_media' THEN
        IF v_item_id IS NULL OR v_publication_id IS NULL
           OR v_business_path IS NULL OR v_business_path = ''
           OR v_business_path ~ '(^|/)\.\.?(/|$)'
           OR v_origin NOT IN ('direct','zip','connector','directory_copy')
           OR v_mode NOT IN ('write','adopt')
           OR pg_catalog.jsonb_typeof(v_request_authority) IS DISTINCT FROM 'object'
           OR v_request_authority ->> 'kind' NOT IN (
               'dataset_import','dataset_upload','dataset_source_ingest','operator_item'
           )
           OR (v_request_authority ->> 'item_id') IS DISTINCT FROM v_item_id::text THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
        END IF;
        v_owner_kind := 'dataset';
        v_authority := v_request_authority;
        v_contract := NULL;
    ELSIF v_kind = 'item_media' THEN
        IF v_item_id IS NULL OR v_publication_id IS NULL
           OR v_logical_path IS NULL OR v_logical_path = ''
           OR v_logical_path ~ '(^|/)\.\.?(/|$)'
           OR v_origin NOT IN ('direct','zip','connector','directory_copy')
           OR v_file_type NOT IN ('point_cloud','image','other') THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
        END IF;
        v_owner_kind := 'dataset';
        v_authority := pg_catalog.jsonb_build_object(
            'kind', 'operator_item', 'operation_id', NULL,
            'item_id', v_item_id::text, 'publication_id', v_publication_id::text
        );
    ELSE
        v_owner_kind := 'dataset_auxiliary';
        v_authority := pg_catalog.jsonb_build_object(
            'auxiliary', pg_catalog.jsonb_build_object(
                'kind', v_kind, 'source_contract', v_contract
            )
        );
    END IF;
    -- A direct upload writes into the caller's already-reserved staging
    -- locator; the owner must persist that exact locator, never invent one.
    IF v_origin = 'direct' THEN
        v_staging := p_request -> 'staging';
        IF v_kind NOT IN ('item_media','role_media')
           OR pg_catalog.jsonb_typeof(v_staging) IS DISTINCT FROM 'object'
           OR NOT (v_staging ? 'key')
           OR (v_staging - ARRAY['key','bucket','etag','version_id'])
              <> '{}'::jsonb
           OR pg_catalog.jsonb_typeof(v_staging -> 'key') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_staging ->> 'key') NOT BETWEEN 1 AND 1000
           OR pg_catalog.octet_length(v_staging ->> 'key') > 1024
           OR (v_staging ->> 'key') ~ '[[:cntrl:]]'
           OR pg_catalog.strpos(v_staging ->> 'key', pg_catalog.chr(92)) > 0
           OR (v_staging ->> 'key') ~ '(^|/)\.{1,2}(/|$)|//'
           OR pg_catalog.left(v_staging ->> 'key', 1) = '/'
           OR pg_catalog.right(v_staging ->> 'key', 1) = '/' THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
        END IF;
        IF v_staging ? 'bucket' AND (
               pg_catalog.jsonb_typeof(v_staging -> 'bucket') IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(v_staging ->> 'bucket') NOT BETWEEN 1 AND 255
               OR (v_staging ->> 'bucket') ~ '[[:space:][:cntrl:]/?#@%:]')
           OR v_staging ? 'etag' AND (
               pg_catalog.jsonb_typeof(v_staging -> 'etag') IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(v_staging ->> 'etag') NOT BETWEEN 1 AND 1024)
           OR v_staging ? 'version_id' AND NOT (
               (v_staging -> 'version_id') = 'null'::jsonb
               OR (pg_catalog.jsonb_typeof(v_staging -> 'version_id') = 'string'
                   AND pg_catalog.char_length(v_staging ->> 'version_id')
                       BETWEEN 1 AND 1024
                   AND (v_staging ->> 'version_id') <> 'null'))
           OR v_staging ? 'etag' AND (v_staging ->> 'etag') = 'null' THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
        END IF;
        v_staging_key := v_staging ->> 'key';
        v_staging_request_bucket := NULLIF(v_staging ->> 'bucket', 'null');
        v_staging_etag := NULLIF(v_staging ->> 'etag', 'null');
        v_staging_version := NULLIF(v_staging ->> 'version_id', 'null');
    ELSIF p_request ? 'staging' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    SELECT creation.* INTO v_existing
    FROM public.managed_original_creations AS creation
    WHERE creation.owner_kind = v_owner_kind
      AND creation.dataset_id = v_dataset_id
      AND (
          (v_kind = 'item_media'
           AND creation.authority ->> 'item_id' = v_item_id::text)
          OR (v_kind = 'role_media' AND creation.authority = v_authority)
          OR (v_kind NOT IN ('item_media','role_media')
              AND creation.authority -> 'auxiliary' = v_authority -> 'auxiliary')
      )
    ORDER BY creation.allocated_at DESC, creation.id DESC
    LIMIT 1
    FOR UPDATE;
    IF FOUND THEN
        IF v_existing.origin IS DISTINCT FROM v_origin
           OR v_existing.requested_by IS DISTINCT FROM v_requested_by
           OR (v_kind IN ('item_media','role_media')
               AND v_existing.logical_path IS DISTINCT FROM
                   COALESCE(NULLIF(v_logical_path, ''), v_business_path))
           OR (v_kind = 'role_media' AND v_business_path IS DISTINCT FROM
               pg_catalog.substr(v_existing.object_key,
                   pg_catalog.char_length(COALESCE(
                       v_existing.configuration -> 'original_ref' ->> 'key_prefix', '')) + 1))
           OR (v_kind IN ('item_media','role_media')
               AND v_authority ? 'publication_id'
               AND v_authority ->> 'publication_id' IS DISTINCT FROM v_publication_id::text) THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_configuration_changed');
        END IF;
        IF v_existing.status = 'verified' THEN
            RETURN aap_assets_v1._managed_original_reply(
                aap_assets_v1._managed_original_facts(v_existing)
                || pg_catalog.jsonb_build_object(
                    'status', v_existing.status, 'sha256', v_existing.sha256,
                    'size_bytes', v_existing.size_bytes, 'etag', v_existing.etag,
                    'version_id', v_existing.version_id,
                    'file_path', pg_catalog.substr(
                        v_existing.object_key,
                        pg_catalog.char_length(
                            COALESCE(v_existing.configuration -> 'original_ref' ->> 'key_prefix', '')
                        ) + 1
                    )
                ), NULL
            );
        ELSIF v_existing.status = 'unknown' THEN
            -- An uncertain physical write must be reconciled, never repeated.
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_terminal');
        ELSIF v_existing.status IN ('failed','conflict') THEN
            -- A failed label alone says nothing about physical publication.
            -- Only this immutable, drained no-write receipt releases the key.
            IF v_existing.receipt IS NOT NULL
               OR NOT COALESCE(v_existing.failure_evidence @>
                   '{"physical_settled":true,"physical_outcome":"not_written"}'::jsonb, false)
               OR EXISTS (SELECT 1 FROM public.storage_operations AS operation
                   WHERE operation.id = v_existing.operation_id) THEN
                RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_terminal');
            END IF;
            IF v_existing.max_bytes IS DISTINCT FROM v_max_bytes
               OR v_existing.content_type IS DISTINCT FROM v_content_type
               OR (v_origin = 'direct'
                   AND v_existing.staging_key IS DISTINCT FROM v_staging_key) THEN
                RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_configuration_changed');
            END IF;
        ELSIF v_existing.max_bytes IS DISTINCT FROM v_max_bytes
           OR v_existing.content_type IS DISTINCT FROM v_content_type
           OR (v_origin = 'direct'
               AND v_existing.staging_key IS DISTINCT FROM v_staging_key) THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_configuration_changed');
        ELSE
            RETURN aap_assets_v1._managed_original_reply(
                aap_assets_v1._managed_original_facts(v_existing)
                || pg_catalog.jsonb_build_object(
                    'status', v_existing.status, 'sha256', v_existing.sha256,
                    'size_bytes', v_existing.size_bytes, 'etag', v_existing.etag,
                    'version_id', v_existing.version_id,
                    'file_path', pg_catalog.substr(
                        v_existing.object_key,
                        pg_catalog.char_length(
                            COALESCE(v_existing.configuration -> 'original_ref' ->> 'key_prefix', '')
                        ) + 1
                    )
                ), NULL
            );
        END IF;
    END IF;

    SELECT binding.* INTO v_binding FROM public.storage_bindings AS binding
    WHERE binding.purpose = 'datasets';
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END IF;
    SELECT location.* INTO v_location FROM public.storage_locations AS location
    WHERE location.id = v_binding.location_id FOR SHARE;
    IF NOT FOUND OR NOT v_location.enabled OR v_location.kind <> 's3'
       OR v_location.config_revision < 1 THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END IF;
    v_bucket := v_binding.bucket;
    IF v_location.id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid THEN
        IF v_location.connection_id IS NOT NULL
           OR v_location.config_revision <> 1
           OR v_binding.generation <> 1
           OR pg_catalog.jsonb_typeof(v_location.config -> 'use_ssl') <> 'boolean'
           OR pg_catalog.jsonb_typeof(v_location.config -> 'endpoint') <> 'string' THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        v_endpoint := aap_assets_v1._managed_original_endpoint(
            CASE WHEN (v_location.config ->> 'use_ssl')::boolean
                THEN 'https://' ELSE 'http://' END
            || pg_catalog.btrim(v_location.config ->> 'endpoint')
        );
        v_prefix := '';
    ELSE
        SELECT connection.* INTO v_connection
        FROM public.storage_connections AS connection
        WHERE connection.id = v_location.connection_id FOR SHARE;
        IF NOT FOUND OR v_connection.kind <> 's3' OR v_connection.scope <> 'global'
           OR v_connection.project_id IS NOT NULL OR v_connection.secret_enc IS NULL
           OR v_connection.revision < 1
           OR pg_catalog.jsonb_typeof(v_connection.config -> 'bucket') <> 'string' THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        v_bucket := v_connection.config ->> 'bucket';
        IF v_bucket IS DISTINCT FROM v_binding.bucket THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        v_endpoint := aap_assets_v1._managed_original_endpoint(
            v_connection.config ->> 'endpoint'
        );
        v_prefix := aap_assets_v1._managed_original_prefix(
            v_connection.config ->> 'base_prefix'
        );
        IF v_prefix IS NULL THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
    END IF;
    -- Only the legacy binding can pin an existing source key in place. A
    -- connection-backed destination bounds a new managed namespace, so the
    -- caller must copy the scanned object with a conditional managed write.
    IF v_mode = 'adopt'
       AND v_location.id <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid THEN
        v_mode := 'write';
    END IF;
    IF v_endpoint IS NULL
       OR pg_catalog.char_length(v_bucket) NOT BETWEEN 1 AND 255
       OR v_bucket ~ '[[:space:][:cntrl:]/?#@%:]'
       OR pg_catalog.strpos(v_bucket, pg_catalog.chr(92)) > 0 THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END IF;
    IF v_origin = 'direct' THEN
        -- Independently prove the staging target from the import binding so the
        -- persisted locator is never accepted on the caller's word alone.
        SELECT import_binding.* INTO v_import_binding
        FROM public.storage_bindings AS import_binding
        WHERE import_binding.purpose = 'import';
        IF NOT FOUND THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        IF v_staging_request_bucket IS NOT NULL
           AND v_staging_request_bucket IS DISTINCT FROM v_import_binding.bucket THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        SELECT import_location.* INTO v_import_location
        FROM public.storage_locations AS import_location
        WHERE import_location.id = v_import_binding.location_id FOR SHARE;
        IF NOT FOUND OR NOT v_import_location.enabled
           OR v_import_location.kind <> 's3'
           OR v_import_location.config_revision < 1 THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        IF v_import_location.id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid THEN
            IF v_import_location.connection_id IS NOT NULL
               OR v_import_location.config_revision <> 1
               OR v_import_binding.generation <> 1
               OR pg_catalog.jsonb_typeof(v_import_location.config -> 'use_ssl')
                  <> 'boolean'
               OR pg_catalog.jsonb_typeof(v_import_location.config -> 'endpoint')
                  <> 'string' THEN
                RETURN aap_assets_v1._managed_original_reply(
                    NULL, 'creation_target_unavailable'
                );
            END IF;
            v_staging_endpoint := aap_assets_v1._managed_original_endpoint(
                CASE WHEN (v_import_location.config ->> 'use_ssl')::boolean
                    THEN 'https://' ELSE 'http://' END
                || pg_catalog.btrim(v_import_location.config ->> 'endpoint')
            );
            v_staging_bucket := v_import_binding.bucket;
        ELSE
            SELECT import_connection.* INTO v_import_connection
            FROM public.storage_connections AS import_connection
            WHERE import_connection.id = v_import_location.connection_id FOR SHARE;
            IF NOT FOUND OR v_import_connection.kind <> 's3'
               OR v_import_connection.scope <> 'global'
               OR v_import_connection.project_id IS NOT NULL
               OR v_import_connection.secret_enc IS NULL
               OR v_import_connection.revision < 1
               OR v_import_binding.generation < 2
               OR pg_catalog.jsonb_typeof(v_import_connection.config -> 'bucket')
                  <> 'string' THEN
                RETURN aap_assets_v1._managed_original_reply(
                    NULL, 'creation_target_unavailable'
                );
            END IF;
            v_staging_bucket := v_import_connection.config ->> 'bucket';
            IF v_staging_bucket IS DISTINCT FROM v_import_binding.bucket THEN
                RETURN aap_assets_v1._managed_original_reply(
                    NULL, 'creation_target_unavailable'
                );
            END IF;
            v_staging_endpoint := aap_assets_v1._managed_original_endpoint(
                v_import_connection.config ->> 'endpoint'
            );
        END IF;
        IF v_staging_endpoint IS NULL
           OR pg_catalog.char_length(v_staging_bucket) NOT BETWEEN 1 AND 255
           OR v_staging_bucket ~ '[[:space:][:cntrl:]/?#@%:]'
           OR pg_catalog.strpos(v_staging_bucket, pg_catalog.chr(92)) > 0 THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        -- The caller-reserved staging head observation is preserved verbatim;
        -- absent facts stay absent rather than being synthesized.
        v_staging_location_id := v_import_location.id;
        v_staging_config_revision := v_import_location.config_revision;
        v_staging_source := pg_catalog.jsonb_strip_nulls(
            pg_catalog.jsonb_build_object(
                'staging_bucket', v_staging_bucket,
                'staging_key', v_staging_key,
                'etag', v_staging_etag,
                'version_id', v_staging_version
            )
        );
    END IF;
    v_creation_id := pg_catalog.gen_random_uuid();
    IF v_kind = 'role_media' THEN
        v_object_key := v_prefix || v_business_path;
    ELSIF v_kind = 'item_media' THEN
        v_object_key := v_prefix || '__aap_managed_datasets__/'
            || v_dataset_id::text || '/_objects/' || v_creation_id::text
            || '/' || v_logical_path;
    ELSE
        v_object_key := v_prefix || '__aap_auxiliary_originals__/'
            || v_dataset_id::text || '/' || v_creation_id::text || '/payload';
    END IF;
    IF v_endpoint IS NULL
       OR pg_catalog.char_length(v_object_key) > 1000
       OR pg_catalog.octet_length(v_object_key) > 1024 THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'resource_budget_exceeded');
    END IF;
    v_identity := aap_assets_v1._managed_original_identity(v_endpoint, v_bucket);
    v_ref := pg_catalog.jsonb_build_object(
        'schema', 1, 'purpose', 'datasets', 'location_id', v_location.id::text,
        'config_revision', v_location.config_revision,
        'binding_generation', v_binding.generation,
        'endpoint_url', v_endpoint, 'bucket', v_bucket, 'key_prefix', v_prefix,
        'object_key', v_object_key, 'storage_identity', v_identity
    );
    v_config := pg_catalog.jsonb_build_object(
        'schema', 2, 'endpoint', v_endpoint,
        'buckets', pg_catalog.jsonb_build_object('datasets', v_bucket),
        'bindings', pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
            'purpose','datasets','location_id',v_location.id,
            'bucket',v_bucket,'generation',v_binding.generation
        )),
        'location', pg_catalog.to_jsonb(v_location),
        'connection_revision', CASE WHEN v_location.connection_id IS NULL
            THEN NULL ELSE v_connection.revision END,
        'mode', v_mode, 'original_ref', v_ref
    ) || CASE WHEN v_origin = 'direct' THEN
        pg_catalog.jsonb_build_object('staging_binding', pg_catalog.jsonb_build_object(
            'location_id', v_staging_location_id,
            'config_revision', v_staging_config_revision,
            'binding_generation', v_import_binding.generation,
            'bucket', v_staging_bucket,
            'endpoint_url', v_staging_endpoint,
            'storage_identity',
            aap_assets_v1._managed_original_identity(v_staging_endpoint, v_staging_bucket)
        ))
    ELSE '{}'::jsonb END;
    IF v_kind = 'role_media' AND v_existing.id IS NOT NULL
       AND v_existing.status IN ('failed','conflict')
       AND (v_existing.location_id, v_existing.storage_identity,
            v_existing.bucket, v_existing.object_key, v_existing.configuration)
           IS DISTINCT FROM
           (v_location.id, v_identity, v_bucket, v_object_key, v_config) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_configuration_changed');
    END IF;
    v_fingerprint := pg_catalog.encode(
        public.digest(pg_catalog.convert_to(v_config::text,'UTF8'),'sha256'),'hex'
    );
    BEGIN
        v_revision := aap_assets_v1._managed_original_gate_revision(v_location.id);
    EXCEPTION WHEN SQLSTATE '55006' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END;
    BEGIN
        INSERT INTO public.managed_original_creations (
            id, owner_kind, dataset_id, project_id, task_id, location_id,
            storage_identity, bucket, object_key, logical_path, origin,
            max_bytes, timeout_seconds, content_type, requested_by, authority,
            configuration, input_fingerprint, maintenance_revision,
            staging_bucket, staging_key, staging_ref, source, status
        ) VALUES (
            v_creation_id, v_owner_kind, v_dataset_id, NULL, NULL,
            v_location.id, v_identity, v_bucket, v_object_key,
            CASE WHEN v_kind = 'role_media'
                     THEN COALESCE(NULLIF(v_logical_path, ''), v_business_path)
                 WHEN v_kind = 'item_media' THEN v_logical_path
                 ELSE '__aap_auxiliary_originals__/' || v_kind END,
            CASE WHEN v_kind IN ('item_media','role_media') THEN v_origin
                 ELSE 'connector' END,
            v_max_bytes, GREATEST(60, LEAST(3600,
                pg_catalog.ceil(v_max_bytes::numeric / 8388608)::integer)),
            v_content_type, v_requested_by, v_authority, v_config,
            v_fingerprint, v_revision,
            v_staging_bucket, v_staging_key, NULL, v_staging_source, 'allocated'
        ) RETURNING * INTO v_creation;
    EXCEPTION WHEN unique_violation THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_key_conflict');
    END;
    IF v_location.connection_id IS NOT NULL THEN
        v_destination := pg_catalog.jsonb_build_object(
            'kind', v_connection.kind, 'config', v_connection.config,
            'secret_enc', pg_catalog.replace(
                pg_catalog.encode(v_connection.secret_enc,'base64'), E'\n',''
            )
        );
    ELSE
        v_destination := NULL;
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        aap_assets_v1._managed_original_facts(v_creation)
        || pg_catalog.jsonb_build_object(
            'destination', v_destination, 'status', v_creation.status,
            'sha256', v_creation.sha256, 'size_bytes', v_creation.size_bytes,
            'etag', v_creation.etag, 'version_id', v_creation.version_id,
            'file_path', pg_catalog.substr(
                v_creation.object_key,
                pg_catalog.char_length(
                    COALESCE(v_creation.configuration -> 'original_ref' ->> 'key_prefix', '')
                ) + 1
            )
        ), NULL
    );
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.fail_operator_original(
    p_creation_id uuid,
    p_outcome text,
    p_error_code text,
    p_evidence jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_creation public.managed_original_creations%ROWTYPE;
    v_physical text;
    v_settled boolean;
    v_size bigint;
    v_delta bigint;
    v_facts jsonb;
BEGIN
    IF p_outcome NOT IN ('failed','conflict','unknown')
       OR p_error_code IS NULL OR p_error_code !~ '^[a-z][a-z0-9_]{0,79}$'
       OR pg_catalog.jsonb_typeof(p_evidence) IS DISTINCT FROM 'object'
       OR NOT (p_evidence ?& ARRAY['physical_settled','physical_outcome'])
       OR pg_catalog.jsonb_typeof(p_evidence -> 'physical_settled')
          IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_evidence -> 'physical_outcome')
          IS DISTINCT FROM 'string'
       OR (p_evidence ->> 'physical_outcome')
          NOT IN ('not_written','written','unknown')
       OR (p_evidence ? 'physical_size_bytes'
           AND (pg_catalog.jsonb_typeof(p_evidence -> 'physical_size_bytes') <> 'number'
                OR (p_evidence ->> 'physical_size_bytes') !~ '^(0|[1-9][0-9]*)$'
                OR pg_catalog.char_length(p_evidence ->> 'physical_size_bytes') > 19
                OR (p_evidence ->> 'physical_size_bytes')::numeric
                   > 9223372036854775807))
       OR (p_evidence ? 'current_head_matches'
           AND pg_catalog.jsonb_typeof(p_evidence -> 'current_head_matches')
               <> 'boolean')
       OR (p_evidence ? 'receipt'
           AND pg_catalog.jsonb_typeof(p_evidence -> 'receipt')
               NOT IN ('object','null'))
       OR (p_evidence ? 'observation'
           AND pg_catalog.jsonb_typeof(p_evidence -> 'observation')
               NOT IN ('object','null'))
       OR (p_evidence ? 'stage'
           AND pg_catalog.jsonb_typeof(p_evidence -> 'stage') <> 'string') THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    v_settled := (p_evidence ->> 'physical_settled')::boolean;
    v_physical := p_evidence ->> 'physical_outcome';
    -- An unsettled close cannot assert a physical outcome: it stays unknown and
    -- keeps both the permit and the pending accounting window.
    IF (NOT v_settled AND v_physical <> 'unknown')
       OR (p_outcome <> 'unknown' AND (NOT v_settled OR v_physical = 'unknown'))
       OR (p_outcome = 'conflict' AND v_physical <> 'not_written') THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    -- An observed write receipt contradicts a no-write claim even when its
    -- independent persistence failed and the creation still says writing.
    IF v_physical = 'not_written'
       AND COALESCE(p_evidence -> 'receipt', 'null'::jsonb) <> 'null'::jsonb THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    IF v_physical = 'written'
       AND (NOT (p_evidence ? 'receipt')
            OR pg_catalog.jsonb_typeof(p_evidence -> 'receipt') <> 'object') THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    SELECT creation.* INTO v_creation
    FROM public.managed_original_creations AS creation
    WHERE creation.id = p_creation_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
    END IF;
    IF v_creation.status NOT IN (
           'allocated','preparing','prepared','writing','written'
       ) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    IF v_creation.receipt IS NOT NULL AND (
        v_physical = 'not_written'
        OR (p_evidence ? 'receipt'
            AND p_evidence -> 'receipt' IS DISTINCT FROM v_creation.receipt)
    ) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
    END IF;
    UPDATE public.managed_original_creations
    SET status = p_outcome, error_code = p_error_code,
        failure_evidence = p_evidence,
        settled_at = CASE WHEN v_settled THEN pg_catalog.clock_timestamp() ELSE NULL END,
        completed_at = pg_catalog.clock_timestamp()
    WHERE id = p_creation_id
      AND status IN ('allocated','preparing','prepared','writing','written')
    RETURNING * INTO v_creation;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    v_facts := (p_evidence - 'physical_outcome' - 'physical_size_bytes'
                - 'current_delta_bytes')
        || pg_catalog.jsonb_build_object(
               'creation_status', p_outcome, 'error_code', p_error_code
           );
    IF v_settled THEN
        -- Known-drained: release only the permit that belongs to this creation.
        DELETE FROM public.storage_operations
        WHERE id = v_creation.operation_id
          AND location_id = v_creation.location_id
          AND revision = v_creation.maintenance_revision
          AND purpose = 'managed_original_creation';
        v_size := CASE
            WHEN p_evidence ? 'physical_size_bytes'
                THEN (p_evidence ->> 'physical_size_bytes')::bigint
            WHEN v_physical = 'written' THEN v_creation.size_bytes
            ELSE NULL
        END;
        v_delta := CASE WHEN v_physical = 'not_written' THEN 0 ELSE NULL END;
        UPDATE public.storage_usage_mutations AS mutation
        SET physical_outcome = v_physical,
            physical_size_bytes = v_size,
            current_delta_bytes = v_delta,
            state = CASE WHEN v_delta IS NULL THEN 'unknown' ELSE 'confirmed' END,
            finished_at = pg_catalog.clock_timestamp(),
            evidence = COALESCE(mutation.evidence, '{}'::jsonb) || v_facts
        WHERE mutation.id = (
            SELECT pending.id FROM public.storage_usage_mutations AS pending
            WHERE pending.producer = 'managed_original_creation'
              AND pending.kind = 'create'
              AND pending.owner_id = v_creation.id
              AND pending.state = 'pending'
            ORDER BY pending.started_at DESC, pending.id DESC
            LIMIT 1
        );
    ELSE
        -- Unknown drain: the permit and the pending intent are both retained;
        -- only diagnostic facts are added to the existing window.
        UPDATE public.storage_usage_mutations AS mutation
        SET evidence = COALESCE(mutation.evidence, '{}'::jsonb) || v_facts
        WHERE mutation.id = (
            SELECT pending.id FROM public.storage_usage_mutations AS pending
            WHERE pending.producer = 'managed_original_creation'
              AND pending.kind = 'create'
              AND pending.owner_id = v_creation.id
              AND pending.state = 'pending'
            ORDER BY pending.started_at DESC, pending.id DESC
            LIMIT 1
        );
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object(
            'creation_id', v_creation.id, 'status', p_outcome,
            'physical_settled', v_settled, 'physical_outcome', v_physical
        ), NULL
    );
END;
$function$;
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
                   OR (v_operation.kind = 'zip'
                       OR v_entry.source ->> 'content_hash' IS NOT NULL)
                      AND v_input ->> 'content_hash' IS DISTINCT FROM
                          v_entry.source ->> 'content_hash'
                   OR (v_operation.kind = 'zip' OR (v_entry.source ->> 'width') IS NOT NULL)
                      AND v_input ->> 'width' IS DISTINCT FROM v_entry.source ->> 'width'
                   OR (v_operation.kind = 'zip' OR (v_entry.source ->> 'height') IS NOT NULL)
                      AND v_input ->> 'height' IS DISTINCT FROM v_entry.source ->> 'height'
                   OR v_entry.source ->> 'file_type' <> 'image'
                      AND (v_input -> 'width' <> 'null'::jsonb
                           OR v_input -> 'height' <> 'null'::jsonb) THEN
                    RETURN aap_assets_v1._dataset_source_ingest_reply(NULL, 'operation_conflict');
                END IF;
                -- LIST/ETag is only an observation. A scan may replace a null
                -- hash hint only with the full bytes verified by its own creation.
                IF v_operation.kind = 'scan' AND NOT EXISTS (
                    SELECT 1 FROM public.managed_original_creations AS creation
                    WHERE creation.dataset_id = p_dataset_id
                      AND creation.owner_kind = 'dataset'
                      AND creation.origin = 'directory_copy'
                      AND creation.status = 'verified'
                      AND creation.verification IS NOT NULL
                      AND creation.requested_by = p_actor_id
                      AND creation.authority ->> 'kind' = 'dataset_source_ingest'
                      AND creation.authority ->> 'operation_id' = p_operation_id::text
                      AND creation.authority ->> 'batch_id' = p_batch_id::text
                      AND creation.authority ->> 'entry_index' = v_entry.entry_index::text
                      AND creation.authority ->> 'item_id' = v_entry.item_id::text
                      AND creation.size_bytes = (v_input ->> 'file_size')::bigint
                      AND creation.md5 = v_input ->> 'content_hash'
                      AND creation.logical_path = v_entry.source ->> 'relpath'
                      AND creation.object_key =
                          COALESCE(creation.configuration -> 'original_ref' ->> 'key_prefix', '')
                          || v_entry.file_path
                ) THEN
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
