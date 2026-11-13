-- Retry a settled failed managed original under the same authority instead of
-- refusing the pending upload forever. The reserved destination/staging keys
-- are released for a retry only by immutable failed/conflict evidence rows;
-- active, verified and uncertain creations still own them. Uncertain writes
-- remain terminal and are never repeated.
ALTER TABLE public.managed_original_creations
    DROP CONSTRAINT uq_managed_creation_reserved_key;
-- aap-statement-break
CREATE UNIQUE INDEX uq_managed_creation_reserved_key_active
    ON public.managed_original_creations (storage_identity, bucket, object_key)
    WHERE status NOT IN ('failed','conflict');
-- aap-statement-break
ALTER TABLE public.managed_original_creations
    DROP CONSTRAINT managed_original_creations_staging_key_key;
-- aap-statement-break
CREATE UNIQUE INDEX uq_managed_creation_staging_key_active
    ON public.managed_original_creations (staging_key)
    WHERE staging_key IS NOT NULL AND status NOT IN ('failed','conflict');
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
            -- A settled failed attempt wrote nothing that may be published.
            -- Retain its immutable evidence and allocate a new creation for the
            -- next attempt under the same fixed publication identity.
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
