CREATE FUNCTION yoao_assets_v1.publish_directory_reference(
    p_actor_id uuid, p_source_id uuid, p_run_id uuid, p_digest text,
    p_sha text, p_size bigint
)
RETURNS TABLE (item_id uuid, created boolean, error_code text)
LANGUAGE plpgsql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_path text;
    v_receipt jsonb;
    v_source_revision integer;
    v_location_revision integer;
    v_item_id uuid;
    v_file_path text;
BEGIN
    IF NOT yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id) THEN
        RETURN QUERY SELECT NULL::uuid, false, 'access_revoked'::text;
        RETURN;
    END IF;
    PERFORM 1 FROM public.dataset_sources AS s WHERE s.id = p_source_id FOR UPDATE;
    SELECT d.id, e.relative_path, e.verified_content, s.config_revision, l.config_revision
    INTO v_dataset_id, v_path, v_receipt, v_source_revision, v_location_revision
    FROM public.dataset_sources AS s
    JOIN public.storage_locations AS l ON l.id = s.location_id
    JOIN public.datasets AS d ON d.id = s.dataset_id
    JOIN public.dataset_scan_runs AS r ON r.source_id = s.id AND r.id = p_run_id
    JOIN public.dataset_scan_entries AS e ON e.run_id = r.id AND e.path_digest = p_digest
    WHERE s.id = p_source_id AND s.enabled AND s.unregistered_at IS NULL
      AND s.ownership_mode = 'reference' AND l.enabled
      AND l.kind IN ('filesystem', 's3', 'sftp', 'agent')
      AND d.archived_at IS NULL AND d.data_type = 'image' AND NOT d.is_temporal
      AND NOT EXISTS (SELECT 1 FROM public.scenes AS scene WHERE scene.dataset_id = d.id)
      AND r.complete AND r.status = 'completed' AND e.kind = 'file' AND e.state = 'observed'
      AND (r.snapshot ->> 'source_revision')::integer = s.config_revision
      AND (r.snapshot ->> 'location_revision')::integer = l.config_revision
      AND coalesce((r.snapshot ->> 'dataset_lifecycle_revision')::integer, 1) = d.lifecycle_revision
      AND e.verified_content ->> 'sha256' = p_sha
      AND e.verified_content -> 'size_bytes' = pg_catalog.to_jsonb(p_size);
    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::uuid, false, 'reference_content_changed'::text;
        RETURN;
    END IF;

    SELECT i.id, i.file_path INTO v_item_id, v_file_path
    FROM public.dataset_references AS ref
    JOIN public.dataset_items AS i ON i.id = ref.dataset_item_id
    WHERE ref.source_id = p_source_id AND ref.run_id = p_run_id AND ref.path_digest = p_digest;
    IF FOUND THEN
        IF v_file_path IS DISTINCT FROM ('__aap_reference__/' || v_item_id::text || '/' || p_sha) THEN
            RETURN QUERY SELECT NULL::uuid, false, 'reference_content_changed'::text;
        ELSE
            RETURN QUERY SELECT v_item_id, false, NULL::text;
        END IF;
        RETURN;
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.project_datasets AS link
        LEFT JOIN yoao_workspace_v1.project_facts AS p ON p.id = link.project_id
        WHERE link.dataset_id = v_dataset_id
          AND (p.id IS NULL OR p.data_type <> 'image' OR p.scene_mode)
    ) THEN
        RETURN QUERY SELECT NULL::uuid, false, 'reference_requires_image_project'::text;
        RETURN;
    END IF;

    v_item_id := pg_catalog.gen_random_uuid();
    INSERT INTO public.dataset_items (id, dataset_id, file_name, file_path, file_type,
        file_size, width, height, metadata)
    VALUES (v_item_id, v_dataset_id, pg_catalog.left(pg_catalog.split_part(v_path, '/', -1), 500),
        '__aap_reference__/' || v_item_id::text || '/' || p_sha, 'image', p_size,
        (v_receipt ->> 'width')::integer, (v_receipt ->> 'height')::integer,
        pg_catalog.jsonb_build_object('storage_mode', 'reference', 'storage_relative_path', v_path));
    INSERT INTO public.dataset_assets (id, dataset_item_id, media_type, size_bytes,
        checksum_algorithm, checksum_value, verification_state)
    VALUES (pg_catalog.gen_random_uuid(), v_item_id, 'image', p_size, 'sha256', p_sha, 'verified');
    INSERT INTO public.dataset_references (dataset_item_id, source_id, run_id, path_digest,
        source_revision, location_revision)
    VALUES (v_item_id, p_source_id, p_run_id, p_digest, v_source_revision, v_location_revision);
    UPDATE public.datasets AS d SET file_count = d.file_count + 1, updated_at = pg_catalog.now()
    WHERE d.id = v_dataset_id;
    RETURN QUERY SELECT v_item_id, true, NULL::text;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.publish_directory_reference(uuid, uuid, uuid, text, text, bigint) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.publish_directory_reference_head(
    p_actor_id uuid, p_source_id uuid, p_revision_id uuid, p_expected_head_id uuid, p_mode text
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_revision public.dataset_source_revisions;
    v_before public.dataset_source_revisions;
    v_current uuid;
BEGIN
    IF p_mode IS NULL OR p_mode NOT IN ('initial', 'content', 'revalidate', 'relocate') THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM public.dataset_sources AS s
    WHERE s.id = p_source_id AND s.enabled AND s.unregistered_at IS NULL FOR UPDATE;
    IF NOT FOUND OR yoao_assets_v1.dataset_source_visible(p_actor_id, p_source_id) IS NOT TRUE THEN
        RETURN false;
    END IF;
    SELECT r.* INTO v_revision FROM public.dataset_source_revisions AS r
    JOIN public.dataset_sources AS s ON s.id = r.source_id
    JOIN public.datasets AS d ON d.id = s.dataset_id AND d.archived_at IS NULL
    JOIN public.dataset_items AS item ON item.id = r.dataset_item_id AND item.dataset_id = d.id
    JOIN public.dataset_assets AS asset ON asset.id = r.asset_id AND asset.dataset_item_id = item.id
    WHERE r.source_id = p_source_id AND r.id = p_revision_id AND r.accepted_by_id = p_actor_id
      AND r.ownership_mode = s.ownership_mode
      AND asset.verification_state = 'verified' AND asset.checksum_algorithm = 'sha256'
      AND asset.checksum_value = r.content_sha256
      AND NOT EXISTS (
          SELECT 1 FROM public.dataset_source_revisions AS later
          WHERE later.source_id = r.source_id AND later.path_digest = r.path_digest
            AND later.acceptance_order > r.acceptance_order
      )
      AND NOT EXISTS (
          SELECT 1 FROM public.dataset_source_revisions AS move
          WHERE move.source_id = r.source_id AND move.previous_revision_id = r.id
            AND move.path_digest <> r.path_digest
      );
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT h.current_revision_id INTO v_current FROM public.dataset_source_items AS h
    WHERE h.source_id = p_source_id AND h.path_digest = v_revision.path_digest FOR UPDATE;
    IF v_current IS DISTINCT FROM p_expected_head_id THEN RETURN false; END IF;

    IF p_mode = 'initial' THEN
        IF p_expected_head_id IS NOT NULL OR v_revision.registration_id IS NOT NULL THEN RETURN false; END IF;
    ELSE
        SELECT r.* INTO v_before FROM public.dataset_source_revisions AS r
        WHERE r.source_id = p_source_id AND r.id = v_revision.previous_revision_id;
        IF NOT FOUND OR v_revision.registration_id IS NULL THEN RETURN false; END IF;
        IF p_mode = 'content' AND (
            v_revision.dataset_item_id = v_before.dataset_item_id
            OR v_revision.content_sha256 = v_before.content_sha256
        ) THEN RETURN false; END IF;
        IF p_mode IN ('revalidate', 'relocate') AND (
            v_revision.dataset_item_id, v_revision.asset_id, v_revision.content_sha256,
            v_revision.ownership_mode, v_revision.owns_item
        ) IS DISTINCT FROM (
            v_before.dataset_item_id, v_before.asset_id, v_before.content_sha256,
            v_before.ownership_mode, v_before.owns_item
        ) THEN RETURN false; END IF;
        IF p_mode = 'relocate' THEN
            IF p_expected_head_id IS NOT NULL OR v_before.path_digest = v_revision.path_digest THEN
                RETURN false;
            END IF;
            DELETE FROM public.dataset_source_items AS h WHERE h.source_id = p_source_id
              AND h.path_digest = v_before.path_digest AND h.current_revision_id = v_before.id;
            IF NOT FOUND THEN RETURN false; END IF;
        ELSIF p_expected_head_id IS DISTINCT FROM v_before.id OR v_before.path_digest <> v_revision.path_digest THEN
            RETURN false;
        END IF;
    END IF;

    INSERT INTO public.dataset_source_items (source_id, path_digest, current_revision_id, run_id,
        dataset_item_id, asset_id, content_sha256, ownership_mode, owns_item)
    VALUES (p_source_id, v_revision.path_digest, v_revision.id, v_revision.run_id,
        v_revision.dataset_item_id, v_revision.asset_id, v_revision.content_sha256,
        v_revision.ownership_mode, v_revision.owns_item)
    ON CONFLICT (source_id, path_digest) DO UPDATE SET
        current_revision_id = EXCLUDED.current_revision_id, run_id = EXCLUDED.run_id,
        dataset_item_id = EXCLUDED.dataset_item_id, asset_id = EXCLUDED.asset_id,
        content_sha256 = EXCLUDED.content_sha256, ownership_mode = EXCLUDED.ownership_mode,
        owns_item = EXCLUDED.owns_item;
    IF p_mode = 'content' THEN
        UPDATE public.dataset_items AS item
        SET metadata = coalesce(item.metadata, '{}'::jsonb) || pg_catalog.jsonb_build_object(
            'source_revision_id', v_revision.id, 'source_revision_number', v_revision.revision_number,
            'previous_source_item_id', v_before.item_id_at_accept
        ), updated_at = pg_catalog.now()
        WHERE item.id = v_revision.dataset_item_id;
    END IF;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.publish_directory_reference_head(uuid, uuid, uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_reference_content(p_item_id uuid)
RETURNS TABLE (
    dataset_id uuid, file_path text, width integer, height integer, asset_id uuid,
    sha256 text, size_bytes bigint, checksum_algorithm text, verification_state text,
    source_id uuid, source_enabled boolean, source_unregistered_at timestamptz,
    source_dataset_id uuid, location_id uuid, source_config jsonb, root_identity jsonb,
    source_revision integer, source_policy text, policy_revision integer,
    bound_source_revision integer, bound_location_revision integer, bound_run_matches boolean,
    bound_run_source_revision jsonb, bound_run_location_revision jsonb, bound_config jsonb,
    bound_anchor jsonb, bound_sha256 text, bound_size jsonb,
    accepted_revision_id uuid, accepted_sha256 text, run_id uuid, run_matches boolean, run_config jsonb,
    origin_anchor jsonb, scan_host_id text, path_digest text, relative_path text, content_receipt jsonb
)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT item.dataset_id, item.file_path::text, item.width, item.height, asset.id,
           asset.checksum_value::text, asset.size_bytes, asset.checksum_algorithm::text,
           asset.verification_state::text, ref.source_id, source.enabled, source.unregistered_at,
           source.dataset_id, source.location_id, source.config, source.root_identity,
           source.config_revision, source.cache_policy::text, source.policy_revision,
           ref.source_revision, ref.location_revision,
           coalesce(original.source_id = ref.source_id AND original.complete, false),
           original.snapshot -> 'source_revision', original.snapshot -> 'location_revision',
           original.snapshot -> 'config', original.snapshot -> 'anchor',
           bound.verified_content ->> 'sha256', bound.verified_content -> 'size_bytes',
           accepted.id, accepted.content_sha256::text, run.id,
           coalesce(run.source_id = ref.source_id AND run.complete, false),
           run.snapshot -> 'config', run.snapshot -> 'anchor', run.snapshot ->> 'scan_host_id',
           entry.path_digest::text, entry.relative_path, entry.verified_content
    FROM public.dataset_references AS ref
    JOIN public.dataset_items AS item ON item.id = ref.dataset_item_id
    JOIN public.dataset_assets AS asset ON asset.dataset_item_id = item.id
    LEFT JOIN public.dataset_sources AS source ON source.id = ref.source_id
    LEFT JOIN public.dataset_scan_runs AS original ON original.id = ref.run_id
    LEFT JOIN public.dataset_scan_entries AS bound ON bound.run_id = ref.run_id AND bound.path_digest = ref.path_digest
    LEFT JOIN LATERAL (
        SELECT r.id, r.run_id, r.path_digest, r.content_sha256
        FROM public.dataset_source_revisions AS r
        WHERE r.source_id = ref.source_id AND r.dataset_item_id = item.id
          AND r.asset_id = asset.id AND r.ownership_mode = 'reference'
        ORDER BY r.acceptance_order DESC LIMIT 1
    ) AS accepted ON true
    LEFT JOIN public.dataset_scan_runs AS run ON run.id = coalesce(accepted.run_id, ref.run_id)
    LEFT JOIN public.dataset_scan_entries AS entry ON entry.run_id = run.id
          AND entry.path_digest = coalesce(accepted.path_digest, ref.path_digest)
    WHERE ref.dataset_item_id = p_item_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_reference_content(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_reference_binding(
    p_item_id uuid, p_file_path text, p_lock boolean
)
RETURNS TABLE (
    error_code text, dataset_id uuid, asset_id uuid, sha256 text, size_bytes bigint,
    width integer, height integer, run_id uuid, accepted_revision_id uuid,
    source_id uuid, source_revision integer, location_id uuid, location_revision integer,
    connection_id uuid, connection_revision integer
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item record;
    v_ref record;
    v_asset record;
    v_source record;
    v_location record;
BEGIN
    SELECT i.dataset_id, i.file_path, i.width, i.height INTO v_item
    FROM public.dataset_items AS i WHERE i.id = p_item_id;
    IF NOT FOUND OR v_item.file_path IS DISTINCT FROM p_file_path THEN
        error_code := 'content_unverified'; RETURN NEXT; RETURN;
    END IF;
    IF p_lock THEN
        PERFORM 1 FROM public.dataset_references AS r WHERE r.dataset_item_id = p_item_id FOR SHARE NOWAIT;
        PERFORM 1 FROM public.dataset_assets AS a WHERE a.dataset_item_id = p_item_id FOR SHARE NOWAIT;
    END IF;
    SELECT r.source_id, r.run_id INTO v_ref FROM public.dataset_references AS r WHERE r.dataset_item_id = p_item_id;
    SELECT a.id, a.checksum_value, a.size_bytes INTO v_asset FROM public.dataset_assets AS a
    WHERE a.dataset_item_id = p_item_id AND a.verification_state = 'verified' AND a.checksum_algorithm = 'sha256';
    IF NOT FOUND OR v_ref.source_id IS NULL THEN
        error_code := 'content_unverified'; RETURN NEXT; RETURN;
    END IF;
    IF p_lock THEN
        PERFORM 1 FROM public.dataset_sources AS s WHERE s.id = v_ref.source_id FOR SHARE NOWAIT;
    END IF;
    SELECT s.id, s.location_id, s.config_revision INTO v_source FROM public.dataset_sources AS s
    WHERE s.id = v_ref.source_id AND s.enabled AND s.unregistered_at IS NULL;
    IF NOT FOUND THEN error_code := 'source_unavailable'; RETURN NEXT; RETURN; END IF;
    IF p_lock THEN
        PERFORM 1 FROM public.storage_locations AS l WHERE l.id = v_source.location_id FOR SHARE NOWAIT;
    END IF;
    SELECT l.id, l.config_revision, l.connection_id INTO v_location FROM public.storage_locations AS l
    WHERE l.id = v_source.location_id AND l.enabled;
    IF NOT FOUND THEN error_code := 'location_unavailable'; RETURN NEXT; RETURN; END IF;
    IF v_location.connection_id IS NOT NULL THEN
        IF p_lock THEN
            PERFORM 1 FROM public.storage_connections AS c WHERE c.id = v_location.connection_id FOR SHARE NOWAIT;
        END IF;
        SELECT c.id, c.revision INTO connection_id, connection_revision
        FROM public.storage_connections AS c WHERE c.id = v_location.connection_id;
    END IF;
    SELECT r.id INTO accepted_revision_id FROM public.dataset_source_revisions AS r
    WHERE r.source_id = v_ref.source_id AND r.dataset_item_id = p_item_id
      AND r.asset_id = v_asset.id AND r.ownership_mode = 'reference'
    ORDER BY r.acceptance_order DESC LIMIT 1;
    dataset_id := v_item.dataset_id;
    asset_id := v_asset.id;
    sha256 := v_asset.checksum_value;
    size_bytes := v_asset.size_bytes;
    width := v_item.width;
    height := v_item.height;
    run_id := v_ref.run_id;
    source_id := v_source.id;
    source_revision := v_source.config_revision;
    location_id := v_location.id;
    location_revision := v_location.config_revision;
    RETURN NEXT;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_reference_binding(uuid, text, boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_directory_reference_bindings(
    p_connections uuid[], p_locations uuid[], p_sources uuid[], p_items uuid[]
)
RETURNS void
LANGUAGE plpgsql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM c.id FROM public.storage_connections AS c WHERE c.id = ANY(p_connections)
    ORDER BY c.id FOR SHARE NOWAIT;
    PERFORM l.id FROM public.storage_locations AS l WHERE l.id = ANY(p_locations)
    ORDER BY l.id FOR SHARE NOWAIT;
    PERFORM s.id FROM public.dataset_sources AS s WHERE s.id = ANY(p_sources)
    ORDER BY s.id FOR SHARE NOWAIT;
    PERFORM i.id FROM public.dataset_items AS i WHERE i.id = ANY(p_items)
    ORDER BY i.id FOR SHARE NOWAIT;
    PERFORM a.id FROM public.dataset_assets AS a WHERE a.dataset_item_id = ANY(p_items)
    ORDER BY a.id FOR SHARE NOWAIT;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_directory_reference_bindings(uuid[], uuid[], uuid[], uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_reference_lineage(
    p_item_id uuid, p_source_id uuid, p_run_id uuid, p_digest text, p_revision_id uuid
)
RETURNS TABLE (
    completed_at timestamptz, accepted_revision_id uuid, revision_number integer,
    previous_revision_id uuid, accepted_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT run.completed_at, accepted.id, accepted.revision_number,
           accepted.previous_revision_id, accepted.accepted_at
    FROM public.dataset_scan_runs AS run
    LEFT JOIN public.dataset_source_revisions AS accepted ON accepted.id = p_revision_id
          AND accepted.item_id_at_accept = p_item_id AND accepted.source_id = p_source_id
          AND accepted.run_id = run.id AND accepted.path_digest = p_digest
          AND accepted.ownership_mode = 'reference'
    WHERE run.id = p_run_id AND run.source_id = p_source_id
      AND (accepted.id IS NOT NULL OR EXISTS (
          SELECT 1 FROM public.dataset_references AS ref
          WHERE ref.dataset_item_id = p_item_id AND ref.source_id = p_source_id
            AND ref.run_id = p_run_id AND ref.path_digest = p_digest
      ))
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_reference_lineage(uuid, uuid, uuid, text, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_directory_reference_location(p_location_id uuid, p_item_id uuid)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_id uuid := p_location_id;
BEGIN
    IF p_location_id IS NULL THEN
        SELECT s.location_id INTO v_id FROM public.dataset_references AS r
        JOIN public.dataset_sources AS s ON s.id = r.source_id WHERE r.dataset_item_id = p_item_id;
    END IF;
    IF v_id IS NOT NULL THEN
        PERFORM 1 FROM public.storage_locations AS l WHERE l.id = v_id FOR SHARE NOWAIT;
    END IF;
    RETURN v_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_directory_reference_location(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_reference_reclaim_facts(
    p_item_id uuid, p_run_id uuid, p_digest text
)
RETURNS TABLE (
    source_id uuid, location_id uuid, kind text, verified_content jsonb,
    observed jsonb, relative_path text, snapshot jsonb
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH observation AS (
        SELECT ref.run_id, ref.path_digest::text
        FROM public.dataset_references AS ref
        WHERE p_run_id IS NULL AND p_digest IS NULL AND ref.dataset_item_id = p_item_id
        UNION ALL
        SELECT p_run_id, p_digest WHERE p_run_id IS NOT NULL AND p_digest IS NOT NULL
    )
    SELECT s.id, s.location_id, l.kind::text, e.verified_content, e.observed, e.relative_path,
           pg_catalog.jsonb_build_object('anchor', r.snapshot -> 'anchor', 'config',
                pg_catalog.jsonb_build_object('relative_path', r.snapshot #> '{config,relative_path}'))
    FROM observation AS pinned
    JOIN public.dataset_scan_runs AS r ON r.id = pinned.run_id
    JOIN public.dataset_scan_entries AS e ON e.run_id = r.id AND e.path_digest = pinned.path_digest
    JOIN public.dataset_sources AS s ON s.id = r.source_id
    JOIN public.storage_locations AS l ON l.id = s.location_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_reference_reclaim_facts(uuid, uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_reference_replica_available(p_primary_asset_id uuid, p_actor_id uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.dataset_reference_replicas AS edge
        JOIN public.dataset_assets AS primary_asset ON primary_asset.id = edge.primary_asset_id
        JOIN public.dataset_items AS primary_item ON primary_item.id = primary_asset.dataset_item_id
        JOIN public.dataset_assets AS target ON target.id = edge.replica_asset_id
        JOIN public.dataset_items AS target_item ON target_item.id = target.dataset_item_id
        JOIN public.dataset_references AS ref ON ref.dataset_item_id = target_item.id
        JOIN public.dataset_sources AS source ON source.id = ref.source_id
        JOIN public.storage_locations AS location ON location.id = source.location_id
        WHERE edge.primary_asset_id = p_primary_asset_id AND edge.revoked_at IS NULL
          AND primary_item.dataset_id = target_item.dataset_id
          AND primary_asset.verification_state = 'verified' AND target.verification_state = 'verified'
          AND primary_asset.checksum_algorithm = 'sha256' AND target.checksum_algorithm = 'sha256'
          AND primary_asset.checksum_value = target.checksum_value AND primary_asset.size_bytes = target.size_bytes
          AND source.enabled AND source.unregistered_at IS NULL AND location.enabled
          AND (p_actor_id IS NULL OR yoao_assets_v1.dataset_source_visible(p_actor_id, source.id))
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_reference_replica_available(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_reference_replica_page(
    p_actor_id uuid, p_item_id uuid, p_limit integer, p_offset integer,
    p_candidates boolean, p_replica_id uuid
)
RETURNS TABLE (
    total bigint, id uuid, item_id uuid, asset_id uuid, source_id uuid, source_name text,
    location_id uuid, location_name text, location_kind text, site_id uuid, site_name text,
    agent_id uuid, agent_state text, priority integer, created_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH primary_asset AS MATERIALIZED (
        SELECT a.id, i.dataset_id, a.checksum_value, a.size_bytes
        FROM public.dataset_items AS i
        JOIN public.dataset_assets AS a ON a.dataset_item_id = i.id
        JOIN public.dataset_references AS r ON r.dataset_item_id = i.id
        WHERE i.id = p_item_id AND yoao_assets_v1.dataset_source_visible(p_actor_id, r.source_id)
    ), eligible AS MATERIALIZED (
        SELECT NULL::uuid AS id, i.id AS item_id, a.id AS asset_id, s.id AS source_id,
               s.name::text AS source_name, l.id AS location_id, l.name::text AS location_name,
               l.kind::text AS location_kind, NULL::integer AS priority, NULL::timestamptz AS created_at
        FROM primary_asset AS original
        JOIN public.dataset_items AS i ON i.dataset_id = original.dataset_id AND i.id <> p_item_id
        JOIN public.dataset_assets AS a ON a.dataset_item_id = i.id
        JOIN public.dataset_references AS r ON r.dataset_item_id = i.id
        JOIN public.dataset_sources AS s ON s.id = r.source_id
        JOIN public.storage_locations AS l ON l.id = s.location_id
        WHERE p_candidates AND a.media_type = 'image' AND a.verification_state = 'verified'
          AND a.checksum_algorithm = 'sha256' AND a.checksum_value = original.checksum_value
          AND a.size_bytes = original.size_bytes AND s.enabled AND s.unregistered_at IS NULL AND l.enabled
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, s.id)
          AND NOT EXISTS (
              SELECT 1 FROM public.dataset_reference_replicas AS edge
              WHERE edge.primary_asset_id = original.id AND edge.replica_asset_id = a.id AND edge.revoked_at IS NULL
          )
          AND EXISTS (
              SELECT 1 FROM public.dataset_source_revisions AS accepted
              WHERE accepted.source_id = s.id AND accepted.dataset_item_id = i.id AND accepted.asset_id = a.id
                AND accepted.ownership_mode = 'reference' AND accepted.content_sha256 = a.checksum_value
          )
        UNION ALL
        SELECT edge.id, a.dataset_item_id, a.id, s.id, s.name::text, l.id,
               l.name::text, l.kind::text, edge.priority, edge.created_at
        FROM primary_asset AS original
        JOIN public.dataset_reference_replicas AS edge ON edge.primary_asset_id = original.id AND edge.revoked_at IS NULL
        JOIN public.dataset_assets AS a ON a.id = edge.replica_asset_id
        JOIN public.dataset_references AS r ON r.dataset_item_id = a.dataset_item_id
        JOIN public.dataset_sources AS s ON s.id = r.source_id
        JOIN public.storage_locations AS l ON l.id = s.location_id
        WHERE NOT p_candidates AND (p_replica_id IS NULL OR edge.id = p_replica_id)
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, s.id)
    ), page AS (
        SELECT e.* FROM eligible AS e
        ORDER BY CASE WHEN p_candidates THEN e.source_name END,
                 CASE WHEN p_candidates THEN e.item_id END, e.priority, e.id
        LIMIT greatest(0, least(p_limit, 200)) OFFSET greatest(0, p_offset)
    )
    SELECT counts.total, page.id, page.item_id, page.asset_id, page.source_id,
           page.source_name, page.location_id, page.location_name, page.location_kind,
           agent.site_id, agent.agent_site_name::text, agent.agent_id, agent.agent_state::text,
           page.priority, page.created_at
    FROM (SELECT pg_catalog.count(*) AS total FROM eligible) AS counts
    LEFT JOIN page ON true
    LEFT JOIN yoao_assets_storage_v1.agent_location_status AS agent ON agent.location_id = page.location_id
    ORDER BY CASE WHEN p_candidates THEN page.source_name END,
             CASE WHEN p_candidates THEN page.item_id END, page.priority, page.id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_reference_replica_page(uuid, uuid, integer, integer, boolean, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_directory_reference_replica_primary(p_item_id uuid, p_asset_id uuid)
RETURNS void
LANGUAGE plpgsql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM a.id FROM public.dataset_assets AS a
    JOIN public.dataset_references AS r ON r.dataset_item_id = a.dataset_item_id
    WHERE a.id = p_asset_id AND a.dataset_item_id = p_item_id FOR UPDATE OF a;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_directory_reference_replica_primary(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.create_directory_reference_replica(
    p_actor_id uuid, p_primary_asset_id uuid, p_replica_asset_id uuid, p_priority integer
)
RETURNS TABLE (id uuid, error_code text)
LANGUAGE plpgsql VOLATILE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.dataset_assets AS a
        JOIN public.dataset_items AS i ON i.id = a.dataset_item_id
        JOIN public.datasets AS d ON d.id = i.dataset_id AND d.archived_at IS NULL
        JOIN public.dataset_references AS r ON r.dataset_item_id = i.id
        JOIN public.dataset_assets AS target ON target.id = p_replica_asset_id
        JOIN public.dataset_items AS other ON other.id = target.dataset_item_id AND other.dataset_id = i.dataset_id
        JOIN public.dataset_references AS target_ref ON target_ref.dataset_item_id = other.id
        WHERE a.id = p_primary_asset_id AND target.id <> a.id
          AND a.verification_state = 'verified' AND target.verification_state = 'verified'
          AND a.checksum_algorithm = 'sha256' AND target.checksum_algorithm = 'sha256'
          AND a.checksum_value = target.checksum_value AND a.size_bytes = target.size_bytes
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, r.source_id)
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, target_ref.source_id)
          AND EXISTS (
              SELECT 1 FROM public.dataset_source_revisions AS revision
              WHERE revision.dataset_item_id = i.id AND revision.asset_id = a.id
                AND revision.source_id = r.source_id AND revision.ownership_mode = 'reference'
          )
          AND EXISTS (
              SELECT 1 FROM public.dataset_source_revisions AS revision
              WHERE revision.dataset_item_id = other.id AND revision.asset_id = target.id
                AND revision.source_id = target_ref.source_id AND revision.ownership_mode = 'reference'
          )
    ) THEN
        RETURN QUERY SELECT NULL::uuid, 'reference_replica_content_mismatch'::text;
        RETURN;
    END IF;
    INSERT INTO public.dataset_reference_replicas AS edge
        (id, primary_asset_id, replica_asset_id, priority, created_by)
    VALUES (pg_catalog.gen_random_uuid(), p_primary_asset_id, p_replica_asset_id, p_priority, p_actor_id)
    ON CONFLICT (primary_asset_id, replica_asset_id) WHERE revoked_at IS NULL DO NOTHING
    RETURNING edge.id INTO v_id;
    RETURN QUERY SELECT v_id, CASE WHEN v_id IS NULL THEN 'reference_replica_exists'::text END;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.create_directory_reference_replica(uuid, uuid, uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.change_directory_reference_replica(
    p_actor_id uuid, p_primary_asset_id uuid, p_replica_id uuid, p_priority integer
)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE v_target uuid;
BEGIN
    SELECT edge.replica_asset_id INTO v_target FROM public.dataset_reference_replicas AS edge
    WHERE edge.id = p_replica_id AND edge.primary_asset_id = p_primary_asset_id
      AND edge.revoked_at IS NULL FOR UPDATE;
    IF NOT FOUND THEN RETURN 'reference_replica_not_found'; END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.dataset_assets AS a
        JOIN public.dataset_items AS i ON i.id = a.dataset_item_id
        JOIN public.datasets AS d ON d.id = i.dataset_id AND d.archived_at IS NULL
        JOIN public.dataset_references AS r ON r.dataset_item_id = i.id
        JOIN public.dataset_assets AS target ON target.id = v_target
        JOIN public.dataset_references AS target_ref ON target_ref.dataset_item_id = target.dataset_item_id
        WHERE a.id = p_primary_asset_id
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, r.source_id)
          AND yoao_assets_v1.dataset_source_visible(p_actor_id, target_ref.source_id)
    ) THEN RETURN 'reference_replica_source_not_found'; END IF;

    IF p_priority IS NULL THEN
        UPDATE public.dataset_reference_replicas AS edge
        SET revoked_at = pg_catalog.clock_timestamp(), revoked_by = p_actor_id
        WHERE edge.id = p_replica_id;
    ELSE
        UPDATE public.dataset_reference_replicas AS edge SET priority = p_priority
        WHERE edge.id = p_replica_id;
    END IF;
    RETURN NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.change_directory_reference_replica(uuid, uuid, uuid, integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_reference_route_order(p_primary_asset_id uuid, p_location_id uuid)
RETURNS TABLE (replica_id uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT route.id FROM (
        SELECT NULL::uuid AS id, 100 AS priority, p_location_id AS location_id
        UNION ALL
        SELECT edge.id, edge.priority, l.id FROM public.dataset_reference_replicas AS edge
        JOIN public.dataset_assets AS target ON target.id = edge.replica_asset_id
        JOIN public.dataset_references AS ref ON ref.dataset_item_id = target.dataset_item_id
        JOIN public.dataset_sources AS s ON s.id = ref.source_id
        JOIN public.storage_locations AS l ON l.id = s.location_id
        WHERE edge.primary_asset_id = p_primary_asset_id AND edge.revoked_at IS NULL
    ) AS route
    LEFT JOIN yoao_assets_storage_v1.agent_location_status AS agent ON agent.location_id = route.location_id
    ORDER BY route.priority, coalesce(agent.agent_state <> 'online', false), route.id NULLS FIRST
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_reference_route_order(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.directory_reference_replica_edge(p_primary_asset_id uuid, p_replica_id uuid)
RETURNS TABLE (
    id uuid, replica_asset_id uuid, priority integer, replica_item_id uuid, primary_dataset_id uuid
)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT edge.id, edge.replica_asset_id, edge.priority, target.dataset_item_id, primary_item.dataset_id
    FROM public.dataset_reference_replicas AS edge
    JOIN public.dataset_assets AS primary_asset ON primary_asset.id = edge.primary_asset_id
    JOIN public.dataset_items AS primary_item ON primary_item.id = primary_asset.dataset_item_id
    JOIN public.dataset_assets AS target ON target.id = edge.replica_asset_id
    WHERE edge.id = p_replica_id AND edge.primary_asset_id = p_primary_asset_id AND edge.revoked_at IS NULL
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_reference_replica_edge(uuid, uuid) FROM PUBLIC;
-- aap-statement-break
