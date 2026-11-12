-- A born prediction Job's exact Dataset set is frozen from its execution Task
-- ids and the durable Task/Dataset owners. A payload field alone is never
-- sufficient, no historical Job is adopted, and a Dataset linked later cannot
-- widen or narrow this born binding.
CREATE FUNCTION aap_assets_v1.fixture_task_dataset_scope(
    p_project_id uuid, p_task_ids jsonb
) RETURNS uuid[] LANGUAGE plpgsql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_item jsonb;
    v_ids uuid[] := ARRAY[]::uuid[];
    v_datasets uuid[];
BEGIN
    IF p_project_id IS NULL
       OR pg_catalog.jsonb_typeof(p_task_ids) IS DISTINCT FROM 'array' THEN
        RETURN NULL;
    END IF;
    FOR v_item IN
        SELECT value FROM pg_catalog.jsonb_array_elements(p_task_ids)
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'string'
           OR (v_item #>> '{}') !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN
            RETURN NULL;
        END IF;
        v_ids := pg_catalog.array_append(v_ids, (v_item #>> '{}')::uuid);
    END LOOP;
    IF pg_catalog.cardinality(v_ids) = 0
       OR pg_catalog.cardinality(v_ids) <> (
           SELECT pg_catalog.count(DISTINCT ids.id)
           FROM pg_catalog.unnest(v_ids) AS ids(id)
       ) THEN
        RETURN NULL;
    END IF;
    -- Every requested Task must resolve inside the exact Job project. A missing,
    -- foreign or source-less Task leaves the whole set unproven.
    IF (
        SELECT pg_catalog.count(*)
        FROM aap_labeling_v1.task_rows AS task
        WHERE task.id = ANY(v_ids) AND task.project_id = p_project_id
    ) <> pg_catalog.cardinality(v_ids) THEN
        RETURN NULL;
    END IF;
    IF EXISTS (
        SELECT 1
        FROM aap_labeling_v1.task_rows AS task
        LEFT JOIN public.dataset_items AS item ON item.id = task.dataset_item_id
        WHERE task.id = ANY(v_ids) AND task.project_id = p_project_id
          AND item.id IS NULL
    ) THEN
        RETURN NULL;
    END IF;
    v_datasets := ARRAY(
        SELECT DISTINCT item.dataset_id
        FROM aap_labeling_v1.task_rows AS task
        JOIN public.dataset_items AS item ON item.id = task.dataset_item_id
        WHERE task.id = ANY(v_ids) AND task.project_id = p_project_id
        ORDER BY item.dataset_id
    );
    IF COALESCE(pg_catalog.cardinality(v_datasets), 0) = 0 THEN
        RETURN NULL;
    END IF;
    RETURN v_datasets;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_task_dataset_scope(uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
-- The born binding is a durable Jobs owner record; it never adopts a Job that
-- already exists and can never be rewritten after birth.
CREATE TABLE aap_jobs_v1.fixture_prediction_job_datasets (
    job_id uuid PRIMARY KEY
        REFERENCES aap_jobs_v1.fixture_prediction_job_births(job_id) ON DELETE RESTRICT,
    dataset_ids uuid[] NOT NULL,
    CHECK (pg_catalog.cardinality(dataset_ids) > 0),
    CHECK (pg_catalog.array_position(dataset_ids, NULL::uuid) IS NULL)
);
-- aap-statement-break
REVOKE ALL ON aap_jobs_v1.fixture_prediction_job_datasets FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_jobs_v1.guard_fixture_prediction_job_datasets()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_payload jsonb;
    v_project_id uuid;
    v_derived uuid[];
BEGIN
    -- Only the Job birth trigger may create the binding, and only when it
    -- exactly matches the derivation from the Job row being born.
    IF TG_OP = 'INSERT' AND pg_catalog.pg_trigger_depth() >= 2
       AND current_user = pg_catalog.pg_get_userbyid((
           SELECT relation.relowner FROM pg_catalog.pg_class AS relation
           WHERE relation.oid='aap_jobs_v1.fixture_prediction_job_datasets'::regclass
       )) THEN
        SELECT job.payload, job.project_id INTO v_payload, v_project_id
        FROM public.async_jobs AS job WHERE job.id = NEW.job_id;
        IF FOUND THEN
            v_derived := aap_assets_v1.fixture_task_dataset_scope(
                v_project_id, v_payload -> 'task_ids'
            );
            IF v_derived IS NOT NULL AND NEW.dataset_ids IS NOT DISTINCT FROM v_derived THEN
                RETURN NEW;
            END IF;
        END IF;
    END IF;
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture prediction dataset scope is immutable';
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_prediction_job_datasets() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER aap_fixture_prediction_job_datasets_guard
BEFORE INSERT OR UPDATE OR DELETE ON aap_jobs_v1.fixture_prediction_job_datasets
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_fixture_prediction_job_datasets();
-- aap-statement-break
-- Prediction births retain both their run/project/actor identity and, for a
-- batch_predict Job, the exact frozen Dataset set.
CREATE OR REPLACE FUNCTION aap_jobs_v1.record_fixture_prediction_job_birth()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_born boolean := false;
    v_datasets uuid[];
BEGIN
    IF EXISTS (SELECT 1 FROM aap_jobs_v1.fixture_prediction_job_births WHERE job_id=NEW.id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_prediction_job_identity_retired';
    END IF;
    IF NEW.kind IN ('batch_predict','prediction_retry') THEN
        INSERT INTO aap_jobs_v1.fixture_prediction_job_births(job_id, run_id, project_id, actor_id, kind)
        SELECT NEW.id, resource.run_id, NEW.project_id, NEW.user_id, NEW.kind
        FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind = 'project' AND resource.resource_id = NEW.project_id
          AND resource.state = 'published';
        v_born := FOUND;
    END IF;
    IF v_born AND NEW.kind = 'batch_predict' THEN
        v_datasets := aap_assets_v1.fixture_task_dataset_scope(
            NEW.project_id, NEW.payload -> 'task_ids'
        );
        IF v_datasets IS NOT NULL THEN
            INSERT INTO aap_jobs_v1.fixture_prediction_job_datasets(job_id, dataset_ids)
            VALUES (NEW.id, v_datasets);
        END IF;
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.record_fixture_prediction_job_birth() FROM PUBLIC;
-- aap-statement-break
-- Prediction's born binding augments, but never weakens, Jobs' typed scope.
CREATE OR REPLACE FUNCTION aap_jobs_v1.fixture_job_dataset_scope(
    p_id uuid, p_kind text, p_payload jsonb, p_project_id uuid, p_user_id uuid
) RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_datasets uuid[];
BEGIN
    IF p_kind = 'export' THEN
        SELECT scope.dataset_ids INTO v_datasets FROM public.image_export_scopes AS scope
        WHERE scope.job_id = p_id AND scope.job_born
          AND (p_project_id IS NULL OR scope.project_id = p_project_id)
          AND (p_user_id IS NULL OR scope.actor_id = p_user_id);
        IF FOUND THEN
            RETURN jsonb_build_object('kind', 'datasets', 'dataset_ids', to_jsonb(v_datasets));
        END IF;
    END IF;
    IF p_kind = 'batch_predict' THEN
        SELECT binding.dataset_ids INTO v_datasets
        FROM aap_jobs_v1.fixture_prediction_job_datasets AS binding
        JOIN aap_jobs_v1.fixture_prediction_job_births AS birth
          ON birth.job_id = binding.job_id
        WHERE binding.job_id = p_id
          AND birth.kind = p_kind
          AND birth.project_id IS NOT DISTINCT FROM p_project_id
          AND birth.actor_id IS NOT DISTINCT FROM p_user_id;
        IF FOUND THEN
            RETURN jsonb_build_object('kind', 'datasets', 'dataset_ids', to_jsonb(v_datasets));
        END IF;
    END IF;
    RETURN aap_jobs_v1.fixture_dataset_job_scope(p_id,p_kind,p_payload,p_project_id,p_user_id);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_job_dataset_scope(uuid,text,jsonb,uuid,uuid) FROM PUBLIC;
