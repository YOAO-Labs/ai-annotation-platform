CREATE FUNCTION yoao_assets_v1._inventory_saved_locator(p_ref jsonb, p_usage boolean)
RETURNS jsonb
LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN pg_catalog.jsonb_typeof(p_ref) <> 'object' THEN NULL
        WHEN p_usage AND p_ref - ARRAY['schema', 'purpose', 'location_id', 'config_revision', 'binding_generation', 'endpoint_url', 'bucket', 'object_key', 'prefix', 'key_prefix', 'storage_identity', 'origin', 'evidence']::text[] <> '{}'::jsonb THEN NULL
        ELSE (
            SELECT pg_catalog.jsonb_object_agg(k, CASE
                WHEN p_usage AND k = 'evidence' AND pg_catalog.jsonb_typeof(v) = 'object' THEN '{}'::jsonb
                WHEN pg_catalog.jsonb_typeof(v) IN ('array','object') THEN 'false'::jsonb
                WHEN k = 'endpoint_url' AND ((v #>> '{}') !~ '^https?://[^[:space:]/?#@]+(/[^[:cntrl:]?#]*)?$'
                    OR position(pg_catalog.chr(92) IN (v #>> '{}')) <> 0) THEN 'false'::jsonb
                ELSE v END)
            FROM pg_catalog.jsonb_each(p_ref) AS item(k, v)
            WHERE k IN ('schema', 'purpose', 'location_id', 'config_revision', 'binding_generation', 'endpoint_url', 'bucket', 'object_key', 'prefix', 'key_prefix', 'storage_identity') OR (p_usage AND k IN ('origin','evidence'))
        ) END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._inventory_saved_locator(jsonb,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_inventory_revisions()
RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT ARRAY(SELECT version_num::text FROM public.alembic_version ORDER BY version_num)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_inventory_revisions() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_inventory_locations(p_after uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', l.id,
        'kind', l.kind,
        'config_revision', l.config_revision,
        'enabled', l.enabled,
        'connection_id', l.connection_id,
        'endpoint', CASE WHEN l.kind = 's3' AND (l.connection_id IS NULL OR c.kind = 's3')
            AND cfg.value->>'endpoint' ~ '^(https?://)?[^[:space:]/?#@]+/?$'
            THEN cfg.value->>'endpoint' END,
        'use_ssl', CASE WHEN pg_catalog.jsonb_typeof(cfg.value->'use_ssl') = 'boolean'
            THEN cfg.value->>'use_ssl' END,
        'connection_kind', c.kind,
        'connection_scope', c.scope,
        'connection_revision', c.revision,
        'bucket', CASE WHEN c.kind = 's3' AND pg_catalog.jsonb_typeof(c.config->'bucket') = 'string'
            THEN c.config->>'bucket' END,
        'base_prefix', CASE WHEN c.kind = 's3' AND pg_catalog.jsonb_typeof(c.config->'base_prefix') = 'string'
            THEN c.config->>'base_prefix' END
    )
    FROM public.storage_locations l
    LEFT JOIN public.storage_connections c ON c.id = l.connection_id
    CROSS JOIN LATERAL (SELECT CASE WHEN l.connection_id IS NULL THEN l.config ELSE c.config END) cfg(value)
    WHERE p_after IS NULL OR l.id > p_after
    ORDER BY l.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_inventory_locations(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_inventory_bindings(p_after text, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'purpose', b.purpose,
        'location_id', b.location_id,
        'bucket', b.bucket,
        'generation', b.generation
    )
    FROM public.storage_bindings b
    WHERE p_after IS NULL OR b.purpose COLLATE "C" > p_after COLLATE "C"
    ORDER BY b.purpose COLLATE "C"
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_inventory_bindings(text,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_target_reference_count(p_location uuid)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT count(*) FROM public.storage_migration_copies r WHERE r.target_location_id = p_location
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_target_reference_count(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_prediction_input_leases(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'bucket', r.bucket,
        'storage_identity', r.storage_identity,
        'state', r.state,
        'size_bytes', r.size_bytes,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL,
        'source_ref_unexpanded', r.source_ref IS NOT NULL,
        'inventory_present', r.inventory IS NOT NULL,
        'inventory_schema', CASE WHEN r.inventory->'schema' = '1'::jsonb THEN 1 END,
        'inventory_objects_type', pg_catalog.jsonb_typeof(r.inventory->'objects')
    )
    FROM public.prediction_input_leases r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_prediction_input_leases(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_prediction_input_leases_objects(p_id uuid, p_key text, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'state', r.state,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL,
        'inventory_key', o.key,
        'object_sha256', CASE WHEN o.value->'sha256' IS NULL OR o.value->'sha256' = 'null'::jsonb THEN NULL
            WHEN o.value->>'sha256' ~ '^[0-9a-f]{64}$' THEN o.value->>'sha256' ELSE '' END,
        'object_size', CASE WHEN pg_catalog.jsonb_typeof(o.value->'size_bytes') IN ('number','null') THEN o.value->'size_bytes'
            WHEN o.value ? 'size_bytes' THEN 'false'::jsonb END,
        'object_version', CASE WHEN pg_catalog.jsonb_typeof(o.value->'version_id') IN ('string','null') THEN o.value->'version_id'
            WHEN o.value ? 'version_id' THEN 'false'::jsonb END,
        'object_state', CASE WHEN pg_catalog.jsonb_typeof(o.value->'state') = 'string' THEN o.value->>'state' END
    )
    FROM public.prediction_input_leases r
    CROSS JOIN LATERAL pg_catalog.jsonb_each(
        CASE WHEN r.inventory->'schema' = '1'::jsonb AND pg_catalog.jsonb_typeof(r.inventory->'objects') = 'object'
        THEN r.inventory->'objects' ELSE '{}'::jsonb END
    ) o
    WHERE (p_id IS NULL OR r.id >= p_id)
      AND (p_id IS NULL OR (r.id, o.key COLLATE "C") > (p_id, p_key COLLATE "C"))
    ORDER BY r.id, o.key COLLATE "C"
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_prediction_input_leases_objects(uuid,text,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_image_pyramid_outputs(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'asset_id', r.asset_id,
        'state', r.state,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL,
        'inventory_present', r.inventory IS NOT NULL,
        'inventory_schema', CASE WHEN r.inventory->'schema' = '1'::jsonb THEN 1 END,
        'inventory_objects_type', pg_catalog.jsonb_typeof(r.inventory->'objects')
    )
    FROM public.image_pyramid_outputs r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_image_pyramid_outputs(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_image_pyramid_outputs_objects(p_id uuid, p_key text, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'state', r.state,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL,
        'inventory_key', o.key,
        'object_sha256', CASE WHEN o.value->'sha256' IS NULL OR o.value->'sha256' = 'null'::jsonb THEN NULL
            WHEN o.value->>'sha256' ~ '^[0-9a-f]{64}$' THEN o.value->>'sha256' ELSE '' END,
        'object_size', CASE WHEN pg_catalog.jsonb_typeof(o.value->'size_bytes') IN ('number','null') THEN o.value->'size_bytes'
            WHEN o.value ? 'size_bytes' THEN 'false'::jsonb END,
        'object_version', CASE WHEN pg_catalog.jsonb_typeof(o.value->'version_id') IN ('string','null') THEN o.value->'version_id'
            WHEN o.value ? 'version_id' THEN 'false'::jsonb END,
        'object_state', CASE WHEN pg_catalog.jsonb_typeof(o.value->'state') = 'string' THEN o.value->>'state' END
    )
    FROM public.image_pyramid_outputs r
    CROSS JOIN LATERAL pg_catalog.jsonb_each(
        CASE WHEN r.inventory->'schema' = '1'::jsonb AND pg_catalog.jsonb_typeof(r.inventory->'objects') = 'object'
        THEN r.inventory->'objects' ELSE '{}'::jsonb END
    ) o
    WHERE (p_id IS NULL OR r.id >= p_id)
      AND (p_id IS NULL OR (r.id, o.key COLLATE "C") > (p_id, p_key COLLATE "C"))
    ORDER BY r.id, o.key COLLATE "C"
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_image_pyramid_outputs_objects(uuid,text,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_video_derivative_outputs(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'state', r.state,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL,
        'original_ref_unexpanded', r.original_ref IS NOT NULL,
        'inventory_present', r.inventory IS NOT NULL,
        'inventory_schema', CASE WHEN r.inventory->'schema' = '1'::jsonb THEN 1 END,
        'inventory_objects_type', pg_catalog.jsonb_typeof(r.inventory->'objects')
    )
    FROM public.video_derivative_outputs r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_video_derivative_outputs(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_video_derivative_outputs_objects(p_id uuid, p_key text, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'state', r.state,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL,
        'inventory_key', o.key,
        'object_sha256', CASE WHEN o.value->'sha256' IS NULL OR o.value->'sha256' = 'null'::jsonb THEN NULL
            WHEN o.value->>'sha256' ~ '^[0-9a-f]{64}$' THEN o.value->>'sha256' ELSE '' END,
        'object_size', CASE WHEN pg_catalog.jsonb_typeof(o.value->'size_bytes') IN ('number','null') THEN o.value->'size_bytes'
            WHEN o.value ? 'size_bytes' THEN 'false'::jsonb END,
        'object_version', CASE WHEN pg_catalog.jsonb_typeof(o.value->'version_id') IN ('string','null') THEN o.value->'version_id'
            WHEN o.value ? 'version_id' THEN 'false'::jsonb END,
        'object_state', CASE WHEN pg_catalog.jsonb_typeof(o.value->'state') = 'string' THEN o.value->>'state' END
    )
    FROM public.video_derivative_outputs r
    CROSS JOIN LATERAL pg_catalog.jsonb_each(
        CASE WHEN r.inventory->'schema' = '1'::jsonb AND pg_catalog.jsonb_typeof(r.inventory->'objects') = 'object'
        THEN r.inventory->'objects' ELSE '{}'::jsonb END
    ) o
    WHERE (p_id IS NULL OR r.id >= p_id)
      AND (p_id IS NULL OR (r.id, o.key COLLATE "C") > (p_id, p_key COLLATE "C"))
    ORDER BY r.id, o.key COLLATE "C"
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_video_derivative_outputs_objects(uuid,text,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.inventory_training_dataset_versions(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'status', r.status,
        'size_bytes', r.size_bytes,
        'package_key', r.package_key,
        'package_sha256', r.package_sha256,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL,
        'snapshot_unexpanded', r.snapshot IS NOT NULL,
        'manifest_unexpanded', r.manifest IS NOT NULL
    )
    FROM public.training_dataset_versions r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.inventory_training_dataset_versions(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.inventory_training_runs(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'status', r.status,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.training_runs r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.inventory_training_runs(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.inventory_training_previews(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'status', r.status,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL,
        'upload_inventory_unexpanded', r.upload_inventory IS NOT NULL
    )
    FROM public.training_previews r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.inventory_training_previews(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.inventory_sample_evaluation_runs(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'status', r.status,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.sample_evaluation_runs r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.inventory_sample_evaluation_runs(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.inventory_active_learning_rounds(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'status', r.status,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.active_learning_rounds r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.inventory_active_learning_rounds(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1.inventory_avatar_uploads(p_token text, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'token', r.token,
        'state', r.state,
        'object_version_id', r.object_version_id,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.avatar_uploads r
    WHERE p_token IS NULL OR r.token COLLATE "C" > p_token COLLATE "C"
    ORDER BY r.token COLLATE "C"
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_identity_v1.inventory_avatar_uploads(text,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.inventory_bug_attachment_uploads(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'state', r.state,
        'object_version_id', r.object_version_id,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.bug_attachment_uploads r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_diagnostics_v1.inventory_bug_attachment_uploads(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.inventory_comment_attachment_uploads(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'state', r.state,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.comment_attachment_uploads r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.inventory_comment_attachment_uploads(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.inventory_guide_asset_uploads(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'state', r.state,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.guide_asset_uploads r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_workspace_v1.inventory_guide_asset_uploads(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.inventory_raster_mask_objects(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'state', r.state,
        'stored_sha256', r.stored_sha256,
        'stored_size_bytes', r.stored_size_bytes,
        'canonical_sha256', r.canonical_sha256,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.raster_mask_objects r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.inventory_raster_mask_objects(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_audit_v1.inventory_audit_archive_outputs(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'state', r.state,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.audit_archive_outputs r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_audit_v1.inventory_audit_archive_outputs(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_managed_original_creations(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'location_id', r.location_id,
        'bucket', r.bucket,
        'object_key', r.object_key,
        'storage_identity', r.storage_identity,
        'status', r.status,
        'version_id', r.version_id,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes,
        'etag', r.etag,
        'staging_bucket', r.staging_bucket,
        'staging_key', r.staging_key,
        'staging_ref', yoao_assets_v1._inventory_saved_locator(r.staging_ref, false),
        'staging_ref_present', r.staging_ref IS NOT NULL,
        'saved_endpoint', CASE WHEN r.configuration->>'endpoint' ~ '^https?://[^[:space:]/?#@]+/?$' THEN r.configuration->>'endpoint' END,
        'legacy_staging_bucket', CASE WHEN pg_catalog.jsonb_typeof(r.configuration->'buckets'->'import') = 'string'
            THEN r.configuration->'buckets'->>'import' END,
        'source_unexpanded', r.source IS NOT NULL
    )
    FROM public.managed_original_creations r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_managed_original_creations(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_managed_original_retirements(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'asset_id', r.asset_id,
        'location_id', r.location_id,
        'bucket', r.bucket,
        'object_key', r.object_key,
        'storage_identity', r.storage_identity,
        'reference_state', r.reference_state,
        'identity_state', r.identity_state,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes,
        'etag', r.etag
    )
    FROM public.managed_original_retirements r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_managed_original_retirements(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_dataset_asset_locations(p_asset_id uuid, p_location_id uuid, p_bucket text, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'asset_id', r.asset_id,
        'location_id', r.location_id,
        'bucket', r.bucket,
        'object_key', r.object_key,
        'reference_state', r.reference_state,
        'etag', r.etag,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.dataset_asset_locations r
    WHERE p_asset_id IS NULL OR (r.asset_id, r.location_id, r.bucket COLLATE "C") > (p_asset_id, p_location_id, p_bucket COLLATE "C")
    ORDER BY r.asset_id, r.location_id, r.bucket COLLATE "C"
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_dataset_asset_locations(uuid,uuid,text,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_task_originals(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'location_id', r.location_id,
        'bucket', r.bucket,
        'object_key', r.object_key,
        'storage_identity', r.storage_identity,
        'version_id', r.version_id,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes,
        'etag', r.etag,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.task_originals r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_task_originals(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_dataset_auxiliary_originals(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'location_id', r.location_id,
        'bucket', r.bucket,
        'object_key', r.object_key,
        'storage_identity', r.storage_identity,
        'version_id', r.version_id,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes,
        'etag', r.etag,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.dataset_auxiliary_originals r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_dataset_auxiliary_originals(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_dataset_source_previews(p_source_id uuid, p_path_digest text, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'source_id', r.source_id,
        'path_digest', r.path_digest,
        'asset_id', r.asset_id,
        'location_id', r.location_id,
        'bucket', r.bucket,
        'object_key', r.object_key,
        'state', r.state,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes
    )
    FROM public.dataset_source_previews r
    WHERE p_source_id IS NULL OR (r.source_id, r.path_digest COLLATE "C") > (p_source_id, p_path_digest COLLATE "C")
    ORDER BY r.source_id, r.path_digest COLLATE "C"
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_dataset_source_previews(uuid,text,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_original_cache_entries(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'asset_id', r.asset_id,
        'location_id', r.location_id,
        'bucket', r.bucket,
        'storage_identity', r.storage_identity,
        'state', r.state,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.original_cache_entries r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_original_cache_entries(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.inventory_training_storage_deletions(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'bucket', r.bucket,
        'prefix', r.prefix,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.training_storage_deletions r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.inventory_training_storage_deletions(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.inventory_mask_format_uploads(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'logical_object_key', r.logical_object_key,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.mask_format_uploads r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.inventory_mask_format_uploads(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.inventory_mask_format_imports(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'status', r.status,
        'staged_object_key', r.staged_object_key,
        'staged_sha256', r.staged_sha256,
        'staging_ref', yoao_assets_v1._inventory_saved_locator(r.staging_ref, false),
        'staging_ref_present', r.staging_ref IS NOT NULL
    )
    FROM public.mask_format_imports r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.inventory_mask_format_imports(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.inventory_thumbnail_outputs(p_id uuid, p_limit integer)
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'state', r.state,
        'version_id', r.version_id,
        'sha256', r.sha256,
        'size_bytes', r.size_bytes,
        'etag', r.etag,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, false),
        'ref_present', r.storage_ref IS NOT NULL
    )
    FROM public.thumbnail_outputs r
    WHERE p_id IS NULL OR r.id > p_id
    ORDER BY r.id
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 1001 THEN p_limit ELSE 0 END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.inventory_thumbnail_outputs(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.usage_prediction_input_leases(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.prediction_input_leases r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.usage_prediction_input_leases(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.usage_image_pyramid_outputs(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.image_pyramid_outputs r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.usage_image_pyramid_outputs(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.usage_video_derivative_outputs(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.video_derivative_outputs r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.usage_video_derivative_outputs(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.usage_training_dataset_versions(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'project_id', r.project_id,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.training_dataset_versions r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.usage_training_dataset_versions(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.usage_training_runs(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'project_id', r.project_id,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.training_runs r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.usage_training_runs(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.usage_training_previews(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'project_id', r.project_id,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.training_previews r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.usage_training_previews(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.usage_sample_evaluation_runs(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'project_id', r.project_id,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.sample_evaluation_runs r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.usage_sample_evaluation_runs(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.usage_active_learning_rounds(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'project_id', r.project_id,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.active_learning_rounds r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.usage_active_learning_rounds(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_identity_v1.usage_avatar_uploads(p_ids text[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'token', r.token,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.avatar_uploads r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.token = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_identity_v1.usage_avatar_uploads(text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_diagnostics_v1.usage_bug_attachment_uploads(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'owner_id', r.owner_id,
        'mime_type', r.mime_type,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.bug_attachment_uploads r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_diagnostics_v1.usage_bug_attachment_uploads(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.usage_comment_attachment_uploads(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'annotation_id', r.annotation_id,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.comment_attachment_uploads r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.usage_comment_attachment_uploads(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.usage_guide_asset_uploads(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'project_id', r.project_id,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.guide_asset_uploads r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_workspace_v1.usage_guide_asset_uploads(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.usage_raster_mask_objects(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'storage_encoding', r.storage_encoding,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.raster_mask_objects r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.usage_raster_mask_objects(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_audit_v1.usage_audit_archive_outputs(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'year', r.year,
        'month', r.month,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.audit_archive_outputs r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_audit_v1.usage_audit_archive_outputs(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.usage_original_cache_entries(p_ids uuid[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', r.id,
        'asset_id', r.asset_id,
        'sha256', r.sha256,
        'storage_identity', r.storage_identity,
        'bucket', r.bucket,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true),
        'invalid_ref', r.storage_ref IS NOT NULL
            AND yoao_assets_v1._inventory_saved_locator(r.storage_ref, true) IS NULL
    )
    FROM public.original_cache_entries r
    WHERE pg_catalog.cardinality(p_ids) BETWEEN 1 AND 1000
      AND r.id = ANY(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.usage_original_cache_entries(uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.usage_training_storage_deletions(p_bucket text, p_prefixes text[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true),
        'prefix', r.prefix
    )
    FROM public.training_storage_deletions r
    WHERE pg_catalog.cardinality(p_prefixes) BETWEEN 1 AND 1000
      AND r.bucket = p_bucket AND r.prefix = ANY(p_prefixes)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.usage_training_storage_deletions(text,text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.usage_mask_format_uploads(p_endpoint text, p_bucket text, p_keys text[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT DISTINCT pg_catalog.jsonb_build_object(
        'logical_key', r.logical_object_key,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.storage_ref, true)
    )
    FROM public.mask_format_uploads r
    WHERE pg_catalog.cardinality(p_keys) BETWEEN 1 AND 1000
      AND r.storage_ref->>'endpoint_url' = p_endpoint AND r.storage_ref->>'bucket' = p_bucket
      AND (r.storage_ref->>'key_prefix' || pg_catalog.regexp_replace(r.logical_object_key, '^.*/', '')) = ANY(p_keys)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.usage_mask_format_uploads(text,text,text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.usage_mask_format_imports(p_endpoint text, p_bucket text, p_keys text[])
RETURNS SETOF jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT DISTINCT pg_catalog.jsonb_build_object(
        'logical_key', r.staged_object_key,
        'storage_ref', yoao_assets_v1._inventory_saved_locator(r.staging_ref, true)
    )
    FROM public.mask_format_imports r
    WHERE pg_catalog.cardinality(p_keys) BETWEEN 1 AND 1000
      AND r.staging_ref->>'endpoint_url' = p_endpoint AND r.staging_ref->>'bucket' = p_bucket
      AND (r.staging_ref->>'key_prefix' || pg_catalog.regexp_replace(r.staged_object_key, '^.*/', '')) = ANY(p_keys)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.usage_mask_format_imports(text,text,text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.usage_original_and_preview_claims(p_legacy uuid, p_config jsonb, p_endpoint text, p_bucket text, p_identity text, p_keys text[], p_tasks uuid[], p_auxiliary uuid[])
RETURNS TABLE(kind text, data jsonb)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH candidate_keys AS (
        SELECT pg_catalog.md5(k) AS key_hash FROM unnest(p_keys) AS k
        WHERE pg_catalog.cardinality(p_keys) BETWEEN 1 AND 1000
    ), legacy_location AS (
        SELECT id FROM public.storage_locations
        WHERE id = p_legacy AND kind = 's3' AND config_revision = 1 AND config = p_config
    ), claims AS (
        SELECT 'managed_original'::text AS kind, r.object_key::text AS object_key FROM public.managed_original_creations r
        WHERE r.storage_identity = p_identity AND r.bucket = p_bucket AND r.object_key = ANY(p_keys)
        UNION
        SELECT 'temporary'::text AS kind, r.staging_key::text AS object_key FROM public.managed_original_creations r
        WHERE coalesce(r.staging_ref->>'endpoint_url', r.configuration->>'endpoint') = p_endpoint
          AND r.staging_bucket = p_bucket AND r.staging_key = ANY(p_keys)
        UNION
        SELECT 'managed_original'::text AS kind, r.object_key::text AS object_key FROM public.dataset_asset_locations r
        WHERE pg_catalog.md5(r.bucket) = pg_catalog.md5(p_bucket) AND pg_catalog.md5(r.object_key) IN (SELECT key_hash FROM candidate_keys)
          AND r.bucket = p_bucket AND r.object_key = ANY(p_keys) AND r.role = 'managed_original'
          AND r.location_id IN (SELECT id FROM legacy_location)
        UNION
        SELECT 'managed_original'::text AS kind, r.object_key::text AS object_key FROM public.managed_original_retirements r
        WHERE pg_catalog.md5(r.bucket) = pg_catalog.md5(p_bucket) AND pg_catalog.md5(r.object_key) IN (SELECT key_hash FROM candidate_keys)
          AND r.bucket = p_bucket AND r.object_key = ANY(p_keys) AND r.identity_state = 'verified_binding' AND r.storage_identity = p_identity
        UNION
        SELECT 'preview_cache'::text AS kind, r.object_key::text AS object_key FROM public.dataset_source_previews r
        WHERE pg_catalog.md5(r.bucket) = pg_catalog.md5(p_bucket) AND pg_catalog.md5(r.object_key) IN (SELECT key_hash FROM candidate_keys)
          AND r.bucket = p_bucket AND r.object_key = ANY(p_keys)
          AND r.location_id IN (SELECT id FROM legacy_location)
        UNION
        SELECT 'preview_cache'::text AS kind, (r.storage_ref->>'prefix' || 'thumbnail.webp')::text AS object_key FROM public.thumbnail_outputs r
        WHERE r.storage_ref->>'endpoint_url' = p_endpoint AND r.storage_ref->>'bucket' = p_bucket
          AND r.storage_ref->>'storage_identity' = p_identity
          AND (r.storage_ref->>'prefix' || 'thumbnail.webp') = ANY(p_keys)
        UNION
        SELECT 'managed_original'::text AS kind, r.object_key::text AS object_key FROM public.dataset_asset_locations r
        WHERE pg_catalog.md5(r.bucket) = pg_catalog.md5(p_bucket) AND pg_catalog.md5(r.object_key) IN (SELECT key_hash FROM candidate_keys)
          AND r.bucket = p_bucket AND r.object_key = ANY(p_keys)
          AND r.storage_ref->>'endpoint_url' = p_endpoint AND r.storage_ref->>'storage_identity' = p_identity
          AND r.storage_ref->>'object_key' = r.object_key AND r.storage_ref->>'bucket' = r.bucket
        UNION
        SELECT 'managed_original'::text AS kind, r.object_key::text AS object_key FROM public.dataset_auxiliary_originals r
        WHERE pg_catalog.md5(r.bucket) = pg_catalog.md5(p_bucket) AND pg_catalog.md5(r.object_key) IN (SELECT key_hash FROM candidate_keys)
          AND r.bucket = p_bucket AND r.object_key = ANY(p_keys)
          AND r.storage_ref->>'endpoint_url' = p_endpoint AND r.storage_ref->>'storage_identity' = p_identity
          AND r.storage_ref->>'object_key' = r.object_key AND r.storage_ref->>'bucket' = r.bucket
        UNION
        SELECT 'managed_original'::text AS kind, r.object_key::text AS object_key FROM public.task_originals r
        WHERE pg_catalog.md5(r.bucket) = pg_catalog.md5(p_bucket) AND pg_catalog.md5(r.object_key) IN (SELECT key_hash FROM candidate_keys)
          AND r.bucket = p_bucket AND r.object_key = ANY(p_keys)
          AND r.storage_ref->>'endpoint_url' = p_endpoint AND r.storage_ref->>'storage_identity' = p_identity
          AND r.storage_ref->>'object_key' = r.object_key AND r.storage_ref->>'bucket' = r.bucket
        UNION
        SELECT 'managed_original'::text AS kind, r.object_key::text AS object_key FROM public.task_originals r
        WHERE pg_catalog.cardinality(p_tasks) <= 1000 AND r.id = ANY(p_tasks)
          AND r.storage_identity = p_identity AND r.bucket = p_bucket AND r.object_key = ANY(p_keys)
        UNION
        SELECT 'managed_original'::text AS kind, r.object_key::text AS object_key FROM public.dataset_auxiliary_originals r
        WHERE pg_catalog.cardinality(p_auxiliary) <= 1000 AND r.id = ANY(p_auxiliary)
          AND r.storage_identity = p_identity AND r.bucket = p_bucket AND r.object_key = ANY(p_keys)
    )
    SELECT c.kind, pg_catalog.jsonb_build_object('key', c.object_key)
    FROM claims c
    WHERE pg_catalog.cardinality(p_keys) BETWEEN 1 AND 1000
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.usage_original_and_preview_claims(uuid,jsonb,text,text,text,text[],uuid[],uuid[]) FROM PUBLIC;

-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.usage_inventory_status()
RETURNS TABLE(async_job_id uuid, status text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT i.async_job_id, i.status::text FROM public.storage_usage_inventories i WHERE i.id = 'platform'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.usage_inventory_status() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.usage_snapshot_facts(p_keys text[])
RETURNS TABLE(scope_key text, object_count bigint, total_size_bytes bigint, category_totals jsonb,
    measurement_started_at timestamptz, measured_at timestamptz, attempt_status text, attempted_at timestamptz, error_code text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT s.scope_key::text, s.object_count, s.total_size_bytes, s.category_totals,
           s.measurement_started_at, s.measured_at, s.attempt_status::text, s.attempted_at, s.error_code::text
    FROM public.storage_usage_snapshots s
    WHERE pg_catalog.cardinality(p_keys) BETWEEN 1 AND 9 AND s.scope_key = ANY(p_keys)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.usage_snapshot_facts(text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_usage_inventory(p_bootstrap_at timestamptz)
RETURNS TABLE(id text, async_job_id uuid, status text, authority jsonb, scope jsonb, bucket_index integer,
    checkpoint jsonb, success_count integer, failed_count integer, generation integer,
    lease_until timestamptz, started_at timestamptz)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_bootstrap_at IS NOT NULL THEN
        INSERT INTO public.storage_usage_inventories
            (id, status, authority, scope, bucket_index, checkpoint, success_count, failed_count, generation, started_at)
        VALUES ('platform', 'never', '{}'::jsonb, '[]'::jsonb, 0, '{}'::jsonb, 0, 0, 0, p_bootstrap_at)
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN QUERY SELECT i.id::text, i.async_job_id, i.status::text, i.authority, i.scope, i.bucket_index,
               i.checkpoint, i.success_count, i.failed_count, i.generation, i.lease_until, i.started_at
    FROM public.storage_usage_inventories i WHERE i.id = 'platform' FOR UPDATE;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_usage_inventory(timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_expired_usage_inventory(p_now timestamptz)
RETURNS TABLE(id text, async_job_id uuid, status text, authority jsonb, scope jsonb, bucket_index integer,
    checkpoint jsonb, success_count integer, failed_count integer, generation integer,
    lease_until timestamptz, started_at timestamptz)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT i.id::text, i.async_job_id, i.status::text, i.authority, i.scope, i.bucket_index,
               i.checkpoint, i.success_count, i.failed_count, i.generation, i.lease_until, i.started_at
    FROM public.storage_usage_inventories i
    WHERE i.id = 'platform' AND i.status IN ('pending', 'running') AND i.lease_until <= p_now
    FOR UPDATE SKIP LOCKED
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_expired_usage_inventory(timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.start_usage_inventory(p_job uuid, p_generation integer, p_authority jsonb, p_scope jsonb, p_now timestamptz, p_lease timestamptz)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_scope) IS DISTINCT FROM 'array' OR pg_catalog.jsonb_array_length(p_scope) NOT BETWEEN 1 AND 9 THEN
        RETURN false;
    END IF;
    UPDATE public.storage_usage_inventories
    SET async_job_id = p_job, status = 'pending', authority = p_authority, scope = p_scope,
        bucket_index = 0, checkpoint = '{}'::jsonb, success_count = 0, failed_count = 0,
        generation = generation + 1, started_at = p_now, lease_until = p_lease
    WHERE id = 'platform' AND generation = p_generation AND status NOT IN ('pending','running');
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.storage_usage_snapshots (scope_key, attempt_status, attempted_at)
    SELECT DISTINCT item->>'key', 'pending', p_now FROM pg_catalog.jsonb_array_elements(p_scope) AS item
    ON CONFLICT (scope_key) DO UPDATE
    SET attempt_status = 'pending', attempted_at = EXCLUDED.attempted_at, error_code = NULL;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.start_usage_inventory(uuid,integer,jsonb,jsonb,timestamptz,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.claim_usage_inventory(p_job uuid, p_generation integer, p_lease timestamptz)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH changed AS (
        UPDATE public.storage_usage_inventories SET status = 'running', lease_until = p_lease
        WHERE id = 'platform' AND async_job_id = p_job AND generation = p_generation AND status = 'pending'
        RETURNING id
    ) SELECT EXISTS (SELECT FROM changed)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.claim_usage_inventory(uuid,integer,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.begin_usage_inventory_page(p_job uuid, p_generation integer, p_index integer, p_checkpoint jsonb)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key text;
BEGIN
    UPDATE public.storage_usage_inventories
    SET checkpoint = p_checkpoint
    WHERE id = 'platform' AND async_job_id = p_job AND generation = p_generation
      AND bucket_index = p_index AND status IN ('pending','running')
      AND p_index >= 0 AND (scope->p_index) ? 'key'
    RETURNING scope->p_index->>'key' INTO v_key;
    IF NOT FOUND OR v_key IS NULL THEN RETURN false; END IF;
    UPDATE public.storage_usage_snapshots SET attempt_status = 'running' WHERE scope_key = v_key;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.begin_usage_inventory_page(uuid,integer,integer,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_usage_inventory_page(p_job uuid, p_generation integer, p_index integer, p_checkpoint jsonb, p_lease timestamptz, p_outcome text, p_error text, p_count bigint, p_size bigint, p_categories jsonb, p_started timestamptz, p_measured timestamptz)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_key text;
BEGIN
    IF p_outcome NOT IN ('continue','completed','failed') OR p_outcome IS NULL
        OR (p_outcome = 'completed' AND (
            p_count IS NULL OR p_count < 0 OR p_size IS NULL OR p_size < 0
            OR p_started IS NULL OR p_measured IS NULL OR p_measured < p_started
        )) THEN
        RETURN false;
    END IF;
    UPDATE public.storage_usage_inventories
    SET checkpoint = CASE WHEN p_outcome = 'continue' THEN p_checkpoint ELSE '{}'::jsonb END,
        bucket_index = bucket_index + (p_outcome <> 'continue')::integer,
        success_count = success_count + (p_outcome = 'completed')::integer,
        failed_count = failed_count + (p_outcome = 'failed')::integer,
        lease_until = p_lease
    WHERE id = 'platform' AND async_job_id = p_job AND generation = p_generation
      AND bucket_index = p_index AND status IN ('pending','running')
      AND p_index >= 0 AND (scope->p_index) ? 'key'
    RETURNING scope->p_index->>'key' INTO v_key;
    IF NOT FOUND OR v_key IS NULL THEN RETURN false; END IF;
    IF p_outcome = 'failed' THEN
        UPDATE public.storage_usage_snapshots SET attempt_status = p_outcome, error_code = p_error WHERE scope_key = v_key;
    ELSIF p_outcome = 'completed' THEN
        UPDATE public.storage_usage_snapshots
        SET attempt_status = p_outcome, error_code = NULL, object_count = p_count, total_size_bytes = p_size,
            category_totals = p_categories, measurement_started_at = p_started, measured_at = p_measured
        WHERE scope_key = v_key;
    ELSE
        RETURN true;
    END IF;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.record_usage_inventory_page(uuid,integer,integer,jsonb,timestamptz,text,text,bigint,bigint,jsonb,timestamptz,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.release_usage_inventory_chunk(p_job uuid, p_generation integer, p_lease timestamptz)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH changed AS (
        UPDATE public.storage_usage_inventories
        SET status = 'pending', generation = generation + 1, lease_until = coalesce(p_lease, lease_until)
        WHERE id = 'platform' AND async_job_id = p_job AND generation = p_generation AND status IN ('pending','running')
        RETURNING id
    ) SELECT EXISTS (SELECT FROM changed)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.release_usage_inventory_chunk(uuid,integer,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.finish_usage_inventory(p_job uuid, p_generation integer, p_status text, p_error text)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_scope jsonb;
    v_index integer;
BEGIN
    IF p_status NOT IN ('completed','failed','cancelled') OR p_status IS NULL THEN RETURN false; END IF;
    UPDATE public.storage_usage_inventories
    SET status = p_status, generation = generation + 1, lease_until = NULL
    WHERE id = 'platform' AND async_job_id IS NOT DISTINCT FROM p_job
      AND generation = p_generation AND status IN ('pending','running')
    RETURNING scope, bucket_index INTO v_scope, v_index;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.storage_usage_snapshots s SET attempt_status = p_status, error_code = p_error
    WHERE s.scope_key IN (
        SELECT item->>'key' FROM pg_catalog.jsonb_array_elements(v_scope) WITH ORDINALITY a(item, n)
        WHERE n > v_index
    );
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.finish_usage_inventory(uuid,integer,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.usage_schedule_summary()
RETURNS TABLE(enabled boolean, interval_hours integer, status text, next_due_at timestamptz, last_started_at timestamptz, pause_reason text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT s.enabled, s.interval_hours, s.status::text, s.next_due_at, s.last_started_at, s.pause_reason::text
    FROM public.storage_usage_schedules s WHERE s.id = 'platform'
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.usage_schedule_summary() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_active_usage_schedule()
RETURNS TABLE(enabled boolean, interval_hours integer, status text, next_due_at timestamptz, last_started_at timestamptz, pause_reason text, authority jsonb, scope jsonb)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT s.enabled, s.interval_hours, s.status::text, s.next_due_at, s.last_started_at, s.pause_reason::text, s.authority, s.scope
    FROM public.storage_usage_schedules s
    WHERE s.id = 'platform' AND s.enabled AND s.status = 'active' FOR UPDATE SKIP LOCKED
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_active_usage_schedule() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.configure_usage_schedule(p_enabled boolean, p_hours integer, p_authority jsonb, p_scope jsonb, p_now timestamptz)
RETURNS TABLE(enabled boolean, interval_hours integer, status text, next_due_at timestamptz, last_started_at timestamptz, pause_reason text)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.storage_usage_schedules AS s
        (id, enabled, interval_hours, status, authority, scope, next_due_at)
    VALUES ('platform', p_enabled, p_hours, CASE WHEN p_enabled THEN 'active' ELSE 'off' END,
        p_authority, p_scope, CASE WHEN p_enabled THEN p_now END)
    ON CONFLICT (id) DO UPDATE SET enabled = EXCLUDED.enabled, interval_hours = EXCLUDED.interval_hours,
        status = EXCLUDED.status, authority = EXCLUDED.authority, scope = EXCLUDED.scope,
        next_due_at = EXCLUDED.next_due_at, pause_reason = NULL
    RETURNING s.enabled, s.interval_hours, s.status::text, s.next_due_at, s.last_started_at, s.pause_reason::text
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.configure_usage_schedule(boolean,integer,jsonb,jsonb,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.pause_usage_schedule(p_reason text)
RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.storage_usage_schedules SET status = 'paused', pause_reason = p_reason, next_due_at = NULL
    WHERE id = 'platform' AND enabled AND status = 'active' AND p_reason IN ('access_revoked','configuration_changed')
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.pause_usage_schedule(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.advance_usage_schedule(p_started timestamptz)
RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.storage_usage_schedules SET last_started_at = p_started,
        next_due_at = p_started + pg_catalog.make_interval(hours => interval_hours)
    WHERE id = 'platform' AND enabled AND status = 'active' AND next_due_at <= p_started
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.advance_usage_schedule(timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.begin_usage_mutation(p_id uuid, p_identity text, p_new_identity text, p_scope text, p_key text, p_producer text, p_owner uuid, p_purpose text, p_kind text, p_version text, p_evidence jsonb)
RETURNS TABLE(id uuid, fresh boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_id uuid;
    v_existing public.storage_usage_mutations%ROWTYPE;
    v_hash text;
BEGIN
    IF p_key IS NULL OR pg_catalog.octet_length(p_key) NOT BETWEEN 1 AND 1024 OR p_kind NOT IN ('create','delete') THEN
        RAISE EXCEPTION 'invalid_accounting_address' USING ERRCODE = '22023';
    END IF;
    v_hash := pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(p_key, 'UTF8')), 'hex');
    INSERT INTO public.storage_usage_mutations AS m
        (id, idempotency_key, scope_key, object_key, key_hash, producer, purpose, owner_id, kind, expected_version, evidence)
    VALUES (p_id, p_identity, p_scope, p_key, v_hash, p_producer, p_purpose, p_owner, p_kind, p_version, p_evidence)
    ON CONFLICT (idempotency_key) DO NOTHING RETURNING m.id INTO v_id;
    IF FOUND THEN RETURN QUERY SELECT v_id, true; RETURN; END IF;
    SELECT m.* INTO STRICT v_existing FROM public.storage_usage_mutations m
    WHERE m.idempotency_key = p_identity FOR UPDATE;
    IF v_existing.object_key <> p_key OR v_existing.scope_key <> p_scope OR v_existing.kind <> p_kind THEN
        RETURN QUERY SELECT NULL::uuid, false;
        RETURN;
    END IF;
    IF p_new_identity IS NULL THEN RETURN QUERY SELECT v_existing.id, false; RETURN; END IF;
    INSERT INTO public.storage_usage_mutations
        (id, idempotency_key, scope_key, object_key, key_hash, producer, purpose, owner_id, kind, expected_version, evidence)
    VALUES (p_id, p_new_identity, p_scope, p_key, v_hash, p_producer, p_purpose, p_owner, p_kind, p_version,
        p_evidence || pg_catalog.jsonb_build_object('logical_intent_id', v_existing.id));
    RETURN QUERY SELECT p_id, true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.begin_usage_mutation(uuid,text,text,text,text,text,uuid,text,text,text,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.publish_usage_mutation(p_id uuid, p_fresh boolean, p_settled boolean, p_delta bigint, p_outcome text, p_size bigint, p_fact jsonb)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_old public.storage_usage_mutations%ROWTYPE;
    v_fact jsonb := p_fact;
    v_delta bigint := CASE WHEN p_fresh AND p_settled THEN p_delta END;
    v_size bigint := p_size;
    v_outcome text := p_outcome;
BEGIN
    SELECT m.* INTO v_old FROM public.storage_usage_mutations m WHERE m.id = p_id FOR UPDATE;
    IF NOT FOUND OR v_old.state = 'confirmed' THEN RETURN false; END IF;
    IF v_old.physical_outcome IN ('written','deleted') THEN
        v_outcome := v_old.physical_outcome;
        v_size := coalesce(v_old.physical_size_bytes, v_size);
        IF v_old.evidence->>'returned_version' IS NOT NULL THEN
            IF v_fact->>'returned_version' IS NOT NULL
                AND v_fact->'returned_version' IS DISTINCT FROM v_old.evidence->'returned_version' THEN
                v_fact := v_fact || pg_catalog.jsonb_build_object('later_returned_version', v_fact->'returned_version');
            END IF;
            v_fact := v_fact || pg_catalog.jsonb_build_object('returned_version', v_old.evidence->'returned_version');
        END IF;
    END IF;
    UPDATE public.storage_usage_mutations
    SET physical_outcome = v_outcome, physical_size_bytes = v_size, current_delta_bytes = v_delta,
        evidence = coalesce(v_old.evidence, '{}'::jsonb) || v_fact,
        state = CASE WHEN v_delta IS NULL THEN 'unknown' ELSE 'confirmed' END,
        finished_at = pg_catalog.clock_timestamp()
    WHERE id = p_id;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.publish_usage_mutation(uuid,boolean,boolean,bigint,text,bigint,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.stage_usage_inventory_objects(p_generation uuid, p_scope text, p_objects jsonb, p_started timestamptz, p_completed timestamptz)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_objects) IS DISTINCT FROM 'array'
        OR pg_catalog.jsonb_array_length(p_objects) > 1000 THEN
        RAISE EXCEPTION 'invalid_inventory_page' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.storage_usage_inventory_objects
        (generation, scope_key, key_hash, object_key, size_bytes, observed_from, observed_through)
    SELECT p_generation, p_scope, pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(a.object_key, 'UTF8')), 'hex'),
           CASE WHEN pg_catalog.octet_length(a.object_key) BETWEEN 1 AND 1024 THEN a.object_key END,
           a.size_bytes, p_started, p_completed
    FROM pg_catalog.jsonb_to_recordset(p_objects) AS a(object_key text, size_bytes bigint);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.stage_usage_inventory_objects(uuid,text,jsonb,timestamptz,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reconcile_usage_bucket(p_scope text, p_generation uuid, p_started timestamptz, p_completed timestamptz)
RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_previous uuid;
    v_start timestamptz;
    v_end timestamptz;
    v_summary jsonb;
BEGIN
    SELECT s.object_generation INTO STRICT v_previous
    FROM public.storage_usage_snapshots s WHERE s.scope_key = p_scope FOR UPDATE;
    SELECT r.started_at, r.measured_at INTO v_start, v_end
    FROM public.storage_usage_reconciliations r WHERE r.generation = v_previous AND r.scope_key = p_scope;
    IF FOUND THEN
        WITH p AS (
            SELECT key_hash, object_key, size_bytes, observed_from, observed_through
            FROM public.storage_usage_inventory_objects
            WHERE scope_key = p_scope AND generation = v_previous
        ), c AS (
            SELECT key_hash, object_key, size_bytes, observed_from, observed_through
            FROM public.storage_usage_inventory_objects
            WHERE scope_key = p_scope AND generation = p_generation
        ), addresses AS (
            SELECT key_hash, object_key FROM p UNION SELECT key_hash, object_key FROM c
            UNION SELECT key_hash, object_key FROM public.storage_usage_mutations
            WHERE scope_key = p_scope AND started_at <= p_completed
                AND (finished_at IS NULL OR finished_at >= v_start)
        ), compared AS (
            SELECT a.key_hash, a.object_key,
                coalesce(c.size_bytes, 0) - coalesce(p.size_bytes, 0) AS observed_delta,
                coalesce(p.observed_from, v_start) AS previous_from,
                coalesce(p.observed_through, v_end) AS previous_through,
                coalesce(c.observed_from, p_started) AS current_from,
                coalesce(c.observed_through, p_completed) AS current_through
            FROM addresses a
            LEFT JOIN p ON p.key_hash = a.key_hash AND p.object_key = a.object_key
            LEFT JOIN c ON c.key_hash = a.key_hash AND c.object_key = a.object_key
        ), facts AS (
            SELECT x.observed_delta, m.known_delta, m.uncertain
            FROM compared x CROSS JOIN LATERAL (
                SELECT coalesce(sum(current_delta_bytes) FILTER (
                    WHERE state = 'confirmed' AND started_at > x.previous_through
                        AND finished_at < x.current_from), 0) AS known_delta,
                    coalesce(bool_or(
                        state <> 'confirmed' OR current_delta_bytes IS NULL
                        OR (current_delta_bytes <> 0 AND NOT (
                            started_at > x.previous_through AND finished_at < x.current_from))
                    ), false) AS uncertain
                FROM public.storage_usage_mutations m WHERE m.scope_key = p_scope
                    AND m.key_hash = x.key_hash AND m.object_key = x.object_key
                    AND m.started_at <= x.current_through
                    AND (m.finished_at IS NULL OR m.finished_at >= x.previous_from)
            ) m
        )
        SELECT pg_catalog.jsonb_build_object(
            'status', 'partial',
            'compared_object_count', count(*),
            'uncertain_object_count', count(*) FILTER (WHERE uncertain),
            'unexplained_object_count', count(*) FILTER (WHERE NOT uncertain AND observed_delta <> known_delta),
            'confirmed_change_bytes', coalesce(sum(known_delta), 0),
            'known_unexplained_change_bytes', coalesce(sum(observed_delta - known_delta) FILTER (WHERE NOT uncertain), 0)
        ) INTO v_summary FROM facts;
    ELSE
        v_summary := '{"status":"no_baseline","compared_object_count":0,"uncertain_object_count":0,"unexplained_object_count":0,"confirmed_change_bytes":0,"known_unexplained_change_bytes":0}'::jsonb;
    END IF;
    v_summary := v_summary || pg_catalog.jsonb_build_object('unexplained_change_bytes',
        CASE WHEN v_summary->>'status' <> 'no_baseline' AND (v_summary->>'uncertain_object_count')::bigint = 0
             THEN v_summary->'known_unexplained_change_bytes' END);
    INSERT INTO public.storage_usage_reconciliations (generation, scope_key, previous_generation, started_at, measured_at, summary)
    VALUES (p_generation, p_scope, v_previous, p_started, p_completed, v_summary);
    UPDATE public.storage_usage_snapshots SET object_generation = p_generation WHERE scope_key = p_scope;
    DELETE FROM public.storage_usage_inventory_objects o
    WHERE o.scope_key = p_scope AND o.generation <> p_generation AND o.generation IS DISTINCT FROM v_previous;
    DELETE FROM public.storage_usage_reconciliations r
    WHERE r.scope_key = p_scope AND r.generation <> p_generation AND r.generation IS DISTINCT FROM v_previous;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.reconcile_usage_bucket(text,uuid,timestamptz,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.discard_usage_staging(p_generation uuid, p_scope text)
RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    DELETE FROM public.storage_usage_inventory_objects o
    WHERE o.generation = p_generation AND (p_scope IS NULL OR o.scope_key = p_scope)
      AND NOT EXISTS (SELECT FROM public.storage_usage_snapshots s
          WHERE s.object_generation = o.generation AND s.scope_key = o.scope_key)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.discard_usage_staging(uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.usage_mutation_summary(p_keys text[])
RETURNS TABLE(confirmed_added_bytes numeric, confirmed_removed_bytes numeric, net_change_bytes numeric,
    pending_operation_count bigint, unknown_operation_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT coalesce(sum(m.current_delta_bytes) FILTER (WHERE m.current_delta_bytes > 0),0),
           coalesce(-sum(m.current_delta_bytes) FILTER (WHERE m.current_delta_bytes < 0),0),
           coalesce(sum(m.current_delta_bytes),0), count(*) FILTER (WHERE m.state = 'pending'),
           count(*) FILTER (WHERE m.state = 'unknown')
    FROM public.storage_usage_mutations m
    WHERE pg_catalog.cardinality(p_keys) BETWEEN 1 AND 9 AND m.scope_key = ANY(p_keys)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.usage_mutation_summary(text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.usage_reconciliation_facts(p_keys text[])
RETURNS TABLE(summary jsonb, measured_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.summary, r.measured_at
    FROM public.storage_usage_reconciliations r JOIN public.storage_usage_snapshots s
      ON s.scope_key = r.scope_key AND s.object_generation = r.generation
    WHERE pg_catalog.cardinality(p_keys) BETWEEN 1 AND 9 AND s.scope_key = ANY(p_keys)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.usage_reconciliation_facts(text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_inventory_binding_context()
RETURNS TABLE(purpose text, location_id uuid, bucket text, generation integer, location_kind text,
    location_config jsonb, config_revision integer, connection_id uuid, connection_kind text,
    connection_scope text, connection_config jsonb, connection_revision integer)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT b.purpose::text, b.location_id, b.bucket::text, b.generation, l.kind::text,
        pg_catalog.jsonb_build_object(
            'endpoint', CASE WHEN l.kind = 's3' AND l.config->>'endpoint' ~ '^(https?://)?[^[:space:]/?#@]+/?$'
                THEN l.config->>'endpoint' END,
            'use_ssl', CASE WHEN pg_catalog.jsonb_typeof(l.config->'use_ssl') = 'boolean'
                THEN l.config->'use_ssl' END
        ), l.config_revision,
        l.connection_id, c.kind::text, c.scope::text,
        pg_catalog.jsonb_build_object(
            'endpoint', CASE WHEN c.kind = 's3' AND c.config->>'endpoint' ~ '^(https?://)?[^[:space:]/?#@]+/?$'
                THEN c.config->>'endpoint' END,
            'use_ssl', CASE WHEN pg_catalog.jsonb_typeof(c.config->'use_ssl') = 'boolean'
                THEN c.config->'use_ssl' END,
            'bucket', CASE WHEN c.kind = 's3' AND pg_catalog.jsonb_typeof(c.config->'bucket') = 'string'
                THEN c.config->>'bucket' END
        ), c.revision
    FROM public.storage_bindings b LEFT JOIN public.storage_locations l ON l.id = b.location_id
    LEFT JOIN public.storage_connections c ON c.id = l.connection_id
    ORDER BY b.purpose LIMIT 101
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_inventory_binding_context() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_location_kind_counts()
RETURNS TABLE(kind text, count bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT l.kind::text, count(*) FROM public.storage_locations l GROUP BY l.kind ORDER BY l.kind LIMIT 101
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_location_kind_counts() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_target_reference_footprint(p_location uuid)
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
        coalesce(pg_catalog.string_agg(
            pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
                pg_catalog.jsonb_build_array(r.id, r.target_location_id, r.input_fingerprint, r.baseline_sha256)::text,
                'UTF8')), 'hex'), '' ORDER BY r.id, r.target_location_id, r.input_fingerprint, r.baseline_sha256
        ), ''), 'UTF8')), 'hex')
    FROM public.storage_migration_copies r WHERE r.target_location_id = p_location
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_target_reference_footprint(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._migration_inventory_job_payload(p_window_id uuid, p_window_revision integer)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT '{"scope":"selected-configured-source","migration_ready":false}'::jsonb ||
           CASE WHEN p_window_id IS NULL THEN '{}'::jsonb ELSE
               pg_catalog.jsonb_build_object('window_id', p_window_id, 'window_revision', p_window_revision)
           END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._migration_inventory_job_payload(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_migration_inventory(p_job_id uuid, p_lock boolean)
RETURNS TABLE(
    async_job_id uuid, window_id uuid, window_revision integer, status character varying,
    created_at timestamptz, started_at timestamptz, completed_at timestamptz, source_location_id uuid,
    limits jsonb, handling jsonb, cancel_requested_at timestamptz, complete boolean, counts jsonb,
    error_code character varying, report_sha256 character varying, report_size_bytes integer,
    requested_by uuid, task_id character varying, source jsonb, authority jsonb,
    input_fingerprint character varying, maintenance_revision integer, operation_id uuid
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        PERFORM 1 FROM public.storage_migration_inventories i WHERE i.async_job_id = p_job_id FOR UPDATE;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    RETURN QUERY SELECT i.async_job_id, i.window_id, i.window_revision, i.status,
           i.created_at, i.started_at, i.completed_at, i.source_location_id, i.limits,
           i.handling, i.cancel_requested_at, i.complete, i.counts, i.error_code,
           i.report_sha256, i.report_size_bytes, i.requested_by, i.task_id, i.source,
           i.authority, i.input_fingerprint, i.maintenance_revision, i.operation_id
    FROM public.storage_migration_inventories i WHERE i.async_job_id = p_job_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.read_migration_inventory(uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_migration_inventory_report(p_job_id uuid, p_lock boolean)
RETURNS TABLE(
    async_job_id uuid, window_id uuid, window_revision integer, status character varying,
    created_at timestamptz, started_at timestamptz, completed_at timestamptz, source_location_id uuid,
    limits jsonb, handling jsonb, cancel_requested_at timestamptz, complete boolean, counts jsonb,
    error_code character varying, report_sha256 character varying, report_size_bytes integer,
    requested_by uuid, task_id character varying, source jsonb, authority jsonb,
    input_fingerprint character varying, maintenance_revision integer, operation_id uuid, report bytea
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        PERFORM 1 FROM public.storage_migration_inventories i WHERE i.async_job_id = p_job_id FOR UPDATE;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    RETURN QUERY SELECT i.async_job_id, i.window_id, i.window_revision, i.status,
           i.created_at, i.started_at, i.completed_at, i.source_location_id, i.limits,
           i.handling, i.cancel_requested_at, i.complete, i.counts, i.error_code,
           i.report_sha256, i.report_size_bytes, i.requested_by, i.task_id, i.source,
           i.authority, i.input_fingerprint, i.maintenance_revision, i.operation_id, i.report
    FROM public.storage_migration_inventories i WHERE i.async_job_id = p_job_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.read_migration_inventory_report(uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_inventory_history(p_limit integer, p_offset integer)
RETURNS TABLE(
    async_job_id uuid, window_id uuid, window_revision integer, status character varying,
    created_at timestamptz, started_at timestamptz, completed_at timestamptz, source_location_id uuid,
    limits jsonb, handling jsonb, cancel_requested_at timestamptz, complete boolean, counts jsonb,
    error_code character varying, report_sha256 character varying, report_size_bytes integer
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT i.async_job_id, i.window_id, i.window_revision, i.status,
           i.created_at, i.started_at, i.completed_at, i.source_location_id, i.limits,
           i.handling, i.cancel_requested_at, i.complete, i.counts, i.error_code,
           i.report_sha256, i.report_size_bytes
    FROM public.storage_migration_inventories i
    ORDER BY i.created_at DESC, i.async_job_id DESC
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 100 AND p_offset BETWEEN 0 AND 1000000 THEN p_limit ELSE 0 END
    OFFSET GREATEST(p_offset, 0)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_inventory_history(integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_inventory_history_count()
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.count(i.async_job_id) FROM public.storage_migration_inventories i
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_inventory_history_count() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_window_has_active_inventory(p_window_id uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.storage_migration_inventories i
        WHERE i.window_id = p_window_id AND i.status IN ('pending', 'running')
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_window_has_active_inventory(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_active_migration_inventory()
RETURNS uuid
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT i.async_job_id FROM public.storage_migration_inventories i
    WHERE i.source_location_id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid
      AND i.status IN ('pending', 'running') FOR UPDATE
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_active_migration_inventory() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.queue_migration_inventory(
    p_job_id uuid, p_actor_id uuid, p_task_id text, p_window_id uuid, p_window_revision integer,
    p_maintenance_revision integer, p_source jsonb, p_limits jsonb, p_handling jsonb,
    p_authority jsonb, p_fingerprint text
)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH queued AS (
        INSERT INTO public.storage_migration_inventories (
            async_job_id, requested_by, task_id, window_id, window_revision, maintenance_revision,
            source_location_id, source, limits, handling, authority, input_fingerprint, status
        )
        SELECT j.id, p_actor_id, p_task_id, p_window_id, p_window_revision, p_maintenance_revision,
               '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid, p_source, p_limits, p_handling,
               p_authority, p_fingerprint, 'pending'
        FROM yoao_jobs_v1.read_job(p_job_id) j
        WHERE j.kind = 'storage_migration_inventory' AND j.status = 'pending'
          AND j.user_id = p_actor_id AND j.celery_task_id = p_task_id
          AND j.payload = yoao_assets_v1._migration_inventory_job_payload(p_window_id, p_window_revision)
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM queued)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.queue_migration_inventory(uuid,uuid,text,uuid,integer,integer,jsonb,jsonb,jsonb,jsonb,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.claim_migration_inventory(p_job_id uuid, p_task_id text, p_started_at timestamptz)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH claimed AS (
        UPDATE public.storage_migration_inventories i SET status = 'running', started_at = p_started_at
        WHERE i.async_job_id = p_job_id AND i.task_id = p_task_id AND i.status = 'pending'
          AND i.cancel_requested_at IS NULL
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM claimed)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.claim_migration_inventory(uuid,text,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.request_migration_inventory_cancel(p_job_id uuid, p_task_id text, p_requested_at timestamptz)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH requested AS (
        UPDATE public.storage_migration_inventories i
        SET cancel_requested_at = COALESCE(i.cancel_requested_at, p_requested_at)
        WHERE i.async_job_id = p_job_id AND i.task_id = p_task_id AND i.status = 'running'
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM requested)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.request_migration_inventory_cancel(uuid,text,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.attach_migration_inventory_operation(p_job_id uuid, p_task_id text, p_operation_id uuid)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH attached AS (
        UPDATE public.storage_migration_inventories i SET operation_id = p_operation_id
        WHERE i.async_job_id = p_job_id AND i.task_id = p_task_id AND i.status = 'running'
          AND i.operation_id IS NULL AND i.cancel_requested_at IS NULL
          AND EXISTS (
              SELECT 1 FROM public.storage_operations o
              WHERE o.id = p_operation_id AND o.location_id = i.source_location_id
                AND o.revision = i.maintenance_revision AND o.purpose = 'storage_migration_inventory'
          )
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM attached)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.attach_migration_inventory_operation(uuid,text,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.end_migration_inventory(
    p_job_id uuid, p_task_id text, p_expected_status text, p_status text,
    p_completed_at timestamptz, p_error_code text
)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH ended AS (
        UPDATE public.storage_migration_inventories i
        SET status = p_status, completed_at = p_completed_at, error_code = p_error_code,
            cancel_requested_at = CASE WHEN p_status = 'cancelled'
                THEN COALESCE(i.cancel_requested_at, p_completed_at) ELSE i.cancel_requested_at END
        WHERE i.async_job_id = p_job_id AND i.task_id = p_task_id AND i.status = p_expected_status
          AND i.status IN ('pending', 'running') AND p_status IN ('cancelled', 'failed')
          AND ((p_status = 'cancelled' AND p_error_code IS NULL) OR
               (p_status = 'failed' AND p_error_code ~ '^[a-z][a-z0-9_]{0,79}$'))
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM ended)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.end_migration_inventory(uuid,text,text,text,timestamptz,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.seal_migration_inventory(
    p_job_id uuid, p_task_id text, p_completed_at timestamptz, p_report bytea,
    p_sha256 text, p_complete boolean, p_counts jsonb, p_error_code text
)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH sealed AS (
        UPDATE public.storage_migration_inventories i
        SET status = CASE WHEN p_complete THEN 'completed' ELSE 'failed' END,
            completed_at = p_completed_at, error_code = p_error_code,
            report = p_report, report_sha256 = p_sha256, report_size_bytes = pg_catalog.octet_length(p_report),
            complete = p_complete, counts = p_counts
        WHERE i.async_job_id = p_job_id AND i.task_id = p_task_id AND i.status = 'running'
          AND i.operation_id IS NOT NULL AND i.cancel_requested_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM public.storage_operations o WHERE o.id = i.operation_id)
          AND pg_catalog.octet_length(p_report) BETWEEN 1 AND 33554432
          AND p_sha256 ~ '^[a-f0-9]{64}$' AND pg_catalog.jsonb_typeof(p_counts) = 'object'
          AND ((p_complete AND p_error_code IS NULL) OR (NOT p_complete AND p_error_code = 'inventory_incomplete'))
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM sealed)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.seal_migration_inventory(uuid,text,timestamptz,bytea,text,boolean,jsonb,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_migration_window(p_window_id uuid, p_lock text)
RETURNS TABLE(
    id uuid, parent_copy_id uuid, target_location_id uuid, state character varying,
    revision integer, source_gate_revision integer, source_config_revision integer,
    created_by uuid, created_at timestamptz, updated_at timestamptz,
    source_location_id uuid, source jsonb, target jsonb
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock IS NOT NULL THEN
        CASE p_lock
            WHEN 'share' THEN
                PERFORM 1 FROM public.storage_migration_windows w WHERE w.id = p_window_id FOR SHARE;
            WHEN 'share_nowait' THEN
                PERFORM 1 FROM public.storage_migration_windows w WHERE w.id = p_window_id FOR SHARE NOWAIT;
            WHEN 'update_nowait' THEN
                PERFORM 1 FROM public.storage_migration_windows w WHERE w.id = p_window_id FOR UPDATE NOWAIT;
            ELSE RETURN;
        END CASE;
        IF NOT FOUND THEN RETURN; END IF;
    END IF;
    RETURN QUERY SELECT w.id, w.parent_copy_id, w.target_location_id, w.state, w.revision,
           w.source_gate_revision, w.source_config_revision, w.created_by, w.created_at,
           w.updated_at, w.source_location_id, w.source, w.target
    FROM public.storage_migration_windows w WHERE w.id = p_window_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.read_migration_window(uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_window_history(p_limit integer, p_offset integer)
RETURNS TABLE(
    id uuid, parent_copy_id uuid, target_location_id uuid, state character varying,
    revision integer, source_gate_revision integer, source_config_revision integer,
    created_by uuid, created_at timestamptz, updated_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT w.id, w.parent_copy_id, w.target_location_id, w.state, w.revision, w.source_gate_revision,
           w.source_config_revision, w.created_by, w.created_at, w.updated_at
    FROM public.storage_migration_windows w
    ORDER BY w.created_at DESC, w.id DESC
    LIMIT CASE WHEN p_limit BETWEEN 1 AND 100 AND p_offset BETWEEN 0 AND 1000000 THEN p_limit ELSE 0 END
    OFFSET GREATEST(p_offset, 0)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_window_history(integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_window_history_count()
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.count(w.id) FROM public.storage_migration_windows w
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_window_history_count() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_window_open_operations(p_window_id uuid)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.count(o.id) FROM public.storage_operations o
    JOIN public.storage_migration_windows w ON o.location_id = w.source_location_id
    WHERE w.id = p_window_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_window_open_operations(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_window_exists()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.storage_migration_windows w
        WHERE w.source_location_id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid AND w.state <> 'closed'
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_window_exists() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.open_migration_window(
    p_window_id uuid, p_parent_id uuid, p_actor_id uuid, p_expected_gate_revision integer,
    p_source jsonb, p_target jsonb
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source_id constant uuid := '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7';
    v_target_id uuid;
    v_gate_revision integer;
    v_stamp timestamptz := pg_catalog.statement_timestamp();
BEGIN
    PERFORM 1 FROM public.storage_maintenance_gates g
    WHERE g.location_id = v_source_id AND g.revision = p_expected_gate_revision AND g.state = 'active'
    FOR UPDATE NOWAIT;
    IF NOT FOUND OR yoao_assets_v1.migration_window_exists() THEN RETURN false; END IF;

    SELECT c.target_location_id INTO v_target_id FROM public.storage_migration_copies c
    WHERE c.id = p_parent_id AND c.baseline_sha256 IS NOT NULL AND c.target = p_target
      AND c.target_location_id <> v_source_id FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;

    UPDATE public.storage_maintenance_gates g
    SET state = 'draining', revision = g.revision + 1, changed_at = v_stamp, changed_by = p_actor_id
    WHERE g.location_id = v_source_id RETURNING g.revision INTO v_gate_revision;
    INSERT INTO public.storage_migration_windows (
        id, source_location_id, source, source_config_revision, parent_copy_id, target_location_id,
        target, state, revision, source_gate_revision, created_by, created_at, updated_at
    ) VALUES (
        p_window_id, v_source_id, p_source, (p_source->>'location_revision')::integer, p_parent_id,
        v_target_id, p_target, 'draining', 1, v_gate_revision, p_actor_id, v_stamp, v_stamp
    );
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.open_migration_window(uuid,uuid,uuid,integer,jsonb,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.transition_migration_window(
    p_window_id uuid, p_expected_revision integer, p_action text, p_actor_id uuid
)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_window record;
    v_gate_state text;
    v_gate_revision integer;
    v_next_window_state text;
    v_next_gate_state text;
    v_stamp timestamptz := pg_catalog.statement_timestamp();
    v_has_operations boolean;
BEGIN
    SELECT w.source_location_id, w.source_gate_revision, w.state INTO v_window
    FROM public.storage_migration_windows w
    WHERE w.id = p_window_id AND w.revision = p_expected_revision
      AND w.source_location_id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid
    FOR UPDATE NOWAIT;
    IF NOT FOUND THEN RETURN 'migration_window_changed'; END IF;
    IF p_action IS NULL OR p_action NOT IN ('stop', 'close')
       OR (p_action = 'stop' AND v_window.state <> 'draining') THEN
        RETURN 'migration_window_state_invalid';
    END IF;
    IF v_window.state = 'closed' THEN RETURN NULL; END IF;

    SELECT g.state, g.revision INTO v_gate_state, v_gate_revision
    FROM public.storage_maintenance_gates g
    WHERE g.location_id = v_window.source_location_id AND g.revision = v_window.source_gate_revision
    FOR UPDATE NOWAIT;
    IF NOT FOUND OR (CASE v_window.state
        WHEN 'draining' THEN v_gate_state <> 'draining'
        WHEN 'stopped' THEN v_gate_state <> 'stopped'
        ELSE v_gate_state NOT IN ('draining', 'stopped') END) THEN
        RETURN 'migration_window_gate_changed';
    END IF;
    IF p_action = 'close' AND EXISTS (
        SELECT 1 FROM public.storage_migration_finalizations f
        WHERE f.window_id = p_window_id AND f.state IN ('bound', 'sealing', 'sealed', 'invalidating')
    ) THEN RETURN 'finalization_active'; END IF;
    IF p_action = 'close' AND yoao_assets_v1.original_reclaim_gate_blocked(v_window.source_location_id) THEN
        RETURN 'managed_original_reclaim_unsettled';
    END IF;

    SELECT EXISTS (SELECT 1 FROM public.storage_operations o WHERE o.location_id = v_window.source_location_id)
    INTO v_has_operations;
    IF p_action = 'stop' THEN
        IF v_has_operations THEN RETURN 'migration_window_unsettled'; END IF;
        v_next_window_state := 'stopped';
        v_next_gate_state := 'stopped';
    ELSIF v_has_operations THEN
        v_next_window_state := 'closing';
    ELSE
        v_next_window_state := 'closed';
        v_next_gate_state := 'active';
    END IF;

    IF v_next_gate_state IS NOT NULL THEN
        UPDATE public.storage_maintenance_gates g
        SET state = v_next_gate_state, revision = g.revision + 1, changed_at = v_stamp, changed_by = p_actor_id
        WHERE g.location_id = v_window.source_location_id RETURNING g.revision INTO v_gate_revision;
    END IF;
    UPDATE public.storage_migration_windows w
    SET state = v_next_window_state, revision = w.revision + 1,
        source_gate_revision = v_gate_revision, updated_at = v_stamp
    WHERE w.id = p_window_id;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.transition_migration_window(uuid,integer,text,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.bump_migration_window_finalization_revision(p_window_id uuid, p_expected_revision integer)
RETURNS boolean
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH bumped AS (
        UPDATE public.storage_migration_windows w
        SET revision = w.revision + 1, updated_at = pg_catalog.statement_timestamp()
        WHERE w.id = p_window_id AND w.state = 'stopped' AND w.revision = p_expected_revision
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM bumped)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.bump_migration_window_finalization_revision(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.register_migration_window_source_read(
    p_window_id uuid, p_window_revision integer, p_gate_revision integer, p_owner_kind text,
    p_owner_id uuid, p_task_id text, p_actor_id uuid, p_authority jsonb, p_source jsonb,
    p_operation_id uuid, p_node text
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source_id constant uuid := '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7';
    v_window record;
    v_worker record;
    v_purpose text;
BEGIN
    IF p_node IS NULL OR p_node !~ '^[A-Za-z0-9_.:@-]{1,128}$'
       OR p_owner_kind IS NULL OR p_owner_kind NOT IN ('inventory', 'copy') THEN
        RETURN false;
    END IF;
    SELECT w.parent_copy_id, w.target_location_id, w.target INTO v_window
    FROM public.storage_migration_windows w
    WHERE w.id = p_window_id AND w.source_location_id = v_source_id AND w.state = 'stopped'
      AND w.revision = p_window_revision AND w.source_gate_revision = p_gate_revision
      AND p_source->'location_revision' = pg_catalog.to_jsonb(w.source_config_revision)
    FOR SHARE NOWAIT;
    IF NOT FOUND OR EXISTS (
        SELECT 1 FROM public.storage_migration_finalizations f
        WHERE f.window_id = p_window_id AND f.state = 'invalidating'
    ) THEN RETURN false; END IF;
    PERFORM 1 FROM public.storage_maintenance_gates g
    WHERE g.location_id = v_source_id AND g.state = 'stopped' AND g.revision = p_gate_revision
    FOR SHARE NOWAIT;
    IF NOT FOUND THEN RETURN false; END IF;

    IF p_owner_kind = 'inventory' THEN
        v_purpose := 'storage_migration_inventory';
        SELECT i.async_job_id INTO v_worker
        FROM public.storage_migration_inventories i
        WHERE i.async_job_id = p_owner_id AND i.task_id = p_task_id AND i.status = 'running'
          AND i.requested_by = p_actor_id AND i.authority = p_authority AND i.source = p_source
          AND i.cancel_requested_at IS NULL AND i.operation_id IS NULL
          AND i.source_location_id = v_source_id AND i.maintenance_revision = p_gate_revision
          AND i.window_id = p_window_id AND i.window_revision = p_window_revision
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN false; END IF;
        PERFORM 1 FROM yoao_jobs_v1.read_job_for_share_nowait(v_worker.async_job_id) j
        WHERE j.kind = v_purpose AND j.status = 'running' AND j.user_id = p_actor_id
          AND j.celery_task_id = p_task_id
          AND NOT COALESCE(j.payload->'cancel_requested' = 'true'::jsonb, false)
          AND j.payload - ARRAY['cancel_requested', 'cancel_requested_at']::text[] =
              yoao_assets_v1._migration_inventory_job_payload(p_window_id, p_window_revision);
        IF NOT FOUND THEN RETURN false; END IF;
    ELSE
        v_purpose := 'storage_migration_copy';
        SELECT a.async_job_id, a.copy_id, a.operation INTO v_worker
        FROM public.storage_migration_copy_attempts a
        WHERE a.id = p_owner_id AND a.task_id = p_task_id AND a.status = 'running'
          AND a.requested_by = p_actor_id AND a.authority = p_authority
          AND a.cancel_requested_at IS NULL AND a.source_operation_id IS NULL
          AND a.scope->>'window_id' = p_window_id::text
          AND a.scope->'window_revision' = pg_catalog.to_jsonb(p_window_revision)
          AND a.scope->'maintenance'->(v_source_id::text) = pg_catalog.to_jsonb(p_gate_revision)
        FOR UPDATE NOWAIT;
        IF NOT FOUND THEN RETURN false; END IF;
        PERFORM 1 FROM yoao_jobs_v1.read_job_for_share_nowait(v_worker.async_job_id) j
        WHERE j.kind = v_purpose AND j.status = 'running' AND j.user_id = p_actor_id
          AND j.celery_task_id = p_task_id
          AND NOT COALESCE(j.payload->'cancel_requested' = 'true'::jsonb, false)
          AND j.payload->>'copy_id' = v_worker.copy_id::text
          AND j.payload->>'operation' = v_worker.operation;
        IF NOT FOUND THEN RETURN false; END IF;
        PERFORM 1 FROM public.storage_migration_copies c
        WHERE c.id = v_worker.copy_id AND c.last_attempt_id = p_owner_id AND c.source = p_source
          AND c.window_id = p_window_id AND c.window_revision = p_window_revision
          AND c.parent_copy_id = v_window.parent_copy_id
          AND c.target_location_id = v_window.target_location_id AND c.target = v_window.target
        FOR SHARE NOWAIT;
        IF NOT FOUND THEN RETURN false; END IF;
    END IF;
    INSERT INTO public.storage_operations (id, location_id, revision, purpose, node_id)
    VALUES (p_operation_id, v_source_id, p_gate_revision, v_purpose, p_node);
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.register_migration_window_source_read(uuid,integer,integer,text,uuid,text,uuid,jsonb,jsonb,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_finalization_for_window(p_window uuid)
RETURNS TABLE (
    finalization_id uuid, revision integer, state text, window_revision integer,
    created_at timestamptz, updated_at timestamptz, native_observed_at text,
    sealed_at timestamptz, invalidated_at timestamptz, inventory_job_id uuid,
    copy_id uuid, counts jsonb, error_code text
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT f.id, f.revision, f.state::text, f.window_revision, f.created_at,
           f.updated_at, f.native->>'observed_at', f.sealed_at, f.invalidated_at,
           f.inventory_job_id, f.copy_id, f.evidence->'counts', f.error_code::text
    FROM public.storage_migration_finalizations AS f
    WHERE f.window_id = p_window
    ORDER BY f.created_at DESC, f.id DESC
    LIMIT 1
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_finalization_for_window(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_finalization_blocks_window(p_window uuid, p_closing boolean)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.storage_migration_finalizations AS f
        WHERE f.window_id = p_window
          AND (f.state = 'invalidating' OR (p_closing AND f.state IN ('bound','sealing','sealed')))
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_finalization_blocks_window(uuid,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_finalization_blocks_source_resume(p_location uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.storage_migration_finalizations AS f
        WHERE f.source_location_id = p_location
          AND f.state IN ('bound','sealing','sealed','invalidating')
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_finalization_blocks_source_resume(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_read_publication_holds_copy(p_copy uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.storage_migration_read_publications AS p
        WHERE p.copy_id = p_copy AND p.state IN ('active','withdrawing')
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_read_publication_holds_copy(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_is_frozen(p_copy uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT yoao_assets_v1.migration_read_publication_holds_copy(p_copy) OR EXISTS (
        SELECT FROM public.storage_migration_finalizations AS f
        WHERE f.copy_id = p_copy AND f.state IN ('sealing','sealed')
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_is_frozen(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_cutover_blocks_target_resume(p_location uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.storage_migration_cutovers AS c
        WHERE c.target_location_id = p_location
          AND c.state IN ('rollback_draining','rollback_bound','rollback_restoring')
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_cutover_blocks_target_resume(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._migration_checkpoint_changed(p_copy uuid)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.storage_migration_finalizations AS f
    SET state = 'invalidating', revision = f.revision + 1,
        updated_at = pg_catalog.statement_timestamp(), error_code = 'finalization_checkpoint_changed'
    WHERE f.copy_id = p_copy AND f.state IN ('sealing','sealed');

    UPDATE public.storage_migration_read_publications AS p
    SET state = 'withdrawing', revision = p.revision + 1,
        updated_at = pg_catalog.statement_timestamp(), withdrawing_at = pg_catalog.statement_timestamp()
    WHERE p.copy_id = p_copy AND p.state = 'active';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1._migration_checkpoint_changed(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_read_route_recheck(
    p_publication uuid, p_revision integer, p_object_id text, p_lock boolean
)
RETURNS TABLE (
    publication_id uuid, publication_revision integer, source_location_id uuid,
    source_location_revision numeric, target_location_id uuid, target_identity jsonb,
    source_object_id text, source_endpoint_url text, source_bucket text, source_object_key text,
    source_version_id text, sha256 text, size_bytes bigint, source_etag text, is_latest boolean,
    target_object_key text, target_version_id text, target_etag text, target_content_sha256 text,
    creation_nonce text, creation_plan_uuid text
)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        PERFORM p.id FROM public.storage_migration_read_publications AS p
        WHERE p.id = p_publication
        FOR SHARE NOWAIT;
    END IF;

    RETURN QUERY
    SELECT p.id, p.revision, p.source_location_id,
           CASE WHEN pg_catalog.jsonb_typeof(p.source->'location_revision') = 'number'
                THEN (p.source->'location_revision')::numeric END,
           p.target_location_id,
           CASE WHEN p.target = pg_catalog.jsonb_build_object(
                       'location_id', p.target->>'location_id',
                       'connection_id', p.target->>'connection_id',
                       'endpoint_url', p.target->>'endpoint_url',
                       'bucket', p.target->>'bucket',
                       'base_prefix', p.target->>'base_prefix',
                       'key_prefix', p.target->>'key_prefix'
                     ) THEN p.target END,
           r.source_object_id::text, r.endpoint_url, r.bucket, r.object_key, r.source_version_id,
           r.sha256::text, r.size_bytes, r.source_etag, r.is_latest, r.target_key, r.target_version_id,
           r.target_etag, r.target_content_sha256::text, r.nonce, r.creation_plan_uuid
    FROM public.storage_migration_read_publications AS p
    JOIN public.storage_migration_read_routes AS r ON r.publication_id = p.id
    WHERE p.id = p_publication AND p.revision = p_revision AND p.state = 'active'
      AND r.source_object_id = p_object_id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_read_route_recheck(uuid,integer,text,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_read_route_lookup(
    p_source uuid, p_endpoint text, p_bucket text, p_key text, p_sha256 text, p_size bigint,
    p_match_version boolean, p_version text, p_etag text, p_lock boolean
)
RETURNS TABLE (
    publication_id uuid, publication_revision integer, source_location_id uuid,
    source_location_revision numeric, target_location_id uuid, target_identity jsonb,
    source_object_id text, source_endpoint_url text, source_bucket text, source_object_key text,
    source_version_id text, sha256 text, size_bytes bigint, source_etag text, is_latest boolean,
    target_object_key text, target_version_id text, target_etag text, target_content_sha256 text,
    creation_nonce text, creation_plan_uuid text
)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_id uuid;
    v_revision integer;
    v_object_id text;
BEGIN
    IF p_lock THEN
        SELECT p.id, p.revision INTO v_id, v_revision
        FROM public.storage_migration_read_publications AS p
        WHERE p.source_location_id = p_source AND p.state = 'active'
        FOR SHARE NOWAIT;
    ELSE
        SELECT p.id, p.revision INTO v_id, v_revision
        FROM public.storage_migration_read_publications AS p
        WHERE p.source_location_id = p_source AND p.state = 'active';
    END IF;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT r.source_object_id INTO v_object_id
    FROM public.storage_migration_read_routes AS r
    WHERE r.publication_id = v_id AND r.endpoint_url = p_endpoint AND r.bucket = p_bucket
      AND r.object_key = p_key AND r.sha256 = p_sha256
      AND (p_size IS NULL OR r.size_bytes = p_size)
      AND (NOT p_match_version OR r.source_version_id IS NOT DISTINCT FROM p_version)
      AND (p_etag IS NULL OR r.source_etag = p_etag)
    ORDER BY r.is_latest DESC, r.source_object_id
    LIMIT 1;
    IF FOUND THEN
        RETURN QUERY SELECT r.*
        FROM yoao_assets_v1.migration_read_route_recheck(v_id, v_revision, v_object_id, false) AS r;
    END IF;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_read_route_lookup(uuid,text,text,text,text,bigint,boolean,text,text,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_read_prefix_inventory(
    p_publication uuid, p_revision integer, p_object_id text, p_prefix text
)
RETURNS TABLE (error_code text, object_key text, sha256 text, size_bytes bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_copy uuid;
    v_source jsonb;
    v_report_sha256 text;
    v_handling jsonb;
    v_endpoint text;
    v_bucket text;
    v_object_key text;
BEGIN
    SELECT p.copy_id, p.source, p.evidence->>'source_report_sha256', r.endpoint_url, r.bucket, r.object_key
    INTO v_copy, v_source, v_report_sha256, v_endpoint, v_bucket, v_object_key
    FROM public.storage_migration_read_publications AS p
    JOIN public.storage_migration_read_routes AS r ON r.publication_id = p.id
    WHERE p.id = p_publication AND p.revision = p_revision AND p.state = 'active'
      AND r.source_object_id = p_object_id;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'read_publication_changed'::text, NULL::text, NULL::text, NULL::bigint;
        RETURN;
    END IF;
    IF p_prefix IS NULL OR pg_catalog.octet_length(p_prefix) NOT BETWEEN 1 AND 1024
       OR NOT pg_catalog.starts_with(v_object_key, p_prefix) THEN
        RETURN QUERY SELECT 'read_publication_prefix_invalid'::text, NULL::text, NULL::text, NULL::bigint;
        RETURN;
    END IF;

    SELECT c.handling INTO v_handling
    FROM public.storage_migration_copies AS c
    WHERE c.id = v_copy AND c.source = v_source AND c.source_report_sha256 = v_report_sha256;
    IF NOT FOUND OR pg_catalog.jsonb_typeof(v_source->'purpose_buckets') IS DISTINCT FROM 'object' THEN
        RETURN QUERY SELECT 'read_publication_prefix_incomplete'::text, NULL::text, NULL::text, NULL::bigint;
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT FROM pg_catalog.jsonb_each(v_source->'purpose_buckets') AS b
        WHERE b.value = pg_catalog.to_jsonb(v_bucket)
    ) OR EXISTS (
        SELECT FROM pg_catalog.jsonb_each(v_source->'purpose_buckets') AS b
        WHERE b.value = pg_catalog.to_jsonb(v_bucket)
          AND v_handling->b.key IS DISTINCT FROM '"migrate"'::jsonb
    ) THEN
        RETURN QUERY SELECT 'read_publication_prefix_incomplete'::text, NULL::text, NULL::text, NULL::bigint;
        RETURN;
    END IF;

    RETURN QUERY
    SELECT NULL::text, r.object_key, r.sha256::text, r.size_bytes
    FROM public.storage_migration_read_routes AS r
    WHERE r.publication_id = p_publication AND r.endpoint_url = v_endpoint AND r.bucket = v_bucket
      AND r.is_latest AND pg_catalog.starts_with(r.object_key, p_prefix)
    ORDER BY r.object_key, r.source_object_id
    LIMIT 100001;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_read_prefix_inventory(uuid,integer,text,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_operator_copy_window_unsettled(p_window uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH scope AS (
        SELECT w.id, w.parent_copy_id, w.source_location_id
        FROM public.storage_migration_windows AS w
        WHERE w.id = p_window
    ), attempts AS (
        SELECT a.status, a.source_operation_id, a.target_operation_id
        FROM scope AS s
        JOIN public.storage_migration_copies AS c ON c.window_id = s.id OR c.id = s.parent_copy_id
        JOIN public.storage_migration_copy_attempts AS a ON a.copy_id = c.id
    )
    SELECT EXISTS (SELECT FROM attempts WHERE status IN ('pending','running'))
        OR EXISTS (
            SELECT FROM public.storage_operations AS o
            WHERE o.location_id = (SELECT s.source_location_id FROM scope AS s)
               OR o.id IN (SELECT a.source_operation_id FROM attempts AS a)
               OR o.id IN (SELECT a.target_operation_id FROM attempts AS a)
        )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_operator_copy_window_unsettled(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_migration_copy_planning()
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.pg_advisory_xact_lock(8592650718001::bigint)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_migration_copy_planning() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_plan(p_copy uuid, p_lock text)
RETURNS TABLE (
    id uuid, window_id uuid, window_revision integer, parent_copy_id uuid,
    generation integer, parent_evidence_sha256 varchar, input_fingerprint varchar,
    source_inventory_job_id uuid, source_report_sha256 varchar, source_complete boolean,
    source jsonb, target_location_id uuid, target jsonb, handling jsonb, limits jsonb,
    catalog_sha256 varchar, baseline_sha256 varchar, summary jsonb, last_attempt_id uuid,
    delta_sequence integer, delta_bytes integer, created_at timestamptz, updated_at timestamptz
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    CASE p_lock
        WHEN 'read' THEN NULL;
        WHEN 'update' THEN
            PERFORM 1 FROM public.storage_migration_copies AS c WHERE c.id = p_copy FOR UPDATE;
            IF NOT FOUND THEN RETURN; END IF;
        WHEN 'update_nowait' THEN
            PERFORM 1 FROM public.storage_migration_copies AS c WHERE c.id = p_copy FOR UPDATE NOWAIT;
            IF NOT FOUND THEN RETURN; END IF;
        WHEN 'share_nowait' THEN
            PERFORM 1 FROM public.storage_migration_copies AS c WHERE c.id = p_copy FOR SHARE NOWAIT;
            IF NOT FOUND THEN RETURN; END IF;
        ELSE RETURN;
    END CASE;
    RETURN QUERY
        SELECT c.id, c.window_id, c.window_revision, c.parent_copy_id,
               c.generation, c.parent_evidence_sha256, c.input_fingerprint,
               c.source_inventory_job_id, c.source_report_sha256, c.source_complete,
               c.source, c.target_location_id, c.target, c.handling, c.limits,
               c.catalog_sha256, c.baseline_sha256, c.summary, c.last_attempt_id,
               c.delta_sequence, c.delta_bytes, c.created_at, c.updated_at
        FROM public.storage_migration_copies AS c WHERE c.id = p_copy;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_plan(uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_migration_copy_by_input(p_fingerprint text)
RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT c.id FROM public.storage_migration_copies AS c
    WHERE c.input_fingerprint = p_fingerprint FOR UPDATE
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_migration_copy_by_input(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_private_inputs(p_copy uuid)
RETURNS TABLE (source_report bytea, catalog bytea, baseline bytea)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT c.source_report, c.catalog, c.baseline
    FROM public.storage_migration_copies AS c WHERE c.id = p_copy
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_private_inputs(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_attempt(p_attempt uuid, p_job uuid, p_lock text)
RETURNS TABLE (
    id uuid, copy_id uuid, async_job_id uuid, task_id varchar, requested_by uuid,
    authority jsonb, scope jsonb, operation varchar, retry_failed boolean, retry_object_ids jsonb,
    status varchar, source_operation_id uuid, target_operation_id uuid, process jsonb,
    created_at timestamptz, started_at timestamptz, completed_at timestamptz,
    cancel_requested_at timestamptz, error_code varchar, result jsonb
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_attempt uuid;
BEGIN
    IF (p_attempt IS NULL) = (p_job IS NULL) OR p_lock IS NULL THEN RETURN; END IF;
    IF p_attempt IS NULL THEN
        SELECT a.id INTO v_attempt FROM public.storage_migration_copy_attempts AS a
        WHERE a.async_job_id = p_job;
    ELSE
        v_attempt := p_attempt;
    END IF;
    CASE p_lock
        WHEN 'read' THEN NULL;
        WHEN 'update' THEN
            PERFORM 1 FROM public.storage_migration_copy_attempts AS a WHERE a.id = v_attempt FOR UPDATE;
            IF NOT FOUND THEN RETURN; END IF;
        WHEN 'update_nowait' THEN
            PERFORM 1 FROM public.storage_migration_copy_attempts AS a WHERE a.id = v_attempt FOR UPDATE NOWAIT;
            IF NOT FOUND THEN RETURN; END IF;
        ELSE RETURN;
    END CASE;
    RETURN QUERY
        SELECT a.id, a.copy_id, a.async_job_id, a.task_id, a.requested_by,
               a.authority, a.scope, a.operation, a.retry_failed, a.retry_object_ids,
               a.status, a.source_operation_id, a.target_operation_id, a.process,
               a.created_at, a.started_at, a.completed_at, a.cancel_requested_at, a.error_code, a.result
        FROM public.storage_migration_copy_attempts AS a
        WHERE a.id = v_attempt AND (p_job IS NULL OR a.async_job_id = p_job);
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_attempt(uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_successor(p_copy uuid)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT c.id FROM public.storage_migration_copies AS c WHERE c.parent_copy_id = p_copy
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_successor(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_history(p_limit integer, p_offset integer)
RETURNS TABLE (
    total bigint, id uuid, window_id uuid, window_revision integer, parent_copy_id uuid,
    generation integer, parent_evidence_sha256 varchar, input_fingerprint varchar,
    source_inventory_job_id uuid, source_report_sha256 varchar, source_complete boolean,
    source jsonb, target_location_id uuid, target jsonb, handling jsonb, limits jsonb,
    catalog_sha256 varchar, baseline_sha256 varchar, summary jsonb, last_attempt_id uuid,
    delta_sequence integer, delta_bytes integer, created_at timestamptz, updated_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT t.total, f.id, f.window_id, f.window_revision, f.parent_copy_id,
           f.generation, f.parent_evidence_sha256, f.input_fingerprint,
           f.source_inventory_job_id, f.source_report_sha256, f.source_complete,
           f.source, f.target_location_id, f.target, f.handling, f.limits,
           f.catalog_sha256, f.baseline_sha256, f.summary, f.last_attempt_id,
           f.delta_sequence, f.delta_bytes, f.created_at, f.updated_at
    FROM (SELECT count(*) AS total FROM public.storage_migration_copies) AS t
    LEFT JOIN (
        SELECT c.id FROM public.storage_migration_copies AS c
        ORDER BY c.created_at DESC, c.id DESC
        LIMIT LEAST(GREATEST(p_limit, 1), 100) OFFSET LEAST(GREATEST(p_offset, 0), 1000000)
    ) AS page ON true
    LEFT JOIN LATERAL yoao_assets_v1.migration_copy_plan(page.id, 'read') AS f ON true
    ORDER BY f.created_at DESC, f.id DESC
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_history(integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_target_candidates(p_limit integer, p_offset integer)
RETURNS TABLE (total bigint, location_id uuid, name varchar, config_revision integer)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RETURN QUERY
        WITH targets AS MATERIALIZED (
            SELECT l.id, c.name, l.config_revision
            FROM yoao_assets_storage_v1.location_metadata AS l
            JOIN yoao_assets_storage_v1.connector_origins AS c ON c.id = l.connection_id
            WHERE l.enabled AND l.kind = 's3' AND c.kind = 's3'
              AND c.scope = 'global' AND c.project_id IS NULL AND c.secret_set
        )
        SELECT t.total, page.id, page.name, page.config_revision
        FROM (SELECT count(*) AS total FROM targets) AS t
        LEFT JOIN (
            SELECT t.id, t.name, t.config_revision FROM targets AS t ORDER BY t.name, t.id
            LIMIT LEAST(GREATEST(p_limit, 1), 100) OFFSET LEAST(GREATEST(p_offset, 0), 1000000)
        ) AS page ON true
        ORDER BY page.name, page.id;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_target_candidates(integer,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.freeze_migration_copy_plan(
    p_copy uuid, p_inventory uuid, p_report_sha text, p_fingerprint text, p_source jsonb,
    p_target_id uuid, p_target jsonb, p_handling jsonb, p_limits jsonb
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    INSERT INTO public.storage_migration_copies (
        id, input_fingerprint, source_inventory_job_id, source_report, source_report_sha256,
        source_complete, source, target_location_id, target, handling, limits
    )
    SELECT p_copy, p_fingerprint, i.async_job_id, i.report, i.report_sha256, i.complete,
           p_source, p_target_id, p_target, p_handling, p_limits
    FROM public.storage_migration_inventories AS i
    WHERE i.async_job_id = p_inventory AND i.window_id IS NULL
      AND i.report IS NOT NULL AND i.report_sha256 = p_report_sha
      AND i.source->'endpoint' = p_source->'endpoint'
      AND i.source->'purpose_buckets' = p_source->'purpose_buckets'
      AND p_target_id <> '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid
      AND p_target->>'location_id' = p_target_id::text
      AND p_fingerprint ~ '^[0-9a-f]{64}$'
      AND jsonb_typeof(p_handling) = 'object' AND jsonb_typeof(p_limits) = 'object';
    RETURN FOUND;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.freeze_migration_copy_plan(uuid,uuid,text,text,jsonb,uuid,jsonb,jsonb,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_parent_settlement(p_copy uuid)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM 1 FROM public.storage_migration_copies AS c WHERE c.id = p_copy FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    PERFORM 1 FROM public.storage_migration_copy_attempts AS a
    WHERE a.copy_id = p_copy ORDER BY a.id FOR UPDATE;
    IF EXISTS (
        SELECT 1 FROM public.storage_migration_copy_attempts AS a
        WHERE a.copy_id = p_copy AND a.status IN ('pending', 'running')
    ) THEN
        RETURN 'active';
    END IF;
    PERFORM 1 FROM public.storage_operations AS o
    WHERE o.id IN (
        SELECT a.source_operation_id FROM public.storage_migration_copy_attempts AS a
        WHERE a.copy_id = p_copy AND a.source_operation_id IS NOT NULL
        UNION ALL
        SELECT a.target_operation_id FROM public.storage_migration_copy_attempts AS a
        WHERE a.copy_id = p_copy AND a.target_operation_id IS NOT NULL
    ) ORDER BY o.id LIMIT 1 FOR UPDATE;
    IF FOUND THEN RETURN 'operations'; END IF;
    RETURN 'settled';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_parent_settlement(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.reserve_migration_copy_successor(
    p_copy uuid, p_parent uuid, p_inventory uuid, p_report_sha text,
    p_parent_evidence text, p_fingerprint text, p_at timestamptz
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF yoao_assets_v1.migration_copy_parent_settlement(p_parent) IS DISTINCT FROM 'settled'
       OR yoao_assets_v1.migration_copy_successor(p_parent) IS NOT NULL
       OR yoao_assets_v1.migration_copy_is_frozen(p_parent) THEN
        RETURN false;
    END IF;
    INSERT INTO public.storage_migration_copies (
        id, parent_copy_id, generation, parent_evidence_sha256, window_id, window_revision,
        input_fingerprint, source_inventory_job_id, source_report, source_report_sha256,
        source_complete, source, target_location_id, target, handling, limits
    )
    SELECT p_copy, c.id, c.generation + 1, p_parent_evidence, i.window_id, i.window_revision,
           p_fingerprint, i.async_job_id, i.report, i.report_sha256, i.complete,
           CASE WHEN i.window_id IS NULL THEN c.source ELSE i.source END,
           c.target_location_id, c.target, c.handling, c.limits
    FROM public.storage_migration_copies AS c
    JOIN public.storage_migration_inventories AS i ON i.async_job_id = p_inventory
    WHERE c.id = p_parent AND c.baseline_sha256 IS NOT NULL
      AND i.report IS NOT NULL AND i.report_sha256 = p_report_sha
      AND i.report_sha256 <> c.source_report_sha256
      AND i.source->'endpoint' = c.source->'endpoint'
      AND i.source->'purpose_buckets' = c.source->'purpose_buckets'
      AND p_fingerprint ~ '^[0-9a-f]{64}$';
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.storage_migration_copies SET updated_at = p_at WHERE id = p_parent;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.reserve_migration_copy_successor(uuid,uuid,uuid,text,text,text,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_failed_selection(p_copy uuid, p_ids text[])
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p_ids IS NOT NULL AND cardinality(p_ids) BETWEEN 1 AND 1000
       AND (SELECT count(*) FROM public.storage_migration_copy_deltas AS d
            WHERE d.copy_id = p_copy AND d.object_id = ANY(p_ids)
              AND d.data->>'state' = 'failed') = cardinality(p_ids)
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_failed_selection(uuid,text[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.queue_migration_copy_attempt(
    p_copy uuid, p_previous uuid, p_attempt uuid, p_job uuid, p_task text, p_requested_by uuid,
    p_api_key uuid, p_proof text, p_scope jsonb, p_operation text,
    p_retry_failed boolean, p_retry_ids text[], p_recovering boolean, p_at timestamptz
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM 1 FROM public.storage_migration_copies AS c
    WHERE c.id = p_copy AND c.last_attempt_id IS NOT DISTINCT FROM p_previous
      AND ((p_operation = 'prepare') = (c.baseline_sha256 IS NULL))
      AND p_scope->>'target_location_id' = c.target_location_id::text
    FOR UPDATE;
    IF NOT FOUND OR yoao_assets_v1.migration_copy_successor(p_copy) IS NOT NULL
       OR yoao_assets_v1.migration_copy_is_frozen(p_copy)
       OR p_operation NOT IN ('prepare', 'run', 'verify')
       OR (p_retry_failed AND p_operation <> 'run') THEN
        RETURN false;
    END IF;
    IF p_recovering THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.storage_migration_copy_attempts AS a
            WHERE a.id = p_previous AND a.copy_id = p_copy AND a.process IS NOT NULL
              AND a.status NOT IN ('pending', 'running') AND a.operation = p_operation
              AND a.retry_failed = p_retry_failed
              AND NULLIF(a.retry_object_ids, 'null'::jsonb) IS NOT DISTINCT FROM to_jsonb(p_retry_ids)
        ) THEN
            RETURN false;
        END IF;
    ELSIF p_retry_ids IS NOT NULL AND (
        NOT p_retry_failed OR NOT yoao_assets_v1.migration_copy_failed_selection(p_copy, p_retry_ids)
    ) THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.storage_migration_copy_attempts AS a
        WHERE a.copy_id = p_copy AND a.status IN ('pending', 'running')
    ) OR EXISTS (
        SELECT 1 FROM public.storage_migration_copy_attempts AS a
        JOIN public.storage_operations AS o ON o.id IN (a.source_operation_id, a.target_operation_id)
        WHERE a.id = p_previous AND a.copy_id = p_copy
    ) THEN
        RETURN false;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM yoao_jobs_v1.read_job(p_job) AS j
        WHERE j.kind = 'storage_migration_copy' AND j.user_id = p_requested_by
          AND j.status = 'pending' AND j.celery_task_id = p_task
          AND j.payload->>'copy_id' = p_copy::text AND j.payload->>'operation' = p_operation
    ) THEN
        RETURN false;
    END IF;
    INSERT INTO public.storage_migration_copy_attempts (
        id, copy_id, async_job_id, task_id, requested_by, authority, scope,
        operation, retry_failed, retry_object_ids, status
    ) VALUES (
        p_attempt, p_copy, p_job, p_task, p_requested_by,
        jsonb_build_object('api_key_id', p_api_key, 'key_proof', p_proof), p_scope,
        p_operation, p_retry_failed, to_jsonb(p_retry_ids), 'pending'
    );
    UPDATE public.storage_migration_copies
    SET last_attempt_id = p_attempt, updated_at = p_at WHERE id = p_copy;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.queue_migration_copy_attempt(uuid,uuid,uuid,uuid,text,uuid,uuid,text,jsonb,text,boolean,text[],boolean,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.claim_migration_copy_attempt(
    p_copy uuid, p_attempt uuid, p_job uuid, p_task text, p_process jsonb, p_at timestamptz
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM 1 FROM public.storage_migration_copies AS c
    WHERE c.id = p_copy AND c.last_attempt_id = p_attempt FOR UPDATE;
    IF NOT FOUND OR yoao_assets_v1.migration_copy_successor(p_copy) IS NOT NULL
       OR yoao_assets_v1.migration_copy_is_frozen(p_copy) THEN
        RETURN false;
    END IF;
    UPDATE public.storage_migration_copy_attempts AS a
    SET status = 'running', started_at = p_at, process = p_process
    WHERE a.id = p_attempt AND a.copy_id = p_copy AND a.async_job_id = p_job
      AND a.task_id = p_task AND a.status = 'pending' AND a.cancel_requested_at IS NULL;
    RETURN FOUND;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.claim_migration_copy_attempt(uuid,uuid,uuid,text,jsonb,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.attach_migration_copy_permits(
    p_copy uuid, p_attempt uuid, p_job uuid, p_task text, p_source uuid, p_target uuid
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_target uuid;
    v_source constant uuid := '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid;
BEGIN
    SELECT c.target_location_id INTO v_target FROM public.storage_migration_copies AS c
    WHERE c.id = p_copy AND c.last_attempt_id = p_attempt FOR UPDATE;
    IF NOT FOUND OR v_target = v_source OR p_source = p_target
       OR yoao_assets_v1.migration_copy_successor(p_copy) IS NOT NULL
       OR yoao_assets_v1.migration_copy_is_frozen(p_copy) THEN
        RETURN false;
    END IF;
    UPDATE public.storage_migration_copy_attempts AS a
    SET source_operation_id = p_source, target_operation_id = p_target
    WHERE a.id = p_attempt AND a.copy_id = p_copy AND a.async_job_id = p_job
      AND a.task_id = p_task AND a.status = 'running' AND a.cancel_requested_at IS NULL
      AND a.source_operation_id IS NULL AND a.target_operation_id IS NULL
      AND EXISTS (
          SELECT 1 FROM public.storage_operations AS o
          WHERE o.id = p_source AND o.location_id = v_source AND o.purpose = 'storage_migration_copy'
            AND o.revision::text = a.scope->'maintenance'->>v_source::text
            AND (NULLIF(a.process, 'null'::jsonb) IS NULL OR o.node_id = 'migration:' || (a.process->>'pid'))
      )
      AND EXISTS (
          SELECT 1 FROM public.storage_operations AS o
          WHERE o.id = p_target AND o.location_id = v_target AND o.purpose = 'storage_migration_copy'
            AND o.revision::text = a.scope->'maintenance'->>v_target::text
            AND (NULLIF(a.process, 'null'::jsonb) IS NULL OR o.node_id = 'migration:' || (a.process->>'pid'))
      );
    RETURN FOUND;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.attach_migration_copy_permits(uuid,uuid,uuid,text,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_permits_open(
    p_copy uuid, p_attempt uuid, p_task text, p_lock boolean
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source uuid;
    v_target uuid;
BEGIN
    SELECT a.source_operation_id, a.target_operation_id INTO v_source, v_target
    FROM public.storage_migration_copy_attempts AS a
    JOIN public.storage_migration_copies AS c ON c.id = a.copy_id AND c.last_attempt_id = a.id
    WHERE a.id = p_attempt AND a.copy_id = p_copy AND a.task_id = p_task;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF p_lock THEN
        PERFORM 1 FROM public.storage_operations AS o
        WHERE o.id IN (v_source, v_target) ORDER BY o.id FOR UPDATE;
    ELSE
        PERFORM 1 FROM public.storage_operations AS o WHERE o.id IN (v_source, v_target) LIMIT 1;
    END IF;
    RETURN FOUND;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_permits_open(uuid,uuid,text,boolean) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.settle_migration_copy_process(
    p_copy uuid, p_attempt uuid, p_task text, p_expected text, p_process jsonb, p_at timestamptz
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_source_id uuid;
    v_target_id uuid;
    v_target_location uuid;
    v_source_location constant uuid := '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid;
    v_operation record;
BEGIN
    SELECT c.target_location_id INTO v_target_location FROM public.storage_migration_copies AS c
    WHERE c.id = p_copy AND c.last_attempt_id = p_attempt FOR UPDATE;
    IF NOT FOUND OR p_process->>'pid' IS NULL OR (p_process->>'pid') !~ '^[1-9][0-9]*$'
       OR p_expected IS NULL OR p_expected = 'pending' THEN
        RETURN false;
    END IF;
    SELECT a.source_operation_id, a.target_operation_id INTO v_source_id, v_target_id
    FROM public.storage_migration_copy_attempts AS a
    WHERE a.id = p_attempt AND a.copy_id = p_copy AND a.task_id = p_task
      AND a.status = p_expected AND a.process = p_process FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    FOR v_operation IN
        SELECT o.id, o.location_id, o.purpose, o.node_id FROM public.storage_operations AS o
        WHERE o.id IN (v_source_id, v_target_id) ORDER BY o.id FOR UPDATE
    LOOP
        IF v_operation.location_id IS DISTINCT FROM
               CASE WHEN v_operation.id = v_source_id THEN v_source_location ELSE v_target_location END
           OR v_operation.purpose <> 'storage_migration_copy'
           OR v_operation.node_id IS DISTINCT FROM ('migration:' || (p_process->>'pid')) THEN
            RETURN false;
        END IF;
    END LOOP;
    DELETE FROM public.storage_operations AS o
    WHERE o.id IN (v_source_id, v_target_id)
      AND o.purpose = 'storage_migration_copy' AND o.node_id = 'migration:' || (p_process->>'pid')
      AND ((o.id = v_source_id AND o.location_id = v_source_location)
           OR (o.id = v_target_id AND o.location_id = v_target_location));
    IF p_expected = 'running' THEN
        UPDATE public.storage_migration_copy_attempts AS a
        SET status = 'failed', completed_at = p_at, error_code = 'copy_process_ended'
        WHERE a.id = p_attempt;
    END IF;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.settle_migration_copy_process(uuid,uuid,text,text,jsonb,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.request_migration_copy_cancel(
    p_copy uuid, p_attempt uuid, p_job uuid, p_task text, p_at timestamptz
)
RETURNS timestamptz LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_at timestamptz;
BEGIN
    PERFORM 1 FROM public.storage_migration_copies AS c
    WHERE c.id = p_copy AND c.last_attempt_id = p_attempt FOR UPDATE;
    IF NOT FOUND OR yoao_assets_v1.migration_copy_successor(p_copy) IS NOT NULL
       OR yoao_assets_v1.migration_copy_is_frozen(p_copy) THEN
        RETURN NULL;
    END IF;
    UPDATE public.storage_migration_copy_attempts AS a
    SET cancel_requested_at = COALESCE(a.cancel_requested_at, p_at)
    WHERE a.id = p_attempt AND a.copy_id = p_copy AND a.async_job_id = p_job
      AND a.task_id = p_task AND a.status = 'running'
    RETURNING a.cancel_requested_at INTO v_at;
    RETURN v_at;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.request_migration_copy_cancel(uuid,uuid,uuid,text,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.finish_migration_copy_attempt(
    p_copy uuid, p_attempt uuid, p_job uuid, p_task text, p_expected text, p_status text,
    p_error text, p_result jsonb, p_at timestamptz
)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_prepared boolean;
BEGIN
    SELECT c.baseline_sha256 IS NOT NULL INTO v_prepared FROM public.storage_migration_copies AS c
    WHERE c.id = p_copy AND c.last_attempt_id = p_attempt FOR UPDATE;
    IF NOT FOUND OR p_expected NOT IN ('pending', 'running')
       OR p_status NOT IN ('completed', 'failed', 'cancelled')
       OR (p_status = 'completed' AND (p_expected <> 'running' OR NOT v_prepared))
       OR (p_result IS NOT NULL AND jsonb_typeof(p_result) <> 'object') THEN
        RETURN false;
    END IF;
    UPDATE public.storage_migration_copy_attempts AS a
    SET status = p_status, completed_at = p_at, error_code = p_error,
        cancel_requested_at = CASE WHEN p_status = 'cancelled'
            THEN COALESCE(a.cancel_requested_at, p_at) ELSE a.cancel_requested_at END,
        result = CASE WHEN p_result IS NULL THEN a.result ELSE jsonb_build_object(
            'io', p_result->'io', 'attempt_subset_complete', p_result->'attempt_subset_complete',
            'selected_count', p_result->'selected_count', 'selected_verified_count', p_result->'selected_verified_count'
        ) END
    WHERE a.id = p_attempt AND a.copy_id = p_copy AND a.async_job_id = p_job
      AND a.task_id = p_task AND a.status = p_expected
      AND (p_status <> 'completed' OR (a.cancel_requested_at IS NULL
           AND a.source_operation_id IS NOT NULL AND a.target_operation_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM public.storage_operations AS o
                           WHERE o.id IN (a.source_operation_id, a.target_operation_id))));
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.storage_migration_copies SET updated_at = p_at WHERE id = p_copy;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.finish_migration_copy_attempt(uuid,uuid,uuid,text,text,text,text,jsonb,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.publish_migration_copy_plan(
    p_copy uuid, p_attempt uuid, p_job uuid, p_task text, p_baseline bytea, p_catalog bytea, p_summary jsonb
)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_sha text;
BEGIN
    IF octet_length(p_baseline) NOT BETWEEN 1 AND 268435456
       OR octet_length(p_catalog) NOT BETWEEN 1 AND 67108864
       OR jsonb_typeof(p_summary) IS DISTINCT FROM 'object'
       OR NOT (p_summary ?& ARRAY['counts', 'decisions', 'source_summary', 'catalog_summary', 'copy_subset_complete']) THEN
        RETURN NULL;
    END IF;
    PERFORM 1 FROM public.storage_migration_copies AS c
    WHERE c.id = p_copy AND c.last_attempt_id = p_attempt AND c.baseline_sha256 IS NULL
      AND c.catalog_sha256 IS NULL FOR UPDATE;
    IF NOT FOUND OR yoao_assets_v1.migration_copy_successor(p_copy) IS NOT NULL
       OR yoao_assets_v1.migration_copy_is_frozen(p_copy) THEN
        RETURN NULL;
    END IF;
    PERFORM 1 FROM public.storage_migration_copy_attempts AS a
    WHERE a.id = p_attempt AND a.copy_id = p_copy AND a.async_job_id = p_job
      AND a.task_id = p_task AND a.status = 'running' AND a.operation = 'prepare'
      AND a.cancel_requested_at IS NULL AND a.source_operation_id IS NOT NULL
      AND a.target_operation_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM public.storage_operations AS o
                      WHERE o.id IN (a.source_operation_id, a.target_operation_id))
    FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    v_sha := encode(sha256(p_baseline), 'hex');
    UPDATE public.storage_migration_copies AS c
    SET baseline = p_baseline, baseline_sha256 = v_sha,
        catalog = p_catalog, catalog_sha256 = encode(sha256(p_catalog), 'hex'),
        summary = jsonb_build_object(
            'counts', p_summary->'counts', 'decisions', p_summary->'decisions',
            'source_summary', p_summary->'source_summary', 'catalog_summary', p_summary->'catalog_summary',
            'copy_subset_complete', p_summary->'copy_subset_complete'
        ) || CASE WHEN p_summary ? 'generation_summary'
                THEN jsonb_build_object('generation_summary', p_summary->'generation_summary')
                ELSE '{}'::jsonb END
    WHERE c.id = p_copy;
    RETURN v_sha;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.publish_migration_copy_plan(uuid,uuid,uuid,text,bytea,bytea,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.record_migration_copy_delta(
    p_copy uuid, p_attempt uuid, p_job uuid, p_task text, p_packed text, p_checksum text, p_at timestamptz
)
RETURNS TABLE (error_code text, changed boolean, sequence integer, completed integer)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_size integer;
    v_total_bytes integer;
    v_sequence integer;
    v_summary jsonb;
    v_counts jsonb;
    v_data jsonb;
    v_state text;
    v_previous text;
    v_object text;
    v_old_size integer;
    v_old_digest text;
BEGIN
    SELECT c.delta_bytes, c.delta_sequence, c.summary INTO v_total_bytes, v_sequence, v_summary
    FROM public.storage_migration_copies AS c
    WHERE c.id = p_copy AND c.last_attempt_id = p_attempt AND c.baseline_sha256 IS NOT NULL
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'copy_context_changed'::text, false, NULL::integer, NULL::integer;
        RETURN;
    END IF;
    PERFORM 1 FROM public.storage_migration_copy_attempts AS a
    WHERE a.id = p_attempt AND a.copy_id = p_copy AND a.async_job_id = p_job
      AND a.task_id = p_task AND a.operation IN ('run', 'verify') FOR UPDATE;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'copy_context_changed'::text, false, NULL::integer, NULL::integer;
        RETURN;
    END IF;
    v_size := octet_length(p_packed) + 1;
    IF v_size IS NULL OR v_size > 262144 OR p_checksum IS DISTINCT FROM
       encode(sha256(convert_to(p_packed || chr(10), 'UTF8')), 'hex') THEN
        RETURN QUERY SELECT 'copy_checkpoint_integrity'::text, false, NULL::integer, NULL::integer;
        RETURN;
    END IF;
    v_data := p_packed::jsonb;
    v_state := v_data->>'state';
    v_object := v_data->>'id';
    IF v_state IS NULL OR v_state NOT IN ('verified', 'pending', 'writing', 'uncertain', 'failed', 'skipped')
       OR v_object IS NULL OR v_object !~ '^[0-9a-f]{64}$' THEN
        RETURN QUERY SELECT 'copy_checkpoint_integrity'::text, false, NULL::integer, NULL::integer;
        RETURN;
    END IF;
    SELECT d.size_bytes, d.digest, d.data->>'state' INTO v_old_size, v_old_digest, v_previous
    FROM public.storage_migration_copy_deltas AS d WHERE d.copy_id = p_copy AND d.object_id = v_object;
    IF NOT FOUND THEN v_previous := 'pending'; END IF;
    v_total_bytes := v_total_bytes + v_size - COALESCE(v_old_size, 0);
    IF v_total_bytes > 268435456 THEN
        RETURN QUERY SELECT 'copy_delta_budget'::text, false, NULL::integer, NULL::integer;
        RETURN;
    END IF;
    v_counts := v_summary->'counts';
    IF jsonb_typeof(v_counts) IS DISTINCT FROM 'object' OR v_previous IS NULL
       OR NOT (v_counts ? v_previous)
       OR NOT (v_counts ?& ARRAY['verified', 'pending', 'writing', 'uncertain', 'failed', 'skipped']) THEN
        RETURN QUERY SELECT 'copy_checkpoint_integrity'::text, false, NULL::integer, NULL::integer;
        RETURN;
    END IF;
    v_counts := jsonb_set(v_counts, ARRAY[v_previous], to_jsonb((v_counts->>v_previous)::integer - 1));
    v_counts := jsonb_set(v_counts, ARRAY[v_state], to_jsonb((v_counts->>v_state)::integer + 1));
    IF EXISTS (SELECT 1 FROM jsonb_each_text(v_counts) AS n WHERE n.value::integer < 0) THEN
        RETURN QUERY SELECT 'copy_checkpoint_integrity'::text, false, NULL::integer, NULL::integer;
        RETURN;
    END IF;
    v_summary := jsonb_set(v_summary, '{counts}', v_counts) || jsonb_build_object(
        'copy_subset_complete', (v_counts->>'pending')::integer = 0
            AND (v_counts->>'writing')::integer = 0 AND (v_counts->>'uncertain')::integer = 0
            AND (v_counts->>'failed')::integer = 0
    );
    v_sequence := v_sequence + 1;
    INSERT INTO public.storage_migration_copy_deltas AS d (copy_id, object_id, sequence, data, digest, size_bytes)
    VALUES (p_copy, v_object, v_sequence, v_data, p_checksum, v_size)
    ON CONFLICT (copy_id, object_id) DO UPDATE
    SET sequence = EXCLUDED.sequence, data = EXCLUDED.data, digest = EXCLUDED.digest,
        size_bytes = EXCLUDED.size_bytes;
    UPDATE public.storage_migration_copies AS c
    SET summary = v_summary, delta_bytes = v_total_bytes, delta_sequence = v_sequence, updated_at = p_at
    WHERE c.id = p_copy;
    PERFORM yoao_assets_v1._migration_checkpoint_changed(p_copy);
    RETURN QUERY SELECT NULL::text, v_old_digest IS DISTINCT FROM p_checksum, v_sequence,
                        (v_counts->>'verified')::integer + (v_counts->>'skipped')::integer;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.record_migration_copy_delta(uuid,uuid,uuid,text,text,text,timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_delta_page(p_copy uuid, p_after integer)
RETURNS TABLE (object_id varchar, sequence integer, data jsonb, digest varchar)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT d.object_id, d.sequence, d.data, d.digest
    FROM public.storage_migration_copy_deltas AS d
    WHERE d.copy_id = p_copy AND d.sequence > GREATEST(p_after, 0)
    ORDER BY d.sequence LIMIT 100000
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_delta_page(uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.migration_copy_failed_objects(p_copy uuid, p_limit integer, p_offset integer)
RETURNS TABLE (total bigint, object_id varchar, size_bytes bigint, error_code text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT t.total, page.object_id, (page.data->>'size_bytes')::bigint,
           CASE WHEN jsonb_typeof(page.data->'error') = 'string' AND page.data->>'error' ~ '^[a-z][a-z0-9_]{0,79}$'
                THEN page.data->>'error' ELSE 'copy_object_failed' END
    FROM (SELECT count(*) AS total FROM public.storage_migration_copy_deltas AS d
          WHERE d.copy_id = p_copy AND d.data->>'state' = 'failed') AS t
    LEFT JOIN (
        SELECT d.object_id, d.data FROM public.storage_migration_copy_deltas AS d
        WHERE d.copy_id = p_copy AND d.data->>'state' = 'failed' ORDER BY d.object_id
        LIMIT LEAST(GREATEST(p_limit, 1), 100) OFFSET LEAST(GREATEST(p_offset, 0), 1000000)
    ) AS page ON true
    ORDER BY page.object_id
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.migration_copy_failed_objects(uuid,integer,integer) FROM PUBLIC;
