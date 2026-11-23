CREATE TABLE yoao_assets_v1.reference_io_requests (
    permit_id uuid PRIMARY KEY REFERENCES public.storage_operations(id) ON DELETE CASCADE,
    action text NOT NULL CHECK (action IN ('probe', 'browse', 'directory', 'scan', 'observation', 'original')),
    actor_id uuid NOT NULL,
    key_id uuid,
    key_proof text,
    request jsonb NOT NULL CHECK (jsonb_typeof(request) = 'object' AND octet_length(request::text) <= 16384),
    expires_at timestamptz NOT NULL,
    claimed_at timestamptz
);
-- aap-statement-break
REVOKE ALL ON TABLE yoao_assets_v1.reference_io_requests FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._reference_io_scope(
    p_permit uuid, p_action text, p_actor uuid, p_key uuid, p_key_proof text, p_request jsonb
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_operation record;
    v_location record;
    v_actor record;
    v_source public.dataset_sources;
    v_run public.dataset_scan_runs;
    v_entry public.dataset_scan_entries;
    v_reference record;
    v_scope text;
    v_base text := '';
    v_path text;
    v_anchor jsonb;
    v_identity jsonb;
    v_expected jsonb;
    v_sha text;
    v_size bigint;
    v_cursor text;
    v_offset bigint;
    v_length bigint;
    v_task uuid;
    v_probe jsonb;
    v_result jsonb;
    v_common_fields text[] := ARRAY['location_revision', 'connection_revision'];
BEGIN
    IF p_actor IS NULL OR jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR octet_length(p_request::text) > 16384 THEN RETURN NULL; END IF;
    SELECT * INTO v_operation FROM yoao_assets_storage_v1.physical_io_receipt(p_permit);
    IF NOT FOUND OR v_operation.started_at <= clock_timestamp() - interval '30 seconds'
       OR NOT EXISTS (SELECT FROM yoao_assets_storage_v1.gate_facts(v_operation.location_id, NULL) AS gate
                      WHERE gate.state = 'active' AND gate.revision = v_operation.revision)
    THEN RETURN NULL; END IF;
    SELECT loc.id, loc.kind, loc.config_revision, loc.connection_id, conn.revision AS connection_revision
      INTO v_location
      FROM yoao_assets_storage_v1.location_metadata AS loc
      JOIN yoao_assets_storage_v1.connector_origins AS conn ON conn.id = loc.connection_id AND conn.kind = loc.kind
      WHERE loc.id = v_operation.location_id AND loc.enabled AND conn.secret_set
        AND loc.kind IN ('s3', 'sftp');
    IF NOT FOUND OR v_location.config_revision IS DISTINCT FROM (p_request ->> 'location_revision')::integer
       OR v_location.connection_revision IS DISTINCT FROM (p_request ->> 'connection_revision')::integer
    THEN RETURN NULL; END IF;
    SELECT * INTO v_actor FROM yoao_identity_v1.read_actor_facts(p_actor);
    IF NOT FOUND OR v_actor.is_active IS DISTINCT FROM true THEN RETURN NULL; END IF;

    IF p_action IN ('browse', 'probe', 'directory', 'scan', 'observation') THEN
        IF v_actor.platform_role NOT IN ('super_admin', 'project_admin') THEN RETURN NULL; END IF;
    END IF;
    IF p_request ->> 'run_id' IS NOT NULL AND p_action IN ('probe', 'scan', 'observation') THEN
        SELECT run.* INTO v_run FROM public.dataset_scan_runs AS run
          WHERE run.id = (p_request ->> 'run_id')::uuid;
        IF NOT FOUND THEN RETURN NULL; END IF;
    END IF;
    IF p_request ->> 'source_id' IS NOT NULL OR v_run.id IS NOT NULL THEN
        SELECT s.* INTO v_source FROM public.dataset_sources AS s
          WHERE s.id = coalesce((p_request ->> 'source_id')::uuid, v_run.source_id)
            AND s.location_id = v_location.id AND s.enabled AND s.unregistered_at IS NULL
            AND s.dataset_id IS NOT NULL;
        IF NOT FOUND OR (v_run.id IS NOT NULL AND v_run.source_id <> v_source.id)
           OR NOT yoao_assets_v1.dataset_source_visible(p_actor, v_source.id)
        THEN RETURN NULL; END IF;
    END IF;
    IF v_run.id IS NOT NULL THEN
        IF v_run.snapshot -> 'source_revision' IS DISTINCT FROM to_jsonb(v_source.config_revision)
           OR v_run.snapshot -> 'location_revision' IS DISTINCT FROM to_jsonb(v_location.config_revision)
           OR v_run.snapshot -> 'config' IS DISTINCT FROM v_source.config
        THEN RETURN NULL; END IF;
        v_anchor := v_run.snapshot -> 'anchor';
    END IF;

    CASE p_action
    WHEN 'browse' THEN
        IF v_operation.purpose <> 'browse'
           OR (p_request - (v_common_fields || ARRAY['path','cursor','limit','after','snapshot'])) <> '{}'::jsonb
           OR NOT yoao_assets_v1.dataset_location_visible(p_actor, v_location.id)
           OR jsonb_typeof(p_request -> 'path') IS DISTINCT FROM 'string'
           OR coalesce((p_request ->> 'limit')::integer, 0) NOT BETWEEN 1 AND 200
           OR octet_length(p_request ->> 'cursor') > 4096
           OR char_length(p_request ->> 'after') > 1000
           OR ((p_request ->> 'snapshot') IS NOT NULL AND p_request ->> 'snapshot' !~ '^[0-9a-f]{64}$')
           OR (v_location.kind = 's3' AND (p_request ->> 'after' IS NOT NULL OR p_request ->> 'snapshot' IS NOT NULL))
           OR (v_location.kind = 'sftp' AND p_request ->> 'cursor' IS NOT NULL)
        THEN RETURN NULL; END IF;
        v_scope := 'datasets:read';
        v_path := p_request ->> 'path';
        v_cursor := p_request ->> 'cursor';
        IF v_location.kind = 'sftp' THEN
            SELECT probe.result -> 'anchor' INTO v_anchor
            FROM yoao_assets_storage_v1.location_probe_facts(v_location.id, v_operation.node_id) AS probe;
        END IF;
        v_result := jsonb_build_object('limit', (p_request ->> 'limit')::integer,
                    'after', p_request ->> 'after', 'snapshot', p_request ->> 'snapshot');
    WHEN 'probe', 'directory' THEN
        IF v_operation.purpose <> (CASE p_action WHEN 'probe' THEN 'probe' ELSE 'registration_directory' END)
           OR (p_request - (v_common_fields || ARRAY['source_id','run_id','path_digest','path','schedule_revision','schedule_token'])) <> '{}'::jsonb
           OR (v_source.id IS NULL AND NOT yoao_assets_v1.dataset_location_visible(p_actor, v_location.id))
           OR (v_source.id IS NOT NULL AND p_request ? 'path')
           OR (p_action = 'directory' AND (v_source.id IS NULL OR p_request ? 'path_digest'))
           OR (v_run.id IS NOT NULL AND (v_run.status <> 'completed' OR NOT v_run.complete))
        THEN RETURN NULL; END IF;
        v_scope := 'datasets:write';
        IF p_request ? 'schedule_token' OR p_request ? 'schedule_revision' THEN
            IF NOT EXISTS (
                SELECT FROM public.dataset_source_sync_schedules AS schedule
                WHERE schedule.source_id = v_source.id AND schedule.actor_id = p_actor
                  AND schedule.enabled AND schedule.revision = (p_request ->> 'schedule_revision')::integer
                  AND schedule.dispatch_token = (p_request ->> 'schedule_token')::uuid
                  AND schedule.dispatch_expires_at > clock_timestamp()
                  AND schedule.api_key_id IS NOT DISTINCT FROM p_key
                  AND schedule.key_proof IS NOT DISTINCT FROM p_key_proof
            ) THEN RETURN NULL; END IF;
        END IF;
        IF v_source.id IS NULL THEN
            IF p_request ? 'path' AND jsonb_typeof(p_request -> 'path') NOT IN ('string', 'null')
            THEN RETURN NULL; END IF;
            v_path := p_request ->> 'path';
        ELSIF p_action = 'directory' THEN
            v_path := v_source.config ->> 'relative_path';
            SELECT probe.result -> 'anchor' INTO v_anchor
            FROM yoao_assets_storage_v1.location_probe_facts(v_location.id, v_operation.node_id) AS probe;
        ELSIF p_request ? 'path_digest' THEN
            IF v_run.status IS DISTINCT FROM 'completed' OR NOT v_run.complete THEN RETURN NULL; END IF;
            SELECT entry.* INTO v_entry FROM public.dataset_scan_entries AS entry
            WHERE entry.run_id = v_run.id AND entry.path_digest = p_request ->> 'path_digest'
              AND entry.kind = 'file' AND entry.state = 'observed';
            IF NOT FOUND THEN RETURN NULL; END IF;
            v_base := v_source.config ->> 'relative_path';
            v_path := v_entry.relative_path;
            v_expected := coalesce(v_entry.verified_content -> 'observation', v_entry.observed);
        END IF;
    WHEN 'scan' THEN
        IF v_operation.purpose <> 'scan'
           OR (p_request - (v_common_fields || ARRAY['run_id','job_id','task_id','generation','lease_token','path_digest','pages'])) <> '{}'::jsonb
           OR v_run.status IS DISTINCT FROM 'running' OR v_run.generation <> v_source.generation
           OR v_run.generation IS DISTINCT FROM (p_request ->> 'generation')::integer
           OR v_run.lease_token IS DISTINCT FROM (p_request ->> 'lease_token')::uuid
           OR v_run.lease_token IS NULL OR v_run.lease_expires_at IS NULL OR v_run.lease_expires_at <= clock_timestamp()
           OR v_run.async_job_id IS DISTINCT FROM (p_request ->> 'job_id')::uuid
           OR coalesce(v_run.snapshot ->> 'maintenance_revision', '1')::integer <> v_operation.revision
           OR v_operation.node_id IS DISTINCT FROM v_source.config ->> 'node_id'
           OR v_run.snapshot ->> 'api_key_id' IS DISTINCT FROM p_key::text
           OR v_run.snapshot ->> 'key_proof' IS DISTINCT FROM p_key_proof
           OR NOT EXISTS (SELECT FROM public.datasets AS d WHERE d.id = v_source.dataset_id
                          AND d.archived_at IS NULL AND d.lifecycle_revision =
                              coalesce(v_run.snapshot ->> 'dataset_lifecycle_revision', '1')::integer)
           OR NOT EXISTS (SELECT FROM yoao_jobs_v1.read_job(v_run.async_job_id) AS job
                          WHERE job.user_id = p_actor AND job.status = 'running'
                            AND job.celery_task_id = p_request ->> 'task_id')
        THEN RETURN NULL; END IF;
        v_scope := 'datasets:write';
        IF v_location.kind = 's3' THEN
            SELECT entry.* INTO v_entry FROM public.dataset_scan_entries AS entry
            WHERE entry.run_id = v_run.id AND entry.relative_path = ''
              AND entry.kind = 'directory' AND entry.state = 'pending' AND v_run.phase = 'enumerating';
            IF NOT FOUND OR coalesce((v_entry.observed #>> '{checkpoint,pages}')::integer, 0) >= 1000
               OR coalesce((v_entry.observed #>> '{checkpoint,pages}')::integer, 0)
                  IS DISTINCT FROM (p_request ->> 'pages')::integer
            THEN RETURN NULL; END IF;
            v_path := v_source.config ->> 'relative_path';
            v_cursor := v_entry.observed #>> '{checkpoint,cursor}';
        ELSE
            SELECT entry.* INTO v_entry FROM public.dataset_scan_entries AS entry
            WHERE entry.run_id = v_run.id AND entry.kind = 'directory' AND (
                (v_run.phase = 'enumerating' AND entry.state = 'pending') OR
                (v_run.phase IN ('resume_validation','validating') AND entry.state = 'captured'
                    AND entry.validated_generation <> v_run.generation)
            ) ORDER BY entry.path_digest LIMIT 1;
            IF NOT FOUND OR v_entry.path_digest IS DISTINCT FROM p_request ->> 'path_digest' THEN RETURN NULL; END IF;
            v_path := concat_ws('/', nullif(v_source.config ->> 'relative_path', ''), nullif(v_entry.relative_path, ''));
            v_identity := coalesce(v_entry.observed -> 'identity',
                                   CASE WHEN v_entry.relative_path = '' THEN v_source.root_identity END);
        END IF;
        v_result := jsonb_build_object('phase', v_run.phase, 'generation', v_run.generation,
                                      'path_digest', v_entry.path_digest, 'checkpoint', v_entry.observed -> 'checkpoint');
    WHEN 'observation' THEN
        IF v_operation.purpose <> 'scan_content'
           OR (p_request - (v_common_fields || ARRAY['run_id','path_digest','verify','refresh','registration_id','job_id'])) <> '{}'::jsonb
           OR v_run.status IS DISTINCT FROM 'completed' OR NOT v_run.complete
           OR coalesce(v_run.snapshot ->> 'maintenance_revision', '1')::integer <> v_operation.revision
           OR jsonb_typeof(p_request -> 'verify') IS DISTINCT FROM 'boolean'
        THEN RETURN NULL; END IF;
        SELECT entry.* INTO v_entry FROM public.dataset_scan_entries AS entry
        WHERE entry.run_id = v_run.id AND entry.path_digest = p_request ->> 'path_digest'
          AND entry.kind = 'file' AND entry.state = 'observed';
        IF NOT FOUND THEN RETURN NULL; END IF;
        v_scope := CASE WHEN (p_request ->> 'verify')::boolean THEN 'datasets:write' ELSE 'datasets:read' END;
        IF v_entry.verified_content IS NULL AND v_scope = 'datasets:read' THEN RETURN NULL; END IF;
        v_base := v_source.config ->> 'relative_path';
        v_path := v_entry.relative_path;
        v_expected := coalesce(v_entry.verified_content -> 'observation', v_entry.observed);
        v_sha := v_entry.verified_content ->> 'sha256';
        IF p_request ->> 'registration_id' IS NOT NULL THEN
            IF NOT EXISTS (
                SELECT FROM public.dataset_registration_runs AS r
                JOIN public.dataset_registration_entries AS e ON e.run_id = r.id AND e.path_digest = v_entry.path_digest
                JOIN public.datasets AS d ON d.id = v_source.dataset_id AND d.archived_at IS NULL
                CROSS JOIN LATERAL yoao_jobs_v1.read_job(r.async_job_id) AS job
                WHERE r.id = (p_request ->> 'registration_id')::uuid AND r.source_id = v_source.id
                  AND r.scan_run_id = v_run.id AND r.async_job_id = (p_request ->> 'job_id')::uuid
                  AND r.status = 'running' AND r.lease_token IS NOT NULL AND r.lease_expires_at > clock_timestamp()
                  AND job.status = 'running' AND job.user_id = p_actor
                  AND r.snapshot ->> 'actor_id' = p_actor::text
                  AND r.snapshot ->> 'api_key_id' IS NOT DISTINCT FROM p_key::text
                  AND r.snapshot ->> 'key_proof' IS NOT DISTINCT FROM p_key_proof
                  AND v_operation.node_id = 'worker:' || (r.snapshot ->> 'node_id')
                  AND d.lifecycle_revision = coalesce(r.snapshot ->> 'dataset_lifecycle_revision', '1')::integer
                  AND ((r.phase = 'preflight' AND e.state = 'pending') OR (r.phase = 'registering' AND e.state = 'ready'))
                  AND (NOT coalesce((p_request ->> 'refresh')::boolean, false)
                       OR (r.phase = 'preflight' AND e.refresh_unverified AND v_scope = 'datasets:write'))
            ) THEN RETURN NULL; END IF;
        ELSIF coalesce((p_request ->> 'refresh')::boolean, false) THEN RETURN NULL;
        END IF;
        IF coalesce((p_request ->> 'refresh')::boolean, false) AND v_entry.verified_content IS NULL THEN
            v_expected := NULL;
        END IF;
        SELECT probe.result INTO v_probe FROM yoao_assets_storage_v1.location_probe_facts(v_location.id, v_operation.node_id) AS probe
          WHERE probe.config_revision = v_location.config_revision;
        IF v_probe -> 'ok' IS DISTINCT FROM 'true'::jsonb
           OR v_probe -> 'anchor' IS DISTINCT FROM v_anchor
           OR (v_probe #>> ARRAY['capabilities', CASE WHEN v_operation.node_id LIKE 'worker:%' THEN 'list' ELSE 'read' END]) IS DISTINCT FROM 'verified'
        THEN RETURN NULL; END IF;
    WHEN 'original' THEN
        IF v_operation.purpose NOT IN ('reference_read','quality_read')
           OR (p_request - (v_common_fields || ARRAY['item_id','asset_id','revision_id','task_id','scope','sha256','source_revision','run_id','path_digest','offset','length'])) <> '{}'::jsonb
        THEN RETURN NULL; END IF;
        SELECT * INTO v_reference FROM yoao_assets_v1.directory_reference_content((p_request ->> 'item_id')::uuid);
        IF NOT FOUND OR v_reference.location_id IS DISTINCT FROM v_location.id
           OR v_reference.asset_id IS DISTINCT FROM (p_request ->> 'asset_id')::uuid
           OR v_reference.accepted_revision_id IS DISTINCT FROM (p_request ->> 'revision_id')::uuid
           OR v_reference.source_revision IS DISTINCT FROM (p_request ->> 'source_revision')::integer
           OR v_reference.run_id IS DISTINCT FROM (p_request ->> 'run_id')::uuid
           OR v_reference.path_digest IS DISTINCT FROM p_request ->> 'path_digest'
           OR v_reference.sha256 IS DISTINCT FROM p_request ->> 'sha256'
           OR v_reference.sha256 IS NULL OR v_reference.sha256 !~ '^[0-9a-f]{64}$'
           OR v_reference.verification_state IS DISTINCT FROM 'verified' OR v_reference.checksum_algorithm IS DISTINCT FROM 'sha256'
           OR v_reference.source_enabled IS DISTINCT FROM true OR v_reference.source_unregistered_at IS NOT NULL
           OR v_reference.dataset_id IS DISTINCT FROM v_reference.source_dataset_id OR v_reference.dataset_id IS NULL
           OR v_reference.bound_run_matches IS DISTINCT FROM true OR v_reference.run_matches IS DISTINCT FROM true
           OR to_jsonb(v_reference.bound_source_revision) IS DISTINCT FROM v_reference.bound_run_source_revision
           OR to_jsonb(v_reference.bound_location_revision) IS DISTINCT FROM v_reference.bound_run_location_revision
           OR v_reference.bound_sha256 IS DISTINCT FROM v_reference.sha256
           OR v_reference.bound_size IS DISTINCT FROM to_jsonb(v_reference.size_bytes)
           OR v_reference.origin_anchor IS DISTINCT FROM v_reference.bound_anchor
           OR (v_reference.accepted_revision_id IS NOT NULL AND v_reference.accepted_sha256 IS DISTINCT FROM v_reference.sha256)
           OR v_reference.file_path IS DISTINCT FROM '__aap_reference__/' || (p_request ->> 'item_id') || '/' || v_reference.sha256
           OR v_reference.content_receipt ->> 'sha256' IS DISTINCT FROM v_reference.sha256
           OR v_reference.content_receipt -> 'size_bytes' IS DISTINCT FROM to_jsonb(v_reference.size_bytes)
           OR (v_reference.source_config - ARRAY['recursive','include_globs','exclude_globs']) IS DISTINCT FROM
              (v_reference.run_config - ARRAY['recursive','include_globs','exclude_globs'])
           OR (v_reference.run_config - ARRAY['recursive','include_globs','exclude_globs']) IS DISTINCT FROM
              (v_reference.bound_config - ARRAY['recursive','include_globs','exclude_globs'])
        THEN RETURN NULL; END IF;
        v_task := (p_request ->> 'task_id')::uuid;
        v_scope := p_request ->> 'scope';
        IF v_scope IS NULL OR v_scope NOT IN ('datasets:read','datasets:write','tasks:read','tasks:write','annotations:read','annotations:write')
           OR (v_operation.purpose = 'quality_read' AND (v_scope <> 'datasets:write' OR v_task IS NOT NULL))
        THEN RETURN NULL; END IF;
        IF v_task IS NULL THEN
            IF NOT yoao_assets_v1.dataset_source_visible(p_actor, v_reference.source_id) THEN RETURN NULL; END IF;
        ELSIF (yoao_labeling_v1.read_media_task_access(p_actor, v_task, (p_request ->> 'item_id')::uuid) -> 'ok') IS DISTINCT FROM 'true'::jsonb
           OR NOT EXISTS (SELECT FROM yoao_labeling_v1.task_rows AS task WHERE task.id = v_task AND task.file_path = v_reference.file_path)
        THEN RETURN NULL; END IF;
        v_base := v_reference.source_config ->> 'relative_path';
        v_path := v_reference.relative_path;
        v_expected := v_reference.content_receipt -> 'observation';
        v_sha := v_reference.sha256;
        v_size := v_reference.size_bytes;
        v_anchor := v_reference.origin_anchor;
        IF jsonb_typeof(v_expected) IS DISTINCT FROM 'object' OR jsonb_typeof(v_anchor) IS DISTINCT FROM 'object'
           OR NOT EXISTS (
               SELECT FROM yoao_assets_storage_v1.location_probe_facts(v_location.id, v_operation.node_id) AS probe
               WHERE probe.result -> 'anchor' = v_anchor
           ) THEN RETURN NULL; END IF;
        IF p_request ? 'offset' OR p_request ? 'length' THEN
            v_offset := (p_request ->> 'offset')::bigint;
            v_length := (p_request ->> 'length')::bigint;
            IF v_offset IS NULL OR v_length IS NULL OR v_offset < 0 OR v_length <= 0
               OR v_offset >= v_size OR v_length > v_size - v_offset THEN RETURN NULL; END IF;
        END IF;
        v_result := jsonb_build_object('accepted_revision_id', v_reference.accepted_revision_id,
                     'source_revision', v_reference.source_revision, 'run_id', v_reference.run_id,
                     'path_digest', v_reference.path_digest, 'offset', v_offset, 'length', v_length);
    ELSE RETURN NULL;
    END CASE;

    IF p_key IS NULL THEN
        IF p_key_proof IS NOT NULL THEN RETURN NULL; END IF;
    ELSIF p_key_proof IS NULL OR NOT EXISTS (
        SELECT FROM yoao_identity_v1.read_accepted_api_key_facts(p_actor, p_key) AS key
        WHERE key.revision = p_key_proof AND (v_scope = ANY(key.scopes) OR '*' = ANY(key.scopes))
    ) THEN RETURN NULL; END IF;
    IF v_base IS NULL OR char_length(v_base) > 1000 OR char_length(v_path) > 1024
       OR (v_path IS NULL AND p_action <> 'probe')
    THEN RETURN NULL; END IF;
    RETURN coalesce(v_result, '{}'::jsonb) || jsonb_build_object(
        'connection_id', v_location.connection_id, 'connection_revision', v_location.connection_revision,
        'kind', v_location.kind, 'location_id', v_location.id, 'location_revision', v_location.config_revision,
        'source_revision', coalesce(v_source.config_revision, (v_result ->> 'source_revision')::integer),
        'base', v_base, 'path', v_path, 'anchor', v_anchor, 'expected_identity', v_identity,
        'expected_observation', v_expected, 'sha256', v_sha, 'size', v_size, 'cursor', v_cursor);
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR lock_not_available THEN
    RETURN NULL;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.prepare_reference_io(
    p_permit uuid, p_action text, p_actor uuid, p_key uuid, p_key_proof text, p_request jsonb
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF yoao_assets_v1._reference_io_scope(p_permit, p_action, p_actor, p_key, p_key_proof, p_request) IS NULL THEN
        RETURN false;
    END IF;
    INSERT INTO yoao_assets_v1.reference_io_requests (permit_id, action, actor_id, key_id, key_proof, request, expires_at)
    VALUES (p_permit, p_action, p_actor, p_key, p_key_proof, p_request, clock_timestamp() + interval '30 seconds')
    ON CONFLICT DO NOTHING;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.capture_reference_io(p_permit uuid, p_action text)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET lock_timeout = '1s'
AS $function$
DECLARE
    v_request yoao_assets_v1.reference_io_requests;
BEGIN
    UPDATE yoao_assets_v1.reference_io_requests
    SET claimed_at = clock_timestamp()
    WHERE permit_id = p_permit AND action = p_action AND claimed_at IS NULL AND expires_at > clock_timestamp()
    RETURNING * INTO v_request;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN yoao_assets_v1._reference_io_scope(p_permit, p_action, v_request.actor_id,
            v_request.key_id, v_request.key_proof, v_request.request);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.recheck_reference_io(p_permit uuid, p_action text)
RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT yoao_assets_v1._reference_io_scope(p_permit, p_action, req.actor_id, req.key_id, req.key_proof, req.request)
    FROM yoao_assets_v1.reference_io_requests AS req
    WHERE req.permit_id = p_permit AND req.action = p_action AND req.claimed_at IS NOT NULL AND req.expires_at > clock_timestamp()
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._reference_io_scope(uuid,text,uuid,uuid,text,jsonb),
    yoao_assets_v1.prepare_reference_io(uuid,text,uuid,uuid,text,jsonb),
    yoao_assets_v1.capture_reference_io(uuid,text), yoao_assets_v1.recheck_reference_io(uuid,text) FROM PUBLIC;
