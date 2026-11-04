-- Ordinary originals retain their existing retirement path. Fixture originals
-- additionally require their coordinator-prepared root and exact dataset store.
CREATE FUNCTION aap_assets_v1.classify_fixture_original_reclaim(
    p_creation_id uuid, p_storage_identity text
)
RETURNS text LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_run yoao_fixture_v1.fixture_runs%ROWTYPE;
    v_root yoao_fixture_v1.fixture_runs%ROWTYPE;
    v_resource yoao_fixture_v1.fixture_run_resources%ROWTYPE;
    v_dataset_id uuid;
BEGIN
    SELECT resource.* INTO v_resource
    FROM yoao_fixture_v1.fixture_run_resources AS resource
    WHERE (resource.kind = 'creation' AND resource.creation_id = p_creation_id)
       OR (resource.kind = 'auxiliary' AND resource.resource_id = p_creation_id);
    IF NOT FOUND THEN RETURN 'ordinary'; END IF;
    SELECT run.* INTO v_run FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id = v_resource.run_id;
    IF NOT FOUND THEN RETURN 'blocked'; END IF;
    SELECT root.* INTO v_root FROM yoao_fixture_v1.fixture_runs AS root
    WHERE root.id = COALESCE(v_run.parent_run_id, v_run.id) FOR SHARE;
    IF NOT FOUND OR v_root.parent_run_id IS NOT NULL THEN RETURN 'blocked'; END IF;
    SELECT run.* INTO v_run FROM yoao_fixture_v1.fixture_runs AS run
    WHERE run.id = v_resource.run_id FOR SHARE;
    IF NOT FOUND OR COALESCE(v_run.parent_run_id, v_run.id) <> v_root.id
       OR v_run.state <> 'sealing' OR v_root.state NOT IN ('open', 'sealing')
       OR v_root.original_store_receipt_sha256 IS NULL
       OR v_root.original_storage_identity IS DISTINCT FROM p_storage_identity THEN
        RETURN 'blocked';
    END IF;
    SELECT resource.* INTO v_resource
    FROM yoao_fixture_v1.fixture_run_resources AS resource
    WHERE (resource.kind = 'creation' AND resource.creation_id = p_creation_id)
       OR (resource.kind = 'auxiliary' AND resource.resource_id = p_creation_id)
    FOR SHARE;
    IF NOT FOUND OR v_resource.run_id <> v_run.id OR v_resource.state <> 'published' THEN
        RETURN 'blocked';
    END IF;
    SELECT creation.dataset_id INTO v_dataset_id
    FROM public.managed_original_creations AS creation WHERE creation.id = p_creation_id;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS dataset
        WHERE dataset.run_id = v_run.id AND dataset.kind = 'dataset'
          AND dataset.resource_id = v_dataset_id AND dataset.state = 'published'
          AND dataset.storage_identity = p_storage_identity
    ) THEN
        RETURN 'blocked';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM yoao_fixture_v1.fixture_original_targets AS target
        JOIN public.managed_original_creations AS creation ON creation.id = p_creation_id
        WHERE target.id = v_root.original_target_id
          AND target.storage_identity = v_root.original_storage_identity
          AND target.store_receipt_sha256 = v_root.original_store_receipt_sha256
          AND target.location_id = creation.location_id AND target.bucket = creation.bucket
          AND creation.storage_identity = target.storage_identity
          AND creation.configuration #>> '{fixture_original_target,id}' = target.id::text
          AND creation.configuration #>> '{fixture_original_target,generation}' = target.generation::text
          AND creation.configuration #>> '{fixture_original_target,store_receipt_sha256}' = target.store_receipt_sha256
          AND creation.configuration #>> '{original_ref,binding_generation}' = target.generation::text
    ) THEN
        RETURN 'blocked';
    END IF;
    IF v_resource.kind = 'auxiliary' AND NOT EXISTS (
        SELECT 1
        FROM yoao_fixture_v1.fixture_auxiliary_intents AS intent
        JOIN yoao_fixture_v1.fixture_run_admissions AS admission
          ON admission.id = intent.admission_id AND admission.run_id = intent.run_id
        JOIN public.managed_original_creations AS creation
          ON creation.id = intent.creation_id
        WHERE intent.creation_id = p_creation_id AND intent.run_id = v_run.id
          AND intent.dataset_id = v_dataset_id AND admission.state = 'settled'
          AND creation.owner_kind = 'dataset_auxiliary'
          AND creation.authority -> 'auxiliary' = pg_catalog.jsonb_build_object(
              'kind', intent.kind, 'source_contract', intent.source_contract
          )
          AND creation.sha256 = intent.sha256 AND creation.size_bytes = intent.size_bytes
    ) THEN
        RETURN 'blocked';
    END IF;
    RETURN 'qualified';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.classify_fixture_original_reclaim(uuid,text) FROM PUBLIC;
