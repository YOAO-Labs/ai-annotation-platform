CREATE SCHEMA yoao_assets_storage_v1;
REVOKE ALL ON SCHEMA yoao_assets_storage_v1 FROM PUBLIC;

CREATE FUNCTION yoao_assets_storage_v1.initialize_gate(p_location uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET lock_timeout = '1s'
AS $fn$
BEGIN
    IF NOT EXISTS (SELECT FROM public.storage_maintenance_gates WHERE location_id = p_location) THEN
        INSERT INTO public.storage_maintenance_gates (location_id) VALUES (p_location)
        ON CONFLICT DO NOTHING;
    END IF;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.gate_facts(p_location uuid, p_lock text)
RETURNS TABLE(state text, revision integer, changed_at timestamptz, changed_by uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF p_lock IS NULL THEN
        RETURN QUERY SELECT g.state::text, g.revision, g.changed_at, g.changed_by
            FROM public.storage_maintenance_gates g WHERE g.location_id = p_location;
    ELSIF p_lock = 'share_nowait' THEN
        RETURN QUERY SELECT g.state::text, g.revision, g.changed_at, g.changed_by
            FROM public.storage_maintenance_gates g WHERE g.location_id = p_location FOR SHARE NOWAIT;
    ELSIF p_lock = 'update_nowait' THEN
        RETURN QUERY SELECT g.state::text, g.revision, g.changed_at, g.changed_by
            FROM public.storage_maintenance_gates g WHERE g.location_id = p_location FOR UPDATE NOWAIT;
    ELSE
        RAISE invalid_parameter_value;
    END IF;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.begin_physical_io(
    p_operation uuid, p_location uuid, p_purpose text, p_node text, p_revision integer
) RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE g record;
BEGIN
    IF p_purpose IS NULL OR p_purpose !~ '^[a-z][a-z0-9_]{0,63}$'
       OR p_node IS NULL OR p_node !~ '^[A-Za-z0-9_.:@-]{1,128}$' THEN
        RAISE invalid_parameter_value;
    END IF;
    SELECT * INTO STRICT g FROM yoao_assets_storage_v1.gate_facts(p_location, 'share_nowait');
    IF p_revision IS NOT NULL AND g.revision <> p_revision THEN
        RAISE EXCEPTION USING ERRCODE = 'YS001', MESSAGE = 'storage_maintenance_changed';
    END IF;
    IF g.state <> 'active' THEN
        RAISE EXCEPTION USING ERRCODE = 'YS002', MESSAGE = 'storage_maintenance';
    END IF;
    INSERT INTO public.storage_operations (id, location_id, revision, purpose, node_id)
    VALUES (p_operation, p_location, g.revision, p_purpose, p_node);
    RETURN g.revision;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.settle_physical_io(p_operation uuid, p_location uuid, p_revision integer)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    WITH settled AS (
        DELETE FROM public.storage_operations o
        WHERE o.id = p_operation AND o.location_id = p_location AND o.revision = p_revision
          AND NOT EXISTS (SELECT FROM public.site_agent_commands c WHERE c.operation_id = o.id
              AND c.closed IS NOT TRUE AND (c.lease_session_id IS NOT NULL OR c.state = 'queued'))
        RETURNING o.id
    )
    SELECT EXISTS (SELECT FROM settled) OR NOT EXISTS (
        SELECT FROM public.storage_operations o WHERE o.id = p_operation);
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.physical_io_is_open(p_purpose text, p_location uuid, p_excluding uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT EXISTS (
        SELECT FROM public.storage_operations AS o
        WHERE o.purpose = p_purpose
          AND (p_location IS NULL OR o.location_id = p_location)
          AND o.id IS DISTINCT FROM p_excluding
    );
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.physical_io_receipt(p_operation uuid)
RETURNS TABLE(id uuid, location_id uuid, revision integer, purpose text, node_id text, started_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT o.id, o.location_id, o.revision, o.purpose::text, o.node_id::text, o.started_at
    FROM public.storage_operations AS o WHERE o.id = p_operation;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.maintenance_status(p_location uuid)
RETURNS TABLE(state text, revision integer, changed_at timestamptz, changed_by uuid,
    measured_at timestamptz, open_operations bigint, operations jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT coalesce(g.state, 'active')::text, coalesce(g.revision, 1), g.changed_at, g.changed_by,
        statement_timestamp(),
        (SELECT count(*) FROM public.storage_operations AS o WHERE o.location_id = p_location),
        (SELECT coalesce(jsonb_agg(to_jsonb(p) ORDER BY p.started_at,p.id), '[]'::jsonb)
         FROM (SELECT o.id,o.purpose,o.node_id,o.revision,o.started_at FROM public.storage_operations AS o
               WHERE o.location_id = p_location ORDER BY o.started_at,o.id LIMIT 50) p)
    FROM (SELECT p_location AS id) a
    LEFT JOIN public.storage_maintenance_gates AS g ON g.location_id = a.id;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.transition_gate(p_location uuid, p_revision integer, p_operation text, p_actor uuid)
RETURNS TABLE(state text, revision integer, changed_at timestamptz, changed_by uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    UPDATE public.storage_maintenance_gates AS g
    SET state = CASE p_operation WHEN 'drain' THEN 'draining' WHEN 'stop' THEN 'stopped' ELSE 'active' END,
        revision = g.revision + 1, changed_at = statement_timestamp(), changed_by = p_actor
    WHERE g.location_id = p_location AND g.revision = p_revision
      AND ((p_operation = 'drain' AND g.state = 'active')
        OR (p_operation = 'stop' AND g.state = 'draining' AND NOT EXISTS (
            SELECT FROM public.storage_operations AS o WHERE o.location_id = p_location
        )) OR (p_operation = 'resume' AND g.state IN ('draining','stopped')))
    RETURNING g.state::text, g.revision, g.changed_at, g.changed_by;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.checkpoint_media_native_stage(
    p_operation uuid, p_location uuid, p_revision integer, p_stage jsonb
) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    WITH changed AS (
        UPDATE public.storage_operations SET native_stage = p_stage
        WHERE id = p_operation AND location_id = p_location AND revision = p_revision
          AND p_stage IS NOT NULL
        RETURNING id
    ) SELECT EXISTS (SELECT FROM changed);
$fn$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_assets_storage_v1 FROM PUBLIC;

CREATE FUNCTION yoao_assets_storage_v1._location_config(p_config jsonb)
RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT coalesce(jsonb_object_agg(f.key, f.value), '{}'::jsonb)
    FROM jsonb_each(p_config) f
    WHERE f.key IN ('endpoint','use_ssl','root_handle','relative_path','scope','use_user_ids',
        'cache_policy','original_cache_quota_bytes','original_cache_ttl_seconds','registration_node',
        'registration_anchor','agent_id','site_id','root_revision','agent_anchor');
$fn$;

CREATE FUNCTION yoao_assets_storage_v1._connector_origin(p_kind text, p_config jsonb)
RETURNS jsonb LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT coalesce(jsonb_object_agg(f.key, f.value), '{}'::jsonb)
    FROM jsonb_each(p_config) f
    WHERE (p_kind = 's3' AND f.key IN ('endpoint','bucket','region','base_prefix','use_ssl')
            AND (f.key <> 'endpoint' OR p_config ->> 'endpoint' !~ '[@?#]'))
       OR (p_kind = 'sftp' AND f.key IN ('host','port','username','base_path','auth_type'));
$fn$;

CREATE VIEW yoao_assets_storage_v1.location_metadata AS
SELECT l.id, l.name, l.kind::text AS kind, l.config_revision,
    yoao_assets_storage_v1._location_config(l.config) AS config, l.connection_id, l.enabled
FROM public.storage_locations l OFFSET 0;

CREATE VIEW yoao_assets_storage_v1.connector_origins AS
SELECT c.id, c.name, c.kind::text AS kind, c.revision, c.scope::text AS scope, c.project_id, c.created_by,
    yoao_assets_storage_v1._connector_origin(c.kind::text, c.config) AS config,
    coalesce(octet_length(c.secret_enc) > 0, false) AS secret_set
FROM public.storage_connections c OFFSET 0;

CREATE VIEW yoao_assets_storage_v1.purpose_bindings AS
SELECT b.purpose::text AS purpose, b.location_id, b.bucket, b.generation
FROM public.storage_bindings b OFFSET 0;

CREATE FUNCTION yoao_assets_storage_v1.location_facts(
    p_location uuid, p_lock text, p_actor uuid, p_role text, p_strict boolean
) RETURNS SETOF yoao_assets_storage_v1.location_metadata
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF p_actor IS NOT NULL AND NOT coalesce(
        yoao_assets_v1._storage_fault_location_visible(p_actor, p_role, p_location, p_strict), false)
    THEN RETURN; END IF;
    IF p_lock = 'share_nowait' THEN
        PERFORM FROM public.storage_locations WHERE id = p_location FOR SHARE NOWAIT;
    ELSIF p_lock = 'update_nowait' THEN
        PERFORM FROM public.storage_locations WHERE id = p_location FOR UPDATE NOWAIT;
    ELSIF p_lock = 'share' THEN
        PERFORM FROM public.storage_locations WHERE id = p_location FOR SHARE;
    ELSIF p_lock = 'update' THEN
        PERFORM FROM public.storage_locations WHERE id = p_location FOR UPDATE;
    ELSIF p_lock IS NOT NULL THEN
        RAISE invalid_parameter_value;
    END IF;
    RETURN QUERY SELECT m.* FROM yoao_assets_storage_v1.location_metadata m
    WHERE m.id = p_location AND (p_actor IS NULL OR
        yoao_assets_v1._storage_fault_location_visible(p_actor,p_role,m.id,p_strict));
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.connector_origin(p_connection uuid, p_lock text)
RETURNS SETOF yoao_assets_storage_v1.connector_origins
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF p_lock = 'share_nowait' THEN
        PERFORM FROM public.storage_connections WHERE id = p_connection FOR SHARE NOWAIT;
    ELSIF p_lock = 'update_nowait' THEN
        PERFORM FROM public.storage_connections WHERE id = p_connection FOR UPDATE NOWAIT;
    ELSIF p_lock = 'share' THEN
        PERFORM FROM public.storage_connections WHERE id = p_connection FOR SHARE;
    ELSIF p_lock = 'update' THEN
        PERFORM FROM public.storage_connections WHERE id = p_connection FOR UPDATE;
    ELSIF p_lock IS NOT NULL THEN
        RAISE invalid_parameter_value;
    END IF;
    RETURN QUERY SELECT m.* FROM yoao_assets_storage_v1.connector_origins m WHERE m.id = p_connection;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.purpose_binding(p_purpose text, p_lock text)
RETURNS SETOF yoao_assets_storage_v1.purpose_bindings
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF p_lock = 'share_nowait' THEN
        PERFORM FROM public.storage_bindings WHERE purpose = p_purpose FOR SHARE NOWAIT;
    ELSIF p_lock = 'update_nowait' THEN
        PERFORM FROM public.storage_bindings WHERE purpose = p_purpose FOR UPDATE NOWAIT;
    ELSIF p_lock = 'share' THEN
        PERFORM FROM public.storage_bindings WHERE purpose = p_purpose FOR SHARE;
    ELSIF p_lock = 'update' THEN
        PERFORM FROM public.storage_bindings WHERE purpose = p_purpose FOR UPDATE;
    ELSIF p_lock IS NOT NULL THEN
        RAISE invalid_parameter_value;
    END IF;
    RETURN QUERY SELECT m.* FROM yoao_assets_storage_v1.purpose_bindings m WHERE m.purpose = p_purpose;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.location_visible(p_actor uuid,p_role text,p_location uuid)
RETURNS boolean LANGUAGE sql STABLE STRICT SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT yoao_assets_v1._storage_fault_location_visible(p_actor,p_role,p_location,false);
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.create_read_location(p_id uuid,p_name text,p_kind text,p_config jsonb)
RETURNS SETOF yoao_assets_storage_v1.location_metadata
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF p_kind NOT IN ('filesystem','agent') OR p_config ->> 'root_handle' IS NULL THEN
        RAISE invalid_parameter_value;
    END IF;
    INSERT INTO public.storage_locations(id, name, kind, config)
    VALUES (p_id,p_name,p_kind,yoao_assets_storage_v1._location_config(p_config));
    RETURN QUERY SELECT m.* FROM yoao_assets_storage_v1.location_metadata m WHERE m.id = p_id;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.edit_location_policy(
    p_location uuid,p_revision integer,p_name text,p_enabled boolean,p_policy jsonb
) RETURNS SETOF yoao_assets_storage_v1.location_metadata
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    UPDATE public.storage_locations AS l
    SET name = p_name, enabled = p_enabled, config_revision = l.config_revision + 1,
        config = l.config || (SELECT coalesce(jsonb_object_agg(f.key,f.value), '{}'::jsonb)
            FROM jsonb_each(p_policy) f WHERE f.key IN ('cache_policy','original_cache_quota_bytes',
                'original_cache_ttl_seconds','scope','use_user_ids'))
    WHERE l.id = p_location AND l.config_revision = p_revision AND (
        l.kind IN ('filesystem','agent') OR (l.connection_id IS NOT NULL AND l.name = p_name
            AND p_policy -> 'scope' IS NOT DISTINCT FROM l.config -> 'scope'
            AND p_policy -> 'use_user_ids' IS NOT DISTINCT FROM l.config -> 'use_user_ids'));
    IF FOUND THEN
        RETURN QUERY SELECT m.* FROM yoao_assets_storage_v1.location_metadata m WHERE m.id = p_location;
    END IF;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.ensure_embedded_targets(p_config jsonb,p_buckets jsonb)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE v_legacy constant uuid := '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7';
BEGIN
    IF jsonb_typeof(p_config -> 'endpoint') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_config -> 'use_ssl') IS DISTINCT FROM 'boolean'
       OR p_config - ARRAY['endpoint','use_ssl'] <> '{}'::jsonb
       OR NOT p_buckets ?& ARRAY['annotations','datasets','bug-reports','media-cache',
            'audit-archive','import','export','avatars','training']
       OR p_buckets - ARRAY['annotations','datasets','bug-reports','media-cache',
            'audit-archive','import','export','avatars','training'] <> '{}'::jsonb
       OR EXISTS (SELECT FROM jsonb_each(p_buckets) e
                   WHERE jsonb_typeof(e.value) IS DISTINCT FROM 'string' OR e.value = '""'::jsonb) THEN
        RAISE invalid_parameter_value;
    END IF;
    INSERT INTO public.storage_locations (id,name,kind,config) VALUES (v_legacy,'legacy-platform-s3','s3',p_config)
    ON CONFLICT DO NOTHING;
    IF NOT EXISTS (SELECT FROM public.storage_locations
        WHERE id = v_legacy AND config = p_config AND kind = 's3' AND connection_id IS NULL) THEN
        RETURN false;
    END IF;
    INSERT INTO public.storage_bindings (purpose,location_id,bucket,generation)
    SELECT e.key,v_legacy,e.value,1 FROM jsonb_each_text(p_buckets) e ON CONFLICT DO NOTHING;
    RETURN true;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.switch_purpose_binding(
    p_purpose text,p_generation integer,p_location uuid,p_bucket text
) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    WITH moved AS (
        UPDATE public.storage_bindings b SET location_id = p_location, bucket = p_bucket, generation = b.generation + 1
        WHERE b.purpose = p_purpose AND b.generation = p_generation
          AND p_purpose IN ('annotations','datasets','bug-reports','media-cache','audit-archive','import','export','avatars','training')
          AND EXISTS (SELECT FROM public.storage_locations l
            LEFT JOIN public.storage_connections c ON c.id = l.connection_id
            WHERE l.id = p_location AND l.enabled AND l.kind = 's3' AND (
                (l.id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid AND l.connection_id IS NULL)
                OR (c.kind = 's3' AND c.scope = 'global' AND c.project_id IS NULL
                    AND octet_length(c.secret_enc) > 0 AND c.config ->> 'bucket' = p_bucket)))
        RETURNING b.purpose
    ) SELECT EXISTS (SELECT FROM moved);
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.location_probe_facts(p_location uuid,p_node text)
RETURNS TABLE(location_id uuid,node_id text,config_revision integer,observed_at timestamptz,result jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT p.location_id,p.node_id::text,p.config_revision,p.observed_at,p.result
    FROM public.storage_location_probes p
    WHERE p.location_id = p_location AND (p_node IS NULL OR p.node_id = p_node)
    ORDER BY p.node_id;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.record_location_probe(
    p_location uuid,p_node text,p_revision integer,p_observed_at timestamptz,p_result jsonb
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF NOT EXISTS (SELECT FROM public.storage_locations AS l
        LEFT JOIN public.storage_maintenance_gates AS g ON g.location_id = l.id
        WHERE l.id = p_location AND l.config_revision = p_revision
          AND coalesce(g.state, 'active') = 'active'
          AND coalesce(g.revision, 1)::text = coalesce(p_result ->> 'maintenance_revision','1'))
    THEN RETURN; END IF;
    IF p_result @> '{"ok":true}' AND p_result ? 'anchor' THEN
        UPDATE public.storage_locations l SET config = l.config || jsonb_build_object('agent_anchor',p_result -> 'anchor')
        WHERE l.id = p_location AND l.kind = 'agent'
            AND coalesce(l.config -> 'agent_anchor','null'::jsonb) = 'null'::jsonb;
    END IF;
    INSERT INTO public.storage_location_probes (location_id,node_id,config_revision,observed_at,result)
    VALUES (p_location,p_node,p_revision,p_observed_at,p_result)
    ON CONFLICT (location_id,node_id) DO UPDATE
    SET config_revision = EXCLUDED.config_revision, observed_at = EXCLUDED.observed_at, result = EXCLUDED.result
    WHERE public.storage_location_probes.observed_at <= EXCLUDED.observed_at;
END;
$fn$;

CREATE VIEW yoao_assets_storage_v1.site_facts AS
SELECT s.id,s.name,s.region,s.created_by,s.created_at FROM public.sites s OFFSET 0;

CREATE VIEW yoao_assets_storage_v1.agent_facts AS
SELECT a.id,a.site_id,a.name,a.protocol_version,a.credential_revision,a.pair_expires_at,
    a.pair_digest IS NOT NULL AS pairing_pending,a.revoked_at,a.draining,a.session_id,a.last_seen,
    (SELECT coalesce(jsonb_agg(jsonb_build_object('root_handle',r.root -> 'root_handle',
                'root_revision',r.root -> 'root_revision') ORDER BY r.position), '[]'::jsonb)
     FROM jsonb_array_elements(a.roots) WITH ORDINALITY r(root,position)) AS roots,
    a.limits,
    (SELECT jsonb_object_agg(f.key,f.value) FROM jsonb_each(a.reported_limits) f
     WHERE f.key IN ('max_concurrency','bandwidth_bytes_per_second','max_bytes')) AS reported_limits,
    a.created_by,a.created_at
FROM public.agent_registrations a OFFSET 0;

CREATE FUNCTION yoao_assets_storage_v1.create_site(p_actor uuid,p_id uuid,p_name text,p_region text)
RETURNS SETOF yoao_assets_storage_v1.site_facts
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    INSERT INTO public.sites (created_by,id,name,region) VALUES (p_actor,p_id,p_name,p_region)
    RETURNING id,name,region,created_by,created_at;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.register_agent(
    p_actor uuid,p_site uuid,p_agent uuid,p_name text,p_limits jsonb,p_pair_digest text
) RETURNS timestamptz LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE v_expires timestamptz;
BEGIN
    PERFORM FROM public.sites WHERE id = p_site FOR SHARE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    v_expires := clock_timestamp() + interval '10 minutes';
    INSERT INTO public.agent_registrations
        (id,site_id,name,protocol_version,credential_revision,roots,limits,created_by,pair_digest,pair_expires_at)
    VALUES (p_agent,p_site,p_name,1,1,'[]'::jsonb,p_limits,p_actor,p_pair_digest,v_expires);
    RETURN v_expires;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.agent_identity(
    p_agent uuid,p_revision integer,p_verifier text,p_lock text
) RETURNS TABLE(agent yoao_assets_storage_v1.agent_facts, authenticated_revision integer,
    credential_expires_at timestamptz, credential_revoked_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE a_id uuid := p_agent;
    a_revision integer := p_revision;
BEGIN
    IF p_verifier IS NOT NULL THEN
        SELECT c.agent_id,c.revision INTO a_id,a_revision
        FROM public.agent_credentials c WHERE c.token_digest = p_verifier
          AND (p_agent IS NULL OR c.agent_id = p_agent)
          AND (p_revision IS NULL OR c.revision = p_revision);
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    IF p_lock = 'share' THEN
        PERFORM FROM public.agent_registrations WHERE id = a_id FOR SHARE;
    ELSIF p_lock IS NOT NULL THEN
        RAISE invalid_parameter_value;
    END IF;
    RETURN QUERY SELECT a,c.revision,c.expires_at,c.revoked_at
    FROM yoao_assets_storage_v1.agent_facts a LEFT JOIN public.agent_credentials c
      ON c.agent_id = a.id AND c.revision = coalesce(a_revision,a.credential_revision)
    WHERE a.id = a_id;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.pair_agent(p_agent uuid,p_pair_digest text,p_token_digest text)
RETURNS TABLE(code text, credential_revision integer, expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE a public.agent_registrations%ROWTYPE;
    stamp timestamptz;
BEGIN
    SELECT * INTO a FROM public.agent_registrations WHERE id = p_agent FOR UPDATE;
    IF NOT FOUND THEN RETURN QUERY SELECT 'agent_not_found'::text,NULL::integer,NULL::timestamptz; RETURN; END IF;
    IF a.revoked_at IS NOT NULL THEN RETURN QUERY SELECT 'agent_revoked'::text,NULL::integer,NULL::timestamptz; RETURN; END IF;
    stamp := clock_timestamp();
    IF a.pair_digest IS NULL OR a.pair_digest IS DISTINCT FROM p_pair_digest OR a.pair_expires_at IS NULL
       OR a.pair_expires_at <= stamp THEN
        RETURN QUERY SELECT 'pair_code_invalid'::text,NULL::integer,NULL::timestamptz; RETURN;
    END IF;
    IF a.protocol_version <> 1 THEN
        RETURN QUERY SELECT 'agent_protocol_incompatible'::text,NULL::integer,NULL::timestamptz; RETURN;
    END IF;
    INSERT INTO public.agent_credentials (agent_id,revision,token_digest,expires_at)
    VALUES (a.id,a.credential_revision,p_token_digest,stamp + interval '30 days');
    UPDATE public.agent_registrations SET pair_digest = NULL,pair_expires_at = NULL WHERE id = a.id;
    RETURN QUERY SELECT NULL::text,a.credential_revision,stamp + interval '30 days';
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.rotate_agent(p_agent uuid,p_revision integer,p_pair_digest text)
RETURNS TABLE(code text, expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE a public.agent_registrations%ROWTYPE;
    stamp timestamptz;
BEGIN
    SELECT * INTO a FROM public.agent_registrations WHERE id = p_agent FOR UPDATE;
    IF NOT FOUND THEN RETURN QUERY SELECT 'agent_not_found'::text,NULL::timestamptz; RETURN; END IF;
    IF p_revision IS NOT NULL AND p_revision <> a.credential_revision THEN
        RETURN QUERY SELECT 'credential_revision_changed'::text,NULL::timestamptz; RETURN;
    END IF;
    IF a.revoked_at IS NOT NULL THEN RETURN QUERY SELECT 'agent_revoked'::text,NULL::timestamptz; RETURN; END IF;
    IF NOT a.draining OR EXISTS (SELECT FROM public.site_agent_commands c
        WHERE c.agent_id = a.id AND c.state IN ('leased','received','unsettled')) THEN
        RETURN QUERY SELECT 'agent_tasks_not_drained'::text,NULL::timestamptz; RETURN;
    END IF;
    stamp := clock_timestamp();
    PERFORM FROM public.agent_credentials WHERE agent_id = a.id ORDER BY revision FOR UPDATE;
    UPDATE public.agent_credentials SET revoked_at = stamp WHERE agent_id = a.id AND revoked_at IS NULL;
    UPDATE public.agent_registrations SET credential_revision = a.credential_revision + 1,
        session_id = NULL,last_seen = NULL,reported_limits = NULL,roots = '[]'::jsonb,
        pair_digest = p_pair_digest,pair_expires_at = stamp + interval '10 minutes'
    WHERE id = a.id;
    RETURN QUERY SELECT NULL::text,stamp + interval '10 minutes';
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.control_agent(
    p_agent uuid,p_revision integer,p_revoke boolean,p_draining boolean
) RETURNS TABLE(code text, agent yoao_assets_storage_v1.agent_facts)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE a public.agent_registrations%ROWTYPE;
    stamp timestamptz;
BEGIN
    SELECT * INTO a FROM public.agent_registrations WHERE id = p_agent FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'agent_not_found'::text,NULL::yoao_assets_storage_v1.agent_facts; RETURN;
    END IF;
    IF p_revision IS NOT NULL AND p_revision <> a.credential_revision THEN
        RETURN QUERY SELECT 'credential_revision_changed'::text,NULL::yoao_assets_storage_v1.agent_facts; RETURN;
    END IF;
    IF p_revoke THEN
        stamp := clock_timestamp();
        UPDATE public.agent_registrations SET revoked_at = coalesce(revoked_at,stamp),draining = true,
            pair_digest = NULL,pair_expires_at = NULL WHERE id = a.id;
        PERFORM FROM public.agent_credentials WHERE agent_id = a.id ORDER BY revision FOR UPDATE;
        UPDATE public.agent_credentials SET revoked_at = stamp WHERE agent_id = a.id AND revoked_at IS NULL;
    ELSIF a.revoked_at IS NULL AND p_draining IS NOT NULL THEN
        UPDATE public.agent_registrations SET draining = p_draining WHERE id = a.id;
    ELSE
        RETURN QUERY SELECT 'agent_revoked'::text,NULL::yoao_assets_storage_v1.agent_facts; RETURN;
    END IF;
    RETURN QUERY SELECT NULL::text,f FROM yoao_assets_storage_v1.agent_facts f WHERE f.id = a.id;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.observe_agent(
    p_agent uuid,p_revision integer,p_verifier text,p_session uuid,p_roots jsonb,p_limits jsonb
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE a public.agent_registrations%ROWTYPE;
    c public.agent_credentials%ROWTYPE;
BEGIN
    SELECT * INTO a FROM public.agent_registrations WHERE id = p_agent FOR UPDATE;
    IF NOT FOUND THEN RETURN 'agent_unauthorized'; END IF;
    SELECT * INTO c FROM public.agent_credentials
    WHERE agent_id = p_agent AND revision = p_revision AND token_digest = p_verifier;
    IF NOT FOUND THEN RETURN 'agent_unauthorized'; END IF;
    IF a.protocol_version <> 1 THEN RETURN 'agent_protocol_incompatible'; END IF;
    IF a.revoked_at IS NOT NULL THEN RETURN 'agent_revoked'; END IF;
    IF a.pair_digest IS NOT NULL THEN RETURN 'agent_unpaired'; END IF;
    IF c.revoked_at IS NOT NULL THEN RETURN 'agent_unauthorized'; END IF;
    IF c.expires_at <= clock_timestamp() THEN RETURN 'credential_expired'; END IF;
    IF a.credential_revision <> p_revision THEN RETURN 'credential_revision_changed'; END IF;
    UPDATE public.agent_registrations SET last_seen = clock_timestamp(),session_id = p_session,
        roots = p_roots,reported_limits = p_limits WHERE id = a.id;
    RETURN NULL;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1._agent_credential_error(p_agent uuid,p_revision integer,p_verifier text)
RETURNS text LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT coalesce((SELECT CASE
        WHEN a.protocol_version <> 1 THEN 'agent_protocol_incompatible'
        WHEN a.revoked_at IS NOT NULL THEN 'agent_revoked'
        WHEN a.pair_digest IS NOT NULL THEN 'agent_unpaired'
        WHEN c.revoked_at IS NOT NULL THEN 'agent_unauthorized'
        WHEN c.expires_at <= statement_timestamp() THEN 'credential_expired'
        WHEN a.credential_revision <> c.revision THEN 'credential_revision_changed'
        ELSE '' END
    FROM public.agent_credentials c JOIN public.agent_registrations a ON a.id = c.agent_id
    WHERE c.agent_id = p_agent AND c.revision = p_revision AND c.token_digest = p_verifier),
    'agent_unauthorized');
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.agent_command_receipt(p_id uuid)
RETURNS SETOF public.site_agent_commands
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT c.* FROM public.site_agent_commands c WHERE c.id = p_id;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.lock_agent_command(
    p_id uuid,p_agent uuid,p_revision integer,p_verifier text
) RETURNS SETOF public.site_agent_commands
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT c.* FROM public.site_agent_commands c
    JOIN public.agent_credentials a ON a.agent_id = c.agent_id AND a.revision = c.credential_revision
    WHERE c.id = p_id AND c.agent_id = p_agent AND c.credential_revision = p_revision
        AND a.token_digest = p_verifier
    FOR UPDATE OF c;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.enqueue_agent_command(
    p_id uuid,p_location uuid,p_agent uuid,p_revision integer,p_operation uuid,p_payload jsonb,
    p_digest text,p_expires timestamptz
) RETURNS SETOF public.site_agent_commands
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    INSERT INTO public.site_agent_commands (id,location_id,agent_id,credential_revision,operation_id,
        payload,command_digest,state,priority,expires_at,received_bytes,relay_released,chunks)
    SELECT p_id,p_location,p_agent,p_revision,p_operation,p_payload,p_digest,'queued',
        CASE p_payload ->> 'operation' WHEN 'read' THEN 0 WHEN 'scan' THEN 2 ELSE 1 END,
        p_expires,0,true,'{}'::jsonb
    FROM public.storage_locations l JOIN public.agent_registrations a ON a.id = p_agent
    JOIN public.agent_credentials c ON c.agent_id = a.id AND c.revision = a.credential_revision
    WHERE l.id = p_location AND l.kind = 'agent' AND l.enabled AND c.revision = p_revision
        AND a.revoked_at IS NULL AND a.pair_digest IS NULL AND c.revoked_at IS NULL
        AND p_expires <= c.expires_at AND p_expires > statement_timestamp()
        AND p_payload ->> 'operation' IN ('read','scan','browse','probe')
        AND p_payload ->> 'command_id' = p_id::text AND p_payload ->> 'agent_id' = p_agent::text
        AND p_payload ->> 'credential_revision' = p_revision::text
        AND p_payload -> 'fence' ->> 'location_id' = l.id::text
        AND p_payload -> 'fence' ->> 'location_revision' = l.config_revision::text
        AND l.config ->> 'agent_id' = a.id::text
        AND (l.config -> 'root_handle',l.config -> 'root_revision') =
            (p_payload -> 'root_handle',p_payload -> 'root_revision')
        AND EXISTS (
            SELECT FROM public.storage_operations o WHERE o.id = p_operation AND o.location_id = p_location
            AND o.revision::text = p_payload -> 'fence' ->> 'maintenance_revision'
            AND o.purpose = p_payload -> 'fence' ->> 'purpose')
    RETURNING *;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.agent_lease_candidates(
    p_agent uuid,p_revision integer,p_verifier text,p_session uuid
) RETURNS SETOF public.site_agent_commands
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE a public.agent_registrations%ROWTYPE;
    v_owned integer;
BEGIN
    PERFORM pg_advisory_xact_lock(7164521385097);
    SELECT * INTO a FROM public.agent_registrations WHERE id = p_agent FOR SHARE;
    IF NOT FOUND OR yoao_assets_storage_v1._agent_credential_error(p_agent,p_revision,p_verifier) <> '' THEN
        RETURN;
    END IF;
    RETURN QUERY SELECT c.* FROM public.site_agent_commands c
    WHERE c.agent_id = p_agent AND c.credential_revision = p_revision
      AND c.state IN ('leased','unsettled') AND c.lease_session_id = p_session AND c.result IS NULL
    ORDER BY c.created_at LIMIT 1 FOR UPDATE;
    IF FOUND OR a.draining OR a.reported_limits IS NULL THEN RETURN; END IF;
    IF (SELECT count(*) FROM public.site_agent_commands c
        WHERE c.state IN ('leased','received','unsettled') OR NOT c.relay_released) >= 4 THEN RETURN; END IF;
    SELECT count(*) INTO v_owned FROM public.site_agent_commands c
    WHERE c.agent_id = p_agent AND c.state IN ('leased','received','unsettled');
    IF v_owned >= least((a.limits ->> 'max_concurrency')::integer,
                        (a.reported_limits ->> 'max_concurrency')::integer) THEN RETURN; END IF;
    RETURN QUERY SELECT c.* FROM public.site_agent_commands c
    WHERE c.agent_id = p_agent AND c.state = 'queued'
    ORDER BY c.priority,c.created_at LIMIT 16 FOR UPDATE SKIP LOCKED;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.register_connector_location(p_connection uuid)
RETURNS SETOF yoao_assets_storage_v1.location_metadata
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE v_location uuid := md5('aap:storage-connection:' || p_connection::text)::uuid;
BEGIN
    INSERT INTO public.storage_locations(id, name, kind, config, connection_id)
    SELECT v_location,'connector-' || c.id::text,c.kind,'{}'::jsonb,c.id
    FROM public.storage_connections c WHERE c.id = p_connection;
    RETURN QUERY SELECT m.* FROM yoao_assets_storage_v1.location_metadata m WHERE m.id = v_location;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.lock_binding_locations(p_current uuid,p_target uuid)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE v_before jsonb;
    v_after jsonb;
    v_count integer;
BEGIN
    SELECT jsonb_object_agg(l.id,l.connection_id), count(*) INTO v_before,v_count
    FROM public.storage_locations l WHERE l.id IN (p_current,p_target);
    IF v_count <> (CASE WHEN p_current = p_target THEN 1 ELSE 2 END) THEN
        RETURN 'missing';
    END IF;
    PERFORM c.id FROM public.storage_connections c
    WHERE c.id IN (SELECT f.value::uuid FROM jsonb_each_text(v_before) f)
    ORDER BY c.id FOR SHARE NOWAIT;
    PERFORM l.id FROM public.storage_locations l WHERE l.id IN (p_current,p_target)
    ORDER BY l.id FOR SHARE NOWAIT;
    SELECT jsonb_object_agg(l.id,l.connection_id) INTO v_after
    FROM public.storage_locations l WHERE l.id IN (p_current,p_target);
    IF v_before IS DISTINCT FROM v_after THEN RETURN 'changed'; END IF;
    RETURN 'locked';
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.claim_agent_command(
    p_id uuid,p_agent uuid,p_revision integer,p_verifier text,p_session uuid,p_rejection text
) RETURNS SETOF public.site_agent_commands
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    UPDATE public.site_agent_commands c
    SET state = CASE WHEN p_rejection IS NULL THEN 'leased' ELSE 'rejected' END,
        lease_session_id = CASE WHEN p_rejection IS NULL THEN p_session ELSE NULL END,
        error_code = p_rejection,updated_at = clock_timestamp()
    WHERE c.id = p_id AND c.agent_id = p_agent AND c.state = 'queued'
      AND (p_rejection IS NOT NULL OR (
          c.credential_revision = p_revision AND p_session IS NOT NULL
          AND c.expires_at > clock_timestamp() AND EXISTS (
              SELECT FROM public.storage_operations o
              JOIN public.storage_maintenance_gates g ON g.location_id = o.location_id
              WHERE o.id = c.operation_id AND o.location_id = c.location_id
                AND o.revision = g.revision AND g.state = 'active'
                AND o.purpose = c.payload -> 'fence' ->> 'purpose'
                AND o.revision::text = c.payload -> 'fence' ->> 'maintenance_revision')))
      AND yoao_assets_storage_v1._agent_credential_error(p_agent,p_revision,p_verifier) = ''
    RETURNING c.*;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.record_agent_chunk(
    p_id uuid,p_agent uuid,p_revision integer,p_verifier text,p_digest text,
    p_offset integer,p_size integer,p_checksum text
) RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    WITH captured AS (
        UPDATE public.site_agent_commands c
        SET chunks = c.chunks || jsonb_build_object(p_offset::text,jsonb_build_object('sha256',p_checksum,'size',p_size)),
            received_bytes = p_offset + p_size,relay_released = false,updated_at = clock_timestamp()
        WHERE c.id = p_id AND c.agent_id = p_agent AND c.credential_revision = p_revision
          AND c.command_digest = p_digest AND c.lease_session_id IS NOT NULL AND c.state = 'leased'
          AND c.expires_at > clock_timestamp() AND c.payload ->> 'operation' = 'read'
          AND c.received_bytes = p_offset AND p_size BETWEEN 1 AND 262144
          AND p_offset + p_size <= (c.payload ->> 'max_bytes')::integer
          AND p_checksum ~ '^[0-9a-f]{64}$'
          AND yoao_assets_storage_v1._agent_credential_error(p_agent,p_revision,p_verifier) = ''
        RETURNING c.id
    ) SELECT EXISTS (SELECT FROM captured);
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.record_agent_result(
    p_id uuid,p_agent uuid,p_revision integer,p_verifier text,p_digest text,
    p_result jsonb,p_result_digest text,p_rejection text
) RETURNS SETOF public.site_agent_commands
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE v_error text;
    v_closed boolean;
    v_row public.site_agent_commands%ROWTYPE;
BEGIN
    IF NOT EXISTS (SELECT FROM public.agent_credentials c
        WHERE c.agent_id = p_agent AND c.revision = p_revision AND c.token_digest = p_verifier) THEN RETURN; END IF;
    SELECT * INTO v_row FROM public.site_agent_commands c
    WHERE c.id = p_id AND c.agent_id = p_agent AND c.credential_revision = p_revision
      AND c.command_digest = p_digest AND c.lease_session_id IS NOT NULL
    FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    v_error := coalesce(p_rejection,nullif(yoao_assets_storage_v1._agent_credential_error(p_agent,p_revision,p_verifier),''));
    IF v_row.result_digest IS NOT NULL THEN
        IF v_row.result_digest <> p_result_digest THEN RETURN; END IF;
        IF v_row.state = 'received' AND v_error IS NOT NULL THEN
            UPDATE public.site_agent_commands SET state = 'rejected',error_code = v_error,updated_at = clock_timestamp()
            WHERE id = p_id RETURNING * INTO v_row;
        END IF;
        RETURN NEXT v_row;
        RETURN;
    END IF;
    IF p_result ->> 'command_id' IS DISTINCT FROM p_id::text
        OR p_result ->> 'command_digest' IS DISTINCT FROM p_digest
        OR jsonb_typeof(p_result -> 'closed') IS DISTINCT FROM 'boolean'
        OR p_result ->> 'status' NOT IN ('completed','failed') THEN RETURN; END IF;
    v_closed := (p_result ->> 'closed')::boolean;
    UPDATE public.site_agent_commands
    SET result = p_result,result_digest = p_result_digest,closed = v_closed,
        error_code = coalesce(v_error,p_result ->> 'error_code'),
        state = CASE WHEN NOT v_closed THEN 'unsettled' WHEN v_error IS NULL THEN 'received' ELSE 'rejected' END,
        updated_at = clock_timestamp()
    WHERE id = p_id RETURNING * INTO v_row;
    IF v_closed AND v_row.operation_id IS NOT NULL THEN
        DELETE FROM public.storage_operations o
        WHERE o.id = v_row.operation_id AND o.location_id = v_row.location_id
          AND o.revision::text = v_row.payload -> 'fence' ->> 'maintenance_revision'
          AND o.purpose = v_row.payload -> 'fence' ->> 'purpose';
    END IF;
    RETURN NEXT v_row;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.lock_agent_ack(p_id uuid)
RETURNS SETOF public.site_agent_commands
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT c.* FROM public.site_agent_commands c WHERE c.id = p_id FOR UPDATE;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.acknowledge_agent_result(p_id uuid,p_digest text,p_accepted boolean,p_code text)
RETURNS boolean LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    WITH ack AS (
        UPDATE public.site_agent_commands
        SET state = CASE WHEN p_accepted THEN 'applied' ELSE 'rejected' END,
            error_code = CASE WHEN p_accepted THEN NULL ELSE coalesce(p_code,'agent_result_rejected') END,
            updated_at = clock_timestamp()
        WHERE id = p_id AND result_digest = p_digest AND state = 'received' AND closed IS TRUE
        RETURNING id
    ) SELECT EXISTS (SELECT FROM ack);
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.cleanup_agent_relays()
RETURNS TABLE(id uuid,result_digest text) LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    UPDATE public.site_agent_commands c
    SET state = 'rejected',error_code = 'agent_command_expired',updated_at = clock_timestamp()
    WHERE c.state = 'received' AND c.closed IS TRUE AND c.expires_at <= clock_timestamp();
    RETURN QUERY SELECT c.id,c.result_digest::text FROM public.site_agent_commands c
    WHERE c.state IN ('applied','rejected') AND c.closed IS TRUE AND NOT c.relay_released
    LIMIT 16 FOR UPDATE SKIP LOCKED;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.release_agent_relay(p_id uuid,p_digest text)
RETURNS SETOF public.site_agent_commands LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    UPDATE public.site_agent_commands SET relay_released = true
    WHERE id = p_id AND result_digest = p_digest AND state IN ('applied','rejected') AND closed IS TRUE
        AND NOT relay_released RETURNING *;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.expire_agent_command(p_id uuid)
RETURNS SETOF public.site_agent_commands LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $fn$
    UPDATE public.site_agent_commands
    SET state = CASE WHEN lease_session_id IS NULL THEN 'rejected' ELSE 'unsettled' END,
        error_code = 'agent_command_expired',updated_at = clock_timestamp()
    WHERE id = p_id AND result IS NULL AND expires_at <= clock_timestamp()
    RETURNING *;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.abandon_agent_command(p_id uuid,p_operation uuid,p_code text)
RETURNS SETOF public.site_agent_commands LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE v_row public.site_agent_commands%ROWTYPE;
BEGIN
    SELECT * INTO v_row FROM public.site_agent_commands c
    WHERE c.id = p_id AND c.operation_id = p_operation FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;
    IF v_row.state NOT IN ('applied','rejected') THEN
        UPDATE public.site_agent_commands
        SET state = CASE WHEN v_row.closed IS NOT TRUE AND v_row.lease_session_id IS NOT NULL
            THEN 'unsettled' ELSE 'rejected' END,
            error_code = CASE WHEN v_row.closed IS NOT TRUE AND v_row.lease_session_id IS NOT NULL
            THEN error_code ELSE p_code END,updated_at = clock_timestamp()
        WHERE id = p_id RETURNING * INTO v_row;
    END IF;
    RETURN NEXT v_row;
END;
$fn$;

CREATE VIEW yoao_assets_storage_v1.agent_location_status AS
SELECT x.location_id,x.agent_id,x.site_id,x.agent_site_name,x.agent_name,x.agent_last_seen,
    x.agent_reason_code,
    CASE x.agent_reason_code
        WHEN 'agent_online' THEN 'online'
        WHEN 'agent_offline' THEN 'offline'
        WHEN 'agent_revoked' THEN 'revoked'
        WHEN 'agent_unpaired' THEN 'pending'
        WHEN 'credential_expired' THEN 'expired'
        WHEN 'agent_draining' THEN 'degraded'
        WHEN 'agent_root_revision_changed' THEN 'degraded'
        ELSE 'unavailable'
    END AS agent_state,now() AS agent_measured_at
FROM (
    SELECT l.id AS location_id,a.id AS agent_id,s.id AS site_id,s.name AS agent_site_name,
        a.name AS agent_name,a.last_seen AS agent_last_seen,
        CASE
            WHEN a.id IS NULL THEN 'agent_unavailable'
            WHEN a.revoked_at IS NOT NULL THEN 'agent_revoked'
            WHEN a.pair_digest IS NOT NULL THEN 'agent_unpaired'
            WHEN c.agent_id IS NULL OR c.revoked_at IS NOT NULL THEN 'agent_unauthorized'
            WHEN c.expires_at <= now() THEN 'credential_expired'
            WHEN a.last_seen IS NULL OR a.last_seen <= now() - interval '60 seconds' THEN 'agent_offline'
            WHEN a.draining THEN 'agent_draining'
            WHEN NOT coalesce(a.roots @> jsonb_build_array(jsonb_build_object(
                'root_handle',l.config -> 'root_handle','root_revision',l.config -> 'root_revision')),false)
                THEN 'agent_root_revision_changed'
            WHEN a.reported_limits IS NULL THEN 'agent_unavailable'
            ELSE 'agent_online'
        END AS agent_reason_code
    FROM public.storage_locations l
    LEFT JOIN public.agent_registrations a ON a.id::text = l.config ->> 'agent_id'
    LEFT JOIN public.sites s ON s.id = a.site_id
    LEFT JOIN public.agent_credentials c ON c.agent_id = a.id AND c.revision = a.credential_revision
    WHERE l.kind = 'agent'
) x OFFSET 0;

CREATE VIEW yoao_assets_storage_v1.probe_health AS
SELECT p.location_id,p.node_id,p.config_revision,p.observed_at,
    jsonb_build_object('ok',p.result -> 'ok','code',p.result -> 'code',
        'capabilities',coalesce(p.result -> 'capabilities','{}'::jsonb)) AS result
FROM public.storage_location_probes p OFFSET 0;

CREATE VIEW yoao_assets_storage_v1.gate_status AS
SELECT g.location_id,g.state,g.revision,g.changed_at,g.changed_by
FROM public.storage_maintenance_gates g OFFSET 0;

CREATE FUNCTION yoao_audit_v1.archive_location_referenced(p_location uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT EXISTS (SELECT FROM public.audit_archive_outputs a WHERE a.storage_ref ->> 'location_id' = p_location::text);
$fn$;

CREATE FUNCTION yoao_audit_v1.archive_maintenance_targets(p_fingerprint boolean)
RETURNS TABLE(record_id text,location_id text,fingerprint text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT a.id::text,a.storage_ref ->> 'location_id',
        CASE WHEN p_fingerprint THEN encode(sha256(convert_to(
            jsonb_build_array('audit_archive_outputs','output',a.id,a.job_id,a.year,a.month,a.state,a.storage_ref)::text,
            'UTF8')),'hex') ELSE '' END
    FROM public.audit_archive_outputs a;
$fn$;

REVOKE ALL ON FUNCTION yoao_audit_v1.archive_location_referenced(uuid),
    yoao_audit_v1.archive_maintenance_targets(boolean) FROM PUBLIC;

CREATE FUNCTION yoao_assets_storage_v1._adopt_installed_owners()
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE v_assets name;
    v_audit name;
    v_function record;
    v_view record;
BEGIN
    SELECT pg_get_userbyid(p.proowner) INTO STRICT v_assets FROM pg_proc p
    WHERE p.oid = 'yoao_assets_v1.read_task_original_binding(uuid)'::regprocedure;
    SELECT pg_get_userbyid(p.proowner) INTO STRICT v_audit FROM pg_proc p
    WHERE p.oid = 'yoao_audit_v1.append_events(jsonb)'::regprocedure;
    IF current_user <> v_assets THEN
        EXECUTE format('GRANT USAGE, CREATE ON SCHEMA yoao_assets_storage_v1 TO %I',v_assets);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON public.storage_locations,
            public.storage_location_probes, public.storage_bindings, public.storage_maintenance_gates TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,name,kind,revision,scope,project_id,created_by,config,secret_enc),
            UPDATE (id) ON public.storage_connections TO %I',v_assets);
        EXECUTE format('GRANT SELECT, INSERT, DELETE, UPDATE (native_stage) ON public.storage_operations TO %I',v_assets);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE (id) ON public.sites TO %I',v_assets);
        EXECUTE format('GRANT SELECT, INSERT,
            UPDATE (credential_revision,pair_digest,pair_expires_at,revoked_at,draining,session_id,last_seen,roots,reported_limits)
            ON public.agent_registrations TO %I',v_assets);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE (revoked_at) ON public.agent_credentials TO %I',v_assets);
        EXECUTE format('GRANT SELECT, INSERT,
            UPDATE (state,lease_session_id,error_code,updated_at,received_bytes,relay_released,chunks,result,result_digest,closed)
            ON public.site_agent_commands TO %I',v_assets);
        EXECUTE format('GRANT USAGE ON SCHEMA yoao_labeling_v1,yoao_learning_v1,yoao_audit_v1,
            yoao_diagnostics_v1,yoao_export_v1 TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,dataset_item_id,project_id) ON yoao_labeling_v1.task_rows TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,task_id) ON yoao_labeling_v1.annotation_rows TO %I',v_assets);
        EXECUTE format('GRANT EXECUTE ON FUNCTION yoao_audit_v1.archive_maintenance_targets(boolean) TO %I',v_assets);
        IF to_regprocedure('yoao_learning_v1.training_dataset_location_versions(uuid)') IS NOT NULL THEN
            EXECUTE format('GRANT EXECUTE ON FUNCTION yoao_learning_v1.training_dataset_location_versions(uuid) TO %I',v_assets);
        END IF;
        FOR v_view IN SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE (n.nspname,c.relname) IN (
                ('yoao_export_v1','export_zip_maintenance'),('yoao_export_v1','export_media_maintenance'),
                ('yoao_learning_v1','training_run_storage_owner'),('yoao_learning_v1','training_preview_storage_owner'),
                ('yoao_learning_v1','sample_evaluation_storage_owners'),('yoao_learning_v1','selection_storage_owners'))
        LOOP
            EXECUTE format('GRANT SELECT ON %I.%I TO %I',v_view.nspname,v_view.relname,v_assets);
        END LOOP;
        EXECUTE format('GRANT SELECT (id,project_id,parent_id,attempt_id,status,storage_ref,snapshot_digest)
            ON public.training_dataset_versions TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,storage_ref,bucket,prefix) ON public.training_storage_deletions TO %I',v_assets);
        EXECUTE format('GRANT SELECT (token,owner_id,state,storage_ref,object_version_id) ON public.avatar_uploads TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,owner_id,project_id,task_id,annotation_id,bound_comment_id,state,storage_ref)
            ON public.comment_attachment_uploads TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,owner_id,project_id,state,storage_ref) ON public.guide_asset_uploads TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,logical_key,canonical_sha256,stored_sha256,stored_size_bytes,storage_encoding,state,storage_ref)
            ON public.raster_mask_objects TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,project_id,uploader_id,logical_object_key,storage_ref) ON public.mask_format_uploads TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,project_id,staged_object_key,staged_sha256,staging_ref,status) ON public.mask_format_imports TO %I',v_assets);
        EXECUTE format('GRANT SELECT (project_id,upload_id) ON public.project_guide_assets TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,dataset_id,project_id) ON public.project_datasets TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,dataset_id) ON public.dataset_items TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,location_id,dataset_id,config_revision,policy_revision,enabled,unregistered_at,generation)
            ON public.dataset_sources TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,source_id,dataset_item_id,asset_id) ON public.dataset_source_revisions TO %I',v_assets);
        EXECUTE format('GRANT SELECT (dataset_item_id,source_id,run_id,path_digest,source_revision,location_revision)
            ON public.dataset_references TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,source_id,status,generation,async_job_id)
            ON public.dataset_scan_runs,public.dataset_registration_runs TO %I',v_assets);
        EXECUTE format('GRANT SELECT (source_id,revision,enabled) ON public.dataset_source_sync_schedules TO %I',v_assets);
        EXECUTE format('GRANT SELECT (source_id,path_digest,asset_id,thumbnail_output_id,generation,policy_revision,state,location_id,bucket,object_key,size_bytes)
            ON public.dataset_source_previews TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,source_id,asset_id,sha256,scope_location_id,source_revision,policy_revision,location_revision,
            location_id,bucket,binding_generation,storage_identity,storage_ref,state,size_bytes) ON public.original_cache_entries TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,entry_id,expires_at) ON public.original_cache_read_leases TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,creation_id,task_id,project_id,location_id,bucket,object_key,storage_identity,storage_ref,version_id)
            ON public.task_originals TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,creation_id,dataset_id,kind,location_id,bucket,object_key,storage_identity,storage_ref,version_id)
            ON public.dataset_auxiliary_originals TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,assertion_digest,creation_id,dataset_id,dataset_item_id,asset_id,location_id,
            bucket,object_key,storage_identity,location_config_revision,binding_generation,role,identity_state,reference_state)
            ON public.managed_original_retirements TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,dataset_item_id,kind,metadata_output_id,storage_ref,state)
            ON public.video_derivative_outputs TO %I',v_assets);
        EXECUTE format('GRANT SELECT (id,owner_kind,owner_id,path_digest,state,storage_ref,version_id)
            ON public.thumbnail_outputs TO %I',v_assets);
        FOR v_view IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='yoao_assets_storage_v1' AND c.relkind='v' LOOP
            EXECUTE format('ALTER VIEW yoao_assets_storage_v1.%I OWNER TO %I',v_view.relname,v_assets);
        END LOOP;
        FOR v_function IN SELECT p.oid::regprocedure AS signature FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'yoao_assets_storage_v1' AND p.proname <> '_adopt_installed_owners' LOOP
            EXECUTE format('ALTER FUNCTION %s OWNER TO %I',v_function.signature,v_assets);
        END LOOP;
        EXECUTE format('REVOKE CREATE ON SCHEMA yoao_assets_storage_v1 FROM %I',v_assets);
    END IF;
    IF current_user <> v_audit THEN
        EXECUTE format('GRANT USAGE, CREATE ON SCHEMA yoao_audit_v1 TO %I',v_audit);
        EXECUTE format('GRANT SELECT (id,job_id,year,month,state,storage_ref) ON public.audit_archive_outputs TO %I',v_audit);
        EXECUTE format('ALTER FUNCTION yoao_audit_v1.archive_location_referenced(uuid) OWNER TO %I',v_audit);
        EXECUTE format('ALTER FUNCTION yoao_audit_v1.archive_maintenance_targets(boolean) OWNER TO %I',v_audit);
        EXECUTE format('REVOKE CREATE ON SCHEMA yoao_audit_v1 FROM %I',v_audit);
    END IF;
END;
$fn$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA yoao_assets_storage_v1 FROM PUBLIC;
