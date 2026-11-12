-- A dedicated cache target cannot be adopted as an original or purpose binding.
CREATE FUNCTION aap_assets_v1.guard_fixture_native_target_original()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock_shared(
        pg_catalog.hashtextextended('aap-fixture-native-location:' || NEW.location_id::text,0));
    PERFORM pg_catalog.pg_advisory_xact_lock_shared(
        pg_catalog.hashtextextended('aap-fixture-native-identity:' || NEW.storage_identity,0));
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets AS target
        WHERE target.location_id=NEW.location_id OR target.storage_identity=NEW.storage_identity
           OR (target.bucket=NEW.bucket AND EXISTS (
               SELECT 1 FROM public.storage_locations AS location
               JOIN public.storage_connections AS connection ON connection.id=location.connection_id
               WHERE location.id=NEW.location_id AND connection.config ->> 'endpoint'=target.endpoint
           ))
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_original_forbidden'; END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_target_original() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_creation_target_guard BEFORE INSERT OR UPDATE OF location_id,storage_identity,bucket
ON public.managed_original_creations FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_target_original();
-- aap-statement-break
CREATE TRIGGER fixture_native_task_original_target_guard BEFORE INSERT OR UPDATE OF location_id,storage_identity,bucket
ON public.task_originals FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_target_original();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_target_binding()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock_shared(
        pg_catalog.hashtextextended('aap-fixture-native-location:' || NEW.location_id::text,0));
    IF EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets WHERE location_id=NEW.location_id)
    THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_binding_forbidden'; END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_target_binding() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_purpose_binding_guard BEFORE INSERT OR UPDATE OF location_id ON public.storage_bindings
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_target_binding();
-- aap-statement-break
CREATE TRIGGER fixture_native_asset_location_guard BEFORE INSERT OR UPDATE OF location_id ON public.dataset_asset_locations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_target_binding();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_target_output()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_target aap_assets_v1.fixture_native_cache_targets%ROWTYPE; v_kind text; v_root uuid;
    v_birth aap_assets_v1.fixture_media_births%ROWTYPE;
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
    IF NOT FOUND OR (
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
-- aap-statement-break
CREATE TRIGGER fixture_thumbnail_target_guard AFTER INSERT OR UPDATE OF storage_ref ON public.thumbnail_outputs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_target_output();
-- aap-statement-break
CREATE TRIGGER fixture_pyramid_target_guard AFTER INSERT OR UPDATE OF storage_ref ON public.image_pyramid_outputs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_target_output();
-- aap-statement-break
CREATE TRIGGER fixture_video_target_guard AFTER INSERT OR UPDATE OF storage_ref ON public.video_derivative_outputs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_target_output();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_target_connection_alias()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NEW.kind='s3' AND NEW.config ->> 'endpoint' IS NOT NULL AND NEW.config ->> 'bucket' IS NOT NULL THEN
        PERFORM pg_catalog.pg_advisory_xact_lock_shared(
            pg_catalog.hashtextextended('aap-fixture-native-endpoint:' || (NEW.config ->> 'endpoint') || ':' ||
                (NEW.config ->> 'bucket'),0));
        IF EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_cache_targets AS target
                   WHERE target.endpoint=NEW.config ->> 'endpoint' AND target.bucket=NEW.config ->> 'bucket'
                     AND target.connection_id<>NEW.id) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_connection_alias_forbidden';
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_target_connection_alias() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_connection_alias_guard BEFORE INSERT OR UPDATE OF kind,config ON public.storage_connections
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_target_connection_alias();
