-- Image export admissions retain the exact Dataset set independently of Jobs.
-- No historical Job is adopted: only the producer can create a new binding.
CREATE TABLE public.image_export_scopes (
    job_id uuid PRIMARY KEY,
    bundle_id uuid NOT NULL UNIQUE,
    project_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    job_born boolean NOT NULL DEFAULT false,
    dataset_ids uuid[] NOT NULL,
    CHECK (array_position(dataset_ids, NULL::uuid) IS NULL)
);
-- aap-statement-break
CREATE INDEX image_export_scopes_dataset_ids ON public.image_export_scopes USING gin (dataset_ids);
-- aap-statement-break
REVOKE ALL ON public.image_export_scopes FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.image_export_snapshot_datasets(p_snapshot jsonb)
RETURNS uuid[] LANGUAGE plpgsql IMMUTABLE PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_item jsonb;
    v_entry jsonb;
    v_source jsonb;
    v_value text;
    v_ids uuid[] := ARRAY[]::uuid[];
BEGIN
    IF jsonb_typeof(p_snapshot) IS DISTINCT FROM 'object'
       OR p_snapshot -> 'schema' IS DISTINCT FROM '3'::jsonb
       OR p_snapshot #>> '{project,data_type}' IS DISTINCT FROM 'image'
       OR jsonb_typeof(p_snapshot -> 'items') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_snapshot -> 'references') IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_snapshot -> 'media') IS DISTINCT FROM 'array' THEN
        RETURN NULL;
    END IF;
    FOR v_item IN SELECT value FROM jsonb_array_elements(p_snapshot -> 'items') LOOP
        v_value := v_item ->> 'dataset_id';
        IF v_value IS NULL OR v_value !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN RETURN NULL; END IF;
        v_ids := array_append(v_ids, v_value::uuid);
    END LOOP;
    FOR v_item IN SELECT value FROM jsonb_each(p_snapshot -> 'references') LOOP
        v_value := v_item ->> 'dataset_id';
        IF v_value IS NULL OR v_value !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN RETURN NULL; END IF;
        v_ids := array_append(v_ids, v_value::uuid);
    END LOOP;
    FOR v_entry IN SELECT value FROM jsonb_array_elements(p_snapshot -> 'media') LOOP
        v_source := v_entry -> 'source';
        IF v_source ->> 'owner_kind' = 'dataset_asset' THEN
            v_value := v_source #>> '{item_facts,dataset_id}';
            IF v_value IS NULL OR v_value !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN RETURN NULL; END IF;
            v_ids := array_append(v_ids, v_value::uuid);
        ELSIF v_source ->> 'owner_kind' IS DISTINCT FROM 'task_original'
           OR v_source -> 'item_facts' IS DISTINCT FROM 'null'::jsonb THEN
            RETURN NULL;
        END IF;
    END LOOP;
    RETURN ARRAY(SELECT DISTINCT item FROM unnest(v_ids) AS ids(item) ORDER BY item);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.image_export_snapshot_datasets(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.prepare_image_export_scope(
    p_job uuid, p_bundle uuid, p_project uuid, p_actor uuid, p_datasets uuid[]
) RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_run record; v_dataset uuid;
BEGIN
    IF p_job IS NULL OR p_bundle IS NULL OR p_project IS NULL OR p_actor IS NULL
       OR p_datasets IS NULL OR array_position(p_datasets, NULL::uuid) IS NOT NULL
       OR p_datasets IS DISTINCT FROM ARRAY(SELECT DISTINCT id FROM unnest(p_datasets) AS ids(id) ORDER BY id) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid image export scope';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('aap-export:job:' || p_job::text, 0));
    IF EXISTS (SELECT 1 FROM public.async_jobs WHERE id = p_job) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'image export Job identity already exists';
    END IF;
    -- Fixture registration takes the same identity lock before publishing a
    -- Dataset resource, including a registration not yet visible to MVCC.
    FOREACH v_dataset IN ARRAY p_datasets LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended('image-export:dataset:' || v_dataset::text, 0));
    END LOOP;
    -- Serialize with fixture seal. Unrelated runs do not hold up admission.
    FOR v_run IN
        SELECT run.state FROM yoao_fixture_v1.fixture_runs AS run
        JOIN yoao_fixture_v1.fixture_run_resources AS resource ON resource.run_id = run.id
        WHERE resource.kind = 'dataset' AND resource.resource_id = ANY(p_datasets)
        ORDER BY run.id FOR SHARE OF run
    LOOP
        IF v_run.state <> 'open' THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_run_closed';
        END IF;
    END LOOP;
    INSERT INTO public.image_export_scopes(job_id,bundle_id,project_id,actor_id,dataset_ids)
    VALUES (p_job,p_bundle,p_project,p_actor,p_datasets);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.prepare_image_export_scope(uuid,uuid,uuid,uuid,uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.image_export_scope_matches(p_job uuid, p_bundle uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.image_export_scopes AS scope
        JOIN public.async_jobs AS job ON job.id = scope.job_id
        JOIN public.export_media_bundles AS bundle ON bundle.id = scope.bundle_id
        WHERE scope.job_id = p_job AND scope.bundle_id = p_bundle
          AND job.kind = 'export' AND job.project_id = scope.project_id
          AND job.user_id = scope.actor_id
          AND bundle.job_id = scope.job_id AND bundle.project_id = scope.project_id
          AND bundle.input_snapshot ->> 'project_id' = scope.project_id::text
          AND job.payload -> 'image_media' = jsonb_build_object(
              'schema', 3, 'bundle_id', scope.bundle_id::text, 'input_digest', bundle.input_digest)
          AND public.image_export_snapshot_datasets(bundle.input_snapshot) = scope.dataset_ids
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.image_export_scope_matches(uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION public.guard_image_export_scope() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_scope public.image_export_scopes%ROWTYPE;
BEGIN
    IF TG_TABLE_NAME = 'image_export_scopes' THEN
        IF TG_OP = 'UPDATE' AND NOT OLD.job_born AND NEW.job_born
           AND (to_jsonb(NEW) - 'job_born') = (to_jsonb(OLD) - 'job_born') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'image export scope is immutable';
    ELSIF TG_TABLE_NAME = 'export_media_bundles' THEN
        SELECT * INTO v_scope FROM public.image_export_scopes WHERE job_id = NEW.job_id;
        IF FOUND AND (NEW.id <> v_scope.bundle_id OR NEW.project_id <> v_scope.project_id
            OR public.image_export_snapshot_datasets(NEW.input_snapshot) IS DISTINCT FROM v_scope.dataset_ids) THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'image export bundle scope changed';
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO v_scope FROM public.image_export_scopes WHERE job_id = OLD.id;
    IF NOT FOUND THEN RETURN NEW; END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.kind IS DISTINCT FROM OLD.kind
       OR (NEW.project_id IS NOT NULL AND NEW.project_id IS DISTINCT FROM v_scope.project_id)
       OR (NEW.user_id IS NOT NULL AND NEW.user_id IS DISTINCT FROM v_scope.actor_id)
       OR (OLD.payload ? 'image_media' AND NEW.payload -> 'image_media' IS DISTINCT FROM OLD.payload -> 'image_media')
       OR (NEW.payload ? 'image_media' AND (NEW.payload #>> '{image_media,bundle_id}') IS DISTINCT FROM v_scope.bundle_id::text) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'image export job binding changed';
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.guard_image_export_scope() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER image_export_scope_immutable BEFORE UPDATE OR DELETE ON public.image_export_scopes
FOR EACH ROW EXECUTE FUNCTION public.guard_image_export_scope();
-- aap-statement-break
-- A retained binding also fences reuse after the generic Job is removed.
CREATE FUNCTION public.guard_image_export_job_birth() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_scope public.image_export_scopes%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('aap-export:job:' || NEW.id::text, 0));
    SELECT * INTO v_scope FROM public.image_export_scopes WHERE job_id = NEW.id FOR UPDATE;
    IF FOUND THEN
        IF v_scope.job_born OR NEW.kind <> 'export'
           OR NEW.project_id IS DISTINCT FROM v_scope.project_id
           OR NEW.user_id IS DISTINCT FROM v_scope.actor_id THEN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'image export Job identity cannot be reused';
        END IF;
        UPDATE public.image_export_scopes SET job_born = true WHERE job_id = NEW.id;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.guard_image_export_job_birth() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER image_export_job_birth BEFORE INSERT ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION public.guard_image_export_job_birth();
-- aap-statement-break
CREATE FUNCTION public.guard_image_export_fixture_dataset_birth() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NEW.kind = 'dataset' THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('image-export:dataset:' || NEW.resource_id::text, 0));
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.guard_image_export_fixture_dataset_birth() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER image_export_fixture_dataset_birth BEFORE INSERT ON yoao_fixture_v1.fixture_run_resources
FOR EACH ROW EXECUTE FUNCTION public.guard_image_export_fixture_dataset_birth();
-- aap-statement-break
CREATE TRIGGER image_export_bundle_binding BEFORE INSERT ON public.export_media_bundles
FOR EACH ROW EXECUTE FUNCTION public.guard_image_export_scope();
-- aap-statement-break
CREATE TRIGGER image_export_job_binding BEFORE UPDATE OF id,kind,project_id,user_id,payload ON public.async_jobs
FOR EACH ROW EXECUTE FUNCTION public.guard_image_export_scope();
-- aap-statement-break
CREATE FUNCTION public.check_image_export_scope_committed() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF NOT public.image_export_scope_matches(NEW.job_id, NEW.bundle_id) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'image export scope is not bound';
    END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION public.check_image_export_scope_committed() FROM PUBLIC;
-- aap-statement-break
CREATE CONSTRAINT TRIGGER image_export_scope_complete
AFTER INSERT ON public.image_export_scopes DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION public.check_image_export_scope_committed();
