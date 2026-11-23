CREATE FUNCTION yoao_assets_v1.directory_source_active_attempts(p_actor_id uuid, p_source_id uuid)
RETURNS TABLE (kind text, id uuid, async_job_id uuid, generation integer, status text)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT kind, id, async_job_id, generation, status FROM (
        SELECT 'scan' AS kind, r.id, r.async_job_id, r.generation, r.status::text
        FROM public.dataset_scan_runs AS r
        WHERE r.source_id = p_source_id AND r.status IN ('pending', 'running')
        UNION ALL
        SELECT 'registration', r.id, r.async_job_id, r.generation, r.status::text
        FROM public.dataset_registration_runs AS r
        WHERE r.source_id = p_source_id AND r.status IN ('pending', 'running')
    ) AS attempts
    WHERE yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id)
    ORDER BY CASE WHEN kind = 'scan' THEN 0 ELSE 1 END, id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_source_active_attempts(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_source_impact(p_actor_id uuid, p_source_id uuid)
RETURNS TABLE (
    item_count bigint, project_count bigint, task_count bigint,
    annotation_count bigint, preview_count bigint, preview_bytes bigint
)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH items AS (
        SELECT DISTINCT revision.dataset_item_id
        FROM public.dataset_source_revisions AS revision
        WHERE revision.source_id = p_source_id AND revision.dataset_item_id IS NOT NULL
    ), tasks AS (
        SELECT t.id, t.project_id FROM yoao_labeling_v1.task_rows AS t
        WHERE t.dataset_item_id IN (SELECT dataset_item_id FROM items)
    ), previews AS (
        SELECT pg_catalog.count(*) AS count, COALESCE(pg_catalog.sum(p.size_bytes), 0)::bigint AS bytes
        FROM public.dataset_source_previews AS p
        WHERE p.source_id = p_source_id AND p.state = 'ready'
    )
    SELECT (SELECT pg_catalog.count(*) FROM items),
           (SELECT pg_catalog.count(DISTINCT t.project_id) FROM tasks AS t),
           (SELECT pg_catalog.count(*) FROM tasks),
           (SELECT pg_catalog.count(*) FROM yoao_labeling_v1.annotation_rows AS a
            WHERE a.task_id IN (SELECT t.id FROM tasks AS t)),
           previews.count, previews.bytes
    FROM previews WHERE yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_source_impact(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.change_directory_source_lifecycle(
    p_actor_id uuid, p_source_id uuid, p_revision integer, p_generation integer,
    p_action text, p_unregistered_at timestamptz
)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH changed AS (
        UPDATE public.dataset_sources AS source
        SET enabled = p_action = 'enable',
            unregistered_at = CASE WHEN p_action = 'unregister' THEN p_unregistered_at END,
            config_revision = source.config_revision + 1,
            generation = source.generation + 1
        WHERE source.id = p_source_id AND source.config_revision = p_revision
          AND source.generation = p_generation AND source.unregistered_at IS NULL
          AND p_action IN ('enable', 'pause', 'unregister')
          AND (p_action <> 'unregister' OR p_unregistered_at IS NOT NULL)
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id)
        RETURNING source.id
    ) SELECT EXISTS (SELECT 1 FROM changed)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.change_directory_source_lifecycle(uuid, uuid, integer, integer, text, timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.declare_directory_source_protection(
    p_actor_id uuid, p_source_id uuid, p_revision integer, p_declaration jsonb
)
RETURNS boolean
LANGUAGE sql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH changed AS (
        UPDATE public.dataset_sources AS source
        SET external_protection_revision = source.external_protection_revision + 1,
            external_protection = p_declaration
        WHERE source.id = p_source_id AND source.external_protection_revision = p_revision
          AND p_declaration ->> 'recorded_by_id' = p_actor_id::text
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id)
        RETURNING source.id
    ) SELECT EXISTS (SELECT 1 FROM changed)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.declare_directory_source_protection(uuid, uuid, integer, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_source_selection_heads(p_actor_id uuid, p_source_id uuid)
RETURNS TABLE (path_digest text, current_revision_id uuid, relative_path text)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT head.path_digest::text, head.current_revision_id, entry.relative_path
    FROM public.dataset_source_items AS head
    LEFT JOIN public.dataset_scan_entries AS entry
           ON entry.run_id = head.run_id AND entry.path_digest = head.path_digest
    WHERE head.source_id = p_source_id AND yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id)
    ORDER BY head.path_digest LIMIT 50001
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_source_selection_heads(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.apply_directory_source_selection(
    p_actor_id uuid, p_source_id uuid, p_revision integer, p_generation integer, p_rules jsonb
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_rules) <> 'object'
       OR pg_catalog.jsonb_typeof(p_rules -> 'recursive') IS DISTINCT FROM 'boolean'
       OR pg_catalog.jsonb_typeof(p_rules -> 'include_globs') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_rules -> 'exclude_globs') IS DISTINCT FROM 'array'
       OR p_rules - ARRAY['recursive', 'include_globs', 'exclude_globs'] <> '{}'::jsonb THEN
        RAISE EXCEPTION 'invalid directory selection rules' USING ERRCODE = '22023';
    END IF;
    UPDATE public.dataset_sources AS source
    SET config = source.config || p_rules,
        config_revision = source.config_revision + 1, generation = source.generation + 1
    WHERE source.id = p_source_id AND source.config_revision = p_revision
      AND source.generation = p_generation AND source.unregistered_at IS NULL
      AND yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id)
      AND NOT EXISTS (SELECT 1 FROM yoao_assets_v1.directory_source_active_attempts(p_actor_id, p_source_id));
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM source_id FROM public.dataset_source_sync_schedules WHERE source_id = p_source_id FOR UPDATE;
    UPDATE public.dataset_source_sync_schedules AS schedule
    SET enabled = false, state = 'blocked', last_error = 'configuration_changed',
        revision = schedule.revision + 1, dispatch_token = NULL, dispatch_expires_at = NULL,
        scan_id = NULL, registration_id = NULL
    WHERE schedule.source_id = p_source_id;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.apply_directory_source_selection(uuid, uuid, integer, integer, jsonb) FROM PUBLIC;
