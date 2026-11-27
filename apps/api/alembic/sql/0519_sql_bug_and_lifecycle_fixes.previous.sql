CREATE OR REPLACE FUNCTION yoao_assets_v1._native_pyramid_retirement_target_valid(p_ref jsonb, p_output uuid)
 RETURNS boolean
 LANGUAGE sql
 IMMUTABLE PARALLEL SAFE
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
    SELECT CASE
        WHEN p_output IS NULL THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref) IS DISTINCT FROM 'object' THEN false
        WHEN NOT yoao_assets_v1._media_object_has_only(p_ref, ARRAY[
            'schema', 'purpose', 'location_id', 'config_revision',
            'binding_generation', 'endpoint_url', 'bucket', 'key_prefix',
            'prefix', 'storage_identity'
        ]) THEN false
        WHEN NOT (p_ref ?& ARRAY[
            'schema', 'purpose', 'location_id', 'config_revision',
            'binding_generation', 'endpoint_url', 'bucket', 'key_prefix',
            'prefix', 'storage_identity'
        ]) THEN false
        WHEN p_ref -> 'schema' IS DISTINCT FROM '2'::jsonb THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'purpose') IS DISTINCT FROM 'string'
            OR p_ref ->> 'purpose' IS DISTINCT FROM 'media-cache' THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'location_id') IS DISTINCT FROM 'string'
            OR (p_ref ->> 'location_id') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'config_revision') IS DISTINCT FROM 'number'
            OR (p_ref ->> 'config_revision') !~ '^[1-9][0-9]{0,9}$'
            THEN false
        WHEN (p_ref ->> 'config_revision')::numeric > 2147483647 THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'binding_generation') IS DISTINCT FROM 'number'
            OR (p_ref ->> 'binding_generation') !~ '^[1-9][0-9]{0,18}$'
            THEN false
        WHEN (p_ref ->> 'binding_generation')::numeric > 9223372036854775807
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'endpoint_url') IS DISTINCT FROM 'string'
            OR pg_catalog.length(p_ref ->> 'endpoint_url') NOT BETWEEN 8 AND 2048
            OR (p_ref ->> 'endpoint_url') !~ '^https?://[^[:space:]/?#@]+$'
            OR pg_catalog.strpos((p_ref ->> 'endpoint_url'), chr(92)) > 0
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'bucket') IS DISTINCT FROM 'string'
            OR pg_catalog.length(p_ref ->> 'bucket') NOT BETWEEN 1 AND 255
            OR (p_ref ->> 'bucket') ~ '[[:space:][:cntrl:]/?#@%:]'
            OR pg_catalog.strpos((p_ref ->> 'bucket'), chr(92)) > 0
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'key_prefix') IS DISTINCT FROM 'string'
            OR pg_catalog.octet_length(p_ref ->> 'key_prefix') > 436
            OR pg_catalog.strpos((p_ref ->> 'key_prefix'), chr(92)) > 0
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'prefix') IS DISTINCT FROM 'string'
            OR pg_catalog.length(p_ref ->> 'prefix') NOT BETWEEN 1 AND 2048
            OR (p_ref ->> 'prefix') IS DISTINCT FROM
                COALESCE(p_ref ->> 'key_prefix', '')
                || 'image-pyramids/objects/' || p_output::text || '/'
            THEN false
        WHEN pg_catalog.jsonb_typeof(p_ref -> 'storage_identity') IS DISTINCT FROM 'string'
            OR (p_ref ->> 'storage_identity') !~ '^[0-9a-f]{64}$'
            THEN false
        WHEN (p_ref ->> 'storage_identity') IS DISTINCT FROM pg_catalog.encode(
            pg_catalog.sha256(pg_catalog.convert_to(
                pg_catalog.jsonb_build_array(
                    pg_catalog.rtrim(p_ref ->> 'endpoint_url', '/'),
                    p_ref ->> 'bucket'
                )::text, 'UTF8'
            )), 'hex'
        ) THEN false
        ELSE true
    END
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_assets_v1.observe_original_reclamation(p_mode text, p_location_id uuid, p_datasets_bucket text, p_annotations_bucket text, p_storage_identity text, p_location_config jsonb, p_retirement_id uuid, p_reclaim_attempt_id uuid, p_limit integer, p_offset integer)
 RETURNS TABLE(measured_at timestamp with time zone, configuration_fingerprint jsonb, counts jsonb, items jsonb, observation jsonb, unknown jsonb)
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
WITH clock AS (
    SELECT statement_timestamp() AS measured_at
),
patterns AS (
    SELECT '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' AS receipt_uuid,
        '^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]([.][0-9]{1,6})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$' AS receipt_timestamp
),
retirement_scope AS MATERIALIZED (
    SELECT r.* FROM public.managed_original_retirements r
    WHERE p_mode = 'preview' OR (p_mode = 'reclaim' AND r.id = p_retirement_id)
    UNION ALL
    SELECT x.* FROM (
        SELECT DISTINCT ON (r.bucket, r.object_key) r.* FROM public.managed_original_retirements r
        WHERE p_mode = 'reconcile' ORDER BY r.bucket, r.object_key, r.id
    ) x
),
locator_peers AS NOT MATERIALIZED (
    SELECT p.* FROM public.managed_original_retirements p
    WHERE p_mode <> 'reclaim'
    UNION ALL
    SELECT p.* FROM retirement_scope r JOIN public.managed_original_retirements p
       ON md5(p.bucket) = md5(r.bucket) AND md5(p.object_key) = md5(r.object_key)
          AND p.bucket = r.bucket AND p.object_key = r.object_key
    WHERE p_mode = 'reclaim'
),
active_declared_holds AS (
    SELECT h.retirement_id,
        count(*) FILTER (WHERE h.kind = 'backup') AS backup_holds,
        count(*) FILTER (WHERE h.kind = 'migration') AS migration_holds
    FROM public.managed_original_retention_holds h
    JOIN locator_peers r ON r.id = h.retirement_id
    WHERE h.released_at IS NULL
    GROUP BY h.retirement_id
), peer_protection AS MATERIALIZED (
    SELECT r.bucket, r.object_key,
        count(*) AS peer_assertions,
        bool_or(r.retain_until IS NULL) AS indefinite_retention,
        CASE WHEN bool_or(r.retain_until IS NULL) THEN NULL
            ELSE max(r.retain_until) END AS effective_retain_until,
        COALESCE(sum(h.backup_holds), 0) AS declared_backup_holds,
        COALESCE(sum(h.migration_holds), 0) AS declared_migration_holds
    FROM locator_peers r
    LEFT JOIN active_declared_holds h ON h.retirement_id = r.id
    GROUP BY r.bucket, r.object_key
),
training_history AS MATERIALIZED (SELECT * FROM yoao_learning_v1.training_original_history()),
learning_history AS MATERIALIZED (SELECT * FROM yoao_learning_v1.active_learning_original_history()),
learning_decisions AS MATERIALIZED (SELECT * FROM yoao_learning_v1.active_learning_original_decisions()),
training_documents AS MATERIALIZED (
    SELECT id, snapshot,
        COALESCE(jsonb_typeof(snapshot) = 'object'
        AND jsonb_typeof(snapshot->'samples') = 'array'
        AND (NOT snapshot ? 'managed_original_contract' OR snapshot->'managed_original_contract' = '1'::jsonb)
        AND (NOT snapshot ? 'source_lineage_contract'
            OR snapshot->'source_lineage_contract' = '{"schema": 1,"scope":"accepted-reference"}'::jsonb)
        AND (NOT snapshot ? 'asset_source_contract'
            OR snapshot->'asset_source_contract' = '{"schema": 1,"reader":"verified-asset-reference-1","freeze":"training-copy"}'::jsonb)
        AND (
            (snapshot->'schema' = '1'::jsonb
             AND (NOT snapshot ? 'task' OR snapshot->'task' = '"image-detection"'::jsonb)
             AND NOT snapshot ? 'geometry' AND NOT snapshot ? 'bridge'
             AND (NOT snapshot ? 'format' OR snapshot->'format' = '"ultralytics-detection"'::jsonb))
            OR (snapshot->'schema' = '2'::jsonb
                AND snapshot @> '{"task":"image-instance-segmentation", "geometry":"normalized-simple-polygon", "bridge":"ultralytics-simple-polygon-1"}'::jsonb
                AND (NOT snapshot ? 'format' OR snapshot->'format' = '"ultralytics-segmentation"'::jsonb))
            OR (snapshot->'schema' = '3'::jsonb
                AND snapshot @> '{"media_type":"image", "format":"ultralytics-yolo"}'::jsonb
                AND ((snapshot @> '{"task":"detect", "geometry":"normalized-bbox", "bridge":"ultralytics-bbox-1"}'::jsonb)
                    OR (snapshot @> '{"task":"segment", "geometry":"normalized-simple-polygon", "bridge":"ultralytics-simple-polygon-1"}'::jsonb)))
            OR (snapshot->'schema' = '4'::jsonb
                AND snapshot @> '{"task":"segment", "media_type":"image", "format":"aap-instance-rle", "geometry":"instance-mask", "bridge":"ultralytics-instance-rle-1"}'::jsonb)
        ), false) AS supported
    FROM training_history
), training_samples AS MATERIALIZED (
    SELECT d.id, d.snapshot, s.value AS sample,
        COALESCE(jsonb_typeof(s.value) = 'object' AND (
            (d.snapshot->'managed_original_contract' = '1'::jsonb AND COALESCE(jsonb_typeof(s.value->'original') = 'object'
        AND s.value->'original'->'schema' = '1'::jsonb
        AND s.value->'original'->>'owner_kind' IN ('dataset_asset','task_original')
        AND (s.value->'original') - ARRAY['schema','owner_kind','content_id','sha256','size_bytes','location_id'] = '{}'::jsonb
        AND jsonb_typeof(s.value->'original'->'content_id') = 'string'
        AND s.value->'original'->>'content_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(s.value->'original'->'location_id') = 'string'
        AND s.value->'original'->>'location_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(s.value->'original'->'sha256') = 'string'
        AND s.value->'original'->>'sha256' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(s.value->'original'->'size_bytes') = 'number'
        AND s.value->'original'->>'size_bytes' ~ '^[1-9][0-9]*$'
        AND (
            (s.value->'original'->>'owner_kind' = 'dataset_asset' AND (
                EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_asset WHERE CAST(receipt_asset.asset_id AS text) = s.value->'original'->>'content_id')
                OR EXISTS (SELECT 1 FROM public.managed_original_retirements receipt_peer WHERE CAST(receipt_peer.asset_id AS text) = s.value->'original'->>'content_id')))
            OR (s.value->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_task WHERE CAST(receipt_task.id AS text) = s.value->'original'->>'content_id'))
        ), false))
            OR (jsonb_typeof(s.value->'source_bucket') = 'string'
             AND length(s.value->>'source_bucket') > 0
             AND jsonb_typeof(s.value->'source_key') = 'string'
             AND length(s.value->>'source_key') > 0
             AND left(s.value->>'source_key', 18) <> '__aap_reference__/'
             AND NOT s.value ? 'asset_reference' AND NOT s.value ? 'source_lineage'
             AND NOT s.value ? 'original' AND NOT d.snapshot ? 'managed_original_contract')
            OR (
                NOT s.value ? 'original' AND jsonb_typeof(s.value->'asset_reference') = 'object'
                AND s.value->'asset_reference'->'schema' = '1'::jsonb
                AND (s.value->'asset_reference') - ARRAY[
                    'schema', 'dataset_id', 'dataset_item_id', 'asset_id', 'sha256', 'size_bytes'
                ] = '{}'::jsonb
                AND jsonb_typeof(s.value->'asset_reference'->'asset_id') = 'string'
                AND s.value->'asset_reference'->>'asset_id' ~ (SELECT receipt_uuid FROM patterns)
                AND jsonb_typeof(s.value->'asset_reference'->'dataset_id') = 'string'
                AND s.value->'asset_reference'->>'dataset_id' ~ (SELECT receipt_uuid FROM patterns)
                AND jsonb_typeof(s.value->'asset_reference'->'dataset_item_id') = 'string'
                AND s.value->'asset_reference'->>'dataset_item_id' ~ (SELECT receipt_uuid FROM patterns)
                AND jsonb_typeof(s.value->'asset_reference'->'sha256') = 'string'
                AND s.value->'asset_reference'->>'sha256' ~ '^[0-9a-f]{64}$'
                AND jsonb_typeof(s.value->'asset_reference'->'size_bytes') = 'number'
                AND s.value->'asset_reference'->>'size_bytes' ~ '^[0-9]+$'
                AND s.value->>'source_key' = '__aap_reference__/'
                    || (s.value->'asset_reference'->>'dataset_item_id') || '/'
                    || (s.value->'asset_reference'->>'sha256')
                AND (NOT s.value ? 'source_bucket')
                AND (
                    EXISTS (SELECT 1 FROM public.managed_original_retirements retained_asset
                        WHERE CAST(retained_asset.asset_id AS text) = s.value->'asset_reference'->>'asset_id')
                    OR EXISTS (SELECT 1 FROM public.dataset_asset_locations saved_asset
                        WHERE CAST(saved_asset.asset_id AS text) = s.value->'asset_reference'->>'asset_id')
                    OR EXISTS (SELECT 1 FROM public.dataset_assets saved_asset
                        JOIN public.dataset_references saved_reference ON saved_reference.dataset_item_id = saved_asset.dataset_item_id
                        JOIN public.dataset_scan_entries saved_entry ON saved_entry.run_id = saved_reference.run_id
                            AND saved_entry.path_digest = saved_reference.path_digest
                        JOIN public.dataset_scan_runs saved_run ON saved_run.id = saved_entry.run_id
                        JOIN public.dataset_sources saved_source ON saved_source.id = saved_run.source_id
                        JOIN public.storage_locations saved_location ON saved_location.id = saved_source.location_id
                        WHERE CAST(saved_asset.id AS text) = s.value->'asset_reference'->>'asset_id'
                            AND jsonb_typeof(saved_entry.observed) = 'object'
                            AND (saved_location.kind IN ('filesystem','sftp') OR (
                                saved_entry.observed->>'kind' = 's3'
                                AND jsonb_typeof(saved_entry.observed->'bucket') = 'string'
                                AND length(saved_entry.observed->>'bucket') > 0
                                AND jsonb_typeof(saved_entry.observed->'key') = 'string'
                                AND length(saved_entry.observed->>'key') > 0)))
                    OR EXISTS (SELECT 1 FROM public.dataset_scan_entries saved_entry
                        JOIN public.dataset_scan_runs saved_run ON saved_run.id = saved_entry.run_id
                        JOIN public.dataset_sources saved_source ON saved_source.id = saved_run.source_id
                        JOIN public.storage_locations saved_location ON saved_location.id = saved_source.location_id
                        WHERE CAST(saved_entry.run_id AS text) = s.value->'source_lineage'->>'scan_run_id'
                            AND saved_entry.path_digest = s.value->'source_lineage'->>'path_digest'
                            AND jsonb_typeof(saved_entry.observed) = 'object'
                            AND (saved_location.kind IN ('filesystem','sftp') OR (
                                saved_entry.observed->>'kind' = 's3'
                                AND jsonb_typeof(saved_entry.observed->'bucket') = 'string'
                                AND length(saved_entry.observed->>'bucket') > 0
                                AND jsonb_typeof(saved_entry.observed->'key') = 'string'
                                AND length(saved_entry.observed->>'key') > 0)))
                )
                AND ((NOT s.value ? 'source_lineage' AND NOT d.snapshot ? 'source_lineage_contract') OR (
                    d.snapshot->'source_lineage_contract' = '{"schema": 1,"scope":"accepted-reference"}'::jsonb
                    AND jsonb_typeof(s.value->'source_lineage') = 'object'
                    AND EXISTS (SELECT 1 FROM public.dataset_scan_entries lineage_entry
                        JOIN public.dataset_scan_runs lineage_run ON lineage_run.id = lineage_entry.run_id
                        JOIN public.dataset_sources lineage_source ON lineage_source.id = lineage_run.source_id
                        JOIN public.storage_locations lineage_location ON lineage_location.id = lineage_source.location_id
                        WHERE CAST(lineage_entry.run_id AS text) = s.value->'source_lineage'->>'scan_run_id'
                            AND lineage_entry.path_digest = s.value->'source_lineage'->>'path_digest'
                            AND CAST(lineage_source.id AS text) = s.value->'source_lineage'->>'source_id'
                            AND CAST(lineage_location.id AS text) = s.value->'source_lineage'->>'location_id'
                            AND lineage_entry.verified_content->>'sha256' = s.value->'asset_reference'->>'sha256'
                            AND lineage_entry.verified_content->>'size_bytes' = s.value->'asset_reference'->>'size_bytes'
                            AND (lineage_location.kind IN ('filesystem','sftp') OR (
                                lineage_location.kind = 's3'
                                AND lineage_run.snapshot->'anchor'->>'kind' = 's3'
                                AND lineage_entry.observed->>'kind' = 's3'
                                AND lineage_entry.observed->>'bucket' = lineage_run.snapshot->'anchor'->>'bucket'
                                AND jsonb_typeof(lineage_entry.observed->'key') = 'string'
                                AND lineage_entry.observed->>'key' =
                                    (lineage_run.snapshot->'anchor'->>'prefix')
                                    || CASE WHEN lineage_run.snapshot->'config'->>'relative_path' = '' OR right(lineage_run.snapshot->'config'->>'relative_path', 1) = '/' THEN lineage_run.snapshot->'config'->>'relative_path'
                                        ELSE (lineage_run.snapshot->'config'->>'relative_path') || '/' END
                                    || lineage_entry.relative_path)))
                    AND jsonb_typeof(s.value->'source_lineage'->'location_id') = 'string'
                    AND s.value->'source_lineage'->>'location_id' ~ (SELECT receipt_uuid FROM patterns)
                    AND jsonb_typeof(s.value->'source_lineage'->'source_id') = 'string'
                    AND s.value->'source_lineage'->>'source_id' ~ (SELECT receipt_uuid FROM patterns)
                    AND s.value->'source_lineage'->>'scan_run_id' ~ (SELECT receipt_uuid FROM patterns)
                    AND jsonb_typeof(s.value->'source_lineage'->'scan_run_id') = 'string'
                    AND s.value->'source_lineage'->>'path_digest' ~ '^[0-9a-f]{64}$'
                    AND jsonb_typeof(s.value->'source_lineage'->'path_digest') = 'string'
                    AND s.value->'source_lineage'->>'source_config_revision' ~ '^[1-9][0-9]*$'
                    AND jsonb_typeof(s.value->'source_lineage'->'source_config_revision') = 'number'
                    AND s.value->'source_lineage'->>'location_config_revision' ~ '^[1-9][0-9]*$'
                    AND jsonb_typeof(s.value->'source_lineage'->'location_config_revision') = 'number'
                    AND (s.value->'source_lineage') - ARRAY[
                        'source_id', 'location_id', 'scan_run_id', 'path_digest',
                        'source_config_revision', 'location_config_revision',
                        'accepted_revision_id', 'revision_number', 'previous_revision_id',
                        'accepted_at', 'scan_completed_at', 'content_verified_at'
                    ] = '{}'::jsonb
                    AND (s.value->'source_lineage'->'accepted_revision_id' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'accepted_revision_id') = 'string'
                            AND s.value->'source_lineage'->>'accepted_revision_id' ~ (SELECT receipt_uuid FROM patterns)))
                    AND (s.value->'source_lineage'->'previous_revision_id' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'previous_revision_id') = 'string'
                            AND s.value->'source_lineage'->>'previous_revision_id' ~ (SELECT receipt_uuid FROM patterns)))
                    AND (s.value->'source_lineage'->'revision_number' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'revision_number') = 'number'
                            AND s.value->'source_lineage'->>'revision_number' ~ '^[1-9][0-9]*$'))
                    AND (s.value->'source_lineage'->'accepted_at' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'accepted_at') = 'string'
                            AND s.value->'source_lineage'->>'accepted_at' ~ (SELECT receipt_timestamp FROM patterns)))
                    AND (s.value->'source_lineage'->'scan_completed_at' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'scan_completed_at') = 'string'
                            AND s.value->'source_lineage'->>'scan_completed_at' ~ (SELECT receipt_timestamp FROM patterns)))
                    AND (s.value->'source_lineage'->'content_verified_at' = 'null'::jsonb
                        OR (jsonb_typeof(s.value->'source_lineage'->'content_verified_at') = 'string'
                            AND s.value->'source_lineage'->>'content_verified_at' ~ (SELECT receipt_timestamp FROM patterns)))
                ))
            )
        ), false) AS supported
    FROM training_documents d
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(d.snapshot->'samples') = 'array'
            THEN d.snapshot->'samples' ELSE '[]'::jsonb END
    ) s(value)
), training_unknown AS (
    SELECT count(*) AS count FROM training_documents d
    WHERE NOT d.supported OR EXISTS (
        SELECT 1 FROM training_samples s WHERE s.id = d.id AND NOT s.supported
    )
), learning_sources AS MATERIALIZED (
    SELECT a.id, p.value->'snapshot' AS snapshot,
        COALESCE(jsonb_typeof(p.value) = 'object'
            AND jsonb_typeof(p.value->'snapshot') = 'object'
            AND NOT (p.value->'snapshot') ? 'asset_reference'
            AND NOT (p.value->'snapshot') ? 'source_lineage'
            AND (
            (a.spec->'original_contract' = '1'::jsonb AND COALESCE(jsonb_typeof(p.value->'snapshot'->'original') = 'object'
        AND p.value->'snapshot'->'original'->'schema' = '1'::jsonb
        AND p.value->'snapshot'->'original'->>'owner_kind' IN ('dataset_asset','task_original')
        AND (p.value->'snapshot'->'original') - ARRAY['schema','owner_kind','content_id','sha256','size_bytes','location_id'] = '{}'::jsonb
        AND jsonb_typeof(p.value->'snapshot'->'original'->'content_id') = 'string'
        AND p.value->'snapshot'->'original'->>'content_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(p.value->'snapshot'->'original'->'location_id') = 'string'
        AND p.value->'snapshot'->'original'->>'location_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(p.value->'snapshot'->'original'->'sha256') = 'string'
        AND p.value->'snapshot'->'original'->>'sha256' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(p.value->'snapshot'->'original'->'size_bytes') = 'number'
        AND p.value->'snapshot'->'original'->>'size_bytes' ~ '^[1-9][0-9]*$'
        AND (
            (p.value->'snapshot'->'original'->>'owner_kind' = 'dataset_asset' AND (
                EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_asset WHERE CAST(receipt_asset.asset_id AS text) = p.value->'snapshot'->'original'->>'content_id')
                OR EXISTS (SELECT 1 FROM public.managed_original_retirements receipt_peer WHERE CAST(receipt_peer.asset_id AS text) = p.value->'snapshot'->'original'->>'content_id')))
            OR (p.value->'snapshot'->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_task WHERE CAST(receipt_task.id AS text) = p.value->'snapshot'->'original'->>'content_id'))
        ), false))
            OR (NOT a.spec ? 'original_contract'
            AND jsonb_typeof(p.value->'snapshot'->'source_bucket') = 'string'
            AND length(p.value->'snapshot'->>'source_bucket') > 0
            AND jsonb_typeof(p.value->'snapshot'->'source_key') = 'string'
            AND length(p.value->'snapshot'->>'source_key') > 0)), false) AS supported
    FROM learning_history a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.spec->'pool') = 'array'
            THEN a.spec->'pool' ELSE '[]'::jsonb END
    ) p(value)
), decision_sources AS MATERIALIZED (
    SELECT a.id, p.value AS snapshot,
        COALESCE(jsonb_typeof(p.value) = 'object'
            AND NOT p.value ? 'asset_reference' AND NOT p.value ? 'source_lineage'
            AND (
            (p.value ? 'original' AND COALESCE(jsonb_typeof(p.value->'original') = 'object'
        AND p.value->'original'->'schema' = '1'::jsonb
        AND p.value->'original'->>'owner_kind' IN ('dataset_asset','task_original')
        AND (p.value->'original') - ARRAY['schema','owner_kind','content_id','sha256','size_bytes','location_id'] = '{}'::jsonb
        AND jsonb_typeof(p.value->'original'->'content_id') = 'string'
        AND p.value->'original'->>'content_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(p.value->'original'->'location_id') = 'string'
        AND p.value->'original'->>'location_id' ~ (SELECT receipt_uuid FROM patterns)
        AND jsonb_typeof(p.value->'original'->'sha256') = 'string'
        AND p.value->'original'->>'sha256' ~ '^[0-9a-f]{64}$'
        AND jsonb_typeof(p.value->'original'->'size_bytes') = 'number'
        AND p.value->'original'->>'size_bytes' ~ '^[1-9][0-9]*$'
        AND (
            (p.value->'original'->>'owner_kind' = 'dataset_asset' AND (
                EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_asset WHERE CAST(receipt_asset.asset_id AS text) = p.value->'original'->>'content_id')
                OR EXISTS (SELECT 1 FROM public.managed_original_retirements receipt_peer WHERE CAST(receipt_peer.asset_id AS text) = p.value->'original'->>'content_id')))
            OR (p.value->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_task WHERE CAST(receipt_task.id AS text) = p.value->'original'->>'content_id'))
        ), false))
            OR (NOT p.value ? 'original' AND jsonb_typeof(p.value->'source_bucket') = 'string'
            AND length(p.value->>'source_bucket') > 0
            AND jsonb_typeof(p.value->'source_key') = 'string'
            AND length(p.value->>'source_key') > 0)), false) AS supported
    FROM learning_decisions a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(a.snapshot->'source_snapshots') = 'array'
            THEN a.snapshot->'source_snapshots' ELSE '[]'::jsonb END
    ) p(value)
), learning_unknown AS (
    SELECT count(*) AS count FROM learning_history a
    WHERE NOT COALESCE(jsonb_typeof(a.spec) = 'object'
        AND a.spec->'schema' IN ('1'::jsonb, '2'::jsonb)
        AND (NOT a.spec ? 'original_contract' OR a.spec->'original_contract' = '1'::jsonb)
        AND jsonb_typeof(a.spec->'pool') = 'array', false)
        OR EXISTS (SELECT 1 FROM learning_sources s WHERE s.id = a.id AND NOT s.supported)
        OR EXISTS (SELECT 1 FROM learning_decisions d
            WHERE d.round_id = a.id AND (
                NOT COALESCE(jsonb_typeof(d.snapshot->'source_snapshots') = 'array', false)
                OR EXISTS (SELECT 1 FROM decision_sources s WHERE s.id = d.id AND NOT s.supported)
            ))
),
evidence AS MATERIALIZED (
    SELECT r.*, clock.measured_at,
        p.peer_assertions, p.indefinite_retention, p.effective_retain_until,
        p.declared_backup_holds, p.declared_migration_holds,
        CASE WHEN r.identity_state <> 'verified_binding' OR r.storage_identity IS NULL
            OR r.binding_generation IS NULL OR r.location_config_revision IS NULL
            THEN 'unknown'
        WHEN EXISTS (SELECT 1 FROM public.managed_original_creations creation
            WHERE creation.id = r.creation_id AND creation.status = 'verified'
                AND creation.storage_identity = r.storage_identity
                AND creation.bucket = r.bucket AND creation.object_key = r.object_key
                AND creation.location_id = r.location_id
                AND creation.configuration->'original_ref'->>'location_id' = CAST(r.location_id AS text)
                AND creation.configuration->'original_ref'->>'bucket' = r.bucket
                AND creation.configuration->'original_ref'->>'object_key' = r.object_key)
            THEN 'current'
        WHEN r.location_id = CAST(p_location_id AS uuid)
            AND r.storage_identity = p_storage_identity AND r.bucket = p_datasets_bucket
            AND b.location_id = r.location_id AND b.bucket = r.bucket
            AND b.generation = r.binding_generation
            AND l.kind = 's3' AND l.config = CAST(p_location_config AS jsonb)
            AND l.config_revision = r.location_config_revision
            THEN 'current' ELSE 'mismatch' END AS current_identity,
        ((SELECT count(*) FROM public.dataset_asset_locations x
            WHERE x.bucket = r.bucket AND x.object_key = r.object_key)
         + (SELECT count(*) FROM public.dataset_auxiliary_originals x
            WHERE x.bucket = r.bucket AND x.object_key = r.object_key)) AS asset_locations,
        (SELECT count(*) FROM public.dataset_items i
            WHERE r.bucket = p_datasets_bucket AND i.file_path = r.object_key) AS dataset_items,
        (SELECT count(*) FROM yoao_labeling_v1.original_reclaim_task_evidence(r.object_key) t
            WHERE r.bucket = CASE WHEN t.dataset_item_id IS NULL
                THEN p_annotations_bucket ELSE p_datasets_bucket END) AS tasks,
        (SELECT count(*) FROM yoao_labeling_v1.original_reclaim_task_evidence(r.object_key) t
            WHERE r.bucket <> CASE WHEN t.dataset_item_id IS NULL
                THEN p_annotations_bucket ELSE p_datasets_bucket END) AS ambiguous_legacy_tasks,
        (SELECT count(*) FROM yoao_labeling_v1.task_dataset_item_link_rows k
            JOIN public.dataset_items i ON i.id = k.dataset_item_id
            WHERE (r.bucket = p_datasets_bucket AND i.file_path = r.object_key)
                OR EXISTS (SELECT 1 FROM public.dataset_assets a
                    JOIN public.dataset_asset_locations x ON x.asset_id = a.id
                    WHERE a.dataset_item_id = i.id
                        AND x.bucket = r.bucket AND x.object_key = r.object_key)) AS task_dataset_links,
        (SELECT count(DISTINCT s.id) FROM training_samples s WHERE
            (s.sample->>'source_bucket' = r.bucket AND s.sample->>'source_key' = r.object_key)
            OR (s.sample->'original'->>'owner_kind' = 'dataset_asset'
                AND (EXISTS (SELECT 1 FROM public.managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.sample->'original'->>'content_id' = CAST(original_peer.asset_id AS text))
                    OR EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_location
                        WHERE CAST(receipt_location.asset_id AS text) = s.sample->'original'->>'content_id'
                            AND receipt_location.bucket = r.bucket AND receipt_location.object_key = r.object_key)))
            OR (s.sample->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_owner
                WHERE CAST(receipt_owner.id AS text) = s.sample->'original'->>'content_id'
                    AND receipt_owner.bucket = r.bucket AND receipt_owner.object_key = r.object_key))
            OR EXISTS (SELECT 1 FROM public.dataset_scan_entries alias_entry
                WHERE CAST(alias_entry.run_id AS text) = s.sample->'source_lineage'->>'scan_run_id'
                    AND alias_entry.path_digest = s.sample->'source_lineage'->>'path_digest'
                    AND alias_entry.observed->>'kind' = 's3'
                    AND alias_entry.observed->>'bucket' = r.bucket
                    AND alias_entry.observed->>'key' = r.object_key)

            OR EXISTS (SELECT 1 FROM public.managed_original_retirements peer
                WHERE md5(peer.bucket) = md5(r.bucket)
                    AND md5(peer.object_key) = md5(r.object_key)
                    AND peer.bucket = r.bucket
                    AND peer.object_key = r.object_key
                    AND (s.sample->'asset_reference'->>'asset_id' = CAST(peer.asset_id AS text)
                        OR s.sample->'asset_reference'->>'dataset_item_id' = CAST(peer.dataset_item_id AS text)))
        ) AS training_versions,
        ((SELECT count(DISTINCT s.id) FROM learning_sources s WHERE
            (s.snapshot->>'source_bucket' = r.bucket AND s.snapshot->>'source_key' = r.object_key)
            OR (s.snapshot->'original'->>'owner_kind' = 'dataset_asset'
                AND (EXISTS (SELECT 1 FROM public.managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->'original'->>'content_id' = CAST(original_peer.asset_id AS text))
                    OR EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_location
                        WHERE CAST(receipt_location.asset_id AS text) = s.snapshot->'original'->>'content_id'
                            AND receipt_location.bucket = r.bucket AND receipt_location.object_key = r.object_key)))
            OR (s.snapshot->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_owner
                WHERE CAST(receipt_owner.id AS text) = s.snapshot->'original'->>'content_id'
                    AND receipt_owner.bucket = r.bucket AND receipt_owner.object_key = r.object_key))

            OR EXISTS (SELECT 1 FROM public.managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->>'dataset_item_id' = CAST(original_peer.dataset_item_id AS text)))
         + (SELECT count(DISTINCT s.id) FROM decision_sources s WHERE
            (s.snapshot->>'source_bucket' = r.bucket AND s.snapshot->>'source_key' = r.object_key)
            OR (s.snapshot->'original'->>'owner_kind' = 'dataset_asset'
                AND (EXISTS (SELECT 1 FROM public.managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->'original'->>'content_id' = CAST(original_peer.asset_id AS text))
                    OR EXISTS (SELECT 1 FROM public.dataset_asset_locations receipt_location
                        WHERE CAST(receipt_location.asset_id AS text) = s.snapshot->'original'->>'content_id'
                            AND receipt_location.bucket = r.bucket AND receipt_location.object_key = r.object_key)))
            OR (s.snapshot->'original'->>'owner_kind' = 'task_original' AND EXISTS (
                SELECT 1 FROM public.task_originals receipt_owner
                WHERE CAST(receipt_owner.id AS text) = s.snapshot->'original'->>'content_id'
                    AND receipt_owner.bucket = r.bucket AND receipt_owner.object_key = r.object_key))
            OR EXISTS (SELECT 1 FROM public.managed_original_retirements original_peer WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key AND s.snapshot->>'dataset_item_id' = CAST(original_peer.dataset_item_id AS text)))
         + yoao_learning_v1.active_learning_pool_item_count(ARRAY(
            SELECT original_peer.dataset_item_id FROM public.managed_original_retirements original_peer
            WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key))
         + yoao_learning_v1.active_learning_candidate_item_count(ARRAY(
            SELECT original_peer.dataset_item_id FROM public.managed_original_retirements original_peer
            WHERE original_peer.bucket = r.bucket AND original_peer.object_key = r.object_key))
        ) AS active_learning_roots,
        u.count AS unknown_training_versions,
        au.count AS unknown_active_learning_rounds
    FROM retirement_scope r
    JOIN peer_protection p ON md5(p.bucket) = md5(r.bucket)
        AND md5(p.object_key) = md5(r.object_key)
        AND p.bucket = r.bucket AND p.object_key = r.object_key
    CROSS JOIN clock CROSS JOIN training_unknown u CROSS JOIN learning_unknown au
    LEFT JOIN public.storage_bindings b ON b.purpose = 'datasets'
    LEFT JOIN public.storage_locations l ON l.id = r.location_id
),
classified AS MATERIALIZED (
    SELECT e.*,
        (asset_locations + dataset_items + tasks + task_dataset_links
            + training_versions + active_learning_roots
            + declared_backup_holds + declared_migration_holds > 0) AS protected,
        (indefinite_retention OR effective_retain_until > measured_at) AS retained,
        (NOT indefinite_retention AND effective_retain_until <= measured_at
            AND declared_backup_holds = 0 AND declared_migration_holds = 0
            AND current_identity = 'current'
            AND asset_verification_state = 'verified' AND sha256 IS NOT NULL
            AND sha256 ~ '^[0-9a-f]{64}$'
            AND size_bytes IS NOT NULL AND reference_state = 'present'
            AND verified_at IS NOT NULL
            AND asset_locations + dataset_items + tasks + task_dataset_links
                + training_versions + active_learning_roots = 0
            AND ambiguous_legacy_tasks = 0 AND unknown_training_versions = 0
            AND unknown_active_learning_rounds = 0) AS policy_candidate
    FROM evidence e
), page AS (
    SELECT * FROM classified ORDER BY retired_at, id LIMIT LEAST(p_limit, 100) OFFSET p_offset
), totals AS (
    SELECT count(*) AS total_assertions,
        count(*) FILTER (WHERE protected) AS protected_assertions,
        count(*) FILTER (WHERE NOT protected AND retained) AS retained_assertions,
        count(*) FILTER (WHERE NOT protected AND NOT retained) AS blocked_assertions,
        count(*) FILTER (WHERE policy_candidate) AS policy_candidate_assertions,
        count(*) FILTER (WHERE size_bytes IS NULL) AS unknown_size_assertions,
        COALESCE(sum(size_bytes), 0) AS known_asserted_bytes
    FROM classified
),
own_operation AS (
    SELECT a.operation_id FROM public.managed_original_reclaim_attempts a
    JOIN public.managed_original_reclaim_claims c ON c.current_attempt_id = a.id
    JOIN evidence r ON r.id = a.retirement_id AND r.creation_id = a.creation_id
    JOIN LATERAL yoao_jobs_v1.read_job(a.async_job_id) job ON true
    JOIN public.storage_operations op ON op.id = a.operation_id
    WHERE a.id = CAST(p_reclaim_attempt_id AS uuid) AND a.status = 'running'
        AND job.status = 'running' AND job.kind = 'managed_original_reclaim'
        AND job.celery_task_id = a.task_id AND job.user_id = a.requested_by
        AND job.payload->>'reclaim_id' = CAST(a.id AS text)
        AND job.payload->>'retirement_id' = CAST(r.id AS text)
        AND op.purpose = 'managed_original_reclaim' AND op.location_id = r.location_id
        AND op.revision = a.maintenance_revision
        AND NOT a.physical_settled AND c.state IN ('claimed','tombstone')
        AND c.creation_id = a.creation_id AND c.version_id = a.version_id
        AND c.bucket = r.bucket AND c.object_key = r.object_key
        AND a.target->>'location_id' = CAST(r.location_id AS text)
        AND a.target->>'bucket' = r.bucket AND a.target->>'object_key' = r.object_key
        AND a.operation_id IS NOT NULL
),
aliases AS MATERIALIZED (
    SELECT d.dataset_item_id, d.run_id, d.path_digest, e.observed,
        run.snapshot, source.location_id, location.kind AS location_kind
    FROM public.dataset_references d
    JOIN public.dataset_scan_entries e ON e.run_id = d.run_id AND e.path_digest = d.path_digest
    JOIN public.dataset_scan_runs run ON run.id = e.run_id
    JOIN public.dataset_sources source ON source.id = d.source_id
    JOIN public.storage_locations location ON location.id = source.location_id
), unknown_aliases AS (
    SELECT count(*) AS count FROM aliases a WHERE a.location_kind = 's3'
        AND NOT COALESCE(a.observed->>'kind' = 's3'
            AND jsonb_typeof(a.observed->'bucket') = 'string'
            AND length(a.observed->>'bucket') > 0
            AND jsonb_typeof(a.observed->'key') = 'string'
            AND length(a.observed->>'key') > 0, false)
),
unknown_operations AS (
    SELECT count(*) AS count FROM public.storage_operations o
    JOIN public.storage_locations sl ON sl.id = o.location_id
    LEFT JOIN public.storage_connections sc ON sc.id = sl.connection_id
    WHERE sl.kind = 's3' AND sl.id <> CAST(p_location_id AS uuid)
        AND NOT COALESCE(jsonb_typeof(sc.config->'bucket') = 'string'
            AND length(sc.config->>'bucket') > 0, false)
        AND NOT EXISTS (SELECT 1 FROM own_operation own WHERE own.operation_id = o.id)
),
groups AS (
    SELECT r.*, to_jsonb(anchor) AS anchor, to_jsonb(creation) AS creation,
        jsonb_build_object('location', to_jsonb(location),
            'connection', CASE WHEN connection.id IS NOT NULL THEN jsonb_build_object(
                'id', connection.id, 'name', connection.name, 'revision', connection.revision,
                'kind', connection.kind, 'config', connection.config, 'scope', connection.scope,
                'project_id', connection.project_id, 'created_by', connection.created_by,
                'created_at', connection.created_at, 'updated_at', connection.updated_at) END) AS configuration,
        COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.id)
            FROM locator_peers p
            WHERE p.bucket = r.bucket AND p.object_key = r.object_key), '[]'::jsonb) AS peers,
        COALESCE((SELECT jsonb_agg(to_jsonb(h) ORDER BY h.id)
            FROM public.managed_original_retention_holds h
            JOIN locator_peers p ON p.id = h.retirement_id
            WHERE p.bucket = r.bucket AND p.object_key = r.object_key), '[]'::jsonb) AS holds,
        COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.id) FROM public.task_originals o
            WHERE o.bucket = r.bucket AND o.object_key = r.object_key), '[]'::jsonb) AS task_originals,
        COALESCE((SELECT jsonb_agg(to_jsonb(a) ORDER BY a.dataset_item_id) FROM aliases a
            WHERE a.observed->>'kind' = 's3' AND a.observed->>'bucket' = r.bucket
                AND a.observed->>'key' = r.object_key), '[]'::jsonb) AS reference_aliases,
        ua.count AS unknown_reference_aliases,
        COALESCE((SELECT jsonb_agg(jsonb_build_object('id', i.async_job_id, 'status', i.status,
                'source', i.source, 'operation_id', i.operation_id) ORDER BY i.async_job_id)
            FROM public.storage_migration_inventories i
            WHERE (i.status IN ('pending','running') OR EXISTS (
                SELECT 1 FROM public.storage_operations o WHERE o.id = i.operation_id))
                AND EXISTS (SELECT 1 FROM jsonb_each_text(CASE WHEN jsonb_typeof(i.source->'purpose_buckets') = 'object' THEN i.source->'purpose_buckets' ELSE '{}'::jsonb END) b
                    WHERE b.value = r.bucket)), '[]'::jsonb) AS migration_inventories,
        COALESCE((SELECT jsonb_agg(jsonb_build_object('id', p.id,
                'source', p.source, 'target', p.target, 'summary', p.summary,
                'delta_sequence', p.delta_sequence) ORDER BY p.id)
            FROM public.storage_migration_copies p
            WHERE (NOT COALESCE(p.summary->'copy_subset_complete' = 'true'::jsonb, false)
                OR EXISTS (SELECT 1 FROM public.storage_migration_copy_attempts a
                    WHERE a.copy_id = p.id AND (a.status IN ('pending','running')
                        OR EXISTS (SELECT 1 FROM public.storage_operations o WHERE o.id IN
                            (a.source_operation_id, a.target_operation_id)))))
                AND (p.target->>'bucket' = r.bucket OR EXISTS (
                    SELECT 1 FROM jsonb_each_text(CASE WHEN jsonb_typeof(p.source->'purpose_buckets') = 'object' THEN p.source->'purpose_buckets' ELSE '{}'::jsonb END) b
                    WHERE b.value = r.bucket))), '[]'::jsonb) AS migration_copies,
        COALESCE((SELECT jsonb_agg(to_jsonb(o) ORDER BY o.id)
            FROM public.storage_operations o
            WHERE NOT EXISTS (SELECT 1 FROM own_operation own WHERE own.operation_id = o.id)
                AND (o.location_id = r.location_id OR EXISTS (
                    SELECT 1 FROM public.managed_original_retirements physical_peer
                    WHERE physical_peer.bucket = r.bucket AND physical_peer.object_key = r.object_key
                        AND physical_peer.location_id = o.location_id)
                OR EXISTS (
                    SELECT 1 FROM public.dataset_asset_locations al
                    WHERE al.location_id = o.location_id AND al.bucket = r.bucket AND al.object_key = r.object_key)
                OR EXISTS (SELECT 1 FROM public.task_originals t WHERE t.location_id = o.location_id
                    AND t.bucket = r.bucket AND t.object_key = r.object_key)
                OR EXISTS (SELECT 1 FROM public.managed_original_creations saved_creation
                    WHERE saved_creation.location_id = o.location_id
                        AND saved_creation.bucket = r.bucket AND saved_creation.object_key = r.object_key)
                OR EXISTS (SELECT 1 FROM aliases a WHERE a.location_id = o.location_id
                    AND a.observed->>'kind' = 's3' AND a.observed->>'bucket' = r.bucket
                    AND a.observed->>'key' = r.object_key)
                OR EXISTS (SELECT 1 FROM public.dataset_scan_runs saved_run
                    JOIN public.dataset_sources saved_source ON saved_source.id = saved_run.source_id
                    WHERE saved_source.location_id = o.location_id
                        AND saved_run.snapshot->'anchor'->>'kind' = 's3'
                        AND saved_run.snapshot->'anchor'->>'bucket' = r.bucket)
                OR EXISTS (SELECT 1 FROM public.storage_locations sl
                    LEFT JOIN public.storage_connections sc ON sc.id = sl.connection_id
                    WHERE sl.id = o.location_id AND sl.kind = 's3'
                        AND (sc.config->>'bucket' = r.bucket
                            OR sl.id = CAST(p_location_id AS uuid))))) , '[]'::jsonb) AS operations,
        uo.count AS unknown_operations,
        jsonb_build_object(
            'assets', (SELECT COALESCE(jsonb_agg(to_jsonb(a) ORDER BY a.asset_id, a.location_id, a.bucket), '[]'::jsonb)
                FROM public.dataset_asset_locations a WHERE a.bucket = r.bucket AND a.object_key = r.object_key),
            'auxiliary', (SELECT COALESCE(jsonb_agg(to_jsonb(a) ORDER BY a.id), '[]'::jsonb)
                FROM public.dataset_auxiliary_originals a WHERE a.bucket = r.bucket AND a.object_key = r.object_key),
            'items', (SELECT COALESCE(jsonb_agg(to_jsonb(i) ORDER BY i.id), '[]'::jsonb)
                FROM public.dataset_items i WHERE i.file_path = r.object_key),
            'tasks', (SELECT COALESCE(jsonb_agg(t.evidence ORDER BY t.id), '[]'::jsonb)
                FROM yoao_labeling_v1.original_reclaim_task_evidence(r.object_key) t),
            'training', (SELECT md5(COALESCE(string_agg(md5(jsonb_build_object('id', v.id, 'snapshot', v.snapshot)::text), '' ORDER BY v.id), ''))
                FROM training_history v),
            'learning', (SELECT md5(COALESCE(string_agg(md5(jsonb_build_object('id', a.id, 'spec', a.spec)::text), '' ORDER BY a.id), ''))
                FROM learning_history a),
            'decisions', (SELECT md5(COALESCE(string_agg(md5(jsonb_build_object('id', a.id, 'snapshot', a.snapshot)::text), '' ORDER BY a.id), ''))
                FROM learning_decisions a)
        ) AS root_evidence
    FROM evidence r JOIN public.managed_original_retirements anchor ON anchor.id = r.id
    LEFT JOIN public.managed_original_creations creation ON creation.id = r.creation_id
    LEFT JOIN public.storage_locations location ON location.id = r.location_id
    LEFT JOIN public.storage_connections connection ON connection.id = location.connection_id
    CROSS JOIN unknown_aliases ua CROSS JOIN unknown_operations uo
),
grouped AS (
    SELECT bucket, object_key,
        bool_or(asset_locations + dataset_items + tasks + task_dataset_links
            + training_versions + active_learning_roots + ambiguous_legacy_tasks
            + jsonb_array_length(task_originals) + jsonb_array_length(reference_aliases)
            + jsonb_array_length(migration_inventories) + jsonb_array_length(migration_copies)
            + jsonb_array_length(operations) + declared_backup_holds + declared_migration_holds > 0) AS protected,
        bool_or(indefinite_retention OR effective_retain_until > measured_at) AS retained,
        bool_or(unknown_training_versions + unknown_active_learning_rounds
            + unknown_reference_aliases + unknown_operations > 0) AS blocked
    FROM groups GROUP BY bucket, object_key
)
SELECT p.measured_at, p.configuration_fingerprint, p.counts, p.items, NULL::jsonb, NULL::jsonb
FROM (
SELECT clock.measured_at, jsonb_build_object(
    'location', (SELECT to_jsonb(l) FROM public.storage_locations l
        WHERE l.id = CAST(p_location_id AS uuid)),
    'bindings', (SELECT COALESCE(jsonb_agg(to_jsonb(b) ORDER BY b.purpose), '[]'::jsonb)
        FROM public.storage_bindings b)
) AS configuration_fingerprint,
    to_jsonb(totals) || jsonb_build_object(
        'unknown_training_versions', u.count,
        'unknown_active_learning_rounds', au.count
    ) AS counts,
    COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'id', p.id, 'owner_type', p.owner_type, 'asset_id', p.asset_id,
        'auxiliary_original_id', p.auxiliary_original_id, 'dataset_id', p.dataset_id,
        'dataset_item_id', p.dataset_item_id, 'retired_at', p.retired_at,
        'policy_revision', p.policy_revision, 'retain_until', p.retain_until,
        'effective_retain_until', p.effective_retain_until,
        'indefinite_retention', p.indefinite_retention,
        'peer_assertions', p.peer_assertions,
        'declared_backup_holds', p.declared_backup_holds,
        'declared_migration_holds', p.declared_migration_holds,
        'size_bytes', p.size_bytes,
        'identity_state', p.current_identity,
        'status', CASE WHEN p.protected THEN 'protected'
            WHEN p.retained THEN 'retained' ELSE 'blocked' END,
        'policy_candidate', p.policy_candidate,
        'roots', jsonb_build_object(
            'asset_locations', p.asset_locations, 'dataset_items', p.dataset_items,
            'tasks', p.tasks, 'task_dataset_links', p.task_dataset_links,
            'training_versions', p.training_versions,
            'active_learning_roots', p.active_learning_roots,
            'ambiguous_legacy_tasks', p.ambiguous_legacy_tasks
        ),
        'blockers', to_jsonb(array_remove(ARRAY[
            CASE WHEN p.asset_locations > 0 THEN 'current_asset_location' END,
            CASE WHEN p.dataset_items > 0 THEN 'current_dataset_item' END,
            CASE WHEN p.tasks > 0 THEN 'current_task' END,
            CASE WHEN p.task_dataset_links > 0 THEN 'current_task_dataset_link' END,
            CASE WHEN p.training_versions > 0 THEN 'retained_training_version' END,
            CASE WHEN p.active_learning_roots > 0 THEN 'active_learning_history' END,
            CASE WHEN p.retain_until IS NULL THEN 'indefinite_retention' END,
            CASE WHEN p.retain_until > p.measured_at THEN 'retention_window' END,
            CASE WHEN p.retain_until IS NOT NULL AND p.indefinite_retention
                THEN 'related_indefinite_retention' END,
            CASE WHEN p.effective_retain_until > p.measured_at
                AND p.retain_until < p.effective_retain_until
                THEN 'related_retention_window' END,
            CASE WHEN p.declared_backup_holds > 0 THEN 'declared_backup_hold' END,
            CASE WHEN p.declared_migration_holds > 0 THEN 'declared_migration_hold' END,
            CASE WHEN p.current_identity = 'unknown' THEN 'storage_identity_unknown' END,
            CASE WHEN p.current_identity = 'mismatch' THEN 'storage_identity_mismatch' END,
            CASE WHEN p.asset_verification_state <> 'verified' OR p.sha256 IS NULL
                OR p.sha256 !~ '^[0-9a-f]64$' OR p.size_bytes IS NULL
                THEN 'content_identity_unverified' END,
            CASE WHEN p.reference_state <> 'present' OR p.verified_at IS NULL
                THEN 'locator_unverified' END,
            CASE WHEN p.unknown_training_versions > 0 THEN 'unsupported_training_snapshot' END,
            CASE WHEN p.unknown_active_learning_rounds > 0 THEN 'unsupported_active_learning_snapshot' END,
            CASE WHEN p.ambiguous_legacy_tasks > 0 THEN 'legacy_task_bucket_unknown' END,
            'exclusive_ownership_unproven', 'backup_holds_unknown',
            'migration_holds_unknown', 'object_presence_not_checked'
        ]::text[], NULL))
    ) ORDER BY p.retired_at, p.id) FROM page p), '[]'::jsonb) AS items
FROM clock CROSS JOIN totals CROSS JOIN training_unknown u CROSS JOIN learning_unknown au
) p WHERE p_mode = 'preview'
UNION ALL
SELECT g.measured_at, NULL::jsonb, NULL::jsonb, NULL::jsonb, to_jsonb(g), NULL::jsonb
FROM groups g WHERE p_mode = 'reclaim'
UNION ALL
SELECT r.measured_at, NULL::jsonb, r.counts, NULL::jsonb, NULL::jsonb, r.unknown
FROM (
SELECT clock.measured_at,
    jsonb_build_object(
        'total_assertions', (SELECT count(*) FROM public.managed_original_retirements),
        'physical_groups', count(g.*),
        'protected_groups', count(g.*) FILTER (WHERE g.protected),
        'retained_groups', count(g.*) FILTER (WHERE NOT g.protected AND g.retained),
        'blocked_groups', count(g.*) FILTER (WHERE NOT g.protected AND NOT g.retained AND g.blocked),
        'unreferenced_groups', count(g.*) FILTER (WHERE NOT g.protected AND NOT g.retained AND NOT g.blocked)
    ) AS counts,
    jsonb_build_object(
        'unknown_training_versions', u.count,
        'unknown_active_learning_rounds', au.count,
        'unknown_reference_aliases', ua.count,
        'unknown_operations', uo.count
    ) AS unknown
FROM clock CROSS JOIN training_unknown u CROSS JOIN learning_unknown au
CROSS JOIN unknown_aliases ua CROSS JOIN unknown_operations uo
LEFT JOIN grouped g ON true
GROUP BY clock.measured_at, u.count, au.count, ua.count, uo.count
) r WHERE p_mode = 'reconcile'
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_inference_topology_v1.patch_registry_plugin_legacy(p_registry_id uuid, p_patch text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET "TimeZone" TO 'UTC'
 SET "DateStyle" TO 'ISO, YMD'
 SET extra_float_digits TO '3'
AS $function$
DECLARE
    v_patch jsonb;
    v_registry public.ml_backend_registry%ROWTYPE;
    v_protected_change boolean;
    v_next_url text;
    v_owner text;
    v_runtime_epoch bigint;
BEGIN
    IF p_registry_id IS NULL OR p_patch IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Registry patch';
    END IF;
    v_patch := p_patch::jsonb;
    IF pg_catalog.jsonb_typeof(v_patch) <> 'object'
       OR v_patch - ARRAY[
            'name', 'url', 'is_interactive', 'auth_method', 'auth_token',
            'extra_params', 'gpu_resource_id', 'vram_budget_mb', 'eviction_priority'
       ] <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unsupported Registry patch field';
    END IF;
    SELECT registry.* INTO v_registry
    FROM public.ml_backend_registry AS registry
    WHERE registry.id = p_registry_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_patch ? 'url' THEN
        IF jsonb_typeof(v_patch->'url') <> 'string' OR v_patch->>'url' = '' THEN
            RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'url must not be null or empty';
        END IF;
        v_next_url := v_patch->>'url';
        SELECT registry.name INTO v_owner
        FROM public.ml_backend_registry AS registry
        WHERE registry.url = v_next_url AND registry.id <> p_registry_id;
        IF FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:registry_url_conflict:' || v_owner || ':';
        END IF;
    ELSE
        v_next_url := v_registry.url;
    END IF;
    v_protected_change :=
        (v_patch ? 'url' AND v_next_url IS DISTINCT FROM v_registry.url)
        OR (v_patch ? 'auth_method' AND v_patch->>'auth_method' IS DISTINCT FROM v_registry.auth_method)
        OR (v_patch ? 'auth_token' AND v_patch->>'auth_token' IS DISTINCT FROM v_registry.auth_token)
        OR (v_patch ? 'extra_params' AND v_patch->'extra_params' IS DISTINCT FROM v_registry.extra_params)
        OR (v_patch ? 'gpu_resource_id' AND v_patch->>'gpu_resource_id' IS DISTINCT FROM v_registry.gpu_resource_id)
        OR (v_patch ? 'vram_budget_mb' AND (v_patch->>'vram_budget_mb')::integer IS DISTINCT FROM v_registry.vram_budget_mb)
        OR (v_patch ? 'eviction_priority' AND (v_patch->>'eviction_priority')::integer IS DISTINCT FROM v_registry.eviction_priority);
    IF v_protected_change THEN
        SELECT fence.runtime_epoch_high_water INTO v_runtime_epoch
        FROM public.gpu_backend_fences AS fence
        WHERE fence.backend_registry_id = p_registry_id;
        IF COALESCE(v_runtime_epoch, 0) > 0 THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:managed_gpu_mutation:managed GPU backend requires retirement before mutation:';
        END IF;
    END IF;
    BEGIN
        UPDATE public.ml_backend_registry AS registry
        SET name = CASE WHEN v_patch ? 'name' THEN v_patch->>'name' ELSE registry.name END,
            url = CASE WHEN v_patch ? 'url' THEN v_next_url ELSE registry.url END,
            is_interactive = CASE WHEN v_patch ? 'is_interactive'
                THEN (v_patch->>'is_interactive')::boolean ELSE registry.is_interactive END,
            auth_method = CASE WHEN v_patch ? 'auth_method'
                THEN v_patch->>'auth_method' ELSE registry.auth_method END,
            auth_token = CASE WHEN v_patch ? 'auth_token'
                THEN v_patch->>'auth_token' ELSE registry.auth_token END,
            extra_params = CASE WHEN v_patch ? 'extra_params'
                THEN v_patch->'extra_params' ELSE registry.extra_params END,
            gpu_resource_id = CASE WHEN v_patch ? 'gpu_resource_id'
                THEN v_patch->>'gpu_resource_id' ELSE registry.gpu_resource_id END,
            vram_budget_mb = CASE WHEN v_patch ? 'vram_budget_mb'
                THEN (v_patch->>'vram_budget_mb')::integer ELSE registry.vram_budget_mb END,
            eviction_priority = CASE WHEN v_patch ? 'eviction_priority'
                THEN (v_patch->>'eviction_priority')::integer ELSE registry.eviction_priority END,
            state = CASE WHEN v_protected_change THEN 'disconnected' ELSE registry.state END,
            health_meta = CASE WHEN v_protected_change THEN NULL ELSE registry.health_meta END,
            last_checked_at = CASE WHEN v_protected_change THEN NULL ELSE registry.last_checked_at END,
            error_message = CASE WHEN v_protected_change THEN NULL ELSE registry.error_message END,
            updated_at = pg_catalog.now()
        WHERE registry.id = p_registry_id;
    EXCEPTION WHEN unique_violation THEN
        SELECT registry.name INTO v_owner
        FROM public.ml_backend_registry AS registry
        WHERE registry.url = v_next_url AND registry.id <> p_registry_id;
        IF FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = 'P0001',
                MESSAGE = 'aap_inference_topology:registry_url_conflict:' || v_owner || ':';
        END IF;
        RAISE;
    END;
    RETURN yoao_inference_topology_v1._registry_json(p_registry_id);
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_inference_gpu_v1.gpu_authority_step(p_action text, p_arguments text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET "TimeZone" TO 'UTC'
 SET "DateStyle" TO 'ISO, YMD'
 SET extra_float_digits TO '3'
AS $function$
DECLARE
    v_args jsonb;
    v_result jsonb;
    v_id uuid;
    v_id2 uuid;
    v_resource text;
    v_now timestamp with time zone;
    v_time timestamp with time zone;
    v_token_expiry timestamp with time zone;
    v_membership public.gpu_backend_memberships%ROWTYPE;
    v_fence public.gpu_backend_fences%ROWTYPE;
    v_registry public.ml_backend_registry%ROWTYPE;
    v_health jsonb;
    v_residency jsonb;
    v_probe jsonb;
    v_subject jsonb;
    v_expected jsonb;
    v_proof jsonb;
    v_memberships jsonb;
    v_expected_item jsonb;
    v_previous_id uuid;
    v_text text;
    v_state text;
    v_reason text;
    v_counter text;
    v_next bigint;
    v_generation bigint;
    v_control bigint;
    v_runtime bigint;
    v_budget bigint;
    v_priority bigint;
    v_concurrency integer;
    v_boot text;
    v_observed_generation text;
    v_pools jsonb;
    v_require_idle boolean;
    v_challenge text;
    v_transition uuid;
    v_target text;
    v_operation text;
    v_blocker text;
    v_wait boolean;
    v_status text;
    v_trusted_empty boolean;
    v_reset_ack boolean;
    v_intent_matches boolean;
    v_enforce_intent boolean;
    v_rollout public.gpu_arbiter_rollouts%ROWTYPE;
    v_cancel public.gpu_backend_cancel_intents%ROWTYPE;
    v_terminal_intent yoao_inference_gpu_v1.installation_terminal_intents%ROWTYPE;
    v_install_control yoao_inference_gpu_v1.installation_control_intents%ROWTYPE;
    v_key bigint;
    v_probe_started timestamp with time zone;
    v_observed timestamp with time zone;
    v_deadline bigint;
    v_fingerprint text;
    v_domain jsonb;
    v_registry_exists boolean;
    v_retirement uuid;
BEGIN
    IF p_action IS NULL OR p_action !~ '^[a-z][a-z0-9_]{0,63}$'
       OR p_arguments IS NULL OR pg_catalog.octet_length(p_arguments) > 1048576 THEN
        PERFORM yoao_inference_gpu_v1._fail();
    END IF;
    BEGIN v_args := p_arguments::jsonb;
    EXCEPTION WHEN invalid_text_representation THEN
        PERFORM yoao_inference_gpu_v1._fail();
    END;
    IF pg_catalog.jsonb_typeof(v_args) IS DISTINCT FROM 'object' THEN
        PERFORM yoao_inference_gpu_v1._fail();
    END IF;
    v_now := pg_catalog.transaction_timestamp();

    IF p_action = 'fence_advance' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args, ARRAY['backend_registry_id','counter','gpu_resource_id','membership_epoch','token_expires_at']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_id := yoao_inference_gpu_v1._uuid(v_args->'backend_registry_id');
        v_resource := yoao_inference_gpu_v1._resource(v_args->'gpu_resource_id');
        v_next := yoao_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807);
        v_counter := yoao_inference_gpu_v1._string(v_args->'counter',32);
        IF v_counter NOT IN ('generation','control_epoch') OR (v_args->'token_expires_at' <> 'null'::jsonb AND pg_catalog.jsonb_typeof(v_args->'token_expires_at') <> 'string') THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_token_expiry := CASE WHEN v_args->'token_expires_at' = 'null'::jsonb THEN NULL ELSE yoao_inference_gpu_v1._timestamp(v_args->'token_expires_at') END;
        PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND OR v_membership.state <> 'active' OR v_membership.membership_epoch <> v_next THEN
            v_result := pg_catalog.jsonb_build_object('status','membership_changed');
        ELSE
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            IF NOT FOUND THEN v_result := pg_catalog.jsonb_build_object('status','fence_missing');
            ELSIF v_counter='generation' AND v_fence.generation_high_water=9223372036854775807 OR v_counter='control_epoch' AND v_fence.control_epoch_high_water=9223372036854775807 THEN
                v_result := pg_catalog.jsonb_build_object('status','high_water_exhausted');
            ELSIF v_counter='generation' THEN
                UPDATE public.gpu_backend_fences f SET generation_high_water=f.generation_high_water+1,
                    token_expiry_high_water=CASE WHEN v_token_expiry IS NULL THEN f.token_expiry_high_water ELSE GREATEST(COALESCE(f.token_expiry_high_water,v_token_expiry),v_token_expiry) END,
                    updated_at=v_now WHERE f.backend_registry_id=v_id RETURNING f.generation_high_water INTO v_next;
                v_result := pg_catalog.jsonb_build_object('status','advanced','value',v_next);
            ELSE
                UPDATE public.gpu_backend_fences f SET control_epoch_high_water=f.control_epoch_high_water+1,
                    token_expiry_high_water=CASE WHEN v_token_expiry IS NULL THEN f.token_expiry_high_water ELSE GREATEST(COALESCE(f.token_expiry_high_water,v_token_expiry),v_token_expiry) END,
                    updated_at=v_now WHERE f.backend_registry_id=v_id RETURNING f.control_epoch_high_water INTO v_next;
                v_result := pg_catalog.jsonb_build_object('status','advanced','value',v_next);
            END IF;
        END IF;

    ELSIF p_action = 'fence_token_expiry' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args, ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','token_expires_at']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_id:=yoao_inference_gpu_v1._uuid(v_args->'backend_registry_id'); v_resource:=yoao_inference_gpu_v1._resource(v_args->'gpu_resource_id');
        v_next:=yoao_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807); v_time:=yoao_inference_gpu_v1._timestamp(v_args->'token_expires_at');
        PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND OR v_membership.state <> 'active' OR v_membership.membership_epoch<>v_next THEN v_result:=pg_catalog.jsonb_build_object('status','membership_changed');
        ELSE
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            IF NOT FOUND THEN v_result:=pg_catalog.jsonb_build_object('status','fence_missing');
            ELSE UPDATE public.gpu_backend_fences f SET token_expiry_high_water=GREATEST(COALESCE(f.token_expiry_high_water,v_time),v_time),updated_at=v_now WHERE f.backend_registry_id=v_id;
                v_result:=pg_catalog.jsonb_build_object('status','recorded','token_expires_at',yoao_inference_gpu_v1._iso(v_time)); END IF;
        END IF;

    ELSIF p_action = 'fence_activate_membership' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args, ARRAY['backend_registry_id','gpu_resource_id','membership_epoch']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_id:=yoao_inference_gpu_v1._uuid(v_args->'backend_registry_id'); v_resource:=yoao_inference_gpu_v1._resource(v_args->'gpu_resource_id');
        v_next:=yoao_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807);
        PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND OR v_membership.membership_epoch<>v_next OR v_membership.state NOT IN ('pending','active') THEN v_result:=pg_catalog.jsonb_build_object('status','membership_changed');
        ELSE SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            IF NOT FOUND THEN v_result:=pg_catalog.jsonb_build_object('status','fence_missing');
            ELSIF v_fence.runtime_epoch_high_water=9223372036854775807 THEN v_result:=pg_catalog.jsonb_build_object('status','high_water_exhausted');
            ELSE UPDATE public.gpu_backend_fences f SET runtime_epoch_high_water=f.runtime_epoch_high_water+1,updated_at=v_now WHERE f.backend_registry_id=v_id RETURNING f.runtime_epoch_high_water INTO v_next;
                IF v_membership.state='pending' THEN UPDATE public.gpu_backend_memberships m SET state='active',updated_at=v_now WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource; END IF;
                v_result:=pg_catalog.jsonb_build_object('status','activated','runtime_epoch_high_water',v_next); END IF;
        END IF;

    ELSIF p_action = 'fence_read' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args, ARRAY['backend_registry_id']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_id:=yoao_inference_gpu_v1._uuid(v_args->'backend_registry_id');
        v_result:=pg_catalog.jsonb_build_object('fence',yoao_inference_gpu_v1._fence_read_doc(v_id));

    ELSIF p_action = 'rollout_read' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args, ARRAY['resource_id']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_resource:=yoao_inference_gpu_v1._resource(v_args->'resource_id');
        v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource));

    ELSIF p_action = 'rollout_read_all' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args, ARRAY[]::text[]) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(yoao_inference_gpu_v1._rollout_snapshot(r.gpu_resource_id) ORDER BY r.gpu_resource_id),'[]'::jsonb)
          INTO v_memberships FROM public.gpu_arbiter_rollouts r;
        v_result:=pg_catalog.jsonb_build_object('snapshots',v_memberships);

    ELSIF p_action = 'rollout_boundary' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args, ARRAY[]::text[]) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        SELECT EXISTS(SELECT 1 FROM public.gpu_arbiter_rollouts r WHERE r.state <> 'off') INTO v_wait;
        v_result:=pg_catalog.jsonb_build_object('active',v_wait);

    ELSIF p_action = 'rollout_begin' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args, ARRAY['resource_id','target_mode','candidate_transition_id']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_resource:=yoao_inference_gpu_v1._resource(v_args->'resource_id'); v_target:=yoao_inference_gpu_v1._string(v_args->'target_mode',16);
        v_transition:=yoao_inference_gpu_v1._uuid(v_args->'candidate_transition_id');
        IF v_target NOT IN ('off','observe','enforce') THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('aap:gpu-resource:'||v_resource,0));
        SELECT * INTO v_rollout FROM public.gpu_arbiter_rollouts r WHERE r.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND THEN
            IF v_target='enforce' THEN
                INSERT INTO public.gpu_arbiter_rollouts(gpu_resource_id,state,effective_mode,target_mode,revision,created_at,updated_at)
                VALUES(v_resource,'off','off','off',1,v_now,v_now);
                UPDATE public.gpu_arbiter_rollouts SET state='promoting',target_mode='enforce',transition_id=v_transition,transition_started_at=v_now,blocker_reason=NULL,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            ELSE
                INSERT INTO public.gpu_arbiter_rollouts(gpu_resource_id,state,effective_mode,target_mode,revision,created_at,updated_at)
                VALUES(v_resource,'off','off',v_target,1,v_now,v_now);
            END IF;
            v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_rollout.state IN ('promoting','demoting') THEN
            IF v_rollout.target_mode=v_target THEN v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource));
            ELSE v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_in_progress'); END IF;
        ELSIF v_target='enforce' AND v_rollout.state='enforcing' THEN
            v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_target IN ('off','observe') AND v_rollout.state='off' THEN
            IF v_rollout.target_mode<>v_target THEN UPDATE public.gpu_arbiter_rollouts SET target_mode=v_target,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource; END IF;
            v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_transition=v_rollout.last_transition_id THEN
            v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_id_reused');
        ELSIF v_target='enforce' AND v_rollout.effective_mode='off' THEN
            UPDATE public.gpu_arbiter_rollouts SET state='promoting',target_mode='enforce',transition_id=v_transition,transition_started_at=v_now,blocker_reason=NULL,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_target IN ('off','observe') AND v_rollout.effective_mode='enforce' THEN
            UPDATE public.gpu_arbiter_rollouts SET state='demoting',target_mode=v_target,transition_id=v_transition,transition_started_at=v_now,blocker_reason=NULL,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSE v_result:=pg_catalog.jsonb_build_object('outcome','unavailable'); END IF;

    ELSIF p_action = 'rollout_complete' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args, ARRAY['resource_id','transition_id']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_resource:=yoao_inference_gpu_v1._resource(v_args->'resource_id'); v_transition:=yoao_inference_gpu_v1._uuid(v_args->'transition_id');
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('aap:gpu-resource:'||v_resource,0));
        SELECT * INTO v_rollout FROM public.gpu_arbiter_rollouts r WHERE r.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND THEN v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_missing');
        ELSIF v_rollout.transition_id IS NULL AND v_rollout.last_transition_id=v_transition THEN v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_rollout.transition_id<>v_transition THEN v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_changed');
        ELSIF v_rollout.state='promoting' THEN
            UPDATE public.gpu_arbiter_rollouts SET state='enforcing',effective_mode='enforce',transition_id=NULL,transition_started_at=NULL,last_transition_id=v_transition,blocker_reason=NULL,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSIF v_rollout.state='demoting' THEN
            UPDATE public.gpu_arbiter_rollouts SET state='off',effective_mode='off',transition_id=NULL,transition_started_at=NULL,last_transition_id=v_transition,blocker_reason=NULL,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSE v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_not_completable'); END IF;

    ELSIF p_action = 'rollout_block' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args, ARRAY['resource_id','transition_id','reason']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_resource:=yoao_inference_gpu_v1._resource(v_args->'resource_id'); v_transition:=yoao_inference_gpu_v1._uuid(v_args->'transition_id'); v_reason:=yoao_inference_gpu_v1._string(v_args->'reason',256);
        PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('aap:gpu-resource:'||v_resource,0));
        SELECT * INTO v_rollout FROM public.gpu_arbiter_rollouts r WHERE r.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND OR v_rollout.transition_id<>v_transition THEN v_result:=pg_catalog.jsonb_build_object('outcome','conflict','reason','transition_changed');
        ELSIF v_rollout.state='blocked' AND v_rollout.blocker_reason=v_reason THEN v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource));
        ELSE UPDATE public.gpu_arbiter_rollouts SET state='blocked',blocker_reason=v_reason,revision=revision+1,updated_at=v_now WHERE gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('snapshot',yoao_inference_gpu_v1._rollout_snapshot(v_resource)); END IF;

    ELSIF p_action IN ('runtime_subject','cold_runtime_subject','idle_eviction_subject','busy_eviction_subject') THEN
        IF p_action='runtime_subject' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id','gpu_resource_id']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSIF p_action='cold_runtime_subject' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id','gpu_resource_id','expected_challenge']) OR (v_args->'expected_challenge'<>'null'::jsonb AND pg_catalog.jsonb_typeof(v_args->'expected_challenge')<>'string') THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSE
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id','gpu_resource_id','expected_generation','challenge']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        END IF;
        v_id:=yoao_inference_gpu_v1._uuid(v_args->'backend_registry_id'); v_resource:=yoao_inference_gpu_v1._resource(v_args->'gpu_resource_id');
        IF p_action IN ('idle_eviction_subject','busy_eviction_subject') THEN
            v_expected:=v_args->'expected_generation';
            IF pg_catalog.jsonb_typeof(v_expected)<>'string' OR v_expected#>>'{}' !~ '^[1-9][0-9]{0,18}$' OR (v_expected#>>'{}')::numeric>9223372036854775807 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            v_challenge:=yoao_inference_gpu_v1._string(v_args->'challenge',64);
            IF v_challenge !~ '^[0-9a-f]{64}$' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSE v_expected:=v_args->'expected_challenge'; IF v_expected<>'null'::jsonb THEN v_challenge:=yoao_inference_gpu_v1._string(v_expected,64); IF v_challenge !~ '^[0-9a-f]{64}$' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF; END IF; END IF;
        IF NOT yoao_inference_gpu_v1._lock_resource(v_resource,true) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        IF NOT FOUND OR v_membership.state<>'active' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','membership_not_active');
        ELSE
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            IF p_action IN ('runtime_subject','cold_runtime_subject') THEN
                BEGIN
                    SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id FOR SHARE NOWAIT;
                EXCEPTION WHEN lock_not_available THEN
                    RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','registry_busy'));
                END;
                IF v_registry.id IS NOT NULL AND v_registry.extra_params ? 'plugin'
                   AND (v_registry.extra_params #>> '{plugin,status}' IS DISTINCT FROM 'active'
                        OR v_registry.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM v_registry.gpu_resource_id) THEN
                    RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','plugin_not_active'));
                END IF;
            ELSE
                SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
            END IF;
            IF NOT FOUND OR v_registry.id IS NULL OR v_fence.backend_registry_id IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_missing');
            ELSE
                v_health:=v_registry.health_meta; v_probe:=v_health->'gpu_arbiter_probe'; v_residency:=v_health->'residency'; v_now:=pg_catalog.transaction_timestamp();
                v_reason:=NULL;
                IF v_registry.state<>'connected' OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.vram_budget_mb IS DISTINCT FROM v_membership.vram_budget_mb OR v_registry.eviction_priority IS DISTINCT FROM v_membership.eviction_priority OR yoao_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency THEN v_reason:='registry_claim_mismatch';
                ELSIF v_fence.runtime_epoch_high_water<=v_membership.runtime_epoch_baseline THEN v_reason:='active_runtime_epoch_invalid';
                ELSIF v_fence.token_expiry_high_water IS NULL THEN v_reason:='token_horizon_missing';
                ELSIF v_registry.last_checked_at IS NULL OR v_registry.last_checked_at>v_now OR v_registry.last_checked_at<v_now-interval '5 minutes' THEN v_reason:='probe_expired';
                ELSIF pg_catalog.jsonb_typeof(v_probe)<>'object' OR pg_catalog.jsonb_typeof(v_residency)<>'object' THEN v_reason:='health_unavailable';
                ELSIF v_probe->>'backend_registry_id'<>v_id::text OR v_probe->>'gpu_resource_id'<>v_resource OR v_probe->>'membership_epoch'<>v_membership.membership_epoch::text OR v_probe->>'membership_state'<>'active' OR v_probe->>'observed_at' IS NULL THEN v_reason:='probe_membership_mismatch';
                ELSIF (v_registry.last_checked_at IS DISTINCT FROM (v_probe->>'observed_at')::timestamptz) THEN v_reason:='probe_registry_clock_mismatch';
                ELSIF v_residency->>'evictable'='true' AND (v_residency->>'generation' IS NULL OR v_residency->'identity' IS NULL OR v_residency->'identity'='null'::jsonb OR v_residency->>'lifecycle_gate'<>'enforce') THEN v_reason:='residency_evictable_inconsistent';
                ELSIF v_residency->>'boot_id' IS NULL OR v_residency->>'control_epoch' IS NULL OR (v_residency->>'lifecycle_gate'<>'enforce' AND p_action IN ('idle_eviction_subject','busy_eviction_subject')) THEN v_reason:='runtime_not_ready';
                ELSIF p_action='runtime_subject' AND (v_residency->>'lifecycle_gate'<>'enforce' OR v_residency->>'state'<>'resident' OR v_residency->>'gpu_loaded'<>'true' OR v_residency->>'draining'<>'false' OR v_residency->>'generation' IS NULL) THEN v_reason:='resident_runtime_not_ready';
                ELSIF p_action='runtime_subject' AND (v_residency->>'generation')::bigint>v_fence.generation_high_water THEN v_reason:='residency_generation_ahead';
                ELSIF p_action='runtime_subject' AND (v_residency->>'control_epoch')::bigint<>v_fence.control_epoch_high_water THEN v_reason:='residency_control_epoch_mismatch';
                ELSIF p_action='cold_runtime_subject' AND (v_residency->>'lifecycle_gate'<>'enforce' OR v_residency->>'gpu_loaded'<>'false' OR v_residency->>'state' NOT IN ('unloaded','resident') OR v_residency->>'draining'<>'false' OR v_residency->>'evictable'<>'false' OR COALESCE((v_residency->>'active_requests')::bigint,1)<>0 OR COALESCE((v_residency->>'builders')::bigint,1)<>0 OR COALESCE((v_residency->>'borrowers')::bigint,1)<>0) THEN v_reason:='cold_runtime_not_ready';
                ELSIF p_action='cold_runtime_subject' AND (v_residency->>'generation' IS NOT NULL AND (v_residency->>'generation')::bigint>v_fence.generation_high_water) THEN v_reason:='residency_generation_ahead';
                ELSIF p_action='cold_runtime_subject' AND (v_residency->>'control_epoch')::bigint<>v_fence.control_epoch_high_water THEN v_reason:='residency_control_epoch_mismatch';
                ELSIF p_action IN ('idle_eviction_subject','busy_eviction_subject') AND v_probe->>'challenge' IS DISTINCT FROM v_challenge THEN v_reason:=CASE WHEN p_action='idle_eviction_subject' THEN 'idle_eviction_challenge_mismatch' ELSE 'busy_eviction_challenge_mismatch' END;
                ELSIF p_action IN ('idle_eviction_subject','busy_eviction_subject') AND v_residency->>'generation' IS NOT NULL AND v_residency->>'generation' IS DISTINCT FROM v_expected#>>'{}' THEN v_reason:=CASE WHEN p_action='idle_eviction_subject' THEN 'idle_eviction_generation_mismatch' ELSE 'busy_eviction_generation_mismatch' END;
                ELSIF p_action IN ('idle_eviction_subject','busy_eviction_subject') AND (v_residency->>'state'<>'resident' OR v_residency->>'gpu_loaded'<>'true' OR v_residency->>'evictable'<>'true' OR (v_residency->>'control_epoch')::bigint<>v_fence.control_epoch_high_water OR (v_residency->>'generation')::bigint>v_fence.generation_high_water OR (p_action='idle_eviction_subject' AND (COALESCE((v_residency->>'active_requests')::bigint,1)<>0 OR COALESCE((v_residency->>'builders')::bigint,1)<>0 OR COALESCE((v_residency->>'borrowers')::bigint,1)<>0))) THEN v_reason:=CASE WHEN p_action='idle_eviction_subject' THEN 'idle_eviction_runtime_not_ready' ELSE 'busy_eviction_runtime_not_ready' END;
                ELSIF p_action='cold_runtime_subject' AND v_expected<>'null'::jsonb AND v_probe->>'challenge' IS DISTINCT FROM v_challenge THEN v_reason:='cold_runtime_challenge_mismatch';
                END IF;
                IF v_reason IS NOT NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason',v_reason);
                ELSE
                    v_budget:=v_membership.vram_budget_mb; v_priority:=v_membership.eviction_priority; v_concurrency:=v_membership.max_concurrency;
                    v_boot:=v_residency->>'boot_id'; v_control:=v_fence.control_epoch_high_water; v_runtime:=v_fence.runtime_epoch_high_water;
                    SELECT COALESCE(pg_catalog.jsonb_agg(k ORDER BY k),'[]'::jsonb) INTO v_pools FROM pg_catalog.jsonb_object_keys(v_residency->'pools') AS p(k);
                    IF p_action='runtime_subject' THEN
                        v_subject:=pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'budget_mb',v_budget,'eviction_priority',v_priority,'max_concurrency',v_concurrency,'boot_id',v_boot,'generation',v_residency->>'generation','control_epoch',v_residency->>'control_epoch','runtime_epoch',v_runtime::text,'db_now',yoao_inference_gpu_v1._iso(v_now));
                    ELSIF p_action='cold_runtime_subject' THEN
                        v_subject:=pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'budget_mb',v_budget,'eviction_priority',v_priority,'max_concurrency',v_concurrency,'boot_id',v_boot,'observed_generation',v_residency->>'generation','generation_high_water',v_fence.generation_high_water,'control_epoch',v_residency->>'control_epoch','runtime_epoch',v_runtime::text,'db_now',yoao_inference_gpu_v1._iso(v_now));
                    ELSE
                        v_subject:=pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'budget_mb',v_budget,'eviction_priority',v_priority,'max_concurrency',v_concurrency,'boot_id',v_boot,'generation',v_residency->>'generation','generation_high_water',v_fence.generation_high_water,'pool_ids',v_pools,'control_epoch',v_residency->>'control_epoch','runtime_epoch',v_runtime::text,'challenge',v_challenge,'require_idle',p_action='idle_eviction_subject','db_now',yoao_inference_gpu_v1._iso(v_now));
                    END IF;
                    v_proof:=pg_catalog.jsonb_build_object('membership',yoao_inference_gpu_v1._membership_doc(v_id,v_resource),'fence',yoao_inference_gpu_v1._fence_doc(v_id),'registry',yoao_inference_gpu_v1._registry_doc(v_id),'db_now',yoao_inference_gpu_v1._iso(v_now));
                    v_result:=pg_catalog.jsonb_build_object('status','ready','subject',v_subject,'proof',v_proof);
                END IF;
            END IF;
        END IF;

    ELSIF p_action IN ('proof_domain','reconcile_resource','repair_resource','proof_reset_from_health') THEN
        IF p_action='proof_domain' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id','wait_for_lock']) OR pg_catalog.jsonb_typeof(v_args->'wait_for_lock')<>'boolean' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSIF p_action='reconcile_resource' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id','allocatable_mb']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            PERFORM yoao_inference_gpu_v1._int(v_args->'allocatable_mb',1,2147483647);
        ELSIF p_action='repair_resource' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id','allocatable_mb','force_proof_reset','readiness_blocker']) OR pg_catalog.jsonb_typeof(v_args->'force_proof_reset')<>'boolean' OR (v_args->'readiness_blocker'<>'null'::jsonb AND pg_catalog.jsonb_typeof(v_args->'readiness_blocker')<>'string') THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            PERFORM yoao_inference_gpu_v1._int(v_args->'allocatable_mb',1,2147483647);
            IF v_args->'readiness_blocker'<>'null'::jsonb THEN PERFORM yoao_inference_gpu_v1._string(v_args->'readiness_blocker',256); END IF;
        ELSE
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id','expected_backend_memberships','wait_for_lock']) OR pg_catalog.jsonb_typeof(v_args->'expected_backend_memberships')<>'array' OR pg_catalog.jsonb_typeof(v_args->'wait_for_lock')<>'boolean' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            v_previous_id:=NULL;
            FOR v_expected_item IN SELECT value FROM pg_catalog.jsonb_array_elements(v_args->'expected_backend_memberships') LOOP
                IF NOT yoao_inference_gpu_v1._exact_keys(v_expected_item,ARRAY['backend_id','membership_epoch','state']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
                v_id:=yoao_inference_gpu_v1._uuid(v_expected_item->'backend_id');
                IF v_previous_id IS NOT NULL AND v_id<=v_previous_id THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
                v_previous_id:=v_id;
                v_text:=yoao_inference_gpu_v1._string(v_expected_item->'membership_epoch',19);
                IF v_text !~ '^[1-9][0-9]{0,18}$' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
                IF v_text::numeric>9223372036854775807 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
                v_state:=yoao_inference_gpu_v1._string(v_expected_item->'state',16);
                IF v_state NOT IN ('pending','active','retiring') THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            END LOOP;
        END IF;
        v_resource:=yoao_inference_gpu_v1._resource(v_args->'resource_id');
        v_wait:=CASE WHEN p_action='proof_domain' OR p_action='proof_reset_from_health' THEN (v_args->>'wait_for_lock')::boolean ELSE true END;
        v_result:=yoao_inference_gpu_v1._domain(v_resource,true,v_wait);
        IF p_action='proof_reset_from_health' AND v_result->>'status'='ok' THEN
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('backend_id',m.backend_registry_id,'membership_epoch',m.membership_epoch::text,'state',m.state) ORDER BY m.backend_registry_id),'[]'::jsonb)
              INTO v_memberships FROM public.gpu_backend_memberships m WHERE m.gpu_resource_id=v_resource;
            IF v_memberships IS DISTINCT FROM v_args->'expected_backend_memberships' THEN v_result:=pg_catalog.jsonb_set(v_result,'{status}','"domain_changed"'::jsonb); ELSE v_result:=pg_catalog.jsonb_set(v_result,'{status}','"matched"'::jsonb); END IF;
        END IF;

    ELSIF p_action = 'resource_runtime_observation' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_resource:=yoao_inference_gpu_v1._resource(v_args->'resource_id');
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('backend_id',m.backend_registry_id,'membership_epoch',m.membership_epoch,'state',m.state) ORDER BY m.backend_registry_id),'[]'::jsonb)
          INTO v_memberships FROM public.gpu_backend_memberships m WHERE m.gpu_resource_id=v_resource;
        v_result:=pg_catalog.jsonb_build_object('status','ok','memberships',v_memberships);

    ELSIF p_action = 'membership_candidates' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['gpu_resource_id','states']) OR pg_catalog.jsonb_typeof(v_args->'states')<>'array' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_resource:=yoao_inference_gpu_v1._resource(v_args->'gpu_resource_id');
        IF v_args->'states' NOT IN ('["pending"]'::jsonb,'["pending","active"]'::jsonb,'["retiring"]'::jsonb) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('backend_id',m.backend_registry_id,'membership_epoch',m.membership_epoch,'state',m.state) ORDER BY m.backend_registry_id),'[]'::jsonb)
          INTO v_memberships FROM public.gpu_backend_memberships m WHERE m.gpu_resource_id=v_resource AND m.state=ANY(ARRAY(SELECT jsonb_array_elements_text(v_args->'states')));
        v_result:=pg_catalog.jsonb_build_object('candidates',v_memberships);

    ELSIF p_action = 'membership_max_concurrency' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_id:=yoao_inference_gpu_v1._uuid(v_args->'backend_registry_id');
        SELECT m.max_concurrency INTO v_concurrency FROM public.gpu_backend_memberships m
         WHERE m.backend_registry_id=v_id AND m.state IN ('pending','active');
        v_result:=pg_catalog.jsonb_build_object('max_concurrency',v_concurrency);

    ELSIF p_action = 'shadow_snapshot' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['resource_id','backend_id'])
           OR (v_args->'resource_id'<>'null'::jsonb AND pg_catalog.jsonb_typeof(v_args->'resource_id')<>'string')
           OR (v_args->'backend_id'<>'null'::jsonb AND pg_catalog.jsonb_typeof(v_args->'backend_id')<>'string')
           OR (v_args->'resource_id'<>'null'::jsonb AND v_args->'backend_id'<>'null'::jsonb) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        IF v_args->'resource_id'='null'::jsonb THEN v_resource:=NULL; ELSE v_resource:=yoao_inference_gpu_v1._resource(v_args->'resource_id'); END IF;
        IF v_args->'backend_id'='null'::jsonb THEN v_id:=NULL; ELSE v_id:=yoao_inference_gpu_v1._uuid(v_args->'backend_id'); END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object('backend_id',r.id,'gpu_resource_id',r.gpu_resource_id,'vram_budget_mb',r.vram_budget_mb,'eviction_priority',r.eviction_priority,'extra_params',r.extra_params,'state',r.state,'last_checked_at',yoao_inference_gpu_v1._iso(r.last_checked_at),'health_meta',r.health_meta) ORDER BY r.id),'[]'::jsonb)
          INTO v_memberships FROM public.ml_backend_registry r WHERE (v_resource IS NULL OR r.gpu_resource_id=v_resource) AND (v_id IS NULL OR r.id=v_id);
        v_result:=pg_catalog.jsonb_build_object('backends',v_memberships);

    ELSIF p_action = 'prepare_cold_generation' OR p_action='prepare_idle_eviction_generation' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','token_expires_at']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_expected:=v_args->'expected_subject'; v_time:=yoao_inference_gpu_v1._timestamp(v_args->'token_expires_at');
        IF p_action='prepare_cold_generation' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_expected,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','observed_generation','generation_high_water','control_epoch','runtime_epoch','db_now']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSE
            IF NOT yoao_inference_gpu_v1._exact_keys(v_expected,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','generation','generation_high_water','pool_ids','control_epoch','runtime_epoch','challenge','require_idle','db_now']) OR pg_catalog.jsonb_typeof(v_expected->'pool_ids')<>'array' OR pg_catalog.jsonb_typeof(v_expected->'require_idle')<>'boolean' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        END IF;
        v_id:=yoao_inference_gpu_v1._uuid(v_expected->'backend_registry_id');
        v_resource:=yoao_inference_gpu_v1._resource(v_expected->'gpu_resource_id');
        v_next:=yoao_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807);
        PERFORM yoao_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647);
        PERFORM yoao_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647);
        PERFORM yoao_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000);
        v_boot:=yoao_inference_gpu_v1._string(v_expected->'boot_id',128);
        PERFORM yoao_inference_gpu_v1._timestamp(v_expected->'db_now');
        IF p_action='prepare_cold_generation' THEN
            PERFORM yoao_inference_gpu_v1._int(v_expected->'generation_high_water',0,9223372036854775807);
            v_control:=yoao_inference_gpu_v1._string(v_expected->'control_epoch',19)::bigint;
            v_runtime:=yoao_inference_gpu_v1._string(v_expected->'runtime_epoch',19)::bigint;
            IF v_expected->>'control_epoch' !~ '^[1-9][0-9]{0,18}$' OR v_expected->>'runtime_epoch' !~ '^[1-9][0-9]{0,18}$' OR v_control>9223372036854775807 OR v_runtime>9223372036854775807 OR (v_expected->'observed_generation'<>'null'::jsonb AND (pg_catalog.jsonb_typeof(v_expected->'observed_generation')<>'string' OR v_expected->>'observed_generation' !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'observed_generation')::numeric>9223372036854775807)) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSE
            PERFORM yoao_inference_gpu_v1._int(v_expected->'generation_high_water',0,9223372036854775807);
            IF yoao_inference_gpu_v1._string(v_expected->'generation',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'generation')::numeric>9223372036854775807 OR yoao_inference_gpu_v1._string(v_expected->'control_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'control_epoch')::numeric>9223372036854775807 OR yoao_inference_gpu_v1._string(v_expected->'runtime_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'runtime_epoch')::numeric>9223372036854775807 OR yoao_inference_gpu_v1._string(v_expected->'challenge',64) !~ '^[0-9a-f]{64}$' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            IF EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value) WHERE pg_catalog.jsonb_typeof(x.value)<>'string' OR x.value#>>'{}'='' OR x.value#>>'{}'<>pg_catalog.btrim(x.value#>>'{}')) OR v_expected->'pool_ids' IS DISTINCT FROM (SELECT COALESCE(pg_catalog.jsonb_agg(x.value#>>'{}' ORDER BY x.value#>>'{}'),'[]'::jsonb) FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value)) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        END IF;
        v_id:=yoao_inference_gpu_v1._uuid(v_expected->'backend_registry_id'); v_resource:=yoao_inference_gpu_v1._resource(v_expected->'gpu_resource_id');
        PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        IF p_action='prepare_cold_generation' THEN
            BEGIN
                SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id FOR SHARE NOWAIT;
            EXCEPTION WHEN lock_not_available THEN
                RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','registry_busy'));
            END;
            IF v_registry.id IS NOT NULL AND v_registry.extra_params ? 'plugin'
               AND (v_registry.extra_params #>> '{plugin,status}' IS DISTINCT FROM 'active'
                    OR v_registry.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM v_registry.gpu_resource_id) THEN
                RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','plugin_not_active'));
            END IF;
        ELSE
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
        END IF;
        v_now:=pg_catalog.transaction_timestamp();
        IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'active' OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_missing');
        ELSE
            v_health:=v_registry.health_meta; v_residency:=v_health->'residency';
            IF v_time<=v_now OR v_fence.generation_high_water=9223372036854775807 OR v_fence.generation_high_water<>yoao_inference_gpu_v1._int(v_expected->'generation_high_water',0,9223372036854775807) OR v_fence.control_epoch_high_water::text<>v_expected->>'control_epoch' OR v_fence.runtime_epoch_high_water::text<>v_expected->>'runtime_epoch' OR v_membership.membership_epoch<>yoao_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807) OR v_membership.vram_budget_mb<>yoao_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647) OR v_membership.eviction_priority<>yoao_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647) OR v_membership.max_concurrency<>yoao_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000) OR v_residency->>'boot_id' IS DISTINCT FROM v_expected->>'boot_id' OR v_registry.state<>'connected' OR v_registry.gpu_resource_id<>v_resource OR v_registry.vram_budget_mb<>v_membership.vram_budget_mb OR v_registry.eviction_priority<>v_membership.eviction_priority OR yoao_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency OR pg_catalog.jsonb_typeof(v_residency)<>'object' OR v_registry.last_checked_at IS NULL OR v_registry.last_checked_at>v_now OR v_registry.last_checked_at<v_now-interval '3 minutes' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_changed');
            ELSIF p_action='prepare_cold_generation' AND v_expected->'observed_generation' IS DISTINCT FROM COALESCE(v_residency->'generation','null'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_changed');
            ELSIF p_action='prepare_idle_eviction_generation' AND (v_expected->>'generation' IS DISTINCT FROM v_residency->>'generation' OR v_expected->'pool_ids' IS DISTINCT FROM (SELECT COALESCE(pg_catalog.jsonb_agg(k ORDER BY k),'[]'::jsonb) FROM pg_catalog.jsonb_object_keys(v_residency->'pools') p(k)) OR v_expected->>'control_epoch' IS DISTINCT FROM v_residency->>'control_epoch') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_changed');
            ELSE
                v_generation:=v_fence.generation_high_water+1;
                UPDATE public.gpu_backend_fences f SET generation_high_water=v_generation,token_expiry_high_water=GREATEST(COALESCE(f.token_expiry_high_water,v_time),v_time),updated_at=v_now WHERE f.backend_registry_id=v_id;
                IF p_action='prepare_cold_generation' THEN
                    v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'budget_mb',v_membership.vram_budget_mb,'eviction_priority',v_membership.eviction_priority,'max_concurrency',v_membership.max_concurrency,'boot_id',v_residency->>'boot_id','observed_generation',v_residency->>'generation','generation',v_generation::text,'control_epoch',v_residency->>'control_epoch','runtime_epoch',v_fence.runtime_epoch_high_water::text,'token_expires_at',yoao_inference_gpu_v1._iso(v_time),'db_now',yoao_inference_gpu_v1._iso(v_now)));
                ELSE
                    v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'budget_mb',v_membership.vram_budget_mb,'eviction_priority',v_membership.eviction_priority,'max_concurrency',v_membership.max_concurrency,'boot_id',v_residency->>'boot_id','source_generation',v_residency->>'generation','generation',v_generation::text,'pool_ids',v_expected->'pool_ids','control_epoch',v_residency->>'control_epoch','runtime_epoch',v_fence.runtime_epoch_high_water::text,'token_expires_at',yoao_inference_gpu_v1._iso(v_time),'require_idle',v_expected->'require_idle','db_now',yoao_inference_gpu_v1._iso(v_now)));
                END IF;
            END IF;
        END IF;

    ELSIF p_action='record_runtime_token_expiry' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','token_expires_at']) OR NOT yoao_inference_gpu_v1._exact_keys(v_args->'expected_subject',ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','generation','control_epoch','runtime_epoch','db_now']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_expected:=v_args->'expected_subject'; v_id:=yoao_inference_gpu_v1._uuid(v_expected->'backend_registry_id'); v_resource:=yoao_inference_gpu_v1._resource(v_expected->'gpu_resource_id');
        v_next:=yoao_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807); v_budget:=yoao_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647); v_priority:=yoao_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647); v_concurrency:=yoao_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000); v_boot:=yoao_inference_gpu_v1._string(v_expected->'boot_id',128); v_time:=yoao_inference_gpu_v1._timestamp(v_args->'token_expires_at'); PERFORM yoao_inference_gpu_v1._timestamp(v_expected->'db_now');
        v_text:=yoao_inference_gpu_v1._string(v_expected->'generation',19); IF v_text !~ '^[1-9][0-9]{0,18}$' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF; IF v_text::numeric>9223372036854775807 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF; v_generation:=v_text::bigint;
        v_text:=yoao_inference_gpu_v1._string(v_expected->'control_epoch',19); IF v_text !~ '^[1-9][0-9]{0,18}$' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF; IF v_text::numeric>9223372036854775807 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF; v_control:=v_text::bigint;
        v_text:=yoao_inference_gpu_v1._string(v_expected->'runtime_epoch',19); IF v_text !~ '^[1-9][0-9]{0,18}$' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF; IF v_text::numeric>9223372036854775807 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF; v_runtime:=v_text::bigint;
        PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        BEGIN
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available THEN
            RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','registry_busy'));
        END;
        IF v_registry.id IS NOT NULL AND v_registry.extra_params ? 'plugin'
           AND (v_registry.extra_params #>> '{plugin,status}' IS DISTINCT FROM 'active'
                OR v_registry.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM v_registry.gpu_resource_id) THEN
            RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','plugin_not_active'));
        END IF;
        v_residency:=v_registry.health_meta->'residency'; v_now:=pg_catalog.transaction_timestamp();
        IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'active' OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL OR v_time<=v_now OR v_membership.membership_epoch<>v_next OR v_membership.vram_budget_mb<>v_budget OR v_membership.eviction_priority<>v_priority OR v_membership.max_concurrency<>v_concurrency OR v_registry.state<>'connected' OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.vram_budget_mb IS DISTINCT FROM v_budget OR v_registry.eviction_priority IS DISTINCT FROM v_priority OR yoao_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_concurrency OR v_fence.generation_high_water<v_generation OR v_fence.control_epoch_high_water<>v_control OR v_fence.runtime_epoch_high_water<>v_runtime OR NOT yoao_inference_gpu_v1._residency_shape(v_residency) OR v_residency->>'state'<>'resident' OR v_residency->>'gpu_loaded'<>'true' OR v_residency->>'draining'<>'false' OR v_residency->>'lifecycle_gate'<>'enforce' OR v_residency->>'generation' IS DISTINCT FROM v_expected->>'generation' OR v_residency->>'control_epoch' IS DISTINCT FROM v_expected->>'control_epoch' OR v_residency->>'boot_id' IS DISTINCT FROM v_boot OR v_residency #>> '{identity,backend_registry_id}' IS DISTINCT FROM v_id::text OR v_residency #>> '{identity,gpu_resource_id}' IS DISTINCT FROM v_resource OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident'='null'::jsonb) OR NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident'='true'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_changed');
        ELSE UPDATE public.gpu_backend_fences f SET token_expiry_high_water=GREATEST(COALESCE(f.token_expiry_high_water,v_time),v_time),updated_at=v_now WHERE f.backend_registry_id=v_id;
            v_result:=pg_catalog.jsonb_build_object('status','recorded','token_expires_at',yoao_inference_gpu_v1._iso(v_time)); END IF;

    ELSIF p_action='prepare_eviction_cancel_generation' THEN
        IF v_args->>'phase'='recover' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['phase','backend_registry_id','gpu_resource_id','owner_id']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            v_id:=yoao_inference_gpu_v1._uuid(v_args->'backend_registry_id'); v_resource:=yoao_inference_gpu_v1._resource(v_args->'gpu_resource_id'); v_reason:=yoao_inference_gpu_v1._string(v_args->'owner_id',256);
            PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
            SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
            SELECT * INTO v_cancel FROM public.gpu_backend_cancel_intents c WHERE c.backend_registry_id=v_id FOR UPDATE;
            IF v_cancel.backend_registry_id IS NULL OR v_cancel.gpu_resource_id IS DISTINCT FROM v_resource OR v_membership.backend_registry_id IS NULL OR v_membership.state<>'active' OR v_membership.membership_epoch<>v_cancel.membership_epoch OR v_fence.backend_registry_id IS NULL OR v_fence.generation_high_water<>v_cancel.generation OR v_fence.control_epoch_high_water<>v_cancel.control_epoch OR v_fence.runtime_epoch_high_water<>v_cancel.runtime_epoch OR v_fence.token_expiry_high_water<v_cancel.token_expires_at OR v_registry.id IS NULL OR v_registry.state<>'connected' OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.vram_budget_mb IS DISTINCT FROM v_membership.vram_budget_mb OR v_registry.eviction_priority IS DISTINCT FROM v_membership.eviction_priority OR yoao_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency OR v_cancel.token_expires_at<=v_now THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_missing');
            ELSIF v_cancel.owner_id IS DISTINCT FROM v_reason THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_identity_changed');
            ELSIF v_cancel.subject_fingerprint IS DISTINCT FROM pg_catalog.encode(public.digest(pg_catalog.convert_to(pg_catalog.jsonb_build_object('backend_registry_id',v_cancel.backend_registry_id,'gpu_resource_id',v_cancel.gpu_resource_id,'membership_epoch',v_cancel.membership_epoch,'boot_id',v_cancel.boot_id,'control_epoch',v_cancel.control_epoch,'runtime_epoch',v_cancel.runtime_epoch,'source_generation',v_cancel.source_generation,'drain_generation',v_cancel.drain_generation,'generation',v_cancel.generation,'owner_id',v_cancel.owner_id,'operation',v_cancel.operation,'owner_hard_deadline_ms',v_cancel.owner_hard_deadline_ms,'drain_token_expires_at',yoao_inference_gpu_v1._iso(v_cancel.drain_token_expires_at),'token_expires_at',yoao_inference_gpu_v1._iso(v_cancel.token_expires_at),'jti',v_cancel.jti,'pool_ids',v_cancel.pool_ids,'backend_url',v_registry.url,'backend_auth_method',v_registry.auth_method,'backend_auth_token',v_registry.auth_token)::text,'UTF8'),'sha256'),'hex') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_source_changed');
            ELSE v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_cancel.membership_epoch,'budget_mb',v_membership.vram_budget_mb,'eviction_priority',v_membership.eviction_priority,'max_concurrency',v_membership.max_concurrency,'boot_id',v_cancel.boot_id,'source_generation',v_cancel.source_generation::text,'drain_generation',v_cancel.drain_generation::text,'generation',v_cancel.generation::text,'pool_ids',v_cancel.pool_ids,'control_epoch',v_cancel.control_epoch::text,'runtime_epoch',v_cancel.runtime_epoch::text,'owner_id',v_cancel.owner_id,'operation',v_cancel.operation,'owner_hard_deadline_ms',v_cancel.owner_hard_deadline_ms,'drain_token_expires_at',yoao_inference_gpu_v1._iso(v_cancel.drain_token_expires_at),'token_expires_at',yoao_inference_gpu_v1._iso(v_cancel.token_expires_at),'jti',v_cancel.jti,'idempotent',true,'db_now',yoao_inference_gpu_v1._iso(v_now))); END IF;
        ELSIF v_args->>'phase'='prepare' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['phase','expected_subject','owner_id','owner_hard_deadline_ms','token_expires_at']) OR NOT yoao_inference_gpu_v1._exact_keys(v_args->'expected_subject',ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','source_generation','generation','pool_ids','control_epoch','runtime_epoch','token_expires_at','require_idle','db_now']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            v_expected:=v_args->'expected_subject'; v_id:=yoao_inference_gpu_v1._uuid(v_expected->'backend_registry_id'); v_resource:=yoao_inference_gpu_v1._resource(v_expected->'gpu_resource_id'); v_reason:=yoao_inference_gpu_v1._string(v_args->'owner_id',256); v_next:=yoao_inference_gpu_v1._int(v_args->'owner_hard_deadline_ms',1,253402300799999); v_time:=yoao_inference_gpu_v1._timestamp(v_args->'token_expires_at'); v_token_expiry:=yoao_inference_gpu_v1._timestamp(v_expected->'token_expires_at');
            IF pg_catalog.jsonb_typeof(v_expected->'require_idle')<>'boolean' OR pg_catalog.jsonb_typeof(v_expected->'pool_ids')<>'array' OR pg_catalog.jsonb_array_length(v_expected->'pool_ids')=0 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            PERFORM yoao_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807);
            PERFORM yoao_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647);
            PERFORM yoao_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647);
            PERFORM yoao_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000);
            PERFORM yoao_inference_gpu_v1._string(v_expected->'boot_id',128);
            PERFORM yoao_inference_gpu_v1._timestamp(v_expected->'db_now');
            IF yoao_inference_gpu_v1._string(v_expected->'source_generation',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'source_generation')::numeric>9223372036854775807 OR yoao_inference_gpu_v1._string(v_expected->'generation',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'generation')::numeric>9223372036854775807 OR yoao_inference_gpu_v1._string(v_expected->'control_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'control_epoch')::numeric>9223372036854775807 OR yoao_inference_gpu_v1._string(v_expected->'runtime_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'runtime_epoch')::numeric>9223372036854775807 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            IF EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value) WHERE pg_catalog.jsonb_typeof(x.value)<>'string' OR x.value#>>'{}'='' OR x.value#>>'{}'<>pg_catalog.btrim(x.value#>>'{}')) OR v_expected->'pool_ids' IS DISTINCT FROM (SELECT COALESCE(pg_catalog.jsonb_agg(x.value#>>'{}' ORDER BY x.value#>>'{}'),'[]'::jsonb) FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value)) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            IF (v_expected->>'require_idle')::boolean THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','busy_cancel_required');
            ELSE
                PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
                SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
                SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
                SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
                SELECT * INTO v_cancel FROM public.gpu_backend_cancel_intents c WHERE c.backend_registry_id=v_id FOR UPDATE;
                v_now:=pg_catalog.transaction_timestamp();
                IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'active' OR v_membership.membership_epoch<>yoao_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807) OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL OR v_time<=v_now OR v_next<=floor(extract(epoch from v_now)*1000)::bigint OR v_time>pg_catalog.to_timestamp(v_next::double precision/1000) OR v_fence.control_epoch_high_water::text IS DISTINCT FROM v_expected->>'control_epoch' OR v_fence.runtime_epoch_high_water::text IS DISTINCT FROM v_expected->>'runtime_epoch' OR v_membership.vram_budget_mb<>yoao_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647) OR v_membership.eviction_priority<>yoao_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647) OR v_membership.max_concurrency<>yoao_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000) OR v_registry.state<>'connected' OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.vram_budget_mb IS DISTINCT FROM v_membership.vram_budget_mb OR v_registry.eviction_priority IS DISTINCT FROM v_membership.eviction_priority OR yoao_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency OR v_registry.health_meta->'residency'->>'boot_id' IS DISTINCT FROM v_expected->>'boot_id' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_conflict');
                ELSIF v_cancel.backend_registry_id IS NOT NULL THEN
                    IF v_cancel.gpu_resource_id IS DISTINCT FROM v_resource OR v_cancel.membership_epoch<>v_membership.membership_epoch OR v_cancel.boot_id IS DISTINCT FROM v_expected->>'boot_id' OR v_cancel.control_epoch::text IS DISTINCT FROM v_expected->>'control_epoch' OR v_cancel.runtime_epoch::text IS DISTINCT FROM v_expected->>'runtime_epoch' OR v_cancel.source_generation<>(v_expected->>'source_generation')::bigint OR v_cancel.drain_generation<>(v_expected->>'generation')::bigint OR v_cancel.owner_id IS DISTINCT FROM v_reason OR v_cancel.operation<>'evict' OR v_cancel.owner_hard_deadline_ms<>v_next OR v_cancel.drain_token_expires_at IS DISTINCT FROM v_token_expiry OR v_cancel.token_expires_at IS DISTINCT FROM v_time OR v_cancel.pool_ids IS DISTINCT FROM v_expected->'pool_ids' OR v_fence.generation_high_water<>v_cancel.generation OR v_fence.token_expiry_high_water IS NULL OR v_fence.token_expiry_high_water<v_cancel.token_expires_at OR v_cancel.subject_fingerprint IS DISTINCT FROM pg_catalog.encode(public.digest(pg_catalog.convert_to(pg_catalog.jsonb_build_object('backend_registry_id',v_cancel.backend_registry_id,'gpu_resource_id',v_cancel.gpu_resource_id,'membership_epoch',v_cancel.membership_epoch,'boot_id',v_cancel.boot_id,'control_epoch',v_cancel.control_epoch,'runtime_epoch',v_cancel.runtime_epoch,'source_generation',v_cancel.source_generation,'drain_generation',v_cancel.drain_generation,'generation',v_cancel.generation,'owner_id',v_cancel.owner_id,'operation',v_cancel.operation,'owner_hard_deadline_ms',v_cancel.owner_hard_deadline_ms,'drain_token_expires_at',yoao_inference_gpu_v1._iso(v_cancel.drain_token_expires_at),'token_expires_at',yoao_inference_gpu_v1._iso(v_cancel.token_expires_at),'jti',v_cancel.jti,'pool_ids',v_cancel.pool_ids,'backend_url',v_registry.url,'backend_auth_method',v_registry.auth_method,'backend_auth_token',v_registry.auth_token)::text,'UTF8'),'sha256'),'hex') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_conflict');
                    ELSE v_status:='true'; END IF;
                ELSE
                    IF v_fence.generation_high_water=9223372036854775807 OR v_fence.generation_high_water<(v_expected->>'generation')::bigint OR v_token_expiry<=v_now OR v_fence.token_expiry_high_water IS DISTINCT FROM v_token_expiry THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_token_horizon_invalid');
                    ELSE
                        v_generation:=v_fence.generation_high_water+1; v_control:=v_fence.control_epoch_high_water; v_runtime:=v_fence.runtime_epoch_high_water; v_id2:=public.gen_random_uuid();
                        INSERT INTO public.gpu_backend_cancel_intents(backend_registry_id,gpu_resource_id,membership_epoch,boot_id,control_epoch,runtime_epoch,source_generation,drain_generation,generation,owner_id,operation,owner_hard_deadline_ms,drain_token_expires_at,token_expires_at,jti,pool_ids,subject_fingerprint)
                        VALUES(v_id,v_resource,v_membership.membership_epoch,v_expected->>'boot_id',v_control,v_runtime,(v_expected->>'source_generation')::bigint,(v_expected->>'generation')::bigint,v_generation,v_reason,'evict',v_next,v_token_expiry,v_time,'transition:'||v_id2::text,v_expected->'pool_ids',pg_catalog.encode(public.digest(pg_catalog.convert_to(pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'boot_id',v_expected->>'boot_id','control_epoch',v_control,'runtime_epoch',v_runtime,'source_generation',(v_expected->>'source_generation')::bigint,'drain_generation',(v_expected->>'generation')::bigint,'generation',v_generation,'owner_id',v_reason,'operation','evict','owner_hard_deadline_ms',v_next,'drain_token_expires_at',yoao_inference_gpu_v1._iso(v_token_expiry),'token_expires_at',yoao_inference_gpu_v1._iso(v_time),'jti','transition:'||v_id2::text,'pool_ids',v_expected->'pool_ids','backend_url',v_registry.url,'backend_auth_method',v_registry.auth_method,'backend_auth_token',v_registry.auth_token)::text,'UTF8'),'sha256'),'hex'));
                        UPDATE public.gpu_backend_fences f SET generation_high_water=v_generation,token_expiry_high_water=GREATEST(COALESCE(f.token_expiry_high_water,v_time),v_time),updated_at=v_now WHERE f.backend_registry_id=v_id;
                        SELECT * INTO v_cancel FROM public.gpu_backend_cancel_intents c WHERE c.backend_registry_id=v_id;
                        v_status:='false';
                    END IF;
                END IF;
                IF v_result IS NULL THEN
                    v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',pg_catalog.jsonb_build_object('backend_registry_id',v_id,'gpu_resource_id',v_resource,'membership_epoch',v_cancel.membership_epoch,'budget_mb',v_membership.vram_budget_mb,'eviction_priority',v_membership.eviction_priority,'max_concurrency',v_membership.max_concurrency,'boot_id',v_cancel.boot_id,'source_generation',v_cancel.source_generation::text,'drain_generation',v_cancel.drain_generation::text,'generation',v_cancel.generation::text,'pool_ids',v_cancel.pool_ids,'control_epoch',v_cancel.control_epoch::text,'runtime_epoch',v_cancel.runtime_epoch::text,'owner_id',v_cancel.owner_id,'operation',v_cancel.operation,'owner_hard_deadline_ms',v_cancel.owner_hard_deadline_ms,'drain_token_expires_at',yoao_inference_gpu_v1._iso(v_cancel.drain_token_expires_at),'token_expires_at',yoao_inference_gpu_v1._iso(v_cancel.token_expires_at),'jti',v_cancel.jti,'idempotent',v_status='true','db_now',yoao_inference_gpu_v1._iso(v_now)));
                END IF;
            END IF;
        ELSE
            PERFORM yoao_inference_gpu_v1._fail();
        END IF;

    ELSIF p_action IN ('eviction_drain_health','cold_terminal_from_health','eviction_cancel_from_health','eviction_phase_from_health') THEN
        IF p_action='eviction_drain_health' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','challenge']) OR pg_catalog.jsonb_typeof(v_args->'challenge')<>'string' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSIF p_action='cold_terminal_from_health' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','challenge']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSIF p_action='eviction_phase_from_health' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','phase','challenge']) OR yoao_inference_gpu_v1._string(v_args->'phase',16) NOT IN ('drain','unload') THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSE
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['expected_subject','ack_confirmed','challenge']) OR pg_catalog.jsonb_typeof(v_args->'ack_confirmed')<>'boolean' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        END IF;
        v_expected:=v_args->'expected_subject';
        IF p_action='cold_terminal_from_health' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_expected,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','observed_generation','generation','control_epoch','runtime_epoch','token_expires_at','db_now']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSIF p_action='eviction_cancel_from_health' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_expected,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','source_generation','drain_generation','generation','pool_ids','control_epoch','runtime_epoch','owner_id','operation','owner_hard_deadline_ms','drain_token_expires_at','token_expires_at','jti','idempotent','db_now']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSE
            IF NOT yoao_inference_gpu_v1._exact_keys(v_expected,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','budget_mb','eviction_priority','max_concurrency','boot_id','source_generation','generation','pool_ids','control_epoch','runtime_epoch','token_expires_at','require_idle','db_now']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        END IF;
        PERFORM yoao_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807);
        PERFORM yoao_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647);
        PERFORM yoao_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647);
        PERFORM yoao_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000);
        PERFORM yoao_inference_gpu_v1._string(v_expected->'boot_id',128);
        PERFORM yoao_inference_gpu_v1._timestamp(v_expected->'token_expires_at');
        PERFORM yoao_inference_gpu_v1._timestamp(v_expected->'db_now');
        IF yoao_inference_gpu_v1._string(v_expected->'generation',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'generation')::numeric>9223372036854775807 OR yoao_inference_gpu_v1._string(v_expected->'control_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'control_epoch')::numeric>9223372036854775807 OR yoao_inference_gpu_v1._string(v_expected->'runtime_epoch',19) !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'runtime_epoch')::numeric>9223372036854775807 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        IF p_action='cold_terminal_from_health' THEN
            IF v_expected->'observed_generation'<>'null'::jsonb AND (pg_catalog.jsonb_typeof(v_expected->'observed_generation')<>'string' OR v_expected->>'observed_generation' !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'observed_generation')::numeric>9223372036854775807) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSE
            PERFORM yoao_inference_gpu_v1._string(v_expected->'source_generation',19);
            IF v_expected->>'source_generation' !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'source_generation')::numeric>9223372036854775807 OR pg_catalog.jsonb_typeof(v_expected->'pool_ids')<>'array' OR pg_catalog.jsonb_array_length(v_expected->'pool_ids')=0 OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value) WHERE pg_catalog.jsonb_typeof(x.value)<>'string' OR x.value#>>'{}'='' OR x.value#>>'{}'<>pg_catalog.btrim(x.value#>>'{}')) OR v_expected->'pool_ids' IS DISTINCT FROM (SELECT COALESCE(pg_catalog.jsonb_agg(x.value#>>'{}' ORDER BY x.value#>>'{}'),'[]'::jsonb) FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value)) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            IF p_action='eviction_cancel_from_health' THEN
                PERFORM yoao_inference_gpu_v1._string(v_expected->'drain_generation',19);
                PERFORM yoao_inference_gpu_v1._string(v_expected->'owner_id',256);
                PERFORM yoao_inference_gpu_v1._string(v_expected->'jti',256);
                PERFORM yoao_inference_gpu_v1._timestamp(v_expected->'drain_token_expires_at');
                PERFORM yoao_inference_gpu_v1._int(v_expected->'owner_hard_deadline_ms',1,9223372036854775807);
                IF v_expected->>'operation'<>'evict' OR pg_catalog.jsonb_typeof(v_expected->'idempotent')<>'boolean' OR v_expected->>'drain_generation' !~ '^[1-9][0-9]{0,18}$' OR (v_expected->>'drain_generation')::numeric>9223372036854775807 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            ELSE
                IF pg_catalog.jsonb_typeof(v_expected->'require_idle')<>'boolean' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            END IF;
        END IF;
        v_expected:=v_args->'expected_subject';
        IF v_args->'challenge'='null'::jsonb THEN
            IF p_action='eviction_drain_health' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
            v_challenge:=NULL;
        ELSE
            v_challenge:=yoao_inference_gpu_v1._string(v_args->'challenge',64);
            IF v_challenge !~ '^[0-9a-f]{64}$' THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        END IF;
        v_id:=yoao_inference_gpu_v1._uuid(v_expected->'backend_registry_id'); v_resource:=yoao_inference_gpu_v1._resource(v_expected->'gpu_resource_id');
        PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
        IF p_action='eviction_cancel_from_health' THEN
            SELECT * INTO v_cancel FROM public.gpu_backend_cancel_intents c WHERE c.backend_registry_id=v_id FOR UPDATE;
        END IF;
        v_now:=pg_catalog.transaction_timestamp();
        IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'active' OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL OR v_membership.membership_epoch<>yoao_inference_gpu_v1._int(v_expected->'membership_epoch',1,9223372036854775807) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_missing');
        ELSIF v_membership.vram_budget_mb<>yoao_inference_gpu_v1._int(v_expected->'budget_mb',1,2147483647) OR v_membership.eviction_priority<>yoao_inference_gpu_v1._int(v_expected->'eviction_priority',-2147483648,2147483647) OR v_membership.max_concurrency<>yoao_inference_gpu_v1._int(v_expected->'max_concurrency',1,10000) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','membership_changed');
        ELSIF (p_action='cold_terminal_from_health' AND v_fence.generation_high_water<>(v_expected->>'generation')::bigint
               AND NOT EXISTS (SELECT 1 FROM yoao_inference_gpu_v1.installation_terminal_intents t
                   WHERE t.backend_registry_id=v_id AND NOT t.settled
                     AND t.request->>'resource_id'=v_resource AND t.request->>'boot_id'=v_expected->>'boot_id'
                     AND t.membership_epoch=v_membership.membership_epoch
                     AND t.source_generation=v_expected->>'generation'
                     AND t.generation=v_fence.generation_high_water
                     AND t.control_epoch=v_fence.control_epoch_high_water AND t.runtime_epoch=v_fence.runtime_epoch_high_water))
           OR (p_action<>'cold_terminal_from_health' AND v_fence.generation_high_water<(v_expected->>'generation')::bigint) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','generation_changed');
        ELSIF v_fence.control_epoch_high_water::text IS DISTINCT FROM v_expected->>'control_epoch' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','control_epoch_changed');
        ELSIF v_fence.runtime_epoch_high_water::text IS DISTINCT FROM v_expected->>'runtime_epoch' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_epoch_changed');
        ELSIF v_fence.token_expiry_high_water IS NULL OR v_fence.token_expiry_high_water<yoao_inference_gpu_v1._timestamp(v_expected->'token_expires_at') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','token_horizon_changed');
        ELSIF v_registry.state<>'connected' OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.vram_budget_mb IS DISTINCT FROM v_membership.vram_budget_mb OR v_registry.eviction_priority IS DISTINCT FROM v_membership.eviction_priority OR yoao_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','registry_claim_changed');
        ELSIF yoao_inference_gpu_v1._timestamp(v_expected->'token_expires_at')<=v_now THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','runtime_subject_changed');
        ELSIF p_action='eviction_cancel_from_health' AND v_cancel.backend_registry_id IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_missing');
        ELSIF p_action='eviction_cancel_from_health' AND (v_cancel.gpu_resource_id IS DISTINCT FROM v_resource OR v_cancel.membership_epoch<>v_membership.membership_epoch OR v_cancel.generation<>(v_expected->>'generation')::bigint OR v_cancel.drain_generation<>(v_expected->>'drain_generation')::bigint OR v_cancel.source_generation<>(v_expected->>'source_generation')::bigint OR v_cancel.owner_id IS DISTINCT FROM v_expected->>'owner_id' OR v_cancel.operation IS DISTINCT FROM v_expected->>'operation' OR v_cancel.owner_hard_deadline_ms<>yoao_inference_gpu_v1._int(v_expected->'owner_hard_deadline_ms',1,9223372036854775807) OR v_cancel.jti IS DISTINCT FROM v_expected->>'jti' OR v_cancel.pool_ids IS DISTINCT FROM v_expected->'pool_ids' OR v_cancel.drain_token_expires_at IS DISTINCT FROM yoao_inference_gpu_v1._timestamp(v_expected->'drain_token_expires_at') OR v_cancel.token_expires_at IS DISTINCT FROM yoao_inference_gpu_v1._timestamp(v_expected->'token_expires_at')) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_changed');
        ELSIF p_action='eviction_cancel_from_health' AND v_cancel.subject_fingerprint IS DISTINCT FROM pg_catalog.encode(public.digest(pg_catalog.convert_to(pg_catalog.jsonb_build_object('backend_registry_id',v_cancel.backend_registry_id,'gpu_resource_id',v_cancel.gpu_resource_id,'membership_epoch',v_cancel.membership_epoch,'boot_id',v_cancel.boot_id,'control_epoch',v_cancel.control_epoch,'runtime_epoch',v_cancel.runtime_epoch,'source_generation',v_cancel.source_generation,'drain_generation',v_cancel.drain_generation,'generation',v_cancel.generation,'owner_id',v_cancel.owner_id,'operation',v_cancel.operation,'owner_hard_deadline_ms',v_cancel.owner_hard_deadline_ms,'drain_token_expires_at',yoao_inference_gpu_v1._iso(v_cancel.drain_token_expires_at),'token_expires_at',yoao_inference_gpu_v1._iso(v_cancel.token_expires_at),'jti',v_cancel.jti,'pool_ids',v_cancel.pool_ids,'backend_url',v_registry.url,'backend_auth_method',v_registry.auth_method,'backend_auth_token',v_registry.auth_token)::text,'UTF8'),'sha256'),'hex') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','cancel_intent_source_changed');
        ELSE
            v_proof:=pg_catalog.jsonb_build_object('membership',yoao_inference_gpu_v1._membership_doc(v_id,v_resource),'fence',yoao_inference_gpu_v1._fence_doc(v_id),'registry',yoao_inference_gpu_v1._registry_doc(v_id),'db_now',yoao_inference_gpu_v1._iso(v_now));
            v_result:=pg_catalog.jsonb_build_object('status','ready','subject',v_proof);
        END IF;

    ELSIF p_action='prepare_legacy_ack' OR p_action='prepare_rollout_control' THEN
        IF p_action='prepare_legacy_ack' THEN
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','token_ttl_seconds','evidence_ttl_seconds']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSE
            IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_registry_id','gpu_resource_id','membership_epoch','transition_id','target_gate','token_ttl_seconds','evidence_ttl_seconds']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        END IF;
        v_id:=yoao_inference_gpu_v1._uuid(v_args->'backend_registry_id'); v_resource:=yoao_inference_gpu_v1._resource(v_args->'gpu_resource_id'); v_next:=yoao_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807); v_budget:=yoao_inference_gpu_v1._int(v_args->'token_ttl_seconds',1,300);
        IF pg_catalog.jsonb_typeof(v_args->'evidence_ttl_seconds')<>'number' OR (v_args->>'evidence_ttl_seconds')::numeric<=0 OR (v_args->>'evidence_ttl_seconds')::numeric>300 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        IF p_action='prepare_rollout_control' THEN v_transition:=yoao_inference_gpu_v1._uuid(v_args->'transition_id'); v_target:=yoao_inference_gpu_v1._string(v_args->'target_gate',16); IF v_target NOT IN ('legacy','enforce') THEN PERFORM yoao_inference_gpu_v1._fail(); END IF; END IF;
        IF NOT yoao_inference_gpu_v1._lock_resource(v_resource,false) THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','gpu_resource_busy');
        ELSE
            PERFORM m.backend_registry_id FROM public.gpu_backend_memberships AS m WHERE m.gpu_resource_id=v_resource ORDER BY m.backend_registry_id FOR UPDATE OF m;
            PERFORM f.backend_registry_id FROM public.gpu_backend_fences AS f WHERE f.backend_registry_id IN (SELECT m.backend_registry_id FROM public.gpu_backend_memberships AS m WHERE m.gpu_resource_id=v_resource) ORDER BY f.backend_registry_id FOR UPDATE OF f;
            SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource;
            SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id;
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
            v_now:=pg_catalog.transaction_timestamp();
            IF v_membership.backend_registry_id IS NULL OR v_membership.membership_epoch<>v_next OR v_membership.state NOT IN ('pending','active') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','membership_changed');
            ELSIF v_fence.backend_registry_id IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','fence_missing');
            ELSIF v_registry.id IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','registry_missing');
            ELSIF p_action='prepare_rollout_control' AND v_membership.state<>'active' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','membership_not_active');
            ELSIF p_action='prepare_rollout_control' AND NOT EXISTS(SELECT 1 FROM public.gpu_arbiter_rollouts r WHERE r.gpu_resource_id=v_resource AND r.transition_id=v_transition AND ((v_target='enforce' AND r.state='promoting') OR (v_target='legacy' AND r.state='demoting'))) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','rollout_transition_changed');
            ELSIF p_action='prepare_legacy_ack' AND v_membership.state='pending' AND v_fence.runtime_epoch_high_water<>v_membership.runtime_epoch_baseline THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','pending_runtime_epoch_mismatch');
            ELSIF v_membership.state='active' AND v_fence.runtime_epoch_high_water<=v_membership.runtime_epoch_baseline THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','active_runtime_epoch_invalid');
            ELSIF v_registry.gpu_resource_id<>v_resource OR v_registry.state<>'connected' OR v_registry.vram_budget_mb<>v_membership.vram_budget_mb OR v_registry.eviction_priority<>v_membership.eviction_priority OR yoao_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','registry_claim_mismatch');
            ELSE
                v_health:=v_registry.health_meta; v_residency:=v_health->'residency'; v_probe:=v_health->'gpu_arbiter_probe';
                IF pg_catalog.jsonb_typeof(v_health) IS DISTINCT FROM 'object' OR pg_catalog.jsonb_typeof(v_probe) IS DISTINCT FROM 'object' OR NOT yoao_inference_gpu_v1._residency_shape(v_residency) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','residency_schema_invalid');
                ELSIF v_probe->>'managed_lifecycle_sha256' IS NULL OR v_probe->>'managed_lifecycle_sha256' !~ '^[0-9a-f]{64}$' OR pg_catalog.jsonb_typeof(v_health#>'{capabilities,managed_lifecycle}') IS DISTINCT FROM 'object' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','managed_lifecycle_capability_mismatch');
                ELSIF v_probe->>'protocol_version' IS DISTINCT FROM '1' OR v_probe->>'challenge' IS NULL OR v_probe->>'challenge' !~ '^[0-9a-f]{64}$' OR v_probe->>'backend_registry_id' IS DISTINCT FROM v_id::text OR v_probe->>'gpu_resource_id' IS DISTINCT FROM v_resource OR v_probe->>'membership_epoch' IS DISTINCT FROM v_membership.membership_epoch::text OR v_probe->>'membership_state' IS NULL OR v_probe->>'membership_state' NOT IN ('pending','active') OR v_probe->>'membership_state' IS DISTINCT FROM v_membership.state THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_membership_mismatch');
                ELSIF v_probe->>'observed_at' IS NULL OR v_probe->>'observed_at' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}(:[0-9]{2})?)$' OR v_probe->>'probe_started_at' IS NULL OR v_probe->>'probe_started_at' !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}(:[0-9]{2})?)$' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_registry_clock_mismatch');
                ELSE
                    v_observed:=(v_probe->>'observed_at')::timestamp with time zone;
                    v_probe_started:=(v_probe->>'probe_started_at')::timestamp with time zone;
                    v_trusted_empty:=v_residency->>'state' IN ('unloaded','resident') AND v_residency->'gpu_loaded'='false'::jsonb AND v_residency->'generation'='null'::jsonb AND v_residency->'evictable'='false'::jsonb AND NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident' IS DISTINCT FROM 'false'::jsonb);
                    v_enforce_intent:=FALSE; v_reset_ack:=FALSE; v_intent_matches:=FALSE;
                    IF v_registry.last_checked_at IS NULL OR v_registry.last_checked_at IS DISTINCT FROM v_observed THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_registry_clock_mismatch');
                    ELSIF v_observed>v_now THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_from_future');
                    ELSIF v_now-v_observed>make_interval(secs=>(v_args->>'evidence_ttl_seconds')::double precision) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_expired');
                    ELSIF v_residency->'identity'<>'null'::jsonb AND v_residency->'identity' IS DISTINCT FROM pg_catalog.jsonb_build_object('audience','aap-gpu-lifecycle','backend_registry_id',v_id::text,'gpu_resource_id',v_resource) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','residency_identity_mismatch');
                    ELSIF p_action='prepare_legacy_ack' AND (v_residency->>'lifecycle_gate'<>'legacy' OR v_residency->'generation'<>'null'::jsonb OR v_residency->'evictable'<>'false'::jsonb OR v_residency->>'state' NOT IN ('unloaded','resident') OR v_residency->'gpu_loaded'='null'::jsonb OR (v_residency->>'active_requests')::bigint<>0 OR (v_residency->>'builders')::bigint<>0 OR (v_residency->>'borrowers')::bigint<>0 OR v_residency->'draining'<>'false'::jsonb OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE pg_catalog.jsonb_typeof(p.pool_value->'resident') IS DISTINCT FROM 'boolean')) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','legacy_residency_not_stably_idle');
                    ELSIF p_action='prepare_legacy_ack' AND v_residency->'gpu_loaded'='true'::jsonb AND (v_residency->>'state'<>'resident' OR NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident'='true'::jsonb)) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','legacy_residency_loaded_inconsistent');
                    ELSIF p_action='prepare_legacy_ack' AND v_membership.state='pending' AND (v_residency->'identity'<>'null'::jsonb OR v_residency->'control_epoch'<>'null'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','pending_lifecycle_identity_already_bound');
                    ELSIF p_action='prepare_legacy_ack' AND v_membership.state='active' AND ((v_residency->'identity'='null'::jsonb)<>(v_residency->'control_epoch'='null'::jsonb)) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','active_lifecycle_identity_incomplete');
                    ELSIF p_action='prepare_legacy_ack' AND v_residency->'control_epoch'<>'null'::jsonb AND (v_residency->>'control_epoch')::numeric>v_fence.control_epoch_high_water THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','residency_control_epoch_ahead');
                    ELSIF p_action='prepare_rollout_control' AND (v_residency->'identity'='null'::jsonb OR v_residency->'control_epoch'='null'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','residency_control_identity_missing');
                    ELSIF p_action='prepare_rollout_control' AND (v_residency->>'control_epoch')::numeric>v_fence.control_epoch_high_water THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','residency_control_epoch_ahead');
                    ELSIF p_action='prepare_rollout_control' AND (v_residency->>'state' NOT IN ('unloaded','resident') OR v_residency->'gpu_loaded'='null'::jsonb OR (v_residency->>'active_requests')::bigint<>0 OR (v_residency->>'builders')::bigint<>0 OR (v_residency->>'borrowers')::bigint<>0 OR v_residency->'draining'<>'false'::jsonb OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE pg_catalog.jsonb_typeof(p.pool_value->'resident') IS DISTINCT FROM 'boolean')) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','rollout_control_residency_not_stably_idle');
                    ELSIF p_action='prepare_rollout_control' AND v_residency->'gpu_loaded'='true'::jsonb AND (v_residency->>'state'<>'resident' OR NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident'='true'::jsonb)) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','rollout_control_residency_loaded_inconsistent');
                    ELSIF p_action='prepare_rollout_control' AND v_residency->'gpu_loaded'='false'::jsonb AND EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->'resident' IS DISTINCT FROM 'false'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','rollout_control_residency_unloaded_inconsistent');
                    ELSE
                        IF p_action='prepare_legacy_ack' THEN
                            v_operation:='reset';
                        ELSE
                            v_enforce_intent:=v_fence.rollout_control_operation='mode_enforce' AND v_fence.rollout_control_transition_id=v_transition AND v_fence.rollout_control_epoch=v_fence.control_epoch_high_water AND v_fence.rollout_control_membership_epoch=v_membership.membership_epoch AND v_fence.rollout_control_boot_id=v_residency->>'boot_id' AND v_fence.rollout_control_token_expires_at IS NOT NULL;
                            v_reset_ack:=v_fence.rollout_control_operation='reset' AND v_fence.rollout_control_transition_id=v_transition AND v_fence.rollout_control_epoch=v_fence.control_epoch_high_water AND v_fence.rollout_control_membership_epoch=v_membership.membership_epoch AND v_fence.rollout_control_boot_id=v_residency->>'boot_id' AND v_fence.rollout_control_token_expires_at IS NOT NULL AND v_residency->>'control_epoch'=v_fence.rollout_control_epoch::text AND v_residency->>'lifecycle_gate'='legacy' AND v_probe_started>v_fence.rollout_control_token_expires_at AND v_trusted_empty;
                            IF v_target='legacy' THEN v_operation:='mode_legacy';
                            ELSIF v_enforce_intent THEN v_operation:='mode_enforce';
                            ELSIF v_residency->>'lifecycle_gate'='enforce' THEN v_operation:='mode_enforce';
                            ELSIF v_reset_ack THEN v_operation:='mode_enforce';
                            ELSE v_operation:='reset'; END IF;
                            v_intent_matches:=v_fence.rollout_control_operation=v_operation AND v_fence.rollout_control_transition_id=v_transition AND v_fence.rollout_control_epoch=v_fence.control_epoch_high_water AND v_fence.rollout_control_membership_epoch=v_membership.membership_epoch AND v_fence.rollout_control_boot_id=v_residency->>'boot_id' AND v_fence.rollout_control_token_expires_at IS NOT NULL;
                        END IF;
                        v_status:='create';
                        IF p_action='prepare_legacy_ack' AND v_membership.state='active' AND v_residency->>'control_epoch'=v_fence.control_epoch_high_water::text THEN
                            v_status:='legacy_acknowledged';
                        ELSIF v_intent_matches AND v_probe_started>v_fence.rollout_control_token_expires_at AND v_residency->>'control_epoch'=v_fence.rollout_control_epoch::text AND ((v_operation='mode_legacy' AND v_residency->>'lifecycle_gate'='legacy' AND v_residency->'evictable'='false'::jsonb) OR (v_operation<>'mode_legacy' AND v_trusted_empty AND v_residency->>'lifecycle_gate'=(CASE WHEN v_operation='reset' THEN 'legacy' ELSE 'enforce' END))) THEN
                            v_status:='acknowledged';
                        ELSIF v_intent_matches THEN
                            v_control:=v_fence.rollout_control_epoch; v_time:=v_fence.rollout_control_token_expires_at;
                            IF v_time>v_now THEN v_status:='replay';
                            ELSIF v_probe_started<=v_time THEN v_status:='awaiting_control_health';
                            END IF;
                        END IF;
                        IF v_status='legacy_acknowledged' THEN
                            v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','acknowledged','backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'boot_id',v_residency->>'boot_id','runtime_epoch',v_fence.runtime_epoch_high_water::text,'control_epoch',v_fence.control_epoch_high_water::text,'token_expires_at',NULL,'proof_ready',COALESCE(v_fence.token_expiry_high_water IS NOT NULL AND v_probe_started>v_fence.token_expiry_high_water AND v_trusted_empty,false)));
                        ELSIF v_status='acknowledged' THEN
                            v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','acknowledged','operation',v_operation,'target_gate',v_target,'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'transition_id',v_transition,'boot_id',v_residency->>'boot_id','control_epoch',v_fence.control_epoch_high_water::text,'token_expires_at',yoao_inference_gpu_v1._iso(v_fence.rollout_control_token_expires_at),'reason','fresh_health_acknowledged'));
                        ELSIF v_status='replay' THEN
                            v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','issue','operation',v_operation,'target_gate',v_target,'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'transition_id',v_transition,'boot_id',v_residency->>'boot_id','control_epoch',v_control::text,'token_expires_at',yoao_inference_gpu_v1._iso(v_time),'reason','control_intent_replay'));
                        ELSIF v_status='awaiting_control_health' THEN
                            v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','awaiting_health','operation',v_operation,'target_gate',v_target,'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'transition_id',v_transition,'boot_id',v_residency->>'boot_id','control_epoch',v_control::text,'token_expires_at',yoao_inference_gpu_v1._iso(v_time),'reason','control_health_not_after_token_horizon'));
                        ELSIF p_action='prepare_rollout_control' AND v_operation='reset' AND (v_residency->>'lifecycle_gate'<>'legacy' OR v_residency->'evictable'<>'false'::jsonb) THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','reset_requires_legacy_gate');
                        ELSIF p_action='prepare_rollout_control' AND v_operation='mode_enforce' AND NOT v_trusted_empty THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','enforce_mode_requires_reset_empty');
                        ELSIF p_action='prepare_rollout_control' AND v_operation='mode_enforce' AND v_residency->>'lifecycle_gate'='legacy' AND NOT v_reset_ack THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','enforce_mode_requires_reset_ack');
                        ELSIF p_action='prepare_rollout_control' AND v_operation='mode_legacy' AND v_residency->>'lifecycle_gate' NOT IN ('legacy','enforce') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','legacy_mode_source_gate_invalid');
                        ELSIF p_action='prepare_rollout_control' AND v_fence.token_expiry_high_water IS NULL THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','token_horizon_missing');
                        ELSIF p_action='prepare_legacy_ack' AND v_fence.token_expiry_high_water IS NOT NULL AND v_probe_started<=v_fence.token_expiry_high_water THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','probe_not_after_token_horizon');
                        ELSIF p_action='prepare_rollout_control' AND v_probe_started<=v_fence.token_expiry_high_water THEN
                            v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','awaiting_health','operation',v_operation,'target_gate',v_target,'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'transition_id',v_transition,'boot_id',v_residency->>'boot_id','control_epoch',v_fence.control_epoch_high_water::text,'token_expires_at',yoao_inference_gpu_v1._iso(v_fence.token_expiry_high_water),'reason','control_health_not_after_token_horizon'));
                        ELSIF v_fence.control_epoch_high_water=9223372036854775807 OR (v_membership.state='pending' AND v_fence.runtime_epoch_high_water=9223372036854775807) THEN PERFORM yoao_inference_gpu_v1._fail();
                        ELSE
                            IF v_membership.state='pending' THEN
                                UPDATE public.gpu_backend_fences f SET runtime_epoch_high_water=f.runtime_epoch_high_water+1 WHERE f.backend_registry_id=v_id RETURNING f.runtime_epoch_high_water INTO v_runtime;
                                UPDATE public.gpu_backend_memberships m SET state='active',updated_at=v_now WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource;
                            ELSE v_runtime:=v_fence.runtime_epoch_high_water; END IF;
                            v_control:=v_fence.control_epoch_high_water+1; v_time:=v_now+make_interval(secs=>v_budget);
                            UPDATE public.gpu_backend_fences f SET control_epoch_high_water=v_control,token_expiry_high_water=GREATEST(COALESCE(f.token_expiry_high_water,v_time),v_time),rollout_control_operation=CASE WHEN p_action='prepare_rollout_control' THEN v_operation ELSE f.rollout_control_operation END,rollout_control_transition_id=CASE WHEN p_action='prepare_rollout_control' THEN v_transition ELSE f.rollout_control_transition_id END,rollout_control_epoch=CASE WHEN p_action='prepare_rollout_control' THEN v_control ELSE f.rollout_control_epoch END,rollout_control_membership_epoch=CASE WHEN p_action='prepare_rollout_control' THEN v_membership.membership_epoch ELSE f.rollout_control_membership_epoch END,rollout_control_boot_id=CASE WHEN p_action='prepare_rollout_control' THEN v_residency->>'boot_id' ELSE f.rollout_control_boot_id END,rollout_control_token_expires_at=CASE WHEN p_action='prepare_rollout_control' THEN v_time ELSE f.rollout_control_token_expires_at END,updated_at=v_now WHERE f.backend_registry_id=v_id;
                            IF p_action='prepare_legacy_ack' THEN
                                v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','issue','backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'boot_id',v_residency->>'boot_id','runtime_epoch',v_runtime::text,'control_epoch',v_control::text,'token_expires_at',yoao_inference_gpu_v1._iso(v_time),'proof_ready',false));
                            ELSE
                                v_result:=pg_catalog.jsonb_build_object('status','ok','preparation',pg_catalog.jsonb_build_object('action','issue','operation',v_operation,'target_gate',v_target,'backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_membership.membership_epoch,'transition_id',v_transition,'boot_id',v_residency->>'boot_id','control_epoch',v_control::text,'token_expires_at',yoao_inference_gpu_v1._iso(v_time),'reason','control_intent_created'));
                            END IF;
                        END IF;
                    END IF;
                END IF;
            END IF;
        END IF;

    ELSIF p_action='retired_probe_subject' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_id','resource_id','membership_epoch']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_id:=yoao_inference_gpu_v1._uuid(v_args->'backend_id'); v_resource:=yoao_inference_gpu_v1._resource(v_args->'resource_id'); v_next:=yoao_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807);
        PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
        IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'retiring' OR v_membership.membership_epoch<>v_next OR v_fence.backend_registry_id IS NULL OR v_fence.generation_high_water IS DISTINCT FROM v_membership.retired_generation_high_water OR v_fence.control_epoch_high_water IS DISTINCT FROM v_membership.retired_control_epoch_high_water THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','retired_membership_changed');
        ELSE
            v_now:=pg_catalog.transaction_timestamp();
            IF v_membership.retired_token_expiry_high_water IS NOT NULL AND v_now<=v_membership.retired_token_expiry_high_water THEN v_reason:='waiting_token_horizon';
            ELSIF v_registry.id IS NULL THEN v_reason:='registry_missing';
            ELSE v_reason:='ready'; END IF;
            IF v_reason='ready' THEN v_challenge:=encode(public.gen_random_bytes(32),'hex'); v_probe_started:=pg_catalog.clock_timestamp();
                v_result:=pg_catalog.jsonb_build_object('status','ready','reason','','subject',pg_catalog.jsonb_build_object('challenge',v_challenge,'probe_started_at',yoao_inference_gpu_v1._iso(v_probe_started)));
            ELSE v_result:=pg_catalog.jsonb_build_object('status','blocked','reason',v_reason,'subject',NULL); END IF;
        END IF;

    ELSIF p_action='retired_probe_commit' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['backend_id','resource_id','membership_epoch','challenge','probe_started_at','endpoint_sha256','residency']) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        v_id:=yoao_inference_gpu_v1._uuid(v_args->'backend_id'); v_resource:=yoao_inference_gpu_v1._resource(v_args->'resource_id'); v_next:=yoao_inference_gpu_v1._int(v_args->'membership_epoch',1,9223372036854775807); v_challenge:=yoao_inference_gpu_v1._string(v_args->'challenge',64); v_probe_started:=yoao_inference_gpu_v1._timestamp(v_args->'probe_started_at'); v_fingerprint:=yoao_inference_gpu_v1._string(v_args->'endpoint_sha256',64); v_residency:=v_args->'residency';
        IF v_challenge !~ '^[0-9a-f]{64}$' OR v_fingerprint !~ '^[0-9a-f]{64}$' OR NOT yoao_inference_gpu_v1._residency_shape(v_residency) THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id;
        v_now:=pg_catalog.clock_timestamp(); v_observed:=v_now;
        IF v_membership.backend_registry_id IS NULL OR v_membership.state<>'retiring' OR v_membership.membership_epoch<>v_next OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL OR v_fence.generation_high_water IS DISTINCT FROM v_membership.retired_generation_high_water OR v_fence.control_epoch_high_water IS DISTINCT FROM v_membership.retired_control_epoch_high_water OR v_membership.retired_token_expiry_high_water IS NOT NULL AND v_now<=v_membership.retired_token_expiry_high_water OR v_probe_started>=v_observed OR v_observed-v_probe_started>interval '5 minutes' THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','retired_membership_changed');
        ELSIF v_residency->>'state' NOT IN ('unloaded','resident') OR v_residency->>'gpu_loaded'<>'false' OR v_residency->>'draining'<>'false' OR v_residency->>'evictable'<>'false' OR (v_residency->>'active_requests')::bigint<>0 OR (v_residency->>'builders')::bigint<>0 OR (v_residency->>'borrowers')::bigint<>0 OR v_residency->'identity'='null'::jsonb OR v_residency #>> '{identity,audience}' IS DISTINCT FROM 'aap-gpu-lifecycle' OR v_residency #>> '{identity,backend_registry_id}' IS DISTINCT FROM v_id::text OR v_residency #>> '{identity,gpu_resource_id}' IS DISTINCT FROM v_resource OR (v_residency->>'generation' IS NOT NULL AND (v_membership.retired_generation_high_water IS NULL OR (v_residency->>'generation')::bigint>v_membership.retired_generation_high_water)) OR (v_residency->>'control_epoch' IS NOT NULL AND (v_membership.retired_control_epoch_high_water IS NULL OR (v_residency->>'control_epoch')::bigint>v_membership.retired_control_epoch_high_water)) OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(pool_id,pool_value) WHERE p.pool_value->>'resident' IS DISTINCT FROM 'false') THEN v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','retired_residency_not_unloaded');
        ELSE
            v_deadline:=floor(extract(epoch FROM (v_observed+interval '3 minutes'))*1000)::bigint;
            v_fingerprint:=encode(public.digest(pg_catalog.jsonb_build_object('backend_id',v_id,'resource_id',v_resource,'membership_epoch',v_next,'retirement_id',v_membership.retirement_id,'challenge',v_challenge,'probe_started_at',yoao_inference_gpu_v1._iso(v_probe_started),'observed_at',yoao_inference_gpu_v1._iso(v_observed),'evidence_deadline_ms',v_deadline,'endpoint_sha256',v_args->>'endpoint_sha256','residency',v_residency)::text,'sha256'),'hex');
            UPDATE public.gpu_backend_memberships m SET retired_health_state='connected',retired_health_meta=v_residency,retired_health_checked_at=v_observed,updated_at=v_observed WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource;
            v_result:=pg_catalog.jsonb_build_object('status','live_unloaded','reason','','proof',pg_catalog.jsonb_build_object('observed_at',yoao_inference_gpu_v1._iso(v_observed),'evidence_deadline_ms',v_deadline,'evidence_fingerprint',v_fingerprint,'residency',v_residency));
        END IF;

    ELSIF p_action='installation_control' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['request','phase','residency','probe_started_at','token_expires_at'])
           OR v_args->>'phase' NOT IN ('reset','mode_enforce')
           OR NOT yoao_inference_gpu_v1._exact_keys(v_args->'request',ARRAY['registry_id','resource_id','endpoint','boot_id','operation_id','container_id','pool_ids'])
           OR pg_catalog.jsonb_typeof(v_args->'request'->'pool_ids') IS DISTINCT FROM 'array'
           OR NOT yoao_inference_gpu_v1._residency_shape(v_args->'residency') THEN
            PERFORM yoao_inference_gpu_v1._fail();
        END IF;
        v_expected:=v_args->'request'; v_residency:=v_args->'residency';
        v_id:=yoao_inference_gpu_v1._uuid(v_expected->'registry_id');
        v_resource:=yoao_inference_gpu_v1._resource(v_expected->'resource_id');
        v_id2:=yoao_inference_gpu_v1._uuid(v_expected->'operation_id');
        v_boot:=yoao_inference_gpu_v1._string(v_expected->'boot_id',128);
        v_text:=yoao_inference_gpu_v1._string(v_expected->'endpoint',2048);
        v_reason:=yoao_inference_gpu_v1._string(v_expected->'container_id',64);
        v_operation:=yoao_inference_gpu_v1._string(v_args->'phase',16);
        v_probe_started:=yoao_inference_gpu_v1._timestamp(v_args->'probe_started_at');
        v_time:=yoao_inference_gpu_v1._timestamp(v_args->'token_expires_at');
        IF v_reason !~ '^[0-9a-f]{64}$'
           OR v_expected->'pool_ids' IS DISTINCT FROM
              (SELECT pg_catalog.jsonb_agg(k ORDER BY k) FROM pg_catalog.jsonb_object_keys(v_residency->'pools') p(k)) THEN
            PERFORM yoao_inference_gpu_v1._fail();
        END IF;
        PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        BEGIN
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available THEN
            RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','registry_busy'));
        END;
        SELECT * INTO v_install_control FROM yoao_inference_gpu_v1.installation_control_intents i WHERE i.backend_registry_id=v_id FOR UPDATE;
        v_now:=pg_catalog.clock_timestamp();
        IF v_membership.backend_registry_id IS NULL OR v_membership.state NOT IN ('pending','active')
           OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL
           OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource OR v_registry.url IS DISTINCT FROM v_text
           OR v_registry.vram_budget_mb IS DISTINCT FROM v_membership.vram_budget_mb
           OR v_registry.eviction_priority IS DISTINCT FROM v_membership.eviction_priority
           OR yoao_inference_gpu_v1._max_concurrency(v_registry.extra_params)<>v_membership.max_concurrency
           OR v_registry.extra_params #>> '{plugin,installation_id}' IS DISTINCT FROM v_id::text
           OR v_registry.extra_params #>> '{plugin,status}' IS NULL
           OR v_registry.extra_params #>> '{plugin,status}' NOT IN ('prepared','disabled')
           OR v_registry.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM v_resource
           OR v_registry.extra_params #>> '{plugin_lifecycle,launch,container_id}' IS DISTINCT FROM v_reason
           OR v_registry.extra_params #>> '{plugin_lifecycle,launch,registry_id}' IS DISTINCT FROM v_id::text
           OR v_registry.extra_params #> '{plugin_lifecycle,launch,resource}' IS DISTINCT FROM v_registry.extra_params #> '{plugin,resource}'
           OR pg_catalog.jsonb_typeof(v_registry.extra_params #> '{plugin_lifecycle,stopped}') IS DISTINCT FROM 'array'
           OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(CASE WHEN pg_catalog.jsonb_typeof(v_registry.extra_params #> '{plugin_lifecycle,stopped}')='array' THEN v_registry.extra_params #> '{plugin_lifecycle,stopped}' ELSE '[]'::jsonb END) s(value)
                     WHERE s.value->>'container_id'=v_reason OR s.value->>'boot_id'=v_boot)
           OR (v_registry.extra_params #>> '{plugin_lifecycle,activation,boot_id}' IS NOT NULL AND v_registry.extra_params #>> '{plugin_lifecycle,activation,boot_id}' IS DISTINCT FROM v_boot)
           OR v_residency->>'boot_id' IS DISTINCT FROM v_boot
           OR v_residency->>'state' NOT IN ('unloaded','resident')
           OR v_residency->'draining' IS DISTINCT FROM 'false'::jsonb
           OR (v_residency->'identity' IS DISTINCT FROM 'null'::jsonb AND v_residency->'identity' IS DISTINCT FROM pg_catalog.jsonb_build_object('audience','aap-gpu-lifecycle','backend_registry_id',v_id::text,'gpu_resource_id',v_resource))
           OR ((v_residency->'identity'='null'::jsonb) <> (v_residency->'control_epoch'='null'::jsonb))
           OR (v_residency->>'control_epoch' IS NOT NULL AND (v_residency->>'control_epoch')::bigint>v_fence.control_epoch_high_water)
           OR (v_membership.state='pending' AND (v_fence.runtime_epoch_high_water<>v_membership.runtime_epoch_baseline OR v_residency->'identity' IS DISTINCT FROM 'null'::jsonb))
           OR (v_membership.state='active' AND v_fence.runtime_epoch_high_water<=v_membership.runtime_epoch_baseline)
           OR EXISTS(SELECT 1 FROM yoao_inference_gpu_v1.installation_terminal_intents t
                     WHERE t.backend_registry_id=v_id AND t.request->>'boot_id'=v_boot)
           OR v_probe_started>v_now OR v_probe_started<v_now-interval '3 minutes'
           OR v_time<=v_now OR v_time>v_now+interval '5 minutes' THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_control_changed');
        ELSIF v_membership.state='active' AND v_residency->>'lifecycle_gate'='enforce'
           AND v_residency->'identity'=pg_catalog.jsonb_build_object('audience','aap-gpu-lifecycle','backend_registry_id',v_id::text,'gpu_resource_id',v_resource)
           AND v_residency->>'control_epoch'=v_fence.control_epoch_high_water::text
           AND v_fence.control_epoch_high_water>0
           AND v_residency->'gpu_loaded' IN ('true'::jsonb,'false'::jsonb)
           AND NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(k,v) WHERE pg_catalog.jsonb_typeof(p.v->'resident') IS DISTINCT FROM 'boolean')
           AND (v_residency->>'generation' IS NULL OR ((v_residency->>'generation')::bigint>0 AND (v_residency->>'generation')::bigint<=v_fence.generation_high_water))
           AND ((v_residency->'gpu_loaded'='true'::jsonb AND v_residency->>'state'='resident'
                 AND v_residency->>'generation' IS NOT NULL
                 AND EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(k,v) WHERE p.v->'resident'='true'::jsonb))
                OR (v_residency->'gpu_loaded'='false'::jsonb
                    AND NOT EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(k,v) WHERE p.v->'resident' IS DISTINCT FROM 'false'::jsonb))) THEN
            v_result:=pg_catalog.jsonb_build_object('status','complete','preparation',
                pg_catalog.jsonb_build_object('membership_epoch',v_membership.membership_epoch,
                    'control_epoch',v_fence.control_epoch_high_water::text,
                    'token_expires_at',yoao_inference_gpu_v1._iso(COALESCE(v_fence.token_expiry_high_water,v_now))));
        ELSIF v_residency->'gpu_loaded' IS DISTINCT FROM 'false'::jsonb
           OR v_residency->'generation' IS DISTINCT FROM 'null'::jsonb
           OR v_residency->'evictable' IS DISTINCT FROM 'false'::jsonb
           OR v_residency->>'active_requests' IS DISTINCT FROM '0'
           OR v_residency->>'builders' IS DISTINCT FROM '0' OR v_residency->>'borrowers' IS DISTINCT FROM '0'
           OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(k,v) WHERE p.v->'resident' IS DISTINCT FROM 'false'::jsonb) THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_control_not_empty');
        ELSIF v_install_control.backend_registry_id IS NOT NULL
           AND v_install_control.request IS DISTINCT FROM v_expected
           AND v_install_control.revision IS NOT DISTINCT FROM v_registry.extra_params #> '{plugin,revision}'
           AND NOT (v_operation='reset' AND (v_install_control.request-'operation_id')=(v_expected-'operation_id')) THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_control_conflict');
        ELSE
            v_intent_matches:=v_install_control.backend_registry_id IS NOT NULL AND v_install_control.request=v_expected
                AND v_install_control.revision IS NOT DISTINCT FROM v_registry.extra_params #> '{plugin,revision}'
                AND v_install_control.membership_epoch=v_membership.membership_epoch
                AND v_install_control.control_epoch=v_fence.control_epoch_high_water;
            IF v_intent_matches AND v_install_control.operation='mode_enforce'
               AND v_residency->>'lifecycle_gate'='enforce'
               AND v_residency->>'control_epoch'=v_install_control.control_epoch::text THEN
                v_status:='complete';
            ELSIF v_intent_matches AND v_install_control.operation='reset' AND v_operation='reset'
               AND v_residency->>'lifecycle_gate'='legacy'
               AND v_residency->>'control_epoch'=v_install_control.control_epoch::text THEN
                v_status:='acknowledged';
            ELSIF v_intent_matches AND v_install_control.operation=v_operation AND v_install_control.token_expires_at>v_now THEN
                v_status:='prepared';
            ELSE
                v_status:='create';
            END IF;
            IF v_status IN ('complete','acknowledged','prepared') THEN
                v_result:=pg_catalog.jsonb_build_object('status',v_status,'preparation',
                    pg_catalog.jsonb_build_object('membership_epoch',v_membership.membership_epoch,
                        'control_epoch',v_install_control.control_epoch::text,
                        'token_expires_at',yoao_inference_gpu_v1._iso(v_install_control.token_expires_at)));
            ELSIF v_fence.token_expiry_high_water IS NOT NULL AND v_probe_started<=v_fence.token_expiry_high_water THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','control_token_horizon','token_horizon',yoao_inference_gpu_v1._iso(v_fence.token_expiry_high_water));
            ELSIF v_operation='mode_enforce' AND NOT (
                v_intent_matches AND v_residency->>'lifecycle_gate'='legacy'
                AND v_probe_started>v_install_control.token_expires_at
                AND ((v_install_control.operation='reset' AND v_residency->>'control_epoch'=v_install_control.control_epoch::text)
                     OR (v_install_control.operation='mode_enforce' AND v_install_control.control_epoch>1
                         AND v_residency->>'control_epoch'=(v_install_control.control_epoch-1)::text))) THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_reset_unconfirmed');
            ELSIF v_operation='reset' AND v_residency->>'lifecycle_gate'<>'legacy' THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_reset_gate_invalid');
            ELSIF v_fence.control_epoch_high_water=9223372036854775807
               OR (v_membership.state='pending' AND v_fence.runtime_epoch_high_water=9223372036854775807) THEN
                PERFORM yoao_inference_gpu_v1._fail();
            ELSE
                IF v_membership.state='pending' THEN
                    UPDATE public.gpu_backend_fences SET runtime_epoch_high_water=runtime_epoch_high_water+1 WHERE backend_registry_id=v_id;
                    UPDATE public.gpu_backend_memberships SET state='active',updated_at=v_now WHERE backend_registry_id=v_id AND gpu_resource_id=v_resource;
                END IF;
                v_control:=v_fence.control_epoch_high_water+1;
                UPDATE public.gpu_backend_fences SET control_epoch_high_water=v_control,
                    token_expiry_high_water=GREATEST(COALESCE(token_expiry_high_water,v_time),v_time),updated_at=v_now WHERE backend_registry_id=v_id;
                INSERT INTO yoao_inference_gpu_v1.installation_control_intents
                    (backend_registry_id,request,revision,membership_epoch,operation,control_epoch,token_expires_at)
                VALUES(v_id,v_expected,v_registry.extra_params #> '{plugin,revision}',v_membership.membership_epoch,v_operation,v_control,v_time)
                ON CONFLICT(backend_registry_id) DO UPDATE SET request=EXCLUDED.request,revision=EXCLUDED.revision,
                    membership_epoch=EXCLUDED.membership_epoch,operation=EXCLUDED.operation,
                    control_epoch=EXCLUDED.control_epoch,token_expires_at=EXCLUDED.token_expires_at;
                v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',
                    pg_catalog.jsonb_build_object('membership_epoch',v_membership.membership_epoch,
                        'control_epoch',v_control::text,'token_expires_at',yoao_inference_gpu_v1._iso(v_time)));
            END IF;
        END IF;

    ELSIF p_action='installation_terminal' THEN
        IF NOT yoao_inference_gpu_v1._exact_keys(v_args,ARRAY['phase','request','residency','preparation','token_expires_at','ledger_source_generation','probe_started_at'])
           OR v_args->>'phase' NOT IN ('reserve','authorize','prove_idle','verify','commit')
           OR NOT yoao_inference_gpu_v1._exact_keys(v_args->'request',ARRAY['registry_id','resource_id','endpoint','boot_id','operation_id','container_id','pool_ids'])
           OR pg_catalog.jsonb_typeof(v_args->'request'->'pool_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(v_args->'request'->'pool_ids')=0
           OR NOT yoao_inference_gpu_v1._residency_shape(v_args->'residency') THEN
            PERFORM yoao_inference_gpu_v1._fail();
        END IF;
        v_expected:=v_args->'request';
        v_residency:=v_args->'residency';
        v_id:=yoao_inference_gpu_v1._uuid(v_expected->'registry_id');
        v_resource:=yoao_inference_gpu_v1._resource(v_expected->'resource_id');
        v_id2:=yoao_inference_gpu_v1._uuid(v_expected->'operation_id');
        v_boot:=yoao_inference_gpu_v1._string(v_expected->'boot_id',128);
        v_text:=yoao_inference_gpu_v1._string(v_expected->'endpoint',2048);
        v_reason:=yoao_inference_gpu_v1._string(v_expected->'container_id',64);
        v_time:=yoao_inference_gpu_v1._timestamp(v_args->'token_expires_at');
        IF v_args->'ledger_source_generation' IS DISTINCT FROM 'null'::jsonb
           AND (pg_catalog.jsonb_typeof(v_args->'ledger_source_generation') IS DISTINCT FROM 'string'
                OR v_args->>'ledger_source_generation' !~ '^[1-9][0-9]{0,18}$'
                OR (v_args->>'ledger_source_generation')::numeric>9223372036854775807) THEN
            PERFORM yoao_inference_gpu_v1._fail();
        END IF;
        IF v_reason !~ '^[0-9a-f]{64}$'
           OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(v_expected->'pool_ids') x(value)
                     WHERE pg_catalog.jsonb_typeof(x.value) IS DISTINCT FROM 'string'
                     OR x.value#>>'{}'='' OR x.value#>>'{}'<>pg_catalog.btrim(x.value#>>'{}'))
           OR v_expected->'pool_ids' IS DISTINCT FROM
              (SELECT pg_catalog.jsonb_agg(k ORDER BY k) FROM pg_catalog.jsonb_object_keys(v_residency->'pools') p(k)) THEN
            PERFORM yoao_inference_gpu_v1._fail();
        END IF;
        IF v_args->>'phase'='reserve' THEN
            IF v_args->'preparation' IS DISTINCT FROM 'null'::jsonb THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
        ELSIF NOT yoao_inference_gpu_v1._exact_keys(v_args->'preparation',ARRAY['membership_epoch','revision','generation','source_generation','control_epoch','runtime_epoch','token_expires_at','settled']) OR pg_catalog.jsonb_typeof(v_args->'preparation'->'settled') IS DISTINCT FROM 'boolean' THEN
            PERFORM yoao_inference_gpu_v1._fail();
        END IF;
        IF v_args->>'phase'='prove_idle' THEN
            v_probe_started:=yoao_inference_gpu_v1._timestamp(v_args->'probe_started_at');
        ELSIF v_args->'probe_started_at' IS DISTINCT FROM 'null'::jsonb THEN
            PERFORM yoao_inference_gpu_v1._fail();
        END IF;
        PERFORM yoao_inference_gpu_v1._lock_resource(v_resource,true);
        SELECT * INTO v_membership FROM public.gpu_backend_memberships m WHERE m.backend_registry_id=v_id AND m.gpu_resource_id=v_resource FOR UPDATE;
        SELECT * INTO v_fence FROM public.gpu_backend_fences f WHERE f.backend_registry_id=v_id FOR UPDATE;
        BEGIN
            SELECT * INTO v_registry FROM public.ml_backend_registry r WHERE r.id=v_id FOR SHARE NOWAIT;
        EXCEPTION WHEN lock_not_available THEN
            RETURN pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',pg_catalog.jsonb_build_object('status','blocked','reason','registry_busy'));
        END;
        SELECT * INTO v_terminal_intent FROM yoao_inference_gpu_v1.installation_terminal_intents i WHERE i.backend_registry_id=v_id FOR UPDATE;
        v_now:=pg_catalog.clock_timestamp();
        IF v_membership.backend_registry_id IS NULL OR v_membership.state NOT IN ('pending','active')
           OR v_fence.backend_registry_id IS NULL OR v_registry.id IS NULL
           OR v_registry.gpu_resource_id IS DISTINCT FROM v_resource
           OR v_registry.url IS DISTINCT FROM v_text
           OR v_registry.extra_params #>> '{plugin,installation_id}' IS DISTINCT FROM v_id::text
           OR v_registry.extra_params #>> '{plugin,status}' NOT IN ('prepared','disabled')
           OR v_registry.extra_params #>> '{plugin,status}' IS NULL
           OR v_registry.extra_params #>> '{plugin,resource,gpu_resource_id}' IS DISTINCT FROM v_resource
           OR v_registry.extra_params #>> '{plugin_lifecycle,launch,container_id}' IS DISTINCT FROM v_reason
           OR v_registry.extra_params #>> '{plugin_lifecycle,launch,registry_id}' IS DISTINCT FROM v_id::text
           OR v_registry.extra_params #> '{plugin_lifecycle,launch,resource}' IS DISTINCT FROM v_registry.extra_params #> '{plugin,resource}'
           OR pg_catalog.jsonb_typeof(v_registry.extra_params #> '{plugin_lifecycle,stopped}') IS DISTINCT FROM 'array'
           OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_array_elements(CASE WHEN pg_catalog.jsonb_typeof(v_registry.extra_params #> '{plugin_lifecycle,stopped}')='array' THEN v_registry.extra_params #> '{plugin_lifecycle,stopped}' ELSE '[]'::jsonb END) s(value)
                     WHERE s.value->>'container_id'=v_reason OR s.value->>'boot_id'=v_boot)
           OR (v_registry.extra_params #>> '{plugin_lifecycle,activation,boot_id}' IS NOT NULL AND v_registry.extra_params #>> '{plugin_lifecycle,activation,boot_id}' IS DISTINCT FROM v_boot)
           OR v_residency->>'boot_id' IS DISTINCT FROM v_boot
           OR NOT COALESCE((
               (v_args->>'phase' IN ('reserve','authorize') AND v_residency->'identity'='null'::jsonb
                AND v_residency->'control_epoch'='null'::jsonb AND v_residency->>'lifecycle_gate'='legacy'
                AND ((v_membership.state='pending' AND v_fence.runtime_epoch_high_water=v_membership.runtime_epoch_baseline)
                     OR (v_membership.state='active' AND v_fence.runtime_epoch_high_water>v_membership.runtime_epoch_baseline)))
               OR (v_residency #>> '{identity,audience}'='aap-gpu-lifecycle'
                   AND v_residency #>> '{identity,backend_registry_id}'=v_id::text
                   AND v_residency #>> '{identity,gpu_resource_id}'=v_resource
                   AND v_residency->>'control_epoch'=v_fence.control_epoch_high_water::text
                   AND v_fence.control_epoch_high_water>0
                   AND v_membership.state='active' AND v_fence.runtime_epoch_high_water>v_membership.runtime_epoch_baseline)), false)
           OR (v_args->>'phase' IN ('reserve','authorize','verify','commit')
               AND (v_time<=v_now OR v_time>v_now+interval '5 minutes')) THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','installation_changed');
        ELSIF v_args->>'phase'='reserve' THEN
            IF v_terminal_intent.backend_registry_id IS NOT NULL
               AND v_terminal_intent.request IS DISTINCT FROM v_expected
               AND (NOT v_terminal_intent.settled OR v_terminal_intent.revision IS NOT DISTINCT FROM v_registry.extra_params #> '{plugin,revision}') THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','terminal_intent_conflict');
            ELSIF v_terminal_intent.backend_registry_id IS NOT NULL AND v_terminal_intent.request=v_expected THEN
                IF v_terminal_intent.revision IS DISTINCT FROM v_registry.extra_params #> '{plugin,revision}'
                   OR v_terminal_intent.membership_epoch<>v_membership.membership_epoch
                   OR v_terminal_intent.generation<>v_fence.generation_high_water
                   OR v_terminal_intent.control_epoch<>v_fence.control_epoch_high_water
                   OR v_terminal_intent.runtime_epoch<>v_fence.runtime_epoch_high_water THEN
                    v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','terminal_intent_changed');
                END IF;
            ELSIF v_fence.generation_high_water=9223372036854775807
               OR (v_residency->>'generation' IS NOT NULL AND (v_residency->>'generation')::bigint>v_fence.generation_high_water)
               OR (v_args->>'ledger_source_generation' IS NOT NULL AND (v_args->>'ledger_source_generation')::bigint>v_fence.generation_high_water) THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','generation_changed');
            ELSE
                IF v_terminal_intent.backend_registry_id IS NULL OR v_terminal_intent.request IS DISTINCT FROM v_expected THEN
                    IF v_residency->'identity'='null'::jsonb THEN
                        IF v_fence.control_epoch_high_water=9223372036854775807 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
                        v_fence.control_epoch_high_water:=v_fence.control_epoch_high_water+1;
                        UPDATE public.gpu_backend_fences SET control_epoch_high_water=v_fence.control_epoch_high_water WHERE backend_registry_id=v_id;
                    END IF;
                    IF v_membership.state='pending' THEN
                        IF v_fence.runtime_epoch_high_water=9223372036854775807 THEN PERFORM yoao_inference_gpu_v1._fail(); END IF;
                        v_fence.runtime_epoch_high_water:=v_fence.runtime_epoch_high_water+1;
                        UPDATE public.gpu_backend_fences SET runtime_epoch_high_water=v_fence.runtime_epoch_high_water WHERE backend_registry_id=v_id;
                        UPDATE public.gpu_backend_memberships SET state='active',updated_at=v_now WHERE backend_registry_id=v_id AND gpu_resource_id=v_resource;
                    END IF;
                END IF;
                v_generation:=v_fence.generation_high_water+1;
                INSERT INTO yoao_inference_gpu_v1.installation_terminal_intents AS i
                    (backend_registry_id,request,revision,membership_epoch,generation,source_generation,control_epoch,runtime_epoch,settled)
                VALUES (v_id,v_expected,v_registry.extra_params #> '{plugin,revision}',v_membership.membership_epoch,
                    v_generation,v_args->>'ledger_source_generation',v_fence.control_epoch_high_water,v_fence.runtime_epoch_high_water,false)
                ON CONFLICT (backend_registry_id) DO UPDATE SET request=EXCLUDED.request,revision=EXCLUDED.revision,
                    membership_epoch=EXCLUDED.membership_epoch,generation=EXCLUDED.generation,source_generation=EXCLUDED.source_generation,
                    control_epoch=EXCLUDED.control_epoch,runtime_epoch=EXCLUDED.runtime_epoch,settled=false,
                    authorized_token_expires_at=NULL
                RETURNING * INTO v_terminal_intent;
            END IF;
            IF v_result IS NULL THEN
                UPDATE public.gpu_backend_fences SET generation_high_water=v_terminal_intent.generation,
                    updated_at=v_now WHERE backend_registry_id=v_id;
                v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',
                    pg_catalog.jsonb_build_object('membership_epoch',v_terminal_intent.membership_epoch,
                        'revision',v_terminal_intent.revision,'generation',v_terminal_intent.generation::text,
                        'source_generation',v_terminal_intent.source_generation,'control_epoch',v_terminal_intent.control_epoch::text,
                        'runtime_epoch',v_terminal_intent.runtime_epoch::text,'token_expires_at',yoao_inference_gpu_v1._iso(v_time),'settled',v_terminal_intent.settled));
            END IF;
        ELSIF v_args->>'phase' IN ('authorize','prove_idle') THEN
            IF v_terminal_intent.backend_registry_id IS NULL OR v_terminal_intent.request IS DISTINCT FROM v_expected
               OR v_terminal_intent.revision IS DISTINCT FROM v_registry.extra_params #> '{plugin,revision}'
               OR v_terminal_intent.revision IS DISTINCT FROM v_args->'preparation'->'revision'
               OR v_terminal_intent.membership_epoch IS DISTINCT FROM v_membership.membership_epoch
               OR v_terminal_intent.membership_epoch IS DISTINCT FROM yoao_inference_gpu_v1._int(v_args->'preparation'->'membership_epoch',1,9223372036854775807)
               OR v_terminal_intent.generation::text IS DISTINCT FROM v_args->'preparation'->>'generation'
               OR v_terminal_intent.source_generation IS DISTINCT FROM v_args->'preparation'->>'source_generation'
               OR v_terminal_intent.source_generation IS DISTINCT FROM v_args->>'ledger_source_generation'
               OR v_terminal_intent.control_epoch::text IS DISTINCT FROM v_args->'preparation'->>'control_epoch'
               OR v_terminal_intent.runtime_epoch::text IS DISTINCT FROM v_args->'preparation'->>'runtime_epoch'
               OR v_terminal_intent.settled IS DISTINCT FROM (v_args->'preparation'->>'settled')::boolean
               OR v_fence.generation_high_water IS DISTINCT FROM v_terminal_intent.generation
               OR v_fence.control_epoch_high_water IS DISTINCT FROM v_terminal_intent.control_epoch
               OR v_fence.runtime_epoch_high_water IS DISTINCT FROM v_terminal_intent.runtime_epoch THEN
                v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','terminal_intent_changed');
            ELSIF v_args->>'phase'='prove_idle' THEN
                IF v_terminal_intent.settled OR v_probe_started>v_now
                   OR v_probe_started<v_now-interval '3 minutes'
                   OR (v_fence.token_expiry_high_water IS NOT NULL AND v_probe_started<=v_fence.token_expiry_high_water)
                   OR v_residency->>'generation' IS DISTINCT FROM v_terminal_intent.source_generation
                   OR v_residency->>'active_requests' IS DISTINCT FROM '0'
                   OR v_residency->>'lifecycle_gate' IS DISTINCT FROM 'enforce'
                   OR v_residency->>'state' NOT IN ('resident','unloaded')
                   OR v_residency->'draining' IS DISTINCT FROM 'false'::jsonb
                   OR v_residency->>'builders' IS DISTINCT FROM '0'
                   OR v_residency->>'borrowers' IS DISTINCT FROM '0' THEN
                    v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','terminal_idle_unproven');
                ELSE
                    v_result:=pg_catalog.jsonb_build_object('status','verified');
                END IF;
            ELSE
                UPDATE yoao_inference_gpu_v1.installation_terminal_intents
                SET authorized_token_expires_at=v_time WHERE backend_registry_id=v_id AND NOT settled;
                UPDATE public.gpu_backend_fences SET
                    token_expiry_high_water=GREATEST(COALESCE(token_expiry_high_water,v_time),v_time),
                    updated_at=v_now WHERE backend_registry_id=v_id;
                v_result:=pg_catalog.jsonb_build_object('status','prepared','preparation',
                    pg_catalog.jsonb_set(v_args->'preparation','{token_expires_at}',pg_catalog.to_jsonb(yoao_inference_gpu_v1._iso(v_time))));
            END IF;
        ELSIF v_terminal_intent.backend_registry_id IS NULL OR v_terminal_intent.request IS DISTINCT FROM v_expected
           OR v_terminal_intent.generation::text IS DISTINCT FROM v_args->'preparation'->>'generation'
           OR v_terminal_intent.source_generation IS DISTINCT FROM v_args->'preparation'->>'source_generation'
           OR v_terminal_intent.source_generation IS DISTINCT FROM v_args->>'ledger_source_generation'
           OR v_terminal_intent.settled IS DISTINCT FROM (v_args->'preparation'->>'settled')::boolean
           OR v_membership.membership_epoch IS DISTINCT FROM yoao_inference_gpu_v1._int(v_args->'preparation'->'membership_epoch',1,9223372036854775807)
           OR v_registry.extra_params #> '{plugin,revision}' IS DISTINCT FROM v_args->'preparation'->'revision'
           OR v_fence.generation_high_water::text IS DISTINCT FROM v_args->'preparation'->>'generation'
           OR v_fence.control_epoch_high_water::text IS DISTINCT FROM v_args->'preparation'->>'control_epoch'
           OR v_fence.runtime_epoch_high_water::text IS DISTINCT FROM v_args->'preparation'->>'runtime_epoch'
           OR v_time IS DISTINCT FROM yoao_inference_gpu_v1._timestamp(v_args->'preparation'->'token_expires_at')
           OR v_fence.token_expiry_high_water<v_time
           OR v_residency->>'generation' IS DISTINCT FROM v_args->'preparation'->>'generation'
           OR v_residency->>'lifecycle_gate' IS DISTINCT FROM 'enforce'
           OR v_residency->>'state' IS DISTINCT FROM 'draining'
           OR v_residency->'draining' IS DISTINCT FROM 'true'::jsonb
           OR v_residency->'gpu_loaded' IS DISTINCT FROM 'false'::jsonb
           OR v_residency->'evictable' IS DISTINCT FROM 'false'::jsonb
           OR v_residency->>'active_requests' IS DISTINCT FROM '0'
           OR v_residency->>'builders' IS DISTINCT FROM '0'
           OR v_residency->>'borrowers' IS DISTINCT FROM '0'
           OR EXISTS(SELECT 1 FROM pg_catalog.jsonb_each(v_residency->'pools') p(k,v) WHERE p.v->'resident' IS DISTINCT FROM 'false'::jsonb) THEN
            v_result:=pg_catalog.jsonb_build_object('status','blocked','reason','terminal_proof_changed');
        ELSE
            IF v_args->>'phase'='commit' THEN
                UPDATE yoao_inference_gpu_v1.installation_terminal_intents SET settled=true WHERE backend_registry_id=v_id;
            END IF;
            v_result:=pg_catalog.jsonb_build_object('status','verified');
        END IF;

    ELSE
        PERFORM yoao_inference_gpu_v1._fail();
    END IF;

    v_result:=pg_catalog.jsonb_build_object('protocol','aap-inference-gpu-native/v1','action',p_action,'result',v_result);
    IF pg_catalog.octet_length(v_result::text)>4194304 THEN
        RAISE EXCEPTION 'GPU Native response exceeds bounded size' USING ERRCODE='22023';
    END IF;
    RETURN v_result;
END
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.mask_qc_command(p_request jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET "TimeZone" TO 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_field text;
    v_id uuid;
    v_actor_id uuid;
    v_task_id uuid;
    v_issue public.mask_qc_issues%ROWTYPE;
    v_run public.mask_qc_runs%ROWTYPE;
    v_data jsonb;
    v_rows jsonb := '[]'::jsonb;
    v_statuses jsonb := '{}'::jsonb;
    v_created boolean := false;
    v_source jsonb;
    v_version integer;
    v_current boolean;
    v_requested jsonb;
    v_account jsonb;
    v_lock_reply jsonb;
    v_tracker_job public.video_tracker_jobs%ROWTYPE;
    v_count integer := 0;
    v_counts jsonb := '{}'::jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-quality-data/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Mask QC request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'read_source' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','scope','task_ids','annotation_ids'];
        WHEN 'find_run' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','singleflight_key'];
        WHEN 'create_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','project_id','async_job_id','requested_by_id','scope_json','config_revision','config_digest','config_snapshot','source_snapshot','source_snapshot_digest','task_snapshot_digests','singleflight_key','summary'];
        WHEN 'read_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','lock'];
        WHEN 'read_run_by_job' THEN
            v_expected_keys := ARRAY['schema','operation','async_job_id','lock'];
        WHEN 'begin_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','at'];
        WHEN 'progress_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','progress_pct'];
        WHEN 'complete_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','at','summary'];
        WHEN 'fail_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','at','error_message'];
        WHEN 'cancel_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','at'];
        WHEN 'reconcile_run' THEN
            v_expected_keys := ARRAY['schema','operation','run_id','at','terminal_cancelled','error_message'];
        WHEN 'upsert_issue' THEN
            v_expected_keys := ARRAY['schema','operation','issue'];
        WHEN 'mark_seen' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','run_id','dedupe_keys'];
        WHEN 'read_issue' THEN
            v_expected_keys := ARRAY['schema','operation','issue_id','lock','nowait'];
        WHEN 'read_issues' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','issue_ids'];
        WHEN 'list_issues' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','limit','status','severity','code','task_id','annotation_id','frame','allowed_task_ids','cursor_rank','cursor_created_at','cursor_id'];
        WHEN 'read_task_run_issues' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','run_id'];
        WHEN 'find_latest_task_run' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','task_id'];
        WHEN 'find_current_completed_task_run' THEN
            v_expected_keys := ARRAY['schema','operation','project_id','task_id','config_digest','source_digest'];
        WHEN 'read_open_task_issues' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','severity','issue_ids'];
        WHEN 'decide_issue' THEN
            v_expected_keys := ARRAY['schema','operation','issue_id','expected_task_id','actor_id','status','decided_at'];
        WHEN 'resolve_repaired_issue' THEN
            v_expected_keys := ARRAY['schema','operation','issue_id','actor_id','decided_at'];
        WHEN 'read_issue_statuses' THEN
            v_expected_keys := ARRAY['schema','operation','issue_ids'];
        WHEN 'issue_versions_current' THEN
            v_expected_keys := ARRAY['schema','operation','issue_id'];
        WHEN 'read_annotation_revision' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','annotation_id','annotation_version'];
        WHEN 'read_tracker_candidate_context' THEN
            v_expected_keys := ARRAY['schema','operation','task_id','job_id'];
        WHEN 'read_run_summary' THEN
            v_expected_keys := ARRAY['schema','operation','run_id'];
        ELSE
            RAISE EXCEPTION 'Invalid Mask QC operation' USING ERRCODE = '22023';
    END CASE;
    IF NOT yoao_labeling_v1._quality_exact_keys(p_request, v_expected_keys) THEN
        RAISE EXCEPTION 'Invalid Mask QC request fields' USING ERRCODE = '22023';
    END IF;

    IF v_operation = 'read_source' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'project_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'scope') IS DISTINCT FROM 'string'
           OR p_request ->> 'scope' NOT IN ('project','task_ids','annotation_ids')
           OR pg_catalog.jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'annotation_ids') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(p_request -> 'task_ids') > 1000
           OR pg_catalog.jsonb_array_length(p_request -> 'annotation_ids') > 1000 THEN
            RAISE EXCEPTION 'Invalid Mask QC source scope' USING ERRCODE = '22023';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'task_ids') AS item(value)
            WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
               OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'annotation_ids') AS item(value)
            WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
               OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ) THEN
            RAISE EXCEPTION 'Invalid Mask QC source identifiers' USING ERRCODE = '22023';
        END IF;
        v_id := (p_request ->> 'project_id')::uuid;
        IF p_request ->> 'scope' = 'task_ids' AND EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids') AS item(id)
            LEFT JOIN public.tasks AS task ON task.id = item.id::uuid AND task.project_id = v_id
            WHERE task.id IS NULL
        ) THEN
            RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,
                'request',p_request,'data',pg_catalog.jsonb_build_object('missing_scope',true,'annotations','[]'::jsonb));
        END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
            'id',annotation.id,'project_id',annotation.project_id,'task_id',annotation.task_id,
            'dataset_item_id',task.dataset_item_id,
            'class_name',annotation.class_name,'source',annotation.source,'track_id',annotation.track_id,
            'version',annotation.version,'geometry',annotation.geometry
        ) ORDER BY annotation.task_id,annotation.id),'[]'::jsonb)
        INTO v_rows
        FROM public.annotations AS annotation
        JOIN public.tasks AS task ON task.id=annotation.task_id
        WHERE annotation.project_id = v_id
          AND annotation.is_active IS TRUE
          AND annotation.was_cancelled IS FALSE
          AND annotation.geometry ->> 'type' IN ('raster_mask','video_track_mask')
          AND (p_request ->> 'scope' <> 'task_ids' OR annotation.task_id IN (
              SELECT item.id::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids') AS item(id)))
          AND (p_request ->> 'scope' <> 'annotation_ids' OR annotation.id IN (
              SELECT item.id::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'annotation_ids') AS item(id)));
        IF p_request ->> 'scope' = 'annotation_ids' AND (
            SELECT pg_catalog.count(DISTINCT item.id::uuid)
            FROM pg_catalog.jsonb_array_elements_text(p_request -> 'annotation_ids') AS item(id)
        ) <> pg_catalog.jsonb_array_length(v_rows) THEN
            RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,
                'request',p_request,'data',pg_catalog.jsonb_build_object('missing_scope',true,'annotations','[]'::jsonb));
        END IF;
        RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,
            'request',p_request,'data',pg_catalog.jsonb_build_object('missing_scope',false,'annotations',v_rows));
    ELSIF v_operation = 'find_run' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'project_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'singleflight_key') IS DISTINCT FROM 'string'
           OR p_request ->> 'singleflight_key' !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'Invalid Mask QC singleflight lookup' USING ERRCODE = '22023';
        END IF;
        SELECT run.* INTO v_run FROM public.mask_qc_runs AS run
        WHERE run.project_id = (p_request ->> 'project_id')::uuid
          AND run.singleflight_key = p_request ->> 'singleflight_key'
          AND run.status IN ('pending','running')
        ORDER BY run.created_at DESC LIMIT 1;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_run); ELSE v_data := NULL; END IF;
    ELSIF v_operation = 'create_run' THEN
        FOREACH v_field IN ARRAY ARRAY['run_id','project_id','async_job_id','requested_by_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RAISE EXCEPTION 'Invalid Mask QC run identity' USING ERRCODE = '22023';
            END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'config_revision') IS DISTINCT FROM 'number'
           OR p_request ->> 'config_revision' !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'config_revision')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'config_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'config_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_snapshot_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'source_snapshot_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'singleflight_key') IS DISTINCT FROM 'string'
           OR p_request ->> 'singleflight_key' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'scope_json') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'config_snapshot') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_snapshot') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request -> 'task_snapshot_digests') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'summary') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'Invalid Mask QC run snapshot' USING ERRCODE = '22023';
        END IF;
        v_actor_id := (p_request ->> 'requested_by_id')::uuid;
        v_lock_reply := yoao_identity_v1.lock_active_actor(v_actor_id);
        IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'aap_labeling_quality_error',
                DETAIL = pg_catalog.jsonb_build_object('status_code',403,
                    'detail',pg_catalog.jsonb_build_object('reason','actor_inactive'))::text;
        END IF;
        INSERT INTO public.mask_qc_runs(
            id,project_id,async_job_id,requested_by_id,status,progress_pct,scope_json,
            config_revision,config_digest,config_snapshot,source_snapshot,source_snapshot_digest,
            task_snapshot_digests,singleflight_key,summary
        ) VALUES (
            (p_request ->> 'run_id')::uuid,(p_request ->> 'project_id')::uuid,
            (p_request ->> 'async_job_id')::uuid,v_actor_id,
            'pending',0,p_request -> 'scope_json',(p_request ->> 'config_revision')::integer,
            p_request ->> 'config_digest',p_request -> 'config_snapshot',p_request -> 'source_snapshot',
            p_request ->> 'source_snapshot_digest',p_request -> 'task_snapshot_digests',
            p_request ->> 'singleflight_key',p_request -> 'summary'
        ) ON CONFLICT (project_id,singleflight_key) WHERE status IN ('pending','running') DO NOTHING
        RETURNING public.mask_qc_runs.* INTO v_run;
        v_created := FOUND;
        IF NOT v_created THEN
            SELECT run.* INTO v_run FROM public.mask_qc_runs AS run
            WHERE run.project_id = (p_request ->> 'project_id')::uuid
              AND run.singleflight_key = p_request ->> 'singleflight_key'
              AND run.status IN ('pending','running')
            ORDER BY run.created_at DESC LIMIT 1;
        END IF;
        v_data := pg_catalog.jsonb_build_object('created',v_created,'row',pg_catalog.to_jsonb(v_run));
    ELSIF v_operation IN ('read_run','read_run_by_job') THEN
        v_field := CASE WHEN v_operation='read_run' THEN 'run_id' ELSE 'async_job_id' END;
        IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
           OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean' THEN
            RAISE EXCEPTION 'Invalid Mask QC run lookup' USING ERRCODE = '22023';
        END IF;
        v_id := (p_request ->> v_field)::uuid;
        IF (p_request ->> 'lock')::boolean THEN
            IF v_operation='read_run' THEN
                SELECT run.* INTO v_run FROM public.mask_qc_runs AS run WHERE run.id=v_id FOR UPDATE;
            ELSE
                SELECT run.* INTO v_run FROM public.mask_qc_runs AS run WHERE run.async_job_id=v_id FOR UPDATE;
            END IF;
        ELSE
            IF v_operation='read_run' THEN
                SELECT run.* INTO v_run FROM public.mask_qc_runs AS run WHERE run.id=v_id;
            ELSE
                SELECT run.* INTO v_run FROM public.mask_qc_runs AS run WHERE run.async_job_id=v_id;
            END IF;
        END IF;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_run); ELSE v_data := NULL; END IF;
    ELSIF v_operation IN ('begin_run','progress_run','complete_run','fail_run','cancel_run','reconcile_run') THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'run_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'run_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION 'Invalid Mask QC run id' USING ERRCODE = '22023';
        END IF;
        v_id := (p_request ->> 'run_id')::uuid;
        CASE v_operation
            WHEN 'begin_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask QC start time' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET status='running', started_at=COALESCE(run.started_at,(p_request ->> 'at')::timestamptz),
                    progress_pct=GREATEST(run.progress_pct,1), updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status='pending' RETURNING run.* INTO v_run;
            WHEN 'progress_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'progress_pct') IS DISTINCT FROM 'number'
                   OR p_request ->> 'progress_pct' !~ '^(0|[1-9][0-9]{0,2})$'
                   OR (p_request ->> 'progress_pct')::integer NOT BETWEEN 0 AND 100 THEN RAISE EXCEPTION 'Invalid Mask QC progress' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET progress_pct=(p_request ->> 'progress_pct')::integer, updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status IN ('pending','running') RETURNING run.* INTO v_run;
            WHEN 'complete_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(p_request -> 'summary') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid Mask QC completion' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET status='completed',progress_pct=100,summary=p_request -> 'summary',
                    completed_at=(p_request ->> 'at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status IN ('pending','running') RETURNING run.* INTO v_run;
            WHEN 'fail_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask QC failure' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET status='failed',error_message=pg_catalog.left(p_request ->> 'error_message',4000),
                    completed_at=(p_request ->> 'at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status NOT IN ('completed','cancelled') RETURNING run.* INTO v_run;
            WHEN 'cancel_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask QC cancellation time' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET status='cancelled',completed_at=(p_request ->> 'at')::timestamptz,
                    updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status IN ('pending','running') RETURNING run.* INTO v_run;
            WHEN 'reconcile_run' THEN
                IF pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string'
                   OR pg_catalog.jsonb_typeof(p_request -> 'terminal_cancelled') IS DISTINCT FROM 'boolean'
                   OR pg_catalog.jsonb_typeof(p_request -> 'error_message') NOT IN ('string','null') THEN RAISE EXCEPTION 'Invalid Mask QC terminal reconciliation' USING ERRCODE = '22023'; END IF;
                UPDATE public.mask_qc_runs AS run
                SET status=CASE WHEN (p_request ->> 'terminal_cancelled')::boolean THEN 'cancelled' ELSE 'failed' END,
                    error_message=CASE WHEN (p_request ->> 'terminal_cancelled')::boolean THEN run.error_message ELSE pg_catalog.left(COALESCE(p_request ->> 'error_message',''),4000) END,
                    completed_at=(p_request ->> 'at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                WHERE run.id=v_id AND run.status IN ('pending','running') RETURNING run.* INTO v_run;
        END CASE;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_run); ELSE v_data := NULL; END IF;
    ELSIF v_operation = 'upsert_issue' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'issue') IS DISTINCT FROM 'object'
           OR NOT yoao_labeling_v1._quality_exact_keys(p_request -> 'issue',ARRAY[
            'id','run_id','last_seen_run_id','project_id','task_id','annotation_id','annotation_version',
            'related_annotation_ids','source_versions','code','severity','severity_rank','status',
            'frame_start','frame_end','metric','threshold','region_bbox','region_mask_ref','region_digest',
            'dedupe_key','source','suggestion','updated_at'
           ]) THEN RAISE EXCEPTION 'Invalid Mask QC issue fields' USING ERRCODE = '22023'; END IF;
        FOREACH v_field IN ARRAY ARRAY['id','run_id','last_seen_run_id','project_id','task_id','annotation_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request #> ARRAY['issue',v_field]) IS DISTINCT FROM 'string'
               OR p_request #>> ARRAY['issue',v_field] !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC issue identity' USING ERRCODE = '22023'; END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request #> '{issue,source_versions}') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,related_annotation_ids}') IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,metric}') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,threshold}') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,source}') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,region_bbox}') NOT IN ('object','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,region_mask_ref}') NOT IN ('object','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,region_digest}') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,suggestion}') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,frame_start}') NOT IN ('number','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,frame_end}') NOT IN ('number','null')
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,annotation_version}') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,severity_rank}') IS DISTINCT FROM 'number'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,code}') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,severity}') IS DISTINCT FROM 'string'
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,dedupe_key}') IS DISTINCT FROM 'string'
           OR (p_request #>> '{issue,annotation_version}') !~ '^[1-9][0-9]{0,9}$'
           OR (p_request #>> '{issue,annotation_version}')::bigint > 2147483647
           OR (p_request #>> '{issue,severity_rank}') !~ '^[0-2]$'
           OR ((p_request #>> '{issue,severity}'='blocker') AND (p_request #>> '{issue,severity_rank}')<>'0')
           OR ((p_request #>> '{issue,severity}'='warning') AND (p_request #>> '{issue,severity_rank}')<>'1')
           OR ((p_request #>> '{issue,severity}'='info') AND (p_request #>> '{issue,severity_rank}')<>'2')
           OR (p_request #>> '{issue,frame_start}' IS NOT NULL AND ((p_request #>> '{issue,frame_start}') !~ '^(0|[1-9][0-9]{0,9})$' OR (p_request #>> '{issue,frame_start}')::bigint > 2147483647))
           OR (p_request #>> '{issue,frame_end}' IS NOT NULL AND ((p_request #>> '{issue,frame_end}') !~ '^(0|[1-9][0-9]{0,9})$' OR (p_request #>> '{issue,frame_end}')::bigint > 2147483647))
           OR ((p_request #>> '{issue,frame_start}' IS NULL) <> (p_request #>> '{issue,frame_end}' IS NULL))
           OR (p_request #>> '{issue,frame_start}' IS NOT NULL AND (p_request #>> '{issue,frame_end}')::bigint < (p_request #>> '{issue,frame_start}')::bigint)
           OR p_request #>> '{issue,dedupe_key}' !~ '^[0-9a-f]{64}$'
           OR p_request #>> '{issue,region_digest}' IS NOT NULL AND p_request #>> '{issue,region_digest}' !~ '^[0-9a-f]{64}$'
           OR p_request #>> '{issue,severity}' NOT IN ('blocker','warning','info')
           OR p_request #>> '{issue,status}' IS DISTINCT FROM 'open'
           OR pg_catalog.length(p_request #>> '{issue,code}') NOT BETWEEN 1 AND 64
           OR pg_catalog.jsonb_typeof(p_request #> '{issue,updated_at}') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Mask QC issue value' USING ERRCODE = '22023';
        END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request #> '{issue,related_annotation_ids}') AS item(value)
            WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
               OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        ) THEN RAISE EXCEPTION 'Invalid Mask QC related annotation ids' USING ERRCODE = '22023'; END IF;
        IF EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_each_text(p_request #> '{issue,source_versions}') AS source(id,version)
            WHERE source.id !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR source.version !~ '^[1-9][0-9]{0,9}$'
               OR source.version::bigint > 2147483647
        ) OR (
            p_request #> '{issue,region_mask_ref}' IS DISTINCT FROM 'null'::jsonb
            AND (p_request #> '{issue,region_mask_ref}' ->> 'sha256') IS DISTINCT FROM p_request #>> '{issue,region_digest}'
        ) THEN RAISE EXCEPTION 'Invalid Mask QC issue source versions or region reference' USING ERRCODE = '22023'; END IF;
        INSERT INTO public.mask_qc_issues AS current_issue(
            id,run_id,last_seen_run_id,project_id,task_id,annotation_id,annotation_version,
            related_annotation_ids,source_versions,code,severity,severity_rank,status,
            frame_start,frame_end,metric,threshold,region_bbox,region_mask_ref,region_digest,
            dedupe_key,source,suggestion,updated_at
        ) VALUES (
            (p_request #>> '{issue,id}')::uuid,(p_request #>> '{issue,run_id}')::uuid,
            (p_request #>> '{issue,last_seen_run_id}')::uuid,(p_request #>> '{issue,project_id}')::uuid,
            (p_request #>> '{issue,task_id}')::uuid,(p_request #>> '{issue,annotation_id}')::uuid,
            (p_request #>> '{issue,annotation_version}')::integer,
            ARRAY(SELECT item.value #>> '{}' FROM pg_catalog.jsonb_array_elements(p_request #> '{issue,related_annotation_ids}') AS item(value))::uuid[],
            p_request #> '{issue,source_versions}',p_request #>> '{issue,code}',p_request #>> '{issue,severity}',
            (p_request #>> '{issue,severity_rank}')::smallint,'open',
            (p_request #>> '{issue,frame_start}')::integer,(p_request #>> '{issue,frame_end}')::integer,
            p_request #> '{issue,metric}',p_request #> '{issue,threshold}',p_request #> '{issue,region_bbox}',
            p_request #> '{issue,region_mask_ref}',p_request #>> '{issue,region_digest}',p_request #>> '{issue,dedupe_key}',
            p_request #> '{issue,source}',p_request #>> '{issue,suggestion}',(p_request #>> '{issue,updated_at}')::timestamptz
        ) ON CONFLICT ON CONSTRAINT uq_mask_qc_issues_dedupe DO UPDATE SET
            severity=EXCLUDED.severity,severity_rank=EXCLUDED.severity_rank,
            status=CASE WHEN EXCLUDED.status='stale' THEN 'stale'
                WHEN current_issue.status IN ('resolved','wont_fix') THEN current_issue.status
                ELSE 'open' END,
            metric=EXCLUDED.metric,threshold=EXCLUDED.threshold,region_bbox=EXCLUDED.region_bbox,
            region_mask_ref=EXCLUDED.region_mask_ref,region_digest=EXCLUDED.region_digest,
            source=EXCLUDED.source,suggestion=EXCLUDED.suggestion,updated_at=EXCLUDED.updated_at
        RETURNING current_issue.* INTO v_issue;
        v_data := pg_catalog.to_jsonb(v_issue);
    ELSIF v_operation = 'mark_seen' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'project_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'run_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'run_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'dedupe_keys') IS DISTINCT FROM 'array'
           OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements_text(p_request -> 'dedupe_keys') AS item(value) WHERE item.value !~ '^[0-9a-f]{64}$') THEN
            RAISE EXCEPTION 'Invalid Mask QC seen keys' USING ERRCODE = '22023';
        END IF;
        UPDATE public.mask_qc_issues AS issue SET last_seen_run_id=(p_request ->> 'run_id')::uuid
        WHERE issue.project_id=(p_request ->> 'project_id')::uuid
          AND issue.dedupe_key IN (SELECT value FROM pg_catalog.jsonb_array_elements_text(p_request -> 'dedupe_keys'));
        GET DIAGNOSTICS v_count = ROW_COUNT;
        v_data := pg_catalog.jsonb_build_object('affected',v_count);
    ELSIF v_operation = 'read_issues' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'project_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'issue_ids') IS DISTINCT FROM 'array'
           OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'issue_ids') AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
            RAISE EXCEPTION 'Invalid Mask QC issue list' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(issue) ORDER BY issue.id),'[]'::jsonb)
        INTO v_rows FROM public.mask_qc_issues AS issue
        WHERE issue.project_id=(p_request ->> 'project_id')::uuid
          AND issue.id IN (SELECT (item.value #>> '{}')::uuid FROM pg_catalog.jsonb_array_elements(p_request -> 'issue_ids') AS item(value));
        v_data := v_rows;
    ELSIF v_operation = 'list_issues' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'project_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'limit') IS DISTINCT FROM 'number'
           OR p_request ->> 'limit' !~ '^[1-9][0-9]*$'
           OR (p_request ->> 'limit')::integer NOT BETWEEN 1 AND 201
           OR pg_catalog.jsonb_typeof(p_request -> 'status') NOT IN ('string','null')
           OR (p_request ->> 'status' IS NOT NULL AND p_request ->> 'status' NOT IN ('open','resolved','wont_fix','stale'))
           OR pg_catalog.jsonb_typeof(p_request -> 'severity') NOT IN ('string','null')
           OR (p_request ->> 'severity' IS NOT NULL AND p_request ->> 'severity' NOT IN ('blocker','warning','info'))
           OR pg_catalog.jsonb_typeof(p_request -> 'code') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_request -> 'task_id') NOT IN ('string','null')
           OR (p_request ->> 'task_id' IS NOT NULL AND p_request ->> 'task_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_request -> 'annotation_id') NOT IN ('string','null')
           OR (p_request ->> 'annotation_id' IS NOT NULL AND p_request ->> 'annotation_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_request -> 'frame') NOT IN ('number','null')
           OR (p_request ->> 'frame' IS NOT NULL AND (p_request ->> 'frame' !~ '^(0|[1-9][0-9]{0,9})$' OR (p_request ->> 'frame')::bigint > 2147483647))
           OR pg_catalog.jsonb_typeof(p_request -> 'allowed_task_ids') NOT IN ('array','null')
           OR pg_catalog.jsonb_typeof(p_request -> 'cursor_rank') NOT IN ('number','null')
           OR pg_catalog.jsonb_typeof(p_request -> 'cursor_created_at') NOT IN ('string','null')
           OR pg_catalog.jsonb_typeof(p_request -> 'cursor_id') NOT IN ('string','null')
           OR (p_request ->> 'cursor_rank' IS NOT NULL AND p_request ->> 'cursor_rank' !~ '^[0-2]$')
           OR (p_request ->> 'cursor_id' IS NOT NULL AND p_request ->> 'cursor_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR ((p_request ->> 'cursor_rank' IS NULL) <> (p_request ->> 'cursor_created_at' IS NULL))
           OR ((p_request ->> 'cursor_rank' IS NULL) <> (p_request ->> 'cursor_id' IS NULL))
           OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(CASE WHEN pg_catalog.jsonb_typeof(p_request -> 'allowed_task_ids')='array' THEN p_request -> 'allowed_task_ids' ELSE '[]'::jsonb END) AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
            RAISE EXCEPTION 'Invalid Mask QC issue listing request' USING ERRCODE = '22023';
        END IF;
        WITH current_issue AS (
            SELECT issue.*,
                CASE WHEN issue.status='stale' OR EXISTS (
                    SELECT 1 FROM pg_catalog.jsonb_each_text(COALESCE(issue.source_versions,pg_catalog.jsonb_build_object(issue.annotation_id::text,issue.annotation_version))) AS source(id,version)
                    LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
                    WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                       OR annotation.version <> source.version::integer
                ) THEN 'stale' ELSE issue.status END AS effective_status
            FROM public.mask_qc_issues AS issue
            WHERE issue.project_id=(p_request ->> 'project_id')::uuid
              AND (p_request ->> 'severity' IS NULL OR issue.severity=p_request ->> 'severity')
              AND (p_request ->> 'code' IS NULL OR issue.code=p_request ->> 'code')
              AND (p_request ->> 'task_id' IS NULL OR issue.task_id=(p_request ->> 'task_id')::uuid)
              AND (p_request ->> 'annotation_id' IS NULL OR issue.annotation_id=(p_request ->> 'annotation_id')::uuid)
              AND (p_request ->> 'frame' IS NULL OR (issue.frame_start <= (p_request ->> 'frame')::integer AND issue.frame_end >= (p_request ->> 'frame')::integer))
              AND (p_request -> 'allowed_task_ids'='null'::jsonb OR issue.task_id IN (
                  SELECT (item.value #>> '{}')::uuid FROM pg_catalog.jsonb_array_elements(p_request -> 'allowed_task_ids') AS item(value)))
        )
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(row_data) ORDER BY row_data.severity_rank,row_data.created_at DESC,row_data.id DESC),'[]'::jsonb)
        INTO v_rows FROM (
            SELECT current_issue.* FROM current_issue
            WHERE (p_request ->> 'status' IS NULL
                   OR (p_request ->> 'status'='stale' AND current_issue.effective_status='stale')
                   OR (p_request ->> 'status'<>'stale' AND current_issue.effective_status=p_request ->> 'status'))
              AND (p_request ->> 'cursor_rank' IS NULL
                   OR current_issue.severity_rank > (p_request ->> 'cursor_rank')::integer
                   OR (current_issue.severity_rank=(p_request ->> 'cursor_rank')::integer AND current_issue.created_at < (p_request ->> 'cursor_created_at')::timestamptz)
                   OR (current_issue.severity_rank=(p_request ->> 'cursor_rank')::integer AND current_issue.created_at=(p_request ->> 'cursor_created_at')::timestamptz AND current_issue.id<(p_request ->> 'cursor_id')::uuid))
            ORDER BY current_issue.severity_rank,current_issue.created_at DESC,current_issue.id DESC
            LIMIT (p_request ->> 'limit')::integer
        ) AS row_data;
        v_data := v_rows;
    ELSIF v_operation = 'read_task_run_issues' THEN
        FOREACH v_field IN ARRAY ARRAY['task_id','run_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC task run lookup' USING ERRCODE = '22023'; END IF;
        END LOOP;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(issue) ORDER BY issue.id),'[]'::jsonb)
        INTO v_rows FROM public.mask_qc_issues AS issue
        WHERE issue.task_id=(p_request ->> 'task_id')::uuid
          AND issue.last_seen_run_id=(p_request ->> 'run_id')::uuid;
        v_data := v_rows;
    ELSIF v_operation = 'find_latest_task_run' THEN
        FOREACH v_field IN ARRAY ARRAY['project_id','task_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC task run lookup' USING ERRCODE = '22023'; END IF;
        END LOOP;
        SELECT run.* INTO v_run FROM public.mask_qc_runs AS run
        WHERE run.project_id=(p_request ->> 'project_id')::uuid
          AND run.task_snapshot_digests ? (p_request ->> 'task_id')
        ORDER BY run.created_at DESC,run.id DESC LIMIT 1;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_run); ELSE v_data := NULL; END IF;
    ELSIF v_operation = 'find_current_completed_task_run' THEN
        FOREACH v_field IN ARRAY ARRAY['project_id','task_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC task run lookup' USING ERRCODE = '22023'; END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'config_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'config_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'source_digest' !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'Invalid Mask QC task run digest' USING ERRCODE = '22023'; END IF;
        SELECT run.* INTO v_run FROM public.mask_qc_runs AS run
        WHERE run.project_id=(p_request ->> 'project_id')::uuid
          AND run.status='completed'
          AND run.config_digest=p_request ->> 'config_digest'
          AND run.task_snapshot_digests ->> (p_request ->> 'task_id')=p_request ->> 'source_digest'
        ORDER BY run.completed_at DESC,run.id DESC LIMIT 1;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_run); ELSE v_data := NULL; END IF;
    ELSIF v_operation = 'read_open_task_issues' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'task_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'severity') NOT IN ('string','null')
           OR (p_request ->> 'severity' IS NOT NULL AND p_request ->> 'severity' NOT IN ('blocker','warning','info'))
           OR pg_catalog.jsonb_typeof(p_request -> 'issue_ids') NOT IN ('array','null')
           OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(CASE WHEN pg_catalog.jsonb_typeof(p_request -> 'issue_ids')='array' THEN p_request -> 'issue_ids' ELSE '[]'::jsonb END) AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string'
                  OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN RAISE EXCEPTION 'Invalid Mask QC open issue query' USING ERRCODE = '22023'; END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(issue) ORDER BY issue.id),'[]'::jsonb)
        INTO v_rows FROM public.mask_qc_issues AS issue
        WHERE issue.task_id=(p_request ->> 'task_id')::uuid
          AND issue.status='open'
          AND (p_request ->> 'severity' IS NULL OR issue.severity=p_request ->> 'severity')
          AND (p_request -> 'issue_ids'='null'::jsonb OR issue.id IN (
              SELECT (item.value #>> '{}')::uuid FROM pg_catalog.jsonb_array_elements(p_request -> 'issue_ids') AS item(value)))
          AND NOT EXISTS (
              SELECT 1 FROM pg_catalog.jsonb_each_text(COALESCE(issue.source_versions,pg_catalog.jsonb_build_object(issue.annotation_id::text,issue.annotation_version))) AS source(id,version)
              LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
              WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                 OR annotation.version <> source.version::integer
          );
        v_data := v_rows;
    ELSIF v_operation IN ('read_issue','decide_issue','resolve_repaired_issue') THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'issue_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'issue_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC issue id' USING ERRCODE = '22023'; END IF;
        v_id := (p_request ->> 'issue_id')::uuid;
        IF v_operation = 'read_issue' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean'
               OR pg_catalog.jsonb_typeof(p_request -> 'nowait') IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'Invalid Mask QC issue lock' USING ERRCODE = '22023'; END IF;
            IF (p_request ->> 'lock')::boolean THEN
                IF (p_request ->> 'nowait')::boolean THEN
                    SELECT issue.* INTO v_issue FROM public.mask_qc_issues AS issue WHERE issue.id=v_id FOR UPDATE NOWAIT;
                ELSE
                    SELECT issue.* INTO v_issue FROM public.mask_qc_issues AS issue WHERE issue.id=v_id FOR UPDATE;
                END IF;
            ELSE
                SELECT issue.* INTO v_issue FROM public.mask_qc_issues AS issue WHERE issue.id=v_id;
            END IF;
            IF FOUND THEN v_data := pg_catalog.to_jsonb(v_issue); ELSE v_data := NULL; END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'actor_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'decided_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask QC decision actor or time' USING ERRCODE = '22023'; END IF;
            v_actor_id := (p_request ->> 'actor_id')::uuid;
            v_lock_reply := yoao_identity_v1.lock_active_actor(v_actor_id);
            IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
                RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,'request',p_request,
                    'data',pg_catalog.jsonb_build_object('changed',false,'reason','actor_inactive'));
            END IF;
            SELECT issue.* INTO v_issue FROM public.mask_qc_issues AS issue WHERE issue.id=v_id FOR UPDATE NOWAIT;
            IF NOT FOUND THEN v_data := pg_catalog.jsonb_build_object('changed',false,'reason','issue_missing');
            ELSE
                IF v_operation='decide_issue' THEN
                    IF pg_catalog.jsonb_typeof(p_request -> 'expected_task_id') IS DISTINCT FROM 'string'
                       OR p_request ->> 'expected_task_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                       OR pg_catalog.jsonb_typeof(p_request -> 'status') IS DISTINCT FROM 'string'
                       OR p_request ->> 'status' NOT IN ('open','resolved','wont_fix') THEN RAISE EXCEPTION 'Invalid Mask QC decision' USING ERRCODE = '22023'; END IF;
                    v_current := v_issue.task_id=(p_request ->> 'expected_task_id')::uuid
                        AND NOT EXISTS (
                            SELECT 1 FROM pg_catalog.jsonb_each_text(COALESCE(v_issue.source_versions,pg_catalog.jsonb_build_object(v_issue.annotation_id::text,v_issue.annotation_version))) AS source(id,version)
                            LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
                            WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                               OR annotation.version <> source.version::integer
                        );
                    IF NOT v_current THEN v_data := pg_catalog.jsonb_build_object('changed',false,'reason','issue_stale');
                    ELSE
                        UPDATE public.mask_qc_issues AS issue SET status=p_request ->> 'status',
                            resolved_by_id=CASE WHEN p_request ->> 'status'='open' THEN NULL ELSE v_actor_id END,
                            resolved_at=CASE WHEN p_request ->> 'status'='open' THEN NULL ELSE (p_request ->> 'decided_at')::timestamptz END,
                            updated_at=pg_catalog.transaction_timestamp()
                        WHERE issue.id=v_id RETURNING issue.* INTO v_issue;
                        v_data := pg_catalog.jsonb_build_object('changed',true,'row',pg_catalog.to_jsonb(v_issue));
                    END IF;
                ELSE
                    UPDATE public.mask_qc_issues AS issue SET status='resolved',resolved_by_id=v_actor_id,
                        resolved_at=(p_request ->> 'decided_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                    WHERE issue.id=v_id RETURNING issue.* INTO v_issue;
                    v_data := pg_catalog.jsonb_build_object('changed',FOUND,'row',CASE WHEN FOUND THEN pg_catalog.to_jsonb(v_issue) ELSE NULL END);
                END IF;
            END IF;
        END IF;
    ELSIF v_operation = 'read_issue_statuses' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'issue_ids') IS DISTINCT FROM 'array'
           OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(p_request -> 'issue_ids') AS item(value)
               WHERE pg_catalog.jsonb_typeof(item.value) IS DISTINCT FROM 'string' OR item.value #>> '{}' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN
            RAISE EXCEPTION 'Invalid Mask QC issue ids' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(pg_catalog.jsonb_object_agg(issue.id::text,
            CASE WHEN issue.status='stale' OR EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_each_text(COALESCE(issue.source_versions,pg_catalog.jsonb_build_object(issue.annotation_id::text,issue.annotation_version))) AS source(id,version)
                LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
                WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                   OR annotation.version <> source.version::integer
            ) THEN 'stale' ELSE issue.status END),'{}'::jsonb)
        INTO v_statuses FROM public.mask_qc_issues AS issue
        WHERE issue.id IN (SELECT (item.value #>> '{}')::uuid FROM pg_catalog.jsonb_array_elements(p_request -> 'issue_ids') AS item(value));
        v_data := v_statuses;
    ELSIF v_operation = 'issue_versions_current' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'issue_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'issue_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC issue id' USING ERRCODE = '22023'; END IF;
        SELECT EXISTS (
            SELECT 1 FROM public.mask_qc_issues AS issue
            WHERE issue.id=(p_request ->> 'issue_id')::uuid
              AND (SELECT pg_catalog.count(*) FROM pg_catalog.jsonb_object_keys(COALESCE(issue.source_versions,'{}'::jsonb)) AS object_keys(key)) > 0
              AND NOT EXISTS (
                  SELECT 1 FROM pg_catalog.jsonb_each_text(issue.source_versions) AS source(id,version)
                  LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
                  WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                     OR annotation.version <> source.version::integer
              )
        ) INTO v_current;
        v_data := pg_catalog.to_jsonb(v_current);
    ELSIF v_operation = 'read_annotation_revision' THEN
        FOREACH v_field IN ARRAY ARRAY['task_id','annotation_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask revision identity' USING ERRCODE = '22023'; END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'annotation_version') IS DISTINCT FROM 'number'
           OR p_request ->> 'annotation_version' !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'annotation_version')::bigint > 2147483647 THEN RAISE EXCEPTION 'Invalid Mask revision version' USING ERRCODE = '22023'; END IF;
        SELECT pg_catalog.jsonb_build_object(
            'geometry',revision.geometry,'source_kind',revision.source_kind,
            'expires_at',revision.expires_at
        )
        INTO v_data FROM public.mask_annotation_revisions AS revision
        WHERE revision.task_id=(p_request ->> 'task_id')::uuid
          AND revision.annotation_id=(p_request ->> 'annotation_id')::uuid
          AND revision.annotation_version=(p_request ->> 'annotation_version')::integer;
    ELSIF v_operation = 'read_tracker_candidate_context' THEN
        FOREACH v_field IN ARRAY ARRAY['task_id','job_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid tracker candidate context identity' USING ERRCODE = '22023'; END IF;
        END LOOP;
        SELECT job.id,job.task_id,job.revision,job.staged_result,job.annotation_id,job.prompt
        INTO v_tracker_job FROM public.video_tracker_jobs AS job
        WHERE job.id=(p_request ->> 'job_id')::uuid
          AND job.task_id=(p_request ->> 'task_id')::uuid;
        IF FOUND THEN
            v_data := pg_catalog.jsonb_build_object(
                'id',v_tracker_job.id,'task_id',v_tracker_job.task_id,
                'revision',v_tracker_job.revision,'staged_result',v_tracker_job.staged_result,
                'annotation_id',v_tracker_job.annotation_id,'prompt',v_tracker_job.prompt
            );
        ELSE v_data := NULL; END IF;
    ELSIF v_operation = 'read_run_summary' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'run_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'run_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask QC run id' USING ERRCODE = '22023'; END IF;
        SELECT pg_catalog.jsonb_build_object(
            'issue_count',COALESCE(pg_catalog.sum(summary.count),0),
            'issues_by_severity',pg_catalog.jsonb_build_object(
                'blocker',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.severity='blocker'),0),
                'warning',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.severity='warning'),0),
                'info',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.severity='info'),0)),
            'issues_by_status',pg_catalog.jsonb_build_object(
                'open',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.effective_status='open'),0),
                'resolved',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.effective_status='resolved'),0),
                'wont_fix',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.effective_status='wont_fix'),0),
                'stale',COALESCE(pg_catalog.sum(summary.count) FILTER (WHERE summary.effective_status='stale'),0)))
        INTO v_counts FROM (
            SELECT issue.severity,
                CASE WHEN issue.status='stale' OR EXISTS (
                    SELECT 1 FROM pg_catalog.jsonb_each_text(COALESCE(issue.source_versions,pg_catalog.jsonb_build_object(issue.annotation_id::text,issue.annotation_version))) AS source(id,version)
                    LEFT JOIN public.annotations AS annotation ON annotation.id=source.id::uuid
                    WHERE annotation.id IS NULL OR annotation.is_active IS NOT TRUE OR annotation.was_cancelled IS TRUE
                       OR annotation.version <> source.version::integer
                ) THEN 'stale' ELSE issue.status END AS effective_status,
                pg_catalog.count(*)::integer AS count
            FROM public.mask_qc_issues AS issue
            WHERE issue.last_seen_run_id=(p_request ->> 'run_id')::uuid
            GROUP BY issue.severity,effective_status
        ) AS summary;
        SELECT run.* INTO v_run FROM public.mask_qc_runs AS run WHERE run.id=(p_request ->> 'run_id')::uuid;
        IF FOUND THEN
            v_data := COALESCE(v_run.summary,'{}'::jsonb) || v_counts;
        ELSE
            v_data := NULL;
        END IF;
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,'request',p_request,'data',v_data);
EXCEPTION
    WHEN invalid_text_representation OR invalid_datetime_format OR datetime_field_overflow OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'Invalid Mask QC request value' USING ERRCODE = '22023';
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.mask_repair_command(p_request jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
 SET "TimeZone" TO 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_field text;
    v_id uuid;
    v_batch public.mask_repair_batches%ROWTYPE;
    v_scope public.mask_review_scopes%ROWTYPE;
    v_segment public.video_segments%ROWTYPE;
    v_data jsonb;
    v_rows jsonb := '[]'::jsonb;
    v_actor_id uuid;
    v_lock_reply jsonb;
    v_count integer := 0;
    v_task public.tasks%ROWTYPE;
    v_annotation public.annotations%ROWTYPE;
    v_issue public.mask_qc_issues%ROWTYPE;
    v_tracker_job public.video_tracker_jobs%ROWTYPE;
    v_existing jsonb;
BEGIN
    IF pg_catalog.jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-quality-data/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Mask repair request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    CASE v_operation
        WHEN 'create_batch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','project_id','requested_by_id','token_hash','plan_digest','request_json','plan_json','result_json','receipt_expires_at'];
        WHEN 'read_batch_by_id' THEN v_expected_keys := ARRAY['schema','operation','batch_id','lock'];
        WHEN 'read_batch_for_source' THEN v_expected_keys := ARRAY['schema','operation','batch_id','lock'];
        WHEN 'read_batch_by_token' THEN v_expected_keys := ARRAY['schema','operation','token_hash','lock'];
        WHEN 'read_batch_by_job' THEN v_expected_keys := ARRAY['schema','operation','job_id','terminal_kind','lock'];
        WHEN 'attach_job' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id'];
        WHEN 'attach_rollback_job' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id'];
        WHEN 'resume_batch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','actor_id'];
        WHEN 'begin_execution' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','at'];
        WHEN 'begin_rollback' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id'];
        WHEN 'record_shard' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','shard_id','result_value','rollback_expires_at'];
        WHEN 'record_rollback_shard' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id','shard_id','result_value'];
        WHEN 'record_candidate' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','item_index','result_value'];
        WHEN 'record_shard_failure' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','shard_id','task_id','error_message'];
        WHEN 'finish_execution' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','status','completed_at'];
        WHEN 'finish_rollback' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id','rolled_back_at','result_json'];
        WHEN 'fail_rollback' THEN v_expected_keys := ARRAY['schema','operation','batch_id','rollback_job_id','error_message','result_json','failed_at'];
        WHEN 'fail_dispatch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','error_message'];
        WHEN 'fail_rollback_dispatch' THEN v_expected_keys := ARRAY['schema','operation','batch_id','async_job_id','error_message'];
        WHEN 'cancel_pending' THEN v_expected_keys := ARRAY['schema','operation','batch_id','status','completed_at'];
        WHEN 'reconcile_terminal' THEN v_expected_keys := ARRAY['schema','operation','job_id','terminal_kind','terminal_cancelled','status','completed_at'];
        WHEN 'insert_review_scope' THEN v_expected_keys := ARRAY['schema','operation','scope_id','project_id','task_id','annotation_id','qc_issue_id','source_job_id','expected_segment_id','reviewer_id','source_annotation_version','result_annotation_version','source_job_revision','frame_start','frame_end','region_mask_ref','region_digest','candidate_digest','decision'];
        WHEN 'read_review_scopes' THEN v_expected_keys := ARRAY['schema','operation','annotation_id','result_annotation_version','frame_start','frame_end','lock'];
        WHEN 'read_repair_targets' THEN v_expected_keys := ARRAY['schema','operation','issue_id','include_related'];
        WHEN 'read_video_segment' THEN v_expected_keys := ARRAY['schema','operation','dataset_item_id','frame_index','requested_id'];
        ELSE RAISE EXCEPTION 'Invalid Mask repair operation' USING ERRCODE = '22023';
    END CASE;
    IF NOT yoao_labeling_v1._quality_exact_keys(p_request, v_expected_keys) THEN
        RAISE EXCEPTION 'Invalid Mask repair request fields' USING ERRCODE = '22023';
    END IF;

    IF v_operation = 'create_batch' THEN
        FOREACH v_field IN ARRAY ARRAY['batch_id','project_id','requested_by_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RAISE EXCEPTION 'Invalid Mask repair identity' USING ERRCODE = '22023';
            END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'token_hash') IS DISTINCT FROM 'string'
           OR p_request ->> 'token_hash' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'plan_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'plan_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'request_json') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'plan_json') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'result_json') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'receipt_expires_at') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Mask repair plan' USING ERRCODE = '22023';
        END IF;
        v_actor_id := (p_request ->> 'requested_by_id')::uuid;
        v_lock_reply := yoao_identity_v1.lock_active_actor(v_actor_id);
        IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,
                'request',p_request,'data',pg_catalog.jsonb_build_object('row',NULL,'reason','actor_inactive'));
        END IF;
        INSERT INTO public.mask_repair_batches(
            id,project_id,requested_by_id,token_hash,status,plan_digest,request_json,plan_json,result_json,receipt_expires_at
        ) VALUES (
            (p_request ->> 'batch_id')::uuid,(p_request ->> 'project_id')::uuid,v_actor_id,
            p_request ->> 'token_hash','planned',p_request ->> 'plan_digest',p_request -> 'request_json',
            p_request -> 'plan_json',p_request -> 'result_json',(p_request ->> 'receipt_expires_at')::timestamptz
        ) RETURNING public.mask_repair_batches.* INTO v_batch;
        v_data := pg_catalog.to_jsonb(v_batch);
    ELSIF v_operation IN ('read_batch_by_id','read_batch_for_source','read_batch_by_token','read_batch_by_job') THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'Invalid Mask repair lock' USING ERRCODE = '22023'; END IF;
        IF v_operation IN ('read_batch_by_id','read_batch_for_source') THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'batch_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair batch id' USING ERRCODE = '22023'; END IF;
            IF (p_request ->> 'lock')::boolean THEN
                IF v_operation='read_batch_for_source' THEN
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.id=(p_request ->> 'batch_id')::uuid FOR SHARE NOWAIT;
                ELSE
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.id=(p_request ->> 'batch_id')::uuid FOR UPDATE;
                END IF;
            ELSE
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.id=(p_request ->> 'batch_id')::uuid;
            END IF;
        ELSIF v_operation='read_batch_by_token' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'token_hash') IS DISTINCT FROM 'string'
               OR p_request ->> 'token_hash' !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'Invalid Mask repair receipt' USING ERRCODE = '22023'; END IF;
            IF (p_request ->> 'lock')::boolean THEN
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.token_hash=p_request ->> 'token_hash' FOR UPDATE;
            ELSE
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.token_hash=p_request ->> 'token_hash';
            END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(p_request -> 'job_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'terminal_kind') IS DISTINCT FROM 'string'
               OR p_request ->> 'terminal_kind' NOT IN ('mask_repair','mask_repair_rollback') THEN RAISE EXCEPTION 'Invalid Mask repair job lookup' USING ERRCODE = '22023'; END IF;
            IF (p_request ->> 'lock')::boolean THEN
                IF p_request ->> 'terminal_kind'='mask_repair_rollback' THEN
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.rollback_async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
                ELSE
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
                END IF;
            ELSE
                IF p_request ->> 'terminal_kind'='mask_repair_rollback' THEN
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.rollback_async_job_id=(p_request ->> 'job_id')::uuid;
                ELSE
                    SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.async_job_id=(p_request ->> 'job_id')::uuid;
                END IF;
            END IF;
        END IF;
        IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); ELSE v_data := NULL; END IF;
    ELSIF v_operation IN ('attach_job','attach_rollback_job','resume_batch','begin_execution','begin_rollback',
        'record_shard','record_rollback_shard','record_candidate','record_shard_failure','finish_execution','finish_rollback',
        'fail_rollback','fail_dispatch','fail_rollback_dispatch','cancel_pending','reconcile_terminal') THEN
        IF v_operation='reconcile_terminal' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'job_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(p_request -> 'terminal_kind') IS DISTINCT FROM 'string'
               OR p_request ->> 'terminal_kind' NOT IN ('mask_repair','mask_repair_rollback')
               OR pg_catalog.jsonb_typeof(p_request -> 'terminal_cancelled') IS DISTINCT FROM 'boolean'
               OR pg_catalog.jsonb_typeof(p_request -> 'status') IS DISTINCT FROM 'string'
               OR p_request ->> 'status' NOT IN ('cancelled','partial','failed','rollback_failed')
               OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair terminal state' USING ERRCODE = '22023'; END IF;
            IF p_request ->> 'terminal_kind'='mask_repair_rollback' THEN
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.rollback_async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
            ELSE
                SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.async_job_id=(p_request ->> 'job_id')::uuid FOR UPDATE;
            END IF;
            IF FOUND AND (
                (p_request ->> 'terminal_kind'='mask_repair_rollback' AND v_batch.status='rolling_back')
                OR (p_request ->> 'terminal_kind'='mask_repair' AND v_batch.status IN ('pending','running'))
            ) THEN
                UPDATE public.mask_repair_batches AS batch SET status=p_request ->> 'status',completed_at=(p_request ->> 'completed_at')::timestamptz,
                    updated_at=pg_catalog.transaction_timestamp() WHERE batch.id=v_batch.id RETURNING batch.* INTO v_batch;
                v_data := pg_catalog.to_jsonb(v_batch);
            ELSE v_data := NULL; END IF;
        ELSE
            IF pg_catalog.jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
               OR p_request ->> 'batch_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair batch id' USING ERRCODE = '22023'; END IF;
            v_id := (p_request ->> 'batch_id')::uuid;
            SELECT batch.* INTO v_batch FROM public.mask_repair_batches AS batch WHERE batch.id=v_id FOR UPDATE;
            IF NOT FOUND THEN v_data := NULL;
            ELSE
                CASE v_operation
                    WHEN 'attach_job' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair job id' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET async_job_id=(p_request ->> 'async_job_id')::uuid,status='pending',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.status='planned' RETURNING batch.* INTO v_batch;
                    WHEN 'attach_rollback_job' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair rollback job id' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET rollback_async_job_id=(p_request ->> 'rollback_job_id')::uuid,status='rolling_back',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.status IN ('completed','partial','rollback_failed') RETURNING batch.* INTO v_batch;
                    WHEN 'resume_batch' THEN
                        FOREACH v_field IN ARRAY ARRAY['async_job_id','actor_id'] LOOP
                            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
                               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair resume identity' USING ERRCODE = '22023'; END IF;
                        END LOOP;
                        v_actor_id := (p_request ->> 'actor_id')::uuid;
                        v_lock_reply := yoao_identity_v1.lock_active_actor(v_actor_id);
                        IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN v_data := pg_catalog.jsonb_build_object('changed',false,'reason','actor_inactive');
                        ELSE
                            UPDATE public.mask_repair_batches AS batch SET async_job_id=(p_request ->> 'async_job_id')::uuid,requested_by_id=v_actor_id,status='pending',updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id AND batch.status IN ('failed','partial') RETURNING batch.* INTO v_batch;
                            IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); ELSE v_data := NULL; END IF;
                        END IF;
                    WHEN 'begin_execution' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair execution binding' USING ERRCODE = '22023'; END IF;
                        PERFORM (p_request ->> 'at')::timestamptz;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid AND v_batch.status IN ('pending','running') THEN
                            UPDATE public.mask_repair_batches AS batch SET status='running',updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        END IF;
                    WHEN 'begin_rollback' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask repair rollback binding' USING ERRCODE = '22023'; END IF;
                        IF v_batch.rollback_async_job_id IS DISTINCT FROM (p_request ->> 'rollback_job_id')::uuid THEN
                            v_data := pg_catalog.jsonb_build_object('changed',false);
                        END IF;
                    WHEN 'record_shard' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'shard_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'shard_id' !~ '^[0-9a-f]{24}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value') IS DISTINCT FROM 'object'
                           OR pg_catalog.jsonb_typeof(p_request -> 'rollback_expires_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair shard result' USING ERRCODE = '22023'; END IF;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid THEN
                            UPDATE public.mask_repair_batches AS batch SET
                                result_json=COALESCE(batch.result_json,'{}'::jsonb) || pg_catalog.jsonb_build_object('shards',COALESCE(batch.result_json -> 'shards','{}'::jsonb) || pg_catalog.jsonb_build_object(p_request ->> 'shard_id',p_request -> 'result_value')),
                                rollback_expires_at=(p_request ->> 'rollback_expires_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                    WHEN 'record_rollback_shard' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'shard_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'shard_id' !~ '^[0-9a-f]{24}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid Mask repair rollback shard' USING ERRCODE = '22023'; END IF;
                        IF NOT yoao_labeling_v1._quality_exact_keys(p_request -> 'result_value', ARRAY['status','mutation_count','versions','completed_at'])
                           AND NOT yoao_labeling_v1._quality_exact_keys(p_request -> 'result_value', ARRAY['status','mutation_count','versions','completed_at','annotation_facts']) THEN RAISE EXCEPTION 'Invalid Mask repair rollback checkpoint' USING ERRCODE = '22023'; END IF;
                        IF p_request -> 'result_value' ->> 'status' IS DISTINCT FROM 'completed'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value' -> 'mutation_count') IS DISTINCT FROM 'number'
                           OR p_request -> 'result_value' ->> 'mutation_count' !~ '^(0|[1-9][0-9]*)$'
                           OR (p_request -> 'result_value' ->> 'mutation_count')::bigint > 2147483647
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value' -> 'versions') IS DISTINCT FROM 'object'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value' -> 'completed_at') IS DISTINCT FROM 'string'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value' -> 'annotation_facts') NOT IN ('object','null') THEN RAISE EXCEPTION 'Invalid Mask repair rollback checkpoint' USING ERRCODE = '22023'; END IF;
                        PERFORM (p_request -> 'result_value' ->> 'completed_at')::timestamptz;
                        IF v_batch.rollback_async_job_id IS DISTINCT FROM (p_request ->> 'rollback_job_id')::uuid
                           OR v_batch.status IS DISTINCT FROM 'rolling_back' THEN
                            v_data := pg_catalog.jsonb_build_object('changed',false);
                        ELSIF NOT EXISTS (
                            SELECT 1
                            FROM pg_catalog.jsonb_array_elements(COALESCE(v_batch.plan_json -> 'shards','[]'::jsonb)) AS plan_shard(value)
                            WHERE value ->> 'id' = p_request ->> 'shard_id'
                        ) THEN
                            RAISE EXCEPTION 'Invalid Mask repair rollback shard plan' USING ERRCODE = '22023';
                        ELSE
                            v_existing := v_batch.result_json -> 'rollback' -> 'shards' -> (p_request ->> 'shard_id');
                            IF (v_existing ->> 'status') = 'completed' THEN
                                IF v_existing IS NOT DISTINCT FROM (p_request -> 'result_value') THEN
                                    v_data := pg_catalog.to_jsonb(v_batch);
                                ELSE
                                    v_data := pg_catalog.jsonb_build_object('changed',false);
                                END IF;
                            ELSE
                                UPDATE public.mask_repair_batches AS batch SET
                                    result_json=COALESCE(batch.result_json,'{}'::jsonb) || pg_catalog.jsonb_build_object('rollback',
                                        COALESCE(batch.result_json -> 'rollback','{}'::jsonb) || pg_catalog.jsonb_build_object('status','running','shards',
                                            COALESCE(batch.result_json -> 'rollback' -> 'shards','{}'::jsonb) || pg_catalog.jsonb_build_object(p_request ->> 'shard_id',p_request -> 'result_value'))),
                                    updated_at=pg_catalog.transaction_timestamp()
                                WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                            END IF;
                        END IF;
                    WHEN 'record_candidate' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'item_index') IS DISTINCT FROM 'number'
                           OR p_request ->> 'item_index' !~ '^(0|[1-9][0-9]*)$'
                           OR (p_request ->> 'item_index')::bigint > 2147483647
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_value') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid Mask repair candidate' USING ERRCODE = '22023'; END IF;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid THEN
                            UPDATE public.mask_repair_batches AS batch SET
                                result_json=COALESCE(batch.result_json,'{}'::jsonb) || pg_catalog.jsonb_build_object('candidates',COALESCE(batch.result_json -> 'candidates','{}'::jsonb) || pg_catalog.jsonb_build_object((p_request ->> 'item_index'),p_request -> 'result_value')),
                                updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                    WHEN 'record_shard_failure' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'task_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'task_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'shard_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'shard_id' !~ '^[0-9a-f]{24}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair shard failure' USING ERRCODE = '22023'; END IF;
                        IF pg_catalog.length(p_request ->> 'error_message') > 500 THEN RAISE EXCEPTION 'Invalid Mask repair shard failure length' USING ERRCODE = '22023'; END IF;
                        IF v_batch.async_job_id=(p_request ->> 'async_job_id')::uuid THEN
                            UPDATE public.mask_repair_batches AS batch SET
                                result_json=COALESCE(batch.result_json,'{}'::jsonb) || pg_catalog.jsonb_build_object('shards',COALESCE(batch.result_json -> 'shards','{}'::jsonb) || pg_catalog.jsonb_build_object(p_request ->> 'shard_id',pg_catalog.jsonb_build_object('status','failed','task_id',p_request ->> 'task_id','reason',pg_catalog.left(p_request ->> 'error_message',500)))),
                                updated_at=pg_catalog.transaction_timestamp()
                            WHERE batch.id=v_id RETURNING batch.* INTO v_batch;
                        ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                    WHEN 'finish_execution' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR p_request ->> 'status' NOT IN ('completed','partial','failed')
                           OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair completion' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status=p_request ->> 'status',completed_at=(p_request ->> 'completed_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.async_job_id=(p_request ->> 'async_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'finish_rollback' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'rolled_back_at') IS DISTINCT FROM 'string'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_json') IS DISTINCT FROM 'object' THEN RAISE EXCEPTION 'Invalid Mask repair rollback completion' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status='rolled_back',rolled_back_at=(p_request ->> 'rolled_back_at')::timestamptz,
                            result_json=p_request -> 'result_json',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.rollback_async_job_id=(p_request ->> 'rollback_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'fail_rollback' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'rollback_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'rollback_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string'
                           OR pg_catalog.jsonb_typeof(p_request -> 'result_json') IS DISTINCT FROM 'object'
                           OR pg_catalog.jsonb_typeof(p_request -> 'failed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair rollback failure' USING ERRCODE = '22023'; END IF;
                        PERFORM (p_request ->> 'failed_at')::timestamptz;
                        UPDATE public.mask_repair_batches AS batch SET status='rollback_failed',
                            result_json=p_request -> 'result_json',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.rollback_async_job_id=(p_request ->> 'rollback_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'fail_dispatch' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair dispatch failure' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status='failed',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.async_job_id=(p_request ->> 'async_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'fail_rollback_dispatch' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'async_job_id') IS DISTINCT FROM 'string'
                           OR p_request ->> 'async_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                           OR pg_catalog.jsonb_typeof(p_request -> 'error_message') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair rollback dispatch failure' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status='rollback_failed',updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.rollback_async_job_id=(p_request ->> 'async_job_id')::uuid RETURNING batch.* INTO v_batch;
                    WHEN 'cancel_pending' THEN
                        IF pg_catalog.jsonb_typeof(p_request -> 'status') IS DISTINCT FROM 'string'
                           OR p_request ->> 'status' NOT IN ('cancelled','partial')
                           OR pg_catalog.jsonb_typeof(p_request -> 'completed_at') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'Invalid Mask repair cancellation' USING ERRCODE = '22023'; END IF;
                        UPDATE public.mask_repair_batches AS batch SET status=p_request ->> 'status',completed_at=(p_request ->> 'completed_at')::timestamptz,updated_at=pg_catalog.transaction_timestamp()
                        WHERE batch.id=v_id AND batch.status IN ('pending','running') RETURNING batch.* INTO v_batch;
                END CASE;
                IF v_data IS NULL OR pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object' THEN
                    IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); END IF;
                END IF;
                IF v_data IS NULL OR pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object' THEN
                    IF FOUND THEN v_data := pg_catalog.to_jsonb(v_batch); ELSE v_data := pg_catalog.jsonb_build_object('changed',false); END IF;
                END IF;
            END IF;
        END IF;
    ELSIF v_operation = 'insert_review_scope' THEN
        FOREACH v_field IN ARRAY ARRAY['scope_id','project_id','task_id','annotation_id'] LOOP
            IF pg_catalog.jsonb_typeof(p_request -> v_field) IS DISTINCT FROM 'string'
               OR p_request ->> v_field !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN RAISE EXCEPTION 'Invalid Mask review scope identity' USING ERRCODE = '22023'; END IF;
        END LOOP;
        IF pg_catalog.jsonb_typeof(p_request -> 'qc_issue_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'qc_issue_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_job_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'source_job_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'expected_segment_id') NOT IN ('string','null')
           OR (p_request ->> 'expected_segment_id' IS NOT NULL AND p_request ->> 'expected_segment_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_request -> 'reviewer_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'reviewer_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'region_mask_ref') IS DISTINCT FROM 'object'
           OR pg_catalog.jsonb_typeof(p_request -> 'source_annotation_version') IS DISTINCT FROM 'number'
           OR (p_request ->> 'source_annotation_version') !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'source_annotation_version')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'result_annotation_version') IS DISTINCT FROM 'number'
           OR (p_request ->> 'result_annotation_version') !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'result_annotation_version')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'source_job_revision') IS DISTINCT FROM 'number'
           OR (p_request ->> 'source_job_revision') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'source_job_revision')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_start') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_start') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_start')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_end') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_end') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_end')::bigint > 2147483647
           OR (p_request ->> 'frame_end')::bigint < (p_request ->> 'frame_start')::bigint
           OR pg_catalog.jsonb_typeof(p_request -> 'region_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'region_digest' !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'candidate_digest') IS DISTINCT FROM 'string'
           OR p_request ->> 'candidate_digest' !~ '^sha256:[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'decision') IS DISTINCT FROM 'string'
           OR p_request ->> 'decision' NOT IN ('accept','reject') THEN RAISE EXCEPTION 'Invalid Mask review scope facts' USING ERRCODE = '22023'; END IF;
        v_actor_id := (p_request ->> 'reviewer_id')::uuid;
        v_lock_reply := yoao_identity_v1.lock_active_actor(v_actor_id);
        IF v_lock_reply -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            v_data := pg_catalog.jsonb_build_object('created',false,'reason','actor_inactive');
        ELSE
            SELECT task.id,task.project_id INTO v_task
            FROM public.tasks AS task WHERE task.id=(p_request ->> 'task_id')::uuid FOR UPDATE NOWAIT;
            SELECT annotation.id,annotation.project_id,annotation.task_id,annotation.version,annotation.is_active,annotation.was_cancelled
            INTO v_annotation FROM public.annotations AS annotation
            WHERE annotation.id=(p_request ->> 'annotation_id')::uuid FOR KEY SHARE;
            SELECT issue.id,issue.project_id,issue.task_id,issue.annotation_id,issue.region_digest,issue.region_mask_ref
            INTO v_issue FROM public.mask_qc_issues AS issue
            WHERE issue.id=(p_request ->> 'qc_issue_id')::uuid FOR KEY SHARE;
            SELECT job.id,job.task_id,job.revision,job.segment_id,job.from_frame,job.to_frame
            INTO v_tracker_job FROM public.video_tracker_jobs AS job
            WHERE job.id=(p_request ->> 'source_job_id')::uuid FOR UPDATE;
            IF v_task.id IS NULL OR v_annotation.id IS NULL OR v_issue.id IS NULL OR v_tracker_job.id IS NULL
               OR v_task.project_id IS DISTINCT FROM (p_request ->> 'project_id')::uuid
               OR v_annotation.project_id IS DISTINCT FROM (p_request ->> 'project_id')::uuid
               OR v_annotation.task_id IS DISTINCT FROM (p_request ->> 'task_id')::uuid
               OR v_annotation.version IS DISTINCT FROM (p_request ->> 'result_annotation_version')::integer
               OR v_annotation.is_active IS NOT TRUE OR v_annotation.was_cancelled IS TRUE
               OR v_issue.project_id IS DISTINCT FROM (p_request ->> 'project_id')::uuid
               OR v_issue.task_id IS DISTINCT FROM (p_request ->> 'task_id')::uuid
               OR v_issue.annotation_id IS DISTINCT FROM (p_request ->> 'annotation_id')::uuid
               OR v_issue.status IS DISTINCT FROM 'open'
               OR v_issue.annotation_version IS DISTINCT FROM (p_request ->> 'source_annotation_version')::integer
               OR v_issue.source_versions ->> v_annotation.id::text IS DISTINCT FROM p_request ->> 'source_annotation_version'
               OR v_issue.region_digest IS DISTINCT FROM p_request ->> 'region_digest'
               OR v_issue.region_mask_ref IS DISTINCT FROM p_request -> 'region_mask_ref'
               OR (p_request -> 'region_mask_ref' ->> 'sha256') IS DISTINCT FROM p_request ->> 'region_digest'
               OR v_tracker_job.task_id IS DISTINCT FROM (p_request ->> 'task_id')::uuid
               OR v_tracker_job.status NOT IN ('partially_reviewed','accepted','discarded','cancelled')
               OR v_tracker_job.revision::bigint IS DISTINCT FROM (p_request ->> 'source_job_revision')::bigint + 1
               OR v_tracker_job.segment_id IS DISTINCT FROM (p_request ->> 'expected_segment_id')::uuid
               OR (p_request ->> 'frame_start')::integer < v_tracker_job.from_frame
               OR (p_request ->> 'frame_end')::integer > v_tracker_job.to_frame THEN
                v_data := pg_catalog.jsonb_build_object('created',false,'reason','review_scope_changed');
            ELSE
        INSERT INTO public.mask_review_scopes(
            id,project_id,task_id,annotation_id,qc_issue_id,source_job_id,reviewer_id,
            source_annotation_version,result_annotation_version,source_job_revision,frame_start,frame_end,
            region_mask_ref,region_digest,candidate_digest,decision
        ) VALUES (
            (p_request ->> 'scope_id')::uuid,(p_request ->> 'project_id')::uuid,(p_request ->> 'task_id')::uuid,
            (p_request ->> 'annotation_id')::uuid,(p_request ->> 'qc_issue_id')::uuid,(p_request ->> 'source_job_id')::uuid,
            (p_request ->> 'reviewer_id')::uuid,(p_request ->> 'source_annotation_version')::integer,
            (p_request ->> 'result_annotation_version')::integer,(p_request ->> 'source_job_revision')::integer,
            (p_request ->> 'frame_start')::integer,(p_request ->> 'frame_end')::integer,p_request -> 'region_mask_ref',
            p_request ->> 'region_digest',p_request ->> 'candidate_digest',p_request ->> 'decision'
        ) RETURNING public.mask_review_scopes.* INTO v_scope;
        v_data := pg_catalog.jsonb_build_object('created',true,'row',pg_catalog.to_jsonb(v_scope));
            END IF;
        END IF;
    ELSIF v_operation = 'read_review_scopes' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'annotation_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'annotation_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'result_annotation_version') IS DISTINCT FROM 'number'
           OR (p_request ->> 'result_annotation_version') !~ '^[1-9][0-9]{0,9}$'
           OR (p_request ->> 'result_annotation_version')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_start') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_start') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_start')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_end') IS DISTINCT FROM 'number'
           OR (p_request ->> 'frame_end') !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_end')::bigint > 2147483647
           OR (p_request ->> 'frame_end')::bigint < (p_request ->> 'frame_start')::bigint
           OR pg_catalog.jsonb_typeof(p_request -> 'lock') IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'Invalid Mask review scope query' USING ERRCODE = '22023'; END IF;
        IF (p_request ->> 'lock')::boolean THEN
            FOR v_scope IN SELECT scope.* FROM public.mask_review_scopes AS scope
                WHERE scope.annotation_id=(p_request ->> 'annotation_id')::uuid
                  AND scope.result_annotation_version=(p_request ->> 'result_annotation_version')::integer
                  AND scope.frame_start <= (p_request ->> 'frame_end')::integer
                  AND scope.frame_end >= (p_request ->> 'frame_start')::integer
                ORDER BY scope.id FOR UPDATE
            LOOP v_rows := v_rows || pg_catalog.jsonb_build_array(pg_catalog.to_jsonb(v_scope)); END LOOP;
        ELSE
            SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.to_jsonb(scope) ORDER BY scope.id),'[]'::jsonb)
            INTO v_rows FROM public.mask_review_scopes AS scope
            WHERE scope.annotation_id=(p_request ->> 'annotation_id')::uuid
              AND scope.result_annotation_version=(p_request ->> 'result_annotation_version')::integer
              AND scope.frame_start <= (p_request ->> 'frame_end')::integer
              AND scope.frame_end >= (p_request ->> 'frame_start')::integer;
        END IF;
        v_data := v_rows;
    ELSIF v_operation = 'read_repair_targets' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'issue_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'issue_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR pg_catalog.jsonb_typeof(p_request -> 'include_related') IS DISTINCT FROM 'boolean' THEN RAISE EXCEPTION 'Invalid Mask repair target query' USING ERRCODE = '22023'; END IF;
        SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
            'id',annotation.id,'project_id',annotation.project_id,'task_id',annotation.task_id,
            'version',annotation.version,'geometry',annotation.geometry,'is_locked',annotation.is_locked,
            'class_name',annotation.class_name,'tool_unit_id',annotation.tool_unit_id
        ) ORDER BY annotation.id),'[]'::jsonb)
        INTO v_rows
        FROM public.mask_qc_issues AS issue
        JOIN public.annotations AS annotation ON (
            (NOT (p_request ->> 'include_related')::boolean AND annotation.id=issue.annotation_id)
            OR ((p_request ->> 'include_related')::boolean
                AND (annotation.id=issue.annotation_id OR annotation.id=ANY(issue.related_annotation_ids))
                AND annotation.is_active IS TRUE
                AND annotation.was_cancelled IS FALSE)
        )
        WHERE issue.id=(p_request ->> 'issue_id')::uuid;
        v_data := v_rows;
    ELSIF v_operation = 'read_video_segment' THEN
        IF pg_catalog.jsonb_typeof(p_request -> 'dataset_item_id') NOT IN ('string','null')
           OR (p_request ->> 'dataset_item_id' IS NOT NULL AND p_request ->> 'dataset_item_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR pg_catalog.jsonb_typeof(p_request -> 'frame_index') IS DISTINCT FROM 'number'
           OR p_request ->> 'frame_index' !~ '^(0|[1-9][0-9]{0,9})$'
           OR (p_request ->> 'frame_index')::bigint > 2147483647
           OR pg_catalog.jsonb_typeof(p_request -> 'requested_id') NOT IN ('string','null')
           OR (p_request ->> 'requested_id' IS NOT NULL AND p_request ->> 'requested_id' !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN RAISE EXCEPTION 'Invalid Labeling video segment lookup' USING ERRCODE = '22023'; END IF;
        SELECT segment.* INTO v_segment FROM public.video_segments AS segment
        WHERE segment.dataset_item_id=(p_request ->> 'dataset_item_id')::uuid
          AND segment.start_frame <= (p_request ->> 'frame_index')::integer
          AND segment.end_frame >= (p_request ->> 'frame_index')::integer
          AND (p_request ->> 'requested_id' IS NULL OR segment.id=(p_request ->> 'requested_id')::uuid)
        ORDER BY segment.segment_index ASC LIMIT 1;
        IF FOUND THEN
            v_data := pg_catalog.jsonb_build_object(
                'id',v_segment.id,'locked_by',v_segment.locked_by,
                'lock_expires_at',v_segment.lock_expires_at
            );
        ELSE v_data := NULL; END IF;
    END IF;
    RETURN pg_catalog.jsonb_build_object('schema','aap-labeling-quality-data/v1','ok',true,'request',p_request,'data',v_data);
EXCEPTION
    WHEN invalid_text_representation OR invalid_datetime_format OR datetime_field_overflow OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'Invalid Mask repair request value' USING ERRCODE = '22023';
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.accept_dataset_task_creation(p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid, p_project_id uuid, p_association_id uuid, p_job_id uuid, p_mode text, p_chunk_size integer)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE
    v_manager jsonb;
    v_dataset_id uuid;
    v_association_id uuid;
    v_job jsonb;
    v_job_data jsonb;
    v_request jsonb;
    v_existing_request jsonb;
    v_operation public.task_creation_operations%ROWTYPE;
    v_inserted integer;
    v_expected_mode text;
BEGIN
    IF p_operation_id IS NULL OR p_dataset_id IS NULL OR p_project_id IS NULL
       OR p_association_id IS NULL OR p_mode IS NULL OR p_chunk_size IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    IF p_chunk_size < 1 OR p_chunk_size > 5000
       OR p_mode NOT IN ('generic', 'pointcloud') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    -- Acceptance is the only stage that checks current actor authority.
    v_dataset_id := yoao_assets_v1.lock_dataset_reference(p_dataset_id);
    v_manager := yoao_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_manager ->> 'schema' IS DISTINCT FROM 'aap-project-manager/v1' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'authority_contract_mismatch')
        );
    END IF;
    IF v_manager ->> 'ok' IS DISTINCT FROM 'true' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', v_manager -> 'error'
        );
    END IF;

    IF v_dataset_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1', 'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'dataset_missing')
        );
    END IF;
    v_expected_mode := CASE
        WHEN v_manager #>> '{data,data_type}' = 'lidar' THEN 'pointcloud'
        ELSE 'generic'
    END;
    IF p_mode IS DISTINCT FROM v_expected_mode THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'mode_mismatch')
        );
    END IF;

    v_association_id := yoao_assets_v1.lock_current_association(
        p_dataset_id,
        p_project_id
    );
    IF v_association_id IS NULL OR v_association_id <> p_association_id THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'association_changed')
        );
    END IF;

    IF p_job_id IS NOT NULL THEN
        v_job := yoao_jobs_v1.lock_task_creation_job(
            p_job_id,
            p_operation_id,
            p_actor_id,
            p_dataset_id,
            p_project_id,
            p_association_id
        );
        IF v_job ->> 'schema' IS DISTINCT FROM 'aap-task-creation-job/v1' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'jobs_contract_mismatch')
            );
        END IF;
        IF v_job ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', v_job -> 'error'
            );
        END IF;
        v_job_data := v_job -> 'data';
    END IF;

    v_request := pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation-request/v1',
        'operation_id', p_operation_id,
        'actor_id', p_actor_id,
        'dataset_id', p_dataset_id,
        'project_id', p_project_id,
        'association_id', p_association_id,
        'job_id', p_job_id,
        'mode', p_mode,
        'chunk_size', p_chunk_size
    );

    SELECT operation.*
    INTO v_operation
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id
    FOR UPDATE;

    IF FOUND THEN
        v_existing_request := v_operation.request;
        IF v_existing_request IS DISTINCT FROM v_request THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'operation_conflict')
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation_id', v_operation.id,
                'actor_id', v_operation.actor_id,
                'dataset_id', v_operation.dataset_id,
                'project_id', v_operation.project_id,
                'association_id', v_operation.association_id,
                'job_id', v_operation.job_id,
                'mode', v_operation.mode,
                'chunk_size', v_operation.chunk_size,
                'state', v_operation.state,
                'total', v_operation.total,
                'next_chunk', v_operation.next_chunk,
                'processed', v_operation.processed,
                'created', v_operation.created
            )
        );
    END IF;

    IF p_job_id IS NOT NULL
       AND (
           v_job_data ->> 'cancel_requested' = 'true'
           OR v_job_data ->> 'status' NOT IN ('pending', 'running')
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'job_not_active')
        );
    END IF;

    INSERT INTO public.task_creation_operations (
        id,
        actor_id,
        dataset_id,
        project_id,
        association_id,
        job_id,
        mode,
        chunk_size,
        request
    )
    VALUES (
        p_operation_id,
        p_actor_id,
        p_dataset_id,
        p_project_id,
        p_association_id,
        p_job_id,
        p_mode,
        p_chunk_size,
        v_request
    )
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;

    IF v_inserted = 0 THEN
        SELECT operation.*
        INTO v_operation
        FROM public.task_creation_operations AS operation
        WHERE operation.id = p_operation_id
        FOR UPDATE;
        IF v_operation.request IS DISTINCT FROM v_request THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'operation_conflict')
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation_id', v_operation.id,
                'actor_id', v_operation.actor_id,
                'dataset_id', v_operation.dataset_id,
                'project_id', v_operation.project_id,
                'association_id', v_operation.association_id,
                'job_id', v_operation.job_id,
                'mode', v_operation.mode,
                'chunk_size', v_operation.chunk_size,
                'state', v_operation.state,
                'total', v_operation.total,
                'next_chunk', v_operation.next_chunk,
                'processed', v_operation.processed,
                'created', v_operation.created
            )
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'actor_id', p_actor_id,
            'dataset_id', p_dataset_id,
            'project_id', p_project_id,
            'association_id', p_association_id,
            'job_id', p_job_id,
            'mode', p_mode,
            'chunk_size', p_chunk_size,
            'state', 'accepted',
            'total', NULL,
            'next_chunk', 0,
            'processed', 0,
            'created', 0
        )
    );
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.read_dataset_task_creation(p_operation_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE
    v_actor_id uuid;
    v_dataset_id uuid;
    v_project_id uuid;
    v_association_id uuid;
    v_job_id uuid;
    v_mode text;
    v_chunk_size integer;
    v_operation public.task_creation_operations%ROWTYPE;
    v_current_link_id uuid;
    v_job jsonb;
    v_job_data jsonb;
BEGIN
    IF p_operation_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    SELECT
        operation.actor_id,
        operation.dataset_id,
        operation.project_id,
        operation.association_id,
        operation.job_id,
        operation.mode,
        operation.chunk_size
    INTO
        v_actor_id,
        v_dataset_id,
        v_project_id,
        v_association_id,
        v_job_id,
        v_mode,
        v_chunk_size
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    PERFORM yoao_assets_v1.lock_dataset_reference(v_dataset_id);
    v_current_link_id := yoao_assets_v1.lock_current_association(
        v_dataset_id,
        v_project_id
    );
    IF v_current_link_id IS NULL OR v_current_link_id <> v_association_id THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'association_changed')
        );
    END IF;

    IF v_job_id IS NOT NULL THEN
        v_job := yoao_jobs_v1.lock_task_creation_job(
            v_job_id,
            p_operation_id,
            v_actor_id,
            v_dataset_id,
            v_project_id,
            v_association_id
        );
        IF v_job ->> 'schema' IS DISTINCT FROM 'aap-task-creation-job/v1' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'jobs_contract_mismatch')
            );
        END IF;
        IF v_job ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', v_job -> 'error'
            );
        END IF;
        v_job_data := v_job -> 'data';
        IF v_job_data ->> 'cancel_requested' = 'true'
           OR v_job_data ->> 'status' = 'cancelled' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'job_cancelled')
            );
        END IF;
    END IF;

    SELECT operation.*
    INTO v_operation
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_operation.actor_id <> v_actor_id
       OR v_operation.dataset_id <> v_dataset_id
       OR v_operation.project_id <> v_project_id
       OR v_operation.association_id <> v_association_id
       OR v_operation.job_id IS DISTINCT FROM v_job_id
       OR v_operation.mode IS DISTINCT FROM v_mode
       OR v_operation.chunk_size IS DISTINCT FROM v_chunk_size THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    IF v_job_id IS NOT NULL
       AND v_job_data ->> 'status' NOT IN ('pending', 'running')
       AND NOT (
           v_job_data ->> 'status' = 'completed'
           AND v_operation.state = 'completed'
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'job_not_active')
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', v_operation.id,
            'actor_id', v_operation.actor_id,
            'dataset_id', v_operation.dataset_id,
            'project_id', v_operation.project_id,
            'association_id', v_operation.association_id,
            'job_id', v_operation.job_id,
            'mode', v_operation.mode,
            'chunk_size', v_operation.chunk_size,
            'state', v_operation.state,
            'total', v_operation.total,
            'next_chunk', v_operation.next_chunk,
            'processed', v_operation.processed,
            'created', v_operation.created
        )
    );
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.prepare_dataset_task_creation(p_operation_id uuid, p_frames jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE
    v_actor_id uuid;
    v_dataset_id uuid;
    v_project_id uuid;
    v_association_id uuid;
    v_job_id uuid;
    v_mode text;
    v_chunk_size integer;
    v_operation public.task_creation_operations%ROWTYPE;
    v_source_ids uuid[];
    v_sources jsonb;
    v_source_items jsonb;
    v_manifest jsonb;
    v_total bigint;
    v_current_link_id uuid;
    v_job jsonb;
    v_job_data jsonb;
    v_frame jsonb;
    v_camera jsonb;
    v_sensor_name text;
BEGIN
    IF p_operation_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    SELECT
        operation.actor_id,
        operation.dataset_id,
        operation.project_id,
        operation.association_id,
        operation.job_id,
        operation.mode,
        operation.chunk_size
    INTO
        v_actor_id,
        v_dataset_id,
        v_project_id,
        v_association_id,
        v_job_id,
        v_mode,
        v_chunk_size
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    -- Execution trusts the accepted actor but fences dataset/link/job generations.
    PERFORM yoao_assets_v1.lock_dataset_reference(v_dataset_id);
    v_current_link_id := yoao_assets_v1.lock_current_association(
        v_dataset_id,
        v_project_id
    );
    IF v_current_link_id IS NULL OR v_current_link_id <> v_association_id THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'association_changed')
        );
    END IF;

    IF v_job_id IS NOT NULL THEN
        v_job := yoao_jobs_v1.lock_task_creation_job(
            v_job_id,
            p_operation_id,
            v_actor_id,
            v_dataset_id,
            v_project_id,
            v_association_id
        );
        IF v_job ->> 'schema' IS DISTINCT FROM 'aap-task-creation-job/v1' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'jobs_contract_mismatch')
            );
        END IF;
        IF v_job ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', v_job -> 'error'
            );
        END IF;
        v_job_data := v_job -> 'data';
    END IF;

    SELECT operation.*
    INTO v_operation
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_operation.actor_id <> v_actor_id
       OR v_operation.dataset_id <> v_dataset_id
       OR v_operation.project_id <> v_project_id
       OR v_operation.association_id <> v_association_id
       OR v_operation.job_id IS DISTINCT FROM v_job_id
       OR v_operation.mode IS DISTINCT FROM v_mode
       OR v_operation.chunk_size IS DISTINCT FROM v_chunk_size THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    IF v_job_id IS NOT NULL
       AND (v_job_data ->> 'cancel_requested' = 'true'
            OR v_job_data ->> 'status' = 'cancelled') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'job_cancelled')
        );
    END IF;

    IF v_operation.manifest IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation_id', v_operation.id,
                'actor_id', v_operation.actor_id,
                'dataset_id', v_operation.dataset_id,
                'project_id', v_operation.project_id,
                'association_id', v_operation.association_id,
                'job_id', v_operation.job_id,
                'mode', v_operation.mode,
                'chunk_size', v_operation.chunk_size,
                'state', v_operation.state,
                'total', v_operation.total,
                'next_chunk', v_operation.next_chunk,
                'processed', v_operation.processed,
                'created', v_operation.created
            )
        );
    END IF;

    IF v_job_id IS NOT NULL
       AND v_job_data ->> 'status' NOT IN ('pending', 'running') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'job_not_active')
        );
    END IF;

    IF v_mode = 'generic' AND p_frames IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    IF v_mode = 'pointcloud'
       AND (p_frames IS NULL OR pg_catalog.jsonb_typeof(p_frames) <> 'array') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    IF v_mode = 'pointcloud' THEN
        FOR v_frame IN
            SELECT frame.value
            FROM pg_catalog.jsonb_array_elements(p_frames) AS frame(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_frame) IS DISTINCT FROM 'object' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-task-creation/v1',
                    'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                );
            END IF;
            IF (v_frame - ARRAY['primary_item_id', 'cameras']) <> '{}'::jsonb
               OR NOT (v_frame ? 'primary_item_id')
               OR NOT (v_frame ? 'cameras')
               OR pg_catalog.jsonb_typeof(v_frame -> 'primary_item_id') <> 'string'
               OR (v_frame ->> 'primary_item_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(v_frame -> 'cameras') <> 'array' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-task-creation/v1',
                    'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                );
            END IF;

            FOR v_camera IN
                SELECT camera.value
                FROM pg_catalog.jsonb_array_elements(v_frame -> 'cameras') AS camera(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_camera) IS DISTINCT FROM 'object' THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-task-creation/v1',
                        'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
                IF (v_camera - ARRAY['item_id', 'sensor_name']) <> '{}'::jsonb
                   OR NOT (v_camera ? 'item_id')
                   OR NOT (v_camera ? 'sensor_name')
                   OR pg_catalog.jsonb_typeof(v_camera -> 'item_id') <> 'string'
                   OR (v_camera ->> 'item_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   OR pg_catalog.jsonb_typeof(v_camera -> 'sensor_name') <> 'string' THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-task-creation/v1',
                        'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
                v_sensor_name := v_camera ->> 'sensor_name';
                IF v_sensor_name = '' OR pg_catalog.char_length(v_sensor_name) > 43 THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-task-creation/v1',
                        'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
            END LOOP;
        END LOOP;

        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_frames) WITH ORDINALITY
                AS frame(value, frame_ordinality)
            GROUP BY (frame.value ->> 'primary_item_id')::uuid
            HAVING pg_catalog.count(*) > 1
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_frames) WITH ORDINALITY
                AS frame(value, frame_ordinality)
            CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                frame.value -> 'cameras'
            ) AS camera(value)
            GROUP BY frame.frame_ordinality, camera.value ->> 'sensor_name'
            HAVING pg_catalog.count(*) > 1
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        SELECT COALESCE(
            pg_catalog.array_agg(requested.item_id ORDER BY requested.item_id),
            ARRAY[]::uuid[]
        )
        INTO v_source_ids
        FROM (
            SELECT DISTINCT source.item_id
            FROM (
                SELECT (frame.value ->> 'primary_item_id')::uuid AS item_id
                FROM pg_catalog.jsonb_array_elements(p_frames) AS frame(value)
                UNION ALL
                SELECT (camera.value ->> 'item_id')::uuid AS item_id
                FROM pg_catalog.jsonb_array_elements(p_frames) AS frame(value)
                CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                    frame.value -> 'cameras'
                ) AS camera(value)
            ) AS source
        ) AS requested;
    END IF;

    IF v_mode = 'generic' THEN
        v_sources := yoao_assets_v1.lock_task_creation_items(v_dataset_id, NULL);
    ELSE
        v_sources := yoao_assets_v1.lock_task_creation_items(v_dataset_id, v_source_ids);
    END IF;
    IF v_sources ->> 'schema' IS DISTINCT FROM 'aap-task-creation-items/v1'
       OR v_sources ->> 'ok' IS DISTINCT FROM 'true' THEN
        IF v_sources ->> 'schema' IS DISTINCT FROM 'aap-task-creation-items/v1' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'assets_contract_mismatch')
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', v_sources -> 'error'
        );
    END IF;
    v_source_items := v_sources #> '{data,items}';

    IF v_mode = 'generic' THEN
        WITH items AS MATERIALIZED (
            SELECT
                (item.value ->> 'id')::uuid AS item_id,
                item.value ->> 'file_name' AS file_name,
                item.value ->> 'file_path' AS file_path,
                item.value ->> 'file_type' AS file_type
            FROM pg_catalog.jsonb_array_elements(v_source_items) AS item(value)
        ),
        candidates AS MATERIALIZED (
            SELECT item.*
            FROM items AS item
            WHERE NOT EXISTS (
                SELECT 1
                FROM public.tasks AS task
                WHERE task.project_id = v_project_id
                  AND task.dataset_item_id = item.item_id
            )
        )
        SELECT
            COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'item_id', candidate.item_id,
                        'file_name', candidate.file_name,
                        'file_path', candidate.file_path,
                        'file_type', candidate.file_type,
                        'links', '[]'::jsonb
                    ) ORDER BY candidate.item_id
                ),
                '[]'::jsonb
            ),
            pg_catalog.count(*)
        INTO v_manifest, v_total
        FROM candidates AS candidate;
    ELSE
        WITH source_items AS MATERIALIZED (
            SELECT
                (item.value ->> 'id')::uuid AS item_id,
                item.value ->> 'file_name' AS file_name,
                item.value ->> 'file_path' AS file_path
            FROM pg_catalog.jsonb_array_elements(v_source_items) AS item(value)
        ),
        frame_rows AS MATERIALIZED (
            SELECT frame.value, frame.ordinality
            FROM pg_catalog.jsonb_array_elements(p_frames) WITH ORDINALITY
                AS frame(value, ordinality)
        ),
        candidates AS MATERIALIZED (
            SELECT
                frame.ordinality,
                primary_item.item_id,
                primary_item.file_name,
                primary_item.file_path,
                frame.value -> 'cameras' AS cameras
            FROM frame_rows AS frame
            JOIN source_items AS primary_item
              ON primary_item.item_id = (frame.value ->> 'primary_item_id')::uuid
            WHERE NOT EXISTS (
                SELECT 1
                FROM public.tasks AS task
                WHERE task.project_id = v_project_id
                  AND task.dataset_item_id = primary_item.item_id
            )
        ),
        manifest_rows AS MATERIALIZED (
            SELECT
                candidate.ordinality,
                pg_catalog.jsonb_build_object(
                    'item_id', candidate.item_id,
                    'file_name', candidate.file_name,
                    'file_path', candidate.file_path,
                    'file_type', 'point_cloud',
                    'links', pg_catalog.jsonb_build_array(
                        pg_catalog.jsonb_build_object(
                            'item_id', candidate.item_id,
                            'role', 'primary_lidar',
                            'sensor_name', NULL
                        )
                    ) || COALESCE(
                        (
                            SELECT pg_catalog.jsonb_agg(
                                pg_catalog.jsonb_build_object(
                                    'item_id', (camera.value ->> 'item_id')::uuid,
                                    'role', 'camera_' || (camera.value ->> 'sensor_name'),
                                    'sensor_name', camera.value ->> 'sensor_name'
                                ) ORDER BY camera.ordinality
                            )
                            FROM pg_catalog.jsonb_array_elements(candidate.cameras)
                                WITH ORDINALITY AS camera(value, ordinality)
                        ),
                        '[]'::jsonb
                    )
                ) AS manifest_item
            FROM candidates AS candidate
        )
        SELECT
            COALESCE(
                pg_catalog.jsonb_agg(
                    manifest_rows.manifest_item ORDER BY manifest_rows.ordinality
                ),
                '[]'::jsonb
            ),
            pg_catalog.count(*)
        INTO v_manifest, v_total
        FROM manifest_rows;
    END IF;

    UPDATE public.task_creation_operations AS operation
    SET manifest = v_manifest,
        total = v_total,
        state = CASE WHEN v_total = 0 THEN 'completed' ELSE 'running' END,
        updated_at = pg_catalog.now()
    WHERE operation.id = p_operation_id;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', v_operation.id,
            'actor_id', v_operation.actor_id,
            'dataset_id', v_operation.dataset_id,
            'project_id', v_operation.project_id,
            'association_id', v_operation.association_id,
            'job_id', v_operation.job_id,
            'mode', v_operation.mode,
            'chunk_size', v_operation.chunk_size,
            'state', CASE WHEN v_total = 0 THEN 'completed' ELSE 'running' END,
            'total', v_total,
            'next_chunk', v_operation.next_chunk,
            'processed', v_operation.processed,
            'created', v_operation.created
        )
    );
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION yoao_labeling_v1.advance_dataset_task_creation(p_operation_id uuid, p_chunk_index integer)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE
    v_actor_id uuid;
    v_dataset_id uuid;
    v_project_id uuid;
    v_association_id uuid;
    v_job_id uuid;
    v_mode text;
    v_chunk_size integer;
    v_operation public.task_creation_operations%ROWTYPE;
    v_current_link_id uuid;
    v_job jsonb;
    v_job_data jsonb;
    v_saved_result jsonb;
    v_chunk_manifest jsonb;
    v_chunk_length integer;
    v_item_ids uuid[];
    v_sources jsonb;
    v_planned_created bigint;
    v_actual_created bigint;
    v_next_processed bigint;
    v_next_created bigint;
    v_next_chunk integer;
    v_project_total bigint;
    v_complete boolean;
    v_progress_ok boolean;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_chunk_index IS NULL OR p_chunk_index < 0 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    SELECT
        operation.actor_id,
        operation.dataset_id,
        operation.project_id,
        operation.association_id,
        operation.job_id,
        operation.mode,
        operation.chunk_size
    INTO
        v_actor_id,
        v_dataset_id,
        v_project_id,
        v_association_id,
        v_job_id,
        v_mode,
        v_chunk_size
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    PERFORM yoao_assets_v1.lock_dataset_reference(v_dataset_id);
    v_current_link_id := yoao_assets_v1.lock_current_association(
        v_dataset_id,
        v_project_id
    );
    IF v_current_link_id IS NULL OR v_current_link_id <> v_association_id THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'association_changed')
        );
    END IF;

    IF v_job_id IS NOT NULL THEN
        v_job := yoao_jobs_v1.lock_task_creation_job(
            v_job_id,
            p_operation_id,
            v_actor_id,
            v_dataset_id,
            v_project_id,
            v_association_id
        );
        IF v_job ->> 'schema' IS DISTINCT FROM 'aap-task-creation-job/v1' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation-chunk/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'jobs_contract_mismatch')
            );
        END IF;
        IF v_job ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation-chunk/v1',
                'ok', false,
                'error', v_job -> 'error'
            );
        END IF;
        v_job_data := v_job -> 'data';
        IF v_job_data ->> 'cancel_requested' = 'true'
           OR v_job_data ->> 'status' = 'cancelled' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation-chunk/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'job_cancelled')
            );
        END IF;
    END IF;

    SELECT operation.*
    INTO v_operation
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_operation.actor_id <> v_actor_id
       OR v_operation.dataset_id <> v_dataset_id
       OR v_operation.project_id <> v_project_id
       OR v_operation.association_id <> v_association_id
       OR v_operation.job_id IS DISTINCT FROM v_job_id
       OR v_operation.mode IS DISTINCT FROM v_mode
       OR v_operation.chunk_size IS DISTINCT FROM v_chunk_size THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    SELECT chunk.result
    INTO v_saved_result
    FROM public.task_creation_chunks AS chunk
    WHERE chunk.operation_id = p_operation_id
      AND chunk.chunk_index = p_chunk_index
    FOR UPDATE;
    IF FOUND THEN
        RETURN v_saved_result;
    END IF;

    IF v_job_id IS NOT NULL
       AND v_job_data ->> 'status' NOT IN ('pending', 'running') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'job_not_active')
        );
    END IF;
    IF v_operation.manifest IS NULL OR v_operation.total IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'plan_not_prepared')
        );
    END IF;
    IF v_operation.total = 0 OR v_operation.state = 'completed' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'plan_complete')
        );
    END IF;
    IF p_chunk_index <> v_operation.next_chunk THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'cursor_conflict')
        );
    END IF;

    SELECT
        COALESCE(
            pg_catalog.jsonb_agg(page.value ORDER BY page.ordinality),
            '[]'::jsonb
        ),
        pg_catalog.count(*)::integer
    INTO v_chunk_manifest, v_chunk_length
    FROM pg_catalog.jsonb_array_elements(v_operation.manifest) WITH ORDINALITY
        AS page(value, ordinality)
    WHERE page.ordinality > (p_chunk_index::bigint * v_chunk_size)
      AND page.ordinality <= ((p_chunk_index::bigint + 1) * v_chunk_size);
    IF v_chunk_length = 0 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'cursor_conflict')
        );
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(requested.item_id ORDER BY requested.item_id),
        ARRAY[]::uuid[]
    )
    INTO v_item_ids
    FROM (
        SELECT DISTINCT source.item_id
        FROM (
            SELECT (item.value ->> 'item_id')::uuid AS item_id
            FROM pg_catalog.jsonb_array_elements(v_chunk_manifest) AS item(value)
            UNION ALL
            SELECT (link.value ->> 'item_id')::uuid AS item_id
            FROM pg_catalog.jsonb_array_elements(v_chunk_manifest) AS item(value)
            CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                item.value -> 'links'
            ) AS link(value)
        ) AS source
    ) AS requested;

    v_sources := yoao_assets_v1.lock_task_creation_items(v_dataset_id, v_item_ids);
    IF v_sources ->> 'schema' IS DISTINCT FROM 'aap-task-creation-items/v1' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'assets_contract_mismatch')
        );
    END IF;
    IF v_sources ->> 'ok' IS DISTINCT FROM 'true' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', v_sources -> 'error'
        );
    END IF;

    PERFORM project.id
    FROM public.projects AS project
    WHERE project.id = v_project_id
    FOR NO KEY UPDATE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'project_missing')
        );
    END IF;

    SELECT pg_catalog.count(*)
    INTO v_planned_created
    FROM pg_catalog.jsonb_array_elements(v_chunk_manifest) AS item(value)
    WHERE NOT EXISTS (
        SELECT 1
        FROM public.tasks AS task
        WHERE task.project_id = v_project_id
          AND task.dataset_item_id = (item.value ->> 'item_id')::uuid
    );

    v_next_processed := v_operation.processed + v_chunk_length;
    v_next_created := v_operation.created + v_planned_created;
    v_next_chunk := p_chunk_index + 1;
    v_complete := v_next_processed = v_operation.total;

    IF v_next_processed > v_operation.total THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'cursor_conflict')
        );
    END IF;

    IF v_job_id IS NOT NULL THEN
        v_progress_ok := yoao_jobs_v1.update_task_creation_progress(
            v_job_id,
            p_operation_id,
            v_next_processed,
            v_operation.total,
            v_next_created
        );
        IF NOT v_progress_ok THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation-chunk/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'job_not_active')
            );
        END IF;
    END IF;

    WITH chunk_items AS MATERIALIZED (
        SELECT item.value, item.ordinality
        FROM pg_catalog.jsonb_array_elements(v_chunk_manifest) WITH ORDINALITY
            AS item(value, ordinality)
    ),
    pending AS MATERIALIZED (
        SELECT
            item.ordinality,
            (item.value ->> 'item_id')::uuid AS item_id,
            item.value ->> 'file_name' AS file_name,
            item.value ->> 'file_path' AS file_path,
            item.value ->> 'file_type' AS file_type,
            item.value -> 'links' AS links
        FROM chunk_items AS item
        WHERE NOT EXISTS (
            SELECT 1
            FROM public.tasks AS task
            WHERE task.project_id = v_project_id
              AND task.dataset_item_id = (item.value ->> 'item_id')::uuid
        )
    ),
    numbered AS MATERIALIZED (
        SELECT
            pending.*,
            pg_catalog.row_number() OVER (ORDER BY pending.ordinality) AS row_number
        FROM pending
    ),
    sequence_values AS MATERIALIZED (
        SELECT
            sequence_row.row_number,
            pg_catalog.nextval(
                'public.display_seq_tasks'::pg_catalog.regclass
            ) AS display_number
        FROM pg_catalog.generate_series(1, v_planned_created) AS sequence_row(row_number)
        ORDER BY sequence_row.row_number
    ),
    inserted AS (
        INSERT INTO public.tasks (
            id,
            project_id,
            dataset_item_id,
            display_id,
            file_name,
            file_path,
            file_type,
            tags,
            status,
            assignee_is_override,
            is_labeled,
            overlap,
            total_annotations,
            total_predictions,
            reviewer_is_override,
            first_review_eligible,
            annotation_contributor_ids,
            reopened_count,
            batch_id,
            version
        )
        SELECT
            pg_catalog.gen_random_uuid(),
            v_project_id,
            numbered.item_id,
            'T-' || sequence_values.display_number::text,
            numbered.file_name,
            numbered.file_path,
            numbered.file_type,
            '[]'::jsonb,
            'pending',
            false,
            false,
            1,
            0,
            0,
            false,
            true,
            '[]'::jsonb,
            0,
            NULL,
            1
        FROM numbered
        JOIN sequence_values USING (row_number)
        ORDER BY numbered.ordinality
        RETURNING id, project_id, dataset_item_id
    ),
    inserted_links AS (
        INSERT INTO public.task_dataset_item_links (
            id,
            task_id,
            dataset_item_id,
            role,
            sensor_name
        )
        SELECT
            pg_catalog.gen_random_uuid(),
            inserted.id,
            (link.value ->> 'item_id')::uuid,
            link.value ->> 'role',
            link.value ->> 'sensor_name'
        FROM inserted
        JOIN pending
          ON pending.item_id = inserted.dataset_item_id
        CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(pending.links) AS link(value)
        RETURNING task_id
    ),
    link_count AS MATERIALIZED (
        SELECT pg_catalog.count(*) AS linked FROM inserted_links
    ),
    created_by_project AS MATERIALIZED (
        SELECT inserted.project_id, pg_catalog.count(*) AS created
        FROM inserted
        GROUP BY inserted.project_id
    ),
    updated_projects AS (
        UPDATE public.projects AS project
        SET total_tasks = COALESCE(project.total_tasks, 0) + created.created,
            updated_at = pg_catalog.now()
        FROM created_by_project AS created
        WHERE project.id = created.project_id
        RETURNING project.id AS project_id, project.total_tasks
    )
    SELECT
        COALESCE(
            (SELECT pg_catalog.sum(created.created) FROM created_by_project AS created),
            0
        ),
        COALESCE(
            (SELECT updated.total_tasks FROM updated_projects AS updated),
            (
                SELECT COALESCE(project.total_tasks, 0)
                FROM public.projects AS project
                WHERE project.id = v_project_id
            )
        )
    INTO v_actual_created, v_project_total
    FROM link_count;

    IF v_actual_created <> v_planned_created THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'initial task candidate set changed during a locked chunk';
    END IF;

    v_result := pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation-chunk/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'project_id', v_project_id,
            'chunk_index', p_chunk_index,
            'created', v_actual_created,
            'processed', v_chunk_length,
            'cumulative_created', v_next_created,
            'cumulative_processed', v_next_processed,
            'total', v_operation.total,
            'next_chunk', v_next_chunk,
            'complete', v_complete,
            'project_total', v_project_total
        )
    );

    UPDATE public.task_creation_operations AS operation
    SET next_chunk = v_next_chunk,
        processed = v_next_processed,
        created = v_next_created,
        state = CASE WHEN v_complete THEN 'completed' ELSE 'running' END,
        updated_at = pg_catalog.now()
    WHERE operation.id = p_operation_id;

    INSERT INTO public.task_creation_chunks (
        operation_id,
        chunk_index,
        result
    )
    VALUES (p_operation_id, p_chunk_index, v_result);

    RETURN v_result;
END;
$function$;
-- aap-statement-break
DO $ownership$
DECLARE
    v_assets name := (
        SELECT pg_catalog.pg_get_userbyid(proowner)
        FROM pg_catalog.pg_proc
        WHERE oid = 'yoao_assets_v1.lock_dataset_reference(uuid)'::regprocedure
    );
    v_labeling name := (
        SELECT pg_catalog.pg_get_userbyid(proowner)
        FROM pg_catalog.pg_proc
        WHERE oid = 'yoao_labeling_v1.accept_dataset_task_creation(uuid,uuid,uuid,uuid,uuid,uuid,text,integer)'::regprocedure
    );
BEGIN
    IF v_labeling IS NOT NULL AND v_assets IS NOT NULL AND v_labeling <> v_assets THEN
        EXECUTE pg_catalog.format(
            'REVOKE EXECUTE ON FUNCTION yoao_assets_v1.read_dataset_source_authority(uuid,boolean) FROM %I',
            v_labeling
        );
    END IF;
END
$ownership$;
