CREATE FUNCTION yoao_assets_v1.directory_source_usage(p_actor_id uuid, p_location_ids uuid[])
RETURNS TABLE (
    location_id uuid, file_count bigint, known_size_bytes numeric,
    unknown_reference_count numeric, conflict_file_count bigint,
    incomparable_file_count bigint, duplicate_reference_count numeric,
    reference_count bigint, inactive_reference_count bigint,
    observed_from timestamptz, observed_through timestamptz, measured_at timestamptz
)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
WITH usage_locations AS (
    SELECT loc.id AS location_id, loc.kind, loc.enabled
    FROM public.storage_locations AS loc
    WHERE loc.id = ANY (p_location_ids) AND yoao_assets_v1.dataset_location_visible(p_actor_id, loc.id)
), usage_candidates AS (
    SELECT page.location_id, page.kind, head.dataset_item_id AS head_id,
           scan.snapshot, scan.completed_at AS observed_at, entry.relative_path,
           entry.verified_content AS receipt, asset.checksum_value AS checksum, asset.size_bytes,
           (NOT source.enabled OR source.unregistered_at IS NOT NULL OR NOT page.enabled) AS inactive,
           (revision.source_id = head.source_id AND revision.path_digest = head.path_digest
            AND revision.run_id = head.run_id AND revision.dataset_item_id = head.dataset_item_id
            AND revision.item_id_at_accept = head.dataset_item_id AND revision.asset_id = head.asset_id
            AND revision.asset_id_at_accept = head.asset_id AND revision.content_sha256 = head.content_sha256
            AND revision.ownership_mode = head.ownership_mode AND head.ownership_mode IN ('reference', 'managed_copy')
            AND scan.source_id = head.source_id AND scan.complete IS TRUE AND scan.status = 'completed'
            AND entry.kind = 'file' AND asset.dataset_item_id = head.dataset_item_id
            AND item.dataset_id = source.dataset_id AND asset.verification_state = 'verified'
            AND asset.checksum_algorithm = 'sha256' AND asset.checksum_value = head.content_sha256) AS relations_valid
    FROM usage_locations AS page
    JOIN public.dataset_sources AS source ON source.location_id = page.location_id
    JOIN public.dataset_source_items AS head ON head.source_id = source.id
    LEFT JOIN public.dataset_source_revisions AS revision ON revision.id = head.current_revision_id
    LEFT JOIN public.dataset_scan_runs AS scan ON scan.id = revision.run_id
    LEFT JOIN public.dataset_scan_entries AS entry ON entry.run_id = revision.run_id AND entry.path_digest = revision.path_digest
    LEFT JOIN public.dataset_items AS item ON item.id = head.dataset_item_id
    LEFT JOIN public.dataset_assets AS asset ON asset.id = head.asset_id
    WHERE yoao_assets_v1.dataset_source_visible(p_actor_id, source.id)
), facts AS (
    SELECT c.*, snapshot->'anchor' AS anchor,
           snapshot->'config'->>'relative_path' AS base,
           receipt->'observation' AS observation,
           concat_ws('/', nullif(snapshot->'config'->>'relative_path', ''), relative_path) AS address
    FROM usage_candidates c
), addresses AS (
    SELECT f.*,
           coalesce(anchor->>'prefix', '') ||
               CASE WHEN coalesce(anchor->>'prefix', '') <> '' AND right(anchor->>'prefix', 1) <> '/' THEN '/' ELSE '' END ||
               base || CASE WHEN base <> '' AND right(base, 1) <> '/' THEN '/' ELSE '' END || relative_path AS s3_key,
           CASE WHEN kind <> 's3' OR observation->>'version_id' IS NULL OR observation->>'version_id' = 'null'
                THEN NULL ELSE observation->>'version_id' END AS object_version,
           base IS NOT NULL AND relative_path <> '' AND length(address) <= 1000
               AND right(address, 1) <> '/'
               AND address !~ '(^/|//|(^|/)\.{1,2}(/|$))'
               AND position(chr(92) in address) = 0
               AND length(address) - length(replace(address, '/', '')) < 32 AS path_valid
    FROM facts f
), located AS (
    SELECT a.*,
           CASE kind
               WHEN 'filesystem' THEN jsonb_build_array(snapshot->'scan_host_id', anchor->'path_digest', anchor->'root', anchor->'base')
               WHEN 's3' THEN jsonb_build_array(anchor->'endpoint', anchor->'bucket')
               WHEN 'sftp' THEN jsonb_build_array(anchor->'root_handle', anchor->'qualification')
           END AS domain,
           CASE WHEN kind = 's3' THEN s3_key ELSE address END AS physical_address,
           coalesce(relations_valid AND size_bytes >= 0
               AND checksum ~ '^[0-9a-f]{64}$'
               AND jsonb_typeof(receipt->'size_bytes') = 'number'
               AND receipt->>'size_bytes' ~ '^(0|[1-9][0-9]*)$'
               AND receipt->'size_bytes' = to_jsonb(size_bytes)
               AND jsonb_typeof(receipt->'sha256') = 'string'
               AND receipt->>'sha256' = checksum
               AND observation->'size' = to_jsonb(size_bytes)
               AND jsonb_typeof(snapshot->'config'->'relative_path') = 'string'
               AND CASE kind
                   WHEN 'filesystem' THEN path_valid
                       AND jsonb_typeof(snapshot->'scan_host_id') = 'string'
                       AND snapshot->>'scan_host_id' ~ '^[0-9a-f]{64}$'
                       AND jsonb_typeof(anchor->'path_digest') = 'string'
                       AND anchor->>'path_digest' ~ '^[0-9a-f]{64}$'
                       AND (anchor->'root')::text ~ '^\[[0-9]+, ?[0-9]+\]$'
                       AND (anchor->'base')::text ~ '^\[[0-9]+, ?[0-9]+\]$'
                       AND (observation->'identity')::text ~ '^\[[0-9]+, ?[0-9]+\]$'
                   WHEN 's3' THEN anchor->>'kind' = 's3'
                       AND jsonb_typeof(anchor->'endpoint') = 'string' AND anchor->>'endpoint' <> ''
                       AND jsonb_typeof(anchor->'bucket') = 'string' AND anchor->>'bucket' <> ''
                       AND jsonb_typeof(anchor->'prefix') = 'string'
                       AND relative_path <> '' AND octet_length(s3_key) <= 1024
                       AND observation->>'kind' = 's3'
                       AND jsonb_typeof(observation->'bucket') = 'string'
                       AND jsonb_typeof(observation->'key') = 'string'
                       AND observation->>'bucket' = anchor->>'bucket'
                       AND observation->>'key' = s3_key
                       AND observation ? 'version_id'
                       AND (observation->'version_id' = 'null'::jsonb
                            OR (jsonb_typeof(observation->'version_id') = 'string' AND observation->>'version_id' <> ''))
                   WHEN 'sftp' THEN path_valid AND anchor->>'kind' = 'sftp'
                       AND jsonb_typeof(anchor->'root_handle') = 'string' AND anchor->>'root_handle' <> ''
                       AND jsonb_typeof(anchor->'qualification') = 'string'
                       AND anchor->>'qualification' ~ '^[0-9a-f]{64}$'
                       AND observation->>'protocol' = 'sftp'
                       AND observation->>'path' = '/' || address
                   ELSE false END, false) AS valid
    FROM addresses a
), grouped AS (
    SELECT location_id, coalesce(physical_address, head_id::text) AS address, object_version,
           count(*) AS refs, bool_and(valid) AS valid,
           count(DISTINCT domain) AS domains,
           count(DISTINCT jsonb_build_array(checksum, size_bytes)) AS contents,
           max(size_bytes) AS size_bytes
    FROM located
    GROUP BY location_id, coalesce(physical_address, head_id::text), object_version
), totals AS (
    SELECT grouped.*, valid AND domains = 1 AND contents = 1 AND NOT split_domain AS known,
           domains > 1 OR split_domain AS incomparable
    FROM grouped JOIN (
        SELECT location_id, count(DISTINCT domain) > 1 AS split_domain
        FROM located GROUP BY location_id
    ) domain_coverage USING (location_id)
)
SELECT l.location_id,
       count(*) FILTER (WHERE known) AS file_count,
       coalesce(sum(size_bytes) FILTER (WHERE known), 0) AS known_size_bytes,
       coalesce(sum(refs) FILTER (WHERE NOT known), 0) AS unknown_reference_count,
       count(*) FILTER (WHERE contents > 1) AS conflict_file_count,
       count(*) FILTER (WHERE incomparable) AS incomparable_file_count,
       coalesce(sum(refs - 1) FILTER (WHERE known), 0) AS duplicate_reference_count,
       coalesce(c.reference_count, 0) AS reference_count,
       coalesce(c.inactive_reference_count, 0) AS inactive_reference_count,
       c.observed_from, c.observed_through,
       statement_timestamp() AS measured_at
FROM usage_locations l
LEFT JOIN totals t USING (location_id)
LEFT JOIN (
    SELECT location_id, count(*) AS reference_count,
           count(*) FILTER (WHERE inactive) AS inactive_reference_count,
           min(observed_at) AS observed_from, max(observed_at) AS observed_through
    FROM usage_candidates GROUP BY location_id
) c USING (location_id)
GROUP BY l.location_id, c.reference_count, c.inactive_reference_count,
         c.observed_from, c.observed_through
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.directory_source_usage(uuid, uuid[]) FROM PUBLIC;
