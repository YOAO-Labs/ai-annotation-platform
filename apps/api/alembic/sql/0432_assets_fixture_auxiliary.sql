-- An operator Creation on a fixture Dataset cannot commit without the exact
-- real owner intent and, for item originals, the actual persisted DatasetItem.
-- The allocator inserts these in the same private transaction before any
-- physical operation is opened. Ordinary operator Datasets are unchanged.
CREATE FUNCTION aap_assets_v1.require_fixture_operator_manifest()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS dataset
        WHERE dataset.kind = 'dataset' AND dataset.resource_id = NEW.dataset_id
    ) THEN
        IF NEW.owner_kind = 'dataset_auxiliary' AND NOT EXISTS (
            SELECT 1
            FROM yoao_fixture_v1.fixture_run_resources AS dataset
            JOIN yoao_fixture_v1.fixture_run_resources AS owned
              ON owned.run_id = dataset.run_id AND owned.kind = 'auxiliary'
             AND owned.resource_id = NEW.id AND owned.state = 'published'
            JOIN yoao_fixture_v1.fixture_auxiliary_intents AS intent
              ON intent.run_id = dataset.run_id AND intent.creation_id = NEW.id
             AND intent.dataset_id = NEW.dataset_id
            JOIN yoao_fixture_v1.fixture_run_admissions AS admission
              ON admission.run_id = intent.run_id AND admission.id = intent.admission_id
             AND admission.state = 'active'
            WHERE dataset.kind = 'dataset' AND dataset.resource_id = NEW.dataset_id
              AND dataset.state = 'published' AND dataset.storage_identity = NEW.storage_identity
              AND NEW.authority -> 'auxiliary' = pg_catalog.jsonb_build_object(
                  'kind', intent.kind, 'source_contract', intent.source_contract
              )
        ) THEN
            RAISE EXCEPTION 'fixture auxiliary manifest required' USING ERRCODE = '55000';
        END IF;
        IF NEW.owner_kind = 'dataset' AND NEW.authority ->> 'kind' = 'operator_item'
           AND NOT EXISTS (
               SELECT 1
               FROM yoao_fixture_v1.fixture_run_resources AS dataset
               JOIN yoao_fixture_v1.fixture_run_resources AS item
                 ON item.run_id = dataset.run_id AND item.kind = 'item'
                AND item.resource_id::text = NEW.authority ->> 'item_id'
                AND item.state = 'published'
               JOIN yoao_fixture_v1.fixture_run_resources AS owned
                 ON owned.run_id = dataset.run_id AND owned.kind = 'creation'
                AND owned.creation_id = NEW.id AND owned.item_id = item.resource_id
                AND owned.state = 'published'
               JOIN yoao_fixture_v1.fixture_run_admissions AS admission
                 ON admission.run_id = owned.run_id AND admission.id = owned.admission_id
                AND admission.state = 'active'
               JOIN public.dataset_items AS actual
                 ON actual.id = item.resource_id AND actual.dataset_id = NEW.dataset_id
                AND actual.file_path = NEW.object_key
               WHERE dataset.kind = 'dataset' AND dataset.resource_id = NEW.dataset_id
                 AND dataset.state = 'published' AND dataset.storage_identity = NEW.storage_identity
           ) THEN
            RAISE EXCEPTION 'fixture operator item manifest required' USING ERRCODE = '55000';
        END IF;
    END IF;
    RETURN NULL;
END;
$function$;
-- aap-statement-break
CREATE CONSTRAINT TRIGGER fixture_operator_manifest_guard
AFTER INSERT ON public.managed_original_creations DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.require_fixture_operator_manifest();
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.require_fixture_operator_manifest() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_auxiliary_intent()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RAISE EXCEPTION 'fixture auxiliary intent is immutable' USING ERRCODE = '55000';
END;
$function$;
-- aap-statement-break
CREATE TRIGGER fixture_auxiliary_intent_immutable
BEFORE UPDATE OR DELETE ON yoao_fixture_v1.fixture_auxiliary_intents
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_auxiliary_intent();
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_auxiliary_intent() FROM PUBLIC;
