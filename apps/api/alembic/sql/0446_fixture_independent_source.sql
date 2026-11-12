-- Jobs may bind a delivery only to the actual fixture subject, original and
-- producer envelope.  This validates identity; execution and bytes remain with
-- their respective owners.
CREATE FUNCTION aap_assets_v1.fixture_independent_source_matches(
    p_run uuid,p_dataset uuid,p_item uuid,p_owner_kind text,p_owner_id uuid,
    p_producer_kind text,p_source jsonb,p_envelope jsonb
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_args jsonb; v_kwargs jsonb; v_delegation jsonb; v_entry jsonb;
BEGIN
    IF p_run IS NULL OR p_owner_id IS NULL OR p_owner_kind NOT IN ('dataset_item','task')
       OR p_producer_kind NOT IN ('pyramid','thumbnail','task_thumbnail','video_metadata','video_derivatives')
       OR pg_catalog.jsonb_typeof(p_source) IS DISTINCT FROM 'object'
       OR (p_source - ARRAY['schema','owner_kind','content_id','sha256','size_bytes',
                            'location_id','location_revision','maintenance_revision','binding']) <> '{}'::jsonb
       OR NOT (p_source ?& ARRAY['schema','owner_kind','content_id','sha256','size_bytes',
                                  'location_id','location_revision','maintenance_revision','binding'])
       OR p_source -> 'schema' IS DISTINCT FROM '1'::jsonb
       OR p_source ->> 'owner_kind' NOT IN ('dataset_asset','task_original')
       OR COALESCE(p_source ->> 'content_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR COALESCE(p_source ->> 'location_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR COALESCE(p_source ->> 'sha256','') !~ '^[0-9a-f]{64}$'
       OR COALESCE(p_source ->> 'binding','') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(p_source -> 'size_bytes') IS DISTINCT FROM 'number'
       OR COALESCE(p_source ->> 'size_bytes','') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_source -> 'location_revision') IS DISTINCT FROM 'number'
       OR COALESCE(p_source ->> 'location_revision','') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_source -> 'maintenance_revision') IS DISTINCT FROM 'number'
       OR COALESCE(p_source ->> 'maintenance_revision','') !~ '^[1-9][0-9]*$'
       OR pg_catalog.jsonb_typeof(p_envelope) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(p_envelope -> 'args') IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_typeof(p_envelope -> 'kwargs') IS DISTINCT FROM 'object'
       THEN RETURN false; END IF;
    IF (p_dataset IS NULL) <> (p_item IS NULL) THEN RETURN false; END IF;
    IF p_item IS NULL THEN
        IF p_owner_kind<>'task' OR p_producer_kind NOT IN ('pyramid','task_thumbnail')
           OR p_source ->> 'owner_kind'<>'task_original' OR NOT EXISTS (
               SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
               JOIN aap_labeling_v1.task_rows AS task ON task.id=resource.resource_id
               JOIN public.task_originals AS original ON original.task_id=task.id AND original.project_id=task.project_id
               JOIN public.managed_original_creations AS creation ON creation.id=original.creation_id
               JOIN public.storage_locations AS location ON location.id=original.location_id
               JOIN public.storage_maintenance_gates AS gate ON gate.location_id=location.id
               WHERE resource.run_id=p_run AND resource.kind='task' AND resource.resource_id=p_owner_id
                 AND resource.state='published' AND task.dataset_item_id IS NULL
                 AND aap_labeling_v1.lock_task_original_pointer(task.id,task.file_path)=original.id
                 AND original.id::text=p_source ->> 'content_id' AND original.sha256=p_source ->> 'sha256'
                 AND original.size_bytes::text=p_source ->> 'size_bytes'
                 AND original.location_id::text=p_source ->> 'location_id' AND creation.status='verified'
                 AND location.config_revision::text=p_source ->> 'location_revision'
                 AND gate.revision::text=p_source ->> 'maintenance_revision'
           ) THEN RETURN false; END IF;
    ELSE
        IF p_source ->> 'owner_kind'<>'dataset_asset' OR NOT EXISTS (
            SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS dataset
            JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=dataset.run_id
            JOIN public.dataset_items AS item ON item.id=resource.resource_id AND item.dataset_id=dataset.resource_id
            JOIN public.dataset_assets AS asset ON asset.dataset_item_id=item.id
            JOIN public.dataset_asset_locations AS source ON source.asset_id=asset.id
            JOIN public.storage_locations AS location ON location.id=source.location_id
            JOIN public.storage_maintenance_gates AS gate ON gate.location_id=location.id
            WHERE dataset.run_id=p_run AND dataset.kind='dataset' AND dataset.resource_id=p_dataset
              AND dataset.state='published' AND resource.kind='item' AND resource.resource_id=p_item
              AND resource.state='published' AND asset.id::text=p_source ->> 'content_id'
              AND asset.verification_state='verified' AND asset.checksum_algorithm='sha256'
              AND asset.checksum_value=p_source ->> 'sha256' AND asset.size_bytes::text=p_source ->> 'size_bytes'
              AND source.role='managed_original' AND source.reference_state='present'
              AND source.location_id::text=p_source ->> 'location_id'
              AND location.config_revision::text=p_source ->> 'location_revision'
              AND gate.revision::text=p_source ->> 'maintenance_revision'
        ) THEN RETURN false; END IF;
        IF p_owner_kind='dataset_item' AND p_owner_id IS DISTINCT FROM p_item THEN RETURN false; END IF;
        IF p_owner_kind='task' AND (
            aap_assets_v1.media_source_binding(p_owner_kind,p_owner_id,p_source) IS DISTINCT FROM
            pg_catalog.jsonb_build_object(
                'schema','aap-assets-media-producer-binding/v1','ownership','fixture',
                'run_id',p_run,'dataset_id',p_dataset,'item_id',p_item
            )
        ) THEN RETURN false; END IF;
    END IF;
    v_args := p_envelope -> 'args';
    v_kwargs := p_envelope -> 'kwargs';
    IF p_producer_kind='pyramid' THEN
        IF pg_catalog.jsonb_array_length(v_args)<>4 OR v_args ->> 0 IS DISTINCT FROM p_owner_kind
           OR v_args ->> 1 IS DISTINCT FROM p_owner_id::text
           OR pg_catalog.jsonb_typeof(v_args -> 2) IS DISTINCT FROM 'boolean'
           OR v_kwargs <> '{}'::jsonb THEN RETURN false; END IF;
        v_delegation := v_args -> 3;
    ELSIF p_producer_kind IN ('thumbnail','task_thumbnail') THEN
        IF pg_catalog.jsonb_array_length(v_args)<>1 OR v_args ->> 0 IS DISTINCT FROM p_owner_id::text
           OR v_kwargs <> '{}'::jsonb OR (p_producer_kind='thumbnail' AND p_owner_kind<>'dataset_item')
           OR (p_producer_kind='task_thumbnail' AND p_owner_kind<>'task') THEN RETURN false; END IF;
        RETURN true;
    ELSIF p_producer_kind='video_metadata' THEN
        IF p_owner_kind<>'dataset_item' OR pg_catalog.jsonb_array_length(v_args)<>1
           OR v_args ->> 0 IS DISTINCT FROM p_item::text OR (v_kwargs - ARRAY['delegation']) <> '{}'::jsonb
           OR NOT (v_kwargs ? 'delegation') THEN RETURN false; END IF;
        v_delegation := v_kwargs -> 'delegation';
    ELSE
        IF p_owner_kind<>'dataset_item' OR pg_catalog.jsonb_array_length(v_args)<>1
           OR pg_catalog.jsonb_typeof(v_args -> 0) IS DISTINCT FROM 'array'
           OR pg_catalog.jsonb_array_length(v_args -> 0)=0 OR v_kwargs <> '{}'::jsonb THEN RETURN false; END IF;
        FOR v_entry IN SELECT value FROM pg_catalog.jsonb_array_elements(v_args -> 0) LOOP
            IF pg_catalog.jsonb_typeof(v_entry) IS DISTINCT FROM 'string' OR NOT EXISTS (
                SELECT 1 FROM public.video_derivative_outputs AS output
                WHERE output.id::text=v_entry #>> '{}' AND output.dataset_item_id=p_item
                  AND output.original_ref=p_source
            ) THEN RETURN false; END IF;
        END LOOP;
        RETURN true;
    END IF;
    RETURN pg_catalog.jsonb_typeof(v_delegation)='object'
       AND v_delegation ->> 'owner_kind'=p_owner_kind
       AND v_delegation ->> 'owner_id'=p_owner_id::text
       AND v_delegation -> 'original'=p_source;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_independent_source_matches(uuid,uuid,uuid,text,uuid,text,jsonb,jsonb) FROM PUBLIC;
