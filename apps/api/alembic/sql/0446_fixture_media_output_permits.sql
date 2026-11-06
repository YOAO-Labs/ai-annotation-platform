-- Fixture removal uses the ordinary output-purpose physical permit, but only
-- the authenticated retirement session may create it after sealing.
CREATE TABLE aap_assets_v1.fixture_media_retirement_permits (
    operation_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    owner_kind text NOT NULL CHECK (owner_kind IN ('thumbnail','pyramid_output','video_output')),
    owner_id uuid NOT NULL,
    token uuid NOT NULL,
    location_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('open','closed'))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_media_retirement_permits FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_media_retirement_permit()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.operation_id,NEW.run_id,NEW.dataset_id,NEW.owner_kind,
          NEW.owner_id,NEW.token,NEW.location_id) IS DISTINCT FROM
         (OLD.operation_id,OLD.run_id,OLD.dataset_id,OLD.owner_kind,
          OLD.owner_id,OLD.token,OLD.location_id)
       OR OLD.state <> 'open' OR NEW.state <> 'closed' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_permit_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_media_retirement_permit() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_retirement_permit_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_media_retirement_permits FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_retirement_permit();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.acquire_fixture_media_retirement_permit(
    p_run uuid,p_dataset uuid,p_token uuid,p_kind text,p_owner uuid,p_operation uuid
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_ref jsonb; v_state text; v_purpose text; v_location uuid; v_gate record;
BEGIN
    IF p_operation IS NULL OR aap_jobs_v1.fixture_media_retirement_authorized(
        p_run,p_dataset,p_token) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_media_principal_required';
    END IF;
    PERFORM 1 FROM aap_assets_v1.fixture_media_births
    WHERE run_id=p_run AND dataset_id=p_dataset AND owner_kind=p_kind
      AND owner_id=p_owner AND generation=0 FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_owner_missing'; END IF;
    IF p_kind='thumbnail' THEN
        SELECT storage_ref,state INTO v_ref,v_state FROM public.thumbnail_outputs WHERE id=p_owner FOR UPDATE;
        v_purpose := 'thumbnail_' || pg_catalog.replace(p_owner::text,'-','');
    ELSIF p_kind='pyramid_output' THEN
        SELECT storage_ref,state INTO v_ref,v_state FROM public.image_pyramid_outputs WHERE id=p_owner FOR UPDATE;
        v_purpose := 'image_pyramid_' || pg_catalog.replace(p_owner::text,'-','');
    ELSIF p_kind='video_output' THEN
        SELECT storage_ref,state INTO v_ref,v_state FROM public.video_derivative_outputs WHERE id=p_owner FOR UPDATE;
        v_purpose := 'video_derivative_' || pg_catalog.replace(p_owner::text,'-','');
    ELSE
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_media_owner_invalid';
    END IF;
    IF NOT FOUND OR v_state IS DISTINCT FROM 'retiring' OR EXISTS (
        SELECT 1 FROM public.storage_operations WHERE purpose=v_purpose
    ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_output_unsettled'; END IF;
    BEGIN v_location := (v_ref ->> 'location_id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_owner_changed';
    END;
    SELECT state,revision INTO v_gate FROM public.storage_maintenance_gates
    WHERE location_id=v_location FOR SHARE;
    IF NOT FOUND OR v_gate.state <> 'active' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='storage_maintenance_busy';
    END IF;
    INSERT INTO public.storage_operations(id,location_id,revision,purpose,node_id)
    VALUES (p_operation,v_location,v_gate.revision,v_purpose,'aap-fixture-media-retirement');
    INSERT INTO aap_assets_v1.fixture_media_retirement_permits
        (operation_id,run_id,dataset_id,owner_kind,owner_id,token,location_id,state)
    VALUES (p_operation,p_run,p_dataset,p_kind,p_owner,p_token,v_location,'open');
    RETURN pg_catalog.jsonb_build_object('id',p_operation,'location_id',v_location,'revision',v_gate.revision);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.acquire_fixture_media_retirement_permit(uuid,uuid,uuid,text,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.close_fixture_media_retirement_permit()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    UPDATE aap_assets_v1.fixture_media_retirement_permits SET state='closed'
    WHERE operation_id=OLD.id AND location_id=OLD.location_id AND state='open';
    RETURN OLD;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.close_fixture_media_retirement_permit() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_retirement_permit_close BEFORE DELETE ON public.storage_operations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.close_fixture_media_retirement_permit();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_media_retirement_session_clean(p_run uuid,p_dataset uuid,p_token uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RETURN NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_retirement_permits
        WHERE run_id=p_run AND dataset_id=p_dataset AND token=p_token AND state='open'
    ) AND NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_version_retirements
        WHERE run_id=p_run AND dataset_id=p_dataset AND state='submitted'
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_media_retirement_session_clean(uuid,uuid,uuid) FROM PUBLIC;
