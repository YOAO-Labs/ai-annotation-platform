-- Serialize successful operator source registrations with fixture original
-- allocation. Successful operator intents remain durable after item deletion.
CREATE FUNCTION aap_assets_v1.guard_fixture_operator_source_receipt()
RETURNS trigger
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_entry jsonb;
    v_key text;
    v_match text[];
BEGIN
    IF NEW.action <> 'append_items' THEN
        RETURN NEW;
    END IF;
    FOR v_entry IN
        SELECT entry.value
        FROM pg_catalog.jsonb_array_elements(NEW.intent -> 'entries') AS entry(value)
        ORDER BY entry.value ->> 'file_path', entry.value ->> 'id'
    LOOP
        v_key := v_entry ->> 'file_path';
        PERFORM pg_catalog.pg_advisory_xact_lock(
            pg_catalog.hashtextextended('fixture-source:' || v_key, 0)
        );
        v_match := pg_catalog.regexp_match(
            v_key,
            '(^|/)__aap_managed_datasets__/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/_objects/'
        );
        IF v_match IS NOT NULL AND EXISTS (
            SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
            WHERE resource.kind = 'dataset' AND resource.resource_id = v_match[2]::uuid
        ) AND NOT EXISTS (
            SELECT 1 FROM public.managed_original_creations AS creation
            JOIN yoao_fixture_v1.fixture_run_resources AS resource
              ON resource.kind = 'creation' AND resource.creation_id = creation.id
            WHERE creation.dataset_id::text = NEW.intent ->> 'dataset_id'
              AND creation.object_key = v_key
              AND resource.item_id::text = v_entry ->> 'id'
        ) THEN
            RAISE SQLSTATE 'P0002' USING DETAIL = 'source_changed';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_operator_source_receipt() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_operator_source_receipt_guard
BEFORE INSERT ON public.dataset_operator_operations
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_operator_source_receipt();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.is_fixture_original_source(p_key text, p_identity text)
RETURNS boolean LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_runs AS root
        WHERE root.parent_run_id IS NULL
          AND root.original_store_receipt_sha256 IS NOT NULL
          AND root.original_storage_identity = p_identity
    ) OR EXISTS (
        SELECT 1
        FROM yoao_fixture_v1.fixture_run_resources AS resource
        JOIN public.managed_original_creations AS creation ON creation.id = resource.creation_id
        WHERE resource.kind = 'creation' AND creation.object_key = p_key
          AND creation.storage_identity = p_identity
    ) OR EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.kind = 'dataset'
          AND (resource.storage_identity IS NULL OR resource.storage_identity = p_identity)
          AND pg_catalog.regexp_match(
              p_key,
              '(^|/)__aap_managed_datasets__/' || resource.resource_id::text || '/_objects/'
          ) IS NOT NULL
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.is_fixture_original_source(text,text) FROM PUBLIC;
