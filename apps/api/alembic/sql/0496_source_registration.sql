CREATE FUNCTION yoao_assets_v1.read_directory_registration(p_run_id uuid, p_lock boolean)
RETURNS TABLE (
    id uuid, scan_run_id uuid, source_id uuid, async_job_id uuid, snapshot jsonb,
    phase varchar, status varchar, generation integer, lease_token uuid,
    lease_expires_at timestamptz, error_code varchar, counts jsonb,
    created_at timestamptz, completed_at timestamptz, comparison_complete boolean
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        PERFORM 1 FROM public.dataset_registration_runs AS r WHERE r.id = p_run_id FOR UPDATE;
    END IF;
    RETURN QUERY SELECT r.id, r.scan_run_id, r.source_id, r.async_job_id, r.snapshot,
        r.phase, r.status, r.generation, r.lease_token, r.lease_expires_at,
        r.error_code, r.counts, r.created_at, r.completed_at, r.comparison_complete
    FROM public.dataset_registration_runs AS r WHERE r.id = p_run_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.read_directory_registration(uuid, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_registration_binding(p_run_id uuid)
RETURNS TABLE (source_id uuid, job_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.source_id, r.async_job_id FROM public.dataset_registration_runs AS r WHERE r.id = p_run_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_registration_binding(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_registration_for_scan(p_scan_id uuid)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.id FROM public.dataset_registration_runs AS r WHERE r.scan_run_id = p_scan_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_registration_for_scan(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_registration_for_job(p_job_id uuid)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.id FROM public.dataset_registration_runs AS r WHERE r.async_job_id = p_job_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_registration_for_job(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.active_directory_registration(p_source_id uuid, p_except_id uuid)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.id FROM public.dataset_registration_runs AS r
    WHERE r.source_id = p_source_id AND r.status IN ('pending', 'running')
      AND r.id IS DISTINCT FROM p_except_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.active_directory_registration(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_registration_probe_paths(p_scan_id uuid)
RETURNS SETOF text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT e.relative_path FROM public.dataset_scan_entries AS e
    WHERE e.run_id = p_scan_id AND e.kind = 'file' AND e.state = 'observed'
    ORDER BY e.path_digest LIMIT 8
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_registration_probe_paths(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.create_directory_preflight(
    p_run_id uuid, p_scan_id uuid, p_actor_id uuid, p_cache_policy text
)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source public.dataset_sources;
    v_scan public.dataset_scan_runs;
    v_location_revision integer;
    v_total bigint;
    v_size bigint;
    v_existing bigint;
BEGIN
    SELECT s.* INTO v_scan FROM public.dataset_scan_runs AS s WHERE s.id = p_scan_id;
    SELECT s.* INTO v_source FROM public.dataset_sources AS s WHERE s.id = v_scan.source_id FOR UPDATE;
    SELECT l.config_revision INTO v_location_revision FROM public.storage_locations AS l
    WHERE l.id = v_source.location_id;
    IF yoao_assets_v1.dataset_source_visible(p_actor_id, v_source.id) IS NOT TRUE THEN
        RETURN 'access_revoked';
    END IF;
    IF v_scan.status IS DISTINCT FROM 'completed' OR v_scan.complete IS NOT TRUE THEN
        RETURN 'scan_incomplete';
    END IF;
    IF NOT v_source.enabled OR v_source.unregistered_at IS NOT NULL
       OR v_source.dataset_id IS NULL
       OR (v_scan.snapshot ->> 'source_revision')::integer IS DISTINCT FROM v_source.config_revision
       OR (v_scan.snapshot ->> 'location_revision')::integer IS DISTINCT FROM v_location_revision THEN
        RETURN 'configuration_changed';
    END IF;
    IF EXISTS (SELECT 1 FROM public.dataset_source_items AS h WHERE h.source_id = v_source.id OFFSET 50000) THEN
        RETURN 'comparison_budget_exceeded';
    END IF;
    SELECT pg_catalog.count(*), coalesce(pg_catalog.sum((e.observed ->> 'size')::bigint), 0)::bigint
    INTO v_total, v_size FROM public.dataset_scan_entries AS e
    WHERE e.run_id = p_scan_id AND e.kind = 'file' AND e.state = 'observed';

    INSERT INTO public.dataset_registration_runs
        (id, scan_run_id, source_id, snapshot, phase, status, generation, counts)
    VALUES (p_run_id, p_scan_id, v_source.id, pg_catalog.jsonb_build_object(
        'source_revision', v_source.config_revision,
        'location_revision', v_location_revision,
        'maintenance_revision', coalesce(v_scan.snapshot -> 'maintenance_revision', '1'::jsonb),
        'dataset_lifecycle_revision', coalesce(v_scan.snapshot -> 'dataset_lifecycle_revision', '1'::jsonb),
        'node_id', v_source.config -> 'node_id', 'ownership_mode', v_source.ownership_mode,
        'cache_policy', p_cache_policy, 'policy_revision', v_source.policy_revision, 'comparison_schema', 1
    ), 'preflight', 'pending', 0, pg_catalog.jsonb_build_object(
        'indexed_count', v_total, 'indexed_bytes', v_size, 'pending_count', v_total,
        'ready_count', 0, 'registered_count', 0, 'unchanged_count', 0, 'error_count', 0,
        'skipped_count', coalesce(v_scan.counts -> 'skipped_count', '0'::jsonb),
        'original_copy_bytes', 0, 'preview_estimated_bytes', 0, 'preview_estimated_count', 0,
        'preview_estimate_error_count', 0, 'original_estimated_bytes', 0,
        'preview_retained_bytes', 0, 'preview_failed_count', 0
    ));

    INSERT INTO public.dataset_registration_entries (run_id, path_digest, state, item_id, attempts, refresh_unverified)
    SELECT p_run_id, e.path_digest, 'pending', h.dataset_item_id, 0, false
    FROM public.dataset_scan_entries AS e
    LEFT JOIN public.dataset_source_items AS h ON h.source_id = v_source.id AND h.path_digest = e.path_digest
    WHERE e.run_id = p_scan_id AND e.kind = 'file' AND e.state = 'observed';

    SELECT pg_catalog.count(*) INTO v_existing FROM public.dataset_registration_entries AS e
    WHERE e.run_id = p_run_id AND e.item_id IS NOT NULL;
    UPDATE public.dataset_registration_runs AS r SET counts = r.counts || pg_catalog.jsonb_build_object(
        'existing_count', v_existing, 'estimated_new_count', v_total - v_existing
    ) WHERE r.id = p_run_id;

    INSERT INTO public.dataset_source_changes (run_id, path_digest, relative_path, baseline_run_id,
        before_item_id, before_revision_id, before_sha256, before_observation, before_anchor, kind, reason)
    SELECT p_run_id, h.path_digest, old.relative_path, h.run_id, h.dataset_item_id,
        h.current_revision_id, h.content_sha256, old.verified_content -> 'observation',
        prior.snapshot -> 'anchor',
        CASE WHEN candidate.kind = 'file' AND candidate.state = 'observed' THEN 'pending'
             WHEN candidate.path_digest IS NOT NULL THEN 'excluded' ELSE 'not_seen' END,
        CASE WHEN candidate.kind = 'file' AND candidate.state = 'observed' THEN NULL
             WHEN candidate.path_digest IS NOT NULL THEN 'not_a_candidate' ELSE 'not_enumerated' END
    FROM public.dataset_source_items AS h
    JOIN public.dataset_scan_entries AS old ON old.run_id = h.run_id AND old.path_digest = h.path_digest
    JOIN public.dataset_scan_runs AS prior ON prior.id = h.run_id
    LEFT JOIN public.dataset_scan_entries AS candidate ON candidate.run_id = p_scan_id AND candidate.path_digest = h.path_digest
    WHERE h.source_id = v_source.id;

    INSERT INTO public.dataset_source_changes (run_id, path_digest, relative_path, kind)
    SELECT p_run_id, e.path_digest, e.relative_path, 'pending' FROM public.dataset_scan_entries AS e
    WHERE e.run_id = p_scan_id AND e.kind = 'file' AND e.state = 'observed'
    ON CONFLICT (run_id, path_digest) DO NOTHING;
    RETURN NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.create_directory_preflight(uuid, uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_preflight_unseen(p_run_id uuid)
RETURNS TABLE (path_digest varchar, relative_path text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT c.path_digest, c.relative_path FROM public.dataset_source_changes AS c
    JOIN public.dataset_registration_runs AS r ON r.id = c.run_id
    WHERE r.id = p_run_id AND r.phase = 'preflight' AND r.status = 'pending'
      AND r.async_job_id IS NULL AND r.generation = 0 AND c.kind = 'not_seen'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_preflight_unseen(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_preflight_exclude_unseen(p_run_id uuid, p_digests text[])
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.dataset_source_changes AS c SET kind = 'excluded', reason = 'outside_selection_rules'
    FROM public.dataset_registration_runs AS r
    WHERE r.id = p_run_id AND r.phase = 'preflight' AND r.status = 'pending'
      AND r.async_job_id IS NULL AND r.generation = 0
      AND c.run_id = r.id AND c.kind = 'not_seen' AND c.path_digest = ANY(p_digests)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_preflight_exclude_unseen(uuid, text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.open_directory_registration_attempt(
    p_run_id uuid, p_previous_job uuid, p_job_id uuid, p_actor_id uuid, p_key_id uuid, p_key_proof text
)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH attached AS (
        UPDATE public.dataset_registration_runs AS r
        SET snapshot = r.snapshot || pg_catalog.jsonb_build_object('actor_id', p_actor_id, 'api_key_id', p_key_id, 'key_proof', p_key_proof),
            comparison_complete = CASE WHEN r.phase = 'preflight' THEN false ELSE r.comparison_complete END,
            async_job_id = p_job_id, status = 'pending', error_code = NULL, completed_at = NULL,
            lease_token = NULL, lease_expires_at = pg_catalog.clock_timestamp()
        FROM yoao_jobs_v1.read_job(p_job_id) AS j
        WHERE r.id = p_run_id AND r.async_job_id IS NOT DISTINCT FROM p_previous_job
          AND j.kind = 'dataset_registration' AND j.status = 'pending' AND j.user_id = p_actor_id
          AND j.payload ->> 'registration_id' = r.id::text AND j.payload ->> 'stage' = r.phase
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, r.source_id)
          AND (r.status <> 'running' OR r.lease_expires_at IS NULL OR r.lease_expires_at <= pg_catalog.clock_timestamp())
        RETURNING 1
    ) SELECT EXISTS (SELECT 1 FROM attached)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.open_directory_registration_attempt(uuid, uuid, uuid, uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._directory_registration_fenced(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_token uuid
)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH job AS MATERIALIZED (
        SELECT j.id FROM yoao_jobs_v1.read_job_for_update(p_job_id) AS j
        WHERE j.celery_task_id = p_task_id AND j.kind = 'dataset_registration' AND j.status = 'running'
          AND j.payload ->> 'registration_id' = p_run_id::text
    )
    SELECT EXISTS (
        SELECT 1 FROM job
        JOIN public.dataset_registration_runs AS r ON r.async_job_id = job.id
        WHERE r.id = p_run_id AND r.status = 'running' AND r.generation = p_generation
          AND r.lease_token = p_token AND r.lease_expires_at > pg_catalog.clock_timestamp()
        FOR UPDATE OF r
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._directory_registration_fenced(uuid, uuid, text, integer, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._transition_directory_registration_entry(
    p_run_id uuid, p_digest text, p_expected text, p_state text, p_error text, p_item_id uuid
)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_entry public.dataset_registration_entries;
    v_run public.dataset_registration_runs;
    v_counts jsonb;
    v_drop_estimate boolean;
BEGIN
    SELECT r.* INTO v_run FROM public.dataset_registration_runs AS r WHERE r.id = p_run_id FOR UPDATE;
    SELECT e.* INTO v_entry FROM public.dataset_registration_entries AS e
    WHERE e.run_id = p_run_id AND e.path_digest = p_digest AND e.state = p_expected FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    v_counts := v_run.counts;
    v_drop_estimate := v_run.phase = 'preflight' AND p_expected = 'ready'
                       AND p_state NOT IN ('ready', 'registered', 'unchanged');
    IF v_drop_estimate THEN
        v_counts := v_counts || pg_catalog.jsonb_build_object(
            'preview_estimated_bytes', (v_counts ->> 'preview_estimated_bytes')::bigint - coalesce(v_entry.preview_estimated_bytes, 0),
            'preview_estimated_count', coalesce((v_counts ->> 'preview_estimated_count')::bigint, 0) - (v_entry.preview_estimated_bytes IS NOT NULL)::int,
            'preview_estimate_error_count', coalesce((v_counts ->> 'preview_estimate_error_count')::bigint, 0) - (v_entry.preview_error IS NOT NULL)::int,
            'original_estimated_bytes', coalesce((v_counts ->> 'original_estimated_bytes')::bigint, 0) - v_entry.original_estimated_bytes
        );
    END IF;
    IF p_expected <> p_state THEN
        v_counts := v_counts || pg_catalog.jsonb_build_object(
            p_expected || '_count', (v_counts ->> (p_expected || '_count'))::bigint - 1,
            p_state || '_count', (v_counts ->> (p_state || '_count'))::bigint + 1
        );
    END IF;
    UPDATE public.dataset_registration_entries AS e
    SET state = p_state, error_code = p_error, item_id = coalesce(p_item_id, e.item_id), attempts = e.attempts + 1,
        preview_estimated_bytes = CASE WHEN v_drop_estimate THEN NULL ELSE e.preview_estimated_bytes END,
        preview_error = CASE WHEN v_drop_estimate THEN NULL ELSE e.preview_error END,
        original_estimated_bytes = CASE WHEN v_drop_estimate THEN 0 ELSE e.original_estimated_bytes END
    WHERE e.run_id = p_run_id AND e.path_digest = p_digest;
    UPDATE public.dataset_registration_runs AS r SET counts = v_counts WHERE r.id = p_run_id;
    RETURN v_entry.attempts + 1;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._transition_directory_registration_entry(uuid, text, text, text, text, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.claim_directory_registration(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_node text, p_token uuid
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_stamp timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.dataset_registration_runs AS r
        WHERE r.id = p_run_id AND r.async_job_id = p_job_id AND r.status = 'pending'
          AND r.snapshot ->> 'node_id' = p_node) THEN RETURN false; END IF;
    IF NOT yoao_jobs_v1.transition_directory_registration_job(
        p_job_id, p_run_id, p_task_id, 'claim', NULL, v_stamp, NULL, NULL
    ) THEN RETURN false; END IF;
    UPDATE public.dataset_registration_runs AS r
    SET generation = r.generation + 1, lease_token = p_token,
        lease_expires_at = v_stamp + interval '60 seconds', status = 'running', error_code = NULL
    WHERE r.id = p_run_id AND r.async_job_id = p_job_id AND r.status = 'pending';
    RETURN FOUND;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.claim_directory_registration(uuid, uuid, text, text, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.next_directory_registration_entry(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_token uuid
)
RETURNS TABLE (path_digest varchar, refresh_unverified boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT yoao_assets_v1._directory_registration_fenced(p_run_id, p_job_id, p_task_id, p_generation, p_token) THEN
        RETURN QUERY SELECT NULL::varchar, false;
        RETURN;
    END IF;
    RETURN QUERY SELECT e.path_digest, e.refresh_unverified
    FROM public.dataset_registration_entries AS e JOIN public.dataset_registration_runs AS r ON r.id = e.run_id
    WHERE e.run_id = p_run_id AND e.state = CASE WHEN r.phase = 'preflight' THEN 'pending' ELSE 'ready' END
    ORDER BY e.path_digest LIMIT 1;
    IF FOUND THEN
        UPDATE public.dataset_registration_runs AS r SET lease_expires_at = pg_catalog.clock_timestamp() + interval '60 seconds'
        WHERE r.id = p_run_id;
    END IF;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.next_directory_registration_entry(uuid, uuid, text, integer, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_directory_registration_entry(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_token uuid,
    p_digest text, p_state text, p_error text, p_item_id uuid, p_preview_estimate bigint,
    p_preview_error text, p_original_size bigint, p_copy_bytes bigint, p_preview_bytes bigint,
    p_preview_failed boolean
)
RETURNS TABLE (attempts integer, state text, rejected text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_registration_runs;
    v_entry public.dataset_registration_entries;
    v_change public.dataset_source_changes;
    v_expected text;
    v_receipt jsonb;
    v_anchor jsonb;
    v_kind text;
    v_reason text;
    v_attempts integer;
    v_original bigint := 0;
BEGIN
    IF NOT yoao_assets_v1._directory_registration_fenced(p_run_id, p_job_id, p_task_id, p_generation, p_token) THEN RETURN; END IF;
    SELECT r.* INTO v_run FROM public.dataset_registration_runs AS r WHERE r.id = p_run_id;
    v_expected := CASE WHEN v_run.phase = 'preflight' THEN 'pending' ELSE 'ready' END;
    SELECT e.* INTO v_entry FROM public.dataset_registration_entries AS e
    WHERE e.run_id = p_run_id AND e.path_digest = p_digest AND e.state = v_expected;
    IF NOT FOUND OR (v_run.phase = 'preflight' AND p_state NOT IN ('ready', 'error'))
        OR (v_run.phase = 'registering' AND p_state NOT IN ('registered', 'unchanged', 'error')) THEN RETURN; END IF;
    IF p_state IN ('registered', 'unchanged') AND (
        p_item_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM public.dataset_source_items AS h WHERE h.source_id = v_run.source_id
                AND h.path_digest = p_digest AND h.dataset_item_id = p_item_id
            UNION ALL
            SELECT 1 FROM public.dataset_source_revisions AS a WHERE a.source_id = v_run.source_id
                AND a.path_digest = p_digest AND a.run_id = v_run.scan_run_id AND a.dataset_item_id = p_item_id
        )
    ) THEN RETURN QUERY SELECT NULL::integer, NULL::text, 'content_binding_changed'::text; RETURN; END IF;

    IF v_run.phase = 'preflight' AND (v_run.snapshot -> 'comparison_schema') = '1'::jsonb THEN
        SELECT c.* INTO v_change FROM public.dataset_source_changes AS c WHERE c.run_id = p_run_id AND c.path_digest = p_digest;
        IF NOT FOUND THEN RETURN QUERY SELECT NULL::integer, NULL::text, 'comparison_entry_missing'::text; RETURN; END IF;
        IF p_state = 'ready' THEN
            SELECT e.verified_content, s.snapshot -> 'anchor' INTO v_receipt, v_anchor
            FROM public.dataset_scan_entries AS e JOIN public.dataset_scan_runs AS s ON s.id = e.run_id
            WHERE e.run_id = v_run.scan_run_id AND e.path_digest = p_digest;
            IF v_receipt ->> 'sha256' IS NULL THEN
                RETURN QUERY SELECT NULL::integer, NULL::text, 'content_unverified'::text; RETURN;
            END IF;
            IF v_change.before_sha256 IS NULL THEN v_kind := 'added';
            ELSIF v_change.before_sha256 <> v_receipt ->> 'sha256' THEN v_kind := 'changed'; v_reason := 'content_changed';
            ELSIF nullif(v_change.before_anchor, 'null'::jsonb) IS DISTINCT FROM nullif(v_anchor, 'null'::jsonb) THEN
                v_kind := 'changed'; v_reason := 'qualification_changed';
            ELSIF nullif(v_change.before_observation, 'null'::jsonb) IS DISTINCT FROM nullif(v_receipt -> 'observation', 'null'::jsonb) THEN
                v_kind := 'changed'; v_reason := 'observation_changed';
            ELSE v_kind := 'unchanged'; END IF;
            UPDATE public.dataset_source_changes AS c SET kind = v_kind, reason = v_reason, candidate_sha256 = v_receipt ->> 'sha256'
            WHERE c.run_id = p_run_id AND c.path_digest = p_digest;
            IF v_kind = 'changed' THEN p_state := 'error'; p_error := 'reference_content_changed'; END IF;
        ELSE
            UPDATE public.dataset_source_changes AS c SET kind = 'unreadable', reason = p_error
            WHERE c.run_id = p_run_id AND c.path_digest = p_digest;
        END IF;
    END IF;

    v_attempts := yoao_assets_v1._transition_directory_registration_entry(p_run_id, p_digest, v_expected, p_state, p_error, p_item_id);
    IF v_attempts IS NULL THEN RETURN; END IF;
    IF p_state = 'ready' THEN
        IF v_run.snapshot ->> 'ownership_mode' = 'managed_copy' AND coalesce(p_item_id, v_entry.item_id) IS NULL THEN
            v_original := p_original_size;
        END IF;
        UPDATE public.dataset_registration_entries AS e SET preview_estimated_bytes = p_preview_estimate,
            preview_error = p_preview_error, original_estimated_bytes = v_original
        WHERE e.run_id = p_run_id AND e.path_digest = p_digest;
        UPDATE public.dataset_registration_runs AS r SET counts = r.counts || pg_catalog.jsonb_build_object(
            'preview_estimated_bytes', coalesce((r.counts ->> 'preview_estimated_bytes')::bigint, 0) + coalesce(p_preview_estimate, 0),
            'preview_estimated_count', coalesce((r.counts ->> 'preview_estimated_count')::bigint, 0) + (p_preview_estimate IS NOT NULL)::int,
            'preview_estimate_error_count', coalesce((r.counts ->> 'preview_estimate_error_count')::bigint, 0) + (p_preview_error IS NOT NULL)::int,
            'original_estimated_bytes', coalesce((r.counts ->> 'original_estimated_bytes')::bigint, 0) + v_original
        ) WHERE r.id = p_run_id;
    END IF;
    UPDATE public.dataset_registration_runs AS r SET lease_expires_at = pg_catalog.clock_timestamp() + interval '60 seconds',
        counts = CASE WHEN p_item_id IS NULL THEN r.counts ELSE r.counts || pg_catalog.jsonb_build_object(
            'original_copy_bytes', (r.counts ->> 'original_copy_bytes')::bigint + p_copy_bytes,
            'preview_retained_bytes', coalesce((r.counts ->> 'preview_retained_bytes')::bigint, 0) + p_preview_bytes,
            'preview_failed_count', coalesce((r.counts ->> 'preview_failed_count')::bigint, 0) + p_preview_failed::int
        ) END
    WHERE r.id = p_run_id;
    RETURN QUERY SELECT v_attempts, p_state, NULL::text;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.record_directory_registration_entry(uuid, uuid, text, integer, uuid, text, text, text, uuid, bigint, text, bigint, bigint, bigint, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.finish_directory_registration(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_status text, p_code text
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_registration_runs;
    v_stamp timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_status NOT IN ('completed', 'cancelled', 'failed') THEN RETURN false; END IF;
    SELECT r.* INTO v_run FROM public.dataset_registration_runs AS r
    WHERE r.id = p_run_id AND r.async_job_id IS NOT DISTINCT FROM p_job_id AND r.status IN ('pending', 'running');
    IF NOT FOUND THEN RETURN false; END IF;
    IF p_job_id IS NOT NULL AND NOT yoao_jobs_v1.transition_directory_registration_job(
        p_job_id, p_run_id, p_task_id, p_status, NULL, v_stamp, p_code,
        v_run.counts || pg_catalog.jsonb_build_object('registration_id', p_run_id, 'phase', v_run.phase, 'error_code', p_code)
    ) THEN RETURN false; END IF;
    UPDATE public.dataset_registration_runs AS r
    SET status = p_status, error_code = p_code, lease_token = NULL, lease_expires_at = NULL, completed_at = v_stamp,
        comparison_complete = CASE WHEN r.phase <> 'preflight' THEN r.comparison_complete
            ELSE p_status = 'completed' AND coalesce(r.snapshot -> 'comparison_schema' = '1'::jsonb, false) END
    WHERE r.id = p_run_id AND r.async_job_id IS NOT DISTINCT FROM p_job_id AND r.status IN ('pending', 'running');
    RETURN FOUND;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.finish_directory_registration(uuid, uuid, text, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_confirmation_sample(p_run_id uuid, p_scope text)
RETURNS TABLE (path_digest varchar, sha256 text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT e.path_digest, observed.verified_content ->> 'sha256'
    FROM public.dataset_registration_runs AS r
    JOIN public.dataset_registration_entries AS e ON e.run_id = r.id
    JOIN public.dataset_scan_entries AS observed ON observed.run_id = r.scan_run_id AND observed.path_digest = e.path_digest
    WHERE r.id = p_run_id AND e.state = 'ready'
      AND (p_scope <> 'additions_only' OR EXISTS (SELECT 1 FROM public.dataset_source_changes AS c
            WHERE c.run_id = r.id AND c.path_digest = e.path_digest AND c.kind = 'added'))
    ORDER BY e.path_digest LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_confirmation_sample(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.fail_directory_confirmation_sample(
    p_run_id uuid, p_job_id uuid, p_generation integer, p_digest text, p_error text
)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE WHEN EXISTS (SELECT 1 FROM public.dataset_registration_runs AS r
        WHERE r.id = p_run_id AND r.async_job_id IS NOT DISTINCT FROM p_job_id
          AND r.generation = p_generation AND r.phase = 'preflight' AND r.status = 'completed')
    THEN yoao_assets_v1._transition_directory_registration_entry(p_run_id, p_digest, 'ready', 'error', p_error, NULL) IS NOT NULL
    ELSE false END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.fail_directory_confirmation_sample(uuid, uuid, integer, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.confirm_directory_registration(p_run_id uuid, p_job_id uuid, p_generation integer, p_scope text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_count bigint;
BEGIN
    UPDATE public.dataset_registration_runs AS r
    SET phase = 'registering', snapshot = r.snapshot || pg_catalog.jsonb_build_object('registration_scope', p_scope)
    WHERE r.id = p_run_id AND r.async_job_id IS NOT DISTINCT FROM p_job_id AND r.generation = p_generation
      AND r.phase = 'preflight' AND r.status = 'completed'
      AND (p_scope = 'all_safe' OR (p_scope = 'additions_only' AND r.comparison_complete));
    IF NOT FOUND THEN RETURN false; END IF;
    IF p_scope = 'additions_only' THEN
        UPDATE public.dataset_registration_entries AS e SET state = 'unchanged'
        FROM public.dataset_source_changes AS c
        WHERE e.run_id = p_run_id AND e.state = 'ready' AND c.run_id = e.run_id
          AND c.path_digest = e.path_digest AND c.kind = 'unchanged';
        GET DIAGNOSTICS v_count = ROW_COUNT;
        UPDATE public.dataset_registration_runs AS r SET counts = r.counts || pg_catalog.jsonb_build_object(
            'ready_count', (r.counts ->> 'ready_count')::bigint - v_count,
            'unchanged_count', (r.counts ->> 'unchanged_count')::bigint + v_count
        ) WHERE r.id = p_run_id;
    END IF;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.confirm_directory_registration(uuid, uuid, integer, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.retry_directory_registration_entry(p_run_id uuid, p_generation integer, p_digest text)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_registration_runs;
    v_kind text;
    v_verified boolean;
    v_state text;
BEGIN
    SELECT r.* INTO v_run FROM public.dataset_registration_runs AS r
    WHERE r.id = p_run_id AND r.generation = p_generation
      AND (r.status <> 'running' OR r.lease_expires_at IS NULL OR r.lease_expires_at <= pg_catalog.clock_timestamp()) FOR UPDATE;
    IF NOT FOUND THEN RETURN 'active_lease'; END IF;
    IF NOT EXISTS (SELECT 1 FROM public.dataset_registration_entries AS e
        WHERE e.run_id = p_run_id AND e.path_digest = p_digest AND e.state = 'error') THEN RETURN 'entry_not_failed'; END IF;
    IF v_run.snapshot -> 'comparison_schema' = '1'::jsonb THEN
        SELECT c.kind INTO v_kind FROM public.dataset_source_changes AS c WHERE c.run_id = p_run_id AND c.path_digest = p_digest;
        IF v_kind IS NULL OR v_kind = 'changed' THEN RETURN 'source_change_requires_rescan'; END IF;
        IF v_run.phase = 'registering' AND v_run.snapshot ->> 'registration_scope' = 'additions_only' AND v_kind <> 'added' THEN
            RETURN 'registration_scope_changed';
        END IF;
    END IF;
    SELECT e.verified_content ->> 'sha256' IS NOT NULL INTO v_verified
    FROM public.dataset_scan_entries AS e WHERE e.run_id = v_run.scan_run_id AND e.path_digest = p_digest;
    v_state := CASE WHEN v_verified AND v_run.phase = 'registering' THEN 'ready' ELSE 'pending' END;
    PERFORM yoao_assets_v1._transition_directory_registration_entry(p_run_id, p_digest, 'error', v_state, NULL, NULL);
    UPDATE public.dataset_registration_entries AS e SET refresh_unverified = NOT v_verified
    WHERE e.run_id = p_run_id AND e.path_digest = p_digest;
    IF v_state = 'pending' THEN UPDATE public.dataset_registration_runs AS r SET phase = 'preflight' WHERE r.id = p_run_id; END IF;
    RETURN NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.retry_directory_registration_entry(uuid, integer, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.arm_directory_registration_dispatch(p_run_id uuid, p_job_id uuid, p_task_id text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH armed AS (
        UPDATE public.dataset_registration_runs AS r SET lease_expires_at = pg_catalog.clock_timestamp() + interval '30 seconds'
        FROM yoao_jobs_v1.read_job(p_job_id) AS j
        WHERE r.id = p_run_id AND r.status = 'pending' AND r.async_job_id = p_job_id
          AND j.kind = 'dataset_registration' AND j.celery_task_id = p_task_id
        RETURNING 1
    ) SELECT EXISTS (SELECT 1 FROM armed)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.arm_directory_registration_dispatch(uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.note_directory_registration_dispatch_failure(p_run_id uuid, p_job_id uuid, p_task_id text)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.dataset_registration_runs AS r SET error_code = 'dispatch_unavailable'
    FROM yoao_jobs_v1.read_job(p_job_id) AS j
    WHERE r.id = p_run_id AND r.status = 'pending' AND r.async_job_id = p_job_id
      AND j.kind = 'dataset_registration' AND j.celery_task_id = p_task_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.note_directory_registration_dispatch_failure(uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.due_directory_registrations(p_stamp timestamptz)
RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.id FROM public.dataset_registration_runs AS r
    WHERE r.status IN ('pending', 'running') AND r.lease_expires_at <= p_stamp
    ORDER BY r.lease_expires_at LIMIT 100
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.due_directory_registrations(timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.requeue_directory_registration(
    p_run_id uuid, p_job_id uuid, p_task_id text, p_generation integer, p_token uuid,
    p_next_task_id text, p_delay integer, p_error text, p_recover boolean
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_stamp timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_recover THEN
        IF NOT EXISTS (SELECT 1 FROM public.dataset_registration_runs AS r
            WHERE r.id = p_run_id AND r.async_job_id = p_job_id AND r.status = 'running'
              AND r.generation = p_generation AND r.lease_token IS NOT DISTINCT FROM p_token
              AND r.lease_expires_at <= v_stamp) THEN RETURN false; END IF;
    ELSIF NOT yoao_assets_v1._directory_registration_fenced(p_run_id, p_job_id, p_task_id, p_generation, p_token) THEN
        RETURN false;
    END IF;
    IF NOT yoao_jobs_v1.transition_directory_registration_job(
        p_job_id, p_run_id, p_task_id, 'requeue', p_next_task_id, v_stamp, NULL, NULL
    ) THEN RETURN false; END IF;
    UPDATE public.dataset_registration_runs AS r SET status = 'pending', lease_token = NULL,
        generation = r.generation + CASE WHEN p_recover THEN 1 ELSE 0 END,
        error_code = CASE WHEN p_recover THEN r.error_code ELSE p_error END,
        lease_expires_at = CASE WHEN p_recover THEN r.lease_expires_at ELSE v_stamp + pg_catalog.make_interval(secs => p_delay) END
    WHERE r.id = p_run_id AND r.async_job_id = p_job_id AND r.generation = p_generation AND r.status = 'running';
    RETURN FOUND;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.requeue_directory_registration(uuid, uuid, text, integer, uuid, text, integer, text, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.consume_directory_relocation(p_run_id uuid, p_digest text, p_item_id uuid, p_revision_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.dataset_registration_runs AS r
        JOIN public.dataset_source_revisions AS a ON a.registration_id = r.id AND a.source_id = r.source_id
        JOIN public.dataset_source_revisions AS old ON old.id = a.previous_revision_id
        JOIN public.dataset_source_changes AS c ON c.run_id = r.id AND c.path_digest = a.path_digest
        WHERE r.id = p_run_id AND r.phase = 'preflight' AND r.status = 'completed' AND r.comparison_complete
          AND a.id = p_revision_id AND a.dataset_item_id = p_item_id AND a.path_digest = p_digest AND c.kind = 'added'
          AND old.path_digest <> a.path_digest) THEN RETURN false; END IF;
    IF yoao_assets_v1._transition_directory_registration_entry(p_run_id, p_digest, 'ready', 'registered', NULL, p_item_id) IS NULL THEN
        RETURN false;
    END IF;
    UPDATE public.dataset_registration_runs AS r SET counts = r.counts || pg_catalog.jsonb_build_object(
        'estimated_new_count', greatest(0, coalesce((r.counts ->> 'estimated_new_count')::bigint, 0) - 1)
    ) WHERE r.id = p_run_id;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.consume_directory_relocation(uuid, text, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_registration_entries(p_run_id uuid, p_state text, p_limit integer, p_offset integer)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH eligible AS MATERIALIZED (
        SELECT e.path_digest, o.relative_path, e.state, e.item_id, e.error_code, e.attempts,
            (o.observed ->> 'size')::bigint AS size_bytes, o.verified_content ->> 'sha256' AS sha256,
            e.preview_estimated_bytes, e.preview_error
        FROM public.dataset_registration_runs AS r
        JOIN public.dataset_registration_entries AS e ON e.run_id = r.id
        JOIN public.dataset_scan_entries AS o ON o.run_id = r.scan_run_id AND o.path_digest = e.path_digest
        WHERE r.id = p_run_id AND (p_state IS NULL OR e.state = p_state)
    ), page AS (
        SELECT e.* FROM eligible AS e ORDER BY e.path_digest LIMIT least(p_limit, 200) OFFSET p_offset
    ) SELECT pg_catalog.jsonb_build_object('total', (SELECT pg_catalog.count(*) FROM eligible),
        'items', coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(page) ORDER BY page.path_digest), '[]'::jsonb)) FROM page
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_registration_entries(uuid, text, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_registration_changes(
    p_run_id uuid, p_kind text, p_limit integer, p_offset integer
)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH run AS MATERIALIZED (
        SELECT r.id, r.created_at, r.comparison_complete, r.phase, r.status
        FROM public.dataset_registration_runs AS r WHERE r.id = p_run_id
    ), changes AS MATERIALIZED (
        SELECT c.path_digest, c.relative_path, c.kind, c.reason, c.before_revision_id, c.before_item_id,
            c.before_sha256, c.candidate_sha256
        FROM public.dataset_source_changes AS c JOIN run ON c.run_id = run.id
        WHERE run.comparison_complete
    ), counts AS (
        SELECT c.kind, pg_catalog.count(*) AS n FROM changes AS c GROUP BY c.kind
    ), eligible AS MATERIALIZED (
        SELECT c.path_digest, c.relative_path, c.kind, c.reason, c.before_item_id, c.before_revision_id,
            a.id AS accepted_revision_id, a.item_id_at_accept AS accepted_item_id,
            (a.id IS NULL AND run.phase = 'preflight' AND run.status = 'completed'
                AND c.kind = 'changed' AND c.reason = 'content_changed' AND c.before_revision_id IS NOT NULL) IS TRUE AS can_accept_revision,
            (a.id IS NULL AND run.phase = 'preflight' AND run.status = 'completed' AND c.kind = 'changed'
                AND c.reason = 'observation_changed' AND c.before_revision_id IS NOT NULL
                AND c.before_item_id IS NOT NULL AND c.before_sha256 = c.candidate_sha256) IS TRUE AS can_revalidate,
            (a.id IS NULL AND run.phase = 'preflight' AND run.status = 'completed' AND c.kind = 'not_seen'
                AND c.before_revision_id IS NOT NULL AND c.before_item_id IS NOT NULL) AS can_relocate,
            CASE WHEN a.path_digest <> old.path_digest THEN observed.relative_path END AS relocated_to_path,
            c.before_sha256, c.candidate_sha256, e.state AS registration_state, e.error_code
        FROM changes AS c CROSS JOIN run
        LEFT JOIN public.dataset_registration_entries AS e ON e.run_id = run.id AND e.path_digest = c.path_digest
        LEFT JOIN public.dataset_source_revisions AS a ON a.registration_id = run.id AND
            (a.path_digest = c.path_digest OR (c.kind = 'not_seen' AND a.previous_revision_id = c.before_revision_id))
        LEFT JOIN public.dataset_source_revisions AS old ON old.id = a.previous_revision_id
        LEFT JOIN public.dataset_scan_entries AS observed ON observed.run_id = a.run_id AND observed.path_digest = a.path_digest
        WHERE p_kind IS NULL OR c.kind = p_kind
    ), page AS (
        SELECT e.* FROM eligible AS e ORDER BY e.path_digest LIMIT least(p_limit, 200) OFFSET p_offset
    )
    SELECT pg_catalog.jsonb_build_object('complete', run.comparison_complete, 'observed_at', run.created_at,
        'counts', CASE WHEN run.comparison_complete THEN
            '{"added":0,"unchanged":0,"changed":0,"not_seen":0,"excluded":0,"unreadable":0}'::jsonb
            || coalesce((SELECT pg_catalog.jsonb_object_agg(counts.kind, counts.n) FROM counts), '{}'::jsonb)
            ELSE '{}'::jsonb END,
        'remaining_added_count', (SELECT pg_catalog.count(*) FROM changes AS c
            JOIN public.dataset_registration_entries AS e ON e.run_id = run.id AND e.path_digest = c.path_digest
            WHERE c.kind = 'added' AND e.state = 'ready'),
        'total', (SELECT pg_catalog.count(*) FROM eligible),
        'items', coalesce((SELECT pg_catalog.jsonb_agg(pg_catalog.to_jsonb(page) ORDER BY page.path_digest) FROM page), '[]'::jsonb)
    ) FROM run
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_registration_changes(uuid, text, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_registration_change_kinds(p_run_id uuid)
RETURNS SETOF text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT DISTINCT c.kind::text FROM public.dataset_source_changes AS c WHERE c.run_id = p_run_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_registration_change_kinds(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_directory_source_head(p_source_id uuid, p_digest text)
RETURNS TABLE (source_id uuid, path_digest varchar, current_revision_id uuid, run_id uuid,
    dataset_item_id uuid, asset_id uuid, content_sha256 varchar, ownership_mode varchar, owns_item boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT h.source_id, h.path_digest, h.current_revision_id, h.run_id,
        h.dataset_item_id, h.asset_id, h.content_sha256, h.ownership_mode, h.owns_item
    FROM public.dataset_source_items AS h WHERE h.source_id = p_source_id AND h.path_digest = p_digest
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.read_directory_source_head(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_directory_source_revision(p_source_id uuid, p_revision_id uuid)
RETURNS TABLE (id uuid, source_id uuid, path_digest varchar, run_id uuid, revision_number integer,
    item_id_at_accept uuid, asset_id_at_accept uuid, dataset_item_id uuid, asset_id uuid,
    content_sha256 varchar, ownership_mode varchar, owns_item boolean, previous_revision_id uuid, registration_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.id, r.source_id, r.path_digest, r.run_id, r.revision_number, r.item_id_at_accept,
        r.asset_id_at_accept, r.dataset_item_id, r.asset_id, r.content_sha256,
        r.ownership_mode, r.owns_item, r.previous_revision_id, r.registration_id
    FROM public.dataset_source_revisions AS r WHERE r.id = p_revision_id AND r.source_id = p_source_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.read_directory_source_revision(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_revision_for_observation(p_source_id uuid, p_scan_id uuid, p_digest text)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.id FROM public.dataset_source_revisions AS r
    WHERE r.source_id = p_source_id AND r.run_id = p_scan_id AND r.path_digest = p_digest
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_revision_for_observation(uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.check_directory_revision_selection(
    p_run_id uuid, p_digest text, p_generation integer, p_previous uuid, p_sha text, p_mode text
)
RETURNS TABLE (code text, accepted_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_registration_runs;
    v_change public.dataset_source_changes;
    v_old_change public.dataset_source_changes;
    v_before public.dataset_source_revisions;
    v_accepted public.dataset_source_revisions;
    v_old_anchor jsonb;
    v_new_anchor jsonb;
    v_current_path text := p_digest;
BEGIN
    SELECT r.* INTO v_run FROM public.dataset_registration_runs AS r WHERE r.id = p_run_id;
    IF NOT FOUND OR v_run.phase <> 'preflight' OR v_run.status <> 'completed'
        OR NOT v_run.comparison_complete OR v_run.generation IS DISTINCT FROM p_generation THEN
        RETURN QUERY SELECT 'revision_preflight_required'::text, NULL::uuid; RETURN;
    END IF;
    SELECT c.* INTO v_change FROM public.dataset_source_changes AS c WHERE c.run_id = p_run_id AND c.path_digest = p_digest;
    IF p_mode = 'relocate' THEN
        SELECT r.* INTO v_before FROM public.dataset_source_revisions AS r
        WHERE r.id = p_previous AND r.source_id = v_run.source_id;
        v_current_path := v_before.path_digest;
        SELECT c.* INTO v_old_change FROM public.dataset_source_changes AS c
        WHERE c.run_id = p_run_id AND c.path_digest = v_current_path;
        SELECT s.snapshot -> 'anchor' INTO v_old_anchor FROM public.dataset_scan_runs AS s WHERE s.id = v_before.run_id;
        SELECT s.snapshot -> 'anchor' INTO v_new_anchor FROM public.dataset_scan_runs AS s WHERE s.id = v_run.scan_run_id;
        IF v_change.kind IS DISTINCT FROM 'added' OR v_change.candidate_sha256 IS DISTINCT FROM p_sha
            OR v_before.id IS NULL OR v_current_path = p_digest OR v_before.content_sha256 IS DISTINCT FROM p_sha
            OR v_old_change.kind IS DISTINCT FROM 'not_seen' OR v_old_change.before_revision_id IS DISTINCT FROM p_previous
            OR v_old_change.before_sha256 IS DISTINCT FROM p_sha OR v_old_anchor IS DISTINCT FROM v_new_anchor THEN
            RETURN QUERY SELECT 'revision_selection_changed'::text, NULL::uuid; RETURN;
        END IF;
    ELSIF p_mode IS NULL OR p_mode NOT IN ('content', 'revalidate') OR v_change.kind IS DISTINCT FROM 'changed'
        OR v_change.reason IS DISTINCT FROM (CASE WHEN p_mode = 'revalidate' THEN 'observation_changed' ELSE 'content_changed' END)
        OR (p_mode = 'revalidate' AND v_change.before_sha256 IS DISTINCT FROM p_sha)
        OR v_change.before_revision_id IS NULL OR v_change.before_revision_id IS DISTINCT FROM p_previous
        OR v_change.candidate_sha256 IS DISTINCT FROM p_sha THEN
        RETURN QUERY SELECT 'revision_selection_changed'::text, NULL::uuid; RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.dataset_scan_entries AS e WHERE e.run_id = v_run.scan_run_id
        AND e.path_digest = p_digest AND e.verified_content ->> 'sha256' = p_sha) THEN
        RETURN QUERY SELECT 'revision_selection_changed'::text, NULL::uuid; RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM public.dataset_scan_runs AS s WHERE s.source_id = v_run.source_id AND s.status IN ('pending', 'running'))
        OR EXISTS (SELECT 1 FROM public.dataset_registration_runs AS r WHERE r.source_id = v_run.source_id AND r.status IN ('pending', 'running')) THEN
        RETURN QUERY SELECT 'source_busy'::text, NULL::uuid; RETURN;
    END IF;
    SELECT r.* INTO v_accepted FROM public.dataset_source_revisions AS r WHERE r.registration_id = p_run_id AND r.path_digest = p_digest;
    IF FOUND THEN
        IF v_accepted.previous_revision_id IS DISTINCT FROM p_previous OR v_accepted.content_sha256 IS DISTINCT FROM p_sha THEN
            RETURN QUERY SELECT 'revision_selection_changed'::text, NULL::uuid;
        ELSIF v_accepted.dataset_item_id IS NULL THEN
            RETURN QUERY SELECT 'accepted_revision_item_deleted'::text, NULL::uuid;
        ELSE
            RETURN QUERY SELECT NULL::text, v_accepted.id;
        END IF;
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.dataset_source_items AS h WHERE h.source_id = v_run.source_id
        AND h.path_digest = v_current_path AND h.current_revision_id = p_previous) THEN
        RETURN QUERY SELECT 'source_revision_changed'::text, NULL::uuid; RETURN;
    END IF;
    IF p_mode = 'relocate' AND (
        EXISTS (SELECT 1 FROM public.dataset_source_items AS h WHERE h.source_id = v_run.source_id AND h.path_digest = p_digest)
        OR NOT EXISTS (SELECT 1 FROM public.dataset_registration_entries AS e
            WHERE e.run_id = p_run_id AND e.path_digest = p_digest AND e.state = 'ready')
    ) THEN
        RETURN QUERY SELECT 'relocation_target_unavailable'::text, NULL::uuid; RETURN;
    END IF;
    RETURN QUERY SELECT NULL::text, NULL::uuid;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.check_directory_revision_selection(uuid, text, integer, uuid, text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.append_directory_source_revision(
    p_revision_id uuid, p_source_id uuid, p_scan_id uuid, p_digest text,
    p_item_id uuid, p_asset_id uuid, p_sha text, p_owns_item boolean, p_actor_id uuid,
    p_registration_id uuid, p_previous uuid, p_generation integer, p_mode text
)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source public.dataset_sources;
    v_previous_id uuid;
    v_number integer;
    v_code text;
    v_accepted uuid;
BEGIN
    SELECT s.* INTO v_source FROM public.dataset_sources AS s WHERE s.id = p_source_id FOR UPDATE;
    IF NOT FOUND OR NOT yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id) THEN RETURN 'access_revoked'; END IF;
    IF EXISTS (SELECT 1 FROM public.dataset_source_revisions AS r
        WHERE r.source_id = p_source_id AND r.path_digest = p_digest AND r.run_id = p_scan_id) THEN
        RETURN 'revision_candidate_already_accepted';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.dataset_scan_runs AS s
        JOIN public.dataset_scan_entries AS e ON e.run_id = s.id
        JOIN public.dataset_items AS i ON i.id = p_item_id AND i.dataset_id = v_source.dataset_id
        JOIN public.dataset_assets AS a ON a.id = p_asset_id AND a.dataset_item_id = i.id
        WHERE s.id = p_scan_id AND s.source_id = p_source_id AND e.path_digest = p_digest
          AND e.verified_content ->> 'sha256' = p_sha AND a.checksum_algorithm = 'sha256'
          AND a.checksum_value = p_sha AND a.verification_state = 'verified'
          AND a.size_bytes = (e.verified_content ->> 'size_bytes')::bigint
    ) THEN RETURN 'content_binding_changed'; END IF;
    SELECT r.id, r.revision_number INTO v_previous_id, v_number
    FROM public.dataset_source_revisions AS r
    WHERE r.source_id = p_source_id AND r.path_digest = p_digest ORDER BY r.revision_number DESC LIMIT 1;
    IF p_registration_id IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM public.dataset_registration_runs AS r
            WHERE r.id = p_registration_id AND r.source_id = p_source_id AND r.scan_run_id = p_scan_id) THEN
            RETURN 'revision_preflight_required';
        END IF;
        SELECT s.code, s.accepted_id INTO v_code, v_accepted
        FROM yoao_assets_v1.check_directory_revision_selection(p_registration_id, p_digest, p_generation, p_previous, p_sha, p_mode) AS s;
        IF v_code IS NOT NULL THEN RETURN v_code; END IF;
        IF v_accepted IS NOT NULL THEN RETURN 'revision_candidate_already_accepted'; END IF;
        v_previous_id := p_previous;
    END IF;
    INSERT INTO public.dataset_source_revisions (id, source_id, path_digest, run_id, revision_number,
        item_id_at_accept, asset_id_at_accept, dataset_item_id, asset_id, content_sha256,
        ownership_mode, owns_item, previous_revision_id, registration_id, accepted_by_id, accepted_at)
    VALUES (p_revision_id, p_source_id, p_digest, p_scan_id, coalesce(v_number, 0) + 1,
        p_item_id, p_asset_id, p_item_id, p_asset_id, p_sha, v_source.ownership_mode,
        p_owns_item, v_previous_id, p_registration_id, p_actor_id, pg_catalog.clock_timestamp());
    RETURN NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.append_directory_source_revision(uuid, uuid, uuid, text, uuid, uuid, text, boolean, uuid, uuid, uuid, integer, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_source_revision_history(p_source_id uuid, p_digest text, p_limit integer, p_offset integer)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH eligible AS MATERIALIZED (
        SELECT r.id AS revision_id, r.previous_revision_id, r.revision_number, r.item_id_at_accept AS item_id,
            r.content_sha256 AS sha256, false AS created, r.acceptance_order, old.path_digest AS previous_path_digest,
            CASE WHEN old.id IS NULL THEN 'initial' WHEN r.path_digest <> old.path_digest THEN 'relocation'
                 WHEN (r.item_id_at_accept, r.asset_id_at_accept, r.content_sha256) =
                      (old.item_id_at_accept, old.asset_id_at_accept, old.content_sha256) THEN 'observation_revalidation'
                 ELSE 'content_revision' END AS acceptance_kind,
            r.path_digest, r.run_id AS scan_run_id, r.registration_id, r.accepted_by_id, r.accepted_at,
            r.dataset_item_id IS NOT NULL AS item_available
        FROM public.dataset_source_revisions AS r LEFT JOIN public.dataset_source_revisions AS old ON old.id = r.previous_revision_id
        WHERE r.source_id = p_source_id AND (p_digest IS NULL OR r.path_digest = p_digest)
    ), page AS (
        SELECT r.* FROM eligible AS r ORDER BY r.path_digest, r.revision_number DESC LIMIT least(p_limit, 200) OFFSET p_offset
    )
    SELECT pg_catalog.jsonb_build_object('total', (SELECT pg_catalog.count(*) FROM eligible),
        'items', coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(page) ORDER BY page.path_digest, page.revision_number DESC), '[]'::jsonb))
    FROM page
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_source_revision_history(uuid, text, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_relocation_candidates(p_run_id uuid, p_digest text, p_limit integer, p_offset integer)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run public.dataset_registration_runs;
    v_old public.dataset_source_changes;
BEGIN
    SELECT r.* INTO v_run FROM public.dataset_registration_runs AS r WHERE r.id = p_run_id;
    SELECT c.* INTO v_old FROM public.dataset_source_changes AS c WHERE c.run_id = p_run_id AND c.path_digest = p_digest;
    IF v_run.comparison_complete IS NOT TRUE OR v_run.phase <> 'preflight' OR v_run.status <> 'completed'
        OR v_old.kind IS DISTINCT FROM 'not_seen' OR v_old.before_revision_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object('error_code', 'revision_preflight_required');
    END IF;
    IF EXISTS (SELECT 1 FROM public.dataset_source_revisions AS a
        WHERE a.registration_id = p_run_id AND a.previous_revision_id = v_old.before_revision_id)
        OR NOT EXISTS (SELECT 1 FROM public.dataset_source_items AS h
            WHERE h.source_id = v_run.source_id AND h.path_digest = p_digest AND h.current_revision_id = v_old.before_revision_id) THEN
        RETURN '{"total":0,"items":[]}'::jsonb;
    END IF;
    RETURN (
        WITH eligible AS MATERIALIZED (
            SELECT c.path_digest, c.relative_path, c.candidate_sha256
            FROM public.dataset_source_changes AS c
            JOIN public.dataset_registration_entries AS e ON e.run_id = c.run_id AND e.path_digest = c.path_digest
            WHERE c.run_id = p_run_id AND c.kind = 'added' AND c.candidate_sha256 = v_old.before_sha256 AND e.state = 'ready'
        ), page AS (
            SELECT c.* FROM eligible AS c ORDER BY c.path_digest LIMIT least(p_limit, 200) OFFSET p_offset
        )
        SELECT pg_catalog.jsonb_build_object('total', (SELECT pg_catalog.count(*) FROM eligible),
            'items', coalesce(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(page) ORDER BY page.path_digest), '[]'::jsonb)) FROM page
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_relocation_candidates(uuid, text, integer, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_directory_sync_schedule(p_source_id uuid, p_lock boolean)
RETURNS TABLE (
    source_id uuid, revision integer, enabled boolean, interval_minutes integer, new_file_policy varchar,
    timezone varchar, actor_id uuid, api_key_id uuid, key_proof varchar, snapshot jsonb, state varchar,
    scan_id uuid, registration_id uuid, next_run_at timestamptz, next_check_at timestamptz,
    last_checked_at timestamptz, last_started_at timestamptz, last_error varchar,
    dispatch_token uuid, dispatch_expires_at timestamptz
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        PERFORM 1 FROM public.dataset_source_sync_schedules AS s WHERE s.source_id = p_source_id FOR UPDATE;
    END IF;
    RETURN QUERY SELECT s.source_id, s.revision, s.enabled, s.interval_minutes, s.new_file_policy,
        s.timezone, s.actor_id, s.api_key_id, s.key_proof, s.snapshot, s.state, s.scan_id,
        s.registration_id, s.next_run_at, s.next_check_at, s.last_checked_at, s.last_started_at,
        s.last_error, s.dispatch_token, s.dispatch_expires_at
    FROM public.dataset_source_sync_schedules AS s WHERE s.source_id = p_source_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.read_directory_sync_schedule(uuid, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.configure_directory_sync(
    p_source_id uuid, p_revision integer, p_actor_id uuid, p_key_id uuid, p_key_proof text,
    p_enabled boolean, p_interval integer, p_policy text, p_timezone text,
    p_maintenance_revision integer, p_lifecycle_revision integer, p_anchor jsonb, p_stamp timestamptz
)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row public.dataset_source_sync_schedules;
    v_exists boolean;
    v_scan_status text;
    v_graph_phase text;
    v_graph_status text;
    v_snapshot jsonb;
    v_delegation_changed boolean;
BEGIN
    IF NOT yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id) THEN RETURN 'access_revoked'; END IF;
    SELECT s.* INTO v_row FROM public.dataset_source_sync_schedules AS s WHERE s.source_id = p_source_id FOR UPDATE;
    v_exists := FOUND;
    IF coalesce(v_row.revision, 0) IS DISTINCT FROM p_revision THEN RETURN 'schedule_revision_changed'; END IF;
    IF NOT v_exists THEN
        v_row.actor_id := p_actor_id;
        v_row.snapshot := '{}'::jsonb;
        v_row.state := 'idle';
        v_row.enabled := false;
    END IF;
    IF p_enabled THEN
        SELECT pg_catalog.jsonb_build_object('source_revision', s.config_revision, 'location_revision', l.config_revision,
            'maintenance_revision', p_maintenance_revision, 'dataset_lifecycle_revision', p_lifecycle_revision,
            'anchor', p_anchor) INTO v_snapshot
        FROM public.dataset_sources AS s JOIN public.storage_locations AS l ON l.id = s.location_id
        WHERE s.id = p_source_id;
        v_delegation_changed := (v_row.actor_id, v_row.api_key_id, v_row.key_proof)
            IS DISTINCT FROM (p_actor_id, p_key_id, p_key_proof);
        IF coalesce((v_row.snapshot ->> 'dataset_lifecycle_revision')::integer, 1) <> p_lifecycle_revision
            OR coalesce((v_row.snapshot ->> 'maintenance_revision')::integer, 1) <> p_maintenance_revision THEN
            v_row.scan_id := NULL;
            v_row.registration_id := NULL;
            v_row.state := 'idle';
        END IF;
        IF v_delegation_changed OR v_row.snapshot IS DISTINCT FROM v_snapshot OR v_row.state = 'blocked' THEN
            SELECT r.status INTO v_scan_status FROM public.dataset_scan_runs AS r WHERE r.id = v_row.scan_id;
            SELECT r.phase, r.status INTO v_graph_phase, v_graph_status FROM public.dataset_registration_runs AS r
            WHERE r.id = v_row.registration_id;
            IF v_graph_phase = 'registering' AND v_graph_status IN ('pending', 'running') THEN
                v_row.state := 'registering';
            ELSIF v_graph_phase = 'preflight' AND v_graph_status = 'completed' THEN
                v_row.state := 'awaiting_review';
            ELSIF v_delegation_changed AND v_row.state <> 'blocked'
                AND (v_scan_status IN ('pending', 'running') OR v_graph_status IN ('pending', 'running')) THEN
                RETURN 'schedule_cycle_authority_pinned';
            ELSE
                v_row.state := 'idle';
            END IF;
        END IF;
        v_row.snapshot := v_snapshot;
        v_row.actor_id := p_actor_id;
        v_row.api_key_id := p_key_id;
        v_row.key_proof := p_key_proof;
        IF v_row.state = 'awaiting_review' THEN
            v_row.next_run_at := NULL;
        ELSIF NOT v_row.enabled OR v_row.next_run_at IS NULL THEN
            v_row.next_run_at := p_stamp;
        END IF;
        v_row.last_error := NULL;
    END IF;
    IF v_exists THEN
        UPDATE public.dataset_source_sync_schedules AS s
        SET revision = s.revision + 1, enabled = p_enabled, interval_minutes = p_interval,
            new_file_policy = p_policy, timezone = p_timezone, actor_id = v_row.actor_id, api_key_id = v_row.api_key_id,
            key_proof = v_row.key_proof, snapshot = v_row.snapshot, state = v_row.state, scan_id = v_row.scan_id,
            registration_id = v_row.registration_id, next_run_at = v_row.next_run_at, last_error = v_row.last_error,
            dispatch_token = NULL, dispatch_expires_at = NULL, next_check_at = p_stamp
        WHERE s.source_id = p_source_id AND s.revision = p_revision;
    ELSE
        INSERT INTO public.dataset_source_sync_schedules (source_id, revision, enabled, interval_minutes,
            new_file_policy, timezone, actor_id, api_key_id, key_proof, snapshot, state, next_run_at, next_check_at)
        VALUES (p_source_id, 1, p_enabled, p_interval, p_policy, p_timezone, v_row.actor_id,
            v_row.api_key_id, v_row.key_proof, v_row.snapshot, v_row.state, v_row.next_run_at, p_stamp);
    END IF;
    RETURN NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.configure_directory_sync(uuid, integer, uuid, uuid, text, boolean, integer, text, text, integer, integer, jsonb, timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.continue_directory_sync(p_source_id uuid, p_revision integer, p_scan_id uuid, p_stamp timestamptz)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_schedule public.dataset_source_sync_schedules;
BEGIN
    SELECT s.* INTO v_schedule FROM public.dataset_source_sync_schedules AS s WHERE s.source_id = p_source_id FOR UPDATE;
    IF NOT FOUND OR v_schedule.revision IS DISTINCT FROM p_revision OR v_schedule.scan_id IS DISTINCT FROM p_scan_id THEN
        RETURN 'schedule_revision_changed';
    END IF;
    IF NOT v_schedule.enabled OR v_schedule.state <> 'awaiting_review'
        OR NOT EXISTS (SELECT 1 FROM public.dataset_registration_runs AS r
            WHERE r.id = v_schedule.registration_id AND r.phase = 'preflight' AND r.status = 'completed') THEN
        RETURN 'schedule_review_changed';
    END IF;
    UPDATE public.dataset_source_sync_schedules AS s
    SET revision = s.revision + 1, state = 'idle', last_error = NULL,
        next_run_at = p_stamp + pg_catalog.make_interval(mins => s.interval_minutes),
        next_check_at = p_stamp + pg_catalog.make_interval(mins => s.interval_minutes),
        dispatch_token = NULL, dispatch_expires_at = NULL
    WHERE s.source_id = p_source_id;
    RETURN NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.continue_directory_sync(uuid, integer, uuid, timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.due_directory_sync_sources(p_stamp timestamptz)
RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT s.source_id FROM public.dataset_source_sync_schedules AS s WHERE s.enabled AND s.next_check_at <= p_stamp
    ORDER BY s.next_check_at, s.source_id LIMIT 100
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.due_directory_sync_sources(timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.claim_directory_sync_wakeup(p_source_id uuid, p_token uuid, p_stamp timestamptz)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH claimed AS (
        UPDATE public.dataset_source_sync_schedules AS s
        SET dispatch_token = p_token, dispatch_expires_at = p_stamp + interval '90 seconds',
            next_check_at = p_stamp + interval '90 seconds'
        WHERE s.source_id = p_source_id AND s.enabled AND s.next_check_at <= p_stamp
          AND (s.dispatch_expires_at IS NULL OR s.dispatch_expires_at <= p_stamp)
        RETURNING 1
    ) SELECT EXISTS (SELECT 1 FROM claimed)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.claim_directory_sync_wakeup(uuid, uuid, timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.fail_directory_sync_wakeup(
    p_source_id uuid, p_revision integer, p_token uuid, p_code text, p_block boolean, p_stamp timestamptz
)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.dataset_source_sync_schedules AS s
    SET dispatch_token = NULL, dispatch_expires_at = NULL, last_checked_at = p_stamp,
        last_error = pg_catalog.left(p_code, 60), next_check_at = p_stamp + pg_catalog.make_interval(mins => s.interval_minutes),
        enabled = CASE WHEN p_block THEN false ELSE s.enabled END,
        state = CASE WHEN p_block THEN 'blocked' ELSE s.state END
    WHERE s.source_id = p_source_id AND s.revision = p_revision AND s.dispatch_token = p_token
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.fail_directory_sync_wakeup(uuid, integer, uuid, text, boolean, timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.settle_directory_sync_wakeup(
    p_source_id uuid, p_revision integer, p_token uuid, p_outcome text, p_run_id uuid,
    p_error text, p_stamp timestamptz
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row public.dataset_source_sync_schedules;
    v_next_check timestamptz := p_stamp + interval '60 seconds';
    v_settling boolean := p_outcome <> 'probing';
BEGIN
    SELECT s.* INTO v_row FROM public.dataset_source_sync_schedules AS s
    WHERE s.source_id = p_source_id AND s.revision = p_revision AND s.dispatch_token = p_token
        AND s.enabled AND s.dispatch_expires_at > pg_catalog.clock_timestamp() FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    CASE p_outcome
        WHEN 'probing' THEN NULL;
        WHEN 'started_scan' THEN
            IF v_row.state <> 'idle' THEN RETURN false; END IF;
            UPDATE public.dataset_scan_runs AS r
            SET snapshot = r.snapshot || pg_catalog.jsonb_build_object('sync_source_id', p_source_id,
                'sync_actor_id', v_row.actor_id, 'sync_new_file_policy', v_row.new_file_policy)
            WHERE r.id = p_run_id AND r.source_id = p_source_id AND r.status = 'pending'
                AND r.generation = 0 AND NOT (r.snapshot ? 'sync_source_id')
                AND r.snapshot ->> 'api_key_id' IS NOT DISTINCT FROM v_row.api_key_id::text
                AND r.snapshot ->> 'key_proof' IS NOT DISTINCT FROM v_row.key_proof;
            IF NOT FOUND THEN RETURN false; END IF;
            v_row.scan_id := p_run_id;
            v_row.registration_id := NULL;
            v_row.state := 'scanning';
            v_row.last_started_at := p_stamp;
            v_row.last_error := NULL;
        WHEN 'started_preflight' THEN
            IF v_row.state <> 'scanning' THEN RETURN false; END IF;
            UPDATE public.dataset_registration_runs AS r
            SET snapshot = r.snapshot || pg_catalog.jsonb_build_object('sync_source_id', p_source_id,
                'sync_actor_id', v_row.actor_id, 'sync_new_file_policy', coalesce(s.snapshot ->> 'sync_new_file_policy', 'manual'))
            FROM public.dataset_scan_runs AS s
            WHERE r.id = p_run_id AND r.source_id = p_source_id AND r.status = 'pending'
                AND r.phase = 'preflight' AND r.generation = 0 AND NOT (r.snapshot ? 'sync_source_id')
                AND r.scan_run_id = s.id AND s.id = v_row.scan_id
                AND r.snapshot ->> 'actor_id' = v_row.actor_id::text
                AND r.snapshot ->> 'api_key_id' IS NOT DISTINCT FROM v_row.api_key_id::text
                AND r.snapshot ->> 'key_proof' IS NOT DISTINCT FROM v_row.key_proof;
            IF NOT FOUND THEN RETURN false; END IF;
            v_row.registration_id := p_run_id;
            v_row.state := 'preflighting';
            v_row.last_error := NULL;
        WHEN 'confirmed_additions' THEN
            IF v_row.registration_id IS DISTINCT FROM p_run_id OR v_row.new_file_policy <> 'pure_additions' THEN RETURN false; END IF;
            UPDATE public.dataset_registration_runs AS r
            SET snapshot = r.snapshot || '{"sync_auto_ingestion":true}'::jsonb
            WHERE r.id = p_run_id AND r.source_id = p_source_id AND r.status = 'pending' AND r.phase = 'registering'
                AND r.snapshot ->> 'registration_scope' = 'additions_only'
                AND r.snapshot ->> 'sync_source_id' = p_source_id::text
                AND r.snapshot ->> 'sync_actor_id' = v_row.actor_id::text
                AND r.snapshot ->> 'actor_id' = v_row.actor_id::text
                AND r.snapshot ->> 'api_key_id' IS NOT DISTINCT FROM v_row.api_key_id::text
                AND r.snapshot ->> 'key_proof' IS NOT DISTINCT FROM v_row.key_proof;
            IF NOT FOUND THEN RETURN false; END IF;
            v_row.state := 'registering';
            v_row.last_error := NULL;
        WHEN 'continued_run' THEN
            v_row.last_error := NULL;
            IF EXISTS (SELECT 1 FROM public.dataset_registration_runs AS r
                WHERE r.id = v_row.registration_id AND r.phase = 'registering'
                    AND r.status IN ('pending', 'running')) THEN v_row.state := 'registering'; END IF;
        WHEN 'awaiting_review' THEN
            IF v_row.state <> 'preflighting' OR NOT EXISTS (SELECT 1 FROM public.dataset_registration_runs AS r
                WHERE r.id = v_row.registration_id AND r.source_id = p_source_id AND r.phase = 'preflight'
                    AND r.status = 'completed') THEN RETURN false; END IF;
            v_row.state := 'awaiting_review';
            v_row.next_run_at := NULL;
        WHEN 'observe_registration' THEN
            IF NOT EXISTS (SELECT 1 FROM public.dataset_registration_runs AS r
                WHERE r.id = v_row.registration_id AND r.source_id = p_source_id AND r.phase = 'registering'
                    AND r.status IN ('pending', 'running')) THEN RETURN false; END IF;
            v_row.state := 'registering';
        WHEN 'finished_cycle' THEN
            v_row.state := 'idle';
            v_row.next_run_at := p_stamp + pg_catalog.make_interval(mins => v_row.interval_minutes);
            v_next_check := v_row.next_run_at;
            v_row.last_error := p_error;
        WHEN 'not_due' THEN
            IF v_row.state <> 'idle' OR v_row.next_run_at IS NULL THEN RETURN false; END IF;
            v_next_check := v_row.next_run_at;
        WHEN 'busy' THEN v_row.last_error := 'source_busy';
        WHEN 'unchanged' THEN NULL;
        ELSE RETURN false;
    END CASE;
    UPDATE public.dataset_source_sync_schedules AS s
    SET state = v_row.state, scan_id = v_row.scan_id, registration_id = v_row.registration_id,
        next_run_at = v_row.next_run_at, next_check_at = v_next_check,
        last_started_at = v_row.last_started_at, last_error = v_row.last_error,
        dispatch_token = CASE WHEN v_settling THEN NULL ELSE s.dispatch_token END,
        dispatch_expires_at = CASE WHEN v_settling THEN NULL ELSE s.dispatch_expires_at END,
        last_checked_at = CASE WHEN v_settling THEN p_stamp ELSE s.last_checked_at END
    WHERE s.source_id = p_source_id;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.settle_directory_sync_wakeup(uuid, integer, uuid, text, uuid, text, timestamptz) FROM PUBLIC;
-- aap-statement-break
