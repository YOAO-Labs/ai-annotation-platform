-- Preserve the actual Images/Media job owner independently of mutable job state.
-- A sealed Dataset cannot acquire a new native lease or a new item-scoped job.
CREATE TABLE aap_assets_v1.fixture_native_media_jobs (
    family text NOT NULL CHECK (family IN ('image','media')),
    job_id uuid NOT NULL,
    run_id uuid NOT NULL,
    dataset_id uuid,
    item_id uuid,
    job_kind text NOT NULL,
    owner_kind text NOT NULL,
    owner_id uuid NOT NULL,
    selection jsonb,
    CHECK ((dataset_id IS NOT NULL AND ((item_id IS NULL) = (selection IS NOT NULL))) OR
           (dataset_id IS NULL AND item_id IS NULL AND selection IS NULL AND owner_kind='task')),
    PRIMARY KEY (family,job_id)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_media_jobs FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_media_job_evidence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_job_immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_media_job_evidence() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_media_job_evidence_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_media_jobs FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_media_job_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.capture_fixture_native_media_job()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_family text;
    v_owner_kind text;
    v_owner_id uuid;
    v_item uuid;
    v_dataset uuid;
    v_run uuid;
    v_root uuid;
    v_run_state text;
    v_root_state text;
    v_birth aap_assets_v1.fixture_native_media_jobs%ROWTYPE;
    v_external aap_assets_v1.fixture_external_media_jobs%ROWTYPE;
    v_owned boolean := false;
    v_selection jsonb;
    v_scan boolean := false;
    v_new_lease boolean;
    v_new_generation boolean;
BEGIN
    v_family := CASE TG_TABLE_NAME WHEN 'asset_image_jobs' THEN 'image' ELSE 'media' END;
    IF TG_OP='UPDATE' THEN
        IF NEW.job_id IS DISTINCT FROM OLD.job_id AND EXISTS (
            SELECT 1 FROM aap_assets_v1.fixture_native_media_jobs AS birth
            WHERE birth.family=v_family AND birth.job_id=OLD.job_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_job_immutable';
        END IF;
    END IF;
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_native_media_jobs
    WHERE family=v_family AND job_id=NEW.job_id;
    SELECT * INTO v_external FROM aap_assets_v1.fixture_external_media_jobs
    WHERE family=v_family AND job_id=NEW.job_id;
    IF v_external.job_id IS NOT NULL AND v_birth.job_id IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_owner_ambiguous';
    END IF;
    IF v_birth.job_id IS NOT NULL OR v_external.job_id IS NOT NULL THEN
        IF TG_OP='INSERT' THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_job_reused';
        ELSIF NEW.job_id IS DISTINCT FROM OLD.job_id THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_job_reused';
        END IF;
    END IF;
    IF v_family='image' THEN
        IF NEW.kind='image_backfill'
           AND NEW.spec #>> '{selection,owner_kind}'='dataset_item'
           AND NEW.spec #>> '{selection,dataset_id}' IS NOT NULL THEN
            v_selection := NEW.spec -> 'selection';
            v_dataset := (v_selection ->> 'dataset_id')::uuid;
            v_scan := true;
        END IF;
    ELSE
        IF NEW.kind='video_backfill' AND NEW.backfill_selection ->> 'dataset_id' IS NOT NULL THEN
            v_selection := NEW.backfill_selection;
            v_dataset := (v_selection ->> 'dataset_id')::uuid;
            v_scan := true;
        END IF;
    END IF;
    IF v_scan THEN
        v_owner_kind := 'dataset';
        v_owner_id := v_dataset;
    ELSIF v_family='image' THEN
        v_owner_kind := NEW.owner_kind;
        v_owner_id := NEW.owner_id;
        IF NEW.owner_kind='dataset_item' THEN
            v_item := NEW.owner_id;
        ELSIF NEW.owner_kind='task' THEN
            SELECT COALESCE(task.dataset_item_id,(
                SELECT link.dataset_item_id FROM aap_labeling_v1.task_dataset_item_link_rows AS link
                WHERE link.task_id=task.id AND link.role='primary_lidar'
            )) INTO v_item FROM aap_labeling_v1.task_rows AS task WHERE task.id=NEW.owner_id;
            IF NOT FOUND AND EXISTS (
                SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                WHERE resource.kind='task' AND resource.resource_id=NEW.owner_id
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_owner_missing';
            END IF;
        END IF;
    ELSE
        v_owner_kind := 'dataset_item';
        v_owner_id := NEW.dataset_item_id;
        v_item := NEW.dataset_item_id;
    END IF;
    IF v_birth.job_id IS NOT NULL AND
       (v_item,v_owner_kind,v_owner_id,NEW.kind,v_selection)
       IS DISTINCT FROM
       (v_birth.item_id,v_birth.owner_kind,v_birth.owner_id,v_birth.job_kind,v_birth.selection) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_owner_changed';
    END IF;
    IF NOT v_scan AND v_item IS NULL THEN
        IF v_family<>'image' OR v_owner_kind<>'task' THEN RETURN NEW; END IF;
        SELECT resource.run_id INTO v_run FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind='task' AND resource.resource_id=v_owner_id;
        IF NOT FOUND THEN
            IF v_birth.job_id IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_owner_changed';
            END IF;
            RETURN NEW;
        END IF;
    ELSIF NOT v_scan THEN
        SELECT item.dataset_id INTO v_dataset FROM public.dataset_items AS item WHERE item.id=v_item;
        IF NOT FOUND THEN
            IF v_birth.job_id IS NOT NULL OR EXISTS (
                SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
                WHERE resource.kind='item' AND resource.resource_id=v_item
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_owner_missing';
            END IF;
            RETURN NEW;
        END IF;
    END IF;
    IF v_dataset IS NOT NULL THEN
        SELECT resource.run_id INTO v_run FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind='dataset' AND resource.resource_id=v_dataset;
        IF NOT FOUND THEN
            IF v_birth.job_id IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_owner_changed';
            END IF;
            RETURN NEW;
        END IF;
    END IF;
    IF v_birth.job_id IS NOT NULL AND (v_birth.run_id,v_birth.dataset_id)
       IS DISTINCT FROM (v_run,v_dataset) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_owner_changed';
    END IF;
    IF v_external.job_id IS NOT NULL AND (v_external.run_id,v_external.dataset_id,
       v_external.item_id,v_external.owner_kind,v_external.owner_id,v_external.job_kind,v_external.selection)
       IS DISTINCT FROM (v_run,v_dataset,v_item,v_owner_kind,v_owner_id,NEW.kind,v_selection) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_owner_changed';
    END IF;
    IF v_birth.job_id IS NULL AND v_external.job_id IS NULL AND TG_OP='UPDATE' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_job_untracked';
    END IF;
    IF TG_OP='INSERT' THEN
        v_new_lease := NEW.lease_token IS NOT NULL;
        v_new_generation := false;
    ELSE
        v_new_lease := NEW.lease_token IS NOT NULL
            AND NEW.lease_token IS DISTINCT FROM OLD.lease_token;
        v_new_generation := NEW.generation IS DISTINCT FROM OLD.generation;
    END IF;
    IF (v_birth.job_id IS NULL AND v_external.job_id IS NULL) OR v_new_lease OR v_new_generation THEN
        SELECT COALESCE(run.parent_run_id,run.id) INTO v_root
        FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id=v_run;
        SELECT root.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS root
        WHERE root.id=v_root AND root.parent_run_id IS NULL FOR SHARE;
        SELECT run.state INTO v_run_state FROM yoao_fixture_v1.fixture_runs AS run
        WHERE run.id=v_run AND COALESCE(run.parent_run_id,run.id)=v_root FOR SHARE;
        IF v_run_state IS DISTINCT FROM 'open' OR v_root_state IS DISTINCT FROM 'open'
           OR NOT (
               (v_dataset IS NULL AND EXISTS (
                   SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS task
                   WHERE task.run_id=v_run AND task.kind='task'
                     AND task.resource_id=v_owner_id AND task.state='published'
               )) OR (v_dataset IS NOT NULL AND EXISTS (
                   SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS dataset
                   WHERE dataset.run_id=v_run AND dataset.kind='dataset'
                     AND dataset.resource_id=v_dataset AND dataset.state='published'
                     AND (v_scan OR EXISTS (
                        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS item
                        WHERE item.run_id=dataset.run_id AND item.kind='item'
                          AND item.resource_id=v_item AND item.state='published'
                     ))
               ))
           ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_run_closed';
        END IF;
    END IF;
    IF v_external.job_id IS NOT NULL THEN RETURN NEW; END IF;
    IF v_birth.job_id IS NULL THEN
        v_owned := v_family='image' AND v_dataset IS NOT NULL
            AND aap_assets_v1.fixture_native_image_admission_current(v_run,v_dataset);
        IF NOT v_owned THEN
            IF v_dataset IS NULL OR NOT (
                (v_family='media' AND (
                    pg_catalog.has_function_privilege(SESSION_USER,
                        'aap_assets_v1.request_video_media(uuid,uuid,uuid,jsonb,boolean)','EXECUTE')
                    OR pg_catalog.has_function_privilege(SESSION_USER,
                        'aap_assets_v1.advance_video_backfill(uuid,bigint,uuid,integer)','EXECUTE')))
                OR (v_family='image' AND (
                    pg_catalog.has_function_privilege(SESSION_USER,
                        'aap_assets_v1.prepare_image_media(uuid,uuid,uuid,uuid,text,text,jsonb,boolean)','EXECUTE')
                    OR pg_catalog.has_function_privilege(SESSION_USER,
                        'aap_assets_v1.advance_image_backfill(uuid,bigint,uuid,integer,jsonb)','EXECUTE')))
            ) THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_job_untracked'; END IF;
            INSERT INTO aap_assets_v1.fixture_external_media_jobs(
                family,job_id,run_id,dataset_id,item_id,job_kind,owner_kind,owner_id,selection,producer_oid)
            VALUES(v_family,NEW.job_id,v_run,v_dataset,v_item,NEW.kind,v_owner_kind,v_owner_id,v_selection,
                   (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=SESSION_USER));
            RETURN NEW;
        END IF;
    END IF;
    IF v_new_lease THEN
        IF v_birth.job_id IS NULL OR aap_assets_v1.consume_fixture_native_claim(
            v_family,NEW.job_id,NEW.generation,NEW.lease_token
        ) IS DISTINCT FROM true THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_claim_untracked';
        END IF;
    END IF;
    IF v_birth.job_id IS NULL THEN
        INSERT INTO aap_assets_v1.fixture_native_media_jobs
            (family,job_id,run_id,dataset_id,item_id,job_kind,owner_kind,owner_id,selection)
        VALUES (v_family,NEW.job_id,v_run,v_dataset,v_item,NEW.kind,v_owner_kind,v_owner_id,v_selection);
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.capture_fixture_native_media_job() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_image_job_birth AFTER INSERT ON public.asset_image_jobs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.capture_fixture_native_media_job();
-- aap-statement-break
CREATE TRIGGER fixture_native_image_job_guard BEFORE UPDATE OF
job_id,kind,owner_kind,owner_id,lease_token,generation,spec ON public.asset_image_jobs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.capture_fixture_native_media_job();
-- aap-statement-break
CREATE TRIGGER fixture_native_video_job_birth AFTER INSERT ON public.asset_media_jobs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.capture_fixture_native_media_job();
-- aap-statement-break
CREATE TRIGGER fixture_native_video_job_guard BEFORE UPDATE OF
job_id,kind,dataset_item_id,lease_token,generation,backfill_selection ON public.asset_media_jobs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.capture_fixture_native_media_job();
-- Keep the real business Job anchor until its exact physical outputs/readers
-- have settled.  Immutable birth, execution, and dispatch evidence outlives it.
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_media_job_delete()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_birth aap_assets_v1.fixture_native_media_jobs%ROWTYPE;
BEGIN
    SELECT * INTO v_birth FROM aap_assets_v1.fixture_native_media_jobs
    WHERE family=CASE TG_TABLE_NAME WHEN 'asset_image_jobs' THEN 'image' ELSE 'media' END
      AND job_id=OLD.job_id;
    IF FOUND AND (
        aap_assets_v1.fixture_native_outputs_retired(v_birth.run_id,v_birth.dataset_id) IS DISTINCT FROM true
        OR NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_job_retirements AS retired
                       WHERE retired.family=v_birth.family AND retired.job_id=v_birth.job_id
                         AND retired.run_id=v_birth.run_id AND retired.dataset_id=v_birth.dataset_id)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_media_unsettled';
    END IF;
    RETURN OLD;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_media_job_delete() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_image_job_delete BEFORE DELETE ON public.asset_image_jobs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_media_job_delete();
-- aap-statement-break
CREATE TRIGGER fixture_native_video_job_delete BEFORE DELETE ON public.asset_media_jobs
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_media_job_delete();
