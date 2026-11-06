-- Individual native PUT versions remain independently accounted for until the
-- physical owner acknowledges deletion and verifies exact-version absence.
CREATE TABLE aap_assets_v1.fixture_native_cache_retirements (
    write_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('prepared','submitted','deleted','absent'))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_cache_retirements FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_cache_retirement()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.write_id,NEW.run_id,NEW.dataset_id) IS DISTINCT FROM
       (OLD.write_id,OLD.run_id,OLD.dataset_id) OR NOT (
           (OLD.state='prepared' AND NEW.state='submitted') OR
           (OLD.state='submitted' AND NEW.state='deleted') OR
           (OLD.state='deleted' AND NEW.state='absent')
       ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_retirement_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_cache_retirement() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_cache_retirement_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_cache_retirements FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_cache_retirement();
-- aap-statement-break
CREATE TABLE aap_assets_v1.fixture_native_scope_retirements (
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    prefix text NOT NULL,
    state text NOT NULL CHECK (state IN ('prepared','absent')),
    PRIMARY KEY (run_id,dataset_id,prefix)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_scope_retirements FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_scope_retirement()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.run_id,NEW.dataset_id,NEW.prefix) IS DISTINCT FROM
       (OLD.run_id,OLD.dataset_id,OLD.prefix)
       OR OLD.state<>'prepared' OR NEW.state<>'absent' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_scope_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_scope_retirement() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_scope_retirement_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_scope_retirements FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_scope_retirement();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_retirement_ready(p_run uuid,p_dataset uuid,p_token uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,p_token)
       AND aap_assets_v1.fixture_independent_deliveries_retired(p_run,p_dataset)
       AND aap_assets_v1.fixture_dataset_legacy_original_readers_retired(p_run,p_dataset)
       AND EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets WHERE run_id=p_run)
       AND NOT EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_native_media_executions
           WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
       )
       AND NOT EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_native_scan_admissions
           WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
       )
       AND NOT EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
           JOIN aap_assets_v1.fixture_native_media_executions AS execution ON execution.id=claim.execution_id
           WHERE execution.run_id=p_run AND execution.dataset_id=p_dataset AND claim.state='admitted'
       )
       AND NOT EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_native_cache_reads
           WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
       )
       AND NOT EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_native_media_admissions AS admission
           LEFT JOIN aap_assets_v1.fixture_native_media_sources AS source
             ON source.admission_token=admission.token
           WHERE admission.run_id=p_run AND admission.dataset_id=p_dataset
             AND (admission.state='admitted' OR
                  (admission.state='consumed' AND (source.admission_token IS NULL OR source.state<>'settled')))
       )
       AND NOT EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_native_media_sources
           WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
       )
       AND NOT EXISTS (
           SELECT 1 FROM aap_assets_v1.fixture_native_cache_writes
           WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'verified'
       )
       AND NOT EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
           JOIN public.dataset_items AS item ON item.id=resource.item_id AND item.dataset_id=p_dataset
           JOIN public.managed_original_creations AS creation ON creation.id=resource.creation_id
           JOIN public.storage_operations AS operation ON operation.location_id=creation.location_id
             AND operation.node_id='aap-assets-media'
             AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
           WHERE resource.run_id=p_run AND resource.kind='creation'
       );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_retirement_ready(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_retirement_scopes(p_run uuid,p_dataset uuid,p_token uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_scopes jsonb;
BEGIN
    IF aap_assets_v1.fixture_native_retirement_ready(p_run,p_dataset,p_token) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_retirement_unsettled';
    END IF;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_jobs AS birth
        LEFT JOIN public.asset_image_jobs AS image ON birth.family='image' AND image.job_id=birth.job_id
        LEFT JOIN public.asset_media_jobs AS media ON birth.family='media' AND media.job_id=birth.job_id
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset
          AND (((birth.family='image' AND image.job_id IS NULL)
             OR (birth.family='media' AND media.job_id IS NULL)) AND NOT EXISTS (
                SELECT 1 FROM aap_assets_v1.fixture_native_job_retirements AS retired
                WHERE retired.family=birth.family AND retired.job_id=birth.job_id
                  AND retired.run_id=p_run AND retired.dataset_id=p_dataset)
            OR (birth.family='image' AND image.kind='pyramid' AND NOT EXISTS (
                SELECT 1 FROM aap_assets_v1.fixture_media_births AS asset
                WHERE asset.owner_kind='pyramid_asset' AND asset.owner_id=image.pyramid_asset_id
                  AND asset.run_id=p_run AND asset.dataset_id=p_dataset AND asset.item_id=birth.item_id
            )))
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_owner_changed'; END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(scope.prefix ORDER BY scope.prefix),'[]'::jsonb)
    INTO v_scopes FROM (
        SELECT 'image-assets/' || birth.job_id::text || '/' AS prefix
        FROM aap_assets_v1.fixture_native_media_jobs AS birth
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset AND birth.family='image'
        UNION
        SELECT 'media-assets/' || birth.job_id::text || '/'
        FROM aap_assets_v1.fixture_native_media_jobs AS birth
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset AND birth.family='media'
        UNION
        SELECT 'image-pyramids/' || birth.owner_id::text || '/'
        FROM aap_assets_v1.fixture_media_births AS birth
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset AND birth.owner_kind='pyramid_asset'
    ) AS scope;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_writes AS write
        WHERE write.run_id=p_run AND write.dataset_id=p_dataset AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.jsonb_array_elements_text(v_scopes) AS scope(prefix)
            WHERE pg_catalog.starts_with(write.object_key,scope.prefix)
        )
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_owner_changed'; END IF;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_writes AS write
        JOIN pg_catalog.jsonb_array_elements_text(v_scopes) AS scope(prefix)
          ON pg_catalog.starts_with(write.object_key,scope.prefix)
        WHERE write.run_id IS DISTINCT FROM p_run OR write.dataset_id IS DISTINCT FROM p_dataset
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_owner_changed'; END IF;
    RETURN v_scopes;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_retirement_scopes(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_retirement_inventory(p_run uuid,p_dataset uuid,p_token uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_scopes jsonb; v_target aap_assets_v1.fixture_native_cache_targets%ROWTYPE;
        v_items jsonb;
BEGIN
    v_scopes := aap_assets_v1.fixture_native_retirement_scopes(p_run,p_dataset,p_token);
    SELECT * INTO v_target FROM aap_assets_v1.fixture_native_cache_targets WHERE run_id=p_run FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_output_missing'; END IF;
    SELECT COALESCE(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
        'prefix',scope.prefix,
        'write_ids',COALESCE((
            SELECT pg_catalog.jsonb_agg(write.id ORDER BY write.id)
            FROM aap_assets_v1.fixture_native_cache_writes AS write
            WHERE write.run_id=p_run AND write.dataset_id=p_dataset
              AND pg_catalog.starts_with(write.object_key,scope.prefix)
        ),'[]'::jsonb)
    ) ORDER BY scope.prefix),'[]'::jsonb) INTO v_items
    FROM pg_catalog.jsonb_array_elements_text(v_scopes) AS scope(prefix);
    RETURN pg_catalog.jsonb_build_object(
        'storage_ref',pg_catalog.jsonb_build_object(
            'schema','aap-fixture-native-output-target/v1','run_id',p_run,
            'endpoint',v_target.endpoint,'bucket',v_target.bucket,
            'storage_identity',v_target.storage_identity,'receipt_sha256',v_target.receipt_sha256),
        'scopes',v_items
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_retirement_inventory(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.prepare_fixture_native_scope(
    p_run uuid,p_dataset uuid,p_token uuid,p_prefix text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_scopes jsonb;
BEGIN
    v_scopes := aap_assets_v1.fixture_native_retirement_scopes(p_run,p_dataset,p_token);
    IF p_prefix IS NULL OR NOT (v_scopes ? p_prefix) THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_native_scope_retirements(run_id,dataset_id,prefix,state)
    VALUES (p_run,p_dataset,p_prefix,'prepared') ON CONFLICT DO NOTHING;
    RETURN EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_scope_retirements
                   WHERE run_id=p_run AND dataset_id=p_dataset AND prefix=p_prefix
                     AND state IN ('prepared','absent'));
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_fixture_native_scope(uuid,uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_fixture_native_scope(
    p_run uuid,p_dataset uuid,p_token uuid,p_prefix text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_scopes jsonb; v_state text;
BEGIN
    v_scopes := aap_assets_v1.fixture_native_retirement_scopes(p_run,p_dataset,p_token);
    IF p_prefix IS NULL OR NOT (v_scopes ? p_prefix) THEN RETURN false; END IF;
    SELECT state INTO v_state FROM aap_assets_v1.fixture_native_scope_retirements
    WHERE run_id=p_run AND dataset_id=p_dataset AND prefix=p_prefix FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_writes AS write
        LEFT JOIN aap_assets_v1.fixture_native_cache_retirements AS retired ON retired.write_id=write.id
        WHERE write.run_id=p_run AND write.dataset_id=p_dataset
          AND pg_catalog.starts_with(write.object_key,p_prefix)
          AND (write.state<>'verified' OR retired.state IS DISTINCT FROM 'absent')
    ) THEN RETURN false; END IF;
    IF v_state='absent' THEN RETURN true; END IF;
    UPDATE aap_assets_v1.fixture_native_scope_retirements SET state='absent'
    WHERE run_id=p_run AND dataset_id=p_dataset AND prefix=p_prefix AND state='prepared';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_fixture_native_scope(uuid,uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.prepare_fixture_native_retirement(
    p_run uuid,p_dataset uuid,p_token uuid,p_write uuid
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_write aap_assets_v1.fixture_native_cache_writes%ROWTYPE;
        v_target aap_assets_v1.fixture_native_cache_targets%ROWTYPE;
        v_state text;
BEGIN
    IF aap_assets_v1.fixture_native_retirement_ready(p_run,p_dataset,p_token) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_retirement_unsettled';
    END IF;
    SELECT * INTO v_write FROM aap_assets_v1.fixture_native_cache_writes
    WHERE id=p_write AND run_id=p_run AND dataset_id=p_dataset AND state='verified' FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_output_missing'; END IF;
    SELECT * INTO v_target FROM aap_assets_v1.fixture_native_cache_targets WHERE run_id=p_run FOR SHARE;
    INSERT INTO aap_assets_v1.fixture_native_cache_retirements(write_id,run_id,dataset_id,state)
    VALUES (p_write,p_run,p_dataset,'prepared') ON CONFLICT DO NOTHING;
    SELECT state INTO v_state FROM aap_assets_v1.fixture_native_cache_retirements
    WHERE write_id=p_write AND run_id=p_run AND dataset_id=p_dataset;
    IF v_state IS NULL OR v_state='submitted' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_retirement_unsettled';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'id',v_write.id,'key',v_write.object_key,'version_id',v_write.version_id,
        'etag',v_write.etag,'sha256',v_write.sha256,'size_bytes',v_write.size_bytes,
        'state',v_state,'storage_ref',pg_catalog.jsonb_build_object(
            'schema','aap-fixture-native-output-target/v1','run_id',p_run,
            'endpoint',v_target.endpoint,'bucket',v_target.bucket,
            'storage_identity',v_target.storage_identity,'receipt_sha256',v_target.receipt_sha256)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_fixture_native_retirement(uuid,uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.advance_fixture_native_retirement(
    p_run uuid,p_dataset uuid,p_token uuid,p_write uuid,p_state text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF aap_assets_v1.fixture_native_retirement_ready(p_run,p_dataset,p_token) IS DISTINCT FROM true
       OR p_state NOT IN ('submitted','deleted','absent') THEN RETURN false; END IF;
    UPDATE aap_assets_v1.fixture_native_cache_retirements SET state=p_state
    WHERE write_id=p_write AND run_id=p_run AND dataset_id=p_dataset
      AND ((state='prepared' AND p_state='submitted') OR
           (state='submitted' AND p_state='deleted') OR (state='deleted' AND p_state='absent'));
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.advance_fixture_native_retirement(uuid,uuid,uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_outputs_retired(p_run uuid,p_dataset uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    WITH expected AS (
        SELECT 'image-assets/' || job.job_id::text || '/' AS prefix
        FROM aap_assets_v1.fixture_native_media_jobs AS job
        WHERE job.run_id=p_run AND job.dataset_id=p_dataset AND job.family='image'
        UNION
        SELECT 'media-assets/' || job.job_id::text || '/'
        FROM aap_assets_v1.fixture_native_media_jobs AS job
        WHERE job.run_id=p_run AND job.dataset_id=p_dataset AND job.family='media'
        UNION
        SELECT 'image-pyramids/' || birth.owner_id::text || '/'
        FROM aap_assets_v1.fixture_media_births AS birth
        WHERE birth.run_id=p_run AND birth.dataset_id=p_dataset AND birth.owner_kind='pyramid_asset'
    )
    SELECT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS dataset ON dataset.run_id=run.id
        JOIN aap_assets_v1.fixture_native_cache_targets AS target ON target.run_id=run.id
        WHERE run.id=p_run AND run.state='sealing'
          AND ((run.family='owned' AND run.parent_run_id IS NOT NULL)
            OR (run.family='screenshot' AND run.parent_run_id IS NULL))
          AND dataset.kind='dataset' AND dataset.resource_id=p_dataset AND dataset.state='published'
    )
    AND NOT EXISTS (
        SELECT 1 FROM expected LEFT JOIN aap_assets_v1.fixture_native_scope_retirements AS scope
          ON scope.run_id=p_run AND scope.dataset_id=p_dataset AND scope.prefix=expected.prefix
        WHERE scope.state IS DISTINCT FROM 'absent'
    )
    AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_scope_retirements AS scope
        WHERE scope.run_id=p_run AND scope.dataset_id=p_dataset
          AND (scope.state<>'absent' OR NOT EXISTS (SELECT 1 FROM expected WHERE expected.prefix=scope.prefix))
    )
    AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_writes AS write
        LEFT JOIN aap_assets_v1.fixture_native_cache_retirements AS retired ON retired.write_id=write.id
        WHERE write.run_id=p_run AND write.dataset_id=p_dataset
          AND (write.state<>'verified' OR retired.state IS DISTINCT FROM 'absent'
            OR retired.run_id IS DISTINCT FROM p_run OR retired.dataset_id IS DISTINCT FROM p_dataset
            OR NOT EXISTS (SELECT 1 FROM expected WHERE pg_catalog.starts_with(write.object_key,expected.prefix)))
    )
    AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_reads
        WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
    )
    AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_executions
        WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
    )
    AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_scan_admissions
        WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
    )
    AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_claims AS claim
        JOIN aap_assets_v1.fixture_native_media_executions AS execution ON execution.id=claim.execution_id
        WHERE execution.run_id=p_run AND execution.dataset_id=p_dataset AND claim.state='admitted'
    )
    AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_admissions AS admission
        LEFT JOIN aap_assets_v1.fixture_native_media_sources AS source
          ON source.admission_token=admission.token
        WHERE admission.run_id=p_run AND admission.dataset_id=p_dataset
          AND (admission.state='admitted' OR
               (admission.state='consumed' AND (source.admission_token IS NULL OR source.state<>'settled')))
    )
    AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_sources
        WHERE run_id=p_run AND dataset_id=p_dataset AND state<>'settled'
    )
    AND NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        JOIN public.dataset_items AS item ON item.id=resource.item_id AND item.dataset_id=p_dataset
        JOIN public.managed_original_creations AS creation ON creation.id=resource.creation_id
        JOIN public.storage_operations AS operation ON operation.location_id=creation.location_id
          AND operation.node_id='aap-assets-media'
          AND operation.purpose='media_original_' || pg_catalog.replace(creation.id::text,'-','')
        WHERE resource.run_id=p_run AND resource.kind='creation'
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_outputs_retired(uuid,uuid) FROM PUBLIC;
