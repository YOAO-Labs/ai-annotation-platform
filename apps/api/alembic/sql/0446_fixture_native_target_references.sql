-- Check only this target's exact physical identity and business location.
CREATE FUNCTION aap_assets_v1.fixture_native_target_references_retired(p_run uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets AS target
        WHERE target.run_id=p_run
          AND NOT EXISTS (SELECT 1 FROM public.storage_connections AS connection
              WHERE connection.id<>target.connection_id AND connection.config ->> 'endpoint'=target.endpoint
                AND connection.config ->> 'bucket'=target.bucket)
          AND NOT EXISTS (SELECT 1 FROM public.storage_bindings WHERE location_id=target.location_id)
          AND NOT EXISTS (SELECT 1 FROM public.dataset_asset_locations WHERE location_id=target.location_id)
          AND NOT EXISTS (SELECT 1 FROM public.managed_original_creations
              WHERE location_id=target.location_id OR storage_identity=target.storage_identity)
          AND NOT EXISTS (SELECT 1 FROM public.task_originals
              WHERE location_id=target.location_id OR storage_identity=target.storage_identity)
          AND NOT EXISTS (SELECT 1 FROM public.managed_original_retirements WHERE location_id=target.location_id)
          AND NOT EXISTS (
              SELECT 1 FROM public.thumbnail_outputs AS output
              WHERE (output.storage_ref ->> 'location_id'=target.location_id::text
                  OR output.storage_ref ->> 'storage_identity'=target.storage_identity
                  OR (output.storage_ref ->> 'endpoint_url'=target.endpoint AND output.storage_ref ->> 'bucket'=target.bucket))
                AND (output.state<>'deleted' OR output.storage_ref ->> 'location_id' IS DISTINCT FROM target.location_id::text
                  OR output.storage_ref ->> 'storage_identity' IS DISTINCT FROM target.storage_identity
                  OR output.storage_ref ->> 'endpoint_url' IS DISTINCT FROM target.endpoint
                  OR output.storage_ref ->> 'bucket' IS DISTINCT FROM target.bucket
                  OR NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_media_births AS birth
                      WHERE birth.owner_kind='thumbnail' AND birth.owner_id=output.id AND birth.run_id=p_run))
          ) AND NOT EXISTS (
              SELECT 1 FROM public.image_pyramid_outputs AS output
              WHERE (output.storage_ref ->> 'location_id'=target.location_id::text
                  OR output.storage_ref ->> 'storage_identity'=target.storage_identity
                  OR (output.storage_ref ->> 'endpoint_url'=target.endpoint AND output.storage_ref ->> 'bucket'=target.bucket))
                AND (output.state<>'deleted' OR output.storage_ref ->> 'location_id' IS DISTINCT FROM target.location_id::text
                  OR output.storage_ref ->> 'storage_identity' IS DISTINCT FROM target.storage_identity
                  OR output.storage_ref ->> 'endpoint_url' IS DISTINCT FROM target.endpoint
                  OR output.storage_ref ->> 'bucket' IS DISTINCT FROM target.bucket
                  OR NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_media_births AS birth
                      WHERE birth.owner_kind='pyramid_output' AND birth.owner_id=output.id AND birth.run_id=p_run))
          ) AND NOT EXISTS (
              SELECT 1 FROM public.video_derivative_outputs AS output
              WHERE (output.storage_ref ->> 'location_id'=target.location_id::text
                  OR output.storage_ref ->> 'storage_identity'=target.storage_identity
                  OR (output.storage_ref ->> 'endpoint_url'=target.endpoint AND output.storage_ref ->> 'bucket'=target.bucket))
                AND (output.state<>'deleted' OR output.storage_ref ->> 'location_id' IS DISTINCT FROM target.location_id::text
                  OR output.storage_ref ->> 'storage_identity' IS DISTINCT FROM target.storage_identity
                  OR output.storage_ref ->> 'endpoint_url' IS DISTINCT FROM target.endpoint
                  OR output.storage_ref ->> 'bucket' IS DISTINCT FROM target.bucket
                  OR NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_media_births AS birth
                      WHERE birth.owner_kind='video_output' AND birth.owner_id=output.id AND birth.run_id=p_run))
          )
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_target_references_retired(uuid) FROM PUBLIC;
