-- Legacy image publication observes bytes before taking business source locks.
CREATE FUNCTION aap_assets_v1.read_legacy_dataset_upload(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid, p_item_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_item public.dataset_items%ROWTYPE;
    v_saved public.dataset_legacy_upload_operations%ROWTYPE;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL
       OR p_dataset_id IS NULL OR p_item_id IS NULL THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'invalid_request');
    END IF;
    v_actor := aap_assets_v1._image_actor(p_actor_id, false);
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, v_actor #>> '{error,code}');
    END IF;
    SELECT operation.* INTO v_saved
    FROM public.dataset_legacy_upload_operations AS operation
    WHERE operation.operation_id = p_operation_id;
    IF FOUND AND (v_saved.actor_id, v_saved.dataset_id, v_saved.item_id)
       IS DISTINCT FROM (p_actor_id, p_dataset_id, p_item_id) THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'operation_conflict');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.datasets AS dataset WHERE dataset.id = p_dataset_id) THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'upload_missing');
    END IF;
    SELECT item.* INTO v_item FROM public.dataset_items AS item
    WHERE item.id = p_item_id AND item.dataset_id = p_dataset_id;
    IF NOT FOUND THEN RETURN aap_assets_v1._legacy_upload_reply(NULL, 'upload_missing'); END IF;
    IF EXISTS (SELECT 1 FROM public.dataset_upload_intents AS intent WHERE intent.item_id = p_item_id) THEN
        RETURN aap_assets_v1._legacy_upload_reply(NULL, 'upload_scope_mismatch');
    END IF;
    IF v_saved.operation_id IS NOT NULL THEN
        IF pg_catalog.jsonb_typeof(v_saved.result) IS DISTINCT FROM 'object'
           OR v_saved.result ->> 'operation_id' IS DISTINCT FROM p_operation_id::text
           OR v_saved.result ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
           OR v_saved.result ->> 'dataset_id' IS DISTINCT FROM p_dataset_id::text
           OR v_saved.result ->> 'item_id' IS DISTINCT FROM p_item_id::text
           OR v_saved.result ->> 'state' IS DISTINCT FROM 'completed' THEN
            RETURN aap_assets_v1._legacy_upload_reply(NULL, 'invalid_response');
        END IF;
        IF v_saved.result ->> 'file_path' IS DISTINCT FROM v_item.file_path
           OR v_saved.result ->> 'file_type' IS DISTINCT FROM v_item.file_type THEN
            RETURN aap_assets_v1._legacy_upload_reply(NULL, 'source_changed');
        END IF;
        RETURN aap_assets_v1._legacy_upload_reply(v_saved.result, NULL);
    END IF;
    RETURN aap_assets_v1._legacy_upload_reply(pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', p_actor_id,
        'dataset_id', p_dataset_id, 'item_id', p_item_id,
        'file_path', v_item.file_path, 'file_type', v_item.file_type,
        'width', v_item.width, 'height', v_item.height,
        'state', 'pending', 'result', NULL, 'duplicate_of', NULL
    ), NULL);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.read_legacy_dataset_upload(uuid, uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_replace_legacy_delivery()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    -- Keep the handoff audit, but only native accepted work may reach a worker.
    IF NEW.kind = 'thumbnail' THEN
        NEW.delivered_at := pg_catalog.clock_timestamp();
        NEW.lease_token := NULL;
        NEW.lease_expires_at := NULL;
    END IF;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_replace_legacy_delivery() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_dataset_import_image_delivery_replaced
BEFORE INSERT OR UPDATE ON public.dataset_import_deliveries
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._image_replace_legacy_delivery();
-- aap-statement-break
CREATE TRIGGER trg_dataset_source_ingest_image_delivery_replaced
BEFORE INSERT OR UPDATE ON public.dataset_source_ingest_deliveries
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._image_replace_legacy_delivery();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._image_delivery_source_bound()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    -- Producers observe and admit after registration on the same connection.
    -- Defer this check so an older producer cannot silently lose its handoff.
    IF NEW.kind = 'thumbnail' AND NOT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        WHERE item.id = NEW.target_id
          AND item.file_type = 'image'
          AND NOT item.media_source_drifted
          AND item.media_object_identity IS NOT NULL
          AND aap_assets_v1._media_object_identity_valid(item.media_object_identity)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'image producer must bind the published source before commit';
    END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._image_delivery_source_bound() FROM PUBLIC;
-- aap-statement-break
CREATE CONSTRAINT TRIGGER trg_dataset_import_image_source_bound
AFTER INSERT OR UPDATE ON public.dataset_import_deliveries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._image_delivery_source_bound();
-- aap-statement-break
CREATE CONSTRAINT TRIGGER trg_dataset_source_ingest_image_source_bound
AFTER INSERT OR UPDATE ON public.dataset_source_ingest_deliveries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1._image_delivery_source_bound();
