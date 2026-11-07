-- An independent legacy delivery is born at its real Jobs producer transaction,
-- not inferred from a later worker, Job status or missing process.
CREATE TABLE aap_assets_v1.fixture_independent_media_births (
    delivery_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    dataset_id uuid,
    item_id uuid,
    owner_kind text NOT NULL CHECK (owner_kind IN ('dataset_item','task')),
    owner_id uuid NOT NULL,
    producer_kind text NOT NULL CHECK (producer_kind IN
        ('pyramid','thumbnail','task_thumbnail','video_metadata','video_derivatives')),
    queue text NOT NULL,
    source jsonb NOT NULL,
    envelope jsonb NOT NULL,
    CHECK ((dataset_id IS NULL) = (item_id IS NULL))
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_independent_media_births FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_independent_media_birth_immutable
BEFORE UPDATE OR DELETE ON aap_assets_v1.fixture_independent_media_births
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.prepare_fixture_independent_execution(
    p_run uuid,p_dataset uuid,p_item uuid,p_owner_kind text,p_owner_id uuid,
    p_producer_kind text,p_delivery uuid,p_queue text,p_source jsonb,p_envelope jsonb
) RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_root uuid; v_root_state text; v_state text;
BEGIN
    IF p_delivery IS NULL OR p_queue IS NULL OR p_queue='' OR
       p_envelope ->> 'schema' IS DISTINCT FROM 'aap-jobs-queue/v2' OR
       p_envelope ->> 'operation' IS DISTINCT FROM 'dispatch' OR
       p_envelope ->> 'execution_id' IS DISTINCT FROM p_delivery::text OR
       pg_catalog.jsonb_typeof(p_envelope -> 'options') IS DISTINCT FROM 'object' OR
       ((p_envelope -> 'options') - ARRAY['queue','soft_time_limit']) <> '{}'::jsonb OR
       p_envelope #>> '{options,queue}' IS DISTINCT FROM p_queue OR
       NOT aap_assets_v1.fixture_independent_source_matches(
           p_run,p_dataset,p_item,p_owner_kind,p_owner_id,p_producer_kind,p_source,p_envelope
       ) THEN RETURN false; END IF;
    SELECT COALESCE(run.parent_run_id,run.id) INTO v_root
    FROM yoao_fixture_v1.fixture_runs AS run WHERE run.id=p_run;
    SELECT root.state INTO v_root_state FROM yoao_fixture_v1.fixture_runs AS root
    WHERE root.id=v_root AND root.parent_run_id IS NULL FOR SHARE;
    SELECT run.state INTO v_state FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id=p_run AND COALESCE(run.parent_run_id,run.id)=v_root FOR SHARE;
    IF v_root_state IS DISTINCT FROM 'open' OR v_state IS DISTINCT FROM 'open' THEN RETURN false; END IF;
    INSERT INTO aap_assets_v1.fixture_independent_media_births
        (delivery_id,run_id,dataset_id,item_id,owner_kind,owner_id,producer_kind,queue,source,envelope)
    VALUES (p_delivery,p_run,p_dataset,p_item,p_owner_kind,p_owner_id,p_producer_kind,p_queue,p_source,p_envelope)
    ON CONFLICT DO NOTHING;
    RETURN EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_independent_media_births AS birth
        WHERE birth.delivery_id=p_delivery AND birth.run_id=p_run
          AND birth.dataset_id IS NOT DISTINCT FROM p_dataset AND birth.item_id IS NOT DISTINCT FROM p_item
          AND birth.owner_kind=p_owner_kind AND birth.owner_id=p_owner_id
          AND birth.producer_kind=p_producer_kind AND birth.queue=p_queue
          AND birth.source=p_source AND birth.envelope=p_envelope
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.prepare_fixture_independent_execution(uuid,uuid,uuid,text,uuid,text,uuid,text,jsonb,jsonb) FROM PUBLIC;
