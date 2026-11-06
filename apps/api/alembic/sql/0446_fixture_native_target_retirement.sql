-- A target tombstone is an owner-computed proof, not a process-exit flag.
CREATE TABLE aap_assets_v1.fixture_native_target_retirements (
    run_id uuid PRIMARY KEY,
    retirement_id uuid NOT NULL UNIQUE,
    storage_identity text NOT NULL,
    target_receipt_sha256 text NOT NULL,
    location_id uuid NOT NULL,
    binding_generation bigint NOT NULL
);
-- aap-statement-break
CREATE TABLE aap_assets_v1.fixture_native_target_physical_acknowledgements (
    run_id uuid PRIMARY KEY REFERENCES aap_assets_v1.fixture_native_target_retirements(run_id),
    receipt text NOT NULL,
    receipt_sha256 text NOT NULL CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$')
);
-- aap-statement-break
REVOKE ALL ON aap_assets_v1.fixture_native_target_retirements,
    aap_assets_v1.fixture_native_target_physical_acknowledgements FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_target_retirement_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_target_retirements FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE TRIGGER fixture_native_target_physical_ack_immutable BEFORE UPDATE OR DELETE
ON aap_assets_v1.fixture_native_target_physical_acknowledgements FOR EACH ROW
EXECUTE FUNCTION aap_assets_v1.guard_fixture_media_birth_evidence();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_fixture_native_target_gate()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_run uuid;
BEGIN
    SELECT run_id INTO v_run FROM aap_assets_v1.fixture_native_cache_targets WHERE location_id=OLD.location_id;
    IF NOT FOUND THEN
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;
    IF TG_OP='UPDATE' AND OLD.state='active' AND NEW.state='stopped'
       AND NEW.revision=OLD.revision+1 AND NEW.changed_at IS NOT NULL
       AND (pg_catalog.to_jsonb(NEW)-'state'-'revision'-'changed_at') IS NOT DISTINCT FROM
           (pg_catalog.to_jsonb(OLD)-'state'-'revision'-'changed_at')
       AND aap_jobs_v1.fixture_media_principal_matches(v_run) IS TRUE
       AND EXISTS (SELECT 1 FROM yoao_fixture_v1.fixture_runs WHERE id=v_run AND state='sealing')
       AND NOT EXISTS (SELECT 1 FROM aap_assets_v1.fixture_native_target_retirements WHERE run_id=v_run)
    THEN RETURN NEW; END IF;
    RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='fixture_native_target_gate_immutable';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.guard_fixture_native_target_gate() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER fixture_native_target_gate_guard BEFORE UPDATE OR DELETE ON public.storage_maintenance_gates
FOR EACH ROW EXECUTE FUNCTION aap_assets_v1.guard_fixture_native_target_gate();
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.fixture_native_target_receipt(p_run uuid)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema','aap-fixture-native-target-retirement/v1','run_id',proof.run_id,
        'storage_identity',proof.storage_identity,'target_receipt_sha256',proof.target_receipt_sha256,
        'retirement_id',proof.retirement_id,'location_id',proof.location_id,
        'binding_generation',proof.binding_generation
    ) FROM aap_assets_v1.fixture_native_target_retirements AS proof WHERE proof.run_id=p_run;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fixture_native_target_receipt(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.retire_fixture_native_target(p_run uuid,p_identity text,p_receipt text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE v_target aap_assets_v1.fixture_native_cache_targets%ROWTYPE; v_result jsonb; v_root uuid;
BEGIN
    IF aap_jobs_v1.fixture_media_principal_matches(p_run) IS DISTINCT FROM true THEN RETURN NULL; END IF;
    SELECT COALESCE(parent_run_id,id) INTO v_root FROM yoao_fixture_v1.fixture_runs WHERE id=p_run;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs WHERE id=v_root AND parent_run_id IS NULL
      AND state IN ('open','sealing') FOR SHARE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    PERFORM 1 FROM yoao_fixture_v1.fixture_runs
    WHERE id=p_run AND COALESCE(parent_run_id,id)=v_root AND state='sealing'
      AND ((family='owned' AND parent_run_id IS NOT NULL) OR (family='screenshot' AND parent_run_id IS NULL)) FOR SHARE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    SELECT * INTO v_target FROM aap_assets_v1.fixture_native_cache_targets WHERE run_id=p_run FOR SHARE;
    IF NOT FOUND OR v_target.storage_identity IS DISTINCT FROM p_identity
       OR v_target.receipt_sha256 IS DISTINCT FROM p_receipt THEN RETURN NULL; END IF;
    PERFORM 1 FROM public.storage_maintenance_gates WHERE location_id=v_target.location_id FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    v_result := aap_assets_v1.fixture_native_target_receipt(p_run);
    IF v_result IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM public.storage_maintenance_gates
                   WHERE location_id=v_target.location_id AND state='stopped') THEN RETURN v_result; END IF;
        RETURN NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.storage_maintenance_gates
                   WHERE location_id=v_target.location_id AND state='active') THEN RETURN NULL; END IF;
    IF aap_assets_v1.fixture_native_target_references_retired(p_run) IS DISTINCT FROM true
       OR aap_jobs_v1.fixture_media_run_retired(p_run) IS DISTINCT FROM true OR EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        WHERE resource.run_id=p_run AND resource.kind='dataset'
          AND (resource.state<>'published' OR aap_assets_v1.fixture_dataset_media_retired(
              p_run,resource.resource_id) IS DISTINCT FROM true)
    ) OR EXISTS (
        SELECT 1 FROM yoao_fixture_v1.fixture_run_resources AS resource
        LEFT JOIN aap_labeling_v1.task_rows AS task ON task.id=resource.resource_id
        WHERE resource.run_id=p_run AND resource.kind='task' AND (
            resource.state<>'published' OR task.id IS NULL OR
            (task.dataset_item_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.dataset_items AS item
                JOIN yoao_fixture_v1.fixture_run_resources AS dataset
                  ON dataset.run_id=p_run AND dataset.kind='dataset' AND dataset.state='published'
                 AND dataset.resource_id=item.dataset_id
                WHERE item.id=task.dataset_item_id
            )) OR
            (task.dataset_item_id IS NULL AND task.original_id IS NOT NULL AND (
                NOT EXISTS (
                    SELECT 1 FROM public.task_originals AS original
                    JOIN public.managed_original_creations AS creation ON creation.id=original.creation_id
                    WHERE original.id=task.original_id AND original.task_id=task.id
                      AND original.project_id=task.project_id AND creation.owner_kind='task'
                      AND creation.task_id=task.id AND creation.project_id=task.project_id
                      AND creation.status='verified' AND creation.settled_at IS NOT NULL
                      AND creation.location_id=original.location_id AND creation.bucket=original.bucket
                      AND creation.object_key=original.object_key AND creation.sha256=original.sha256
                      AND creation.size_bytes=original.size_bytes AND creation.etag=original.etag
                      AND creation.version_id IS NOT DISTINCT FROM original.version_id
                      AND creation.storage_identity=original.storage_identity
                      AND original.location_id<>v_target.location_id
                      AND NOT aap_assets_v1.is_fixture_original_source(original.object_key,original.storage_identity)
                ) OR EXISTS (
                    SELECT 1 FROM aap_assets_v1.fixture_media_births AS birth
                    WHERE birth.run_id=p_run AND birth.task_id=task.id AND birth.dataset_id IS NULL
                ) OR EXISTS (
                    SELECT 1 FROM aap_assets_v1.fixture_independent_media_births AS birth
                    WHERE birth.run_id=p_run AND birth.owner_kind='task'
                      AND birth.owner_id=task.id AND birth.dataset_id IS NULL
                ) OR EXISTS (
                    SELECT 1 FROM aap_assets_v1.fixture_native_media_jobs AS birth
                    WHERE birth.run_id=p_run AND birth.owner_kind='task'
                      AND birth.owner_id=task.id AND birth.dataset_id IS NULL
                )
            )) OR
            (task.dataset_item_id IS NULL AND task.original_id IS NULL AND NOT EXISTS (
                SELECT 1 FROM aap_labeling_v1.task_dataset_item_link_rows AS link
                JOIN public.dataset_items AS item ON item.id=link.dataset_item_id
                JOIN yoao_fixture_v1.fixture_run_resources AS dataset
                  ON dataset.run_id=p_run AND dataset.kind='dataset' AND dataset.state='published'
                 AND dataset.resource_id=item.dataset_id
                WHERE link.task_id=task.id AND link.role='primary_lidar'
            ))
        )
    ) OR EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_media_executions WHERE run_id=p_run AND state<>'settled'
    ) OR EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_independent_media_births AS birth
        LEFT JOIN aap_assets_v1.fixture_independent_media_executions AS execution USING(delivery_id)
        LEFT JOIN aap_assets_v1.fixture_independent_no_start AS no_start USING(delivery_id)
        WHERE birth.run_id=p_run AND execution.state IS DISTINCT FROM 'settled' AND no_start.delivery_id IS NULL
    ) OR EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_reads WHERE run_id=p_run AND state<>'settled'
    ) OR EXISTS (
        SELECT 1 FROM public.storage_operations WHERE location_id=v_target.location_id
    ) OR EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_native_cache_writes AS write
        LEFT JOIN aap_assets_v1.fixture_native_cache_retirements AS retired ON retired.write_id=write.id
        WHERE write.run_id=p_run AND (write.state<>'verified' OR retired.state IS DISTINCT FROM 'absent')
    ) OR EXISTS (
        SELECT 1 FROM aap_assets_v1.fixture_media_versions AS version
        LEFT JOIN aap_assets_v1.fixture_media_version_retirements AS retired ON retired.version_id=version.id
        WHERE version.run_id=p_run AND (version.state<>'verified' OR retired.state IS DISTINCT FROM 'absent')
    ) THEN RETURN NULL; END IF;
    UPDATE public.storage_maintenance_gates SET state='stopped',revision=revision+1,
        changed_at=pg_catalog.clock_timestamp() WHERE location_id=v_target.location_id AND state='active';
    IF NOT FOUND THEN RETURN NULL; END IF;
    INSERT INTO aap_assets_v1.fixture_native_target_retirements
        (run_id,retirement_id,storage_identity,target_receipt_sha256,location_id,binding_generation)
    VALUES(p_run,pg_catalog.gen_random_uuid(),p_identity,p_receipt,v_target.location_id,v_target.binding_generation);
    RETURN aap_assets_v1.fixture_native_target_receipt(p_run);
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.retire_fixture_native_target(uuid,text,text) FROM PUBLIC;
