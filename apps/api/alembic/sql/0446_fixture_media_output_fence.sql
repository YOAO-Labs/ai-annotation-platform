-- A sealed run permits already-admitted producers to settle before retirement
-- starts.  No missing process or Job state is accepted as settlement.
CREATE FUNCTION aap_assets_v1.fixture_dataset_media_producers_quiescent(p_run uuid,p_dataset uuid)
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
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_dataset_media_producers_quiescent(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_assets_v1.fixture_video_output_anchors (
    output_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    snapshot jsonb NOT NULL CHECK (pg_catalog.jsonb_typeof(snapshot)='object')
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_video_output_anchors FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_video_output_anchor_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_video_output_anchors FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
-- Fixture output rows remain authoritative until physical retirement.  Once a
-- run seals, only its authenticated retirement owner may advance exact rows.
CREATE FUNCTION aap_assets_v1.guard_fixture_media_output_mutation()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_kind text; v_birth aap_assets_v1.fixture_media_births%ROWTYPE;
    v_root uuid; v_root_state text; v_state text;
BEGIN
    v_kind := CASE TG_TABLE_NAME WHEN 'thumbnail_outputs' THEN 'thumbnail'
        WHEN 'image_pyramid_outputs' THEN 'pyramid_output'
        WHEN 'video_derivative_outputs' THEN 'video_output' END;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_media_births
    WHERE owner_kind=v_kind AND owner_id=OLD.id AND generation=0;
    IF NOT FOUND THEN
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='DELETE' THEN
        IF v_kind='video_output' AND OLD.state='deleted' AND EXISTS (
            SELECT 1 FROM aap_assets_v1.fixture_video_output_anchors AS anchor
            WHERE anchor.output_id=OLD.id AND anchor.run_id=v_birth.run_id
              AND anchor.dataset_id=v_birth.dataset_id AND anchor.item_id=v_birth.item_id
              AND anchor.snapshot=pg_catalog.to_jsonb(OLD)
        ) AND COALESCE((
            SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
            FROM pg_catalog.pg_class AS c WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')
        ),false) THEN RETURN OLD; END IF;
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_output_retained';
    END IF;
    SELECT COALESCE(parent_run_id,id) INTO v_root FROM yoao_fixture_v1.fixture_runs WHERE id=v_birth.run_id;
    SELECT state INTO v_root_state FROM yoao_fixture_v1.fixture_runs
    WHERE id=v_root AND parent_run_id IS NULL FOR SHARE;
    SELECT state INTO v_state FROM yoao_fixture_v1.fixture_runs
    WHERE id=v_birth.run_id AND COALESCE(parent_run_id,id)=v_root FOR SHARE;
    IF v_root_state='open' AND v_state='open' THEN RETURN NEW; END IF;
    IF v_root_state IN ('open','sealing') AND v_state='sealing' AND v_birth.dataset_id IS NOT NULL THEN
        IF NOT pg_catalog.pg_try_advisory_xact_lock_shared(
            pg_catalog.hashtextextended('aap-fixture-media:retirement:' || v_birth.dataset_id::text,0)
        ) THEN
            RAISE EXCEPTION USING ERRCODE='55P03', MESSAGE='fixture_media_retirement_busy';
        END IF;
        IF aap_jobs_v1.fixture_media_retirement_started(v_birth.run_id,v_birth.dataset_id) IS FALSE THEN
            RETURN NEW;
        END IF;
    END IF;
    IF v_root_state NOT IN ('open','sealing') OR v_state IS DISTINCT FROM 'sealing'
       OR v_birth.dataset_id IS NULL
       OR aap_jobs_v1.fixture_media_retirement_current(v_birth.run_id,v_birth.dataset_id) IS DISTINCT FROM true
       OR NOT ((OLD.state IN ('building','ready','failed') AND NEW.state='retiring')
            OR (OLD.state='retiring' AND NEW.state='deleted'))
       OR (CASE WHEN v_kind='thumbnail' THEN
            (pg_catalog.to_jsonb(NEW)-'state'-'retired_at') IS DISTINCT FROM
            (pg_catalog.to_jsonb(OLD)-'state'-'retired_at')
            OR (pg_catalog.to_jsonb(OLD) ->> 'retired_at' IS NOT NULL
                AND pg_catalog.to_jsonb(NEW) -> 'retired_at' IS DISTINCT FROM pg_catalog.to_jsonb(OLD) -> 'retired_at')
            OR (NEW.state='retiring' AND pg_catalog.to_jsonb(NEW) ->> 'retired_at' IS NULL)
           ELSE (pg_catalog.to_jsonb(NEW)-'state') IS DISTINCT FROM (pg_catalog.to_jsonb(OLD)-'state') END)
    THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_output_sealed'; END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_media_output_mutation() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_thumbnail_output_fence BEFORE UPDATE OR DELETE ON public.thumbnail_outputs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_output_mutation();
-- aap-statement-break
CREATE TRIGGER fixture_pyramid_output_fence BEFORE UPDATE OR DELETE ON public.image_pyramid_outputs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_output_mutation();
-- aap-statement-break
CREATE TRIGGER fixture_video_output_fence BEFORE UPDATE OR DELETE ON public.video_derivative_outputs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_output_mutation();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.transition_fixture_media_output(
    p_run uuid,p_dataset uuid,p_token uuid,p_kind text,p_owner uuid,p_expected text,p_next text
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_manifest jsonb;
BEGIN
    IF aap_jobs_v1.fixture_media_retirement_authorized(p_run,p_dataset,p_token) IS DISTINCT FROM true
       OR NOT ((p_expected IN ('building','ready','failed') AND p_next='retiring')
            OR (p_expected='retiring' AND p_next='deleted')) THEN RETURN false; END IF;
    v_manifest := aap_assets_v1.fixture_media_output_inventory(p_run,p_dataset,p_token,p_kind,p_owner);
    IF v_manifest ->> 'state' IS DISTINCT FROM p_expected
       OR (p_next='deleted' AND aap_assets_v1.fixture_media_output_retirement_done(
           p_run,p_dataset,p_token,p_kind,p_owner) IS DISTINCT FROM true) THEN RETURN false; END IF;
    IF p_kind='thumbnail' THEN
        UPDATE public.thumbnail_outputs SET state=p_next,retired_at=COALESCE(retired_at,pg_catalog.now())
        WHERE id=p_owner AND state=p_expected;
    ELSIF p_kind='pyramid_output' THEN
        UPDATE public.image_pyramid_outputs SET state=p_next WHERE id=p_owner AND state=p_expected;
    ELSIF p_kind='video_output' THEN
        UPDATE public.video_derivative_outputs SET state=p_next WHERE id=p_owner AND state=p_expected;
    ELSE RETURN false; END IF;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.transition_fixture_media_output(uuid,uuid,uuid,text,uuid,text,text) FROM PUBLIC;
