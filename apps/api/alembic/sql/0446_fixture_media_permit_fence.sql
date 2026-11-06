-- Existing output permits serialize physical readers and writers with sealing.
-- A sealed fixture may open only an output already retired by its native owner;
-- the native output's state then prohibits reads and publication.
CREATE FUNCTION aap_assets_v1.guard_fixture_media_output_permit()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_kind text;
    v_id uuid;
    v_birth aap_assets_v1.fixture_media_births%ROWTYPE;
    v_run_state text;
    v_root_state text;
    v_output_state text;
BEGIN
    IF NEW.purpose ~ '^thumbnail_[0-9a-f]{32}$' THEN
        v_kind := 'thumbnail';
        v_id := pg_catalog.substr(NEW.purpose,11)::uuid;
    ELSIF NEW.purpose ~ '^image_pyramid_[0-9a-f]{32}$' THEN
        v_kind := 'pyramid_output';
        v_id := pg_catalog.substr(NEW.purpose,15)::uuid;
    ELSIF NEW.purpose ~ '^video_derivative_[0-9a-f]{32}$' THEN
        v_kind := 'video_output';
        v_id := pg_catalog.substr(NEW.purpose,18)::uuid;
    ELSE
        RETURN NEW;
    END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_media_births
    WHERE owner_kind=v_kind AND owner_id=v_id AND generation=0;
    IF NOT FOUND THEN RETURN NEW; END IF;
    SELECT run.state,root.state INTO v_run_state,v_root_state
    FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
    WHERE run.id=v_birth.run_id FOR SHARE OF run,root;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_owner_missing';
    END IF;
    IF v_run_state='open' AND v_root_state='open' THEN RETURN NEW; END IF;
    IF v_kind='thumbnail' THEN
        SELECT state INTO v_output_state FROM public.thumbnail_outputs WHERE id=v_id;
    ELSIF v_kind='pyramid_output' THEN
        SELECT state INTO v_output_state FROM public.image_pyramid_outputs WHERE id=v_id;
    ELSE
        SELECT state INTO v_output_state FROM public.video_derivative_outputs WHERE id=v_id;
    END IF;
    IF v_run_state <> 'sealing' OR v_output_state IS DISTINCT FROM 'retiring'
       OR aap_jobs_v1.fixture_media_retirement_current(v_birth.run_id,v_birth.dataset_id)
          IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_output_closed';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_media_output_permit() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_output_permit BEFORE INSERT ON public.storage_operations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_output_permit();
