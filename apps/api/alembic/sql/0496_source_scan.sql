CREATE FUNCTION yoao_assets_v1._lock_dataset_directory_source(p_source_id uuid)
RETURNS public.dataset_sources
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_location_id uuid;
    v_source public.dataset_sources;
BEGIN
    SELECT source.location_id INTO v_location_id
    FROM public.dataset_sources AS source WHERE source.id = p_source_id;
    PERFORM 1 FROM public.storage_locations AS location
    WHERE location.id = v_location_id FOR UPDATE;
    SELECT source.* INTO v_source FROM public.dataset_sources AS source
    WHERE source.id = p_source_id FOR UPDATE;
    RETURN v_source;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._lock_dataset_directory_source(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_dataset_directory_source(p_source_id uuid)
RETURNS TABLE (
    id uuid, dataset_id uuid, location_id uuid, name text, config jsonb,
    config_revision integer, enabled boolean, generation integer, root_identity jsonb,
    ownership_mode text, cache_policy text, policy_revision integer,
    external_protection_revision integer, external_protection jsonb,
    unregistered_at timestamptz, created_at timestamptz
)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT source.id, source.dataset_id, source.location_id, source.name::text,
           source.config, source.config_revision, source.enabled, source.generation,
           source.root_identity, source.ownership_mode::text, source.cache_policy::text,
           source.policy_revision, source.external_protection_revision,
           source.external_protection, source.unregistered_at, source.created_at
    FROM yoao_assets_v1._lock_dataset_directory_source(p_source_id) AS source
    WHERE source.id IS NOT NULL
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_dataset_directory_source(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.create_dataset_directory_source(
    p_actor_id uuid, p_dataset_id uuid, p_location_id uuid, p_name text,
    p_ownership_mode text, p_cache_policy text, p_config jsonb
)
RETURNS uuid
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.dataset_sources (
        id, dataset_id, location_id, name, ownership_mode, cache_policy, config
    )
    SELECT pg_catalog.gen_random_uuid(), d.id, l.id, p_name, p_ownership_mode,
           p_cache_policy, pg_catalog.jsonb_build_object(
               'relative_path', p_config -> 'relative_path', 'node_id', p_config -> 'node_id',
               'recursive', p_config -> 'recursive', 'include_globs', p_config -> 'include_globs',
               'exclude_globs', p_config -> 'exclude_globs'
           )
    FROM public.datasets AS d
    CROSS JOIN public.storage_locations AS l
    JOIN yoao_identity_v1.read_actor_facts(p_actor_id) AS actor ON actor.is_active
    WHERE d.id = p_dataset_id AND d.archived_at IS NULL
      AND l.id = p_location_id AND l.enabled
      AND (l.kind IN ('filesystem', 'agent') OR
           (l.kind IN ('s3', 'sftp') AND l.connection_id IS NOT NULL))
      AND (actor.platform_role = 'super_admin' OR
           (actor.platform_role = 'project_admin' AND d.created_by = p_actor_id))
      AND yoao_assets_v1.dataset_location_visible(p_actor_id, p_location_id)
    RETURNING id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.create_dataset_directory_source(uuid, uuid, uuid, text, text, text, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.edit_dataset_directory_source(
    p_actor_id uuid, p_source_id uuid, p_revision integer, p_name text,
    p_enabled boolean, p_cache_policy text
)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source public.dataset_sources;
BEGIN
    v_source := yoao_assets_v1._lock_dataset_directory_source(p_source_id);
    IF v_source.id IS NULL OR NOT yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id) THEN
        RETURN 'not_found';
    END IF;
    IF v_source.config_revision IS DISTINCT FROM p_revision THEN
        RETURN 'revision_changed';
    END IF;
    IF v_source.unregistered_at IS NOT NULL THEN
        RETURN 'unregistered';
    END IF;
    IF p_enabled IS NOT NULL AND p_enabled <> v_source.enabled THEN
        RETURN 'lifecycle_impact_required';
    END IF;
    IF EXISTS (SELECT 1 FROM public.dataset_scan_runs AS run
               WHERE run.source_id = p_source_id AND run.status IN ('pending', 'running')) THEN
        RETURN 'scan_active';
    END IF;
    IF EXISTS (SELECT 1 FROM public.dataset_registration_runs AS run
               WHERE run.source_id = p_source_id AND run.status IN ('pending', 'running')) THEN
        RETURN 'registration_active';
    END IF;
    UPDATE public.dataset_sources AS source
    SET name = COALESCE(p_name, source.name),
        cache_policy = COALESCE(p_cache_policy, source.cache_policy),
        policy_revision = source.policy_revision +
            CASE WHEN p_cache_policy IS NOT NULL AND p_cache_policy <> source.cache_policy THEN 1 ELSE 0 END,
        config_revision = source.config_revision + 1
    WHERE source.id = p_source_id;
    RETURN 'updated';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.edit_dataset_directory_source(uuid, uuid, integer, text, boolean, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_dataset_scan(p_run_id uuid)
RETURNS TABLE (
    id uuid, source_id uuid, source_revision integer, async_job_id uuid, status text,
    phase text, complete boolean, generation integer, error_code text, counts jsonb,
    created_at timestamptz, completed_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT run.id, run.source_id, COALESCE((run.snapshot ->> 'source_revision')::integer, 0),
           run.async_job_id, run.status::text, run.phase::text, run.complete, run.generation,
           run.error_code::text, run.counts, run.created_at, run.completed_at
    FROM public.dataset_scan_runs AS run WHERE run.id = p_run_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.read_dataset_scan(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_scan_snapshot(p_run_id uuid)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT run.snapshot - ARRAY['api_key_id', 'key_proof']::text[]
    FROM public.dataset_scan_runs AS run WHERE run.id = p_run_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_scan_snapshot(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.list_dataset_scans(
    p_actor_id uuid, p_source_id uuid, p_complete boolean, p_active_only boolean,
    p_limit integer, p_offset integer
)
RETURNS TABLE (items jsonb, total bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH matching AS MATERIALIZED (
        SELECT run.id, run.source_id, COALESCE((run.snapshot ->> 'source_revision')::integer, 0) AS source_revision,
               run.async_job_id, run.status, run.phase, run.complete, run.generation,
               run.error_code, run.counts, run.created_at, run.completed_at
        FROM public.dataset_scan_runs AS run
        WHERE run.source_id = p_source_id
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id)
          AND (p_complete IS NULL OR run.complete = p_complete)
          AND (NOT p_active_only OR run.status IN ('pending', 'running'))
    ), page AS (
        SELECT * FROM matching
        ORDER BY CASE WHEN p_complete THEN completed_at END DESC NULLS LAST, created_at DESC, id
        LIMIT LEAST(p_limit, 200) OFFSET p_offset
    )
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(page)
              ORDER BY CASE WHEN p_complete THEN page.completed_at END DESC NULLS LAST,
                       page.created_at DESC, page.id), '[]'::jsonb),
           (SELECT pg_catalog.count(*) FROM matching)
    FROM page
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.list_dataset_scans(uuid, uuid, boolean, boolean, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._lock_dataset_scan(p_run_id uuid)
RETURNS public.dataset_scan_runs
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source_id uuid;
    v_job_id uuid;
    v_run public.dataset_scan_runs;
BEGIN
    SELECT run.source_id INTO v_source_id FROM public.dataset_scan_runs AS run WHERE run.id = p_run_id;
    IF v_source_id IS NULL THEN
        RETURN NULL;
    END IF;
    PERFORM yoao_assets_v1._lock_dataset_directory_source(v_source_id);
    SELECT run.async_job_id INTO v_job_id FROM public.dataset_scan_runs AS run WHERE run.id = p_run_id;
    IF v_job_id IS NOT NULL THEN
        PERFORM job.id FROM yoao_jobs_v1.read_job_for_update(v_job_id) AS job;
    END IF;
    SELECT run.* INTO v_run FROM public.dataset_scan_runs AS run WHERE run.id = p_run_id FOR UPDATE;
    RETURN v_run;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._lock_dataset_scan(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_dataset_scan_execution(p_run_id uuid, p_lock boolean)
RETURNS TABLE (
    id uuid, source_id uuid, async_job_id uuid, snapshot jsonb, status text, phase text,
    complete boolean, generation integer, lease_token uuid, lease_expires_at timestamptz,
    error_code text, counts jsonb, created_at timestamptz, completed_at timestamptz
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        PERFORM yoao_assets_v1._lock_dataset_scan(p_run_id);
    END IF;
    RETURN QUERY
        SELECT run.id, run.source_id, run.async_job_id, run.snapshot, run.status::text,
               run.phase::text, run.complete, run.generation, run.lease_token,
               run.lease_expires_at, run.error_code::text, run.counts, run.created_at,
               run.completed_at
        FROM public.dataset_scan_runs AS run WHERE run.id = p_run_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.read_dataset_scan_execution(uuid, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.active_dataset_scan(p_source_id uuid, p_except_run_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT run.id FROM public.dataset_scan_runs AS run
    WHERE run.source_id = p_source_id AND run.status IN ('pending', 'running')
      AND run.id IS DISTINCT FROM p_except_run_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.active_dataset_scan(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_scan_for_job(p_job_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT run.id FROM public.dataset_scan_runs AS run WHERE run.async_job_id = p_job_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_scan_for_job(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_scan_dispatch_route(p_run_id uuid)
RETURNS TABLE (task_id text, node_id text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT job.celery_task_id::text, source.config ->> 'node_id'
    FROM public.dataset_scan_runs AS run
    JOIN public.dataset_sources AS source ON source.id = run.source_id
    CROSS JOIN LATERAL yoao_jobs_v1.read_job(run.async_job_id) AS job
    WHERE run.id = p_run_id AND job.kind = 'dataset_scan'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_scan_dispatch_route(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.start_dataset_scan(
    p_actor_id uuid, p_source_id uuid, p_snapshot jsonb
)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source public.dataset_sources;
    v_run_id uuid := pg_catalog.gen_random_uuid();
BEGIN
    v_source := yoao_assets_v1._lock_dataset_directory_source(p_source_id);
    IF v_source.id IS NULL OR NOT v_source.enabled OR v_source.unregistered_at IS NOT NULL
        OR v_source.dataset_id IS NULL
        OR NOT yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id) THEN
        RETURN NULL;
    END IF;
    INSERT INTO public.dataset_scan_runs (id, source_id, snapshot)
    VALUES (v_run_id, p_source_id, pg_catalog.jsonb_build_object(
        'source_revision', v_source.config_revision, 'config', v_source.config,
        'location_revision', p_snapshot -> 'location_revision',
        'maintenance_revision', p_snapshot -> 'maintenance_revision',
        'dataset_lifecycle_revision', p_snapshot -> 'dataset_lifecycle_revision',
        'anchor', p_snapshot -> 'anchor', 'scan_host_id', p_snapshot -> 'scan_host_id',
        'budget', pg_catalog.jsonb_build_object('max_entries', 50000, 'max_directories', 5000)
    ));
    INSERT INTO public.dataset_scan_entries (run_id, path_digest, relative_path, kind, state, observed)
    VALUES (v_run_id, pg_catalog.encode(pg_catalog.sha256(''::bytea), 'hex'), '', 'directory', 'pending', '{}'::jsonb);
    RETURN v_run_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.start_dataset_scan(uuid, uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.bind_dataset_scan_attempt(
    p_run_id uuid, p_previous_job_id uuid, p_generation integer, p_job_id uuid,
    p_api_key_id uuid, p_key_proof text
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_scan_runs;
BEGIN
    v_run := yoao_assets_v1._lock_dataset_scan(p_run_id);
    IF v_run.id IS NULL OR v_run.async_job_id IS DISTINCT FROM p_previous_job_id
        OR v_run.generation IS DISTINCT FROM p_generation OR v_run.complete
        OR (v_run.status = 'running' AND v_run.lease_expires_at > pg_catalog.clock_timestamp())
        OR NOT EXISTS (
            SELECT 1 FROM yoao_jobs_v1.read_job_for_update(p_job_id) AS job
            JOIN public.dataset_sources AS source ON source.id = v_run.source_id
            WHERE job.kind = 'dataset_scan' AND job.status = 'pending'
              AND job.celery_task_id IS NOT NULL AND job.id IS DISTINCT FROM p_previous_job_id
              AND job.payload ->> 'run_id' = p_run_id::text
              AND job.payload ->> 'source_id' = source.id::text
              AND job.payload ->> 'dataset_id' = source.dataset_id::text
              AND yoao_assets_v1.dataset_source_visible(job.user_id, source.id)
        ) THEN
        RETURN false;
    END IF;
    UPDATE public.dataset_scan_runs AS run
    SET async_job_id = p_job_id, status = 'pending', error_code = NULL, complete = false,
        completed_at = NULL, lease_token = NULL, lease_expires_at = NULL,
        snapshot = run.snapshot || pg_catalog.jsonb_build_object('api_key_id', p_api_key_id, 'key_proof', p_key_proof)
    WHERE run.id = p_run_id;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.bind_dataset_scan_attempt(uuid, uuid, integer, uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.claim_dataset_scan(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_node_id text, p_host_id text
)
RETURNS timestamptz
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_scan_runs;
    v_generation integer;
BEGIN
    v_run := yoao_assets_v1._lock_dataset_scan(p_run_id);
    IF v_run.id IS NULL OR v_run.status <> 'pending' OR p_job_id IS NULL
        OR v_run.async_job_id IS DISTINCT FROM p_job_id OR p_task_id IS NULL
        OR NOT EXISTS (
            SELECT 1 FROM yoao_jobs_v1.read_job(p_job_id) AS job
            WHERE job.kind = 'dataset_scan' AND job.status = 'pending'
              AND job.celery_task_id = p_task_id
              AND yoao_assets_v1.dataset_source_visible(job.user_id, v_run.source_id)
        ) THEN
        RETURN NULL;
    END IF;
    UPDATE public.dataset_sources AS source SET generation = source.generation + 1
    WHERE source.id = v_run.source_id AND source.config ->> 'node_id' = p_node_id
    RETURNING source.generation INTO v_generation;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    UPDATE public.dataset_scan_runs AS run
    SET snapshot = run.snapshot || pg_catalog.jsonb_build_object('scan_host_id', p_host_id),
        generation = v_generation, lease_token = pg_catalog.gen_random_uuid(),
        lease_expires_at = pg_catalog.clock_timestamp() + interval '60 seconds',
        status = 'running', phase = 'resume_validation'
    WHERE run.id = p_run_id;
    RETURN pg_catalog.clock_timestamp();
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.claim_dataset_scan(uuid, uuid, text, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.settle_dataset_scan(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_lease_token uuid,
    p_previous_status text, p_status text, p_code text
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_scan_runs;
    v_at timestamptz;
    v_complete boolean := p_status = 'completed';
BEGIN
    v_run := yoao_assets_v1._lock_dataset_scan(p_run_id);
    IF v_run.id IS NULL OR v_run.status NOT IN ('pending', 'running')
        OR v_run.status IS DISTINCT FROM p_previous_status
        OR v_run.async_job_id IS DISTINCT FROM p_job_id
        OR v_run.generation IS DISTINCT FROM p_generation
        OR v_run.lease_token IS DISTINCT FROM p_lease_token
        OR p_status IS NULL OR p_status NOT IN ('completed', 'failed', 'cancelled') THEN
        RETURN false;
    END IF;
    IF v_complete THEN
        IF v_run.status <> 'running' OR p_code IS NOT NULL THEN
            RETURN false;
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.dataset_sources AS source
            JOIN public.storage_locations AS location ON location.id = source.location_id
            WHERE source.id = v_run.source_id AND location.kind = 's3'
        ) THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.dataset_scan_entries AS entry
                WHERE entry.run_id = p_run_id AND entry.relative_path = ''
                  AND entry.kind = 'directory' AND entry.state = 'captured'
            ) THEN
                RETURN false;
            END IF;
        ELSIF v_run.phase <> 'validating' OR EXISTS (
            SELECT 1 FROM public.dataset_scan_entries AS entry
            WHERE entry.run_id = p_run_id AND entry.kind = 'directory'
              AND (entry.state = 'pending' OR
                   (entry.state = 'captured' AND entry.validated_generation <> p_generation))
        ) THEN
            RETURN false;
        END IF;
    END IF;
    v_at := pg_catalog.clock_timestamp();
    IF p_job_id IS NOT NULL AND NOT yoao_jobs_v1.settle_dataset_scan_job(
        p_job_id, p_task_id, p_status, v_at, p_code,
        v_run.counts || pg_catalog.jsonb_build_object('complete', v_complete, 'error_code', p_code, 'run_id', v_run.id)
    ) THEN
        RETURN false;
    END IF;
    UPDATE public.dataset_scan_runs AS run
    SET status = p_status, error_code = p_code, complete = v_complete, completed_at = v_at,
        lease_token = NULL, lease_expires_at = NULL
    WHERE run.id = p_run_id;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.settle_dataset_scan(uuid, uuid, text, integer, uuid, text, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._running_dataset_scan(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_lease_token uuid
)
RETURNS public.dataset_scan_runs
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_scan_runs;
BEGIN
    v_run := yoao_assets_v1._lock_dataset_scan(p_run_id);
    IF v_run.id IS NULL OR v_run.status <> 'running'
        OR p_job_id IS NULL OR v_run.async_job_id IS DISTINCT FROM p_job_id
        OR p_lease_token IS NULL OR v_run.lease_token IS DISTINCT FROM p_lease_token
        OR v_run.generation IS DISTINCT FROM p_generation
        OR v_run.lease_expires_at IS NULL OR v_run.lease_expires_at <= pg_catalog.clock_timestamp()
        OR NOT EXISTS (
            SELECT 1 FROM yoao_jobs_v1.read_job(p_job_id) AS job
            JOIN public.dataset_sources AS source ON source.id = v_run.source_id
            WHERE job.kind = 'dataset_scan' AND job.status = 'running'
              AND job.celery_task_id = p_task_id AND source.generation = p_generation
              AND yoao_assets_v1.dataset_source_visible(job.user_id, source.id)
        ) THEN
        RETURN NULL;
    END IF;
    RETURN v_run;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._running_dataset_scan(uuid, uuid, text, integer, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.next_dataset_scan_directory(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_lease_token uuid
)
RETURNS TABLE (outcome text, path_digest text, relative_path text, observed jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_scan_runs;
    v_entry public.dataset_scan_entries;
BEGIN
    v_run := yoao_assets_v1._running_dataset_scan(p_run_id, p_job_id, p_task_id, p_generation, p_lease_token);
    IF v_run.id IS NULL THEN
        RETURN QUERY SELECT 'stale'::text, NULL::text, NULL::text, NULL::jsonb;
        RETURN;
    END IF;
    SELECT entry.* INTO v_entry FROM public.dataset_scan_entries AS entry
    WHERE entry.run_id = p_run_id AND entry.kind = 'directory'
      AND CASE WHEN v_run.phase IN ('resume_validation', 'validating')
          THEN entry.state = 'captured' AND entry.validated_generation <> p_generation
          ELSE entry.state = 'pending' END
    ORDER BY entry.path_digest LIMIT 1;
    IF FOUND THEN
        UPDATE public.dataset_scan_runs AS run
        SET lease_expires_at = pg_catalog.clock_timestamp() + interval '60 seconds'
        WHERE run.id = p_run_id;
        RETURN QUERY SELECT 'work'::text, v_entry.path_digest::text, v_entry.relative_path, v_entry.observed;
    ELSIF v_run.phase = 'validating' THEN
        RETURN QUERY SELECT 'done'::text, NULL::text, NULL::text, NULL::jsonb;
    ELSE
        UPDATE public.dataset_scan_runs AS run
        SET phase = CASE WHEN v_run.phase = 'resume_validation' THEN 'enumerating' ELSE 'validating' END
        WHERE run.id = p_run_id;
        IF v_run.phase <> 'resume_validation' THEN
            UPDATE public.dataset_scan_entries AS entry SET validated_generation = 0
            WHERE entry.run_id = p_run_id AND entry.kind = 'directory';
        END IF;
        RETURN QUERY SELECT 'transition'::text, NULL::text, NULL::text, NULL::jsonb;
    END IF;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.next_dataset_scan_directory(uuid, uuid, text, integer, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_dataset_scan_directory(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_lease_token uuid,
    p_path_digest text
)
RETURNS TABLE (relative_path text, state text, observed jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_scan_runs;
BEGIN
    v_run := yoao_assets_v1._running_dataset_scan(p_run_id, p_job_id, p_task_id, p_generation, p_lease_token);
    IF v_run.id IS NULL THEN
        RETURN;
    END IF;
    RETURN QUERY SELECT entry.relative_path, entry.state::text, entry.observed
    FROM public.dataset_scan_entries AS entry
    WHERE entry.run_id = p_run_id AND entry.path_digest = p_path_digest AND entry.kind = 'directory';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.read_dataset_scan_directory(uuid, uuid, text, integer, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.validate_dataset_scan_directory(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_lease_token uuid,
    p_phase text, p_path_digest text, p_directory_digest text
)
RETURNS TABLE (outcome text, progress_count bigint)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_scan_runs;
    v_entry public.dataset_scan_entries;
BEGIN
    v_run := yoao_assets_v1._running_dataset_scan(p_run_id, p_job_id, p_task_id, p_generation, p_lease_token);
    IF v_run.id IS NULL OR v_run.phase IS DISTINCT FROM p_phase
        OR v_run.phase NOT IN ('resume_validation', 'validating') THEN
        RETURN QUERY SELECT 'stale'::text, NULL::bigint;
        RETURN;
    END IF;
    SELECT entry.* INTO v_entry FROM public.dataset_scan_entries AS entry
    WHERE entry.run_id = p_run_id AND entry.path_digest = p_path_digest
      AND entry.kind = 'directory' AND entry.state = 'captured';
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'stale'::text, NULL::bigint;
    ELSIF v_entry.directory_digest IS DISTINCT FROM p_directory_digest THEN
        RETURN QUERY SELECT 'directory_changed'::text, NULL::bigint;
    ELSIF v_entry.validated_generation = p_generation THEN
        RETURN QUERY SELECT 'accepted'::text, NULL::bigint;
    ELSE
        UPDATE public.dataset_scan_entries AS entry SET validated_generation = p_generation
        WHERE entry.run_id = p_run_id AND entry.path_digest = p_path_digest;
        RETURN QUERY SELECT 'accepted'::text, pg_catalog.count(*)
        FROM public.dataset_scan_entries AS entry
        WHERE entry.run_id = p_run_id AND entry.kind = 'directory' AND entry.state = 'captured'
          AND entry.validated_generation = p_generation;
    END IF;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.validate_dataset_scan_directory(uuid, uuid, text, integer, uuid, text, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.capture_dataset_scan_directory(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_lease_token uuid,
    p_path_digest text, p_directory_digest text, p_identity jsonb, p_entries jsonb
)
RETURNS TABLE (outcome text, counts jsonb, progress_count bigint)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_scan_runs;
    v_entry public.dataset_scan_entries;
    v_root_identity jsonb;
    v_count bigint;
    v_directories bigint;
    v_counts jsonb;
    v_progress bigint;
BEGIN
    v_run := yoao_assets_v1._running_dataset_scan(p_run_id, p_job_id, p_task_id, p_generation, p_lease_token);
    IF v_run.id IS NULL OR v_run.phase <> 'enumerating' THEN
        RETURN QUERY SELECT 'stale'::text, NULL::jsonb, NULL::bigint;
        RETURN;
    END IF;
    SELECT entry.* INTO v_entry FROM public.dataset_scan_entries AS entry
    WHERE entry.run_id = p_run_id AND entry.path_digest = p_path_digest
      AND entry.kind = 'directory' AND entry.state = 'pending';
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'stale'::text, NULL::jsonb, NULL::bigint;
        RETURN;
    END IF;
    SELECT pg_catalog.count(*), pg_catalog.count(*) FILTER (WHERE entry.kind = 'directory')
    INTO v_count, v_directories FROM public.dataset_scan_entries AS entry WHERE entry.run_id = p_run_id;
    IF v_count - 1 + pg_catalog.jsonb_array_length(p_entries) > (v_run.snapshot #>> '{budget,max_entries}')::integer
        OR v_directories + (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_array_elements(p_entries) AS child
                            WHERE child ->> 'kind' = 'directory') > (v_run.snapshot #>> '{budget,max_directories}')::integer THEN
        RETURN QUERY SELECT 'scan_budget_exceeded'::text, NULL::jsonb, NULL::bigint;
        RETURN;
    END IF;
    IF v_entry.relative_path = '' THEN
        SELECT NULLIF(source.root_identity, 'null'::jsonb) INTO v_root_identity
        FROM public.dataset_sources AS source WHERE source.id = v_run.source_id;
        IF v_root_identity IS NOT NULL AND v_root_identity IS DISTINCT FROM p_identity THEN
            RETURN QUERY SELECT 'directory_identity_changed'::text, NULL::jsonb, NULL::bigint;
            RETURN;
        END IF;
        UPDATE public.dataset_sources AS source SET root_identity = p_identity WHERE source.id = v_run.source_id;
    END IF;
    INSERT INTO public.dataset_scan_entries (run_id, path_digest, relative_path, kind, state, observed)
    SELECT p_run_id, child.path_digest, child.relative_path, child.kind, child.state, child.observed
    FROM pg_catalog.jsonb_to_recordset(p_entries) AS child(path_digest text, relative_path text, kind text, state text, observed jsonb)
    ON CONFLICT (run_id, path_digest) DO NOTHING;
    UPDATE public.dataset_scan_entries AS entry
    SET state = 'captured', observed = pg_catalog.jsonb_build_object('identity', p_identity),
        directory_digest = p_directory_digest
    WHERE entry.run_id = p_run_id AND entry.path_digest = p_path_digest;
    SELECT pg_catalog.jsonb_build_object(
               'processed_count', pg_catalog.count(*) FILTER (WHERE entry.relative_path <> ''),
               'file_count', pg_catalog.count(*) FILTER (WHERE entry.kind = 'file' AND entry.state = 'observed' AND entry.relative_path <> ''),
               'directory_count', pg_catalog.count(*) FILTER (WHERE entry.kind = 'directory' AND entry.relative_path <> ''),
               'skipped_count', pg_catalog.count(*) FILTER (WHERE entry.state = 'skipped' AND entry.relative_path <> '')
           ), pg_catalog.count(*) FILTER (WHERE entry.kind = 'directory' AND entry.state = 'captured')
    INTO v_counts, v_progress FROM public.dataset_scan_entries AS entry WHERE entry.run_id = p_run_id;
    UPDATE public.dataset_scan_runs AS run SET counts = v_counts,
        lease_expires_at = pg_catalog.clock_timestamp() + interval '60 seconds'
    WHERE run.id = p_run_id;
    RETURN QUERY SELECT 'accepted'::text, v_counts, v_progress;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.capture_dataset_scan_directory(uuid, uuid, text, integer, uuid, text, text, jsonb, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.open_dataset_scan_s3_page(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_lease_token uuid
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_scan_runs;
BEGIN
    v_run := yoao_assets_v1._running_dataset_scan(p_run_id, p_job_id, p_task_id, p_generation, p_lease_token);
    IF v_run.id IS NULL THEN
        RETURN false;
    END IF;
    UPDATE public.dataset_scan_runs AS run SET phase = 'enumerating',
        lease_expires_at = pg_catalog.clock_timestamp() + interval '60 seconds'
    WHERE run.id = p_run_id;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.open_dataset_scan_s3_page(uuid, uuid, text, integer, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.capture_dataset_scan_s3_page(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_lease_token uuid,
    p_cursor text, p_pages integer, p_next_cursor text, p_listed_count integer, p_entries jsonb
)
RETURNS TABLE (outcome text, counts jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_scan_runs;
    v_root_digest text := pg_catalog.encode(pg_catalog.sha256(''::bytea), 'hex');
    v_checkpoint jsonb;
    v_tokens jsonb;
    v_digest text;
    v_listed integer;
    v_counts jsonb;
BEGIN
    v_run := yoao_assets_v1._running_dataset_scan(p_run_id, p_job_id, p_task_id, p_generation, p_lease_token);
    IF v_run.id IS NULL OR v_run.phase <> 'enumerating' THEN
        RETURN QUERY SELECT 'stale'::text, NULL::jsonb;
        RETURN;
    END IF;
    SELECT entry.observed -> 'checkpoint' INTO v_checkpoint FROM public.dataset_scan_entries AS entry
    WHERE entry.run_id = p_run_id AND entry.path_digest = v_root_digest
      AND entry.kind = 'directory' AND entry.state = 'pending';
    IF NOT FOUND OR v_checkpoint ->> 'cursor' IS DISTINCT FROM p_cursor
        OR COALESCE((v_checkpoint ->> 'pages')::integer, 0) IS DISTINCT FROM p_pages THEN
        RETURN QUERY SELECT 'stale'::text, NULL::jsonb;
        RETURN;
    END IF;
    v_tokens := COALESCE(v_checkpoint -> 'tokens', '[]'::jsonb);
    IF NULLIF(p_next_cursor, '') IS NOT NULL THEN
        v_digest := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_next_cursor, 'UTF8')), 'hex');
        IF v_tokens ? v_digest THEN
            RETURN QUERY SELECT 's3_invalid_cursor'::text, NULL::jsonb;
            RETURN;
        END IF;
        v_tokens := v_tokens || pg_catalog.jsonb_build_array(v_digest);
    END IF;
    v_listed := COALESCE((v_checkpoint ->> 'listed')::integer, 0) + p_listed_count;
    IF v_listed > (v_run.snapshot #>> '{budget,max_entries}')::integer THEN
        RETURN QUERY SELECT 'scan_budget_exceeded'::text, NULL::jsonb;
        RETURN;
    END IF;
    INSERT INTO public.dataset_scan_entries (run_id, path_digest, relative_path, kind, state, observed)
    SELECT p_run_id, child.path_digest, child.relative_path, 'file', child.state, child.observed
    FROM pg_catalog.jsonb_to_recordset(p_entries) AS child(path_digest text, relative_path text, state text, observed jsonb)
    ON CONFLICT (run_id, path_digest) DO NOTHING;
    UPDATE public.dataset_scan_entries AS entry
    SET state = CASE WHEN NULLIF(p_next_cursor, '') IS NULL THEN 'captured' ELSE 'pending' END,
        observed = pg_catalog.jsonb_build_object('checkpoint', pg_catalog.jsonb_build_object(
            'cursor', p_next_cursor, 'pages', p_pages + 1, 'tokens', v_tokens, 'listed', v_listed
        ))
    WHERE entry.run_id = p_run_id AND entry.path_digest = v_root_digest;
    SELECT pg_catalog.jsonb_build_object(
               'processed_count', pg_catalog.count(*),
               'file_count', pg_catalog.count(*) FILTER (WHERE entry.state = 'observed'),
               'skipped_count', pg_catalog.count(*) FILTER (WHERE entry.state = 'skipped'),
               'directory_count', 0
           ) INTO v_counts FROM public.dataset_scan_entries AS entry
    WHERE entry.run_id = p_run_id AND entry.kind = 'file';
    UPDATE public.dataset_scan_runs AS run SET counts = v_counts WHERE run.id = p_run_id;
    RETURN QUERY SELECT 'accepted'::text, v_counts;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.capture_dataset_scan_s3_page(uuid, uuid, text, integer, uuid, text, integer, text, integer, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_observed_dataset_scan_file(
    p_actor_id uuid, p_run_id uuid, p_path_digest text, p_lock boolean
)
RETURNS TABLE (
    run_id uuid, path_digest text, relative_path text, kind text, state text,
    observed jsonb, verified_content jsonb, directory_digest text, validated_generation integer
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.dataset_scan_runs AS run
        WHERE run.id = p_run_id AND run.complete AND run.status = 'completed'
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, run.source_id)
    ) THEN
        RETURN;
    END IF;
    IF p_lock THEN
        PERFORM 1 FROM public.dataset_scan_entries AS entry
        WHERE entry.run_id = p_run_id AND entry.path_digest = p_path_digest FOR UPDATE;
    END IF;
    RETURN QUERY SELECT entry.run_id, entry.path_digest::text, entry.relative_path,
                        entry.kind::text, entry.state::text, entry.observed,
                        entry.verified_content, entry.directory_digest::text, entry.validated_generation
    FROM public.dataset_scan_entries AS entry
    WHERE entry.run_id = p_run_id AND entry.path_digest = p_path_digest
      AND entry.kind = 'file' AND entry.state = 'observed';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.read_observed_dataset_scan_file(uuid, uuid, text, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.verify_observed_dataset_scan_file(
    p_actor_id uuid, p_run_id uuid, p_path_digest text, p_receipt jsonb
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_scan_runs;
BEGIN
    v_run := yoao_assets_v1._lock_dataset_scan(p_run_id);
    IF v_run.id IS NULL OR NOT v_run.complete OR v_run.status <> 'completed'
        OR NOT yoao_assets_v1.dataset_source_visible(p_actor_id, v_run.source_id) THEN
        RETURN false;
    END IF;
    UPDATE public.dataset_scan_entries AS entry SET verified_content = p_receipt
    WHERE entry.run_id = p_run_id AND entry.path_digest = p_path_digest
      AND entry.kind = 'file' AND entry.state = 'observed' AND entry.verified_content IS NULL;
    RETURN FOUND;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.verify_observed_dataset_scan_file(uuid, uuid, text, jsonb) FROM PUBLIC;
