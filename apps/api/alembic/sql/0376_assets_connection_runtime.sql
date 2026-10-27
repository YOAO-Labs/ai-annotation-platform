-- Versioned owner capabilities for the standalone Assets Source connection
-- runtime. The portable owner never reads Identity core rows: it re-checks an
-- accepted API key through the published narrow Identity capability, and owns
-- the storage_operations ledger for the physical probe until the worker proves
-- its blocking transport has settled.

CREATE OR REPLACE FUNCTION aap_assets_v1._connection_probe_reply(
    p_data jsonb,
    p_code text
)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN p_code IS NULL
        THEN pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-connection-probe/v1',
            'ok', true, 'data', p_data
        )
        ELSE pg_catalog.jsonb_build_object(
            'schema', 'aap-assets-connection-probe/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', p_code)
        )
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._connection_probe_reply(jsonb, text) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1._connection_authority_proof(
    p_actor_id uuid,
    p_api_key_id uuid,
    p_required_scopes text[],
    p_expected jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_scopes text[];
    v_expires_at timestamptz;
    v_revision text;
    v_proof jsonb;
BEGIN
    IF p_api_key_id IS NULL THEN
        IF p_expected IS NOT NULL THEN
            RETURN aap_assets_v1._connection_probe_reply(NULL, 'invalid_request');
        END IF;
        RETURN aap_assets_v1._connection_probe_reply(
            pg_catalog.jsonb_build_object('proof', NULL), NULL
        );
    END IF;
    IF p_required_scopes IS NULL
       OR pg_catalog.array_position(p_required_scopes, NULL) IS NOT NULL
       OR EXISTS (
           SELECT 1 FROM pg_catalog.unnest(p_required_scopes) AS scope(value)
           WHERE pg_catalog.char_length(scope.value) NOT BETWEEN 1 AND 128
       ) THEN
        RETURN aap_assets_v1._connection_probe_reply(NULL, 'invalid_request');
    END IF;
    SELECT facts.scopes, facts.expires_at, facts.revision
    INTO v_scopes, v_expires_at, v_revision
    FROM aap_identity_v1.read_accepted_api_key_facts(
        p_actor_id, p_api_key_id
    ) AS facts;
    IF NOT FOUND
       OR NOT (
           '*' = ANY (v_scopes)
           OR p_required_scopes <@ v_scopes
       ) THEN
        RETURN aap_assets_v1._connection_probe_reply(NULL, 'api_key_authority_revoked');
    END IF;
    v_proof := pg_catalog.jsonb_build_object(
        'scopes', pg_catalog.to_jsonb(v_scopes),
        'expires_at', v_expires_at,
        'revision', v_revision
    );
    IF p_expected IS NOT NULL THEN
        IF pg_catalog.jsonb_typeof(p_expected) IS DISTINCT FROM 'object'
           OR (p_expected - ARRAY['scopes', 'expires_at', 'revision']) <> '{}'::jsonb
           OR NOT (p_expected ?& ARRAY['scopes', 'expires_at', 'revision'])
           OR p_expected -> 'scopes' IS DISTINCT FROM pg_catalog.to_jsonb(v_scopes)
           OR p_expected ->> 'revision' IS DISTINCT FROM v_revision THEN
            RETURN aap_assets_v1._connection_probe_reply(
                NULL, 'api_key_authority_revoked'
            );
        END IF;
        IF (p_expected -> 'expires_at') = 'null'::jsonb THEN
            IF v_expires_at IS NOT NULL THEN
                RETURN aap_assets_v1._connection_probe_reply(
                    NULL, 'api_key_authority_revoked'
                );
            END IF;
        ELSIF v_expires_at IS NULL
           OR (p_expected ->> 'expires_at')::timestamptz IS DISTINCT FROM v_expires_at THEN
            RETURN aap_assets_v1._connection_probe_reply(
                NULL, 'api_key_authority_revoked'
            );
        END IF;
    END IF;
    RETURN aap_assets_v1._connection_probe_reply(
        pg_catalog.jsonb_build_object('proof', v_proof), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._connection_authority_proof(uuid, uuid, text[], jsonb) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.begin_connection_probe(
    p_connection_id uuid,
    p_actor_id uuid,
    p_api_key_id uuid,
    p_required_scopes text[]
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_location_id uuid;
    v_revision integer;
    v_operation_id uuid := pg_catalog.gen_random_uuid();
    v_authority jsonb;
BEGIN
    IF p_connection_id IS NULL OR p_actor_id IS NULL THEN
        RETURN aap_assets_v1._connection_probe_reply(NULL, 'invalid_request');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.storage_connections AS connection
        WHERE connection.id = p_connection_id
    ) THEN
        RETURN aap_assets_v1._connection_probe_reply(NULL, 'connection_missing');
    END IF;
    v_authority := aap_assets_v1._connection_authority_proof(
        p_actor_id, p_api_key_id, p_required_scopes, NULL
    );
    IF v_authority -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_authority;
    END IF;
    v_location_id := aap_assets_v1._connector_location_id(p_connection_id);
    BEGIN
        v_revision := aap_assets_v1._managed_original_gate_revision(v_location_id);
    EXCEPTION WHEN SQLSTATE '55006' THEN
        RETURN aap_assets_v1._connection_probe_reply(NULL, 'storage_maintenance');
    END;
    INSERT INTO public.storage_operations (
        id, location_id, revision, purpose, node_id
    ) VALUES (
        v_operation_id, v_location_id, v_revision,
        'connection_probe', 'aap-assets-connection-runtime'
    );
    RETURN aap_assets_v1._connection_probe_reply(
        pg_catalog.jsonb_build_object(
            'operation_id', v_operation_id,
            'connection_id', p_connection_id,
            'location_id', v_location_id,
            'revision', v_revision,
            'proof', v_authority #> '{data,proof}'
        ), NULL
    );
EXCEPTION
    WHEN unique_violation THEN
        RETURN aap_assets_v1._connection_probe_reply(NULL, 'operation_conflict');
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_assets_v1._connection_probe_reply(NULL, 'connection_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_connection_probe(uuid, uuid, uuid, text[]) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.finish_connection_probe(
    p_operation_id uuid,
    p_connection_id uuid,
    p_actor_id uuid,
    p_api_key_id uuid,
    p_required_scopes text[],
    p_expected jsonb,
    p_settled boolean
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation public.storage_operations%ROWTYPE;
    v_revision integer;
    v_authority jsonb;
    v_retained boolean := false;
BEGIN
    IF p_operation_id IS NULL OR p_connection_id IS NULL OR p_actor_id IS NULL
       OR p_settled IS NULL THEN
        RETURN aap_assets_v1._connection_probe_reply(NULL, 'invalid_request');
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.storage_operations AS operation
    WHERE operation.id = p_operation_id
      AND operation.location_id = aap_assets_v1._connector_location_id(p_connection_id)
      AND operation.purpose = 'connection_probe'
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN aap_assets_v1._connection_probe_reply(NULL, 'operation_missing');
    END IF;
    -- storage_access semantics: a settled physical attempt releases its own
    -- receipt even when the business result is rejected afterwards. Only an
    -- unsettled transport retains the row.
    IF p_settled THEN
        DELETE FROM public.storage_operations AS operation
        WHERE operation.id = p_operation_id;
    ELSE
        v_retained := true;
    END IF;
    v_authority := aap_assets_v1._connection_authority_proof(
        p_actor_id, p_api_key_id, p_required_scopes, p_expected
    );
    IF v_authority -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN v_authority;
    END IF;
    BEGIN
        v_revision := aap_assets_v1._managed_original_gate_revision(
            v_operation.location_id
        );
    EXCEPTION WHEN SQLSTATE '55006' THEN
        RETURN aap_assets_v1._connection_probe_reply(NULL, 'storage_maintenance');
    END;
    IF v_revision IS DISTINCT FROM v_operation.revision THEN
        RETURN aap_assets_v1._connection_probe_reply(NULL, 'storage_maintenance_changed');
    END IF;
    RETURN aap_assets_v1._connection_probe_reply(
        pg_catalog.jsonb_build_object(
            'settled', p_settled,
            'retained', v_retained,
            'proof', v_authority #> '{data,proof}'
        ), NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_connection_probe(uuid, uuid, uuid, uuid, text[], jsonb, boolean) FROM PUBLIC;
-- aap-statement-break

CREATE OR REPLACE FUNCTION aap_assets_v1.update_storage_connection_authorized(
    operation_id uuid,
    actor_id uuid,
    connection_id uuid,
    intent jsonb,
    config jsonb,
    secret_enc text,
    source_snapshot text,
    policy_digest text,
    fallback_allowlist jsonb,
    audit_context jsonb,
    api_key_id uuid,
    required_scopes text[],
    expected_proof jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_authority jsonb;
BEGIN
    -- The accepted-key fence and the published update share this one
    -- transaction: a rotation cannot slip between proof read and publication.
    IF api_key_id IS NOT NULL AND expected_proof IS NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    IF api_key_id IS NULL AND expected_proof IS NOT NULL THEN
        RETURN aap_assets_v1._storage_connection_reply(NULL, 'invalid_request');
    END IF;
    v_authority := aap_assets_v1._connection_authority_proof(
        actor_id, api_key_id, required_scopes, expected_proof
    );
    IF v_authority -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._storage_connection_reply(
            NULL, v_authority #>> '{error,code}'
        );
    END IF;
    RETURN aap_assets_v1.update_storage_connection(
        operation_id, actor_id, connection_id, intent, config, secret_enc,
        source_snapshot, policy_digest, fallback_allowlist, audit_context
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.update_storage_connection_authorized(
    uuid, uuid, uuid, jsonb, jsonb, text, text, text, jsonb, jsonb,
    uuid, text[], jsonb
) FROM PUBLIC;
-- aap-statement-break
