CREATE FUNCTION yoao_assets_v1.dataset_metadata(
    p_dataset_id uuid, p_lock_nowait boolean, p_lock_share_nowait boolean
)
RETURNS TABLE (
    id uuid, display_id text, name text, description text, data_type text,
    storage_scan_prefix text, is_temporal boolean, file_count integer, created_by uuid,
    archived_at timestamptz, archived_by uuid, lifecycle_revision integer,
    metadata_ jsonb, created_at timestamptz, updated_at timestamptz
)
LANGUAGE plpgsql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock_nowait THEN
        PERFORM d.id FROM public.datasets AS d WHERE d.id = p_dataset_id
        FOR NO KEY UPDATE NOWAIT;
    ELSIF p_lock_share_nowait THEN
        PERFORM d.id FROM public.datasets AS d WHERE d.id = p_dataset_id
        FOR SHARE NOWAIT;
    END IF;
    RETURN QUERY
    SELECT d.id, d.display_id::text, d.name::text, d.description, d.data_type::text,
           d.storage_scan_prefix::text, d.is_temporal, d.file_count, d.created_by,
           d.archived_at, d.archived_by, d.lifecycle_revision, d.metadata,
           d.created_at, d.updated_at
    FROM public.datasets AS d WHERE d.id = p_dataset_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_metadata(uuid, boolean, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.change_dataset_archive_state(
    p_dataset_id uuid, p_actor_id uuid, p_revision integer,
    p_archive boolean, p_measured_at timestamptz
)
RETURNS boolean
LANGUAGE sql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH changed AS (
        UPDATE public.datasets AS d
        SET archived_at = CASE WHEN p_archive THEN p_measured_at END,
            archived_by = CASE WHEN p_archive THEN p_actor_id END,
            lifecycle_revision = d.lifecycle_revision + 1,
            updated_at = pg_catalog.now()
        FROM yoao_identity_v1.read_actor_facts(p_actor_id) AS actor
        WHERE d.id = p_dataset_id AND d.lifecycle_revision = p_revision
          AND (d.archived_at IS NULL) = p_archive
          AND actor.is_active
          AND (actor.platform_role = 'super_admin'
               OR (actor.platform_role = 'project_admin' AND d.created_by = p_actor_id))
        RETURNING d.id
    ) SELECT EXISTS (SELECT 1 FROM changed)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.change_dataset_archive_state(uuid, uuid, integer, boolean, timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.project_has_dataset(p_project_id uuid, p_dataset_id uuid)
RETURNS boolean
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.project_datasets AS link
                   WHERE link.project_id = p_project_id AND link.dataset_id = p_dataset_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.project_has_dataset(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_project_ids(p_dataset_id uuid)
RETURNS TABLE (project_id uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT link.project_id FROM public.project_datasets AS link WHERE link.dataset_id = p_dataset_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_project_ids(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.search_dataset_names(p_actor_id uuid, p_search text, p_limit integer)
RETURNS TABLE (id uuid, name text, data_type text)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT d.id, d.name::text, d.data_type::text
    FROM public.datasets AS d
    WHERE d.name ILIKE ('%' || p_search || '%') AND pg_catalog.char_length(p_search) BETWEEN 1 AND 64
      AND EXISTS (SELECT 1 FROM yoao_identity_v1.read_actor_facts(p_actor_id) AS actor WHERE actor.is_active)
    ORDER BY d.updated_at DESC
    LIMIT GREATEST(0, LEAST(p_limit, 20))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.search_dataset_names(uuid, text, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_scene_choices(p_dataset_id uuid, p_scene_id uuid)
RETURNS TABLE (id uuid, display_id text, name text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT scene.id, scene.display_id::text, scene.name::text
    FROM public.scenes AS scene
    WHERE scene.dataset_id = p_dataset_id AND (p_scene_id IS NULL OR scene.id = p_scene_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_scene_choices(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_owns_directory_media(p_item_id uuid)
RETURNS boolean
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.dataset_source_revisions AS revision
                   WHERE revision.dataset_item_id = p_item_id AND revision.owns_item)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_owns_directory_media(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_location_visible(p_actor_id uuid, p_location_id uuid)
RETURNS boolean
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.storage_locations AS location
        CROSS JOIN yoao_identity_v1.read_actor_facts(p_actor_id) AS actor
        WHERE location.id = p_location_id AND actor.is_active AND (
            actor.platform_role = 'super_admin'
            OR EXISTS (SELECT 1 FROM public.storage_connections AS connection
                       WHERE connection.id = location.connection_id
                         AND (connection.scope = 'global' OR connection.created_by = p_actor_id))
            OR (location.kind IN ('filesystem', 'agent') AND (
                location.config ->> 'scope' = 'global'
                OR location.config -> 'use_user_ids' @> pg_catalog.jsonb_build_array(p_actor_id::text)
            ))
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_location_visible(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_source_visible(p_actor_id uuid, p_source_id uuid)
RETURNS boolean
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.dataset_sources AS source
        CROSS JOIN yoao_identity_v1.read_actor_facts(p_actor_id) AS actor
        WHERE source.id = p_source_id AND actor.is_active AND (
            actor.platform_role = 'super_admin' OR (
                actor.platform_role = 'project_admin'
                AND EXISTS (SELECT 1 FROM public.datasets AS d
                            WHERE d.id = source.dataset_id AND d.created_by = p_actor_id)
                AND yoao_assets_v1.dataset_location_visible(p_actor_id, source.location_id)
            )
        )
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_source_visible(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.visible_dataset_sources(
    p_actor_id uuid, p_dataset_id uuid, p_location_id uuid, p_source_id uuid
)
RETURNS TABLE (
    id uuid, dataset_id uuid, location_id uuid, name text,
    config jsonb, config_revision integer, enabled boolean, generation integer,
    root_identity jsonb, ownership_mode text, cache_policy text, policy_revision integer,
    external_protection_revision integer, external_protection jsonb,
    unregistered_at timestamptz, created_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT s.id, s.dataset_id, s.location_id, s.name::text,
           s.config, s.config_revision, s.enabled, s.generation,
           s.root_identity, s.ownership_mode::text, s.cache_policy::text, s.policy_revision,
           s.external_protection_revision, s.external_protection, s.unregistered_at, s.created_at
    FROM public.dataset_sources AS s
    WHERE (p_dataset_id IS NULL OR s.dataset_id = p_dataset_id)
      AND (p_location_id IS NULL OR s.location_id = p_location_id)
      AND (p_source_id IS NULL OR s.id = p_source_id)
      AND yoao_assets_v1.dataset_source_visible(p_actor_id, s.id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.visible_dataset_sources(uuid, uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_job_visible(
    p_actor_id uuid, p_kind text, p_job_id uuid, p_payload_run_id text, p_location_id uuid
)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE p_kind
        WHEN 'dataset_scan' THEN EXISTS (
            SELECT 1 FROM public.dataset_scan_runs AS scan
            JOIN public.dataset_sources AS source ON source.id = scan.source_id
            WHERE (scan.async_job_id = p_job_id OR scan.id::text = p_payload_run_id)
              AND (p_location_id IS NULL OR source.location_id = p_location_id)
              AND yoao_assets_v1.dataset_source_visible(p_actor_id, source.id)
        )
        WHEN 'dataset_registration' THEN EXISTS (
            SELECT 1 FROM public.dataset_registration_runs AS registration
            JOIN public.dataset_sources AS source ON source.id = registration.source_id
            WHERE (registration.async_job_id = p_job_id OR registration.id::text = p_payload_run_id)
              AND (p_location_id IS NULL OR source.location_id = p_location_id)
              AND yoao_assets_v1.dataset_source_visible(p_actor_id, source.id)
        )
        ELSE false
    END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_job_visible(uuid, text, uuid, text, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_location_job_runs(p_actor_id uuid, p_location_id uuid)
RETURNS TABLE (kind text, run_id text, async_job_id uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH scope AS (
        SELECT source.id FROM public.dataset_sources AS source
        WHERE source.location_id = p_location_id
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, source.id)
    )
    SELECT 'dataset_scan', scan.id::text, scan.async_job_id
    FROM public.dataset_scan_runs AS scan WHERE scan.source_id IN (SELECT id FROM scope)
    UNION ALL
    SELECT 'dataset_registration', registration.id::text, registration.async_job_id
    FROM public.dataset_registration_runs AS registration
    WHERE registration.source_id IN (SELECT id FROM scope)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_location_job_runs(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_relationship_digest(p_text text)
RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_text, 'UTF8')), 'hex')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_relationship_digest(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_archive_impact(p_dataset_id uuid)
RETURNS TABLE (
    measured_at timestamptz, items bigint, sources bigint, projects bigint,
    tasks bigint, annotations bigint, active_scans bigint, active_registrations bigint,
    enabled_schedules bigint, pending_jobs bigint, ready_lineage_versions bigint,
    unfinished_lineage_versions bigint, fingerprint text
)
LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RETURN QUERY
    WITH dataset_items AS (
        SELECT item.id, item.dataset_id FROM public.dataset_items AS item
        WHERE item.dataset_id = p_dataset_id
    ), dataset_sources AS (
        SELECT s.id, s.location_id, s.config_revision, s.policy_revision,
               s.enabled, s.unregistered_at, s.generation
        FROM public.dataset_sources AS s WHERE s.dataset_id = p_dataset_id
    ), item_links AS (
        SELECT l.id, l.task_id, l.dataset_item_id, l.role
        FROM yoao_labeling_v1.task_dataset_item_link_rows AS l
        WHERE l.dataset_item_id IN (SELECT i.id FROM dataset_items AS i)
    ), linked_tasks AS (
        SELECT t.id, t.dataset_item_id, t.project_id FROM yoao_labeling_v1.task_rows AS t
        WHERE t.dataset_item_id IN (SELECT i.id FROM dataset_items AS i)
           OR t.id IN (SELECT l.task_id FROM item_links AS l)
    ), linked_projects AS (
        SELECT p.id, p.dataset_id, p.project_id FROM public.project_datasets AS p
        WHERE p.dataset_id = p_dataset_id
    ), task_annotations AS (
        SELECT a.id, a.task_id FROM yoao_labeling_v1.annotation_rows AS a
        WHERE a.task_id IN (SELECT t.id FROM linked_tasks AS t)
    ), scans AS (
        SELECT r.id, r.source_id, r.status, r.generation FROM public.dataset_scan_runs AS r
        WHERE r.source_id IN (SELECT s.id FROM dataset_sources AS s)
          AND r.status IN ('pending', 'running')
    ), registrations AS (
        SELECT r.id, r.source_id, r.status, r.generation FROM public.dataset_registration_runs AS r
        WHERE r.source_id IN (SELECT s.id FROM dataset_sources AS s)
          AND r.status IN ('pending', 'running')
    ), schedules AS (
        SELECT r.source_id, r.revision, r.enabled FROM public.dataset_source_sync_schedules AS r
        WHERE r.source_id IN (SELECT s.id FROM dataset_sources AS s) AND r.enabled
    ), jobs AS (
        SELECT j.id, j.kind, j.status FROM yoao_jobs_v1.dataset_active_job_facts(p_dataset_id) AS j
    ), versions AS (
        SELECT v.id, v.status, v.snapshot_digest
        FROM yoao_learning_v1.training_dataset_asset_versions(p_dataset_id, false) AS v
        WHERE v.status IN ('pending', 'running', 'ready')
    ), project_ids AS (
        SELECT p.project_id FROM linked_projects AS p
        UNION
        SELECT t.project_id FROM linked_tasks AS t
    ), relationship_parts AS (
        SELECT
            (SELECT pg_catalog.count(*) FROM dataset_items) AS item_count,
            (SELECT pg_catalog.count(*) FROM dataset_sources) AS source_count,
            (SELECT pg_catalog.count(*) FROM project_ids) AS project_count,
            (SELECT pg_catalog.count(*) FROM linked_tasks) AS task_count,
            (SELECT pg_catalog.count(*) FROM task_annotations) AS annotation_count,
            (SELECT pg_catalog.count(*) FROM scans) AS scan_count,
            (SELECT pg_catalog.count(*) FROM registrations) AS registration_count,
            (SELECT pg_catalog.count(*) FROM schedules) AS schedule_count,
            (SELECT pg_catalog.count(*) FROM jobs) AS job_count,
            (SELECT pg_catalog.count(*) FROM versions AS v WHERE v.status = 'ready') AS ready_count,
            (SELECT pg_catalog.count(*) FROM versions AS v WHERE v.status IN ('pending', 'running')) AS building_count,
            ARRAY[
                (SELECT pg_catalog.string_agg(yoao_assets_v1._dataset_relationship_digest(
                        pg_catalog.jsonb_build_array(i.id, i.dataset_id)::text),
                        '' ORDER BY i.id, i.dataset_id) FROM dataset_items AS i),
                (SELECT pg_catalog.string_agg(yoao_assets_v1._dataset_relationship_digest(
                        pg_catalog.jsonb_build_array(s.id, s.location_id, s.config_revision,
                            s.policy_revision, s.enabled, s.unregistered_at, s.generation)::text),
                        '' ORDER BY s.id, s.location_id, s.config_revision, s.policy_revision,
                            s.enabled, s.unregistered_at, s.generation) FROM dataset_sources AS s),
                (SELECT pg_catalog.string_agg(yoao_assets_v1._dataset_relationship_digest(
                        pg_catalog.jsonb_build_array(p.id, p.dataset_id, p.project_id)::text),
                        '' ORDER BY p.id, p.dataset_id, p.project_id) FROM linked_projects AS p),
                (SELECT pg_catalog.string_agg(yoao_assets_v1._dataset_relationship_digest(
                        pg_catalog.jsonb_build_array(t.id, t.dataset_item_id, t.project_id)::text),
                        '' ORDER BY t.id, t.dataset_item_id, t.project_id) FROM linked_tasks AS t),
                (SELECT pg_catalog.string_agg(yoao_assets_v1._dataset_relationship_digest(
                        pg_catalog.jsonb_build_array(l.id, l.task_id, l.dataset_item_id, l.role)::text),
                        '' ORDER BY l.id, l.task_id, l.dataset_item_id, l.role) FROM item_links AS l),
                (SELECT pg_catalog.string_agg(yoao_assets_v1._dataset_relationship_digest(
                        pg_catalog.jsonb_build_array(a.id, a.task_id)::text),
                        '' ORDER BY a.id, a.task_id) FROM task_annotations AS a),
                (SELECT pg_catalog.string_agg(yoao_assets_v1._dataset_relationship_digest(
                        pg_catalog.jsonb_build_array(r.id, r.source_id, r.status, r.generation)::text),
                        '' ORDER BY r.id, r.source_id, r.status, r.generation) FROM scans AS r),
                (SELECT pg_catalog.string_agg(yoao_assets_v1._dataset_relationship_digest(
                        pg_catalog.jsonb_build_array(r.id, r.source_id, r.status, r.generation)::text),
                        '' ORDER BY r.id, r.source_id, r.status, r.generation) FROM registrations AS r),
                (SELECT pg_catalog.string_agg(yoao_assets_v1._dataset_relationship_digest(
                        pg_catalog.jsonb_build_array(r.source_id, r.revision, r.enabled)::text),
                        '' ORDER BY r.source_id, r.revision, r.enabled) FROM schedules AS r),
                (SELECT pg_catalog.string_agg(yoao_assets_v1._dataset_relationship_digest(
                        pg_catalog.jsonb_build_array(j.id, j.kind, j.status)::text),
                        '' ORDER BY j.id, j.kind, j.status) FROM jobs AS j),
                (SELECT pg_catalog.string_agg(yoao_assets_v1._dataset_relationship_digest(
                        pg_catalog.jsonb_build_array(v.id, v.status, v.snapshot_digest)::text),
                        '' ORDER BY v.id, v.status, v.snapshot_digest) FROM versions AS v)
            ] AS parts
    )
    SELECT pg_catalog.statement_timestamp(), p.item_count, p.source_count, p.project_count,
           p.task_count, p.annotation_count, p.scan_count, p.registration_count,
           p.schedule_count, p.job_count, p.ready_count, p.building_count,
           yoao_assets_v1._dataset_relationship_digest((
               SELECT pg_catalog.jsonb_agg(yoao_assets_v1._dataset_relationship_digest(COALESCE(part, ''))
                                           ORDER BY ordinal)::text
               FROM pg_catalog.unnest(p.parts) WITH ORDINALITY AS sequence(part, ordinal)
           ))
    FROM relationship_parts AS p;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_archive_impact(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_source_display_locations(p_actor_id uuid, p_location_ids uuid[])
RETURNS TABLE (id uuid, kind text, config jsonb)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT loc.id, loc.kind::text,
           pg_catalog.jsonb_build_object('cache_policy', COALESCE(loc.config ->> 'cache_policy', 'preview'))
    FROM public.storage_locations AS loc
    WHERE loc.id = ANY (p_location_ids) AND yoao_assets_v1.dataset_location_visible(p_actor_id, loc.id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_source_display_locations(uuid, uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_scan_entry_page(
    p_actor_id uuid, p_run_id uuid, p_limit integer, p_offset bigint
)
RETURNS jsonb
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH run AS (
        SELECT r.id, r.source_id, r.snapshot -> 'anchor' AS anchor
        FROM public.dataset_scan_runs AS r
        WHERE r.id = p_run_id AND yoao_assets_v1.dataset_source_visible(p_actor_id, r.source_id)
    ), page AS (
        SELECT e.path_digest, e.relative_path, e.kind, e.state, e.observed -> 'size' AS size_bytes,
               e.verified_content, r.id AS run_id, r.source_id, r.anchor
        FROM run AS r JOIN public.dataset_scan_entries AS e ON e.run_id = r.id
        WHERE e.relative_path <> ''
        ORDER BY e.path_digest
        LIMIT LEAST(GREATEST(p_limit, 0), 200) OFFSET GREATEST(p_offset, 0)
    ), details AS (
        SELECT page.*, head.dataset_item_id AS head_item_id, preview.state AS preview_state,
               preview.size_bytes AS preview_bytes,
               CASE
                   WHEN history.id IS NOT NULL THEN history.dataset_item_id
                   WHEN page.verified_content IS NOT NULL
                    AND head.content_sha256 = page.verified_content ->> 'sha256'
                    AND baseline.verified_content -> 'observation' = page.verified_content -> 'observation'
                    AND baseline_run.snapshot -> 'anchor' = page.anchor
                   THEN head.dataset_item_id
               END AS registered_item_id
        FROM page
        LEFT JOIN public.dataset_source_items AS head
               ON head.source_id = page.source_id AND head.path_digest = page.path_digest
        LEFT JOIN public.dataset_scan_entries AS baseline
               ON baseline.run_id = head.run_id AND baseline.path_digest = head.path_digest
        LEFT JOIN public.dataset_scan_runs AS baseline_run ON baseline_run.id = head.run_id
        LEFT JOIN LATERAL (
            SELECT revision.id, revision.dataset_item_id FROM public.dataset_source_revisions AS revision
            WHERE revision.run_id = page.run_id AND revision.source_id = page.source_id
              AND revision.path_digest = page.path_digest
            ORDER BY revision.acceptance_order DESC LIMIT 1
        ) AS history ON true
        LEFT JOIN public.dataset_source_previews AS preview
               ON preview.source_id = page.source_id AND preview.path_digest = page.path_digest
    )
    SELECT pg_catalog.jsonb_build_object(
        'total', (SELECT pg_catalog.count(*) FROM public.dataset_scan_entries AS e
                  WHERE e.run_id = (SELECT id FROM run) AND e.relative_path <> ''),
        'items', COALESCE((SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
            'path_digest', d.path_digest, 'relative_path', d.relative_path,
            'kind', d.kind, 'state', d.state, 'size_bytes', d.size_bytes,
            'content_verified', d.verified_content IS NOT NULL,
            'registered_item_id', d.registered_item_id,
            'preview_state', CASE WHEN d.head_item_id = d.registered_item_id THEN d.preview_state END,
            'preview_bytes', CASE WHEN d.head_item_id = d.registered_item_id THEN d.preview_bytes END,
            'verified_content', CASE WHEN d.verified_content IS NOT NULL THEN pg_catalog.jsonb_build_object(
                'sha256', d.verified_content -> 'sha256',
                'size_bytes', d.verified_content -> 'size_bytes',
                'media_type', d.verified_content -> 'media_type',
                'width', d.verified_content -> 'width',
                'height', d.verified_content -> 'height',
                'verified_at', d.verified_content -> 'verified_at'
            ) END
        ) ORDER BY d.path_digest) FROM details AS d), '[]'::jsonb)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_scan_entry_page(uuid, uuid, integer, bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_dataset_names(p_actor_id uuid, p_location_id uuid, p_ids uuid[])
RETURNS TABLE (id uuid, name text)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT d.id, d.name::text FROM public.datasets AS d
    WHERE d.id = ANY (p_ids) AND EXISTS (
        SELECT 1 FROM public.dataset_sources AS s
        WHERE s.dataset_id = d.id AND s.location_id = p_location_id
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, s.id)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_dataset_names(uuid, uuid, uuid[]) FROM PUBLIC;
