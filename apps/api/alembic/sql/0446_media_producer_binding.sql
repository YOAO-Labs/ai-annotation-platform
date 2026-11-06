-- Jobs calls this owner-only source classifier inside the original producer's
-- transaction.  It never accepts a caller-supplied fixture run or location.
CREATE FUNCTION aap_assets_v1.media_source_binding(p_kind text,p_owner uuid,p_source jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_task record; v_link record; v_primary uuid; v_links integer := 0; v_primary_count integer := 0;
    v_item uuid; v_dataset uuid; v_run uuid; v_item_run uuid; v_task_run uuid;
    v_root uuid; v_state text; v_root_state text; v_key text; v_identity text; v_source_kind text;
    v_content uuid; v_location uuid; v_location_revision bigint; v_gate_revision bigint;
    v_endpoint text; v_bucket text; v_saved_identity text; v_gate_state text;
BEGIN
    IF p_kind NOT IN ('task','dataset_item') OR p_owner IS NULL
       OR pg_catalog.jsonb_typeof(p_source) IS DISTINCT FROM 'object'
       OR NOT (p_source ?& ARRAY['schema','owner_kind','content_id','sha256','size_bytes','location_id',
                                 'location_revision','maintenance_revision','binding'])
       OR (p_source - ARRAY['schema','owner_kind','content_id','sha256','size_bytes','location_id',
                             'location_revision','maintenance_revision','binding']) <> '{}'::jsonb
       OR p_source -> 'schema' IS DISTINCT FROM '1'::jsonb
       OR COALESCE(p_source ->> 'content_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR COALESCE(p_source ->> 'location_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR COALESCE(p_source ->> 'sha256','') !~ '^[0-9a-f]{64}$'
       OR COALESCE(p_source ->> 'binding','') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_source -> 'size_bytes') IS DISTINCT FROM 'number'
       OR COALESCE(p_source ->> 'size_bytes','') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_source -> 'location_revision') IS DISTINCT FROM 'number'
       OR COALESCE(p_source ->> 'location_revision','') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_source -> 'maintenance_revision') IS DISTINCT FROM 'number'
       OR COALESCE(p_source ->> 'maintenance_revision','') !~ '^[1-9][0-9]*$' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_source_invalid';
    END IF;
    v_content := (p_source ->> 'content_id')::uuid;
    v_location := (p_source ->> 'location_id')::uuid;
    IF p_kind='task' THEN
        SELECT * INTO v_task FROM aap_labeling_v1.lock_task_for_core_read(p_owner,'share',true,NULL);
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_subject_missing'; END IF;
        FOR v_link IN SELECT * FROM aap_labeling_v1.lock_task_dataset_item_links_for_core_read(p_owner,false,NULL,NULL) LOOP
            v_links := v_links+1;
            IF v_link.role='primary_lidar' THEN v_primary_count:=v_primary_count+1; v_primary:=v_link.dataset_item_id; END IF;
        END LOOP;
        IF v_primary_count>1 OR (v_task.original_id IS NOT NULL AND (v_task.dataset_item_id IS NOT NULL OR v_links>0))
           OR (v_task.dataset_item_id IS NOT NULL AND v_primary IS NOT NULL AND v_task.dataset_item_id<>v_primary) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_subject_conflict';
        END IF;
        v_item := COALESCE(v_task.dataset_item_id,v_primary);
        SELECT run_id INTO v_task_run FROM yoao_fixture_v1.fixture_run_resources
        WHERE kind='task' AND resource_id=p_owner AND state<>'retired';
    ELSE
        v_item := p_owner;
    END IF;
    IF v_item IS NOT NULL THEN
        v_source_kind := 'dataset_asset';
        SELECT item.dataset_id,source.object_key,
               COALESCE(source.storage_ref ->> 'storage_identity',creation.storage_identity),
               COALESCE(source.storage_ref ->> 'endpoint_url',
                   CASE WHEN location.connection_id IS NULL THEN
                       (CASE WHEN location.config ->> 'use_ssl'='true' THEN 'https://' ELSE 'http://' END)
                       || (location.config ->> 'endpoint')
                   WHEN connection.config ->> 'endpoint' ~ '^https?://' THEN connection.config ->> 'endpoint'
                   ELSE (CASE WHEN connection.config ->> 'use_ssl'='true' THEN 'https://' ELSE 'http://' END)
                       || (connection.config ->> 'endpoint') END),
               source.bucket,location.config_revision
        INTO v_dataset,v_key,v_saved_identity,v_endpoint,v_bucket,v_location_revision
        FROM public.dataset_items AS item
        JOIN public.dataset_assets AS asset ON asset.dataset_item_id=item.id
        JOIN public.dataset_asset_locations AS source ON source.asset_id=asset.id
        JOIN public.storage_locations AS location ON location.id=source.location_id
        LEFT JOIN public.storage_connections AS connection ON connection.id=location.connection_id
        LEFT JOIN public.managed_original_creations AS creation ON creation.id=source.creation_id
        WHERE item.id=v_item AND asset.id=v_content AND source.location_id=v_location
          AND source.role='managed_original' AND source.reference_state='present'
          AND source.object_key=item.file_path AND asset.verification_state='verified'
          AND asset.checksum_algorithm='sha256' AND asset.checksum_value=p_source ->> 'sha256'
          AND asset.size_bytes::text=p_source ->> 'size_bytes' AND item.file_type=asset.media_type
          AND location.enabled
        FOR SHARE OF item,asset,source,location;
        IF NOT FOUND OR (SELECT pg_catalog.count(*) FROM public.dataset_asset_locations AS source
                         WHERE source.asset_id=v_content AND source.role='managed_original'
                           AND source.reference_state='present')<>1 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_source_changed';
        END IF;
        IF v_endpoint IS NULL OR v_bucket IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_source_changed';
        END IF;
        v_identity := aap_assets_v1._managed_original_identity(v_endpoint,v_bucket);
        IF v_saved_identity IS NOT NULL AND v_saved_identity IS DISTINCT FROM v_identity THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_source_changed';
        END IF;
        SELECT resource.run_id INTO v_run FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind='dataset' AND resource.resource_id=v_dataset AND resource.state<>'retired';
        SELECT resource.run_id INTO v_item_run FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind='item' AND resource.resource_id=v_item AND resource.state<>'retired';
        IF (v_run IS NULL) IS DISTINCT FROM (v_item_run IS NULL)
           OR (v_run IS NOT NULL AND v_run IS DISTINCT FROM v_item_run)
           OR (v_task_run IS NOT NULL AND v_task_run IS DISTINCT FROM v_run) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_scope_unknown';
        END IF;
    ELSE
        v_source_kind := 'task_original';
        IF p_kind<>'task' OR v_task.original_id IS DISTINCT FROM v_content THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_source_changed';
        END IF;
        SELECT original.object_key,original.storage_identity,location.config_revision
        INTO v_key,v_identity,v_location_revision
        FROM public.task_originals AS original
        JOIN public.storage_locations AS location ON location.id=original.location_id
        JOIN public.managed_original_creations AS creation ON creation.id=original.creation_id
        WHERE original.id=v_content AND original.task_id=p_owner AND original.project_id=v_task.project_id
          AND original.location_id=v_location AND original.object_key=v_task.file_path
          AND original.sha256=p_source ->> 'sha256' AND original.size_bytes::text=p_source ->> 'size_bytes'
          AND creation.owner_kind='task' AND creation.task_id=original.task_id
          AND creation.project_id=original.project_id AND creation.dataset_id IS NULL
          AND creation.location_id=original.location_id AND creation.storage_identity=original.storage_identity
          AND creation.bucket=original.bucket AND creation.object_key=original.object_key
          AND creation.sha256=original.sha256 AND creation.size_bytes=original.size_bytes
          AND creation.etag=original.etag AND creation.version_id IS NOT DISTINCT FROM original.version_id
          AND creation.status='verified' AND creation.settled_at IS NOT NULL
          AND location.enabled
        FOR SHARE OF original,location,creation;
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_source_changed'; END IF;
        IF v_task_run IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_task_original_source_untracked';
        END IF;
        v_run := v_task_run;
    END IF;
    SELECT gate.revision,gate.state INTO v_gate_revision,v_gate_state
    FROM public.storage_maintenance_gates AS gate WHERE gate.location_id=v_location FOR SHARE;
    IF NOT FOUND THEN v_gate_revision:=1; v_gate_state:='active'; END IF;
    IF v_gate_state IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_source_changed';
    END IF;
    IF p_source ->> 'owner_kind' IS DISTINCT FROM v_source_kind
       OR v_location_revision::text IS DISTINCT FROM p_source ->> 'location_revision'
       OR v_gate_revision::text IS DISTINCT FROM p_source ->> 'maintenance_revision' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_source_changed';
    END IF;
    IF v_run IS NULL THEN
        IF v_identity IS NULL OR aap_assets_v1.is_fixture_original_source(v_key,v_identity) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_scope_unknown';
        END IF;
        RETURN pg_catalog.jsonb_build_object('schema','aap-assets-media-producer-binding/v1',
            'ownership','ordinary','run_id',NULL,'dataset_id',NULL,'item_id',NULL);
    END IF;
    SELECT COALESCE(parent_run_id,id) INTO v_root FROM yoao_fixture_v1.fixture_runs WHERE id=v_run;
    SELECT state INTO v_root_state FROM yoao_fixture_v1.fixture_runs WHERE id=v_root AND parent_run_id IS NULL FOR SHARE;
    SELECT state INTO v_state FROM yoao_fixture_v1.fixture_runs
    WHERE id=v_run AND COALESCE(parent_run_id,id)=v_root FOR SHARE;
    IF v_state IS DISTINCT FROM 'open' OR v_root_state IS DISTINCT FROM 'open'
       OR (v_item IS NOT NULL AND NOT EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
           WHERE resource.run_id=v_run AND resource.kind='item' AND resource.resource_id=v_item AND resource.state='published'
       )) OR (p_kind='task' AND NOT EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
           WHERE resource.run_id=v_run AND resource.kind='task' AND resource.resource_id=p_owner AND resource.state='published'
       )) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_scope_closed'; END IF;
    RETURN pg_catalog.jsonb_build_object('schema','aap-assets-media-producer-binding/v1',
        'ownership','fixture','run_id',v_run,'dataset_id',v_dataset,'item_id',v_item);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.media_source_binding(text,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.media_producer_binding(p_kind text,p_owner uuid,p_producer text,p_delivery uuid,p_source jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_entry jsonb; v_output record; v_binding jsonb; v_current jsonb; v_seen boolean := false;
BEGIN
    IF p_delivery IS NULL OR p_producer NOT IN ('pyramid','thumbnail','task_thumbnail','video_metadata','video_derivatives')
       OR (p_producer IN ('thumbnail','video_metadata','video_derivatives') AND p_kind<>'dataset_item')
       OR (p_producer='task_thumbnail' AND p_kind<>'task') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_kind_invalid';
    END IF;
    IF p_producer<>'video_derivatives' THEN
        v_binding := aap_assets_v1.media_source_binding(p_kind,p_owner,p_source);
        IF v_binding ->> 'ownership'='fixture' AND aap_assets_v1.fixture_explicit_media_producer(
            (v_binding ->> 'run_id')::uuid,(v_binding ->> 'dataset_id')::uuid,(v_binding ->> 'item_id')::uuid,
            CASE WHEN v_binding ->> 'dataset_id' IS NULL AND p_kind='task' THEN p_owner ELSE NULL END,
            p_delivery,p_producer) IS DISTINCT FROM true THEN
            RETURN pg_catalog.jsonb_build_object('schema','aap-assets-media-producer-binding/v1',
                'ownership','ordinary','run_id',NULL,'dataset_id',NULL,'item_id',NULL);
        END IF;
        RETURN v_binding;
    END IF;
    IF pg_catalog.jsonb_typeof(p_source) IS DISTINCT FROM 'object'
       OR p_source ->> 'schema' IS DISTINCT FROM 'aap-video-derivative-inputs/v1'
       OR (p_source - ARRAY['schema','outputs']) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(p_source -> 'outputs') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(p_source -> 'outputs')=0 THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_source_invalid';
    END IF;
    FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(p_source -> 'outputs') LOOP
        IF pg_catalog.jsonb_typeof(v_entry) IS DISTINCT FROM 'object'
           OR (v_entry - ARRAY['output_id','original']) <> '{}'::jsonb
           OR COALESCE(v_entry ->> 'output_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_source_invalid';
        END IF;
        SELECT output.dataset_item_id,output.original_ref INTO v_output
        FROM public.video_derivative_outputs AS output WHERE output.id=(v_entry ->> 'output_id')::uuid FOR SHARE;
        IF NOT FOUND OR v_output.original_ref IS DISTINCT FROM v_entry -> 'original' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_source_changed';
        END IF;
        IF v_output.dataset_item_id=p_owner THEN v_seen:=true; END IF;
        v_current := aap_assets_v1.media_source_binding('dataset_item',v_output.dataset_item_id,v_entry -> 'original');
        IF v_current ->> 'ownership'='fixture' AND aap_assets_v1.fixture_explicit_media_producer(
            (v_current ->> 'run_id')::uuid,(v_current ->> 'dataset_id')::uuid,(v_current ->> 'item_id')::uuid,
            NULL,p_delivery,p_producer) IS DISTINCT FROM true THEN
            IF EXISTS (SELECT 1 FROM aap_assets_v1.fixture_media_births AS owned
                       WHERE owned.owner_kind='video_output' AND owned.owner_id=(v_entry ->> 'output_id')::uuid) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_scope_untracked';
            END IF;
            v_current := pg_catalog.jsonb_build_object('schema','aap-assets-media-producer-binding/v1',
                'ownership','ordinary','run_id',NULL,'dataset_id',NULL,'item_id',NULL);
        END IF;
        IF v_binding IS NULL THEN v_binding:=v_current;
        ELSIF v_binding ->> 'ownership' IS DISTINCT FROM v_current ->> 'ownership'
           OR (v_current ->> 'ownership'='fixture' AND (v_current IS DISTINCT FROM v_binding
               OR v_output.dataset_item_id IS DISTINCT FROM p_owner
               OR v_entry -> 'original' IS DISTINCT FROM p_source -> 'outputs' -> 0 -> 'original')) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_scope_unknown';
        END IF;
    END LOOP;
    IF NOT v_seen OR (v_binding ->> 'ownership'='fixture' AND EXISTS (
        SELECT 1 FROM pg_catalog.jsonb_array_elements(p_source -> 'outputs') AS entry(value)
        JOIN public.video_derivative_outputs AS output ON output.id::text=entry.value ->> 'output_id'
        WHERE output.dataset_item_id<>p_owner
    )) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='media_producer_scope_unknown'; END IF;
    RETURN v_binding;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.media_producer_binding(text,uuid,text,uuid,jsonb) FROM PUBLIC;
