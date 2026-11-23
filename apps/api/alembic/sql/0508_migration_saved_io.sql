CREATE TABLE public.storage_migration_io_reads (
    operation_id uuid PRIMARY KEY REFERENCES public.storage_operations(id) ON DELETE CASCADE,
    publication_id uuid NOT NULL,
    publication_revision integer NOT NULL,
    members jsonb NOT NULL,
    owner_operation_id uuid
);
-- aap-statement-break
CREATE TABLE public.storage_migration_io_writes (
    attempt_id uuid NOT NULL REFERENCES public.storage_migration_copy_attempts(id) ON DELETE CASCADE,
    object_id text NOT NULL CHECK (object_id ~ '^[0-9a-f]{64}$'),
    delta_sequence integer NOT NULL,
    nonce text NOT NULL CHECK (nonce ~ '^[0-9a-f]{48}$'),
    PRIMARY KEY (attempt_id, object_id)
);
-- aap-statement-break
CREATE TABLE public.storage_migration_io_probes (
    operation_id uuid PRIMARY KEY REFERENCES public.storage_operations(id) ON DELETE CASCADE,
    publication_id uuid NOT NULL,
    purpose text NOT NULL
);
-- aap-statement-break
CREATE TABLE public.storage_migration_io_flights (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    spool_owner uuid NOT NULL,
    operation_id uuid NOT NULL,
    parent_operation_id uuid,
    CONSTRAINT migration_io_live_operation FOREIGN KEY (operation_id)
        REFERENCES public.storage_operations(id) ON DELETE RESTRICT,
    CONSTRAINT migration_io_live_parent_operation FOREIGN KEY (parent_operation_id)
        REFERENCES public.storage_operations(id) ON DELETE RESTRICT
);
-- aap-statement-break
CREATE INDEX ix_migration_io_flights_operation ON public.storage_migration_io_flights (operation_id);
-- aap-statement-break
CREATE INDEX ix_migration_io_flights_parent ON public.storage_migration_io_flights (parent_operation_id);
-- aap-statement-break
CREATE INDEX ix_migration_io_flights_spool_owner ON public.storage_migration_io_flights (spool_owner);
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_io_open_flight(p_spool uuid, p_operation uuid, p_kind text)
RETURNS uuid
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH proof AS MATERIALIZED (
        SELECT op.id, parent.id AS parent_id
        FROM public.storage_operations op
        JOIN public.storage_maintenance_gates g ON g.location_id = op.location_id
            AND g.revision = op.revision AND g.state = 'active'
        JOIN public.storage_migration_copy_attempts a ON a.target_operation_id = op.id
            AND a.status = 'running' AND a.operation IN ('run','verify') AND a.cancel_requested_at IS NULL
        JOIN public.storage_migration_copies c ON c.id = a.copy_id AND c.last_attempt_id = a.id
            AND c.target_location_id = op.location_id
        JOIN public.storage_operations parent ON parent.id = a.source_operation_id
            AND parent.purpose = 'storage_migration_copy'
            AND parent.location_id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid
        WHERE p_kind = 'copy' AND op.id = p_operation AND op.purpose = 'storage_migration_copy'
        UNION ALL
        SELECT op.id, b.owner_operation_id
        FROM public.storage_operations op
        JOIN public.storage_maintenance_gates g ON g.location_id = op.location_id
            AND g.revision = op.revision AND g.state = 'active'
        JOIN public.storage_migration_io_reads b ON b.operation_id = op.id
        JOIN public.storage_migration_read_publications pub ON pub.id = b.publication_id
            AND pub.revision = b.publication_revision AND pub.state = 'active'
        WHERE p_kind = 'read' AND op.id = p_operation
        UNION ALL
        SELECT op.id, NULL::uuid
        FROM public.storage_operations op
        JOIN public.storage_maintenance_gates g ON g.location_id = op.location_id
            AND g.revision = op.revision AND g.state = 'active'
        JOIN public.storage_migration_io_probes p ON p.operation_id = op.id
        JOIN public.storage_migration_read_publications pub ON pub.id = p.publication_id
            AND pub.state = 'active'
        WHERE p_kind = 'cutover' AND op.id = p_operation AND op.purpose = 'migration_cutover_probe'
    ), opened AS (
        INSERT INTO public.storage_migration_io_flights (spool_owner, operation_id, parent_operation_id)
        SELECT p_spool, id, parent_id FROM proof WHERE p_spool IS NOT NULL
        RETURNING id
    )
    SELECT id FROM opened
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_io_close_flight(p_spool uuid, p_flight uuid)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH closed AS (
        DELETE FROM public.storage_migration_io_flights f
        WHERE f.spool_owner = p_spool AND f.id = p_flight RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM closed)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_io_reconcile_flights(p_spool uuid)
RETURNS bigint
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH ended AS (
        DELETE FROM public.storage_migration_io_flights f
        WHERE f.spool_owner = p_spool RETURNING 1
    )
    SELECT count(*) FROM ended
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._migration_io_target(
    p_location uuid, p_identity jsonb, p_location_revision integer, p_connection_revision integer
) RETURNS jsonb
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT jsonb_build_object('location_id', l.id, 'location_revision', l.config_revision,
        'connection_revision', c.revision, 'bucket', c.config->>'bucket',
        'endpoint_url', p_identity->>'endpoint_url',
        'base_prefix', prefix.value, 'key_prefix', prefix.value || '_aap_migration/')
    FROM public.storage_locations l
    JOIN public.storage_connections c ON c.id = l.connection_id
    CROSS JOIN LATERAL (SELECT coalesce(c.config->>'base_prefix', '') AS raw) b
    CROSS JOIN LATERAL (SELECT CASE WHEN b.raw = '' OR right(b.raw, 1) = '/' THEN b.raw
        ELSE b.raw || '/' END AS value) prefix
    WHERE l.id = p_location AND l.enabled AND l.kind = 's3' AND c.kind = 's3'
      AND c.scope = 'global' AND c.project_id IS NULL
      AND l.config_revision = p_location_revision AND c.revision = p_connection_revision
      AND p_identity = jsonb_build_object('location_id', l.id, 'connection_id', c.id,
          'endpoint_url', rtrim(CASE WHEN position('://' IN c.config->>'endpoint') > 0
              THEN c.config->>'endpoint'
              ELSE CASE WHEN c.config->'use_ssl' = 'true'::jsonb THEN 'https://' ELSE 'http://' END
                   || (c.config->>'endpoint') END, '/'),
          'bucket', c.config->>'bucket', 'base_prefix', prefix.value,
          'key_prefix', prefix.value || '_aap_migration/')
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_io_copy(
    p_copy uuid, p_attempt uuid, p_task text, p_operation uuid, p_phase text,
    p_location_revision integer, p_connection_revision integer, p_object text
) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT jsonb_build_object('target', target.facts, 'baseline_sha256', c.baseline_sha256,
        'limits', c.limits, 'source', c.source, 'generation', c.generation,
        'allowlist', a.scope->'allowlist',
        'retry_failed', a.retry_failed, 'delta', d.data, 'delta_digest', d.digest,
        'delta_sequence', d.sequence, 'write_claimed', EXISTS (
            SELECT 1 FROM public.storage_migration_io_writes recorded
            WHERE recorded.attempt_id = a.id AND recorded.object_id = p_object))
    FROM public.storage_migration_copies c
    JOIN public.storage_migration_copy_attempts a ON a.id = c.last_attempt_id AND a.copy_id = c.id
    JOIN LATERAL yoao_jobs_v1.read_job(a.async_job_id) j ON j.id = a.async_job_id
    JOIN LATERAL yoao_identity_v1.read_actor_facts(a.requested_by) u
        ON u.actor_id = a.requested_by AND u.is_active AND u.platform_role = 'super_admin'
    JOIN public.storage_operations op ON op.id = a.target_operation_id
        AND op.location_id = c.target_location_id AND op.purpose = 'storage_migration_copy'
    JOIN public.storage_maintenance_gates gate ON gate.location_id = op.location_id
        AND gate.state = 'active' AND gate.revision = op.revision
    JOIN public.storage_operations src ON src.id = a.source_operation_id
        AND src.location_id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid
        AND src.purpose = 'storage_migration_copy'
    JOIN public.storage_maintenance_gates sg ON sg.location_id = src.location_id
        AND sg.revision = src.revision
    JOIN public.storage_locations sl ON sl.id = src.location_id AND sl.enabled
        AND sl.kind = 's3' AND sl.connection_id IS NULL
        AND to_jsonb(sl.config_revision) = a.scope->'source'
    LEFT JOIN public.storage_migration_windows w ON w.id = c.window_id
    LEFT JOIN public.storage_migration_copy_deltas d ON d.copy_id = c.id AND d.object_id = p_object
    CROSS JOIN LATERAL (SELECT yoao_assets_v1._migration_io_target(
        c.target_location_id, c.target, p_location_revision, p_connection_revision) AS facts) target
    WHERE c.id = p_copy AND a.id = p_attempt AND op.id = p_operation
      AND a.task_id = p_task AND p_phase IN ('run','verify') AND a.operation = p_phase
      AND a.status = 'running' AND a.cancel_requested_at IS NULL
      AND j.status = 'running' AND j.kind = 'storage_migration_copy'
      AND j.user_id = a.requested_by AND j.celery_task_id = a.task_id
      AND j.payload->>'copy_id' = c.id::text AND j.payload->>'operation' = a.operation
      AND (j.payload->>'cancel_requested') IS DISTINCT FROM 'true'
      AND a.scope->'target' = jsonb_build_object('location', p_location_revision, 'connection', p_connection_revision)
      AND a.scope->'maintenance'->op.location_id::text = to_jsonb(op.revision)
      AND a.scope->'maintenance'->src.location_id::text = to_jsonb(src.revision)
      AND (c.window_id IS NULL AND sg.state = 'active' OR
          c.window_id IS NOT NULL AND sg.state = 'stopped' AND w.state = 'stopped'
          AND w.revision = c.window_revision AND w.source_gate_revision = sg.revision
          AND w.parent_copy_id = c.parent_copy_id AND w.target = c.target)
      AND (a.authority->>'api_key_id' IS NULL OR EXISTS (
          SELECT 1 FROM yoao_identity_v1.read_accepted_api_key_facts(
              u.actor_id, (a.authority->>'api_key_id')::uuid) k
          WHERE '*' = ANY(k.scopes) AND k.revision = a.authority->>'key_proof'
      ))
      AND NOT EXISTS (SELECT 1 FROM public.storage_migration_copies child WHERE child.parent_copy_id = c.id)
      AND NOT EXISTS (SELECT 1 FROM public.storage_migration_finalizations f
          WHERE f.copy_id = c.id AND f.state IN ('sealing','sealed','invalidating'))
      AND (p_object IS NULL OR p_object ~ '^[0-9a-f]{64}$'
          AND (a.retry_object_ids IS NULL OR a.retry_object_ids ? p_object))
      AND target.facts IS NOT NULL AND c.baseline_sha256 IS NOT NULL
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_io_baseline(
    p_copy uuid, p_attempt uuid, p_task text, p_operation uuid, p_phase text,
    p_location_revision integer, p_connection_revision integer, p_digest text
) RETURNS bytea
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT c.baseline FROM public.storage_migration_copies c
    WHERE c.id = p_copy AND c.baseline_sha256 = p_digest
      AND octet_length(c.baseline) BETWEEN 1 AND 268435456
      AND yoao_assets_v1.migration_io_copy(p_copy, p_attempt, p_task, p_operation, p_phase,
          p_location_revision, p_connection_revision, NULL)->>'baseline_sha256' = p_digest
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_io_claim_write(
    p_copy uuid, p_attempt uuid, p_task text, p_operation uuid, p_phase text,
    p_location_revision integer, p_connection_revision integer, p_object text,
    p_sequence integer, p_nonce text
) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH proof AS MATERIALIZED (
        SELECT yoao_assets_v1.migration_io_copy(p_copy, p_attempt, p_task, p_operation,
            p_phase, p_location_revision, p_connection_revision, p_object) AS facts
    ), written AS (
        INSERT INTO public.storage_migration_io_writes (attempt_id, object_id, delta_sequence, nonce)
        SELECT p_attempt, p_object, p_sequence, p_nonce FROM proof
        WHERE p_phase = 'run' AND p_nonce ~ '^[0-9a-f]{48}$'
          AND facts->'delta_sequence' = to_jsonb(p_sequence)
          AND facts->'delta'->>'decision' = 'copy' AND facts->'delta'->>'state' = 'writing'
          AND facts->'delta'->>'id' = p_object AND facts->'delta'->>'nonce' = p_nonce
          AND facts->'delta'->>'receipt' IS NULL
        ON CONFLICT (attempt_id, object_id) DO NOTHING RETURNING true
    )
    SELECT jsonb_build_object('claimed', EXISTS (SELECT 1 FROM written))
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_io_bind_read(
    p_operation uuid, p_publication uuid, p_revision integer, p_request jsonb
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_members jsonb := p_request->'members';
    v_owner uuid := (p_request->>'owner_operation_id')::uuid;
    v_expected integer;
BEGIN
    IF jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request - ARRAY['members','owner_operation_id'] <> '{}'::jsonb
       OR jsonb_typeof(v_members) IS DISTINCT FROM 'object'
       OR octet_length(v_members::text) > 16777216 THEN
        RETURN false;
    END IF;
    SELECT count(*) INTO v_expected FROM jsonb_each(v_members);
    IF v_expected NOT BETWEEN 1 AND 100000 OR NOT EXISTS (
        SELECT 1 FROM public.storage_migration_read_publications pub
        JOIN public.storage_operations op ON op.id = p_operation AND op.location_id = pub.target_location_id
            AND op.purpose = 'migration_read_' || replace(pub.id::text, '-', '')
        JOIN public.storage_maintenance_gates g ON g.location_id = op.location_id
            AND g.revision = op.revision AND g.state = 'active'
        WHERE pub.id = p_publication AND pub.revision = p_revision AND pub.state = 'active'
          AND (v_owner IS NULL OR EXISTS (SELECT 1 FROM public.storage_operations parent
              WHERE parent.id = v_owner AND parent.location_id = op.location_id
                AND parent.revision = op.revision AND parent.id <> op.id AND parent.node_id = op.node_id))
    ) OR v_expected <> (
        SELECT count(*) FROM jsonb_each_text(v_members) member(id, version)
        JOIN public.storage_migration_read_routes r ON r.publication_id = p_publication
            AND r.source_object_id = member.id AND r.target_version_id = member.version
    ) THEN
        RETURN false;
    END IF;
    INSERT INTO public.storage_migration_io_reads VALUES
        (p_operation, p_publication, p_revision, v_members, v_owner)
    ON CONFLICT (operation_id) DO NOTHING;
    RETURN FOUND;
END
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_io_read(
    p_operation uuid, p_publication uuid, p_revision integer, p_object text,
    p_version text, p_location_revision integer, p_connection_revision integer, p_sign boolean
) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT jsonb_build_object('target', target.facts, 'key', r.target_object_key,
        'etag', r.target_etag, 'size', r.size_bytes, 'content_sha256', r.target_content_sha256,
        'plan', r.creation_plan_uuid, 'nonce', r.creation_nonce, 'sign_seconds', sign.seconds)
    FROM public.storage_migration_io_reads b
    JOIN public.storage_operations op ON op.id = b.operation_id
    JOIN public.storage_maintenance_gates g ON g.location_id = op.location_id
        AND g.revision = op.revision AND g.state = 'active'
    JOIN LATERAL yoao_assets_v1.migration_read_route_recheck(
        p_publication, p_revision, p_object, false) r ON r.target_location_id = op.location_id
    JOIN public.storage_locations sl ON sl.id = r.source_location_id AND sl.enabled
        AND sl.config_revision = r.source_location_revision
    CROSS JOIN LATERAL (SELECT yoao_assets_v1._migration_io_target(r.target_location_id,
        r.target_identity, p_location_revision, p_connection_revision) AS facts) target
    LEFT JOIN public.storage_operations owner_op ON owner_op.id = b.owner_operation_id
        AND owner_op.location_id = op.location_id AND owner_op.revision = op.revision
    LEFT JOIN LATERAL (
        SELECT floor(extract(epoch FROM e.expires_at - statement_timestamp()))::integer AS seconds
        FROM yoao_export_v1.retained_zip_download(
            CASE WHEN p_sign AND owner_op.purpose ~ '^export_read_[0-9a-f]{32}$'
                 THEN substring(owner_op.purpose FROM 13)::uuid END) e
        WHERE p_sign
          AND e.storage_ref->>'location_id' = r.source_location_id::text
          AND e.storage_ref->>'endpoint_url' = r.source_endpoint_url
          AND e.storage_ref->>'bucket' = r.source_bucket
          AND e.storage_ref->>'object_key' = r.source_object_key
          AND e.sha256 = r.sha256 AND e.size_bytes = r.size_bytes
          AND (e.version_id IS NULL OR e.version_id = r.source_version_id)
    ) sign ON true
    WHERE b.operation_id = p_operation AND b.publication_id = p_publication
      AND b.publication_revision = p_revision AND b.members->>p_object = p_version
      AND r.target_version_id = p_version AND target.facts IS NOT NULL
      AND (b.owner_operation_id IS NULL OR owner_op.id IS NOT NULL)
      AND (NOT p_sign OR sign.seconds > 0)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_io_cutover(
    p_publication uuid, p_revision integer, p_finalization_revision integer, p_window_revision integer,
    p_actor uuid, p_purpose text, p_generation integer, p_operation uuid,
    p_location_revision integer, p_connection_revision integer
) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    WITH proof AS MATERIALIZED (
        SELECT yoao_assets_v1._migration_io_target(pub.target_location_id,
            pub.target, p_location_revision, p_connection_revision) AS facts
        FROM public.storage_migration_read_publications pub
        JOIN public.storage_migration_finalizations f ON f.id = pub.id AND f.state = 'sealed'
            AND f.revision = pub.finalization_revision AND f.target = pub.target
            AND f.copy_id = pub.copy_id AND f.window_id = pub.window_id
        JOIN public.storage_migration_windows w ON w.id = pub.window_id AND w.state = 'stopped'
            AND w.revision = pub.window_revision AND w.revision = f.window_revision
        JOIN public.storage_maintenance_gates sg ON sg.location_id = pub.source_location_id
            AND sg.state = 'stopped' AND sg.revision = w.source_gate_revision
        JOIN public.storage_operations op ON op.id = p_operation AND op.purpose = 'migration_cutover_probe'
            AND op.location_id = pub.target_location_id
        JOIN public.storage_maintenance_gates g ON g.location_id = op.location_id
            AND g.state = 'active' AND g.revision = op.revision
        JOIN LATERAL yoao_identity_v1.read_actor_facts(p_actor) u
            ON u.actor_id = p_actor AND u.is_active AND u.platform_role = 'super_admin'
        JOIN public.storage_bindings b ON b.purpose = p_purpose AND b.generation = p_generation
            AND b.location_id = pub.source_location_id AND b.bucket = pub.source->'purpose_buckets'->>b.purpose
        WHERE pub.id = p_publication AND pub.state = 'active' AND pub.revision = p_revision
          AND f.revision = p_finalization_revision AND w.revision = p_window_revision
          AND p_purpose IN ('annotations','audit-archive','avatars','bug-reports','datasets','export','import','media-cache','training')
          AND NOT EXISTS (SELECT 1 FROM public.storage_migration_cutovers cut WHERE cut.id = pub.id)
          AND 9 = (SELECT count(*) FROM public.storage_bindings v
              WHERE pub.source->'purpose_buckets' ? v.purpose
                AND v.location_id = pub.source_location_id AND v.bucket = pub.source->'purpose_buckets'->>v.purpose)
    ), claimed AS (
        INSERT INTO public.storage_migration_io_probes
        SELECT p_operation, p_publication, p_purpose FROM proof WHERE facts IS NOT NULL
        ON CONFLICT (operation_id) DO NOTHING RETURNING operation_id
    )
    SELECT facts FROM proof CROSS JOIN claimed
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1._migration_io_target(uuid,jsonb,integer,integer),
    yoao_assets_v1.migration_io_copy(uuid,uuid,text,uuid,text,integer,integer,text),
    yoao_assets_v1.migration_io_baseline(uuid,uuid,text,uuid,text,integer,integer,text),
    yoao_assets_v1.migration_io_claim_write(uuid,uuid,text,uuid,text,integer,integer,text,integer,text),
    yoao_assets_v1.migration_io_bind_read(uuid,uuid,integer,jsonb),
    yoao_assets_v1.migration_io_read(uuid,uuid,integer,text,text,integer,integer,boolean),
    yoao_assets_v1.migration_io_open_flight(uuid,uuid,text),
    yoao_assets_v1.migration_io_close_flight(uuid,uuid),
    yoao_assets_v1.migration_io_reconcile_flights(uuid),
    yoao_assets_v1.migration_io_cutover(uuid,integer,integer,integer,uuid,text,integer,uuid,integer,integer)
FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON TABLE public.storage_migration_io_reads, public.storage_migration_io_writes,
    public.storage_migration_io_probes, public.storage_migration_io_flights FROM PUBLIC;
