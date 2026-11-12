CREATE FUNCTION aap_assets_v1.is_fixture_spatial_dataset(p_dataset_id uuid)
RETURNS boolean LANGUAGE sql STABLE PARALLEL SAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind = 'dataset' AND resource.resource_id = p_dataset_id
    );
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.is_fixture_spatial_dataset(uuid) FROM PUBLIC;
-- aap-statement-break
-- Bind the physical source read to the existing accepted point-cloud
-- preparation, exact DatasetItem and the ordinary Assets operation fence.
CREATE FUNCTION aap_assets_v1.begin_spatial_calibration_original(
    p_preparation_id uuid, p_item_id uuid, p_object_key text
)
RETURNS jsonb LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_scope jsonb;
    v_dataset_id uuid;
    v_permit jsonb;
    v_fixture boolean;
BEGIN
    v_scope := aap_labeling_v1.read_spatial_preparation_scope(p_preparation_id);
    IF v_scope ->> 'schema' IS DISTINCT FROM 'aap-labeling-spatial-preparation/v1'
       OR v_scope -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RAISE EXCEPTION 'spatial preparation is not active' USING ERRCODE = '23514';
    END IF;
    v_dataset_id := (v_scope #>> '{data,dataset_id}')::uuid;
    IF NOT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        WHERE item.id = p_item_id AND item.dataset_id = v_dataset_id
          AND item.file_path = p_object_key AND item.file_type = 'other'
    ) THEN
        RAISE EXCEPTION 'spatial calibration source changed' USING ERRCODE = '23514';
    END IF;
    SELECT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind = 'dataset' AND resource.resource_id = v_dataset_id
    ) INTO v_fixture;
    v_permit := aap_assets_v1._begin_media_original_read(p_item_id, NULL, p_object_key);
    IF v_fixture AND v_permit ->> 'ownership' IS DISTINCT FROM 'fixture' THEN
        RAISE EXCEPTION 'fixture calibration original unverified' USING ERRCODE = '23514';
    END IF;
    IF v_permit ->> 'ownership' = 'fixture' THEN
        UPDATE public.storage_operations
        SET purpose = 'spatial_calibration_' || pg_catalog.replace(v_permit ->> 'creation_id', '-', ''),
            node_id = 'aap-assets-spatial'
        WHERE id = (v_permit ->> 'operation_id')::uuid
          AND node_id = 'aap-assets-media';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'spatial calibration operation changed' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN v_permit;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.begin_spatial_calibration_original(uuid,uuid,text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.finish_spatial_calibration_original(
    p_creation_id uuid, p_operation_id uuid
)
RETURNS boolean LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    DELETE FROM public.storage_operations AS operation
    USING public.managed_original_creations AS creation
    WHERE creation.id = p_creation_id AND operation.id = p_operation_id
      AND operation.location_id = creation.location_id
      AND operation.purpose = 'spatial_calibration_' || pg_catalog.replace(creation.id::text, '-', '')
      AND operation.node_id = 'aap-assets-spatial';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.finish_spatial_calibration_original(uuid,uuid) FROM PUBLIC;
