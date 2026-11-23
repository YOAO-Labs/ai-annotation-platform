CREATE FUNCTION yoao_assets_storage_v1._fingerprint(p_text text)
RETURNS text LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT encode(sha256(convert_to(p_text,'UTF8')),'hex');
$fn$;

CREATE FUNCTION yoao_assets_storage_v1._saved_target_roles(p_hash boolean)
RETURNS TABLE(family text, purpose text, record_id text, location_id text,
    dataset_id uuid, project_id uuid, task_id uuid, item_id uuid, source_id uuid, asset_id uuid, fingerprint text)
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    RETURN QUERY
    SELECT 'prediction_input_leases'::text,'import'::text,p.id::text,p.storage_ref ->> 'location_id',
        NULL::uuid,NULL::uuid,p.task_id,NULL::uuid,NULL::uuid,NULL::uuid,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'prediction_input_leases','output',p.id,p.owner_kind,p.owner_id,p.task_id,p.contract_version,p.storage_ref,p.bucket,p.storage_identity,p.state)::text) ELSE '' END
    FROM public.prediction_input_leases p
    UNION ALL SELECT 'image_pyramid_outputs','media-cache',p.id::text,p.storage_ref ->> 'location_id',
        NULL,NULL,a.task_id,a.dataset_item_id,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'image_pyramid_outputs','output',p.id,p.asset_id,p.generation,p.lease_token,p.storage_ref,p.state)::text) ELSE '' END
    FROM public.image_pyramid_outputs p LEFT JOIN public.image_pyramid_assets a ON a.id=p.asset_id
    UNION ALL SELECT 'video_derivative_outputs','media-cache',p.id::text,p.storage_ref ->> 'location_id',
        NULL,NULL,NULL,p.dataset_item_id,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'video_derivative_outputs','output',p.id,p.dataset_item_id,p.kind,p.metadata_output_id,p.storage_ref,p.state)::text) ELSE '' END
    FROM public.video_derivative_outputs p
    UNION ALL SELECT 'export_media_bundles','export',p.id::text,p.storage_ref ->> 'location_id',NULL,p.project_id,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'export_media_bundles','output',p.id,p.project_id,p.job_id,p.zip_output_id,p.storage_ref,p.state)::text) ELSE '' END
    FROM yoao_export_v1.export_media_maintenance p
    UNION ALL SELECT 'training_dataset_versions','training',p.id::text,p.storage_ref ->> 'location_id',NULL,p.project_id,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'training_dataset_versions','output',p.id,p.project_id,p.parent_id,p.attempt_id,p.status,p.storage_ref,p.snapshot_digest)::text) ELSE '' END
    FROM public.training_dataset_versions p
    UNION ALL SELECT 'training_runs','training',p.id::text,p.storage_ref ->> 'location_id',NULL,p.project_id,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'training_runs','output',p.id,p.project_id,p.dataset_id,p.current_attempt_id,p.status,p.storage_ref)::text) ELSE '' END
    FROM yoao_learning_v1.training_run_storage_owner p
    UNION ALL SELECT 'training_previews','training',p.id::text,p.storage_ref ->> 'location_id',NULL,p.project_id,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'training_previews','output',p.id,p.project_id,p.dataset_id,p.claim_id,p.status,p.storage_ref)::text) ELSE '' END
    FROM yoao_learning_v1.training_preview_storage_owner p
    UNION ALL SELECT 'sample_evaluation_runs','training',p.id::text,p.storage_ref ->> 'location_id',NULL,p.project_id,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'sample_evaluation_runs','output',p.id,p.project_id,p.dataset_id,p.generation,p.current_attempt_id,p.status,p.storage_ref)::text) ELSE '' END
    FROM yoao_learning_v1.sample_evaluation_storage_owners p
    UNION ALL SELECT 'active_learning_rounds','training',p.id::text,p.storage_ref ->> 'location_id',NULL,p.project_id,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'active_learning_rounds','output',p.id,p.project_id,p.generation,p.current_attempt_id,p.status,p.storage_ref)::text) ELSE '' END
    FROM yoao_learning_v1.selection_storage_owners p
    UNION ALL SELECT 'avatar_uploads','avatars',p.token::text,p.storage_ref ->> 'location_id',NULL,NULL,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'avatar_uploads','output',p.token,p.owner_id,p.state,p.storage_ref,p.object_version_id)::text) ELSE '' END
    FROM public.avatar_uploads p
    UNION ALL SELECT 'bug_attachment_uploads','bug-reports',p.id::text,p.storage_ref ->> 'location_id',NULL,NULL,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'bug_attachment_uploads','output',p.id,p.owner_id,p.bound_report_id,p.state,p.storage_ref,p.object_version_id)::text) ELSE '' END
    FROM yoao_diagnostics_v1.bug_attachment_maintenance_facts() p
    UNION ALL SELECT 'comment_attachment_uploads','annotations',p.id::text,p.storage_ref ->> 'location_id',NULL,p.project_id,p.task_id,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'comment_attachment_uploads','output',p.id,p.owner_id,p.project_id,p.task_id,p.annotation_id,p.bound_comment_id,p.state,p.storage_ref)::text) ELSE '' END
    FROM public.comment_attachment_uploads p
    UNION ALL SELECT 'guide_asset_uploads','annotations',p.id::text,p.storage_ref ->> 'location_id',NULL,p.project_id,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'guide_asset_uploads','output',p.id,p.owner_id,p.project_id,p.state,p.storage_ref)::text) ELSE '' END
    FROM public.guide_asset_uploads p
    UNION ALL SELECT 'raster_mask_objects','annotations',p.id::text,p.storage_ref ->> 'location_id',NULL,NULL,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'raster_mask_objects','output',p.id,p.logical_key,p.canonical_sha256,p.stored_sha256,p.stored_size_bytes,p.storage_encoding,p.state,p.storage_ref)::text) ELSE '' END
    FROM public.raster_mask_objects p
    UNION ALL SELECT 'export_outputs','export',p.id::text,p.storage_ref ->> 'location_id',NULL,p.project_id,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'export_outputs','output',p.id,p.project_id,p.job_id,p.media_bundle_id,p.state,p.storage_ref)::text) ELSE '' END
    FROM yoao_export_v1.export_zip_maintenance p
    UNION ALL SELECT 'audit_archive_outputs','audit-archive',p.record_id,p.location_id,NULL,NULL,NULL,NULL,NULL,NULL,p.fingerprint
    FROM yoao_audit_v1.archive_maintenance_targets(p_hash) p
    UNION ALL SELECT 'managed_original_creations',CASE p.owner_kind WHEN 'task' THEN 'annotations' ELSE 'datasets' END,
        p.id::text,p.location_id::text,p.dataset_id,p.project_id,p.task_id,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'managed_original_creations','final',p.id,p.owner_kind,p.dataset_id,p.project_id,p.task_id,p.status,p.location_id,
            p.bucket,p.object_key,p.storage_identity,p.version_id,p.configuration -> 'original_ref',p.configuration -> 'endpoint')::text) ELSE '' END
    FROM public.managed_original_creations p
    UNION ALL SELECT 'managed_original_creations','import',p.id::text,p.staging_ref ->> 'location_id',p.dataset_id,p.project_id,p.task_id,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'managed_original_creations','staging',p.id,p.owner_kind,p.dataset_id,p.project_id,p.task_id,p.status,p.staging_ref,p.staging_bucket,p.staging_key,p.origin)::text) ELSE '' END
    FROM public.managed_original_creations p WHERE p.staging_key IS NOT NULL AND p.staging_key <> ''
    UNION ALL SELECT 'managed_original_retirements','datasets',p.id::text,p.location_id::text,p.dataset_id,NULL,NULL,p.dataset_item_id,NULL,p.asset_id,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'managed_original_retirements','output',p.id,p.assertion_digest,p.creation_id,p.dataset_id,p.dataset_item_id,p.asset_id,p.location_id,
            p.bucket,p.object_key,p.storage_identity,p.location_config_revision,p.binding_generation,p.role,p.identity_state,p.reference_state)::text) ELSE '' END
    FROM public.managed_original_retirements p
    UNION ALL SELECT 'dataset_asset_locations','datasets',jsonb_build_array(p.asset_id,p.location_id,p.bucket)::text,p.location_id::text,
        NULL,NULL,NULL,NULL,NULL,p.asset_id,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'dataset_asset_locations','output',p.asset_id,p.location_id,p.bucket,p.object_key,p.role,p.reference_state,p.creation_id,p.storage_ref)::text) ELSE '' END
    FROM public.dataset_asset_locations p WHERE p.role = 'managed_original'
    UNION ALL SELECT 'task_originals','annotations',p.id::text,p.location_id::text,NULL,p.project_id,p.task_id,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'task_originals','output',p.id,p.creation_id,p.task_id,p.project_id,p.location_id,p.bucket,p.object_key,p.storage_identity,p.storage_ref,p.version_id)::text) ELSE '' END
    FROM public.task_originals p
    UNION ALL SELECT 'dataset_auxiliary_originals','datasets',p.id::text,p.location_id::text,p.dataset_id,NULL,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'dataset_auxiliary_originals','output',p.id,p.creation_id,p.dataset_id,p.kind,p.location_id,p.bucket,p.object_key,p.storage_identity,p.storage_ref,p.version_id)::text) ELSE '' END
    FROM public.dataset_auxiliary_originals p
    UNION ALL SELECT 'dataset_source_previews','media-cache',jsonb_build_array(p.source_id,p.path_digest)::text,p.location_id::text,
        NULL,NULL,NULL,NULL,p.source_id,p.asset_id,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'dataset_source_previews','output',p.source_id,p.path_digest,p.asset_id,p.thumbnail_output_id,p.generation,p.policy_revision,p.state,p.location_id,p.bucket,p.object_key)::text) ELSE '' END
    FROM public.dataset_source_previews p WHERE p.bucket <> '' AND p.object_key <> ''
    UNION ALL SELECT 'original_cache_entries','media-cache',p.id::text,p.location_id::text,NULL,NULL,NULL,NULL,p.source_id,p.asset_id,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'original_cache_entries','output',p.id,p.source_id,p.asset_id,p.sha256,p.scope_location_id,p.source_revision,p.policy_revision,
            p.location_revision,p.location_id,p.bucket,p.binding_generation,p.storage_identity,p.storage_ref,p.state)::text) ELSE '' END
    FROM public.original_cache_entries p
    UNION ALL SELECT 'training_storage_deletions','training',p.id::text,p.storage_ref ->> 'location_id',NULL,NULL,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'training_storage_deletions','output',p.id,p.storage_ref,p.bucket,p.prefix)::text) ELSE '' END
    FROM public.training_storage_deletions p
    UNION ALL SELECT 'mask_format_uploads','import',p.id::text,p.storage_ref ->> 'location_id',NULL,p.project_id,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'mask_format_uploads','output',p.id,p.project_id,p.uploader_id,p.logical_object_key,p.storage_ref)::text) ELSE '' END
    FROM public.mask_format_uploads p
    UNION ALL SELECT 'mask_format_imports','import',p.id::text,p.staging_ref ->> 'location_id',NULL,p.project_id,NULL,NULL,NULL,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'mask_format_imports','output',p.id,p.project_id,p.staged_object_key,p.staged_sha256,p.staging_ref,p.status)::text) ELSE '' END
    FROM public.mask_format_imports p
    UNION ALL SELECT 'thumbnail_outputs','media-cache',p.id::text,p.storage_ref ->> 'location_id',NULL,NULL,
        CASE WHEN p.owner_kind = 'task_thumbnail' THEN p.owner_id END,
        CASE WHEN p.owner_kind = 'dataset_thumbnail' THEN p.owner_id END,
        CASE WHEN p.owner_kind = 'directory_preview' THEN p.owner_id END,NULL,
        CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
            'thumbnail_outputs','output',p.id,p.owner_kind,p.owner_id,p.path_digest,p.state,p.storage_ref,p.version_id)::text) ELSE '' END
    FROM public.thumbnail_outputs p;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1._saved_target_impact(p_location uuid,p_hash boolean)
RETURNS TABLE(counts jsonb,fingerprint text) LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp AS $fn$
    WITH roles AS (SELECT * FROM yoao_assets_storage_v1._saved_target_roles(p_hash)),
    picked AS (SELECT * FROM roles p WHERE p.location_id = p_location::text),
    assets AS (SELECT a.id,a.dataset_item_id FROM public.dataset_assets a WHERE a.id IN (SELECT p.asset_id FROM picked p)),
    sources AS (SELECT s.id,s.dataset_id FROM public.dataset_sources s WHERE s.id IN (SELECT p.source_id FROM picked p)),
    items AS (
        SELECT p.item_id AS id FROM picked p WHERE p.item_id IS NOT NULL
        UNION SELECT a.dataset_item_id FROM assets a
        UNION SELECT t.dataset_item_id FROM yoao_labeling_v1.task_rows t
            WHERE t.id IN (SELECT p.task_id FROM picked p) AND t.dataset_item_id IS NOT NULL
    ),
    tasks AS (
        SELECT p.task_id AS id FROM picked p WHERE p.task_id IS NOT NULL
        UNION SELECT t.id FROM yoao_labeling_v1.task_rows t WHERE t.dataset_item_id IN (SELECT i.id FROM items i)
    ),
    datasets AS (
        SELECT p.dataset_id AS id FROM picked p WHERE p.dataset_id IS NOT NULL
        UNION SELECT s.dataset_id FROM sources s WHERE s.dataset_id IS NOT NULL
        UNION SELECT d.dataset_id FROM public.dataset_items d WHERE d.id IN (SELECT i.id FROM items i)
    ),
    project_links AS (SELECT p.id,p.dataset_id,p.project_id FROM public.project_datasets p WHERE p.dataset_id IN (SELECT d.id FROM datasets d)),
    guides AS (SELECT g.project_id,g.upload_id FROM public.project_guide_assets g
        WHERE g.upload_id::text IN (SELECT p.record_id FROM picked p WHERE p.family = 'guide_asset_uploads')),
    projects AS (
        SELECT p.project_id AS id FROM picked p WHERE p.project_id IS NOT NULL
        UNION SELECT t.project_id FROM yoao_labeling_v1.task_rows t WHERE t.id IN (SELECT a.id FROM tasks a)
        UNION SELECT p.project_id FROM project_links p
        UNION SELECT g.project_id FROM guides g
    ),
    bindings AS (SELECT b.purpose,b.location_id,b.bucket,b.generation FROM public.storage_bindings b WHERE b.location_id = p_location)
    SELECT jsonb_build_object(
        'target_records',(SELECT count(*) FROM picked),
        'target_annotations',(SELECT count(*) FROM picked p WHERE p.purpose='annotations'),
        'target_datasets',(SELECT count(*) FROM picked p WHERE p.purpose='datasets'),
        'target_training',(SELECT count(*) FROM picked p WHERE p.purpose='training'),
        'target_media_cache',(SELECT count(*) FROM picked p WHERE p.purpose='media-cache'),
        'target_audit_archive',(SELECT count(*) FROM picked p WHERE p.purpose='audit-archive'),
        'target_import',(SELECT count(*) FROM picked p WHERE p.purpose='import'),
        'target_export',(SELECT count(*) FROM picked p WHERE p.purpose='export'),
        'target_avatars',(SELECT count(*) FROM picked p WHERE p.purpose='avatars'),
        'target_bug_reports',(SELECT count(*) FROM picked p WHERE p.purpose='bug-reports'),
        'target_related_datasets',(SELECT count(*) FROM datasets),
        'target_related_projects',(SELECT count(*) FROM projects),
        'target_related_tasks',(SELECT count(*) FROM tasks),
        'target_versions',(SELECT count(*) FROM picked p WHERE p.family='training_dataset_versions'),
        'target_runs',(SELECT count(*) FROM picked p WHERE p.family='training_runs'),
        'current_bindings',(SELECT count(*) FROM bindings),
        'unlocated_targets',(SELECT count(*) FROM roles p WHERE p.location_id IS NULL)
    ), CASE WHEN p_hash THEN (WITH h AS (
        SELECT 1 AS family,p.fingerprint AS v FROM picked p
        UNION ALL SELECT 2,yoao_assets_storage_v1._fingerprint(jsonb_build_array('storage_bindings',b.purpose,b.location_id,b.bucket,b.generation)::text) FROM bindings b
        UNION ALL SELECT 3,yoao_assets_storage_v1._fingerprint(jsonb_build_array('dataset_assets',a.id,a.dataset_item_id)::text) FROM assets a
        UNION ALL SELECT 4,yoao_assets_storage_v1._fingerprint(jsonb_build_array('dataset_sources',s.id,s.dataset_id)::text) FROM sources s
        UNION ALL SELECT 5,yoao_assets_storage_v1._fingerprint(jsonb_build_array('dataset_items',i.id,i.dataset_id)::text)
            FROM public.dataset_items i WHERE i.id IN (SELECT a.id FROM items a)
        UNION ALL SELECT 6,yoao_assets_storage_v1._fingerprint(jsonb_build_array('tasks',t.id,t.dataset_item_id,t.project_id)::text)
            FROM yoao_labeling_v1.task_rows t WHERE t.id IN (SELECT a.id FROM tasks a)
        UNION ALL SELECT 7,yoao_assets_storage_v1._fingerprint(jsonb_build_array('project_datasets',p.id,p.dataset_id,p.project_id)::text) FROM project_links p
        UNION ALL SELECT 8,yoao_assets_storage_v1._fingerprint(jsonb_build_array('project_guide_assets',g.project_id,g.upload_id)::text) FROM guides g
        UNION ALL SELECT 9,yoao_assets_storage_v1._fingerprint(jsonb_build_array('image_pyramid_assets',a.id,a.dataset_item_id,a.task_id)::text)
            FROM public.image_pyramid_assets a WHERE a.id IN (SELECT p.asset_id FROM public.image_pyramid_outputs p WHERE p.storage_ref ->> 'location_id'=p_location::text)
    ), grouped AS (SELECT h.family,string_agg(h.v,'' ORDER BY h.v) AS v FROM h GROUP BY h.family)
    SELECT yoao_assets_storage_v1._fingerprint(jsonb_agg(yoao_assets_storage_v1._fingerprint(coalesce(g.v,'')) ORDER BY n.id)::text)
    FROM generate_series(1,9) n(id) LEFT JOIN grouped g ON g.family=n.id) END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.maintenance_dependency_snapshot(p_location uuid,p_hash boolean)
RETURNS TABLE(measured_at timestamptz, counts jsonb, dependency_fingerprint text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    RETURN QUERY
    WITH sources AS (SELECT d.id,d.dataset_id,d.config_revision,d.policy_revision,d.enabled,d.unregistered_at,d.generation
        FROM public.dataset_sources d WHERE d.location_id=p_location),
    locations AS (SELECT a.asset_id,a.location_id,a.bucket,a.object_key,a.role,a.reference_state
        FROM public.dataset_asset_locations a WHERE a.location_id=p_location),
    revisions AS (SELECT r.id,r.dataset_item_id,r.asset_id FROM public.dataset_source_revisions r WHERE r.source_id IN (SELECT s.id FROM sources s)),
    refs AS (SELECT r.dataset_item_id,r.source_id,r.run_id,r.path_digest,r.source_revision,r.location_revision
        FROM public.dataset_references r WHERE r.source_id IN (SELECT s.id FROM sources s)),
    assets AS (SELECT a.id,a.dataset_item_id FROM public.dataset_assets a WHERE a.id IN (SELECT l.asset_id FROM locations l)),
    item_ids AS (
        SELECT r.dataset_item_id AS id FROM revisions r WHERE r.dataset_item_id IS NOT NULL
        UNION SELECT a.dataset_item_id FROM assets a
        UNION SELECT r.dataset_item_id FROM refs r
    ),
    items AS (SELECT i.id,i.dataset_id FROM public.dataset_items i WHERE i.id IN (SELECT k.id FROM item_ids k)),
    datasets AS (
        SELECT s.dataset_id AS id FROM sources s WHERE s.dataset_id IS NOT NULL
        UNION SELECT i.dataset_id FROM items i
    ),
    tasks AS (SELECT t.id,t.dataset_item_id,t.project_id FROM yoao_labeling_v1.task_rows t WHERE t.dataset_item_id IN (SELECT k.id FROM item_ids k)),
    project_links AS (SELECT p.id,p.dataset_id,p.project_id FROM public.project_datasets p WHERE p.dataset_id IN (SELECT d.id FROM datasets d)),
    projects AS (SELECT p.project_id FROM project_links p UNION SELECT t.project_id FROM tasks t),
    annotations AS (SELECT a.id,a.task_id FROM yoao_labeling_v1.annotation_rows a WHERE a.task_id IN (SELECT t.id FROM tasks t)),
    scans AS (SELECT r.id,r.source_id,r.status,r.generation,r.async_job_id FROM public.dataset_scan_runs r
        WHERE r.source_id IN (SELECT s.id FROM sources s) AND r.status IN ('pending','running')),
    registrations AS (SELECT r.id,r.source_id,r.status,r.generation,r.async_job_id FROM public.dataset_registration_runs r
        WHERE r.source_id IN (SELECT s.id FROM sources s) AND r.status IN ('pending','running')),
    schedules AS (SELECT s.source_id,s.revision,s.enabled FROM public.dataset_source_sync_schedules s
        WHERE s.source_id IN (SELECT a.id FROM sources a) AND s.enabled),
    previews AS (SELECT p.source_id,p.path_digest,p.asset_id,p.generation,p.policy_revision,p.state,p.size_bytes
        FROM public.dataset_source_previews p WHERE p.source_id IN (SELECT s.id FROM sources s)),
    cache AS (SELECT c.id,c.source_id,c.asset_id,c.state,c.size_bytes,c.source_revision,c.policy_revision,c.location_revision
        FROM public.original_cache_entries c WHERE c.scope_location_id=p_location),
    leases AS (SELECT l.id,l.entry_id,l.expires_at FROM public.original_cache_read_leases l
        WHERE l.entry_id IN (SELECT c.id FROM cache c) AND l.expires_at>statement_timestamp()),
    inputs AS (SELECT i.id,i.task_id,i.state FROM public.prediction_input_leases i
        WHERE i.task_id IN (SELECT t.id FROM tasks t) AND i.state IN ('active','cleanup_pending')),
    versions AS (SELECT v.id,v.status,v.snapshot_digest FROM yoao_learning_v1.training_dataset_location_versions(p_location) v
        WHERE v.status IN ('pending','running','ready')),
    saved AS (SELECT * FROM yoao_assets_storage_v1._saved_target_impact(p_location,p_hash))
    SELECT statement_timestamp(), saved.counts || jsonb_build_object(
        'migration_copy_plans',yoao_assets_v1.migration_copy_target_reference_count(p_location),
        'sources',(SELECT count(*) FROM sources),
        'enabled_sources',(SELECT count(*) FROM sources s WHERE s.enabled),
        'unregistered_sources',(SELECT count(*) FROM sources s WHERE s.unregistered_at IS NOT NULL),
        'items',(SELECT count(*) FROM item_ids),
        'reference_items',(SELECT count(*) FROM refs),
        'datasets',(SELECT count(*) FROM datasets),
        'projects',(SELECT count(*) FROM projects),
        'tasks',(SELECT count(*) FROM tasks),
        'annotations',(SELECT count(*) FROM annotations),
        'active_scans',(SELECT count(*) FROM scans),
        'active_registrations',(SELECT count(*) FROM registrations),
        'enabled_schedules',(SELECT count(*) FROM schedules),
        'ready_previews',(SELECT count(*) FROM previews p WHERE p.state='ready'),
        'failed_previews',(SELECT count(*) FROM previews p WHERE p.state='failed'),
        'preview_bytes',(SELECT coalesce(sum(p.size_bytes),0) FROM previews p WHERE p.state='ready'),
        'cache_entries',(SELECT count(*) FROM cache),
        'cache_bytes',(SELECT coalesce(sum(c.size_bytes),0) FROM cache c),
        'active_cache_read_leases',(SELECT count(*) FROM leases),
        'active_input_leases',(SELECT count(*) FROM inputs i WHERE i.state='active'),
        'cleanup_input_leases',(SELECT count(*) FROM inputs i WHERE i.state='cleanup_pending'),
        'ready_lineage_versions',(SELECT count(*) FROM versions v WHERE v.status='ready'),
        'unfinished_lineage_versions',(SELECT count(*) FROM versions v WHERE v.status IN ('pending','running'))
    ), CASE WHEN p_hash THEN yoao_assets_storage_v1._fingerprint(jsonb_build_array(
        yoao_assets_v1.migration_copy_target_reference_footprint(p_location),saved.fingerprint,
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(s.id,s.dataset_id,s.config_revision,s.policy_revision,s.enabled,s.unregistered_at,s.generation)::text),'' ORDER BY s.id),'')) FROM sources s),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(r.id,r.dataset_item_id,r.asset_id)::text),'' ORDER BY r.id),'')) FROM revisions r),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(r.dataset_item_id,r.source_id,r.run_id,r.path_digest,r.source_revision,r.location_revision)::text),'' ORDER BY r.dataset_item_id,r.source_id,r.run_id,r.path_digest,r.source_revision,r.location_revision),'')) FROM refs r),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(a.asset_id,a.location_id,a.bucket,a.object_key,a.role,a.reference_state)::text),'' ORDER BY a.asset_id,a.location_id,a.bucket,a.object_key,a.role,a.reference_state),'')) FROM locations a),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(a.id,a.dataset_item_id)::text),'' ORDER BY a.id),'')) FROM assets a),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(i.id,i.dataset_id)::text),'' ORDER BY i.id),'')) FROM items i),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(p.id,p.dataset_id,p.project_id)::text),'' ORDER BY p.id),'')) FROM project_links p),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(t.id,t.dataset_item_id,t.project_id)::text),'' ORDER BY t.id),'')) FROM tasks t),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(a.id,a.task_id)::text),'' ORDER BY a.id,a.task_id),'')) FROM annotations a),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(s.id,s.source_id,s.status,s.generation,s.async_job_id)::text),'' ORDER BY s.id),'')) FROM scans s),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(r.id,r.source_id,r.status,r.generation,r.async_job_id)::text),'' ORDER BY r.id),'')) FROM registrations r),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(s.source_id,s.revision,s.enabled)::text),'' ORDER BY s.source_id),'')) FROM schedules s),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(p.source_id,p.path_digest,p.asset_id,p.generation,p.policy_revision,p.state,p.size_bytes)::text),'' ORDER BY p.source_id,p.path_digest),'')) FROM previews p),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(c.id,c.source_id,c.asset_id,c.state,c.size_bytes,c.source_revision,c.policy_revision,c.location_revision)::text),'' ORDER BY c.id),'')) FROM cache c),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(l.id,l.entry_id,l.expires_at)::text),'' ORDER BY l.id),'')) FROM leases l),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(i.id,i.task_id,i.state)::text),'' ORDER BY i.id),'')) FROM inputs i),
        (SELECT yoao_assets_storage_v1._fingerprint(coalesce(string_agg(yoao_assets_storage_v1._fingerprint(jsonb_build_array(v.id,v.status,v.snapshot_digest)::text),'' ORDER BY v.id),'')) FROM versions v)
    )::text) END
    FROM saved;
END;
$fn$;

CREATE FUNCTION yoao_assets_storage_v1.location_storage_referenced(p_location uuid)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    RETURN yoao_assets_v1.migration_copy_target_reference_count(p_location) > 0
        OR yoao_audit_v1.archive_location_referenced(p_location)
        OR yoao_export_v1.saved_location_is_referenced(p_location)
        OR yoao_diagnostics_v1.bug_attachment_location_referenced(p_location)
        OR EXISTS (SELECT FROM public.avatar_uploads p WHERE p.storage_ref ->> 'location_id'=p_location::text)
        OR EXISTS (SELECT FROM public.storage_bindings p WHERE p.location_id=p_location)
        OR EXISTS (SELECT FROM public.dataset_sources p WHERE p.location_id=p_location)
        OR EXISTS (SELECT FROM public.dataset_asset_locations p WHERE p.location_id=p_location)
        OR EXISTS (SELECT FROM public.dataset_auxiliary_originals p WHERE p.location_id=p_location)
        OR EXISTS (SELECT FROM public.managed_original_retirements p WHERE p.location_id=p_location)
        OR EXISTS (SELECT FROM public.managed_original_creations p
            WHERE p.location_id=p_location OR p.staging_ref ->> 'location_id'=p_location::text)
        OR EXISTS (SELECT FROM public.original_cache_entries p WHERE p.location_id=p_location)
        OR EXISTS (SELECT FROM public.task_originals p WHERE p.location_id=p_location)
        OR EXISTS (
            SELECT FROM public.prediction_input_leases p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM public.thumbnail_outputs p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM public.image_pyramid_outputs p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM public.video_derivative_outputs p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM public.mask_format_uploads p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM public.mask_format_imports p WHERE p.staging_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM public.comment_attachment_uploads p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM public.guide_asset_uploads p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM public.raster_mask_objects p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM public.training_dataset_versions p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM public.training_storage_deletions p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM yoao_learning_v1.training_run_storage_owner p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM yoao_learning_v1.training_preview_storage_owner p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM yoao_learning_v1.sample_evaluation_storage_owners p WHERE p.storage_ref ->> 'location_id'=p_location::text
            UNION ALL SELECT FROM yoao_learning_v1.selection_storage_owners p WHERE p.storage_ref ->> 'location_id'=p_location::text);
END;
$fn$;

REVOKE ALL ON FUNCTION yoao_assets_storage_v1._fingerprint(text),
    yoao_assets_storage_v1._saved_target_roles(boolean),
    yoao_assets_storage_v1._saved_target_impact(uuid,boolean),
    yoao_assets_storage_v1.maintenance_dependency_snapshot(uuid,boolean),
    yoao_assets_storage_v1.location_storage_referenced(uuid) FROM PUBLIC;
