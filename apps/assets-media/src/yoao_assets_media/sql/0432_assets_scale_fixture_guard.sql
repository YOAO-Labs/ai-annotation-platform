-- Only a sealed scale run's exact Dataset manifest may use this owner guard.
CREATE FUNCTION aap_assets_v1.guard_scale_fixture_originals(
    p_run_id uuid, p_dataset_ids uuid[]
)
RETURNS void
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run yoao_fixture_v1.fixture_runs%ROWTYPE;
BEGIN
    IF p_run_id IS NULL OR p_dataset_ids IS NULL
       OR pg_catalog.array_position(p_dataset_ids, NULL) IS NOT NULL
       OR pg_catalog.cardinality(p_dataset_ids) <> (
           SELECT pg_catalog.count(DISTINCT id) FROM pg_catalog.unnest(p_dataset_ids) AS ids(id)
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid scale fixture dataset scope';
    END IF;
    SELECT * INTO v_run FROM yoao_fixture_v1.fixture_runs WHERE id = p_run_id FOR SHARE;
    IF NOT FOUND OR v_run.family <> 'scale' OR v_run.state <> 'sealing'
       OR EXISTS (
           SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
           WHERE resource.run_id = p_run_id AND resource.kind = 'dataset'
             AND NOT (resource.resource_id = ANY(p_dataset_ids))
       ) OR EXISTS (
           SELECT 1 FROM pg_catalog.unnest(p_dataset_ids) AS ids(id)
           WHERE NOT EXISTS (
               SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
               WHERE resource.run_id = p_run_id AND resource.kind = 'dataset'
                 AND resource.resource_id = ids.id
           )
       ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_run_scope_changed';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.dataset_assets AS asset
        JOIN public.dataset_items AS item ON item.id = asset.dataset_item_id
        WHERE item.dataset_id = ANY(p_dataset_ids)
    ) OR EXISTS (
        SELECT 1 FROM public.managed_original_creations AS creation
        WHERE creation.dataset_id = ANY(p_dataset_ids)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_run_unaccounted_original';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_scale_fixture_originals(uuid, uuid[]) FROM PUBLIC;
