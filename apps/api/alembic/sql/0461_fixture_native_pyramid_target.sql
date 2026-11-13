-- Native pyramid allocation already records its exact birth before this target
-- guard runs. Its live claim is not a legacy producer admission.
CREATE OR REPLACE FUNCTION aap_assets_v1.guard_fixture_native_target_output()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_target aap_assets_v1.fixture_native_cache_targets%ROWTYPE; v_kind text; v_root uuid;
    v_birth aap_assets_v1.fixture_media_births%ROWTYPE;
    v_native boolean := false;
BEGIN
    IF NEW.storage_ref ->> 'endpoint_url' IS NOT NULL AND NEW.storage_ref ->> 'bucket' IS NOT NULL THEN
        PERFORM pg_catalog.pg_advisory_xact_lock_shared(
            pg_catalog.hashtextextended('aap-fixture-native-endpoint:' || (NEW.storage_ref ->> 'endpoint_url') || ':' ||
                (NEW.storage_ref ->> 'bucket'),0));
    END IF;
    IF NEW.storage_ref ->> 'location_id' IS NOT NULL THEN
        PERFORM pg_catalog.pg_advisory_xact_lock_shared(
            pg_catalog.hashtextextended('aap-fixture-native-location:' || (NEW.storage_ref ->> 'location_id'),0));
    END IF;
    IF NEW.storage_ref ->> 'storage_identity' IS NOT NULL THEN
        PERFORM pg_catalog.pg_advisory_xact_lock_shared(
            pg_catalog.hashtextextended('aap-fixture-native-identity:' || (NEW.storage_ref ->> 'storage_identity'),0));
    END IF;
    SELECT * INTO v_target FROM aap_assets_v1.fixture_native_cache_targets AS target
    WHERE target.location_id::text=NEW.storage_ref ->> 'location_id'
       OR target.storage_identity=NEW.storage_ref ->> 'storage_identity'
       OR (target.bucket=NEW.storage_ref ->> 'bucket' AND target.endpoint=NEW.storage_ref ->> 'endpoint_url');
    IF NOT FOUND THEN RETURN NEW; END IF;
    v_kind := CASE TG_TABLE_NAME WHEN 'thumbnail_outputs' THEN 'thumbnail'
        WHEN 'image_pyramid_outputs' THEN 'pyramid_output' WHEN 'video_derivative_outputs' THEN 'video_output' END;
    SELECT COALESCE(parent_run_id,id) INTO v_root FROM yoao_fixture_v1.fixture_runs WHERE id=v_target.run_id;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs WHERE id=v_root AND parent_run_id IS NULL
      AND state IN ('open','sealing') FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_target_closed'; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs WHERE id=v_target.run_id
      AND COALESCE(parent_run_id,id)=v_root AND state IN ('open','sealing') FOR SHARE;
    IF NOT FOUND OR NEW.storage_ref ->> 'location_id' IS DISTINCT FROM v_target.location_id::text
       OR NEW.storage_ref ->> 'storage_identity' IS DISTINCT FROM v_target.storage_identity
       OR NEW.storage_ref ->> 'endpoint_url' IS DISTINCT FROM v_target.endpoint
       OR NEW.storage_ref ->> 'bucket' IS DISTINCT FROM v_target.bucket THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_target_owner_changed';
    END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_media_births AS birth
    WHERE birth.owner_kind=v_kind AND birth.owner_id=NEW.id AND birth.run_id=v_target.run_id AND birth.generation=0;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_target_owner_changed';
    END IF;
    IF v_kind='pyramid_output' THEN
        SELECT EXISTS (
            SELECT 1 FROM aap_assets_v1.fixture_native_pyramid_generations AS binding
            JOIN aap_assets_v1.fixture_native_media_claims AS claim
              ON claim.family='image' AND claim.job_id=binding.job_id
                AND claim.job_generation=binding.job_generation
            JOIN public.asset_image_jobs AS job ON job.job_id=binding.job_id
            WHERE binding.asset_id=NEW.asset_id AND binding.generation=NEW.generation
              AND (binding.run_id,binding.dataset_id,binding.item_id)=
                  (v_birth.run_id,v_birth.dataset_id,v_birth.item_id)
              AND claim.state='claimed' AND claim.lease_token=NEW.lease_token
              AND job.kind='pyramid' AND job.status='running'
              AND job.generation=binding.job_generation
              AND job.lease_token=claim.lease_token
              AND job.lease_expires_at>pg_catalog.clock_timestamp()
              AND job.pyramid_asset_id=binding.asset_id
              AND job.pyramid_generation=binding.generation
        ) INTO v_native;
    END IF;
    IF NOT v_native AND (
        aap_assets_v1.fixture_legacy_birth_admission_current(
            v_birth.run_id,v_birth.dataset_id,v_birth.item_id,v_birth.task_id) IS DISTINCT FROM true
        AND NOT (v_kind='video_output' AND pg_catalog.to_jsonb(NEW) ->> 'kind'='metadata'
            AND aap_assets_v1.fixture_screenshot_producer_current(
                v_birth.run_id,v_birth.dataset_id,v_birth.item_id) IS NOT NULL)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_target_owner_changed';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_target_output() FROM PUBLIC;
