-- Derived from native births, executions, readers and individual physical
-- version receipts; no caller can write an aggregate retirement flag.
CREATE FUNCTION aap_assets_v1.fixture_dataset_media_retired(p_run uuid,p_dataset uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()),4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture media proof requires a test database';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id=run.id
        JOIN public.datasets AS dataset ON dataset.id=resource.resource_id
        WHERE run.id=p_run AND ((run.parent_run_id IS NOT NULL AND run.family='owned')
            OR (run.parent_run_id IS NULL AND run.family IN ('screenshot','scale')))
          AND run.state='sealing' AND resource.kind='dataset' AND resource.state='published'
          AND resource.resource_id=p_dataset
    ) THEN RETURN NULL; END IF;
    IF NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets WHERE run_id=p_run) THEN
        RETURN EXISTS (
            SELECT 1 FROM aap_assets_v1.fixture_run_no_media_birth_certificates AS certificate
            WHERE certificate.run_id=p_run
        );
    END IF;
    IF aap_assets_v1.fixture_dataset_media_producers_quiescent(p_run,p_dataset) IS DISTINCT FROM true THEN
        RETURN false;
    END IF;
    IF aap_assets_v1.fixture_native_outputs_retired(p_run,p_dataset) IS DISTINCT FROM true THEN
        RETURN false;
    END IF;
    IF aap_assets_v1.fixture_independent_deliveries_retired(p_run,p_dataset) IS DISTINCT FROM true THEN
        RETURN false;
    END IF;
    IF aap_assets_v1.fixture_dataset_legacy_original_readers_retired(p_run,p_dataset) IS DISTINCT FROM true THEN
        RETURN false;
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_external_media_sources AS source
        WHERE source.run_id=p_run AND source.dataset_id=p_dataset AND source.state<>'settled'
    ) THEN RETURN false; END IF;
    IF EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS source
        LEFT JOIN public.dataset_items AS item ON item.id=source.item_id
        LEFT JOIN public.managed_original_creations AS creation ON creation.id=source.creation_id
        LEFT JOIN public.storage_operations AS operation ON operation.location_id=creation.location_id
            AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
            AND operation.node_id='aap-assets-media'
        WHERE source.run_id=p_run AND source.kind='creation'
          AND (item.dataset_id=p_dataset OR item.id IS NULL)
          AND (item.id IS NULL OR creation.id IS NULL OR operation.id IS NOT NULL)
    ) THEN RETURN false; END IF;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_reads AS reader
        WHERE reader.run_id=p_run AND reader.dataset_id=p_dataset AND reader.state<>'settled'
    ) THEN RETURN false; END IF;
    -- Each fixture-born native Job was retired by its actual Assets owner,
    -- after exact execution and per-version proof.  No Job status is evidence.
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_jobs AS birth
        LEFT JOIN aap_assets_v1.fixture_native_job_retirements AS retired
          ON retired.family=birth.family AND retired.job_id=birth.job_id
         AND retired.run_id=birth.run_id AND retired.dataset_id=birth.dataset_id
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset AND (
            retired.job_id IS NULL OR
            (birth.family='image' AND EXISTS (SELECT 1 FROM public.asset_image_jobs AS job WHERE job.job_id=birth.job_id)) OR
            (birth.family='media' AND EXISTS (SELECT 1 FROM public.asset_media_jobs AS job WHERE job.job_id=birth.job_id))
        )
    ) THEN RETURN false; END IF;
    IF EXISTS (
        SELECT 1 FROM public.asset_image_jobs AS job
        WHERE ((job.owner_kind='dataset_item' AND EXISTS (
                   SELECT 1 FROM public.dataset_items AS item
                   WHERE item.id=job.owner_id AND item.dataset_id=p_dataset))
           OR (job.owner_kind='task' AND EXISTS (
                   SELECT 1 FROM aap_labeling_v1.task_rows AS task
                   JOIN public.dataset_items AS item ON item.id=task.dataset_item_id
                   WHERE task.id=job.owner_id AND item.dataset_id=p_dataset))
           OR (job.kind='image_backfill' AND job.spec #>> '{selection,dataset_id}'=p_dataset::text))
          AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_external_media_jobs AS external
              WHERE external.family='image' AND external.job_id=job.job_id
                AND external.run_id=p_run AND external.dataset_id=p_dataset)
    ) THEN RETURN false; END IF;
    IF EXISTS (
        SELECT 1 FROM public.asset_media_jobs AS job
        WHERE ((job.dataset_item_id IS NOT NULL AND EXISTS (
                   SELECT 1 FROM public.dataset_items AS item
                   WHERE item.id=job.dataset_item_id AND item.dataset_id=p_dataset))
           OR (job.kind='video_backfill' AND job.backfill_selection ->> 'dataset_id'=p_dataset::text))
          AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_external_media_jobs AS external
              WHERE external.family='media' AND external.job_id=job.job_id
                AND external.run_id=p_run AND external.dataset_id=p_dataset)
    ) THEN RETURN false; END IF;
    -- A staged pre-existing global Image page is not a retirement proof.
    -- New global scans exclude fixture sources before snapshot and pagination.
    IF EXISTS (
        SELECT 1 FROM public.asset_image_jobs AS job
        WHERE job.kind='image_backfill' AND job.progress ? 'pending_batch'
          AND (job.spec #>> '{selection,all_owners}'='true'
            OR EXISTS (SELECT 1 FROM public.project_datasets AS relation
                       WHERE relation.dataset_id=p_dataset
                         AND relation.project_id::text=job.spec #>> '{selection,project_id}')
            OR EXISTS (SELECT 1 FROM public.dataset_items AS item WHERE item.dataset_id=p_dataset
                       AND (job.spec #> '{selection,owner_ids}') ? item.id::text)
            OR EXISTS (SELECT 1 FROM aap_labeling_v1.task_rows AS task
                       JOIN public.dataset_items AS item ON item.id=task.dataset_item_id
                       WHERE item.dataset_id=p_dataset
                         AND (job.spec #> '{selection,owner_ids}') ? task.id::text)
            OR job.spec #>> '{selection,dataset_id}'=p_dataset::text)
          AND (pg_catalog.jsonb_typeof(job.progress #> '{pending_batch,items}') IS DISTINCT FROM 'array'
            OR EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_array_elements(
                    CASE WHEN pg_catalog.jsonb_typeof(job.progress #> '{pending_batch,items}')='array'
                         THEN job.progress #> '{pending_batch,items}' ELSE '[]'::jsonb END
                ) AS candidate(value)
                WHERE candidate.value #>> '{owner,kind}' NOT IN ('task','dataset_item')
                   OR candidate.value #>> '{owner,id}' IS NULL
                   OR EXISTS (SELECT 1 FROM public.dataset_items AS item
                              WHERE item.dataset_id=p_dataset
                                AND item.id::text=candidate.value #>> '{owner,id}')
                   OR EXISTS (SELECT 1 FROM aap_labeling_v1.task_rows AS task
                              JOIN public.dataset_items AS item ON item.id=task.dataset_item_id
                              WHERE item.dataset_id=p_dataset
                                AND task.id::text=candidate.value #>> '{owner,id}')
            ))
    ) THEN RETURN false; END IF;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_births AS birth
        LEFT JOIN public.image_pyramid_assets AS asset
          ON birth.owner_kind='pyramid_asset' AND asset.id=birth.owner_id
        LEFT JOIN public.image_pyramid_generations AS generation
          ON birth.owner_kind='pyramid_generation' AND generation.asset_id=birth.owner_id
         AND generation.generation=birth.generation
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset
          AND ((birth.owner_kind='pyramid_asset' AND asset.id IS NULL)
            OR (birth.owner_kind='pyramid_generation' AND (generation.asset_id IS NULL
                OR aap_assets_v1.fixture_independent_pyramid_bindings_retired(
                    p_run,p_dataset,birth.owner_id,birth.generation
                ) IS DISTINCT FROM true
                OR (
                    aap_jobs_v1.fixture_pyramid_attempts_retired(
                        p_run,p_dataset,birth.owner_id,birth.generation,generation.attempts
                    ) IS DISTINCT FROM true
                    AND aap_assets_v1.fixture_native_pyramid_generation_retired(
                        p_run,p_dataset,birth.owner_id,birth.generation
                    ) IS DISTINCT FROM true
                    AND aap_assets_v1.fixture_independent_pyramid_generation_retired(
                        p_run,p_dataset,birth.owner_id,birth.generation,generation.attempts
                    ) IS DISTINCT FROM true
                ))))
    ) THEN RETURN false; END IF;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_births AS birth
        LEFT JOIN public.thumbnail_outputs AS thumbnail
          ON birth.owner_kind='thumbnail' AND thumbnail.id=birth.owner_id
        LEFT JOIN public.image_pyramid_outputs AS pyramid
          ON birth.owner_kind='pyramid_output' AND pyramid.id=birth.owner_id
        LEFT JOIN public.video_derivative_outputs AS video
          ON birth.owner_kind='video_output' AND video.id=birth.owner_id
        LEFT JOIN aap_assets_v1.fixture_video_output_anchors AS anchor
          ON birth.owner_kind='video_output' AND anchor.output_id=birth.owner_id
         AND anchor.run_id=p_run AND anchor.dataset_id=p_dataset AND anchor.item_id=birth.item_id
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset
          AND birth.owner_kind IN ('thumbnail','pyramid_output','video_output')
          AND (
            (birth.owner_kind='thumbnail' AND (thumbnail.id IS NULL OR thumbnail.state <> 'deleted'
                OR thumbnail.uncertain_write OR (thumbnail.sha256 IS NULL
                    AND thumbnail.error_code IS DISTINCT FROM 'thumbnail_write_rejected')))
            OR (birth.owner_kind='pyramid_output' AND (pyramid.id IS NULL OR pyramid.state <> 'deleted'))
            OR (birth.owner_kind='video_output' AND (
                (video.id IS NULL AND anchor.output_id IS NULL)
                OR COALESCE(video.state,anchor.snapshot ->> 'state') IS DISTINCT FROM 'deleted'
                OR COALESCE(video.lease_token,(anchor.snapshot ->> 'lease_token')::uuid) IS NULL
                OR (
                    aap_jobs_v1.fixture_video_attempt_retired(
                        p_run,p_dataset,birth.owner_id,COALESCE(video.lease_token,(anchor.snapshot ->> 'lease_token')::uuid)
                    ) IS DISTINCT FROM true
                    AND aap_assets_v1.fixture_independent_video_attempt_retired(
                        p_run,p_dataset,birth.owner_id,COALESCE(video.lease_token,(anchor.snapshot ->> 'lease_token')::uuid)
                    ) IS DISTINCT FROM true
                )
            ))
            OR EXISTS (
                SELECT 1 FROM public.storage_operations AS operation
                WHERE operation.purpose=(CASE birth.owner_kind
                    WHEN 'thumbnail' THEN 'thumbnail_'
                    WHEN 'pyramid_output' THEN 'image_pyramid_'
                    ELSE 'video_derivative_' END) || pg_catalog.replace(birth.owner_id::text,'-','')
            )
            OR EXISTS (SELECT 1 FROM aap_assets_v1.media_native_stages AS stage
                       WHERE stage.output_kind=birth.owner_kind AND stage.output_id=birth.owner_id
                         AND stage.state <> 'settled')
            OR EXISTS (
                SELECT 1 FROM aap_assets_v1.fixture_media_versions AS version
                LEFT JOIN aap_assets_v1.fixture_media_version_retirements AS retirement
                  ON retirement.version_id=version.id
                WHERE version.run_id=p_run AND version.dataset_id=p_dataset
                  AND version.owner_kind=birth.owner_kind AND version.owner_id=birth.owner_id
                  AND (version.state <> 'verified' OR retirement.state IS DISTINCT FROM 'absent')
            )
            OR (birth.owner_kind='thumbnail' AND thumbnail.sha256 IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM aap_assets_v1.fixture_media_versions AS version
                WHERE version.run_id=p_run AND version.dataset_id=p_dataset
                  AND version.owner_kind='thumbnail' AND version.owner_id=birth.owner_id
                  AND version.sha256=thumbnail.sha256 AND version.size_bytes=thumbnail.size_bytes
                  AND version.etag=thumbnail.etag
                  AND NULLIF(version.version_id,'null') IS NOT DISTINCT FROM thumbnail.version_id
            ))
            OR (birth.owner_kind IN ('pyramid_output','video_output') AND NOT EXISTS (
                SELECT 1 FROM aap_assets_v1.fixture_media_versions AS version
                WHERE version.run_id=p_run AND version.dataset_id=p_dataset
                  AND version.owner_kind=birth.owner_kind AND version.owner_id=birth.owner_id
            ))
          )
    ) THEN RETURN false; END IF;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_births AS birth
        JOIN LATERAL (
            SELECT output.inventory FROM public.image_pyramid_outputs AS output
            WHERE birth.owner_kind='pyramid_output' AND output.id=birth.owner_id
            UNION ALL
            SELECT output.inventory FROM public.video_derivative_outputs AS output
            WHERE birth.owner_kind='video_output' AND output.id=birth.owner_id
            UNION ALL
            SELECT anchor.snapshot -> 'inventory' FROM aap_assets_v1.fixture_video_output_anchors AS anchor
            WHERE birth.owner_kind='video_output' AND anchor.output_id=birth.owner_id
              AND anchor.run_id=p_run AND anchor.dataset_id=p_dataset AND anchor.item_id=birth.item_id
        ) AS saved ON true
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset AND saved.inventory IS NOT NULL
          AND (pg_catalog.jsonb_typeof(saved.inventory -> 'objects') IS DISTINCT FROM 'object'
            OR EXISTS (
                SELECT 1 FROM pg_catalog.jsonb_each(
                    CASE WHEN pg_catalog.jsonb_typeof(saved.inventory -> 'objects')='object'
                         THEN saved.inventory -> 'objects' ELSE '{}'::jsonb END
                ) AS object(key,receipt)
                WHERE NOT EXISTS (
                    SELECT 1 FROM aap_assets_v1.fixture_media_versions AS version
                    WHERE version.run_id=p_run AND version.dataset_id=p_dataset
                      AND version.owner_kind=birth.owner_kind AND version.owner_id=birth.owner_id
                      AND version.object_key=object.key AND version.sha256=object.receipt ->> 'sha256'
                      AND pg_catalog.to_jsonb(version.size_bytes)=object.receipt -> 'size_bytes'
                )
            )
            OR EXISTS (
                SELECT 1 FROM aap_assets_v1.fixture_media_versions AS version
                WHERE version.run_id=p_run AND version.dataset_id=p_dataset
                  AND version.owner_kind=birth.owner_kind AND version.owner_id=birth.owner_id
                  AND NOT (saved.inventory -> 'objects' ? version.object_key)
            ))
    ) THEN RETURN false; END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_dataset_media_retired(uuid,uuid) FROM PUBLIC;
-- The per-run coordinator cannot query another run's proof.
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_media_retirement_complete(p_run uuid,p_dataset uuid,p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,p_token) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_media_principal_required';
    END IF;
    RETURN aap_assets_v1.fixture_dataset_media_retired(p_run,p_dataset);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_media_retirement_complete(uuid,uuid,uuid) FROM PUBLIC;
