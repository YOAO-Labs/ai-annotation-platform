-- Existing active fixture Datasets and Tasks may already have unrecorded media work.
-- Do not fabricate birth evidence or migrate across an unproved execution.
DO $function$
BEGIN
    IF EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        JOIN yoao_fixture_v1.fixture_runs AS run ON run.id = resource.run_id
        WHERE resource.kind IN ('dataset','task') AND run.state <> 'retired'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'fixture_media_birth_upgrade_unsettled';
    END IF;
END;
$function$;
-- aap-statement-break
-- Independent media owners survive their business rows.  Capture the actual
-- owner at birth while the exact Dataset and both fixture run fences are open.
CREATE TABLE aap_assets_v1.fixture_media_births (
    owner_kind text NOT NULL CHECK (owner_kind IN
        ('thumbnail', 'pyramid_asset', 'pyramid_generation', 'pyramid_output', 'video_output')),
    owner_id uuid NOT NULL,
    generation integer NOT NULL DEFAULT 0 CHECK (generation >= 0),
    run_id uuid NOT NULL,
    dataset_id uuid,
    item_id uuid,
    task_id uuid,
    producer_admission_id uuid,
    PRIMARY KEY (owner_kind, owner_id, generation),
    CHECK ((owner_kind = 'pyramid_generation') = (generation > 0)),
    CHECK ((dataset_id IS NOT NULL AND item_id IS NOT NULL) OR
           (dataset_id IS NULL AND item_id IS NULL AND task_id IS NOT NULL))
);
-- aap-statement-break
CREATE INDEX fixture_media_births_dataset ON aap_assets_v1.fixture_media_births
    (run_id,dataset_id,owner_kind,owner_id,generation);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_media_births FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_birth_immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_media_birth_immutable
BEFORE UPDATE OR DELETE ON aap_assets_v1.fixture_media_births
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.record_fixture_media_birth()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_kind text;
    v_id uuid;
    v_generation integer := 0;
    v_item uuid;
    v_task uuid;
    v_dataset uuid;
    v_run uuid;
    v_root uuid;
    v_state text;
    v_claimed boolean;
    v_target_run uuid;
    v_screenshot_admission uuid;
    v_root_state text;
BEGIN
    IF TG_TABLE_NAME = 'thumbnail_outputs' THEN
        IF NEW.owner_kind = 'dataset_thumbnail' THEN
            v_item := NEW.owner_id;
        ELSIF NEW.owner_kind = 'task_thumbnail' THEN
            v_task := NEW.owner_id;
        ELSE
            RETURN NEW;
        END IF;
        v_kind := 'thumbnail';
        v_id := NEW.id;
    ELSIF TG_TABLE_NAME = 'image_pyramid_assets' THEN
        v_item := NEW.dataset_item_id;
        v_task := NEW.task_id;
        v_kind := 'pyramid_asset';
        v_id := NEW.id;
    ELSIF TG_TABLE_NAME IN ('image_pyramid_generations', 'image_pyramid_outputs') THEN
        SELECT asset.dataset_item_id, asset.task_id INTO v_item, v_task
        FROM public.image_pyramid_assets AS asset WHERE asset.id = NEW.asset_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_owner_missing';
        END IF;
        IF TG_TABLE_NAME = 'image_pyramid_generations' THEN
            v_kind := 'pyramid_generation';
            v_id := NEW.asset_id;
            v_generation := NEW.generation;
        ELSE
            v_kind := 'pyramid_output';
            v_id := NEW.id;
        END IF;
    ELSIF TG_TABLE_NAME = 'video_derivative_outputs' THEN
        v_item := NEW.dataset_item_id;
        v_kind := 'video_output';
        v_id := NEW.id;
    ELSE
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_owner_invalid';
    END IF;
    IF v_task IS NOT NULL THEN
        SELECT COALESCE(task.dataset_item_id,(
            SELECT link.dataset_item_id FROM aap_labeling_v1.task_dataset_item_link_rows AS link
            WHERE link.task_id=task.id AND link.role='primary_lidar'
        )) INTO v_item FROM aap_labeling_v1.task_rows AS task WHERE task.id = v_task;
        IF NOT FOUND THEN
            IF EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                       WHERE resource.kind = 'task' AND resource.resource_id = v_task) THEN
                RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_owner_missing';
            END IF;
            RETURN NEW;
        END IF;
    END IF;
    IF v_item IS NULL THEN
        IF v_task IS NULL THEN RETURN NEW; END IF;
        SELECT resource.run_id INTO v_run FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind='task' AND resource.resource_id=v_task;
        IF NOT FOUND THEN RETURN NEW; END IF;
        IF v_kind IN ('thumbnail','pyramid_output','video_output') THEN
            SELECT target.run_id INTO v_target_run FROM aap_assets_v1.fixture_native_cache_targets AS target
            WHERE target.location_id::text=pg_catalog.to_jsonb(NEW) #>> '{storage_ref,location_id}'
               OR target.storage_identity=pg_catalog.to_jsonb(NEW) #>> '{storage_ref,storage_identity}';
            IF NOT FOUND THEN RETURN NEW; END IF;
            IF v_target_run IS DISTINCT FROM v_run OR
               aap_assets_v1.fixture_legacy_birth_admission_current(v_run,NULL,NULL,v_task) IS DISTINCT FROM true THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_birth_untracked';
            END IF;
        ELSIF aap_assets_v1.fixture_legacy_birth_admission_current(v_run,NULL,NULL,v_task) IS DISTINCT FROM true THEN
            RETURN NEW;
        END IF;
        SELECT COALESCE(run.parent_run_id,run.id) INTO v_root
        FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id=v_run;
        SELECT root.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS root
        WHERE root.id=v_root AND root.parent_run_id IS NULL FOR SHARE;
        SELECT run.state INTO v_state FROM yoao_fixture_v1.fixture_runs AS run
        WHERE run.id=v_run AND COALESCE(run.parent_run_id,run.id)=v_root FOR SHARE;
        IF v_state NOT IN ('open','sealing') OR v_root_state NOT IN ('open','sealing')
           OR NOT EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                          WHERE resource.run_id=v_run AND resource.kind='task'
                            AND resource.resource_id=v_task AND resource.state='published') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_birth_closed';
        END IF;
        INSERT INTO aap_assets_v1.fixture_media_births
            (owner_kind,owner_id,generation,run_id,task_id)
        VALUES (v_kind,v_id,v_generation,v_run,v_task);
        RETURN NEW;
    END IF;
    SELECT item.dataset_id INTO v_dataset FROM public.dataset_items AS item WHERE item.id = v_item;
    IF NOT FOUND THEN
        IF EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS item
                   WHERE item.kind = 'item' AND item.resource_id = v_item) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_owner_missing';
        END IF;
        RETURN NEW;
    END IF;
    SELECT dataset.run_id INTO v_run FROM yoao_fixture_v1.fixture_run_resources AS dataset
    WHERE dataset.kind = 'dataset' AND dataset.resource_id = v_dataset;
    IF NOT FOUND THEN RETURN NEW; END IF;
    IF v_kind='video_output' AND pg_catalog.to_jsonb(NEW) ->> 'kind'='metadata' THEN
        v_screenshot_admission := aap_assets_v1.fixture_screenshot_producer_current(v_run,v_dataset,v_item);
    END IF;
    v_claimed := aap_assets_v1.fixture_legacy_birth_admission_current(v_run,v_dataset,v_item,NULL)
        OR v_screenshot_admission IS NOT NULL;
    IF v_kind IN ('pyramid_asset','pyramid_generation') THEN
        v_claimed := v_claimed OR aap_assets_v1.fixture_native_admission_current('image',v_run,v_dataset);
        IF NOT v_claimed THEN RETURN NEW; END IF;
    ELSE
        SELECT target.run_id INTO v_target_run FROM aap_assets_v1.fixture_native_cache_targets AS target
        WHERE target.location_id::text=pg_catalog.to_jsonb(NEW) #>> '{storage_ref,location_id}'
           OR target.storage_identity=pg_catalog.to_jsonb(NEW) #>> '{storage_ref,storage_identity}';
        IF NOT FOUND THEN
            IF v_claimed THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_target_untracked'; END IF;
            RETURN NEW;
        END IF;
        IF v_target_run IS DISTINCT FROM v_run OR NOT v_claimed THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_media_birth_untracked';
        END IF;
    END IF;
    SELECT COALESCE(run.parent_run_id, run.id) INTO v_root
    FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id = v_run;
    SELECT root.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS root
    WHERE root.id = v_root AND root.parent_run_id IS NULL FOR SHARE;
    SELECT run.state INTO v_state FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id = v_run AND COALESCE(run.parent_run_id, run.id) = v_root FOR SHARE;
    IF v_state NOT IN ('open','sealing') OR v_root_state NOT IN ('open','sealing')
       OR NOT EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS dataset
           JOIN yoao_fixture_v1.fixture_run_resources AS item ON item.run_id = dataset.run_id
           WHERE dataset.run_id = v_run AND dataset.kind = 'dataset'
             AND dataset.resource_id = v_dataset AND dataset.state = 'published'
             AND item.kind = 'item' AND item.resource_id = v_item AND item.state = 'published'
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_media_birth_closed';
    END IF;
    INSERT INTO aap_assets_v1.fixture_media_births
        (owner_kind, owner_id, generation, run_id, dataset_id, item_id, producer_admission_id)
    VALUES (v_kind, v_id, v_generation, v_run, v_dataset, v_item, v_screenshot_admission);
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.record_fixture_media_birth() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_thumbnail_birth AFTER INSERT ON public.thumbnail_outputs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.record_fixture_media_birth();
-- aap-statement-break
CREATE TRIGGER fixture_pyramid_asset_birth AFTER INSERT ON public.image_pyramid_assets
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.record_fixture_media_birth();
-- aap-statement-break
CREATE TRIGGER fixture_pyramid_generation_birth AFTER INSERT ON public.image_pyramid_generations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.record_fixture_media_birth();
-- aap-statement-break
CREATE TRIGGER fixture_pyramid_output_birth AFTER INSERT ON public.image_pyramid_outputs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.record_fixture_media_birth();
-- aap-statement-break
CREATE TRIGGER fixture_video_output_birth AFTER INSERT ON public.video_derivative_outputs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.record_fixture_media_birth();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_pyramid_generation_owned(
    p_run_id uuid,p_dataset_id uuid,p_item_id uuid,p_asset_id uuid,p_generation integer
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_births AS birth
        JOIN public.image_pyramid_assets AS asset ON asset.id=birth.owner_id
        JOIN public.image_pyramid_generations AS generation
          ON generation.asset_id=asset.id AND generation.generation=birth.generation
        WHERE birth.owner_kind='pyramid_generation' AND birth.owner_id=p_asset_id
          AND birth.generation=p_generation AND birth.run_id=p_run_id
          AND birth.dataset_id=p_dataset_id AND birth.item_id=p_item_id
          AND asset.dataset_item_id=p_item_id AND asset.task_id IS NULL
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_pyramid_generation_owned(uuid,uuid,uuid,uuid,integer) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_media_child_source_matches(
    p_run_id uuid,p_dataset_id uuid,p_item_id uuid,p_cutoff timestamptz,p_original jsonb
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        JOIN yoao_fixture_v1.fixture_run_resources AS resource
          ON resource.run_id=p_run_id AND resource.kind='item'
         AND resource.resource_id=item.id AND resource.state='published'
        JOIN public.dataset_assets AS asset ON asset.dataset_item_id=item.id
        JOIN public.dataset_asset_locations AS location ON location.asset_id=asset.id
        WHERE item.id=p_item_id AND item.dataset_id=p_dataset_id
          AND item.file_type='image' AND item.created_at <= p_cutoff
          AND asset.id::text=p_original ->> 'content_id'
          AND asset.checksum_value=p_original ->> 'sha256'
          AND asset.size_bytes::text=p_original ->> 'size_bytes'
          AND location.location_id::text=p_original ->> 'location_id'
          AND location.role='managed_original' AND location.reference_state='present'
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_media_child_source_matches(uuid,uuid,uuid,timestamptz,jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_media_owner_exists(p_kind text,p_owner_id uuid)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_kind='dataset_item' THEN
        RETURN EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources
                       WHERE kind='item' AND resource_id=p_owner_id);
    ELSIF p_kind='task' THEN
        RETURN EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_run_resources
                       WHERE kind='task' AND resource_id=p_owner_id)
            OR EXISTS (
                SELECT 1 FROM aap_labeling_v1.task_rows AS task
                JOIN yoao_fixture_v1.fixture_run_resources AS resource
                  ON resource.kind='item' AND resource.resource_id=task.dataset_item_id
                WHERE task.id=p_owner_id
            );
    ELSIF p_kind='video_output' THEN
        RETURN EXISTS (SELECT 1 FROM aap_assets_v1.fixture_media_births
                       WHERE owner_kind='video_output' AND owner_id=p_owner_id AND generation=0);
    END IF;
    RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_media_kind_invalid';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_media_owner_exists(text,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_media_item_in_cutoff(
    p_run_id uuid,p_dataset_id uuid,p_item_id uuid,p_cutoff timestamptz
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        JOIN yoao_fixture_v1.fixture_run_resources AS resource
          ON resource.run_id=p_run_id AND resource.kind='item'
         AND resource.resource_id=item.id AND resource.state='published'
        WHERE item.id=p_item_id AND item.dataset_id=p_dataset_id
          AND item.created_at <= p_cutoff
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_media_item_in_cutoff(uuid,uuid,uuid,timestamptz) FROM PUBLIC;
-- The ordinary grace-based collectors cannot retire a fixture's exact
-- versions or its still-active original reader.  Those owners remain with
-- the scoped fixture retirement path; unrelated business GC is unchanged.
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_media_ordinary_gc(
    p_kind text,p_owner_id uuid,p_generation integer
) RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_kind IS NULL OR p_owner_id IS NULL OR p_generation IS NULL
       OR p_kind NOT IN ('thumbnail','pyramid_generation','pyramid_output','video_output')
       OR p_generation < 0 OR (p_kind='pyramid_generation') <> (p_generation > 0) THEN
        RAISE EXCEPTION USING ERRCODE='22023', MESSAGE='fixture_media_kind_invalid';
    END IF;
    RETURN NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_births
        WHERE owner_kind=p_kind AND owner_id=p_owner_id AND generation=p_generation
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_media_ordinary_gc(text,uuid,integer) FROM PUBLIC;
