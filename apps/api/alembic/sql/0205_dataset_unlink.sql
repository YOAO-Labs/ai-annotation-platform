CREATE TABLE public.dataset_unlink_operations (
    operation_id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    project_id uuid NOT NULL,
    request jsonb NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now()
);
-- aap-statement-break
REVOKE ALL ON TABLE public.dataset_unlink_operations FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1._dataset_unlink_failure(p_schema text, p_code text)
RETURNS jsonb
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'schema', p_schema, 'ok', false,
        'error', pg_catalog.jsonb_build_object('code', p_code)
    )
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1._dataset_unlink_failure(text, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.preview_dataset_unlink(
    p_actor_id uuid, p_dataset_id uuid, p_project_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_schema constant text := 'aap-dataset-unlink-preview/v1';
    v_manager jsonb;
    v_preview jsonb;
BEGIN
    IF p_actor_id IS NULL OR p_dataset_id IS NULL OR p_project_id IS NULL THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'invalid_request');
    END IF;
    v_manager := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_manager ->> 'schema' IS DISTINCT FROM 'aap-project-manager/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink authority contract mismatch';
    END IF;
    IF v_manager -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false, 'error', v_manager -> 'error'
        );
    END IF;
    v_preview := aap_labeling_v1.preview_dataset_unlink(p_dataset_id, p_project_id);
    IF v_preview ->> 'schema' IS DISTINCT FROM v_schema
       OR v_preview -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_preview -> 'data') IS DISTINCT FROM 'object'
       OR ((v_preview -> 'data') - ARRAY['will_delete_tasks', 'will_delete_annotations', 'will_delete_batches']) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(v_preview #> '{data,will_delete_tasks}') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_preview #> '{data,will_delete_annotations}') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_preview #> '{data,will_delete_batches}') IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink preview contract mismatch';
    END IF;
    IF (v_preview #>> '{data,will_delete_tasks}')::bigint < 0
       OR (v_preview #>> '{data,will_delete_annotations}')::bigint < 0
       OR (v_preview #>> '{data,will_delete_batches}')::bigint < 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid unlink preview counts';
    END IF;
    RETURN v_preview;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.preview_dataset_unlink(uuid, uuid, uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.unlink_dataset(
    p_operation_id uuid, p_actor_id uuid, p_dataset_id uuid,
    p_project_id uuid, p_cleanup_bucket text, p_audit_context jsonb DEFAULT NULL
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_schema constant text := 'aap-dataset-unlink/v1';
    v_actor jsonb;
    v_manager jsonb;
    v_request jsonb;
    v_saved public.dataset_unlink_operations%ROWTYPE;
    v_dataset_id uuid;
    v_project_id uuid;
    v_association_id uuid;
    v_guard jsonb;
    v_deleted jsonb;
    v_data jsonb;
    v_tasks bigint;
    v_annotations bigint;
    v_batches bigint;
    v_batch_ids uuid[];
    v_audit_id bigint;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_dataset_id IS NULL
       OR p_project_id IS NULL OR p_cleanup_bucket IS NULL
       OR pg_catalog.char_length(p_cleanup_bucket) NOT BETWEEN 3 AND 63
       OR p_cleanup_bucket !~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'
       OR p_cleanup_bucket ~ '^[0-9]+(\.[0-9]+){3}$'
       OR aap_audit_v1.valid_request_context(p_audit_context) IS DISTINCT FROM true THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'invalid_request');
    END IF;
    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink identity contract mismatch';
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false, 'error', v_actor -> 'error'
        );
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-dataset-unlink:' || p_operation_id::text, 0)
    );
    v_request := pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', p_actor_id,
        'dataset_id', p_dataset_id, 'project_id', p_project_id,
        'cleanup_bucket', p_cleanup_bucket
    );
    SELECT receipt.* INTO v_saved
    FROM public.dataset_unlink_operations AS receipt
    WHERE receipt.operation_id = p_operation_id FOR UPDATE;
    IF FOUND THEN
        IF v_saved.request IS DISTINCT FROM v_request THEN
            RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'operation_conflict');
        END IF;
        RETURN v_saved.result;
    END IF;

    -- Keep the existing append/create order: Dataset precedes Project/association.
    v_dataset_id := aap_assets_v1.lock_dataset_reference(p_dataset_id);
    IF v_dataset_id IS NULL THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'dataset_missing');
    END IF;
    v_manager := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_manager ->> 'schema' IS DISTINCT FROM 'aap-project-manager/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink authority contract mismatch';
    END IF;
    IF v_manager -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false, 'error', v_manager -> 'error'
        );
    END IF;
    SELECT project.id INTO v_project_id FROM public.projects AS project
    WHERE project.id = p_project_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'project_missing');
    END IF;
    v_association_id := aap_assets_v1.lock_current_association(p_dataset_id, p_project_id);
    IF v_association_id IS NULL THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'link_missing');
    END IF;
    PERFORM aap_labeling_v1.lock_dataset_unlink_scope(p_dataset_id, p_project_id);
    v_guard := aap_learning_v1.guard_dataset_unlink(p_dataset_id, p_project_id);
    IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink learning contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false, 'error', v_guard -> 'error'
        );
    END IF;
    v_guard := aap_labeling_v1.guard_dataset_unlink(p_dataset_id, p_project_id);
    IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink labeling guard contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', v_schema, 'ok', false, 'error', v_guard -> 'error'
        );
    END IF;
    PERFORM aap_diagnostics_v1.detach_dataset_task_references(p_dataset_id, p_project_id);
    PERFORM aap_assets_v1.prepare_dataset_unlink_cleanup(
        p_operation_id, p_dataset_id, p_project_id, p_cleanup_bucket
    );
    PERFORM aap_jobs_v1.cancel_dataset_task_creation(
        p_dataset_id, p_project_id, v_association_id
    );
    v_deleted := aap_labeling_v1.delete_dataset_tasks(p_dataset_id, p_project_id);
    v_data := v_deleted -> 'data';
    IF v_deleted ->> 'schema' IS DISTINCT FROM 'aap-dataset-task-unlink/v1'
       OR v_deleted -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_data) IS DISTINCT FROM 'object'
       OR (v_data - ARRAY['deleted_tasks', 'deleted_annotations', 'deleted_batches', 'deleted_batch_ids', 'soft']) <> '{}'::jsonb
       OR pg_catalog.jsonb_typeof(v_data -> 'deleted_tasks') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_data -> 'deleted_annotations') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_data -> 'deleted_batches') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_data -> 'deleted_batch_ids') IS DISTINCT FROM 'array'
       OR v_data -> 'soft' IS DISTINCT FROM 'false'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'unlink deletion contract mismatch';
    END IF;
    v_tasks := (v_data ->> 'deleted_tasks')::bigint;
    v_annotations := (v_data ->> 'deleted_annotations')::bigint;
    v_batches := (v_data ->> 'deleted_batches')::bigint;
    v_batch_ids := ARRAY(
        SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(v_data -> 'deleted_batch_ids') AS ids(value)
    );
    IF v_tasks < 0 OR v_annotations < 0 OR v_batches < 0
       OR v_batches <> pg_catalog.cardinality(v_batch_ids)
       OR v_batches <> (SELECT pg_catalog.count(DISTINCT value) FROM pg_catalog.unnest(v_batch_ids) AS ids(value)) THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid unlink deletion counts';
    END IF;
    IF aap_assets_v1.remove_dataset_association(
        p_dataset_id, p_project_id, v_association_id
    ) IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'unlink association changed';
    END IF;
    v_audit_id := aap_audit_v1.record_dataset_unlink(
        p_operation_id, p_actor_id, p_dataset_id, p_project_id,
        v_tasks, v_annotations, v_batch_ids, p_audit_context
    );
    IF v_audit_id IS NULL OR v_audit_id <= 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid unlink audit receipt';
    END IF;
    v_result := pg_catalog.jsonb_build_object(
        'schema', v_schema, 'ok', true,
        'data', v_data || pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id, 'actor_id', p_actor_id,
            'dataset_id', p_dataset_id, 'project_id', p_project_id,
            'association_id', v_association_id, 'cleanup_bucket', p_cleanup_bucket,
            'audit_id', v_audit_id
        )
    );
    INSERT INTO public.dataset_unlink_operations(operation_id, actor_id, dataset_id, project_id, request, result)
    VALUES (p_operation_id, p_actor_id, p_dataset_id, p_project_id, v_request, v_result);
    RETURN v_result;
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'project_busy');
    WHEN foreign_key_violation THEN
        RETURN aap_workspace_v1._dataset_unlink_failure(v_schema, 'source_in_use');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.unlink_dataset(uuid, uuid, uuid, uuid, text, jsonb) FROM PUBLIC;
