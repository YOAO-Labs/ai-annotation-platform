CREATE FUNCTION yoao_assets_v1._dataset_workbench_preferred_mapping(
    p_actor_id uuid, p_item_id uuid, p_preferred_source_id uuid
)
RETURNS TABLE (source_id uuid, path_digest text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT head.source_id, head.path_digest::text
    FROM public.dataset_source_items AS head
    JOIN public.dataset_sources AS source ON source.id = head.source_id
    JOIN public.storage_locations AS location ON location.id = source.location_id
    LEFT JOIN public.dataset_source_previews AS preview
      ON preview.source_id = head.source_id AND preview.path_digest = head.path_digest
    WHERE head.dataset_item_id = p_item_id
      AND (p_preferred_source_id IS NULL OR head.source_id = p_preferred_source_id)
      AND yoao_assets_v1.dataset_source_visible(p_actor_id, source.id)
    ORDER BY CASE WHEN source.enabled AND source.unregistered_at IS NULL AND location.enabled
                      AND source.cache_policy IN ('preview', 'on_demand')
                      AND COALESCE(location.config ->> 'cache_policy', 'preview') IN ('preview', 'on_demand')
                      AND preview.state = 'ready' AND preview.asset_id = head.asset_id
                      AND preview.source_sha256 = head.content_sha256
                  THEN 0 ELSE 1 END,
             head.source_id
    LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_workbench_preferred_mapping(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_workbench_preview_mapping(
    p_actor_id uuid, p_dataset_id uuid, p_item_id uuid
)
RETURNS TABLE (source_id uuid, path_digest text)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT mapping.source_id, mapping.path_digest
    FROM public.dataset_items AS item
    CROSS JOIN LATERAL yoao_assets_v1._dataset_workbench_preferred_mapping(p_actor_id, item.id, NULL) AS mapping
    WHERE item.id = p_item_id AND item.dataset_id = p_dataset_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_workbench_preview_mapping(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_workbench_observations(
    p_actor_id uuid, p_dataset_id uuid, p_reference_source_ids uuid[], p_item_ids uuid[],
    p_preferred_source_id uuid, p_details boolean
)
RETURNS TABLE (
    item_id uuid, state text, observed_at timestamptz, preview_available boolean,
    source_id uuid, source_name text, location_id uuid, is_reference boolean, can_manage boolean,
    agent_unavailable_reason text, agent_state text, agent_reason_code text, agent_site_name text,
    agent_name text, agent_last_seen timestamptz, agent_measured_at timestamptz,
    asset_id uuid, source_disabled boolean, preview_requested boolean
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT item.id,
           CASE
               WHEN flags.is_reference AND evidence.replaced THEN 'changed'
               WHEN flags.is_reference AND flags.disabled THEN
                   CASE WHEN evidence.preview_ready THEN 'preview_only' ELSE 'unavailable' END
               WHEN flags.is_reference AND change.kind = 'changed' THEN 'changed'
               WHEN flags.is_reference AND change.kind = 'not_seen' THEN 'missing'
               WHEN flags.is_reference AND (change.kind = 'unreadable' OR flags.agent_blocked) THEN
                   CASE WHEN evidence.preview_ready THEN 'preview_only' ELSE 'unavailable' END
               WHEN flags.is_reference AND asset.verification_state = 'verified' AND source.id IS NOT NULL
                   THEN 'verified'
               WHEN NOT flags.is_reference AND asset.verification_state = 'verified' AND replica.verified_at IS NOT NULL
                   THEN 'verified'
               ELSE 'unverified'
           END,
           CASE WHEN p_details THEN CASE WHEN flags.is_reference
               THEN COALESCE(change.observed_at, accepted.accepted_at, item.created_at)
               ELSE replica.verified_at END END,
           p_details AND evidence.preview_ready,
           CASE WHEN p_details AND flags.manageable THEN source.id END,
           CASE WHEN p_details AND flags.manageable THEN source.name::text END,
           CASE WHEN p_details AND flags.manageable THEN source.location_id END,
           flags.is_reference,
           flags.manageable,
           CASE WHEN p_details AND flags.manageable AND flags.is_reference AND NOT flags.disabled
                     AND NOT evidence.replaced AND flags.agent_blocked
                     AND (change.kind IS NULL OR change.kind NOT IN ('changed', 'not_seen', 'unreadable'))
                THEN COALESCE(agent.agent_reason_code, 'agent_unavailable') END,
           CASE WHEN p_details AND flags.manageable AND location.kind = 'agent'
                THEN COALESCE(agent.agent_state, 'unavailable') END,
           CASE WHEN p_details AND flags.manageable AND location.kind = 'agent'
                THEN COALESCE(agent.agent_reason_code, 'agent_unavailable') END,
           CASE WHEN p_details AND flags.manageable AND location.kind = 'agent' THEN agent.agent_site_name::text END,
           CASE WHEN p_details AND flags.manageable AND location.kind = 'agent' THEN agent.agent_name::text END,
           CASE WHEN p_details AND flags.manageable AND location.kind = 'agent' THEN agent.agent_last_seen END,
           CASE WHEN p_details AND flags.manageable AND location.kind = 'agent' THEN agent.agent_measured_at END,
           asset.id, flags.disabled, flags.preview_requested
    FROM public.dataset_items AS item
    LEFT JOIN public.dataset_assets AS asset ON asset.dataset_item_id = item.id
    LEFT JOIN public.dataset_references AS ref ON ref.dataset_item_id = item.id
    LEFT JOIN public.dataset_sources AS source ON source.id = COALESCE(
        ref.source_id,
        CASE WHEN p_details THEN (
            SELECT mapping.source_id
            FROM yoao_assets_v1._dataset_workbench_preferred_mapping(
                p_actor_id, item.id, p_preferred_source_id
            ) AS mapping
        ) END
    )
    LEFT JOIN public.storage_locations AS location ON location.id = source.location_id
    LEFT JOIN yoao_assets_storage_v1.agent_location_status AS agent
      ON location.kind = 'agent' AND agent.location_id = location.id
    LEFT JOIN LATERAL (
        SELECT revision.id, revision.accepted_at
        FROM public.dataset_source_revisions AS revision
        WHERE revision.source_id = ref.source_id
          AND revision.item_id_at_accept = item.id AND revision.asset_id_at_accept = asset.id
        ORDER BY revision.acceptance_order DESC
        LIMIT 1
    ) AS accepted ON true
    LEFT JOIN LATERAL (
        SELECT difference.kind, scan.completed_at AS observed_at
        FROM public.dataset_source_changes AS difference
        JOIN public.dataset_registration_runs AS registration ON registration.id = difference.run_id
        JOIN public.dataset_scan_runs AS scan ON scan.id = registration.scan_run_id
        WHERE difference.before_item_id = item.id AND registration.source_id = ref.source_id
          AND registration.comparison_complete AND difference.before_revision_id = accepted.id
        ORDER BY scan.completed_at DESC NULLS LAST, scan.created_at DESC, scan.id
        LIMIT 1
    ) AS change ON true
    LEFT JOIN LATERAL (
        SELECT locator.verified_at
        FROM public.dataset_asset_locations AS locator
        JOIN public.storage_locations AS target ON target.id = locator.location_id
        WHERE ref.dataset_item_id IS NULL AND locator.asset_id = asset.id
          AND locator.role = 'managed_original' AND locator.reference_state = 'present'
          AND locator.object_key = item.file_path AND target.enabled
        ORDER BY locator.verified_at DESC NULLS LAST
        LIMIT 1
    ) AS replica ON true
    CROSS JOIN LATERAL (
        SELECT ref.dataset_item_id IS NOT NULL AS is_reference,
               COALESCE(yoao_assets_v1.dataset_source_visible(p_actor_id, source.id), false) AS manageable,
               source.enabled IS FALSE OR source.unregistered_at IS NOT NULL OR location.enabled IS FALSE AS disabled,
               source.cache_policy IN ('preview', 'on_demand')
                   AND COALESCE(location.config ->> 'cache_policy', 'preview') IN ('preview', 'on_demand') AS preview_requested,
               location.kind = 'agent'
                   AND COALESCE(agent.agent_reason_code, 'agent_unavailable') <> 'agent_online' AS agent_blocked
    ) AS flags
    CROSS JOIN LATERAL (
        SELECT EXISTS (
            SELECT 1 FROM public.dataset_source_items AS head
            WHERE head.source_id = ref.source_id AND head.path_digest = ref.path_digest
              AND head.dataset_item_id <> item.id
        ) AS replaced,
        COALESCE(flags.manageable AND source.enabled AND source.unregistered_at IS NULL
            AND location.enabled AND flags.preview_requested AND EXISTS (
                SELECT 1 FROM public.dataset_source_previews AS preview
                JOIN public.dataset_source_items AS head
                  ON head.source_id = preview.source_id AND head.path_digest = preview.path_digest
                WHERE preview.source_id = source.id AND head.dataset_item_id = item.id
                  AND preview.asset_id = asset.id AND preview.source_sha256 = asset.checksum_value
                  AND preview.state = 'ready'
            ), false) AS preview_ready
    ) AS evidence
    WHERE p_actor_id IS NOT NULL AND (p_dataset_id IS NOT NULL OR p_reference_source_ids IS NOT NULL)
      AND (p_dataset_id IS NULL OR item.dataset_id = p_dataset_id)
      AND (p_item_ids IS NULL OR item.id = ANY(p_item_ids))
      AND (p_reference_source_ids IS NULL OR (ref.source_id = ANY(p_reference_source_ids) AND flags.manageable))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_workbench_observations(uuid, uuid, uuid[], uuid[], uuid, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_workbench_item_states(p_actor_id uuid, p_dataset_id uuid)
RETURNS TABLE (item_id uuid, state text)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT item.item_id, item.state
    FROM yoao_assets_v1._dataset_workbench_observations(p_actor_id, p_dataset_id, NULL, NULL, NULL, false) AS item
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_workbench_item_states(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_workbench_item_matches(
    p_actor_id uuid, p_dataset_id uuid, p_source_id uuid, p_state text, p_search text,
    p_scene_id uuid, p_media_type text, p_extension text, p_dimension_state text,
    p_min_width integer, p_max_width integer, p_min_height integer, p_max_height integer, p_item_id uuid
)
RETURNS TABLE (item_id uuid, created_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT item.id, item.created_at
    FROM public.dataset_items AS item
    CROSS JOIN LATERAL (SELECT COALESCE(item.width > 0 AND item.height > 0, false) AS known) AS dimensions
    WHERE p_actor_id IS NOT NULL AND item.dataset_id = p_dataset_id
      AND (p_item_id IS NULL OR item.id = p_item_id)
      AND (p_source_id IS NULL OR (
          EXISTS (SELECT 1 FROM public.dataset_sources AS source
                  WHERE source.id = p_source_id AND source.dataset_id = p_dataset_id
                    AND yoao_assets_v1.dataset_source_visible(p_actor_id, source.id))
          AND (
              EXISTS (SELECT 1 FROM public.dataset_source_items AS head
                      WHERE head.source_id = p_source_id AND head.dataset_item_id = item.id)
              OR EXISTS (SELECT 1 FROM public.dataset_references AS ref
                         WHERE ref.source_id = p_source_id AND ref.dataset_item_id = item.id)
          )
      ))
      AND (p_scene_id IS NULL OR (
          item.scene_id = p_scene_id AND EXISTS (SELECT 1 FROM public.scenes AS scene
                                                WHERE scene.id = p_scene_id AND scene.dataset_id = p_dataset_id)
      ))
      AND (p_search IS NULL OR item.file_name ILIKE ('%' || p_search || '%'))
      AND (p_media_type IS NULL OR item.file_type = p_media_type)
      AND (p_extension IS NULL OR (
          item.file_name ~ '[^/]+[.][^/.]+$'
          AND pg_catalog.lower(substring(item.file_name FROM '[^/.]+$')) IN (
              p_extension, CASE WHEN p_extension = 'jpg' THEN 'jpeg' ELSE p_extension END
          )
      ))
      AND (p_dimension_state IS DISTINCT FROM 'known' OR dimensions.known)
      AND (p_dimension_state IS DISTINCT FROM 'unknown' OR NOT dimensions.known)
      AND (pg_catalog.num_nonnulls(p_min_width, p_max_width, p_min_height, p_max_height) = 0 OR dimensions.known)
      AND (p_min_width IS NULL OR item.width >= p_min_width)
      AND (p_max_width IS NULL OR item.width <= p_max_width)
      AND (p_min_height IS NULL OR item.height >= p_min_height)
      AND (p_max_height IS NULL OR item.height <= p_max_height)
      AND CASE WHEN p_state IS NULL THEN true ELSE item.id IN (
          SELECT observation.item_id
          FROM yoao_assets_v1._dataset_workbench_observations(
              p_actor_id, p_dataset_id, NULL,
              CASE WHEN p_item_id IS NOT NULL THEN ARRAY[p_item_id] END, NULL, false
          ) AS observation
          WHERE observation.state = p_state
      ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_workbench_item_matches(uuid, uuid, uuid, text, text, uuid, text, text, text, integer, integer, integer, integer, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._dataset_workbench_cache_evidence(
    p_actor_id uuid, p_dataset_id uuid, p_reference_source_ids uuid[], p_item_ids uuid[]
)
RETURNS TABLE (
    item_id uuid, id uuid, scope_location_id uuid, asset_id uuid, sha256 text, size_bytes bigint,
    location_id uuid, bucket text, binding_generation integer, storage_identity text,
    storage_ref jsonb, verified_at timestamptz, expires_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT item.id, cache.id, cache.scope_location_id, cache.asset_id, cache.sha256::text,
           cache.size_bytes, cache.location_id, cache.bucket::text, cache.binding_generation,
           cache.storage_identity::text, cache.storage_ref, cache.verified_at, cache.expires_at
    FROM public.original_cache_entries AS cache
    JOIN public.dataset_assets AS asset ON asset.id = cache.asset_id
    JOIN public.dataset_items AS item ON item.id = asset.dataset_item_id
    JOIN public.dataset_references AS ref ON ref.dataset_item_id = item.id AND ref.source_id = cache.source_id
    JOIN public.dataset_sources AS source ON source.id = ref.source_id AND source.location_id = cache.scope_location_id
    JOIN public.storage_locations AS location ON location.id = source.location_id
    JOIN public.dataset_asset_locations AS locator
      ON locator.asset_id = asset.id AND locator.location_id = cache.location_id AND locator.bucket = cache.bucket
     AND locator.role = 'original_cache' AND locator.reference_state = 'present'
     AND locator.object_key = CASE WHEN cache.storage_ref IS NOT NULL
             THEN (cache.storage_ref ->> 'prefix') || 'payload'
             ELSE pg_catalog.concat('original-cache/v1/', cache.asset_id, '/', cache.sha256, '/', cache.id)
         END
    WHERE (p_dataset_id IS NOT NULL OR p_reference_source_ids IS NOT NULL)
      AND (p_dataset_id IS NULL OR item.dataset_id = p_dataset_id)
      AND (p_reference_source_ids IS NULL OR ref.source_id = ANY(p_reference_source_ids))
      AND (p_item_ids IS NULL OR item.id = ANY(p_item_ids))
      AND yoao_assets_v1.dataset_source_visible(p_actor_id, source.id)
      AND asset.verification_state = 'verified' AND cache.sha256 = asset.checksum_value
      AND cache.size_bytes = asset.size_bytes
      AND source.enabled AND source.unregistered_at IS NULL AND location.enabled
      AND source.ownership_mode = 'reference' AND source.cache_policy = 'on_demand'
      AND location.config ->> 'cache_policy' = 'on_demand'
      AND location.config ->> 'original_cache_quota_bytes' <> '0'
      AND cache.source_revision = source.config_revision AND cache.policy_revision = source.policy_revision
      AND cache.location_revision = location.config_revision
      AND cache.state = 'ready' AND cache.expires_at > pg_catalog.now()
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._dataset_workbench_cache_evidence(uuid, uuid, uuid[], uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_workbench_cache_candidates(
    p_actor_id uuid, p_dataset_id uuid, p_reference_source_ids uuid[], p_item_ids uuid[]
)
RETURNS TABLE (
    id uuid, scope_location_id uuid, asset_id uuid, sha256 text, size_bytes bigint,
    location_id uuid, bucket text, binding_generation integer, storage_identity text, storage_ref jsonb
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT cache.id, cache.scope_location_id, cache.asset_id, cache.sha256, cache.size_bytes,
           cache.location_id, cache.bucket, cache.binding_generation, cache.storage_identity, cache.storage_ref
    FROM yoao_assets_v1._dataset_workbench_cache_evidence(
        p_actor_id, p_dataset_id, p_reference_source_ids, p_item_ids
    ) AS cache
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_workbench_cache_candidates(uuid, uuid, uuid[], uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_workbench_page_observations(
    p_actor_id uuid, p_dataset_id uuid, p_preferred_source_id uuid, p_item_ids uuid[], p_cache_ids uuid[]
)
RETURNS TABLE (
    item_id uuid, state text, observed_at timestamptz, preview_available boolean,
    original_cache_entry_id uuid, original_cache_recorded boolean,
    original_cache_recorded_at timestamptz, original_cache_expires_at timestamptz,
    source_id uuid, source_name text, location_id uuid, is_reference boolean, can_manage boolean,
    agent_unavailable_reason text, reference_replica_recorded boolean,
    agent_state text, agent_reason_code text, agent_site_name text, agent_name text,
    agent_last_seen timestamptz, agent_measured_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH input AS (
        SELECT CASE WHEN p_dataset_id IS NOT NULL AND pg_catalog.cardinality(p_item_ids) BETWEEN 1 AND 200
                    THEN p_item_ids ELSE ARRAY[]::uuid[] END AS item_ids
    ), cache AS (
        SELECT record.item_id, record.id, record.verified_at, record.expires_at
        FROM input
        CROSS JOIN LATERAL yoao_assets_v1._dataset_workbench_cache_evidence(
            p_actor_id, p_dataset_id, NULL,
            CASE WHEN pg_catalog.cardinality(p_cache_ids) > 0 THEN input.item_ids ELSE ARRAY[]::uuid[] END
        ) AS record
        WHERE record.id = ANY(p_cache_ids)
    )
    SELECT item.item_id, item.state, item.observed_at, item.preview_available,
           cache.id, cache.verified_at IS NOT NULL, cache.verified_at, cache.expires_at,
           item.source_id, item.source_name, item.location_id, item.is_reference, item.can_manage,
           item.agent_unavailable_reason,
           CASE WHEN item.can_manage AND item.is_reference AND NOT item.source_disabled
                THEN COALESCE(yoao_assets_v1.directory_reference_replica_available(item.asset_id, p_actor_id), false)
                ELSE false END,
           item.agent_state, item.agent_reason_code, item.agent_site_name, item.agent_name,
           item.agent_last_seen, item.agent_measured_at
    FROM input
    CROSS JOIN LATERAL yoao_assets_v1._dataset_workbench_observations(
        p_actor_id, p_dataset_id, NULL, input.item_ids, p_preferred_source_id, true
    ) AS item
    LEFT JOIN cache ON cache.item_id = item.item_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_workbench_page_observations(uuid, uuid, uuid, uuid[], uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_workbench_source_reference_evidence(
    p_actor_id uuid, p_source_ids uuid[], p_cache_ids uuid[], p_preview_source_ids uuid[], p_preview_digests text[]
)
RETURNS TABLE (
    source_id uuid, original text, preview text, count bigint, observed_from timestamptz,
    observed_through timestamptz, preview_recorded_at timestamptz,
    original_cache_recorded_count bigint, original_cache_recorded_at timestamptz
)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH usable_previews AS MATERIALIZED (
        SELECT key.source_id, key.path_digest
        FROM ROWS FROM (pg_catalog.unnest(p_preview_source_ids), pg_catalog.unnest(p_preview_digests))
            AS key(source_id, path_digest)
    ), usable_caches AS MATERIALIZED (
        SELECT pg_catalog.unnest(p_cache_ids) AS id
    ), observations AS MATERIALIZED (
        SELECT item.item_id, item.asset_id, item.source_id, item.state, item.observed_at,
               item.source_disabled, item.preview_requested
        FROM yoao_assets_v1._dataset_workbench_observations(p_actor_id, NULL, p_source_ids, NULL, NULL, true) AS item
    ), cache AS (
        SELECT record.item_id, record.verified_at
        FROM yoao_assets_v1._dataset_workbench_cache_evidence(
            p_actor_id, NULL,
            CASE WHEN pg_catalog.cardinality(p_cache_ids) > 0 THEN p_source_ids ELSE ARRAY[]::uuid[] END,
            NULL
        ) AS record
        WHERE EXISTS (SELECT 1 FROM usable_caches AS usable WHERE usable.id = record.id)
    ), previews AS (
        SELECT obs.item_id,
               pg_catalog.max(preview.measured_at) FILTER (WHERE preview.state = 'ready' AND usable.source_id IS NOT NULL) AS ready_at,
               pg_catalog.max(preview.measured_at) FILTER (WHERE preview.state = 'failed') AS failed_at
        FROM observations AS obs
        JOIN public.dataset_assets AS asset ON asset.id = obs.asset_id
        JOIN public.dataset_source_items AS head
          ON head.source_id = obs.source_id AND head.dataset_item_id = obs.item_id
         AND head.asset_id = asset.id AND head.content_sha256 = asset.checksum_value
        JOIN public.dataset_source_previews AS preview
          ON preview.source_id = head.source_id AND preview.path_digest = head.path_digest
         AND preview.asset_id = asset.id AND preview.source_sha256 = asset.checksum_value
        LEFT JOIN usable_previews AS usable ON usable.source_id = preview.source_id AND usable.path_digest = preview.path_digest
        WHERE NOT obs.source_disabled AND obs.preview_requested
          AND asset.verification_state = 'verified' AND preview.spec = 'webp-256-exif-q80'
        GROUP BY obs.item_id
    )
    SELECT obs.source_id, obs.state, status.preview, pg_catalog.count(*),
           pg_catalog.min(obs.observed_at), pg_catalog.max(obs.observed_at),
           pg_catalog.max(CASE status.preview WHEN 'ready' THEN evidence.ready_at WHEN 'failed' THEN evidence.failed_at END),
           pg_catalog.count(cache.verified_at), pg_catalog.max(cache.verified_at)
    FROM observations AS obs
    LEFT JOIN cache ON cache.item_id = obs.item_id
    LEFT JOIN previews AS evidence ON evidence.item_id = obs.item_id
    CROSS JOIN LATERAL (
        SELECT CASE
            WHEN obs.source_disabled THEN 'blocked'
            WHEN NOT obs.preview_requested THEN 'not_requested'
            WHEN evidence.ready_at IS NOT NULL THEN 'ready'
            WHEN evidence.failed_at IS NOT NULL THEN 'failed'
            ELSE 'unrecorded'
        END AS preview
    ) AS status
    GROUP BY obs.source_id, obs.state, status.preview
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_workbench_source_reference_evidence(uuid, uuid[], uuid[], uuid[], text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.dataset_workbench_source_runs(p_actor_id uuid, p_source_ids uuid[])
RETURNS TABLE (
    source_id uuid, kind text, evidence text, run_id uuid, scan_run_id uuid, job_id uuid,
    status text, phase text, complete boolean, matches_current_config boolean,
    created_at timestamptz, completed_at timestamptz
)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH visible AS MATERIALIZED (
        SELECT source.id, source.config_revision, source.policy_revision,
               location.config_revision AS location_revision
        FROM public.dataset_sources AS source
        JOIN public.storage_locations AS location ON location.id = source.location_id
        WHERE source.id = ANY(p_source_ids) AND yoao_assets_v1.dataset_source_visible(p_actor_id, source.id)
    ), attempts AS MATERIALIZED (
        SELECT scope.id AS source_id, 'scan' AS kind, run.id, run.id AS scan_run_id,
               run.async_job_id, run.status, run.phase, run.complete,
               run.snapshot ->> 'source_revision' = scope.config_revision::text
                   AND run.snapshot ->> 'location_revision' = scope.location_revision::text AS matches,
               run.created_at, run.completed_at
        FROM visible AS scope
        JOIN public.dataset_scan_runs AS run ON run.source_id = scope.id
        UNION ALL
        SELECT scope.id, 'preflight', run.id, run.scan_run_id,
               run.async_job_id, run.status, run.phase, run.comparison_complete,
               run.snapshot ->> 'source_revision' = scope.config_revision::text
                   AND run.snapshot ->> 'location_revision' = scope.location_revision::text
                   AND run.snapshot ->> 'policy_revision' = scope.policy_revision::text,
               run.created_at, run.completed_at
        FROM visible AS scope
        JOIN public.dataset_registration_runs AS run ON run.source_id = scope.id
    ), selected AS (
        SELECT 'recent' AS evidence, attempt.*,
               pg_catalog.row_number() OVER (PARTITION BY attempt.source_id, attempt.kind
                   ORDER BY CASE WHEN attempt.status IN ('pending', 'running') THEN 0 ELSE 1 END,
                            attempt.created_at DESC, attempt.id DESC) AS rank
        FROM attempts AS attempt
        UNION ALL
        SELECT 'last_complete', attempt.*,
               pg_catalog.row_number() OVER (PARTITION BY attempt.source_id, attempt.kind
                   ORDER BY attempt.completed_at DESC NULLS LAST, attempt.created_at DESC, attempt.id DESC)
        FROM attempts AS attempt
        WHERE attempt.status = 'completed' AND attempt.complete
    )
    SELECT picked.source_id, picked.kind, picked.evidence, picked.id, picked.scan_run_id, picked.async_job_id,
           picked.status::text, CASE WHEN picked.phase IN ('enumerating', 'validating', 'resume_validation', 'preflight', 'registering')
                                    THEN picked.phase::text ELSE 'unknown' END,
           picked.complete, COALESCE(picked.matches, false), picked.created_at, picked.completed_at
    FROM selected AS picked WHERE picked.rank = 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.dataset_workbench_source_runs(uuid, uuid[]) FROM PUBLIC;
