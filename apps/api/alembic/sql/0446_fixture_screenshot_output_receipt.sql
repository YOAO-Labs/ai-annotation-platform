-- Return only the exact currently published screenshot object and its actual
-- immutable readback receipt; the scoped proxy still performs physical GET.
CREATE FUNCTION aap_assets_v1.fixture_screenshot_output_receipt(
    p_run uuid,p_dataset uuid,p_item uuid,p_key text
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_result record;
BEGIN
    IF NOT COALESCE((SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')),false)
    THEN RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_private_owner_required'; END IF;
    IF p_run IS NULL OR p_dataset IS NULL OR p_item IS NULL OR p_key IS NULL OR p_key='' THEN RETURN NULL; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_run_resources AS dataset ON dataset.run_id=run.id AND dataset.kind='dataset'
      AND dataset.resource_id=p_dataset AND dataset.state='published'
    JOIN yoao_fixture_v1.fixture_run_resources AS item ON item.run_id=run.id AND item.kind='item'
      AND item.resource_id=p_item AND item.state='published'
    WHERE run.id=p_run AND run.parent_run_id IS NULL AND run.family='screenshot' AND run.state='open'
    FOR SHARE OF run,dataset,item;
    IF NOT FOUND THEN RETURN NULL; END IF;
    BEGIN
        SELECT * INTO STRICT v_result FROM (
            SELECT write.id AS record_id,'native'::text AS evidence_kind,write.creation_id,
                   creation.sha256 AS source_sha256,write.object_key,write.sha256,write.size_bytes,write.etag,write.version_id
            FROM public.dataset_items AS item
            JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=p_run
              AND resource.kind='creation' AND resource.item_id=item.id AND resource.state='published'
            JOIN public.managed_original_creations AS creation ON creation.id=resource.creation_id
              AND creation.dataset_id=p_dataset AND creation.status='verified' AND creation.settled_at IS NOT NULL
            JOIN aap_assets_v1.fixture_native_cache_writes AS write ON write.run_id=p_run
              AND write.dataset_id=p_dataset AND write.item_id=item.id AND write.creation_id=creation.id
              AND write.family='image' AND write.state='verified' AND write.object_key=p_key
            JOIN aap_assets_v1.fixture_native_media_jobs AS owned ON owned.family='image' AND owned.job_id=write.job_id
              AND owned.run_id=p_run AND owned.dataset_id=p_dataset AND owned.item_id=p_item
            JOIN public.asset_image_jobs AS job ON job.job_id=write.job_id AND job.kind='thumbnail'
              AND job.generation=write.job_generation AND job.last_lease_token=write.lease_token
              AND job.status='ready' AND job.last_outcome='published'
              AND job.result #>> '{artifact,key}'=p_key AND job.result #>> '{artifact,sha256}'=write.sha256
              AND job.result #>> '{artifact,byte_size}'=write.size_bytes::text
              AND job.result ->> 'source_sha256'=creation.sha256
              AND job.source_generation=item.media_source_generation
            JOIN aap_assets_v1.fixture_native_media_sources AS source ON source.source_operation_id=write.source_operation_id
              AND source.run_id=p_run AND source.dataset_id=p_dataset AND source.item_id=p_item
              AND source.creation_id=creation.id AND source.state='settled'
              AND source.family='image' AND source.job_id=job.job_id
              AND source.job_generation=job.generation AND source.lease_token=job.last_lease_token
              AND source.source_snapshot=job.source_snapshot
            WHERE item.id=p_item AND item.dataset_id=p_dataset AND item.thumbnail_path=p_key
              AND item.media_source_drifted IS FALSE
            UNION ALL
            SELECT write.id,'native'::text,creation.id,creation.sha256,write.object_key,
                   write.sha256,write.size_bytes,write.etag,write.version_id
            FROM public.dataset_items AS item
            JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=p_run
              AND resource.kind='creation' AND resource.item_id=item.id AND resource.state='published'
            JOIN public.managed_original_creations AS creation ON creation.id=resource.creation_id
              AND creation.dataset_id=p_dataset AND creation.status='verified' AND creation.settled_at IS NOT NULL
            JOIN public.dataset_assets AS asset ON asset.dataset_item_id=item.id AND asset.checksum_value=creation.sha256
              AND asset.size_bytes=creation.size_bytes AND asset.verification_state='verified'
            JOIN public.dataset_asset_locations AS location ON location.asset_id=asset.id
              AND location.creation_id=creation.id AND location.role='managed_original'
              AND location.reference_state='present' AND location.object_key=creation.object_key
              AND location.location_id=creation.location_id AND location.bucket=creation.bucket
            JOIN aap_assets_v1.fixture_native_cache_writes AS write ON write.run_id=p_run
              AND write.dataset_id=p_dataset AND write.item_id=item.id AND write.creation_id=creation.id
              AND write.etag IS NOT NULL AND write.etag<>'' AND write.version_id IS NOT NULL
              AND write.version_id NOT IN ('','null')
              AND write.family='media' AND write.state='verified' AND write.object_key=p_key
            JOIN aap_assets_v1.fixture_native_media_jobs AS owned ON owned.family='media'
              AND owned.job_id=write.job_id AND owned.run_id=p_run AND owned.dataset_id=p_dataset
              AND owned.item_id=p_item AND owned.job_kind='video_metadata'
              AND owned.owner_kind='dataset_item' AND owned.owner_id=p_item
            JOIN public.asset_media_jobs AS job ON job.job_id=write.job_id
              AND job.dataset_item_id=p_item AND job.kind='video_metadata'
              AND job.generation=write.job_generation AND job.last_lease_token=write.lease_token
              AND job.status='ready' AND job.last_outcome='published'
              AND job.source_generation=item.media_source_generation
            JOIN yoao_fixture_v1.fixture_native_media_work_intents AS intent
              ON intent.family='media' AND intent.run_id=p_run AND intent.dataset_id=p_dataset
              AND intent.job_id=job.job_id AND intent.generation=job.generation
              AND intent.state IN ('admitted','settled')
            JOIN aap_assets_v1.fixture_native_media_sources AS source
              ON source.source_operation_id=write.source_operation_id
              AND source.run_id=p_run AND source.dataset_id=p_dataset AND source.item_id=p_item
              AND source.creation_id=creation.id AND source.state='settled' AND source.family='media'
              AND source.job_id=job.job_id AND source.job_generation=job.generation
              AND source.lease_token=job.last_lease_token
              AND source.source_snapshot=aap_assets_v1._media_source_snapshot(item)
            WHERE item.id=p_item AND item.dataset_id=p_dataset AND item.file_type='video'
              AND item.thumbnail_path=p_key AND item.metadata #>> '{video,poster_frame_path}'=p_key
              AND item.media_source_drifted IS FALSE
              AND item.media_materialized_generation=item.media_source_generation
              AND p_key='media-assets/' || job.job_id::text || '/g' || job.generation::text
                  || '/a' || job.last_lease_token::text || '/poster.webp'
        ) AS receipt;
    EXCEPTION WHEN no_data_found THEN RETURN NULL;
    WHEN too_many_rows THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_screenshot_output_ambiguous';
    END;
    RETURN pg_catalog.jsonb_build_object('schema','aap-fixture-screenshot-output/v1',
        'run_id',p_run,'dataset_id',p_dataset,'item_id',p_item,'key',v_result.object_key,
        'source_creation_id',v_result.creation_id,'source_sha256',v_result.source_sha256,
        'record_id',v_result.record_id,'evidence_kind',v_result.evidence_kind,'sha256',v_result.sha256,
        'size_bytes',v_result.size_bytes,'etag',v_result.etag,'version_id',v_result.version_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_screenshot_output_receipt(uuid,uuid,uuid,text) FROM PUBLIC;
