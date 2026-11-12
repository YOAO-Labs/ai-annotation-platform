-- A real version deletion has three durable physical checkpoints.  In
-- particular an unanswered submitted DELETE is never treated as absence.
CREATE TABLE aap_assets_v1.fixture_media_version_retirements (
    version_id uuid PRIMARY KEY REFERENCES aap_assets_v1.fixture_media_versions(id),
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    owner_kind text NOT NULL,
    owner_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN ('prepared','submitted','deleted','absent')),
    submitted_at timestamptz,
    deleted_at timestamptz,
    absent_at timestamptz,
    CHECK ((state='prepared') = (submitted_at IS NULL)),
    CHECK ((state IN ('deleted','absent')) = (deleted_at IS NOT NULL)),
    CHECK ((state='absent') = (absent_at IS NOT NULL))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_media_version_retirements FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_media_version_retirement()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE'
       OR (NEW.version_id,NEW.run_id,NEW.dataset_id,NEW.owner_kind,NEW.owner_id)
          IS DISTINCT FROM (OLD.version_id,OLD.run_id,OLD.dataset_id,OLD.owner_kind,OLD.owner_id)
       OR (OLD.submitted_at IS NOT NULL AND NEW.submitted_at IS DISTINCT FROM OLD.submitted_at)
       OR (OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at)
       OR (OLD.absent_at IS NOT NULL AND NEW.absent_at IS DISTINCT FROM OLD.absent_at)
       OR NOT ((OLD.state='prepared' AND NEW.state='submitted')
            OR (OLD.state='submitted' AND NEW.state='deleted')
            OR (OLD.state='deleted' AND NEW.state='absent')) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_version_retirement_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_media_version_retirement() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_version_retirement_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_media_version_retirements FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_version_retirement();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.prepare_fixture_media_version_retirement(
    p_run uuid,p_dataset uuid,p_token uuid,p_version uuid,p_operation uuid
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_version aap_assets_v1.fixture_media_versions%ROWTYPE;
    v_ref jsonb;
    v_state text;
    v_purpose text;
    v_existing aap_assets_v1.fixture_media_version_retirements%ROWTYPE;
BEGIN
    IF aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,p_token) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_media_principal_required';
    END IF;
    SELECT * INTO v_version FROM aap_assets_v1.fixture_media_versions
    WHERE id=p_version AND run_id=p_run AND dataset_id=p_dataset FOR UPDATE;
    IF NOT FOUND OR v_version.state <> 'verified' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_version_unsettled';
    END IF;
    IF v_version.owner_kind='thumbnail' THEN
        SELECT storage_ref,state INTO v_ref,v_state FROM public.thumbnail_outputs
        WHERE id=v_version.owner_id FOR SHARE;
        v_purpose := 'thumbnail_' || pg_catalog.replace(v_version.owner_id::text,'-','');
    ELSIF v_version.owner_kind='pyramid_output' THEN
        SELECT storage_ref,state INTO v_ref,v_state FROM public.image_pyramid_outputs
        WHERE id=v_version.owner_id FOR SHARE;
        v_purpose := 'image_pyramid_' || pg_catalog.replace(v_version.owner_id::text,'-','');
    ELSE
        SELECT storage_ref,state INTO v_ref,v_state FROM public.video_derivative_outputs
        WHERE id=v_version.owner_id FOR SHARE;
        v_purpose := 'video_derivative_' || pg_catalog.replace(v_version.owner_id::text,'-','');
    END IF;
    IF NOT FOUND OR v_state IS DISTINCT FROM 'retiring'
       OR NOT EXISTS (
           SELECT 1 FROM public.storage_operations AS operation
           JOIN aap_assets_v1.fixture_media_retirement_permits AS permit ON permit.operation_id=operation.id
           WHERE operation.id=p_operation AND operation.purpose=v_purpose
             AND operation.location_id::text=v_ref ->> 'location_id'
             AND permit.run_id=p_run AND permit.dataset_id=p_dataset AND permit.token=p_token
             AND permit.owner_kind=v_version.owner_kind AND permit.owner_id=v_version.owner_id
             AND permit.location_id=operation.location_id AND permit.state='open'
       )
       OR EXISTS (SELECT 1 FROM public.storage_operations AS operation
                  WHERE operation.purpose=v_purpose AND operation.id<>p_operation) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_output_unsettled';
    END IF;
    INSERT INTO aap_assets_v1.fixture_media_version_retirements
        (version_id,run_id,dataset_id,owner_kind,owner_id,state)
    VALUES (v_version.id,p_run,p_dataset,v_version.owner_kind,v_version.owner_id,'prepared')
    ON CONFLICT (version_id) DO NOTHING;
    SELECT * INTO v_existing FROM aap_assets_v1.fixture_media_version_retirements
    WHERE version_id=p_version FOR UPDATE;
    IF (v_existing.run_id,v_existing.dataset_id,v_existing.owner_kind,v_existing.owner_id)
       IS DISTINCT FROM (p_run,p_dataset,v_version.owner_kind,v_version.owner_id)
       OR v_existing.state='submitted' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_version_unsettled';
    END IF;
    RETURN pg_catalog.jsonb_build_object(
        'id',v_version.id,'state',v_existing.state,'storage_ref',v_ref,
        'key',v_version.object_key,'sha256',v_version.sha256,'size_bytes',v_version.size_bytes,
        'etag',v_version.etag,'version_id',v_version.version_id
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_fixture_media_version_retirement(uuid,uuid,uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.advance_fixture_media_version_retirement(
    p_run uuid,p_dataset uuid,p_token uuid,p_version uuid,p_operation uuid,p_state text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_retirement aap_assets_v1.fixture_media_version_retirements%ROWTYPE; v_purpose text;
BEGIN
    IF aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,p_token) IS DISTINCT FROM true THEN
        RETURN false;
    END IF;
    SELECT * INTO v_retirement FROM aap_assets_v1.fixture_media_version_retirements
    WHERE version_id=p_version AND run_id=p_run AND dataset_id=p_dataset FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    v_purpose := CASE v_retirement.owner_kind
        WHEN 'thumbnail' THEN 'thumbnail_'
        WHEN 'pyramid_output' THEN 'image_pyramid_'
        WHEN 'video_output' THEN 'video_derivative_'
        ELSE NULL END || pg_catalog.replace(v_retirement.owner_id::text,'-','');
    IF v_purpose IS NULL OR NOT EXISTS (
        SELECT 1 FROM public.storage_operations AS operation
        JOIN aap_assets_v1.fixture_media_retirement_permits AS permit ON permit.operation_id=operation.id
        WHERE operation.id=p_operation AND operation.purpose=v_purpose
          AND permit.run_id=p_run AND permit.dataset_id=p_dataset AND permit.token=p_token
          AND permit.owner_kind=v_retirement.owner_kind AND permit.owner_id=v_retirement.owner_id
          AND permit.location_id=operation.location_id AND permit.state='open'
    ) THEN RETURN false; END IF;
    IF p_state='submitted' AND v_retirement.state='prepared' THEN
        UPDATE aap_assets_v1.fixture_media_version_retirements
        SET state='submitted',submitted_at=pg_catalog.clock_timestamp() WHERE version_id=p_version;
    ELSIF p_state='deleted' AND v_retirement.state='submitted' THEN
        UPDATE aap_assets_v1.fixture_media_version_retirements
        SET state='deleted',deleted_at=pg_catalog.clock_timestamp() WHERE version_id=p_version;
    ELSIF p_state='absent' AND v_retirement.state='deleted' THEN
        UPDATE aap_assets_v1.fixture_media_version_retirements
        SET state='absent',absent_at=pg_catalog.clock_timestamp() WHERE version_id=p_version;
    ELSE
        RETURN false;
    END IF;
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.advance_fixture_media_version_retirement(uuid,uuid,uuid,uuid,uuid,text) FROM PUBLIC;
