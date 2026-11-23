CREATE TABLE yoao_assets_v1.saved_binding_probe (
    operation_id uuid PRIMARY KEY REFERENCES public.storage_operations(id) ON DELETE CASCADE,
    actor_id uuid NOT NULL,
    api_key_id uuid,
    key_revision text,
    purpose text NOT NULL,
    old_location_id uuid NOT NULL,
    binding_generation integer NOT NULL,
    location_id uuid NOT NULL,
    config_revision integer NOT NULL,
    connection_revision integer NOT NULL,
    expires_at timestamptz NOT NULL,
    attempt_id uuid,
    CHECK ((api_key_id IS NULL) = (key_revision IS NULL))
);
-- aap-statement-break
REVOKE ALL ON TABLE yoao_assets_v1.saved_binding_probe FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.saved_io_allowlist(p_fallback jsonb)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT CASE WHEN policy->>'ok' = 'true' THEN policy #> '{data,entries}' END
    FROM yoao_settings_v1.resolve_connector_host_allowlist(p_fallback) AS resolved(policy);
$body$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.saved_io_allowlist(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.saved_io_connector(
    p_connection_id uuid, p_connection_revision integer, p_fallback jsonb
)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT jsonb_build_object('kind', c.kind, 'config', c.config,
        'ciphertext', encode(c.secret_enc, 'hex'),
        'allowlist', yoao_assets_v1.saved_io_allowlist(p_fallback))
    FROM public.storage_connections c
    WHERE c.id = p_connection_id AND c.revision = p_connection_revision
      AND c.kind IN ('s3', 'sftp') AND octet_length(c.secret_enc) > 0;
$body$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.saved_io_connector(uuid,integer,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.saved_io_target(
    p_location_id uuid, p_config_revision integer, p_connection_revision integer, p_fallback jsonb
)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT yoao_assets_v1.saved_io_connector(c.id, p_connection_revision, p_fallback)
    FROM public.storage_locations l
    JOIN public.storage_connections c ON c.id = l.connection_id
    WHERE l.id = p_location_id AND l.config_revision = p_config_revision
      AND l.kind = 's3' AND c.kind = 's3' AND c.scope = 'global' AND c.project_id IS NULL
      AND c.revision = p_connection_revision;
$body$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.saved_io_target(uuid,integer,integer,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._saved_binding_probe_authority(
    p_actor_id uuid, p_key_id uuid, p_key_revision text
)
RETURNS boolean LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, pg_temp
AS $body$
BEGIN
    IF NOT EXISTS (
        SELECT FROM yoao_identity_v1.read_actor_facts(p_actor_id) AS actor
        WHERE actor.platform_role = 'super_admin' AND actor.is_active
    ) THEN
        RETURN false;
    END IF;
    IF p_key_id IS NULL THEN
        RETURN p_key_revision IS NULL;
    END IF;
    RETURN EXISTS (
        SELECT FROM yoao_identity_v1.read_accepted_api_key_facts(p_actor_id, p_key_id) AS key
        WHERE key.revision = p_key_revision
          AND ('*' = ANY(key.scopes) OR 'datasets:write' = ANY(key.scopes))
    );
END;
$body$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._saved_binding_probe_authority(uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._saved_binding_probe_facts(
    p_operation_id uuid, p_purpose text, p_location_id uuid,
    p_config_revision integer, p_connection_revision integer, p_generation integer
)
RETURNS jsonb LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp
AS $body$
    SELECT jsonb_build_object('location_id', l.id, 'config_revision', l.config_revision,
        'connection_revision', c.revision, 'purpose', b.purpose, 'old_location_id', b.location_id,
        'bucket', c.config->>'bucket', 'prefix',
        CASE WHEN coalesce(c.config->>'base_prefix', '') IN ('', '/') THEN ''
            ELSE regexp_replace(c.config->>'base_prefix', '/$', '') || '/' END
        || CASE WHEN b.purpose = 'export' THEN '_yoao_managed/' ELSE '_aap_managed/' END
        || b.purpose || '/')
    FROM public.storage_operations o
    JOIN public.storage_maintenance_gates g ON g.location_id = o.location_id
    JOIN public.storage_locations l ON l.id = o.location_id
    JOIN public.storage_connections c ON c.id = l.connection_id
    JOIN public.storage_bindings b ON b.purpose = p_purpose
    WHERE o.id = p_operation_id AND o.location_id = p_location_id
      AND o.purpose = CASE p_purpose
        WHEN 'avatars' THEN 'avatar_binding_probe'
        WHEN 'bug-reports' THEN 'bug_attachment_binding_probe'
        WHEN 'audit-archive' THEN 'audit_archive_binding_probe'
        WHEN 'export' THEN 'export_binding_probe'
        WHEN 'import' THEN 'import_binding_probe'
        WHEN 'annotations' THEN 'annotations_binding_probe'
        WHEN 'datasets' THEN 'dataset_binding_probe'
        WHEN 'media-cache' THEN 'media_cache_binding_probe'
        WHEN 'training' THEN 'training_binding_probe'
      END
      AND g.state = 'active' AND g.revision = o.revision
      AND l.enabled AND l.kind = 's3' AND l.config_revision = p_config_revision
      AND c.kind = 's3' AND c.scope = 'global' AND c.project_id IS NULL
      AND c.revision = p_connection_revision AND octet_length(c.secret_enc) > 0
      AND b.generation = p_generation AND b.location_id <> p_location_id;
$body$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._saved_binding_probe_facts(uuid,text,uuid,integer,integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.begin_saved_binding_probe(
    p_operation_id uuid, p_actor_id uuid, p_api_key_id uuid, p_key_revision text,
    p_purpose text, p_location_id uuid, p_config_revision integer,
    p_connection_revision integer, p_generation integer
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    facts jsonb;
BEGIN
    IF NOT yoao_assets_v1._saved_binding_probe_authority(p_actor_id, p_api_key_id, p_key_revision) THEN
        RETURN false;
    END IF;
    facts := yoao_assets_v1._saved_binding_probe_facts(p_operation_id, p_purpose,
        p_location_id, p_config_revision, p_connection_revision, p_generation);
    IF facts IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO yoao_assets_v1.saved_binding_probe (
        operation_id, actor_id, api_key_id, key_revision, purpose, old_location_id,
        binding_generation, location_id, config_revision, connection_revision, expires_at
    ) VALUES (
        p_operation_id, p_actor_id, p_api_key_id, p_key_revision, p_purpose,
        (facts->>'old_location_id')::uuid, p_generation, p_location_id,
        p_config_revision, p_connection_revision, clock_timestamp() + interval '5 minutes'
    ) ON CONFLICT (operation_id) DO NOTHING;
    RETURN FOUND;
END;
$body$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.begin_saved_binding_probe(uuid,uuid,uuid,text,text,uuid,integer,integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.capture_saved_binding_probe(
    p_operation_id uuid, p_attempt_id uuid, p_admit boolean
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $body$
DECLARE
    intent yoao_assets_v1.saved_binding_probe%ROWTYPE;
    facts jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_attempt_id IS NULL OR p_admit IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT * INTO intent FROM yoao_assets_v1.saved_binding_probe
    WHERE operation_id = p_operation_id FOR UPDATE NOWAIT;
    IF NOT FOUND OR intent.expires_at <= clock_timestamp()
        OR (p_admit AND intent.attempt_id IS NOT NULL)
        OR (NOT p_admit AND intent.attempt_id IS DISTINCT FROM p_attempt_id)
        OR NOT yoao_assets_v1._saved_binding_probe_authority(intent.actor_id, intent.api_key_id, intent.key_revision) THEN
        RETURN NULL;
    END IF;
    facts := yoao_assets_v1._saved_binding_probe_facts(p_operation_id, intent.purpose,
        intent.location_id, intent.config_revision, intent.connection_revision, intent.binding_generation);
    IF facts IS NULL OR (facts->>'old_location_id')::uuid IS DISTINCT FROM intent.old_location_id THEN
        RETURN NULL;
    END IF;
    IF p_admit THEN
        UPDATE yoao_assets_v1.saved_binding_probe SET attempt_id = p_attempt_id
        WHERE operation_id = p_operation_id;
    END IF;
    RETURN facts;
END;
$body$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.capture_saved_binding_probe(uuid,uuid,boolean) FROM PUBLIC;
