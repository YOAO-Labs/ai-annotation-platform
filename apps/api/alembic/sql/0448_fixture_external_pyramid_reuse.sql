-- A fixture consumer can reuse an ordinary native Images generation, but it
-- does not own its lease, source permit, output, or subsequent attempts.
CREATE TABLE aap_assets_v1.fixture_external_pyramid_reuses (
    consumer_kind text NOT NULL CHECK (consumer_kind IN ('child','independent')),
    consumer_id uuid NOT NULL,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation>0),
    job_id uuid NOT NULL,
    job_generation bigint NOT NULL CHECK (job_generation>0),
    lease_token uuid NOT NULL,
    source_operation_id uuid NOT NULL REFERENCES aap_assets_v1.fixture_external_media_sources(source_operation_id),
    PRIMARY KEY (consumer_kind,consumer_id)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_external_pyramid_reuses FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_external_pyramid_reuse_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_external_pyramid_reuses FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
-- Called only by the existing Jobs/Assets binding capabilities, in their
-- transaction and with their real executor session and token.
CREATE FUNCTION aap_assets_v1.record_fixture_external_pyramid_reuse(
    p_kind text,p_consumer uuid,p_token uuid,p_asset uuid,p_generation integer
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_run uuid; v_dataset uuid; v_item uuid; v_owner_kind text; v_owner uuid;
    v_source jsonb; v_match record; v_existing aap_assets_v1.fixture_external_pyramid_reuses%ROWTYPE;
BEGIN
    IF p_kind='child' THEN
        SELECT child.run_id,child.dataset_id,child.item_id,'dataset_item',child.item_id,
               child.delegation -> 'original'
        INTO v_run,v_dataset,v_item,v_owner_kind,v_owner,v_source
        FROM aap_jobs_v1.fixture_media_children AS child
        WHERE child.child_id=p_consumer
          AND (child.asset_id IS NULL OR (child.asset_id,child.generation)=(p_asset,p_generation))
          AND aap_jobs_v1.fixture_media_session_authorized(
              'pyramid',child.child_id,p_token,child.run_id,child.dataset_id,child.item_id);
    ELSIF p_kind='independent' THEN
        SELECT birth.run_id,birth.dataset_id,birth.item_id,birth.owner_kind,birth.owner_id,birth.source
        INTO v_run,v_dataset,v_item,v_owner_kind,v_owner,v_source
        FROM aap_assets_v1.fixture_independent_media_births AS birth
        WHERE birth.delivery_id=p_consumer AND birth.producer_kind='pyramid'
          AND NOT EXISTS (
              SELECT 1 FROM aap_assets_v1.fixture_independent_pyramid_bindings AS previous
              WHERE previous.delivery_id=p_consumer
                AND (previous.asset_id,previous.generation) IS DISTINCT FROM (p_asset,p_generation)
          )
          AND aap_assets_v1.fixture_independent_session_authorized(
              birth.delivery_id,p_token,birth.run_id,birth.dataset_id,birth.item_id,
              CASE WHEN birth.dataset_id IS NULL THEN birth.owner_id ELSE NULL END);
    ELSE
        RETURN false;
    END IF;
    IF NOT FOUND OR v_dataset IS NULL OR v_item IS NULL OR p_asset IS NULL OR p_generation<1
       OR aap_assets_v1.media_source_binding('dataset_item',v_item,v_source)
          IS DISTINCT FROM pg_catalog.jsonb_build_object(
              'schema','aap-assets-media-producer-binding/v1','ownership','fixture',
              'run_id',v_run,'dataset_id',v_dataset,'item_id',v_item)
       OR EXISTS (SELECT 1 FROM aap_assets_v1.fixture_media_births AS owned
                  WHERE owned.owner_kind='pyramid_generation' AND owned.owner_id=p_asset
                    AND owned.generation=p_generation) THEN RETURN false; END IF;

    SELECT * INTO v_existing FROM aap_assets_v1.fixture_external_pyramid_reuses
    WHERE consumer_kind=p_kind AND consumer_id=p_consumer;
    IF FOUND THEN
        RETURN (v_existing.run_id,v_existing.dataset_id,v_existing.item_id,v_existing.asset_id,v_existing.generation)
            IS NOT DISTINCT FROM (v_run,v_dataset,v_item,p_asset,p_generation);
    END IF;

    BEGIN
        SELECT source.job_id,source.job_generation,source.lease_token,source.source_operation_id
        INTO STRICT v_match
        FROM public.image_pyramid_assets AS asset
        JOIN public.image_pyramid_generations AS generation ON generation.asset_id=asset.id
        JOIN public.asset_image_jobs AS job ON job.job_id=generation.job_id
          AND job.generation=generation.job_generation AND job.kind='pyramid'
          AND job.pyramid_asset_id=asset.id AND job.pyramid_generation=generation.generation
          AND job.source_generation=generation.source_generation AND job.source_snapshot=generation.source_snapshot
          AND job.lease_token=generation.lease_token
        JOIN aap_assets_v1.fixture_external_media_jobs AS ordinary
          ON ordinary.family='image' AND ordinary.job_id=job.job_id AND ordinary.run_id=v_run
         AND ordinary.dataset_id=v_dataset AND ordinary.item_id=v_item
         AND ordinary.owner_kind=job.owner_kind AND ordinary.owner_id=job.owner_id
        JOIN aap_assets_v1.fixture_external_media_sources AS source
          ON source.family='image' AND source.job_id=job.job_id AND source.job_generation=job.generation
         AND source.lease_token=job.lease_token AND source.source_snapshot=job.source_snapshot
         AND source.run_id=v_run AND source.dataset_id=v_dataset AND source.item_id=v_item AND source.state='open'
        JOIN public.managed_original_creations AS creation ON creation.id=source.creation_id
        JOIN public.dataset_assets AS original ON original.id::text=v_source ->> 'content_id'
          AND original.dataset_item_id=v_item AND original.checksum_algorithm='sha256'
          AND original.verification_state='verified' AND original.checksum_value=creation.sha256
          AND original.size_bytes=creation.size_bytes
        JOIN public.dataset_asset_locations AS location ON location.asset_id=original.id
          AND location.creation_id=creation.id AND location.location_id=creation.location_id
          AND location.object_key=creation.object_key AND location.bucket=creation.bucket
          AND location.role='managed_original' AND location.reference_state='present'
        JOIN public.storage_operations AS operation ON operation.id=source.source_operation_id
          AND operation.location_id=creation.location_id AND operation.node_id='aap-assets-media'
          AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
        WHERE asset.id=p_asset AND generation.generation=p_generation AND generation.status='building'
          AND job.status='running' AND job.lease_token IS NOT NULL
          AND ((v_owner_kind='dataset_item' AND asset.dataset_item_id=v_item AND asset.task_id IS NULL
                AND job.owner_kind='dataset_item' AND job.owner_id=v_item)
            OR (v_owner_kind='task' AND asset.task_id=v_owner AND asset.dataset_item_id IS NULL
                AND job.owner_kind='task' AND job.owner_id=v_owner));
    EXCEPTION WHEN no_data_found OR too_many_rows THEN RETURN false;
    END;
    INSERT INTO aap_assets_v1.fixture_external_pyramid_reuses(
        consumer_kind,consumer_id,run_id,dataset_id,item_id,asset_id,generation,
        job_id,job_generation,lease_token,source_operation_id)
    VALUES(p_kind,p_consumer,v_run,v_dataset,v_item,p_asset,p_generation,
        v_match.job_id,v_match.job_generation,v_match.lease_token,v_match.source_operation_id)
    ON CONFLICT DO NOTHING;
    RETURN EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_external_pyramid_reuses AS bound
        WHERE bound.consumer_kind=p_kind AND bound.consumer_id=p_consumer AND bound.run_id=v_run
          AND bound.dataset_id=v_dataset AND bound.item_id=v_item AND bound.asset_id=p_asset
          AND bound.generation=p_generation AND bound.job_id=v_match.job_id
          AND bound.job_generation=v_match.job_generation AND bound.lease_token=v_match.lease_token
          AND bound.source_operation_id=v_match.source_operation_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_fixture_external_pyramid_reuse(text,uuid,uuid,uuid,integer) FROM PUBLIC;
-- aap-statement-break
-- Shared positive evidence stays internal; the public coordinator-facing
-- wrapper retains its exact per-run principal check.
CREATE FUNCTION aap_assets_v1.fixture_external_image_attempt_sources_proven(
    p_run uuid,p_dataset uuid,p_item uuid,p_job uuid,p_generation bigint,p_lease uuid
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_external_media_sources AS source
        WHERE source.run_id=p_run AND source.dataset_id=p_dataset AND source.item_id=p_item
          AND source.family='image' AND source.job_id=p_job AND source.job_generation=p_generation
          AND source.lease_token=p_lease
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_external_media_sources AS source
        WHERE source.run_id=p_run AND source.dataset_id=p_dataset AND source.item_id=p_item
          AND source.family='image' AND source.job_id=p_job AND source.job_generation=p_generation
          AND source.lease_token=p_lease AND source.state<>'settled'
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_external_image_attempt_sources_proven(uuid,uuid,uuid,uuid,bigint,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.fixture_external_image_attempt_sources_settled(
    p_run uuid,p_dataset uuid,p_item uuid,p_job uuid,p_generation bigint,p_lease uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF aap_jobs_v1.fixture_media_principal_matches(p_run) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_media_principal_required';
    END IF;
    RETURN aap_assets_v1.fixture_external_image_attempt_sources_proven(
        p_run,p_dataset,p_item,p_job,p_generation,p_lease);
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.bind_fixture_media_child_generation(
    p_child_id uuid,p_token uuid,p_asset_id uuid,p_generation integer
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT aap_jobs_v1.fixture_media_session_owns('pyramid',p_child_id) THEN RETURN false; END IF;
    UPDATE aap_jobs_v1.fixture_media_children AS child
    SET asset_id=p_asset_id,generation=p_generation
    WHERE child.child_id=p_child_id AND child.state='running' AND child.token=p_token
      AND child.backend_pid=pg_catalog.pg_backend_pid()
      AND (child.asset_id IS NULL OR (child.asset_id,child.generation)=(p_asset_id,p_generation))
      AND EXISTS (
          SELECT 1 FROM aap_jobs_v1.fixture_media_executor_principals AS principal
          JOIN pg_catalog.pg_roles AS role ON role.oid=principal.principal_oid
          WHERE principal.run_id=child.run_id AND role.rolname=SESSION_USER
            AND principal.principal_name=role.rolname AND principal.state='active'
      ) AND (
          aap_assets_v1.fixture_pyramid_generation_owned(
              child.run_id,child.dataset_id,child.item_id,p_asset_id,p_generation
          ) IS TRUE OR aap_assets_v1.record_fixture_external_pyramid_reuse(
              'child',p_child_id,p_token,p_asset_id,p_generation
          ) IS TRUE
      );
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.bind_fixture_independent_pyramid(
    p_delivery uuid,p_token uuid,p_asset uuid,p_generation integer
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_independent_media_births%ROWTYPE;
BEGIN
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_independent_media_births WHERE delivery_id=p_delivery;
    IF NOT FOUND OR v_birth.producer_kind<>'pyramid' OR aap_assets_v1.fixture_independent_session_authorized(
        p_delivery,p_token,v_birth.run_id,v_birth.dataset_id,v_birth.item_id,
        CASE WHEN v_birth.dataset_id IS NULL THEN v_birth.owner_id ELSE NULL END
    ) IS DISTINCT FROM true THEN RETURN false; END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.image_pyramid_generations AS generation
        JOIN aap_assets_v1.fixture_media_births AS birth
          ON birth.owner_kind='pyramid_generation' AND birth.owner_id=generation.asset_id
         AND birth.generation=generation.generation
        WHERE generation.asset_id=p_asset AND generation.generation=p_generation
          AND generation.auth_ref -> 'original'=v_birth.source AND birth.run_id=v_birth.run_id
          AND birth.dataset_id IS NOT DISTINCT FROM v_birth.dataset_id
          AND birth.item_id IS NOT DISTINCT FROM v_birth.item_id
          AND birth.task_id IS NOT DISTINCT FROM CASE WHEN v_birth.dataset_id IS NULL THEN v_birth.owner_id ELSE NULL END
    ) AND aap_assets_v1.record_fixture_external_pyramid_reuse(
        'independent',p_delivery,p_token,p_asset,p_generation
    ) IS DISTINCT FROM true THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_independent_pyramid_bindings(delivery_id,asset_id,generation)
    VALUES(p_delivery,p_asset,p_generation) ON CONFLICT DO NOTHING;
    RETURN EXISTS (SELECT 1 FROM aap_assets_v1.fixture_independent_pyramid_bindings
                   WHERE delivery_id=p_delivery AND asset_id=p_asset AND generation=p_generation);
END;
$function$;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_assets_v1.fixture_dataset_media_producers_quiescent(p_run uuid,p_dataset uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_independent_media_executions AS execution
        JOIN aap_assets_v1.fixture_independent_media_births AS birth USING(delivery_id)
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset AND execution.state<>'settled'
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_executions
        WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_admissions
        WHERE run_id=p_run AND dataset_id=p_dataset AND state='admitted'
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_scan_admissions
        WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_sources
        WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_external_media_sources
        WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_legacy_birth_admissions
        WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_screenshot_producer_admissions
        WHERE run_id=p_run AND dataset_id=p_dataset AND state NOT IN ('settled','reused','rejected')
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.media_native_stages AS stage
        JOIN aap_assets_v1.fixture_media_births AS birth
          ON birth.owner_kind=stage.output_kind AND birth.owner_id=stage.output_id
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset AND stage.state<>'settled'
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_external_pyramid_reuses AS reuse
        LEFT JOIN aap_assets_v1.fixture_external_media_sources AS source
          ON source.source_operation_id=reuse.source_operation_id
         AND source.run_id=reuse.run_id AND source.dataset_id=reuse.dataset_id AND source.item_id=reuse.item_id
         AND source.family='image' AND source.job_id=reuse.job_id
         AND source.job_generation=reuse.job_generation AND source.lease_token=reuse.lease_token
        WHERE reuse.run_id=p_run AND reuse.dataset_id=p_dataset AND (
            source.source_operation_id IS NULL OR source.state<>'settled'
            OR aap_assets_v1.fixture_external_image_attempt_sources_proven(
                reuse.run_id,reuse.dataset_id,reuse.item_id,reuse.job_id,reuse.job_generation,reuse.lease_token
            ) IS DISTINCT FROM true
        )
    );
$function$;
