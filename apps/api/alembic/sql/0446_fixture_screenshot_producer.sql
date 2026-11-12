-- A screenshot producer may allocate one exact output before its durable Jobs
-- delivery exists, but the binding cannot settle without that real delivery.
CREATE TABLE aap_assets_v1.fixture_screenshot_producer_admissions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    root_admission uuid NOT NULL,
    dataset_id uuid NOT NULL,
    item_id uuid NOT NULL,
    business_pid integer NOT NULL CHECK (business_pid>0),
    state text NOT NULL CHECK (state IN ('admitted','settled','reused','rejected')),
    delivery_id uuid NOT NULL UNIQUE,
    output_id uuid,
    CHECK ((state='admitted' AND output_id IS NULL) OR (state IN ('settled','reused','rejected') AND output_id IS NOT NULL))
);
-- aap-statement-break
CREATE UNIQUE INDEX fixture_screenshot_producer_active_pid
ON aap_assets_v1.fixture_screenshot_producer_admissions(business_pid) WHERE state='admitted';
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_screenshot_producer_admissions FROM PUBLIC;
-- aap-statement-break
CREATE TABLE aap_assets_v1.fixture_screenshot_producer_candidates (
    admission_id uuid NOT NULL REFERENCES aap_assets_v1.fixture_screenshot_producer_admissions(id),
    output_id uuid NOT NULL,
    snapshot jsonb NOT NULL,
    PRIMARY KEY(admission_id,output_id)
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_screenshot_producer_candidates FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_screenshot_producer_candidate_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_screenshot_producer_candidates FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_screenshot_producer_admission()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP='DELETE' OR (NEW.id,NEW.run_id,NEW.root_admission,NEW.dataset_id,NEW.item_id,NEW.business_pid,NEW.delivery_id)
       IS DISTINCT FROM (OLD.id,OLD.run_id,OLD.root_admission,OLD.dataset_id,OLD.item_id,OLD.business_pid,OLD.delivery_id)
       OR OLD.state<>'admitted' OR NEW.state NOT IN ('settled','reused','rejected') THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_screenshot_producer_immutable';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_screenshot_producer_admission() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_screenshot_producer_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_screenshot_producer_admissions FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_screenshot_producer_admission();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.begin_fixture_screenshot_producer(
    p_run uuid,p_root_admission uuid,p_dataset uuid,p_item uuid,p_delivery uuid,p_pid integer,p_id uuid
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF p_run IS NULL OR p_root_admission IS NULL OR p_dataset IS NULL OR p_item IS NULL
       OR p_delivery IS NULL OR p_pid IS NULL OR p_pid<=0 OR p_id IS NULL
       OR aap_jobs_v1.fixture_media_principal_matches(p_run) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_source_lock_held(p_pid,p_id) IS DISTINCT FROM true THEN RETURN false; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs AS run
    JOIN yoao_fixture_v1.fixture_run_admissions AS admission ON admission.run_id=run.id AND admission.id=p_root_admission
    JOIN yoao_fixture_v1.fixture_run_resources AS dataset ON dataset.run_id=run.id AND dataset.kind='dataset'
      AND dataset.resource_id=p_dataset AND dataset.state='published'
    JOIN yoao_fixture_v1.fixture_run_resources AS item ON item.run_id=run.id AND item.kind='item'
      AND item.resource_id=p_item AND item.state='published'
    JOIN public.dataset_items AS actual ON actual.id=p_item AND actual.dataset_id=p_dataset
    JOIN aap_assets_v1.fixture_native_cache_targets AS target ON target.run_id=run.id
    WHERE run.id=p_run AND run.parent_run_id IS NULL AND run.family='screenshot'
      AND run.state='open' AND admission.state='active' AND admission.kind='screenshot_seed'
      FOR SHARE OF run,admission,dataset,item,actual,target;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_screenshot_producer_admissions(
        id,run_id,root_admission,dataset_id,item_id,delivery_id,business_pid,state)
    VALUES(p_id,p_run,p_root_admission,p_dataset,p_item,p_delivery,p_pid,'admitted');
    INSERT INTO aap_assets_v1.fixture_screenshot_producer_candidates(admission_id,output_id,snapshot)
    SELECT p_id,output.id,pg_catalog.to_jsonb(output) FROM public.video_derivative_outputs AS output
    WHERE output.dataset_item_id=p_item AND output.kind='metadata';
    RETURN true;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_fixture_screenshot_producer(uuid,uuid,uuid,uuid,uuid,integer,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_screenshot_producer_current(p_run uuid,p_dataset uuid,p_item uuid)
RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT admission.id FROM aap_assets_v1.fixture_screenshot_producer_admissions AS admission
    WHERE admission.run_id=p_run AND admission.dataset_id=p_dataset AND admission.item_id=p_item
      AND admission.state='admitted' AND admission.business_pid=pg_catalog.pg_backend_pid()
      AND aap_assets_v1.fixture_native_source_lock_held(admission.business_pid,admission.id);
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_screenshot_producer_current(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_screenshot_producer_target(p_id uuid)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object('run_id',target.run_id,'endpoint',target.endpoint,'bucket',target.bucket,
        'storage_identity',target.storage_identity,'receipt_sha256',target.receipt_sha256,
        'location_id',target.location_id,'binding_generation',target.binding_generation)
    FROM aap_assets_v1.fixture_screenshot_producer_admissions AS admission
    JOIN aap_assets_v1.fixture_native_cache_targets AS target ON target.run_id=admission.run_id
    WHERE admission.id=p_id AND admission.state='admitted'
      AND aap_jobs_v1.fixture_media_principal_matches(admission.run_id)
      AND aap_assets_v1.fixture_native_source_lock_held(admission.business_pid,admission.id);
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_screenshot_producer_target(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_fixture_screenshot_producer(p_id uuid,p_delivery uuid,p_output uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_admission aap_assets_v1.fixture_screenshot_producer_admissions%ROWTYPE;
BEGIN
    SELECT * INTO v_admission FROM aap_assets_v1.fixture_screenshot_producer_admissions WHERE id=p_id FOR UPDATE;
    IF NOT FOUND OR aap_jobs_v1.fixture_media_principal_matches(v_admission.run_id) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_source_lock_held(v_admission.business_pid,p_id) IS DISTINCT FROM true THEN RETURN false; END IF;
    IF p_delivery IS DISTINCT FROM v_admission.delivery_id THEN RETURN false; END IF;
    IF v_admission.state='settled' THEN
        RETURN v_admission.delivery_id=p_delivery AND v_admission.output_id=p_output;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_independent_media_births AS birth
        JOIN public.video_derivative_outputs AS output ON output.id=p_output
        JOIN aap_assets_v1.fixture_media_births AS owned ON owned.owner_kind='video_output' AND owned.owner_id=output.id
        WHERE birth.delivery_id=p_delivery AND birth.run_id=v_admission.run_id
          AND birth.dataset_id=v_admission.dataset_id AND birth.item_id=v_admission.item_id
          AND birth.owner_kind='dataset_item' AND birth.owner_id=v_admission.item_id
          AND birth.producer_kind='video_derivatives' AND birth.envelope -> 'args' =
              pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_array(p_output::text))
          AND birth.source IS NOT DISTINCT FROM output.original_ref
          AND output.dataset_item_id=v_admission.item_id AND output.kind='metadata'
          AND owned.run_id=v_admission.run_id AND owned.dataset_id=v_admission.dataset_id
          AND owned.item_id=v_admission.item_id AND owned.generation=0 AND owned.producer_admission_id=p_id
    ) THEN RETURN false; END IF;
    UPDATE aap_assets_v1.fixture_screenshot_producer_admissions
    SET state='settled',output_id=p_output WHERE id=p_id AND state='admitted';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_fixture_screenshot_producer(uuid,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_explicit_media_producer(
    p_run uuid,p_dataset uuid,p_item uuid,p_task uuid,p_delivery uuid,p_producer text
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_screenshot_producer_admissions AS admission
        WHERE p_producer='video_derivatives' AND p_task IS NULL
          AND admission.run_id=p_run AND admission.dataset_id=p_dataset AND admission.item_id=p_item
          AND admission.delivery_id=p_delivery AND admission.state='admitted'
          AND admission.business_pid=pg_catalog.pg_backend_pid()
          AND aap_assets_v1.fixture_native_source_lock_held(admission.business_pid,admission.id)
    ) OR EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_legacy_birth_admissions AS admission
        WHERE admission.run_id=p_run AND admission.dataset_id IS NOT DISTINCT FROM p_dataset
          AND admission.item_id IS NOT DISTINCT FROM p_item AND admission.task_id IS NOT DISTINCT FROM p_task
          AND admission.delivery_id=p_delivery AND admission.state='admitted'
          AND admission.business_pid=pg_catalog.pg_backend_pid()
          AND aap_assets_v1.fixture_native_source_lock_held(admission.business_pid,admission.id)
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_explicit_media_producer(uuid,uuid,uuid,uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.reuse_fixture_screenshot_producer(p_id uuid,p_output uuid)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_admission aap_assets_v1.fixture_screenshot_producer_admissions%ROWTYPE;
BEGIN
    SELECT * INTO v_admission FROM aap_assets_v1.fixture_screenshot_producer_admissions WHERE id=p_id FOR UPDATE;
    IF NOT FOUND OR aap_jobs_v1.fixture_media_principal_matches(v_admission.run_id) IS DISTINCT FROM true
       OR aap_assets_v1.fixture_native_source_lock_held(v_admission.business_pid,p_id) IS DISTINCT FROM true THEN RETURN 'blocked'; END IF;
    IF v_admission.state='reused' AND v_admission.output_id=p_output THEN RETURN 'reused'; END IF;
    IF v_admission.state='rejected' AND v_admission.output_id=p_output THEN RETURN 'unqualified'; END IF;
    IF v_admission.state<>'admitted' OR EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_births WHERE producer_admission_id=p_id
    ) OR EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_independent_media_births WHERE delivery_id=v_admission.delivery_id
    ) OR NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_screenshot_producer_candidates AS candidate
        JOIN public.video_derivative_outputs AS output ON output.id=candidate.output_id
        WHERE candidate.admission_id=p_id AND output.id=p_output
          AND output.dataset_item_id=v_admission.item_id AND output.kind='metadata'
          AND output.state IN ('building','ready')
          AND output.dataset_item_id::text=candidate.snapshot ->> 'dataset_item_id'
          AND output.kind=candidate.snapshot ->> 'kind'
          AND output.original_ref IS NOT DISTINCT FROM candidate.snapshot -> 'original_ref'
          AND output.auth_ref IS NOT DISTINCT FROM candidate.snapshot -> 'auth_ref'
          AND output.storage_ref IS NOT DISTINCT FROM candidate.snapshot -> 'storage_ref'
          AND output.metadata_output_id IS NOT DISTINCT FROM (candidate.snapshot ->> 'metadata_output_id')::uuid
          AND aap_assets_v1.media_source_binding('dataset_item',v_admission.item_id,output.original_ref)
              @> pg_catalog.jsonb_build_object('ownership','fixture','run_id',v_admission.run_id,
                                               'dataset_id',v_admission.dataset_id,'item_id',v_admission.item_id)
    ) THEN RETURN 'blocked'; END IF;
    IF NOT EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_births AS birth
        WHERE birth.owner_kind='video_output' AND birth.owner_id=p_output AND birth.generation=0
          AND birth.run_id=v_admission.run_id AND birth.dataset_id=v_admission.dataset_id
          AND birth.item_id=v_admission.item_id
    ) THEN
        UPDATE aap_assets_v1.fixture_screenshot_producer_admissions
        SET state='rejected',output_id=p_output WHERE id=p_id AND state='admitted';
        RETURN CASE WHEN FOUND THEN 'unqualified' ELSE 'blocked' END;
    END IF;
    UPDATE aap_assets_v1.fixture_screenshot_producer_admissions
    SET state='reused',output_id=p_output WHERE id=p_id AND state='admitted';
    RETURN CASE WHEN FOUND THEN 'reused' ELSE 'blocked' END;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.reuse_fixture_screenshot_producer(uuid,uuid) FROM PUBLIC;
