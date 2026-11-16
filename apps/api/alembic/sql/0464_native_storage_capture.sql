-- Private saved-target context for the Assets Source signer.
--
-- Source owns registered connection secrets and host policy.  These three
-- capabilities resolve only the immutable saved allocation, recheck the exact
-- operation/lease/gate/output fence, and return a context that Source signs.
-- Ordinary API, worker and cleanup processes never receive the destination
-- credentials, and no current purpose binding can redirect a saved target.

CREATE OR REPLACE FUNCTION aap_assets_v1._native_pyramid_target_destination(
    p_location_id uuid, p_endpoint_url text, p_bucket text, p_storage_identity text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_location public.storage_locations%ROWTYPE;
    v_connection public.storage_connections%ROWTYPE;
    v_endpoint text;
    v_destination jsonb;
    v_setting jsonb;
BEGIN
    IF p_location_id IS NULL OR p_endpoint_url IS NULL OR p_bucket IS NULL
       OR p_storage_identity IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT location.* INTO v_location
    FROM public.storage_locations AS location
    WHERE location.id = p_location_id
    FOR SHARE;
    -- A disabled location may still hold readable and retirable saved bytes;
    -- fresh writes retain their enabled/location/gate checks in allocation.
    IF NOT FOUND OR v_location.kind <> 's3' THEN
        RETURN NULL;
    END IF;
    IF v_location.connection_id IS NULL THEN
        IF pg_catalog.jsonb_typeof(v_location.config -> 'use_ssl')
                IS DISTINCT FROM 'boolean'
           OR pg_catalog.jsonb_typeof(v_location.config -> 'endpoint')
                IS DISTINCT FROM 'string' THEN
            RETURN NULL;
        END IF;
        v_endpoint := CASE WHEN (v_location.config ->> 'use_ssl')::boolean
            THEN 'https://' ELSE 'http://' END
            || pg_catalog.btrim(v_location.config ->> 'endpoint');
        v_endpoint := aap_assets_v1._managed_original_endpoint(v_endpoint);
        v_destination := NULL;
    ELSE
        SELECT connection.* INTO v_connection
        FROM public.storage_connections AS connection
        WHERE connection.id = v_location.connection_id
        FOR SHARE;
        IF NOT FOUND OR v_connection.kind <> 's3'
           OR v_connection.scope <> 'global'
           OR v_connection.project_id IS NOT NULL
           OR v_connection.secret_enc IS NULL
           OR pg_catalog.octet_length(v_connection.secret_enc) NOT BETWEEN 1 AND 49152
           OR pg_catalog.jsonb_typeof(v_connection.config -> 'endpoint')
                IS DISTINCT FROM 'string'
           -- The saved bucket must still be the registered connection's bucket.
           -- Credential rotation or an A->B purpose switch leaves it untouched;
           -- a mutated or non-string connection bucket is a real refusal.
           OR pg_catalog.jsonb_typeof(v_connection.config -> 'bucket')
                IS DISTINCT FROM 'string'
           OR v_connection.config ->> 'bucket' IS DISTINCT FROM p_bucket THEN
            RETURN NULL;
        END IF;
        v_endpoint := aap_assets_v1._managed_original_endpoint(
            pg_catalog.rtrim(v_connection.config ->> 'endpoint', '/')
        );
        v_destination := pg_catalog.jsonb_build_object(
            'kind', v_connection.kind,
            'config', v_connection.config,
            'secret_enc', pg_catalog.replace(
                pg_catalog.encode(v_connection.secret_enc, 'base64'), E'\n', ''
            )
        );
    END IF;
    -- The saved coordinates must still describe this registered location and
    -- connection.  Credential rotation advances the connection/location
    -- revision but not the endpoint; an endpoint mutation is a real refusal.
    IF v_endpoint IS NULL
       OR v_endpoint IS DISTINCT FROM p_endpoint_url
       OR aap_assets_v1._managed_original_identity(p_endpoint_url, p_bucket)
            IS DISTINCT FROM p_storage_identity THEN
        RETURN NULL;
    END IF;
    v_setting := aap_settings_v1.resolve_connector_host_allowlist('[]'::jsonb);
    IF v_setting -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN NULL;
    END IF;
    -- A missing database setting is distinct from an explicitly stored empty
    -- array.  Source applies its configured fallback only when the captured
    -- value is null; an explicit database [] remains an authoritative deny-all
    -- policy and must never be replaced by the environment fallback.
    RETURN pg_catalog.jsonb_build_object(
        'destination', v_destination,
        'host_allowlist', CASE
            WHEN v_setting #>> '{data,source}' = 'database'
                THEN v_setting #> '{data,entries}'
            ELSE NULL
        END
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._native_pyramid_target_destination(uuid, text, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.capture_native_pyramid_write_io(
    p_operation_id uuid, p_job_id uuid, p_job_generation bigint,
    p_lease_token uuid, p_mode text
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_member aap_assets_v1.image_pyramid_native_members%ROWTYPE;
    v_attempt jsonb;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_target aap_assets_v1.native_pyramid_retirement_targets%ROWTYPE;
    v_io jsonb;
    v_members jsonb;
    v_ref jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_job_id IS NULL OR p_job_generation IS NULL
       OR p_job_generation < 1 OR p_lease_token IS NULL
       OR p_mode IS NULL OR p_mode NOT IN ('put', 'verify') THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT member.* INTO v_member
    FROM aap_assets_v1.image_pyramid_native_members AS member
    WHERE member.operation_id = p_operation_id;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
    END IF;
    BEGIN
        v_attempt := aap_assets_v1._native_pyramid_attempt(
            v_member.output_id, true
        );
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF v_attempt IS NULL OR v_attempt ->> 'state' IS DISTINCT FROM 'building'
       OR v_attempt ->> 'job_id' IS DISTINCT FROM p_job_id::text
       OR (v_attempt ->> 'job_generation')::bigint IS DISTINCT FROM p_job_generation
       OR v_attempt ->> 'lease_token' IS DISTINCT FROM p_lease_token::text THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    BEGIN
        SELECT member.* INTO v_member
        FROM aap_assets_v1.image_pyramid_native_members AS member
        WHERE member.operation_id = p_operation_id
        FOR SHARE;
        SELECT output.* INTO v_output
        FROM public.image_pyramid_outputs AS output
        WHERE output.id = v_member.output_id;
        SELECT target.* INTO v_target
        FROM aap_assets_v1.native_pyramid_retirement_targets AS target
        WHERE target.output_id = v_member.output_id;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND OR v_output.id IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
    END IF;
    IF aap_assets_v1._native_pyramid_output_fixture_owned(v_output.id) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'permission_denied');
    END IF;
    IF p_mode = 'put' THEN
        -- A put is admitted once, before any physical request.  An observed
        -- receipt has already moved the member past this state.
        IF v_member.state IS DISTINCT FROM 'submitted' THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        v_members := pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
            'operation_id', v_member.operation_id,
            'relative_key', v_member.relative_key,
            'sha256', v_member.sha256,
            'size_bytes', v_member.size_bytes,
            'etag', NULL,
            'version_id', NULL
        ));
    ELSE
        -- Verification uses only the recorded written receipt; it never asks
        -- the caller for a current version to guess.
        IF v_member.state NOT IN ('written', 'verified')
           OR v_member.etag IS NULL
           OR v_member.etag !~ '^"[^"]+"$'
           OR pg_catalog.length(v_member.etag) > 1024
           OR (v_member.version_id IS NOT NULL
               AND pg_catalog.length(v_member.version_id) > 1024) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        -- Verification is the success outcome of this member's already admitted
        -- durable operation.  A closed or absent permit means that operation is
        -- settled, so no new verification capability may be issued.
        IF NOT EXISTS (
            SELECT 1
            FROM public.storage_operations AS operation
            WHERE operation.id = v_member.operation_permit
              AND operation.location_id = v_target.location_id
              AND operation.purpose = (
                    'image_pyramid_'
                        || pg_catalog.replace(v_member.output_id::text, '-', '')
                )
        ) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
        END IF;
        v_members := pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
            'operation_id', v_member.operation_id,
            'relative_key', v_member.relative_key,
            'sha256', v_member.sha256,
            'size_bytes', v_member.size_bytes,
            'etag', v_member.etag,
            'version_id', v_member.version_id
        ));
    END IF;
    v_ref := v_attempt -> 'storage_ref';
    IF v_ref IS DISTINCT FROM v_output.storage_ref
       OR NOT aap_assets_v1._native_pyramid_retirement_target_valid(
            v_ref, v_output.id
          )
       OR v_target.output_id IS DISTINCT FROM v_output.id
       OR v_target.asset_id IS DISTINCT FROM v_output.asset_id
       OR v_target.generation IS DISTINCT FROM v_output.generation
       OR NOT aap_assets_v1._native_pyramid_retirement_policy_valid(v_target.policy) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_io := aap_assets_v1._native_pyramid_target_destination(
        v_target.location_id, v_target.endpoint_url, v_target.bucket,
        v_target.storage_identity
    );
    IF v_io IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_unavailable');
    END IF;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'mode', p_mode,
        'scope_id', v_member.operation_id,
        'scope_token', p_lease_token,
        'output_id', v_output.id,
        'storage_ref', v_output.storage_ref,
        'valid_until', (v_attempt ->> 'lease_expires_at')::timestamptz,
        'url_expiry_seconds', v_target.url_expiry_seconds,
        'members', v_members,
        'destination', v_io -> 'destination',
        'host_allowlist', v_io -> 'host_allowlist'
    ), NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.capture_native_pyramid_write_io(uuid, uuid, bigint, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.capture_native_pyramid_read_io(
    p_operation_id uuid, p_token uuid, p_relative_keys jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_permit aap_assets_v1.native_pyramid_read_permits%ROWTYPE;
    v_source_reply jsonb;
    v_source jsonb;
    v_projection jsonb;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_target aap_assets_v1.native_pyramid_retirement_targets%ROWTYPE;
    v_ref jsonb;
    v_gate record;
    v_keys text[];
    v_members jsonb;
    v_io jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_token IS NULL
       OR p_relative_keys IS NULL
       OR pg_catalog.jsonb_typeof(p_relative_keys) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_relative_keys) NOT BETWEEN 1 AND 100
       OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_relative_keys) AS entry(value)
            WHERE pg_catalog.jsonb_typeof(entry.value) IS DISTINCT FROM 'string'
       ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    SELECT pg_catalog.array_agg(value ORDER BY ordinality)
    INTO v_keys
    FROM pg_catalog.jsonb_array_elements_text(p_relative_keys)
         WITH ORDINALITY AS entry(value, ordinality);
    IF pg_catalog.cardinality(v_keys) <> (
        SELECT pg_catalog.count(DISTINCT value) FROM pg_catalog.unnest(v_keys) AS key(value)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.unnest(v_keys) AS key(value)
        WHERE pg_catalog.char_length(key.value) NOT BETWEEN 1 AND 2048
    ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT permit.* INTO v_permit
        FROM aap_assets_v1.native_pyramid_read_permits AS permit
        WHERE permit.operation_id = p_operation_id
          AND permit.token = p_token
        FOR SHARE;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
    END IF;
    IF v_permit.state <> 'open' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    IF v_permit.fixture_owned IS DISTINCT FROM false THEN
        RETURN aap_assets_v1._image_reply(NULL, 'permission_denied');
    END IF;
    v_source_reply := aap_assets_v1._image_owner_source(
        v_permit.actor_id, v_permit.task_id, v_permit.dataset_item_id, false
    );
    IF v_source_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_source_reply;
    END IF;
    v_source := v_source_reply -> 'data';
    IF NOT aap_assets_v1._image_source_data_valid(v_source)
       OR (v_source ->> 'source_generation')::bigint
            IS DISTINCT FROM v_permit.source_generation
       OR v_source ->> 'source_snapshot' IS DISTINCT FROM v_permit.source_snapshot THEN
        RETURN aap_assets_v1._image_reply(NULL, 'source_changed');
    END IF;
    v_projection := aap_assets_v1._image_pyramid_projection(
        v_source, v_permit.generation, v_permit.profile_version
    );
    IF v_projection IS NULL
       OR v_projection ->> 'status' IS DISTINCT FROM 'ready'
       OR v_projection ->> 'generation' IS DISTINCT FROM v_permit.generation::text
       OR v_projection ->> 'native' IS DISTINCT FROM 'true'
       OR pg_catalog.jsonb_typeof(v_projection -> 'native_output')
            IS DISTINCT FROM 'object'
       OR (v_projection #>> '{native_output,output_id}')
            IS DISTINCT FROM v_permit.output_id::text
       OR (v_projection #> '{native_output,storage_ref}')
            IS DISTINCT FROM v_permit.storage_ref
       OR (v_projection #>> '{native_output,url_expiry_seconds}')::integer
            IS DISTINCT FROM v_permit.url_expiry_seconds
       OR (v_projection #>> '{native_output,fixture_owned}')
            IS DISTINCT FROM 'false' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    BEGIN
        SELECT output.* INTO v_output
        FROM public.image_pyramid_outputs AS output
        WHERE output.id = v_permit.output_id
        FOR SHARE;
        SELECT target.* INTO v_target
        FROM aap_assets_v1.native_pyramid_retirement_targets AS target
        WHERE target.output_id = v_permit.output_id
        FOR SHARE;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND OR v_output.state IS DISTINCT FROM 'ready'
       OR v_output.asset_id IS DISTINCT FROM v_permit.asset_id
       OR v_output.generation IS DISTINCT FROM v_permit.generation
       OR v_output.storage_ref IS DISTINCT FROM v_permit.storage_ref
       OR NOT aap_assets_v1._native_pyramid_retirement_target_valid(
            v_permit.storage_ref, v_permit.output_id
          )
       OR v_target.output_id IS DISTINCT FROM v_permit.output_id
       OR NOT aap_assets_v1._native_pyramid_retirement_policy_valid(v_target.policy)
       OR v_target.url_expiry_seconds IS DISTINCT FROM v_permit.url_expiry_seconds THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_ref := pg_catalog.jsonb_build_object(
        'schema', 2,
        'purpose', 'media-cache',
        'location_id', v_target.location_id::text,
        'config_revision', v_target.config_revision,
        'binding_generation', v_target.binding_generation,
        'endpoint_url', v_target.endpoint_url,
        'bucket', v_target.bucket,
        'key_prefix', v_target.key_prefix,
        'prefix', v_target.prefix,
        'storage_identity', v_target.storage_identity
    );
    IF v_ref IS DISTINCT FROM v_output.storage_ref THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    SELECT gate.state, gate.revision INTO v_gate
    FROM public.storage_maintenance_gates AS gate
    WHERE gate.location_id = v_target.location_id
    FOR SHARE;
    IF NOT FOUND OR v_gate.state <> 'active'
       OR v_gate.revision IS DISTINCT FROM v_permit.gate_revision THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_unavailable');
    END IF;
    SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'operation_id', member.operation_id,
        'relative_key', member.relative_key,
        'sha256', member.sha256,
        'size_bytes', member.size_bytes,
        'etag', member.etag,
        'version_id', member.version_id
    ) ORDER BY requested.ordinality)
    INTO v_members
    FROM pg_catalog.unnest(v_keys) WITH ORDINALITY AS requested(relative_key, ordinality)
    JOIN aap_assets_v1.image_pyramid_native_members AS member
      ON member.output_id = v_permit.output_id
     AND member.relative_key = requested.relative_key
     AND member.state = 'verified'
    WHERE member.etag IS NOT NULL
      AND member.etag ~ '^"[^"]+"$'
      AND pg_catalog.length(member.etag) <= 1024
      AND (member.version_id IS NULL
           OR pg_catalog.length(member.version_id) <= 1024);
    IF v_members IS NULL
       OR pg_catalog.jsonb_array_length(v_members) <> pg_catalog.cardinality(v_keys) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'asset_missing');
    END IF;
    v_io := aap_assets_v1._native_pyramid_target_destination(
        v_target.location_id, v_target.endpoint_url, v_target.bucket,
        v_target.storage_identity
    );
    IF v_io IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_unavailable');
    END IF;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'mode', 'read',
        'scope_id', v_permit.operation_id,
        'scope_token', v_permit.token,
        'output_id', v_permit.output_id,
        'storage_ref', v_permit.storage_ref,
        'valid_until', NULL,
        'url_expiry_seconds', v_permit.url_expiry_seconds,
        'members', v_members,
        'destination', v_io -> 'destination',
        'host_allowlist', v_io -> 'host_allowlist'
    ), NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.capture_native_pyramid_read_io(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.capture_native_pyramid_retirement_io(
    p_output_id uuid, p_lease_token uuid, p_page_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_retirement aap_assets_v1.native_pyramid_retirements%ROWTYPE;
    v_target aap_assets_v1.native_pyramid_retirement_targets%ROWTYPE;
    v_output public.image_pyramid_outputs%ROWTYPE;
    v_page aap_assets_v1.native_pyramid_retirement_pages%ROWTYPE;
    v_permit record;
    v_ref jsonb;
    v_members jsonb;
    v_io jsonb;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_output_id IS NULL OR p_lease_token IS NULL OR p_page_id IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_request');
    END IF;
    BEGIN
        SELECT retirement.* INTO v_retirement
        FROM aap_assets_v1.native_pyramid_retirements AS retirement
        WHERE retirement.output_id = p_output_id
        FOR SHARE;
        IF NOT FOUND OR v_retirement.closed_at IS NOT NULL
           OR v_retirement.lease_token IS DISTINCT FROM p_lease_token
           OR v_retirement.lease_expires_at IS NULL
           OR v_retirement.lease_expires_at <= v_now THEN
            RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
        END IF;
        SELECT target.* INTO v_target
        FROM aap_assets_v1.native_pyramid_retirement_targets AS target
        WHERE target.output_id = p_output_id
        FOR SHARE;
        SELECT output.* INTO v_output
        FROM public.image_pyramid_outputs AS output
        WHERE output.id = p_output_id
        FOR SHARE;
        SELECT page.* INTO v_page
        FROM aap_assets_v1.native_pyramid_retirement_pages AS page
        WHERE page.page_id = p_page_id
        FOR SHARE;
    EXCEPTION WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    END;
    IF NOT FOUND
       OR v_page.output_id IS DISTINCT FROM p_output_id
       OR v_page.lease_token IS DISTINCT FROM p_lease_token
       OR v_page.state <> 'prepared'
       OR v_output.state IS DISTINCT FROM 'retiring' THEN
        RETURN aap_assets_v1._image_reply(NULL, 'lease_lost');
    END IF;
    IF aap_assets_v1._native_pyramid_output_fixture_owned(p_output_id) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'permission_denied');
    END IF;
    IF v_target.output_id IS DISTINCT FROM p_output_id
       OR v_target.asset_id IS DISTINCT FROM v_output.asset_id
       OR v_target.generation IS DISTINCT FROM v_output.generation
       OR NOT aap_assets_v1._native_pyramid_retirement_target_valid(
            v_output.storage_ref, p_output_id
          )
       OR NOT aap_assets_v1._native_pyramid_retirement_policy_valid(v_target.policy) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    v_ref := pg_catalog.jsonb_build_object(
        'schema', 2,
        'purpose', 'media-cache',
        'location_id', v_target.location_id::text,
        'config_revision', v_target.config_revision,
        'binding_generation', v_target.binding_generation,
        'endpoint_url', v_target.endpoint_url,
        'bucket', v_target.bucket,
        'key_prefix', v_target.key_prefix,
        'prefix', v_target.prefix,
        'storage_identity', v_target.storage_identity
    );
    IF v_ref IS DISTINCT FROM v_output.storage_ref THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    SELECT gate.state, gate.revision INTO v_permit
    FROM public.storage_maintenance_gates AS gate
    WHERE gate.location_id = v_target.location_id
    FOR SHARE;
    IF NOT FOUND OR v_permit.state <> 'active'
       OR v_permit.revision IS DISTINCT FROM v_page.gate_revision THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_unavailable');
    END IF;
    SELECT operation.location_id, operation.revision, operation.purpose
    INTO v_permit
    FROM public.storage_operations AS operation
    WHERE operation.id = p_page_id
    FOR SHARE;
    IF NOT FOUND
       OR v_permit.location_id IS DISTINCT FROM v_target.location_id
       OR v_permit.revision IS DISTINCT FROM v_page.gate_revision
       OR v_permit.purpose IS DISTINCT FROM (
            'image_pyramid_retirement_'
                || pg_catalog.replace(p_output_id::text, '-', '')
       ) THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
    END IF;
    IF v_page.terminal THEN
        IF pg_catalog.cardinality(v_page.member_ids) <> 0 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_members := '[]'::jsonb;
    ELSE
        IF pg_catalog.cardinality(v_page.member_ids) = 0 THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
        v_members := aap_assets_v1._native_pyramid_retirement_page_items(
            p_output_id, v_page.member_ids
        );
        IF v_members IS NULL
           OR pg_catalog.jsonb_array_length(v_members)
                <> pg_catalog.cardinality(v_page.member_ids) THEN
            RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
        END IF;
    END IF;
    v_io := aap_assets_v1._native_pyramid_target_destination(
        v_target.location_id, v_target.endpoint_url, v_target.bucket,
        v_target.storage_identity
    );
    IF v_io IS NULL THEN
        RETURN aap_assets_v1._image_reply(NULL, 'storage_unavailable');
    END IF;
    RETURN aap_assets_v1._image_reply(pg_catalog.jsonb_build_object(
        'mode', CASE WHEN v_page.terminal THEN 'retirement_prefix'
            ELSE 'retirement_members' END,
        'scope_id', v_page.page_id,
        'scope_token', p_lease_token,
        'output_id', p_output_id,
        'storage_ref', v_output.storage_ref,
        'valid_until', v_retirement.lease_expires_at,
        'url_expiry_seconds', v_target.url_expiry_seconds,
        'members', v_members,
        'destination', v_io -> 'destination',
        'host_allowlist', v_io -> 'host_allowlist'
    ), NULL);
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._image_reply(NULL, 'operation_busy');
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN aap_assets_v1._image_reply(NULL, 'invalid_response');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.capture_native_pyramid_retirement_io(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
DO $owner$
DECLARE
    v_owner name;
    v_signature text;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(proowner) INTO STRICT v_owner
    FROM pg_catalog.pg_proc
    WHERE oid =
          'aap_assets_v1.read_image_generation(uuid,uuid,uuid,integer,text)'::regprocedure;
    FOREACH v_signature IN ARRAY ARRAY[
        '_native_pyramid_target_destination(uuid,text,text,text)',
        'capture_native_pyramid_write_io(uuid,uuid,bigint,uuid,text)',
        'capture_native_pyramid_read_io(uuid,uuid,jsonb)',
        'capture_native_pyramid_retirement_io(uuid,uuid,uuid)'
    ] LOOP
        EXECUTE pg_catalog.format(
            'ALTER FUNCTION aap_assets_v1.%s OWNER TO %I', v_signature, v_owner
        );
    END LOOP;
END;
$owner$;
