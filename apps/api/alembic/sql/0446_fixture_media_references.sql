-- No new business pointer may attach a sealed fixture output or attach one
-- owned by a different item. Detaching existing pointers remains possible.
CREATE FUNCTION aap_assets_v1.guard_fixture_media_reference()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_new jsonb := pg_catalog.to_jsonb(NEW);
    v_old jsonb := CASE WHEN TG_OP='UPDATE' THEN pg_catalog.to_jsonb(OLD) ELSE '{}'::jsonb END;
    v_columns text[];
    v_kinds text[];
    v_column text;
    v_kind text;
    v_id uuid;
    v_item uuid;
    v_index integer;
    v_birth aap_assets_v1.fixture_media_births%ROWTYPE;
    v_state text;
    v_root_state text;
BEGIN
    IF TG_TABLE_NAME='dataset_items' THEN
        v_columns := ARRAY['thumbnail_output_id','video_output_id','video_building_output_id'];
        v_kinds := ARRAY['thumbnail','video_output','video_output'];
        v_item := (v_new ->> 'id')::uuid;
    ELSIF TG_TABLE_NAME='tasks' THEN
        v_columns := ARRAY['thumbnail_output_id']; v_kinds := ARRAY['thumbnail'];
        v_item := (v_new ->> 'dataset_item_id')::uuid;
    ELSIF TG_TABLE_NAME IN ('video_frame_cache','video_chunks') THEN
        v_columns := ARRAY['output_id']; v_kinds := ARRAY['video_output'];
        v_item := (v_new ->> 'dataset_item_id')::uuid;
    ELSIF TG_TABLE_NAME='video_derivative_outputs' THEN
        v_columns := ARRAY['metadata_output_id']; v_kinds := ARRAY['video_output'];
        v_item := (v_new ->> 'dataset_item_id')::uuid;
    ELSIF TG_TABLE_NAME='dataset_source_previews' THEN
        v_columns := ARRAY['thumbnail_output_id']; v_kinds := ARRAY['thumbnail'];
    ELSIF TG_TABLE_NAME='image_pyramid_generations' THEN
        v_columns := ARRAY['output_id']; v_kinds := ARRAY['pyramid_output'];
    ELSE
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_reference_invalid';
    END IF;
    FOR v_index IN 1..pg_catalog.cardinality(v_columns) LOOP
        v_column := v_columns[v_index];
        v_kind := v_kinds[v_index];
        IF v_new ->> v_column IS NULL THEN CONTINUE; END IF;
        v_id := (v_new ->> v_column)::uuid;
        SELECT * INTO v_birth FROM aap_assets_v1.fixture_media_births
        WHERE owner_id=v_id AND generation=0
          AND owner_kind IN ('thumbnail','pyramid_output','video_output');
        IF NOT FOUND THEN CONTINUE; END IF;
        IF TG_OP='UPDATE' AND v_new -> v_column IS NOT DISTINCT FROM v_old -> v_column
           AND v_new -> 'dataset_item_id' IS NOT DISTINCT FROM v_old -> 'dataset_item_id'
           AND v_new -> 'dataset_id' IS NOT DISTINCT FROM v_old -> 'dataset_id'
           AND v_new -> 'id' IS NOT DISTINCT FROM v_old -> 'id'
           AND v_new -> 'asset_id' IS NOT DISTINCT FROM v_old -> 'asset_id'
           AND v_new -> 'generation' IS NOT DISTINCT FROM v_old -> 'generation' THEN
            CONTINUE;
        END IF;
        IF v_birth.owner_kind <> v_kind OR TG_TABLE_NAME='dataset_source_previews'
           OR (TG_TABLE_NAME <> 'image_pyramid_generations' AND v_item IS DISTINCT FROM v_birth.item_id)
           OR (TG_TABLE_NAME='dataset_items' AND (v_new ->> 'dataset_id')::uuid IS DISTINCT FROM v_birth.dataset_id)
           OR (TG_TABLE_NAME='tasks' AND v_birth.task_id IS NOT NULL
               AND v_birth.task_id IS DISTINCT FROM (v_new ->> 'id')::uuid)
           OR (TG_TABLE_NAME='tasks' AND NOT EXISTS (
               SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS task
               WHERE task.run_id=v_birth.run_id AND task.kind='task'
                 AND task.resource_id=(v_new ->> 'id')::uuid AND task.state='published'))
           OR (TG_TABLE_NAME='image_pyramid_generations' AND NOT EXISTS (
               SELECT 1 FROM public.image_pyramid_outputs AS output
               WHERE output.id=v_id AND output.asset_id=(v_new ->> 'asset_id')::uuid
                 AND output.generation=(v_new ->> 'generation')::integer
           )) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_reference_mismatch';
        END IF;
        SELECT run.state,root.state INTO v_state,v_root_state
        FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_runs AS root ON root.id=COALESCE(run.parent_run_id,run.id)
        WHERE run.id=v_birth.run_id FOR SHARE OF run,root;
        IF v_state IS DISTINCT FROM 'open' OR v_root_state IS DISTINCT FROM 'open' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_reference_closed';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_media_reference() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_item_reference BEFORE INSERT OR UPDATE OF id,dataset_id,thumbnail_output_id,video_output_id,video_building_output_id
ON public.dataset_items FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_reference();
-- aap-statement-break
CREATE TRIGGER fixture_media_task_reference BEFORE INSERT OR UPDATE OF id,thumbnail_output_id,dataset_item_id
ON public.tasks FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_reference();
-- aap-statement-break
CREATE TRIGGER fixture_media_frame_reference BEFORE INSERT OR UPDATE OF output_id,dataset_item_id
ON public.video_frame_cache FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_reference();
-- aap-statement-break
CREATE TRIGGER fixture_media_chunk_reference BEFORE INSERT OR UPDATE OF output_id,dataset_item_id
ON public.video_chunks FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_reference();
-- aap-statement-break
CREATE TRIGGER fixture_media_preview_reference BEFORE INSERT OR UPDATE OF thumbnail_output_id
ON public.dataset_source_previews FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_reference();
-- aap-statement-break
CREATE TRIGGER fixture_media_pyramid_reference BEFORE INSERT OR UPDATE OF asset_id,generation,output_id
ON public.image_pyramid_generations FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_reference();
-- aap-statement-break
CREATE TRIGGER fixture_video_metadata_reference BEFORE INSERT OR UPDATE OF metadata_output_id,dataset_item_id
ON public.video_derivative_outputs FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_reference();
