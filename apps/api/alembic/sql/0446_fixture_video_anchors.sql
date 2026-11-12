-- Only the private coordinator may remove the business anchor after native
-- target retirement has frozen all of this Dataset's physical evidence.
CREATE FUNCTION aap_assets_v1.fixture_video_output_disposition(p_run uuid,p_dataset uuid,p_output uuid)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT COALESCE((SELECT pg_catalog.pg_has_role(SESSION_USER,pg_catalog.pg_get_userbyid(c.relowner),'MEMBER')
        FROM pg_catalog.pg_class AS c WHERE c.oid=pg_catalog.to_regclass('public.fixture_export_zip_retirements')),false)
    THEN RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='fixture_private_owner_required'; END IF;
    IF EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_births AS birth
        JOIN yoao_fixture_v1.fixture_run_resources AS item
          ON item.run_id=birth.run_id AND item.kind='item' AND item.resource_id=birth.item_id
         AND item.state IN ('published','retired')
        WHERE birth.owner_kind='video_output' AND birth.owner_id=p_output AND birth.generation=0
          AND birth.run_id=p_run AND birth.dataset_id=p_dataset
          AND (EXISTS (SELECT 1 FROM public.video_derivative_outputs AS output
                       WHERE output.id=p_output AND output.dataset_item_id=birth.item_id)
            OR EXISTS (SELECT 1 FROM aap_assets_v1.fixture_video_output_anchors AS anchor
                       WHERE anchor.output_id=p_output AND anchor.run_id=p_run
                         AND anchor.dataset_id=p_dataset AND anchor.item_id=birth.item_id))
    ) THEN RETURN 'owned'; END IF;
    RETURN 'unknown';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_video_output_disposition(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.retire_fixture_video_output_anchor(p_run uuid,p_dataset uuid,p_output uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_media_births%ROWTYPE;
    v_output public.video_derivative_outputs%ROWTYPE; v_root uuid;
BEGIN
    IF aap_assets_v1.fixture_video_output_disposition(p_run,p_dataset,p_output) IS DISTINCT FROM 'owned'
    THEN RETURN false; END IF;
    IF EXISTS (SELECT 1 FROM aap_assets_v1.fixture_video_output_anchors AS anchor
               JOIN aap_assets_v1.fixture_native_target_retirements AS target ON target.run_id=anchor.run_id
               WHERE anchor.output_id=p_output AND anchor.run_id=p_run AND anchor.dataset_id=p_dataset)
       AND NOT EXISTS (SELECT 1 FROM public.video_derivative_outputs WHERE id=p_output) THEN RETURN true; END IF;
    SELECT COALESCE(parent_run_id,id) INTO v_root FROM yoao_fixture_v1.fixture_runs WHERE id=p_run;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs WHERE id=v_root AND parent_run_id IS NULL
      AND state IN ('open','sealing') FOR SHARE;
    IF NOT FOUND THEN RETURN false; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs
    WHERE id=p_run AND COALESCE(parent_run_id,id)=v_root AND state='sealing'
      AND ((family='owned' AND parent_run_id IS NOT NULL) OR (family='screenshot' AND parent_run_id IS NULL)) FOR SHARE;
    IF NOT FOUND OR NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_target_retirements WHERE run_id=p_run)
       OR aap_assets_v1.fixture_dataset_media_retired(p_run,p_dataset) IS DISTINCT FROM true THEN RETURN false; END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_media_births
    WHERE owner_kind='video_output' AND owner_id=p_output AND generation=0
      AND run_id=p_run AND dataset_id=p_dataset FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT * INTO v_output FROM public.video_derivative_outputs WHERE id=p_output FOR UPDATE;
    IF NOT FOUND THEN
        RETURN EXISTS (SELECT 1 FROM aap_assets_v1.fixture_video_output_anchors AS anchor
                       WHERE anchor.output_id=p_output AND anchor.run_id=p_run
                         AND anchor.dataset_id=p_dataset AND anchor.item_id=v_birth.item_id);
    END IF;
    IF v_output.state<>'deleted' OR v_output.dataset_item_id IS DISTINCT FROM v_birth.item_id
       OR EXISTS (SELECT 1 FROM public.dataset_items WHERE id<>v_birth.item_id
                  AND (video_output_id=p_output OR video_building_output_id=p_output))
       OR EXISTS (SELECT 1 FROM public.video_frame_cache WHERE output_id=p_output AND dataset_item_id<>v_birth.item_id)
       OR EXISTS (SELECT 1 FROM public.video_chunks WHERE output_id=p_output AND dataset_item_id<>v_birth.item_id)
       OR EXISTS (SELECT 1 FROM public.video_derivative_outputs WHERE metadata_output_id=p_output AND id<>p_output)
    THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_video_output_anchors(output_id,run_id,dataset_id,item_id,snapshot)
    VALUES(p_output,p_run,p_dataset,v_birth.item_id,pg_catalog.to_jsonb(v_output));
    UPDATE public.dataset_items SET
        video_output_id=CASE WHEN video_output_id=p_output THEN NULL ELSE video_output_id END,
        video_building_output_id=CASE WHEN video_building_output_id=p_output THEN NULL ELSE video_building_output_id END
    WHERE id=v_birth.item_id AND (video_output_id=p_output OR video_building_output_id=p_output);
    UPDATE public.video_frame_cache SET output_id=NULL WHERE output_id=p_output AND dataset_item_id=v_birth.item_id;
    UPDATE public.video_chunks SET output_id=NULL WHERE output_id=p_output AND dataset_item_id=v_birth.item_id;
    DELETE FROM public.video_derivative_outputs WHERE id=p_output AND state='deleted';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.retire_fixture_video_output_anchor(uuid,uuid,uuid) FROM PUBLIC;
