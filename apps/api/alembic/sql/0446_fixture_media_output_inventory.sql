-- Enumerate only owners actually born in the sealed Dataset.  The owner
-- receives immutable identities, not an aggregate assertion of completion.
CREATE FUNCTION aap_assets_v1.fixture_media_retirement_inventory(p_run uuid,p_dataset uuid,p_token uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_result jsonb;
BEGIN
    IF aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,p_token) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_media_principal_required';
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'kind',owner_kind,'id',owner_id,'item_id',item_id) ORDER BY owner_kind,owner_id), '[]'::jsonb)
    INTO v_result FROM aap_assets_v1.fixture_media_births
    WHERE run_id=p_run AND dataset_id=p_dataset AND generation=0
      AND owner_kind IN ('thumbnail','pyramid_output','video_output');
    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_media_retirement_inventory(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_media_output_inventory(
    p_run uuid,p_dataset uuid,p_token uuid,p_kind text,p_owner uuid
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_media_births%ROWTYPE; v_output jsonb; v_versions jsonb;
    v_purpose text; v_asset uuid; v_generation integer; v_attempts integer; v_lease uuid;
BEGIN
    IF aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,p_token) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_media_principal_required';
    END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_media_births
    WHERE owner_kind=p_kind AND owner_id=p_owner AND generation=0
      AND run_id=p_run AND dataset_id=p_dataset FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_owner_missing'; END IF;
    IF aap_assets_v1.fixture_dataset_legacy_original_readers_retired(p_run,p_dataset) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_source_unsettled';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS source
        JOIN public.managed_original_creations AS creation ON creation.id=source.creation_id
        WHERE source.run_id=p_run AND source.kind='creation' AND source.item_id=v_birth.item_id
          AND creation.dataset_id=p_dataset
    ) OR EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS source
        LEFT JOIN public.managed_original_creations AS creation ON creation.id=source.creation_id
        LEFT JOIN public.storage_operations AS operation ON operation.location_id=creation.location_id
          AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
          AND operation.node_id='aap-assets-media'
        WHERE source.run_id=p_run AND source.kind='creation' AND source.item_id=v_birth.item_id
          AND (creation.id IS NULL OR creation.dataset_id IS DISTINCT FROM p_dataset OR operation.id IS NOT NULL)
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_source_unsettled'; END IF;
    IF p_kind='thumbnail' THEN
        SELECT pg_catalog.jsonb_build_object(
            'state',output.state,'storage_ref',output.storage_ref,
            'uncertain_write',output.uncertain_write,'sha256',output.sha256,
            'size_bytes',output.size_bytes,'etag',output.etag,'version_id',output.version_id,
            'error_code',output.error_code
        ) INTO v_output FROM public.thumbnail_outputs AS output WHERE id=p_owner FOR SHARE;
        v_purpose := 'thumbnail_' || pg_catalog.replace(p_owner::text,'-','');
        IF EXISTS (SELECT 1 FROM public.dataset_items WHERE thumbnail_output_id=p_owner AND id<>v_birth.item_id)
           OR EXISTS (SELECT 1 FROM public.dataset_source_previews WHERE thumbnail_output_id=p_owner)
           OR EXISTS (
               SELECT 1 FROM aap_labeling_v1.task_rows AS task
               LEFT JOIN yoao_fixture_v1.fixture_run_resources AS owned
                 ON owned.run_id=p_run AND owned.kind='task' AND owned.resource_id=task.id AND owned.state='published'
               WHERE task.thumbnail_output_id=p_owner
                 AND (task.dataset_item_id IS DISTINCT FROM v_birth.item_id OR owned.resource_id IS NULL)
           ) OR (v_output ->> 'uncertain_write')::boolean IS DISTINCT FROM false THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_output_unsettled';
        END IF;
    ELSIF p_kind='pyramid_output' THEN
        SELECT pg_catalog.jsonb_build_object('state',output.state,'storage_ref',output.storage_ref),
               output.asset_id,output.generation
        INTO v_output,v_asset,v_generation FROM public.image_pyramid_outputs AS output WHERE id=p_owner FOR SHARE;
        SELECT attempts INTO v_attempts FROM public.image_pyramid_generations
        WHERE asset_id=v_asset AND generation=v_generation;
        IF NOT FOUND OR aap_assets_v1.fixture_independent_pyramid_bindings_retired(
            p_run,p_dataset,v_asset,v_generation) IS DISTINCT FROM true
           OR (aap_jobs_v1.fixture_pyramid_attempts_retired(
                p_run,p_dataset,v_asset,v_generation,v_attempts) IS DISTINCT FROM true
               AND aap_assets_v1.fixture_native_pyramid_generation_retired(
                p_run,p_dataset,v_asset,v_generation) IS DISTINCT FROM true
               AND aap_assets_v1.fixture_independent_pyramid_generation_retired(
                p_run,p_dataset,v_asset,v_generation,v_attempts) IS DISTINCT FROM true)
           OR EXISTS (SELECT 1 FROM public.image_pyramid_generations AS other
                      WHERE other.output_id=p_owner AND (other.asset_id,other.generation)
                            IS DISTINCT FROM (v_asset,v_generation)) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_output_unsettled';
        END IF;
        v_purpose := 'image_pyramid_' || pg_catalog.replace(p_owner::text,'-','');
    ELSIF p_kind='video_output' THEN
        SELECT pg_catalog.jsonb_build_object('state',output.state,'storage_ref',output.storage_ref),
               output.lease_token INTO v_output,v_lease
        FROM public.video_derivative_outputs AS output WHERE id=p_owner FOR SHARE;
        IF NOT FOUND OR v_lease IS NULL OR (
            aap_jobs_v1.fixture_video_attempt_retired(p_run,p_dataset,p_owner,v_lease) IS DISTINCT FROM true
            AND aap_assets_v1.fixture_independent_video_attempt_retired(
                p_run,p_dataset,p_owner,v_lease) IS DISTINCT FROM true
        )
           OR EXISTS (SELECT 1 FROM public.dataset_items AS item
                      WHERE (item.video_output_id=p_owner OR item.video_building_output_id=p_owner)
                        AND item.id<>v_birth.item_id)
           OR EXISTS (SELECT 1 FROM public.video_frame_cache AS frame
                      WHERE frame.output_id=p_owner AND frame.dataset_item_id<>v_birth.item_id)
           OR EXISTS (SELECT 1 FROM public.video_chunks AS chunk
                      WHERE chunk.output_id=p_owner AND chunk.dataset_item_id<>v_birth.item_id) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_output_unsettled';
        END IF;
        v_purpose := 'video_derivative_' || pg_catalog.replace(p_owner::text,'-','');
    ELSE
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_media_owner_invalid';
    END IF;
    IF v_output IS NULL OR EXISTS (SELECT 1 FROM public.storage_operations WHERE purpose=v_purpose)
       OR EXISTS (SELECT 1 FROM aap_assets_v1.media_native_stages
                  WHERE output_kind=p_kind AND output_id=p_owner AND state<>'settled')
       OR EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_native_cache_reads AS reader
           JOIN aap_assets_v1.fixture_media_versions AS version ON version.id=reader.write_id
           WHERE reader.run_id=p_run AND reader.dataset_id=p_dataset AND reader.evidence_kind='legacy'
             AND reader.state<>'settled' AND version.owner_kind=p_kind AND version.owner_id=p_owner
       )
       OR EXISTS (SELECT 1 FROM aap_assets_v1.fixture_media_versions
                  WHERE owner_kind=p_kind AND owner_id=p_owner AND
                    (run_id<>p_run OR dataset_id<>p_dataset OR state<>'verified')) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_output_unsettled';
    END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(id ORDER BY id),'[]'::jsonb) INTO v_versions
    FROM aap_assets_v1.fixture_media_versions
    WHERE run_id=p_run AND dataset_id=p_dataset AND owner_kind=p_kind AND owner_id=p_owner;
    IF pg_catalog.jsonb_array_length(v_versions)=0 AND NOT (
        p_kind='thumbnail' AND v_output ->> 'sha256' IS NULL
          AND v_output ->> 'error_code'='thumbnail_write_rejected'
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_version_untracked'; END IF;
    RETURN pg_catalog.jsonb_build_object('kind',p_kind,'id',p_owner,'item_id',v_birth.item_id,
        'state',v_output ->> 'state','storage_ref',v_output -> 'storage_ref','versions',v_versions);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_media_output_inventory(uuid,uuid,uuid,text,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_media_output_retirement_done(
    p_run uuid,p_dataset uuid,p_token uuid,p_kind text,p_owner uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_manifest jsonb;
BEGIN
    v_manifest := aap_assets_v1.fixture_media_output_inventory(p_run,p_dataset,p_token,p_kind,p_owner);
    IF v_manifest ->> 'state' NOT IN ('retiring','deleted') THEN RETURN false; END IF;
    RETURN NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_versions AS version
        LEFT JOIN aap_assets_v1.fixture_media_version_retirements AS retirement
          ON retirement.version_id=version.id
        WHERE version.run_id=p_run AND version.dataset_id=p_dataset
          AND version.owner_kind=p_kind AND version.owner_id=p_owner
          AND retirement.state IS DISTINCT FROM 'absent'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_media_output_retirement_done(uuid,uuid,uuid,text,uuid) FROM PUBLIC;
