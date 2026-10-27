-- Narrow owner capabilities for nuScenes auxiliary originals and pinned media
-- facts. Every caller-facing function is SECURITY DEFINER and is granted only
-- to the trusted operator LOGIN; the internal helpers below are ordinary
-- functions reachable only from those definers. No table or sequence privilege
-- is added by this artifact.
--
-- Physical responsibility is independent of business publication: allocation
-- persists an opaque Dataset identity without reading public.datasets, while
-- publication and item/scene binding validate the caller's business scope in
-- the caller's own unit of work.
CREATE OR REPLACE FUNCTION aap_assets_v1._managed_original_import_prefix(p_base text)
RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_base text := COALESCE(p_base, '');
    v_parts text[];
    v_result text;
BEGIN
    IF pg_catalog.right(v_base, 1) = '/' THEN
        v_base := pg_catalog.left(v_base, pg_catalog.char_length(v_base) - 1);
    END IF;
    IF v_base <> pg_catalog.btrim(v_base)
       OR pg_catalog.strpos(v_base, pg_catalog.chr(92)) > 0
       OR v_base ~ '[[:cntrl:]]' THEN
        RETURN NULL;
    END IF;
    IF v_base <> '' THEN
        v_parts := pg_catalog.string_to_array(v_base, '/');
        IF EXISTS (
            SELECT 1 FROM pg_catalog.unnest(v_parts) AS part(value)
            WHERE part.value = '' OR part.value IN ('.', '..')
        ) THEN
            RETURN NULL;
        END IF;
    END IF;
    v_result := CASE WHEN v_base = '' THEN '' ELSE v_base || '/' END
        || '_aap_managed/import/';
    IF pg_catalog.octet_length(v_result) > 645 THEN
        RETURN NULL;
    END IF;
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_original_import_prefix(text) FROM PUBLIC;
-- aap-statement-break

-- Mirror storage_usage_accounting.scope_key() exactly so native mutation
-- facts share the same physical scope as inventory observations.
CREATE OR REPLACE FUNCTION aap_assets_v1._managed_original_scope_key(
    p_endpoint_url text,
    p_bucket text
)
RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_endpoint_url IS NULL OR p_bucket IS NULL
       OR p_endpoint_url !~ '^https?://[^/?#@[:space:]]+$'
       OR pg_catalog.strpos(p_endpoint_url, '@') > 0 THEN
        RETURN NULL;
    END IF;
    RETURN pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                pg_catalog.jsonb_build_array(
                    pg_catalog.substr(
                        p_endpoint_url,
                        pg_catalog.strpos(p_endpoint_url, '://') + 3
                    ),
                    p_endpoint_url LIKE 'https://%',
                    p_bucket
                )::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._managed_original_scope_key(text,text) FROM PUBLIC;
-- aap-statement-break

-- Verified-reuse permits carry their creation in the durable purpose so a
-- close can never delete an unrelated or write permit.
CREATE OR REPLACE FUNCTION aap_assets_v1._operator_read_permit_purpose(p_creation_id uuid)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT 'managed_original_read_' || pg_catalog.replace(p_creation_id::text, '-', '')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._operator_read_permit_purpose(uuid) FROM PUBLIC;
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
        ELSIF v_existing.status IN ('failed','conflict','unknown') THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_terminal');
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
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_operator_auxiliary(jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.publish_operator_auxiliary(p_creation_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_creation public.managed_original_creations%ROWTYPE;
    v_owner public.dataset_auxiliary_originals%ROWTYPE;
    v_kind text;
    v_contract jsonb;
    v_ref jsonb;
BEGIN
    SELECT creation.* INTO v_creation
    FROM public.managed_original_creations AS creation
    WHERE creation.id = p_creation_id
      AND creation.owner_kind = 'dataset_auxiliary'
      AND creation.status = 'verified'
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
    END IF;
    v_kind := v_creation.authority -> 'auxiliary' ->> 'kind';
    v_contract := v_creation.authority -> 'auxiliary' -> 'source_contract';
    IF v_kind NOT IN ('raw_lidar','map')
       OR pg_catalog.jsonb_typeof(v_contract) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    SELECT owner.* INTO v_owner FROM public.dataset_auxiliary_originals AS owner
    WHERE owner.creation_id = v_creation.id;
    IF NOT FOUND THEN
        v_ref := v_creation.configuration -> 'original_ref';
        IF v_ref IS NULL OR pg_catalog.jsonb_typeof(v_ref) IS DISTINCT FROM 'object' THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
        END IF;
        INSERT INTO public.dataset_auxiliary_originals (
            id, creation_id, dataset_id, kind, source_contract, location_id,
            storage_identity, bucket, object_key, storage_ref, sha256,
            size_bytes, etag, version_id, created_at
        ) VALUES (
            v_creation.id, v_creation.id, v_creation.dataset_id, v_kind, v_contract,
            v_creation.location_id, v_creation.storage_identity, v_creation.bucket,
            v_creation.object_key, v_ref, v_creation.sha256, v_creation.size_bytes,
            v_creation.etag, v_creation.version_id,
            COALESCE(v_creation.completed_at, pg_catalog.clock_timestamp())
        ) RETURNING * INTO v_owner;
    ELSIF v_owner.sha256 IS DISTINCT FROM v_creation.sha256
       OR v_owner.size_bytes IS DISTINCT FROM v_creation.size_bytes
       OR v_owner.kind IS DISTINCT FROM v_kind THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.to_jsonb(v_owner), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.publish_operator_auxiliary(uuid) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.operator_auxiliary_owner(
    p_dataset_id uuid,
    p_kind text,
    p_contract jsonb,
    p_existing uuid
)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_owner public.dataset_auxiliary_originals%ROWTYPE;
BEGIN
    IF p_kind NOT IN ('raw_lidar','map')
       OR pg_catalog.jsonb_typeof(p_contract) IS DISTINCT FROM 'object' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    IF p_existing IS NOT NULL THEN
        SELECT owner.* INTO v_owner FROM public.dataset_auxiliary_originals AS owner
        WHERE owner.id = p_existing AND owner.dataset_id = p_dataset_id;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
        END IF;
    ELSE
        SELECT owner.* INTO v_owner FROM public.dataset_auxiliary_originals AS owner
        WHERE owner.dataset_id = p_dataset_id AND owner.kind = p_kind
          AND owner.source_contract = p_contract
        ORDER BY owner.created_at DESC, owner.id DESC LIMIT 1;
        IF NOT FOUND THEN
            RETURN aap_assets_v1._managed_original_reply(
                pg_catalog.jsonb_build_object('found', false), NULL
            );
        END IF;
    END IF;
    IF v_owner.kind IS DISTINCT FROM p_kind
       OR v_owner.source_contract IS DISTINCT FROM p_contract THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.to_jsonb(v_owner) || pg_catalog.jsonb_build_object('found', true),
        NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.operator_auxiliary_owner(uuid,text,jsonb,uuid) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.operator_auxiliary_material(
    p_dataset_id uuid,
    p_owner_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_owner public.dataset_auxiliary_originals%ROWTYPE;
    v_location public.storage_locations%ROWTYPE;
    v_connection public.storage_connections%ROWTYPE;
    v_destination jsonb;
BEGIN
    SELECT owner.* INTO v_owner FROM public.dataset_auxiliary_originals AS owner
    WHERE owner.id = p_owner_id AND owner.dataset_id = p_dataset_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
    END IF;
    SELECT location.* INTO v_location FROM public.storage_locations AS location
    WHERE location.id = v_owner.location_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END IF;
    IF v_location.connection_id IS NOT NULL THEN
        SELECT connection.* INTO v_connection FROM public.storage_connections AS connection
        WHERE connection.id = v_location.connection_id;
        IF NOT FOUND OR v_connection.secret_enc IS NULL THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
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
        pg_catalog.to_jsonb(v_owner) || pg_catalog.jsonb_build_object(
            'endpoint_url', v_owner.storage_ref ->> 'endpoint_url',
            'key_prefix', v_owner.storage_ref ->> 'key_prefix',
            'destination', v_destination
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.operator_auxiliary_material(uuid,uuid) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.operator_item_original_material(
    p_dataset_id uuid,
    p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item public.dataset_items%ROWTYPE;
    v_replica public.dataset_asset_locations%ROWTYPE;
    v_creation public.managed_original_creations%ROWTYPE;
    v_location public.storage_locations%ROWTYPE;
    v_connection public.storage_connections%ROWTYPE;
    v_destination jsonb;
    v_ref jsonb;
BEGIN
    SELECT item.* INTO v_item FROM public.dataset_items AS item
    WHERE item.id = p_item_id AND item.dataset_id = p_dataset_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(
            pg_catalog.jsonb_build_object('found', false), NULL
        );
    END IF;
    SELECT replica.* INTO v_replica FROM public.dataset_asset_locations AS replica
    JOIN public.dataset_assets AS asset ON asset.id = replica.asset_id
    WHERE asset.dataset_item_id = p_item_id
      AND asset.verification_state = 'verified'
      AND replica.role = 'managed_original'
      AND replica.creation_id IS NOT NULL
    ORDER BY replica.verified_at DESC NULLS LAST LIMIT 1;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(
            pg_catalog.jsonb_build_object('found', true, 'source', NULL), NULL
        );
    END IF;
    SELECT creation.* INTO v_creation FROM public.managed_original_creations AS creation
    WHERE creation.id = v_replica.creation_id AND creation.owner_kind = 'dataset'
      AND creation.dataset_id = p_dataset_id AND creation.status = 'verified';
    IF NOT FOUND OR v_replica.object_key IS DISTINCT FROM v_item.file_path THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
    END IF;
    SELECT location.* INTO v_location FROM public.storage_locations AS location
    WHERE location.id = v_creation.location_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END IF;
    IF v_location.connection_id IS NOT NULL THEN
        SELECT connection.* INTO v_connection FROM public.storage_connections AS connection
        WHERE connection.id = v_location.connection_id;
        IF NOT FOUND OR v_connection.secret_enc IS NULL THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
        END IF;
        v_destination := pg_catalog.jsonb_build_object(
            'kind', v_connection.kind, 'config', v_connection.config,
            'secret_enc', pg_catalog.replace(
                pg_catalog.encode(v_connection.secret_enc,'base64'), E'\n',''
            )
        );
    ELSE
        v_destination := NULL;
    END IF;
    v_ref := v_creation.configuration -> 'original_ref';
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object(
            'found', true,
            'source', pg_catalog.jsonb_build_object(
                'creation_id', v_creation.id, 'sha256', v_creation.sha256,
                'size_bytes', v_creation.size_bytes, 'object_key', v_creation.object_key,
                'bucket', v_creation.bucket, 'location_id', v_creation.location_id,
                'etag', v_creation.etag, 'version_id', v_creation.version_id,
                'storage_ref', v_ref,
                'endpoint_url', v_ref ->> 'endpoint_url',
                'key_prefix', v_ref ->> 'key_prefix',
                'destination', v_destination
            )
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.operator_item_original_material(uuid,uuid) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.bind_operator_auxiliary_item(
    p_dataset_id uuid,
    p_item_id uuid,
    p_owner_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_owner public.dataset_auxiliary_originals%ROWTYPE;
    v_item public.dataset_items%ROWTYPE;
BEGIN
    SELECT owner.* INTO v_owner FROM public.dataset_auxiliary_originals AS owner
    WHERE owner.id = p_owner_id AND owner.dataset_id = p_dataset_id
      AND owner.kind = 'raw_lidar';
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    SELECT item.* INTO v_item FROM public.dataset_items AS item
    WHERE item.id = p_item_id AND item.dataset_id = p_dataset_id FOR UPDATE;
    IF NOT FOUND OR v_item.file_type <> 'point_cloud'
       OR v_item.nuscenes_raw_original_id NOT IN (NULL, v_owner.id) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    IF ((v_item.metadata -> 'nuscenes_export') - 'source_storage_key'
            - 'source_sha256' - 'source_file_size')
        IS DISTINCT FROM v_owner.source_contract
       OR (v_item.metadata -> 'nuscenes_export' ->> 'source_storage_key')
        IS DISTINCT FROM v_owner.object_key
       OR (v_item.metadata -> 'nuscenes_export' ->> 'source_sha256')
        IS DISTINCT FROM v_owner.sha256
       OR (v_item.metadata -> 'nuscenes_export' ->> 'source_file_size')::bigint
        IS DISTINCT FROM v_owner.size_bytes THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
    END IF;
    UPDATE public.dataset_items SET nuscenes_raw_original_id = v_owner.id,
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = p_item_id;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object('item_id', p_item_id, 'owner_id', v_owner.id), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.bind_operator_auxiliary_item(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.bind_operator_auxiliary_scene(
    p_dataset_id uuid,
    p_scene_id uuid,
    p_owner_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_owner public.dataset_auxiliary_originals%ROWTYPE;
    v_scene public.scenes%ROWTYPE;
BEGIN
    SELECT owner.* INTO v_owner FROM public.dataset_auxiliary_originals AS owner
    WHERE owner.id = p_owner_id AND owner.dataset_id = p_dataset_id
      AND owner.kind = 'map';
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    SELECT scene.* INTO v_scene FROM public.scenes AS scene
    WHERE scene.id = p_scene_id AND scene.dataset_id = p_dataset_id FOR UPDATE;
    IF NOT FOUND OR v_scene.source_format <> 'nuscenes'
       OR v_scene.nuscenes_map_original_id NOT IN (NULL, v_owner.id) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    IF (v_scene.source_metadata -> 'nuscenes_export' -> 'map')
        IS DISTINCT FROM v_owner.source_contract
       OR (v_scene.source_metadata -> 'nuscenes_export' ->> 'map_storage_key')
        IS DISTINCT FROM v_owner.object_key
       OR (v_scene.source_metadata -> 'nuscenes_export' ->> 'map_sha256')
        IS DISTINCT FROM v_owner.sha256
       OR (v_scene.source_metadata -> 'nuscenes_export' ->> 'map_file_size')::bigint
        IS DISTINCT FROM v_owner.size_bytes THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
    END IF;
    UPDATE public.scenes SET nuscenes_map_original_id = v_owner.id,
        updated_at = pg_catalog.clock_timestamp()
    WHERE id = p_scene_id;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object('scene_id', p_scene_id, 'owner_id', v_owner.id), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.bind_operator_auxiliary_scene(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.operator_dataset_state(p_dataset_id uuid)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row public.datasets%ROWTYPE;
BEGIN
    SELECT dataset.* INTO v_row FROM public.datasets AS dataset
    WHERE dataset.id = p_dataset_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object(
            'name', v_row.name, 'created_by', v_row.created_by,
            'data_type', v_row.data_type, 'is_temporal', v_row.is_temporal,
            'metadata', v_row.metadata
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.operator_dataset_state(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.operator_scene_state(
    p_dataset_id uuid,
    p_scene_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row public.scenes%ROWTYPE;
BEGIN
    SELECT scene.* INTO v_row FROM public.scenes AS scene
    WHERE scene.id = p_scene_id AND scene.dataset_id = p_dataset_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object(
            'name', v_row.name, 'source_format', v_row.source_format,
            'source_metadata', v_row.source_metadata
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.operator_scene_state(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.open_operator_storage_operation(p_creation_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_creation public.managed_original_creations%ROWTYPE;
    v_location public.storage_locations%ROWTYPE;
    v_revision integer;
    v_operation uuid;
    v_mode text;
    v_purpose text;
BEGIN
    SELECT creation.* INTO v_creation
    FROM public.managed_original_creations AS creation
    WHERE creation.id = p_creation_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
    END IF;
    SELECT location.* INTO v_location
    FROM public.storage_locations AS location
    WHERE location.id = v_creation.location_id;
    IF NOT FOUND OR NOT v_location.enabled OR v_location.kind <> 's3' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END IF;
    -- The saved configuration must still describe this exact creation before a
    -- caller acts on it; drift is never silently accepted. These comparisons
    -- are independent of which allocator (native SQL or Main Python) froze it.
    IF v_creation.configuration -> 'original_ref' IS NULL
       OR v_creation.configuration -> 'original_ref' ->> 'endpoint_url' IS NULL
       OR v_creation.location_id::text
          IS DISTINCT FROM v_creation.configuration -> 'original_ref' ->> 'location_id'
       OR v_creation.bucket
          IS DISTINCT FROM v_creation.configuration -> 'original_ref' ->> 'bucket'
       OR v_creation.object_key
          IS DISTINCT FROM v_creation.configuration -> 'original_ref' ->> 'object_key'
       OR v_creation.storage_identity
          IS DISTINCT FROM v_creation.configuration -> 'original_ref' ->> 'storage_identity'
       OR v_creation.configuration ->> 'endpoint'
          IS DISTINCT FROM v_creation.configuration -> 'original_ref' ->> 'endpoint_url'
       OR v_creation.storage_identity IS DISTINCT FROM
          aap_assets_v1._managed_original_identity(
              v_creation.configuration -> 'original_ref' ->> 'endpoint_url',
              v_creation.bucket
          ) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_configuration_changed');
    END IF;
    BEGIN
        v_revision := aap_assets_v1._managed_original_gate_revision(v_creation.location_id);
    EXCEPTION WHEN SQLSTATE '55006' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable');
    END;
    IF v_revision IS DISTINCT FROM v_creation.maintenance_revision THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_configuration_changed');
    END IF;
    IF v_creation.status = 'allocated' THEN
        -- Acquisition and claim are one atomic step: a caller can never hold a
        -- permit for a creation another caller claimed, or claim without one.
        v_mode := 'write';
        v_purpose := 'managed_original_creation';
    ELSIF v_creation.status = 'verified' THEN
        -- Verified reuse is read-only and never re-claims the creation.
        v_mode := 'read';
        v_purpose := aap_assets_v1._operator_read_permit_purpose(v_creation.id);
    ELSE
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    v_operation := pg_catalog.gen_random_uuid();
    INSERT INTO public.storage_operations (
        id, location_id, revision, purpose, node_id
    ) VALUES (
        v_operation, v_creation.location_id, v_revision, v_purpose, 'aap-assets-owner'
    );
    IF v_mode = 'write' THEN
        UPDATE public.managed_original_creations
        SET status = 'preparing', claimed_at = pg_catalog.clock_timestamp(),
            operation_id = v_operation
        WHERE id = p_creation_id AND status = 'allocated';
        IF NOT FOUND THEN
            RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
        END IF;
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object(
            'operation_id', v_operation, 'mode', v_mode
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.open_operator_storage_operation(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.record_operator_original_prepared(
    p_creation_id uuid,
    p_prepared jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_creation public.managed_original_creations%ROWTYPE;
BEGIN
    SELECT creation.* INTO v_creation
    FROM public.managed_original_creations AS creation
    WHERE creation.id = p_creation_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
    END IF;
    IF v_creation.status <> 'preparing' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    IF pg_catalog.jsonb_typeof(p_prepared) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_prepared -> 'sha256') IS DISTINCT FROM 'string'
       OR (p_prepared ->> 'sha256') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_prepared -> 'md5') IS DISTINCT FROM 'string'
       OR (p_prepared ->> 'md5') !~ '^[0-9a-f]{32}$'
       OR pg_catalog.jsonb_typeof(p_prepared -> 'size_bytes') IS DISTINCT FROM 'number'
       OR (p_prepared ->> 'size_bytes') !~ '^(0|[1-9][0-9]*)$'
       OR (p_prepared ? 'bucket'
           AND (p_prepared ->> 'bucket') IS DISTINCT FROM v_creation.bucket)
       OR (p_prepared ? 'object_key'
           AND (p_prepared ->> 'object_key') IS DISTINCT FROM v_creation.object_key) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    -- Numeric comparison avoids a cast failure on an arbitrarily long literal.
    IF (p_prepared ->> 'size_bytes')::numeric > v_creation.max_bytes
       OR (p_prepared ->> 'size_bytes')::numeric > 9223372036854775807 THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_byte_budget_exceeded');
    END IF;
    UPDATE public.managed_original_creations
    SET status = 'prepared', frozen_at = pg_catalog.clock_timestamp(),
        sha256 = p_prepared ->> 'sha256', md5 = p_prepared ->> 'md5',
        size_bytes = (p_prepared ->> 'size_bytes')::bigint,
        prepared = p_prepared
    WHERE id = p_creation_id AND status = 'preparing'
    RETURNING * INTO v_creation;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object(
            'creation_id', v_creation.id, 'sha256', v_creation.sha256,
            'md5', v_creation.md5, 'size_bytes', v_creation.size_bytes
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_operator_original_prepared(uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.mark_operator_original_writing(p_creation_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_creation public.managed_original_creations%ROWTYPE;
    v_scope text;
    v_identity text;
    v_attempt uuid;
BEGIN
    UPDATE public.managed_original_creations
    SET status = 'writing', write_started_at = pg_catalog.clock_timestamp()
    WHERE id = p_creation_id AND status = 'prepared'
    RETURNING * INTO v_creation;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    -- Commit the durable accounting intent with the writing transition so a
    -- crash before completion cannot lose the unresolved physical write.
    v_scope := aap_assets_v1._managed_original_scope_key(
        v_creation.configuration ->> 'endpoint', v_creation.bucket
    );
    IF v_scope IS NULL OR v_creation.sha256 IS NULL
       OR v_creation.md5 IS NULL OR v_creation.size_bytes IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '55000',
            MESSAGE = 'managed_original_accounting_intent_unavailable';
    END IF;
    v_attempt := pg_catalog.gen_random_uuid();
    v_identity := pg_catalog.encode(
        public.digest(
            pg_catalog.convert_to(
                pg_catalog.jsonb_build_array(
                    'managed_original_creation', v_scope, v_creation.id::text,
                    v_creation.object_key, 'create', v_creation.id::text
                )::text,
                'UTF8'
            ),
            'sha256'
        ),
        'hex'
    );
    INSERT INTO public.storage_usage_mutations (
        id, idempotency_key, scope_key, object_key, key_hash, producer, purpose,
        owner_id, kind, expected_version, state, physical_outcome, evidence
    ) VALUES (
        v_attempt, v_identity, v_scope, v_creation.object_key,
        pg_catalog.encode(
            public.digest(
                pg_catalog.convert_to(v_creation.object_key, 'UTF8'), 'sha256'
            ), 'hex'
        ),
        'managed_original_creation', 'managed_original', v_creation.id, 'create',
        NULL, 'pending', 'unknown',
        pg_catalog.jsonb_build_object(
            'owner_kind', v_creation.owner_kind,
            'origin', v_creation.origin,
            'logical_intent_id', v_attempt::text,
            'logical_group', v_identity,
            'attempt_nonce', v_attempt::text
        )
    );
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object(
            'creation_id', v_creation.id, 'status', v_creation.status,
            'mutation_id', v_attempt
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.mark_operator_original_writing(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.record_operator_original_written(
    p_creation_id uuid,
    p_receipt jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_creation public.managed_original_creations%ROWTYPE;
BEGIN
    IF pg_catalog.jsonb_typeof(p_receipt) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_receipt -> 'etag') IS DISTINCT FROM 'string'
       OR pg_catalog.char_length(p_receipt ->> 'etag') NOT BETWEEN 1 AND 1024
       OR (p_receipt ->> 'etag') = 'null'
       OR (p_receipt ? 'version_id' AND NOT (
               (p_receipt -> 'version_id') = 'null'::jsonb
               OR (pg_catalog.jsonb_typeof(p_receipt -> 'version_id') = 'string'
                   AND pg_catalog.char_length(p_receipt ->> 'version_id')
                       BETWEEN 1 AND 1024
                   AND (p_receipt ->> 'version_id') <> 'null')))
       OR (p_receipt ? 'request_id' AND NOT (
               (p_receipt -> 'request_id') = 'null'::jsonb
               OR (pg_catalog.jsonb_typeof(p_receipt -> 'request_id') = 'string'
                   AND pg_catalog.char_length(p_receipt ->> 'request_id')
                       BETWEEN 1 AND 1024))) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    UPDATE public.managed_original_creations
    SET status = 'written', acknowledged_at = pg_catalog.clock_timestamp(),
        receipt = p_receipt, etag = p_receipt ->> 'etag',
        version_id = NULLIF(p_receipt ->> 'version_id', 'null'),
        request_id = NULLIF(p_receipt ->> 'request_id', 'null')
    WHERE id = p_creation_id AND status = 'writing'
    RETURNING * INTO v_creation;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object(
            'creation_id', v_creation.id, 'status', v_creation.status,
            'etag', v_creation.etag, 'version_id', v_creation.version_id
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_operator_original_written(uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.record_operator_original_verified(
    p_creation_id uuid,
    p_verification jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_creation public.managed_original_creations%ROWTYPE;
    v_delta bigint;
BEGIN
    SELECT creation.* INTO v_creation
    FROM public.managed_original_creations AS creation
    WHERE creation.id = p_creation_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
    END IF;
    IF v_creation.status <> 'written' THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    IF pg_catalog.jsonb_typeof(p_verification) IS DISTINCT FROM 'object'
       OR NOT (p_verification ?& ARRAY[
           'sha256','md5','size_bytes','etag','version_id','physical_settled'
       ])
       OR pg_catalog.jsonb_typeof(p_verification -> 'sha256') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_verification -> 'md5') IS DISTINCT FROM 'string'
       OR pg_catalog.jsonb_typeof(p_verification -> 'size_bytes')
          IS DISTINCT FROM 'number'
       OR (p_verification ->> 'size_bytes') !~ '^(0|[1-9][0-9]*)$'
       OR pg_catalog.jsonb_typeof(p_verification -> 'etag') IS DISTINCT FROM 'string'
       OR NOT ((p_verification -> 'version_id') = 'null'::jsonb
               OR (pg_catalog.jsonb_typeof(p_verification -> 'version_id') = 'string'
                   AND pg_catalog.char_length(p_verification ->> 'version_id')
                       BETWEEN 1 AND 1024
                   AND (p_verification ->> 'version_id') <> 'null'))
       OR pg_catalog.jsonb_typeof(p_verification -> 'physical_settled')
          IS DISTINCT FROM 'boolean'
       OR (p_verification ->> 'physical_settled')::boolean IS NOT TRUE
       OR (p_verification ? 'current_head_matches'
           AND pg_catalog.jsonb_typeof(p_verification -> 'current_head_matches')
               <> 'boolean')
       OR (p_verification ? 'observation'
           AND pg_catalog.jsonb_typeof(p_verification -> 'observation')
               NOT IN ('object','null')) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    -- Readback must reproduce the frozen content and the acknowledged receipt
    -- field by field; a contradictory observation never settles anything.
    IF (p_verification ->> 'sha256') IS DISTINCT FROM v_creation.sha256
       OR (p_verification ->> 'md5') IS DISTINCT FROM v_creation.md5
       OR (p_verification ->> 'size_bytes')::numeric
          IS DISTINCT FROM v_creation.size_bytes::numeric
       OR (p_verification ->> 'etag') IS DISTINCT FROM v_creation.etag
       OR (p_verification ->> 'version_id') IS DISTINCT FROM v_creation.version_id THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_evidence_mismatch');
    END IF;
    UPDATE public.managed_original_creations
    SET status = 'verified', verification = p_verification,
        readback_at = pg_catalog.clock_timestamp(),
        settled_at = pg_catalog.clock_timestamp(),
        completed_at = pg_catalog.clock_timestamp()
    WHERE id = p_creation_id AND status = 'written'
    RETURNING * INTO v_creation;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_state_conflict');
    END IF;
    -- Only this creation's own write permit may settle, and only now.
    DELETE FROM public.storage_operations
    WHERE id = v_creation.operation_id
      AND location_id = v_creation.location_id
      AND revision = v_creation.maintenance_revision
      AND purpose = 'managed_original_creation';
    -- Attribute the physical delta exactly as Main's accounting does: only a
    -- version-pinned, current-head-matching readback confirms bytes.
    v_delta := CASE
        WHEN v_creation.version_id IS NOT NULL
         AND v_creation.version_id NOT IN ('', 'null')
         AND p_verification -> 'current_head_matches' = 'true'::jsonb
        THEN v_creation.size_bytes
        ELSE NULL
    END;
    UPDATE public.storage_usage_mutations AS mutation
    SET physical_outcome = 'written',
        physical_size_bytes = v_creation.size_bytes,
        current_delta_bytes = v_delta,
        state = CASE WHEN v_delta IS NULL THEN 'unknown' ELSE 'confirmed' END,
        finished_at = pg_catalog.clock_timestamp(),
        evidence = COALESCE(mutation.evidence, '{}'::jsonb)
            || pg_catalog.jsonb_strip_nulls(pg_catalog.jsonb_build_object(
                   'full_readback', true,
                   'current_head_matches', p_verification -> 'current_head_matches',
                   'returned_version', NULLIF(
                       v_creation.receipt -> 'response' ->> 'VersionId', 'null'
                   )
               ))
    WHERE mutation.id = (
        SELECT pending.id FROM public.storage_usage_mutations AS pending
        WHERE pending.producer = 'managed_original_creation'
          AND pending.kind = 'create'
          AND pending.owner_id = v_creation.id
          AND pending.state = 'pending'
        ORDER BY pending.started_at DESC, pending.id DESC
        LIMIT 1
    );
    RETURN aap_assets_v1._managed_original_reply(
        aap_assets_v1._managed_original_facts(v_creation)
        || pg_catalog.jsonb_build_object(
            'status', v_creation.status, 'sha256', v_creation.sha256,
            'md5', v_creation.md5, 'size_bytes', v_creation.size_bytes,
            'etag', v_creation.etag, 'version_id', v_creation.version_id
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_operator_original_verified(uuid,jsonb) FROM PUBLIC;
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
    IF NOT v_settled AND v_physical <> 'unknown' THEN
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
    UPDATE public.managed_original_creations
    SET status = p_outcome, error_code = p_error_code,
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
REVOKE ALL ON FUNCTION aap_assets_v1.fail_operator_original(uuid,text,text,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.close_operator_storage_operation(
    p_creation_id uuid,
    p_operation_id uuid,
    p_physical_settled boolean
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_creation public.managed_original_creations%ROWTYPE;
    v_purpose text;
BEGIN
    IF p_physical_settled IS NULL THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'invalid_request');
    END IF;
    SELECT creation.* INTO v_creation
    FROM public.managed_original_creations AS creation
    WHERE creation.id = p_creation_id
    FOR SHARE;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_missing');
    END IF;
    -- Only a creation-bound read permit may close here; write permits settle
    -- exclusively through the verified or failure transitions.
    v_purpose := aap_assets_v1._operator_read_permit_purpose(v_creation.id);
    IF NOT EXISTS (
        SELECT 1 FROM public.storage_operations AS operation
        WHERE operation.id = p_operation_id
          AND operation.location_id = v_creation.location_id
          AND operation.revision = v_creation.maintenance_revision
          AND operation.purpose = v_purpose
    ) THEN
        RETURN aap_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch');
    END IF;
    IF p_physical_settled IS NOT TRUE THEN
        -- Unknown drain keeps the durable responsibility; a later proven close
        -- of the same permit may still settle it.
        RETURN aap_assets_v1._managed_original_reply(NULL, 'operation_unsettled');
    END IF;
    DELETE FROM public.storage_operations
    WHERE id = p_operation_id
      AND location_id = v_creation.location_id
      AND revision = v_creation.maintenance_revision
      AND purpose = v_purpose;
    RETURN aap_assets_v1._managed_original_reply(
        pg_catalog.jsonb_build_object(
            'creation_id', v_creation.id, 'operation_id', p_operation_id,
            'status', 'settled'
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.close_operator_storage_operation(uuid,uuid,boolean) FROM PUBLIC;
-- aap-statement-break
DROP FUNCTION IF EXISTS aap_assets_v1.mark_operator_original_claimed(uuid,uuid) RESTRICT;
-- aap-statement-break
DROP FUNCTION aap_assets_v1.begin_managed_original(jsonb) RESTRICT;
-- aap-statement-break
DROP FUNCTION aap_assets_v1.record_managed_original_proof(uuid,jsonb) RESTRICT;
