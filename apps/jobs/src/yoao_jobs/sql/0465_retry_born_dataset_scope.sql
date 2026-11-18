-- A native prediction-retry Job can only be classified from the attempt frozen
-- in the same admission transaction, its exact Task source, and any retained
-- input evidence. A historical attempt retained after its Job was deleted is
-- never adopted: birth_xid records the inserting top-level transaction and is
-- NULL for every pre-migration row. Add the column without a default so no
-- historical row is rewritten; only new inserts receive the volatile default.
ALTER TABLE public.prediction_retry_attempts
    ADD COLUMN birth_xid bigint;
-- aap-statement-break
ALTER TABLE public.prediction_retry_attempts
    ALTER COLUMN birth_xid SET DEFAULT pg_catalog.txid_current();
-- aap-statement-break
-- A supplied xid, including a different one, never survives the insert. The
-- column default and this BEFORE trigger both use the top-level transaction id,
-- which remains stable across PL/pgSQL exception subtransactions and savepoints.
CREATE FUNCTION aap_jobs_v1.bind_prediction_retry_attempt_birth()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    NEW.birth_xid := pg_catalog.txid_current();
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.bind_prediction_retry_attempt_birth() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_prediction_retry_attempt_bind_birth
BEFORE INSERT ON public.prediction_retry_attempts
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.bind_prediction_retry_attempt_birth();
-- aap-statement-break
-- The admitted source facts are frozen only while the attempt is bound to a
-- live Job or born receipt. The retry state machine may still advance
-- state/outcome/terminal_digest/updated_at; a bound attempt may not be
-- retargeted to another Task source, backend, queue or retained evidence.
-- Historical and marked-but-unbound attempts keep their maintenance path; the
-- birth_xid itself can never be rewritten.
CREATE FUNCTION aap_jobs_v1.guard_prediction_retry_attempt_scope()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        -- Only a bound Job/born receipt makes the attempt part of a durable
        -- proof. Historical and marked-but-unbound rows keep their existing
        -- maintenance path.
        IF EXISTS (
               SELECT 1 FROM public.async_jobs AS job WHERE job.id = OLD.job_id
           )
           OR EXISTS (
               SELECT 1
                 FROM aap_jobs_v1.fixture_worker_job_datasets AS receipt
                WHERE receipt.job_id = OLD.job_id
           ) THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='prediction retry attempt birth is immutable';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.birth_xid IS DISTINCT FROM OLD.birth_xid THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry attempt birth is immutable';
    END IF;
    IF (EXISTS (
            SELECT 1 FROM public.async_jobs AS job WHERE job.id = OLD.job_id
        )
        OR EXISTS (
            SELECT 1
              FROM aap_jobs_v1.fixture_worker_job_datasets AS receipt
             WHERE receipt.job_id = OLD.job_id
        ))
       AND (NEW.job_id IS DISTINCT FROM OLD.job_id
            OR NEW.failed_prediction_id IS DISTINCT FROM OLD.failed_prediction_id
            OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
            OR NEW.project_id IS DISTINCT FROM OLD.project_id
            OR NEW.task_id IS DISTINCT FROM OLD.task_id
            OR NEW.ml_backend_id IS DISTINCT FROM OLD.ml_backend_id
            OR NEW.ml_backend_pool_id IS DISTINCT FROM OLD.ml_backend_pool_id
            OR NEW.source_extra IS DISTINCT FROM OLD.source_extra
            OR NEW.retry_count_at_admission IS DISTINCT FROM OLD.retry_count_at_admission
            OR NEW.write_scope IS DISTINCT FROM OLD.write_scope
            OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
            OR NEW.execution_queue IS DISTINCT FROM OLD.execution_queue
            OR NEW.legacy_delivery IS DISTINCT FROM OLD.legacy_delivery
            OR NEW.created_at IS DISTINCT FROM OLD.created_at) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry attempt scope is immutable';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_prediction_retry_attempt_scope() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_prediction_retry_attempt_scope_immutable
BEFORE UPDATE OR DELETE ON public.prediction_retry_attempts
FOR EACH ROW EXECUTE FUNCTION aap_jobs_v1.guard_prediction_retry_attempt_scope();
-- aap-statement-break
-- Assets owns the canonical Task/Dataset source read. It receives only the
-- frozen write-scope source facts and frozen failure evidence. It never
-- resolves a Dataset by Task file path.
CREATE FUNCTION aap_assets_v1.retry_task_dataset_scope(
    p_task_id uuid,
    p_project_id uuid,
    p_source_dataset_item_id uuid,
    p_source_file_path text,
    p_source_file_type text,
    p_accepted_input_id uuid,
    p_source_extra jsonb
) RETURNS uuid[]
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_task record;
    v_link record;
    v_evidence jsonb;
    v_datasets uuid[] := ARRAY[]::uuid[];
    v_dataset uuid;
    v_item uuid;
    v_text text;
    v_task_text text;
    v_job_text text;
    v_input_text text;
    v_manifest_text text;
    v_digest text;
    v_ordinal_text text;
    v_member_path text;
    v_member_type text;
    v_primary_count integer := 0;
    v_primary_item uuid;
    v_has_reference boolean := false;
    v_has_original boolean := false;
    v_has_video boolean := false;
    v_uuid_pattern constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
BEGIN
    IF p_task_id IS NULL OR p_project_id IS NULL
       OR p_source_file_path IS NULL
       OR pg_catalog.char_length(p_source_file_path) NOT BETWEEN 1 AND 1000
       OR p_source_file_type IS NULL
       OR pg_catalog.char_length(p_source_file_type) NOT BETWEEN 1 AND 20
       OR p_source_extra IS NULL
       OR pg_catalog.jsonb_typeof(p_source_extra) IS DISTINCT FROM 'object' THEN
        RETURN NULL;
    END IF;

    SELECT task.id, task.project_id, task.dataset_item_id, task.file_path,
           task.file_type, task.original_id
      INTO v_task
      FROM aap_labeling_v1.task_rows AS task
     WHERE task.id = p_task_id AND task.project_id = p_project_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF v_task.dataset_item_id IS DISTINCT FROM p_source_dataset_item_id
       OR v_task.file_path IS DISTINCT FROM p_source_file_path
       OR v_task.file_type IS DISTINCT FROM p_source_file_type THEN
        RETURN NULL;
    END IF;

    -- Every link is a conservative part of the Task source. A missing link,
    -- DatasetItem or Dataset owner leaves the whole source unknown.
    FOR v_link IN
        SELECT link.dataset_item_id, link.role
          FROM aap_labeling_v1.task_dataset_item_link_rows AS link
         WHERE link.task_id = p_task_id
         ORDER BY link.dataset_item_id, link.role
    LOOP
        IF v_link.dataset_item_id IS NULL THEN
            RETURN NULL;
        END IF;
        IF v_link.role = 'primary_lidar' THEN
            v_primary_count := v_primary_count + 1;
            v_primary_item := v_link.dataset_item_id;
        END IF;
        SELECT item.dataset_id INTO v_dataset
          FROM public.dataset_items AS item
          JOIN public.datasets AS dataset ON dataset.id = item.dataset_id
         WHERE item.id = v_link.dataset_item_id;
        IF NOT FOUND THEN
            -- A missing Task link or Dataset owner leaves the whole source
            -- unknown; no link row may be silently skipped.
            RETURN NULL;
        END IF;
        v_datasets := pg_catalog.array_append(v_datasets, v_dataset);
    END LOOP;
    IF v_primary_count > 1 THEN
        RETURN NULL;
    END IF;

    IF v_task.dataset_item_id IS NOT NULL THEN
        IF v_task.original_id IS NOT NULL THEN
            RETURN NULL;
        END IF;
        IF v_primary_count > 0
           AND v_primary_item IS DISTINCT FROM v_task.dataset_item_id THEN
            RETURN NULL;
        END IF;
        SELECT item.dataset_id INTO v_dataset
          FROM public.dataset_items AS item
          JOIN public.datasets AS dataset ON dataset.id = item.dataset_id
         WHERE item.id = v_task.dataset_item_id
           AND item.file_path = v_task.file_path
           AND item.file_type = v_task.file_type;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        v_datasets := pg_catalog.array_append(v_datasets, v_dataset);
    ELSIF v_task.original_id IS NOT NULL THEN
        IF v_primary_count > 0 THEN
            RETURN NULL;
        END IF;
        SELECT original.object_key INTO v_text
          FROM public.task_originals AS original
         WHERE original.id = v_task.original_id
           AND original.task_id = p_task_id
           AND original.project_id = p_project_id;
        IF NOT FOUND OR v_text IS DISTINCT FROM v_task.file_path THEN
            RETURN NULL;
        END IF;
    ELSIF v_primary_count = 1 THEN
        SELECT item.dataset_id INTO v_dataset
          FROM public.dataset_items AS item
          JOIN public.datasets AS dataset ON dataset.id = item.dataset_id
         WHERE item.id = v_primary_item
           AND item.file_path = v_task.file_path
           AND item.file_type = v_task.file_type;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        v_datasets := pg_catalog.array_append(v_datasets, v_dataset);
    ELSE
        -- A Task with no Dataset item, primary link or TaskOriginal may only
        -- be dataset-independent plain text. The retry consumer signs this
        -- frozen path from the Task bucket; it never resolves a Dataset by
        -- path. Any image/video/lidar source is unsupported here and unknown.
        IF v_task.file_type IS DISTINCT FROM 'text' THEN
            RETURN NULL;
        END IF;
        IF pg_catalog.left(v_task.file_path, 18) = '__aap_reference__/' THEN
            RETURN NULL;
        END IF;
    END IF;

    IF p_source_extra ? 'accepted_input' THEN
        v_evidence := p_source_extra -> 'accepted_input';
        IF pg_catalog.jsonb_typeof(v_evidence) IS DISTINCT FROM 'object' THEN
            RETURN NULL;
        END IF;
        v_input_text := v_evidence ->> 'input_id';
        v_job_text := v_evidence ->> 'job_id';
        v_task_text := v_evidence ->> 'task_id';
        v_digest := v_evidence ->> 'sha256';
        v_text := v_evidence ->> 'byte_size';
        IF v_input_text IS NULL OR v_input_text !~ v_uuid_pattern
           OR v_job_text IS NULL OR v_job_text !~ v_uuid_pattern
           OR v_task_text IS DISTINCT FROM p_task_id::text
           OR v_digest IS NULL OR v_digest !~ '^[0-9a-f]{64}$'
           OR v_text IS NULL OR v_text !~ '^[1-9][0-9]{0,18}$' THEN
            RETURN NULL;
        END IF;
        IF p_accepted_input_id IS DISTINCT FROM v_input_text::uuid THEN
            RETURN NULL;
        END IF;
        SELECT input.dataset_item_id INTO v_item
          FROM public.asset_accepted_image_inputs AS input
         WHERE input.input_id = v_input_text::uuid
           AND input.job_id = v_job_text::uuid
           AND input.task_id = p_task_id
           AND input.project_id = p_project_id;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        IF v_item IS NOT NULL THEN
            SELECT item.dataset_id INTO v_dataset
              FROM public.dataset_items AS item
              JOIN public.datasets AS dataset ON dataset.id = item.dataset_id
             WHERE item.id = v_item;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            v_datasets := pg_catalog.array_append(v_datasets, v_dataset);
        END IF;
    ELSIF p_accepted_input_id IS NOT NULL THEN
        RETURN NULL;
    END IF;

    IF p_source_extra ? 'accepted_input_manifest' THEN
        v_evidence := p_source_extra -> 'accepted_input_manifest';
        IF pg_catalog.jsonb_typeof(v_evidence) IS DISTINCT FROM 'object' THEN
            RETURN NULL;
        END IF;
        v_text := v_evidence ->> 'protocol';
        v_manifest_text := v_evidence ->> 'manifest_id';
        v_task_text := v_evidence ->> 'task_id';
        v_digest := v_evidence ->> 'selection_digest';
        v_ordinal_text := v_evidence ->> 'ordinal';
        IF v_text IS DISTINCT FROM 'aap-assets-input-manifest/v1'
           OR v_manifest_text IS NULL OR v_manifest_text !~ v_uuid_pattern
           OR v_task_text IS DISTINCT FROM p_task_id::text
           OR v_digest IS NULL OR v_digest !~ '^[0-9a-f]{64}$'
           OR v_ordinal_text IS NULL OR v_ordinal_text !~ '^[1-9][0-9]{0,18}$' THEN
            RETURN NULL;
        END IF;
        SELECT member.dataset_item_id, member.file_path, member.file_type
          INTO v_item, v_member_path, v_member_type
          FROM public.asset_accepted_image_input_manifests AS header
          JOIN public.asset_accepted_image_input_manifest_members AS member
            ON member.job_id = header.job_id
           AND member.task_id = p_task_id
           AND member.ordinal = v_ordinal_text::bigint
         WHERE header.job_id = v_manifest_text::uuid
           AND header.project_id = p_project_id
           AND header.selection_digest = v_digest
           AND header.status = 'sealed'
           AND member.project_id = p_project_id
           AND member.file_type = 'image';
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        IF v_member_path IS DISTINCT FROM p_source_file_path
           OR v_member_type IS DISTINCT FROM p_source_file_type THEN
            RETURN NULL;
        END IF;
        IF v_item IS NOT NULL THEN
            SELECT item.dataset_id INTO v_dataset
              FROM public.dataset_items AS item
              JOIN public.datasets AS dataset ON dataset.id = item.dataset_id
             WHERE item.id = v_item;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;
            v_datasets := pg_catalog.array_append(v_datasets, v_dataset);
        END IF;
    END IF;

    v_has_reference := p_source_extra ? 'reference_input';
    v_has_original := p_source_extra ? 'original_input';
    v_has_video := (
        p_source_extra -> 'accepted_video_input' IS NOT NULL
        OR (p_source_extra -> 'original_video_input' IS NOT NULL
            AND p_source_extra -> 'original_video_input' <> 'null'::jsonb
            AND p_source_extra -> 'original_video_input' <> 'false'::jsonb)
        OR (p_source_extra -> 'video_execution' IS NOT NULL
            AND p_source_extra -> 'video_execution' <> 'null'::jsonb
            AND p_source_extra -> 'video_execution' <> 'false'::jsonb)
    );
    IF v_has_reference AND v_task.dataset_item_id IS NULL THEN
        RETURN NULL;
    END IF;
    IF v_has_original
       AND v_task.dataset_item_id IS NULL
       AND v_task.original_id IS NULL THEN
        RETURN NULL;
    END IF;
    IF v_has_video THEN
        IF v_task.dataset_item_id IS NULL
           OR v_task.file_type IS DISTINCT FROM 'video' THEN
            RETURN NULL;
        END IF;
        IF p_source_extra ? 'accepted_video_input' THEN
            v_evidence := p_source_extra -> 'accepted_video_input';
            IF pg_catalog.jsonb_typeof(v_evidence) IS DISTINCT FROM 'object' THEN
                RETURN NULL;
            END IF;
            v_task_text := v_evidence ->> 'task_id';
            v_text := v_evidence ->> 'project_id';
            IF v_task_text IS DISTINCT FROM p_task_id::text
               OR v_text IS DISTINCT FROM p_project_id::text THEN
                RETURN NULL;
            END IF;
        END IF;
    END IF;

    RETURN ARRAY(
        SELECT DISTINCT dataset_id
          FROM pg_catalog.unnest(v_datasets) AS dataset_id
         WHERE dataset_id IS NOT NULL
         ORDER BY dataset_id
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.retry_task_dataset_scope(uuid,uuid,uuid,text,text,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
-- A retry authority proof is a same-Task transfer, never a new source. Validate
-- the single Task binding against the exact Task/Item/original owners. The
-- Jobs layer separately validates actor, project, snapshot and immutability.
CREATE FUNCTION aap_assets_v1.retry_authority_binding_matches(
    p_task_id uuid,
    p_project_id uuid,
    p_source_dataset_item_id uuid,
    p_source_file_path text,
    p_source_file_type text,
    p_task_version text,
    p_expected_batch_id text,
    p_authority jsonb
) RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_task record;
    v_binding jsonb;
    v_reference jsonb;
    v_original jsonb;
    v_facts jsonb;
    v_relation jsonb;
    v_item_facts jsonb;
    v_ownership jsonb;
    v_text text;
    v_dataset uuid;
    v_item uuid;
    v_kind text;
    v_primary_count integer;
    v_primary_item uuid;
    v_is_reference boolean;
    v_uuid_pattern constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
BEGIN
    IF p_task_id IS NULL OR p_project_id IS NULL
       OR p_source_file_path IS NULL
       OR p_source_file_type IS NULL
       OR p_task_version IS NULL
       OR pg_catalog.jsonb_typeof(p_authority) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    SELECT task.id, task.project_id, task.dataset_item_id, task.file_path,
           task.file_type, task.original_id
      INTO v_task
      FROM aap_labeling_v1.task_rows AS task
     WHERE task.id = p_task_id AND task.project_id = p_project_id;
    IF NOT FOUND
       OR v_task.dataset_item_id IS DISTINCT FROM p_source_dataset_item_id
       OR v_task.file_path IS DISTINCT FROM p_source_file_path
       OR v_task.file_type IS DISTINCT FROM p_source_file_type THEN
        RETURN false;
    END IF;

    SELECT pg_catalog.count(*) INTO v_primary_count
      FROM aap_labeling_v1.task_dataset_item_link_rows AS link
     WHERE link.task_id = p_task_id AND link.role = 'primary_lidar';
    IF v_primary_count > 1 THEN
        RETURN false;
    END IF;
    IF v_primary_count = 1 THEN
        SELECT link.dataset_item_id INTO v_primary_item
          FROM aap_labeling_v1.task_dataset_item_link_rows AS link
         WHERE link.task_id = p_task_id AND link.role = 'primary_lidar';
    END IF;

    IF v_task.dataset_item_id IS NOT NULL THEN
        IF v_task.original_id IS NOT NULL
           OR (v_primary_count = 1
               AND v_primary_item IS DISTINCT FROM v_task.dataset_item_id) THEN
            RETURN false;
        END IF;
        v_item := v_task.dataset_item_id;
        SELECT item.dataset_id, item.file_path, item.file_type
          INTO v_dataset, v_text, v_kind
          FROM public.dataset_items AS item
         WHERE item.id = v_item;
        IF NOT FOUND
           OR v_text IS DISTINCT FROM v_task.file_path
           OR v_kind IS DISTINCT FROM v_task.file_type THEN
            RETURN false;
        END IF;
        v_kind := 'item';
    ELSIF v_task.original_id IS NOT NULL THEN
        IF v_primary_count > 0 THEN
            RETURN false;
        END IF;
        SELECT original.object_key INTO v_text
          FROM public.task_originals AS original
         WHERE original.id = v_task.original_id
           AND original.task_id = p_task_id
           AND original.project_id = p_project_id;
        IF NOT FOUND OR v_text IS DISTINCT FROM v_task.file_path THEN
            RETURN false;
        END IF;
        v_kind := 'task_original';
    ELSIF v_primary_count = 1 THEN
        v_item := v_primary_item;
        SELECT item.dataset_id, item.file_path, item.file_type
          INTO v_dataset, v_text, v_kind
          FROM public.dataset_items AS item
         WHERE item.id = v_item;
        IF NOT FOUND
           OR v_text IS DISTINCT FROM v_task.file_path
           OR v_kind IS DISTINCT FROM v_task.file_type THEN
            RETURN false;
        END IF;
        v_kind := 'item';
    ELSE
        RETURN false;
    END IF;

    IF pg_catalog.jsonb_typeof(p_authority -> 'tasks') IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_array_length(p_authority -> 'tasks') <> 1 THEN
        RETURN false;
    END IF;
    v_binding := p_authority -> 'tasks' -> 0;
    IF pg_catalog.jsonb_typeof(v_binding) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF v_binding ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR v_binding ->> 'file_path' IS DISTINCT FROM p_source_file_path
       OR v_binding ->> 'version' IS DISTINCT FROM p_task_version THEN
        RETURN false;
    END IF;
    IF p_source_dataset_item_id IS NULL THEN
        IF v_binding -> 'item_id' IS DISTINCT FROM 'null'::jsonb THEN
            RETURN false;
        END IF;
    ELSIF v_binding ->> 'item_id' IS DISTINCT FROM p_source_dataset_item_id::text THEN
        RETURN false;
    END IF;
    IF p_expected_batch_id IS NULL THEN
        IF v_binding -> 'batch_id' IS DISTINCT FROM 'null'::jsonb THEN
            RETURN false;
        END IF;
    ELSIF v_binding ->> 'batch_id' IS DISTINCT FROM p_expected_batch_id THEN
        RETURN false;
    END IF;

    v_is_reference := pg_catalog.left(v_task.file_path, 18) = '__aap_reference__/';
    IF (v_binding ? 'reference') = (v_binding ? 'original') THEN
        RETURN false;
    END IF;

    IF v_is_reference THEN
        IF v_kind IS DISTINCT FROM 'item'
           OR NOT (v_binding ? 'reference')
           OR pg_catalog.jsonb_typeof(v_binding -> 'reference') IS DISTINCT FROM 'object' THEN
            RETURN false;
        END IF;
        v_reference := v_binding -> 'reference';
        IF v_reference ->> 'dataset_id' IS DISTINCT FROM v_dataset::text
           OR pg_catalog.jsonb_typeof(v_reference -> 'asset_id') IS DISTINCT FROM 'string'
           OR (v_reference ->> 'asset_id') !~ v_uuid_pattern
           OR pg_catalog.jsonb_typeof(v_reference -> 'sha256') IS DISTINCT FROM 'string'
           OR (v_reference ->> 'sha256') !~ '^[0-9a-f]{64}$'
           OR pg_catalog.jsonb_typeof(v_reference -> 'size') IS DISTINCT FROM 'number' THEN
            RETURN false;
        END IF;
        RETURN true;
    END IF;

    IF NOT (v_binding ? 'original')
       OR pg_catalog.jsonb_typeof(v_binding -> 'original') IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    v_original := v_binding -> 'original';
    IF pg_catalog.jsonb_typeof(v_original -> 'owner_kind') IS DISTINCT FROM 'string'
       OR v_original ->> 'object_key' IS DISTINCT FROM v_task.file_path
       OR pg_catalog.jsonb_typeof(v_original -> 'sha256') IS DISTINCT FROM 'string'
       OR (v_original ->> 'sha256') !~ '^[0-9a-f]{64}$'
       OR pg_catalog.jsonb_typeof(v_original -> 'size_bytes') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_original -> 'facts') IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    v_facts := v_original -> 'facts';
    IF pg_catalog.jsonb_typeof(v_facts -> 'relation') IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    v_relation := v_facts -> 'relation';
    IF v_relation ->> 'task_id' IS DISTINCT FROM p_task_id::text
       OR v_relation ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR v_relation ->> 'file_path' IS DISTINCT FROM v_task.file_path
       OR v_relation ->> 'file_type' IS DISTINCT FROM v_task.file_type THEN
        RETURN false;
    END IF;

    IF v_kind = 'task_original' THEN
        IF v_original ->> 'owner_kind' IS DISTINCT FROM 'task_original'
           OR v_facts ->> 'owner_kind' IS DISTINCT FROM 'task_original'
           OR v_facts -> 'item_id' IS DISTINCT FROM 'null'::jsonb
           OR pg_catalog.jsonb_typeof(v_facts -> 'ownership_facts')
              IS DISTINCT FROM 'object' THEN
            RETURN false;
        END IF;
        v_ownership := v_facts -> 'ownership_facts';
        IF v_ownership ->> 'task_id' IS DISTINCT FROM p_task_id::text
           OR v_ownership ->> 'project_id' IS DISTINCT FROM p_project_id::text
           OR v_ownership ->> 'object_key' IS DISTINCT FROM v_task.file_path THEN
            RETURN false;
        END IF;
        RETURN true;
    END IF;

    IF v_original ->> 'owner_kind' IS DISTINCT FROM 'dataset_asset'
       OR v_facts ->> 'owner_kind' IS DISTINCT FROM 'dataset_asset'
       OR v_facts ->> 'item_id' IS DISTINCT FROM v_item::text
       OR pg_catalog.jsonb_typeof(v_facts -> 'item_facts')
          IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    v_item_facts := v_facts -> 'item_facts';
    IF v_item_facts ->> 'id' IS DISTINCT FROM v_item::text
       OR v_item_facts ->> 'dataset_id' IS DISTINCT FROM v_dataset::text
       OR v_item_facts ->> 'file_path' IS DISTINCT FROM v_task.file_path
       OR v_item_facts ->> 'file_type' IS DISTINCT FROM v_task.file_type THEN
        RETURN false;
    END IF;
    RETURN true;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.retry_authority_binding_matches(uuid,uuid,uuid,text,text,text,text,jsonb) FROM PUBLIC;
-- aap-statement-break
-- The Jobs wrapper binds the receipt to the admitted Job, the same-transaction
-- attempt birth marker, the frozen write scope, the Task row and the failed
-- prediction. It never accepts a payload field as proof by itself.
CREATE FUNCTION aap_jobs_v1.retry_born_dataset_scope(
    p_job_id uuid, p_project_id uuid, p_actor_id uuid, p_payload jsonb
) RETURNS uuid[]
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_attempt public.prediction_retry_attempts%ROWTYPE;
    v_scope jsonb;
    v_task record;
    v_failed record;
    v_uuid_pattern constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_text text;
    v_task_text text;
    v_failed_text text;
    v_digest text;
    v_queue text;
    v_task_id uuid;
    v_source_item uuid;
    v_accepted_input uuid;
    v_source_path text;
    v_source_type text;
    v_datasets uuid[];
BEGIN
    IF p_job_id IS NULL OR p_project_id IS NULL OR p_actor_id IS NULL
       OR p_payload IS NULL
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN
        RETURN NULL;
    END IF;
    SELECT * INTO v_job FROM public.async_jobs WHERE id = p_job_id;
    IF NOT FOUND
       OR v_job.kind IS DISTINCT FROM 'prediction_retry'
       OR v_job.project_id IS DISTINCT FROM p_project_id
       OR v_job.user_id IS DISTINCT FROM p_actor_id
       OR v_job.payload IS DISTINCT FROM p_payload
       OR pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object' THEN
        RETURN NULL;
    END IF;
    SELECT * INTO v_attempt
      FROM public.prediction_retry_attempts
     WHERE job_id = p_job_id;
    IF NOT FOUND
       OR v_attempt.project_id IS DISTINCT FROM p_project_id
       OR v_attempt.actor_id IS DISTINCT FROM p_actor_id
       OR v_attempt.birth_xid IS DISTINCT FROM pg_catalog.txid_current()
       OR v_attempt.legacy_delivery IS NOT FALSE
       OR pg_catalog.jsonb_typeof(v_attempt.write_scope) IS DISTINCT FROM 'object'
       OR pg_catalog.jsonb_typeof(v_attempt.source_extra) IS DISTINCT FROM 'object' THEN
        RETURN NULL;
    END IF;
    v_scope := v_attempt.write_scope;

    IF pg_catalog.jsonb_typeof(p_payload -> 'failed_prediction_id') IS DISTINCT FROM 'string' THEN
        RETURN NULL;
    END IF;
    v_failed_text := p_payload ->> 'failed_prediction_id';
    IF v_failed_text IS NULL OR v_failed_text !~ v_uuid_pattern THEN
        RETURN NULL;
    END IF;
    IF v_failed_text::uuid IS DISTINCT FROM v_attempt.failed_prediction_id THEN
        RETURN NULL;
    END IF;

    IF pg_catalog.jsonb_typeof(p_payload -> 'task_id') NOT IN ('string','null') THEN
        RETURN NULL;
    END IF;
    IF p_payload -> 'task_id' = 'null'::jsonb THEN
        IF v_attempt.task_id IS NOT NULL THEN
            RETURN NULL;
        END IF;
        v_task_id := NULL;
    ELSE
        v_task_text := p_payload ->> 'task_id';
        IF v_task_text IS NULL OR v_task_text !~ v_uuid_pattern THEN
            RETURN NULL;
        END IF;
        v_task_id := v_task_text::uuid;
        IF v_task_id IS DISTINCT FROM v_attempt.task_id THEN
            RETURN NULL;
        END IF;
    END IF;
    IF v_task_id IS NULL THEN
        RETURN NULL;
    END IF;

    IF pg_catalog.jsonb_typeof(p_payload -> 'prediction_retry_request_digest') IS DISTINCT FROM 'string' THEN
        RETURN NULL;
    END IF;
    v_digest := p_payload ->> 'prediction_retry_request_digest';
    IF v_digest IS NULL OR v_digest !~ '^[0-9a-f]{64}$'
       OR v_digest IS DISTINCT FROM v_attempt.request_digest THEN
        RETURN NULL;
    END IF;

    IF pg_catalog.jsonb_typeof(p_payload -> 'execution_queue') NOT IN ('string','null') THEN
        RETURN NULL;
    END IF;
    IF p_payload -> 'execution_queue' = 'null'::jsonb THEN
        v_queue := NULL;
    ELSE
        v_queue := p_payload ->> 'execution_queue';
        IF v_queue IS NULL
           OR pg_catalog.octet_length(v_queue) NOT BETWEEN 1 AND 256 THEN
            RETURN NULL;
        END IF;
    END IF;
    IF v_queue IS DISTINCT FROM v_attempt.execution_queue THEN
        RETURN NULL;
    END IF;

    IF v_scope ->> 'operation' IS DISTINCT FROM 'prediction_retry'
       OR v_scope ->> 'job_id' IS DISTINCT FROM p_job_id::text
       OR v_scope -> 'job_id_known' IS DISTINCT FROM 'true'::jsonb
       OR v_scope ->> 'task_id' IS DISTINCT FROM v_task_id::text
       OR v_scope ->> 'project_id' IS DISTINCT FROM p_project_id::text
       OR v_scope ->> 'actor_id' IS DISTINCT FROM p_actor_id::text
       OR v_scope -> 'actor_known' IS DISTINCT FROM 'true'::jsonb
       OR v_scope -> 'execution_scope_known' IS DISTINCT FROM 'true'::jsonb
       OR v_scope -> 'source_binding_known' IS DISTINCT FROM 'true'::jsonb
       OR v_scope -> 'expected_batch_id_known' IS DISTINCT FROM 'true'::jsonb
       OR v_scope -> 'execution_scope' IS DISTINCT FROM 'null'::jsonb THEN
        RETURN NULL;
    END IF;

    v_text := v_scope ->> 'task_source_generation';
    IF pg_catalog.jsonb_typeof(v_scope -> 'task_source_generation') IS DISTINCT FROM 'number'
       OR v_text IS NULL OR v_text !~ '^[1-9][0-9]{0,18}$' THEN
        RETURN NULL;
    END IF;
    v_text := v_scope ->> 'task_version';
    IF pg_catalog.jsonb_typeof(v_scope -> 'task_version') IS DISTINCT FROM 'number'
       OR v_text IS NULL OR v_text !~ '^[1-9][0-9]{0,15}$' THEN
        RETURN NULL;
    END IF;
    IF pg_catalog.jsonb_typeof(v_scope -> 'task_status') IS DISTINCT FROM 'string'
       OR v_scope ->> 'task_status' IS NULL
       OR pg_catalog.char_length(v_scope ->> 'task_status') NOT BETWEEN 1 AND 30 THEN
        RETURN NULL;
    END IF;

    v_source_path := v_scope ->> 'source_file_path';
    IF v_source_path IS NULL
       OR pg_catalog.char_length(v_source_path) NOT BETWEEN 1 AND 1000 THEN
        RETURN NULL;
    END IF;
    v_source_type := v_scope ->> 'source_file_type';
    IF v_source_type IS NULL
       OR pg_catalog.char_length(v_source_type) NOT BETWEEN 1 AND 20 THEN
        RETURN NULL;
    END IF;

    IF v_scope -> 'source_dataset_item_id' = 'null'::jsonb THEN
        v_source_item := NULL;
    ELSIF pg_catalog.jsonb_typeof(v_scope -> 'source_dataset_item_id') = 'string' THEN
        v_text := v_scope ->> 'source_dataset_item_id';
        IF v_text !~ v_uuid_pattern THEN
            RETURN NULL;
        END IF;
        v_source_item := v_text::uuid;
    ELSE
        RETURN NULL;
    END IF;

    IF v_scope -> 'accepted_input_id' = 'null'::jsonb THEN
        v_accepted_input := NULL;
    ELSIF pg_catalog.jsonb_typeof(v_scope -> 'accepted_input_id') = 'string' THEN
        v_text := v_scope ->> 'accepted_input_id';
        IF v_text !~ v_uuid_pattern THEN
            RETURN NULL;
        END IF;
        v_accepted_input := v_text::uuid;
    ELSE
        RETURN NULL;
    END IF;

    SELECT task.id, task.project_id, task.dataset_item_id, task.file_path,
           task.file_type
      INTO v_task
      FROM aap_labeling_v1.task_rows AS task
     WHERE task.id = v_task_id;
    IF NOT FOUND
       OR v_task.project_id IS DISTINCT FROM p_project_id
       OR v_task.dataset_item_id IS DISTINCT FROM v_source_item
       OR v_task.file_path IS DISTINCT FROM v_source_path
       OR v_task.file_type IS DISTINCT FROM v_source_type THEN
        RETURN NULL;
    END IF;

    SELECT failed.id, failed.project_id INTO v_failed
      FROM public.failed_predictions AS failed
     WHERE failed.id = v_attempt.failed_prediction_id;
    IF NOT FOUND OR v_failed.project_id IS DISTINCT FROM p_project_id THEN
        RETURN NULL;
    END IF;

    v_datasets := aap_assets_v1.retry_task_dataset_scope(
        v_task_id,
        p_project_id,
        v_source_item,
        v_source_path,
        v_source_type,
        v_accepted_input,
        v_attempt.source_extra
    );
    RETURN v_datasets;
END
$function$;
-- aap-statement-break

REVOKE ALL ON FUNCTION aap_jobs_v1.retry_born_dataset_scope(uuid,uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
-- The ordinary retry API binds an execution snapshot and, for image/video
-- retries, a same-Task authority proof after admitting the Job. Validate that
-- single transfer against the final NEW payload/snapshot and the same attempt.
CREATE FUNCTION aap_jobs_v1.retry_authority_snapshot_matches(
    p_job_id uuid,
    p_payload jsonb,
    p_snapshot_id uuid
) RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_job record;
    v_attempt record;
    v_scope jsonb;
    v_image jsonb;
    v_video jsonb;
    v_authority jsonb;
    v_snapshot_digest text;
    v_extra jsonb;
    v_text text;
    v_task_version text;
    v_expected_batch text;
    v_source_item uuid;
    v_source_path text;
    v_source_type text;
    v_uuid_pattern constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
BEGIN
    IF p_job_id IS NULL OR p_payload IS NULL
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    SELECT job.kind, job.project_id, job.user_id
      INTO v_job
      FROM public.async_jobs AS job
     WHERE job.id = p_job_id;
    IF NOT FOUND
       OR v_job.kind IS DISTINCT FROM 'prediction_retry'
       OR v_job.project_id IS NULL
       OR v_job.user_id IS NULL THEN
        RETURN false;
    END IF;
    SELECT attempt.*
      INTO v_attempt
      FROM public.prediction_retry_attempts AS attempt
     WHERE attempt.job_id = p_job_id;
    IF NOT FOUND
       OR v_attempt.project_id IS DISTINCT FROM v_job.project_id
       OR v_attempt.actor_id IS DISTINCT FROM v_job.user_id
       OR v_attempt.birth_xid IS DISTINCT FROM pg_catalog.txid_current()
       OR pg_catalog.jsonb_typeof(v_attempt.write_scope) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    v_scope := v_attempt.write_scope;
    v_text := v_scope ->> 'task_version';
    IF v_text IS NULL OR v_text !~ '^[1-9][0-9]{0,15}$' THEN
        RETURN false;
    END IF;
    v_task_version := v_text;
    v_expected_batch := v_scope ->> 'expected_batch_id';
    v_source_path := v_scope ->> 'source_file_path';
    v_source_type := v_scope ->> 'source_file_type';
    IF v_source_path IS NULL OR v_source_type IS NULL THEN
        RETURN false;
    END IF;
    IF v_scope -> 'source_dataset_item_id' = 'null'::jsonb THEN
        v_source_item := NULL;
    ELSIF pg_catalog.jsonb_typeof(v_scope -> 'source_dataset_item_id') = 'string' THEN
        v_text := v_scope ->> 'source_dataset_item_id';
        IF v_text !~ v_uuid_pattern THEN
            RETURN false;
        END IF;
        v_source_item := v_text::uuid;
    ELSE
        RETURN false;
    END IF;

    IF p_snapshot_id IS NOT NULL THEN
        SELECT snapshot.digest INTO v_snapshot_digest
          FROM public.prediction_execution_snapshots AS snapshot
         WHERE snapshot.id = p_snapshot_id
           AND snapshot.project_id = v_job.project_id;
        IF NOT FOUND
           OR v_snapshot_digest IS NULL
           OR v_snapshot_digest !~ '^[0-9a-f]{64}$' THEN
            RETURN false;
        END IF;
    END IF;

    v_image := COALESCE(p_payload -> 'prediction_authority', 'null'::jsonb);
    v_video := COALESCE(p_payload -> 'video_prediction_authority', 'null'::jsonb);
    IF v_image = 'null'::jsonb AND v_video = 'null'::jsonb THEN
        IF p_snapshot_id IS NULL THEN
            RETURN true;
        END IF;
        IF v_snapshot_digest IS NULL THEN
            RETURN false;
        END IF;
        v_extra := v_attempt.source_extra -> 'execution_snapshot';
        IF v_extra IS NOT NULL AND v_extra <> 'null'::jsonb THEN
            IF v_extra ->> 'id' IS DISTINCT FROM p_snapshot_id::text THEN
                RETURN false;
            END IF;
            IF v_extra ->> 'digest' IS NOT NULL
               AND v_extra ->> 'digest' IS DISTINCT FROM v_snapshot_digest THEN
                RETURN false;
            END IF;
        END IF;
        RETURN true;
    END IF;
    IF v_image <> 'null'::jsonb AND v_video <> 'null'::jsonb THEN
        RETURN false;
    END IF;
    v_authority := CASE
        WHEN v_image <> 'null'::jsonb THEN v_image
        ELSE v_video
    END;
    IF pg_catalog.jsonb_typeof(v_authority) IS DISTINCT FROM 'object' THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_typeof(v_authority -> 'schema') IS DISTINCT FROM 'number'
       OR (v_authority ->> 'schema') NOT IN ('1','2')
       OR v_authority ->> 'job_kind' IS DISTINCT FROM 'prediction_retry'
       OR v_authority ->> 'actor_id' IS DISTINCT FROM v_job.user_id::text
       OR v_authority ->> 'project_id' IS DISTINCT FROM v_job.project_id::text
       OR pg_catalog.jsonb_typeof(v_authority -> 'tasks') IS DISTINCT FROM 'array'
       OR v_authority ->> 'predict_mode' IS DISTINCT FROM 'append' THEN
        RETURN false;
    END IF;
    IF pg_catalog.jsonb_array_length(v_authority -> 'tasks') <> 1 THEN
        RETURN false;
    END IF;
    IF p_snapshot_id IS NULL
       OR v_authority ->> 'snapshot_id' IS DISTINCT FROM p_snapshot_id::text
       OR v_authority ->> 'snapshot_digest' IS NULL
       OR v_authority ->> 'snapshot_digest' !~ '^[0-9a-f]{64}$'
       OR v_authority ->> 'snapshot_digest' IS DISTINCT FROM v_snapshot_digest THEN
        RETURN false;
    END IF;
    IF v_expected_batch IS NULL THEN
        IF v_authority -> 'batch_id' IS DISTINCT FROM 'null'::jsonb THEN
            RETURN false;
        END IF;
    ELSIF v_authority ->> 'batch_id' IS DISTINCT FROM v_expected_batch THEN
        RETURN false;
    END IF;
    v_text := v_authority ->> 'execution_scope';
    IF v_text IS NULL OR v_text NOT IN ('workbench','bulk') THEN
        RETURN false;
    END IF;
    v_text := v_authority ->> 'execution_unit';
    IF v_text IS NOT NULL AND v_text NOT IN ('image','video') THEN
        RETURN false;
    END IF;
    IF v_video <> 'null'::jsonb THEN
        IF v_authority ->> 'media_type' IS DISTINCT FROM 'video'
           OR v_source_type IS DISTINCT FROM 'video'
           OR (v_text IS NOT NULL AND v_text IS DISTINCT FROM 'video') THEN
            RETURN false;
        END IF;
    ELSE
        IF v_source_type = 'video' THEN
            RETURN false;
        END IF;
    END IF;

    RETURN aap_assets_v1.retry_authority_binding_matches(
        v_attempt.task_id,
        v_job.project_id,
        v_source_item,
        v_source_path,
        v_source_type,
        v_task_version,
        v_expected_batch,
        v_authority
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.retry_authority_snapshot_matches(uuid,jsonb,uuid) FROM PUBLIC;
-- aap-statement-break
-- The family payload fence owns only authority/snapshot transitions on a Job
-- that already has a non-NULL born receipt. Core fields remain with the shared
-- Jobs guard. A transition is legal once, in the admission transaction, and
-- must match the same Task/Item/original/snapshot exactly.
CREATE FUNCTION aap_jobs_v1.guard_retry_born_authority()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_receipt uuid[];
    v_old_image jsonb;
    v_old_video jsonb;
    v_new_image jsonb;
    v_new_video jsonb;
BEGIN
    IF NEW.kind IS DISTINCT FROM 'prediction_retry' THEN
        RETURN NEW;
    END IF;
    SELECT dataset_ids INTO v_receipt
      FROM aap_jobs_v1.fixture_worker_job_datasets
     WHERE job_id = NEW.id AND kind = 'prediction_retry';
    IF NOT FOUND OR v_receipt IS NULL THEN
        -- Unbound/historical shapes keep their existing product behavior.
        RETURN NEW;
    END IF;
    v_old_image := COALESCE(OLD.payload -> 'prediction_authority', 'null'::jsonb);
    v_old_video := COALESCE(OLD.payload -> 'video_prediction_authority', 'null'::jsonb);
    v_new_image := COALESCE(NEW.payload -> 'prediction_authority', 'null'::jsonb);
    v_new_video := COALESCE(NEW.payload -> 'video_prediction_authority', 'null'::jsonb);
    IF v_old_image IS NOT DISTINCT FROM v_new_image
       AND v_old_video IS NOT DISTINCT FROM v_new_video
       AND OLD.execution_snapshot_id IS NOT DISTINCT FROM NEW.execution_snapshot_id THEN
        RETURN NEW;
    END IF;

    IF (v_old_image <> 'null'::jsonb
        AND v_new_image IS DISTINCT FROM v_old_image)
       OR (v_old_video <> 'null'::jsonb
           AND v_new_video IS DISTINCT FROM v_old_video) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry authority is immutable';
    END IF;
    IF OLD.execution_snapshot_id IS NOT NULL
       AND NEW.execution_snapshot_id IS DISTINCT FROM OLD.execution_snapshot_id THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry snapshot is immutable';
    END IF;
    IF aap_jobs_v1.retry_authority_snapshot_matches(
        NEW.id, NEW.payload, NEW.execution_snapshot_id
    ) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry authority changed the Task source';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_retry_born_authority() FROM PUBLIC;
-- aap-statement-break
CREATE TRIGGER trg_async_jobs_retry_born_authority
BEFORE UPDATE OF payload, execution_snapshot_id ON public.async_jobs
FOR EACH ROW
WHEN (NEW.kind = 'prediction_retry')
EXECUTE FUNCTION aap_jobs_v1.guard_retry_born_authority();
-- aap-statement-break
-- The shared AFTER INSERT receipt is derived before delivery/execution/member
-- rows exist. This deferred family guard proves those durable rows still bind
-- the same admission at commit, without depending on mutable phase/state.
CREATE FUNCTION aap_jobs_v1.guard_retry_born_dataset_scope()
RETURNS trigger LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_job public.async_jobs%ROWTYPE;
    v_attempt public.prediction_retry_attempts%ROWTYPE;
    v_delivery public.prediction_retry_deliveries%ROWTYPE;
    v_execution public.batch_result_executions%ROWTYPE;
    v_member public.batch_result_members%ROWTYPE;
    v_birth_xid bigint;
    v_member_count bigint;
    v_derived uuid[];
    v_uuid_pattern constant text :=
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    v_failed_text text;
    v_task_text text;
    v_digest text;
    v_queue text;
    v_task_id uuid;
BEGIN
    IF NEW.kind IS DISTINCT FROM 'prediction_retry' THEN
        RETURN NEW;
    END IF;
    IF NEW.job_id IS NULL OR NEW.project_id IS NULL OR NEW.actor_id IS NULL
       OR (NEW.dataset_ids IS NOT NULL
           AND pg_catalog.array_position(NEW.dataset_ids, NULL::uuid) IS NOT NULL) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset receipt is invalid';
    END IF;

    SELECT birth_xid INTO v_birth_xid
      FROM public.prediction_retry_attempts
     WHERE job_id = NEW.job_id;
    IF NOT FOUND OR v_birth_xid IS DISTINCT FROM pg_catalog.txid_current() THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset admission is not current';
    END IF;

    SELECT * INTO v_job FROM public.async_jobs WHERE id = NEW.job_id;
    IF NOT FOUND
       OR v_job.kind IS DISTINCT FROM NEW.kind
       OR v_job.project_id IS DISTINCT FROM NEW.project_id
       OR v_job.user_id IS DISTINCT FROM NEW.actor_id
       OR pg_catalog.jsonb_typeof(v_job.payload) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset job identity is missing';
    END IF;

    SELECT * INTO v_attempt
      FROM public.prediction_retry_attempts
     WHERE job_id = NEW.job_id;
    IF NOT FOUND
       OR v_attempt.project_id IS DISTINCT FROM NEW.project_id
       OR v_attempt.actor_id IS DISTINCT FROM NEW.actor_id THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset attempt is missing';
    END IF;

    IF pg_catalog.jsonb_typeof(v_job.payload -> 'failed_prediction_id') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset payload is malformed';
    END IF;
    v_failed_text := v_job.payload ->> 'failed_prediction_id';
    IF v_failed_text IS NULL OR v_failed_text !~ v_uuid_pattern THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset payload is malformed';
    END IF;
    IF v_failed_text::uuid IS DISTINCT FROM v_attempt.failed_prediction_id THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset attempt identity changed';
    END IF;

    IF pg_catalog.jsonb_typeof(v_job.payload -> 'task_id') NOT IN ('string','null') THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset payload is malformed';
    END IF;
    IF v_job.payload -> 'task_id' = 'null'::jsonb THEN
        v_task_id := NULL;
    ELSE
        v_task_text := v_job.payload ->> 'task_id';
        IF v_task_text IS NULL OR v_task_text !~ v_uuid_pattern THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='prediction retry born dataset payload is malformed';
        END IF;
        v_task_id := v_task_text::uuid;
    END IF;
    IF v_task_id IS DISTINCT FROM v_attempt.task_id THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset attempt identity changed';
    END IF;

    IF pg_catalog.jsonb_typeof(v_job.payload -> 'prediction_retry_request_digest') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset payload is malformed';
    END IF;
    v_digest := v_job.payload ->> 'prediction_retry_request_digest';
    IF v_digest IS NULL OR v_digest !~ '^[0-9a-f]{64}$'
       OR v_digest IS DISTINCT FROM v_attempt.request_digest THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset attempt identity changed';
    END IF;

    IF pg_catalog.jsonb_typeof(v_job.payload -> 'execution_queue') NOT IN ('string','null') THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset payload is malformed';
    END IF;
    IF v_job.payload -> 'execution_queue' = 'null'::jsonb THEN
        v_queue := NULL;
    ELSE
        v_queue := v_job.payload ->> 'execution_queue';
        IF v_queue IS NULL
           OR pg_catalog.octet_length(v_queue) NOT BETWEEN 1 AND 256 THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='prediction retry born dataset payload is malformed';
        END IF;
    END IF;

    SELECT * INTO v_delivery
      FROM public.prediction_retry_deliveries
     WHERE job_id = NEW.job_id;
    IF NOT FOUND
       OR v_delivery.failed_prediction_id IS DISTINCT FROM v_attempt.failed_prediction_id
       OR v_delivery.actor_id IS DISTINCT FROM v_attempt.actor_id
       OR v_delivery.project_id IS DISTINCT FROM v_attempt.project_id
       OR v_delivery.execution_queue IS DISTINCT FROM v_queue THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset delivery is missing';
    END IF;

    SELECT * INTO v_execution
      FROM public.batch_result_executions
     WHERE job_id = NEW.job_id;
    IF NOT FOUND
       OR v_execution.actor_id IS DISTINCT FROM v_attempt.actor_id
       OR v_execution.project_id IS DISTINCT FROM v_attempt.project_id
       OR v_execution.request_digest IS DISTINCT FROM v_attempt.request_digest THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset execution is missing';
    END IF;

    SELECT pg_catalog.count(*) INTO v_member_count
      FROM public.batch_result_members AS member
     WHERE member.job_id = NEW.job_id;
    IF v_member_count > 1 THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset member scope changed';
    ELSIF v_member_count = 1 THEN
        SELECT * INTO v_member
          FROM public.batch_result_members AS member
         WHERE member.job_id = NEW.job_id;
        IF v_member.ordinal IS DISTINCT FROM 1
           OR v_member.task_id IS DISTINCT FROM v_attempt.task_id
           OR v_member.write_scope IS DISTINCT FROM v_attempt.write_scope THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='prediction retry born dataset member scope changed';
        END IF;
    END IF;

    v_derived := aap_jobs_v1.retry_born_dataset_scope(
        NEW.job_id, NEW.project_id, NEW.actor_id, v_job.payload
    );
    IF v_derived IS NULL THEN
        IF NEW.dataset_ids IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='prediction retry born dataset scope is unknown';
        END IF;
    ELSIF NEW.dataset_ids IS DISTINCT FROM v_derived THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry born dataset scope changed';
    END IF;
    IF NEW.dataset_ids IS NOT NULL
       AND aap_jobs_v1.retry_authority_snapshot_matches(
           NEW.job_id, v_job.payload, v_job.execution_snapshot_id
       ) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='prediction retry final authority changed the born scope';
    END IF;
    RETURN NEW;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_retry_born_dataset_scope() FROM PUBLIC;
-- aap-statement-break
CREATE CONSTRAINT TRIGGER aap_retry_born_dataset_scope_guard
AFTER INSERT ON aap_jobs_v1.fixture_worker_job_datasets
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
WHEN (NEW.kind = 'prediction_retry')
EXECUTE FUNCTION aap_jobs_v1.guard_retry_born_dataset_scope();
