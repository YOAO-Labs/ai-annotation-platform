-- Forward owner integration: project deletion retires managed uploads before
-- the Labeling coordinator removes their comments. Frozen 0199 bytes stay intact.
CREATE FUNCTION aap_labeling_v1.retire_project_comment_uploads(p_project_id uuid)
RETURNS integer
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_retired integer;
    -- Retirement records the actual wall-clock instant of the mutation, not
    -- the transaction start that precedes the upload's own created_at.
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project id is required';
    END IF;
    -- The owner deletes exactly the comments matched by
    -- aap_labeling_v1.delete_project_owned_records: same Project, or an
    -- annotation/task of that Project. Retire that exact owner set.
    PERFORM upload.id
    FROM public.comment_attachment_uploads AS upload
    JOIN public.annotation_comments AS comment ON comment.id = upload.bound_comment_id
    WHERE upload.state = 'ready'
      AND (
          comment.project_id = p_project_id
          OR comment.annotation_id IN (
              SELECT annotation.id
              FROM public.annotations AS annotation
              WHERE annotation.project_id = p_project_id
                 OR annotation.task_id IN (
                     SELECT task.id
                     FROM public.tasks AS task
                     WHERE task.project_id = p_project_id
                 )
          )
      )
    ORDER BY upload.id
    FOR UPDATE OF upload NOWAIT;

    UPDATE public.comment_attachment_uploads AS upload
    SET state = 'retired',
        retired_at = GREATEST(v_now, upload.created_at)
    FROM public.annotation_comments AS comment
    WHERE comment.id = upload.bound_comment_id
      AND upload.state = 'ready'
      AND (
          comment.project_id = p_project_id
          OR comment.annotation_id IN (
              SELECT annotation.id
              FROM public.annotations AS annotation
              WHERE annotation.project_id = p_project_id
                 OR annotation.task_id IN (
                     SELECT task.id
                     FROM public.tasks AS task
                     WHERE task.project_id = p_project_id
                 )
          )
      );
    GET DIAGNOSTICS v_retired = ROW_COUNT;
    RETURN v_retired;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.retire_project_comment_uploads(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_workspace_v1.retire_project_uploads(p_project_id uuid)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_guide_ids uuid[];
    v_retired_guides integer := 0;
    v_retired_comments integer := 0;
    v_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    IF p_project_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project id is required';
    END IF;

    -- Guide owners lock by UUID before their reference rows, matching services.
    SELECT COALESCE(pg_catalog.array_agg(reference.upload_id), ARRAY[]::uuid[])
    INTO v_guide_ids
    FROM public.project_guide_assets AS reference
    WHERE reference.project_id = p_project_id;

    IF pg_catalog.array_length(v_guide_ids, 1) IS NOT NULL THEN
        PERFORM owner.id
        FROM public.guide_asset_uploads AS owner
        WHERE owner.id = ANY(v_guide_ids)
        ORDER BY owner.id
        FOR UPDATE NOWAIT;

        DELETE FROM public.project_guide_assets AS reference
        WHERE reference.project_id = p_project_id;

        UPDATE public.guide_asset_uploads AS owner
        SET state = 'retired',
            retired_at = GREATEST(v_now, owner.created_at)
        WHERE owner.id = ANY(v_guide_ids)
          AND owner.state = 'ready'
          AND NOT EXISTS (
              SELECT 1
              FROM public.project_guide_assets AS remaining
              WHERE remaining.upload_id = owner.id
          );
        GET DIAGNOSTICS v_retired_guides = ROW_COUNT;
    END IF;

    v_retired_comments := aap_labeling_v1.retire_project_comment_uploads(p_project_id);

    RETURN pg_catalog.jsonb_build_object(
        'guide_uploads', v_retired_guides,
        'comment_uploads', v_retired_comments
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.retire_project_uploads(uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_workspace_v1.delete_project(
    p_operation_id uuid, p_actor_id uuid, p_project_id uuid, p_audit_context jsonb
)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor jsonb;
    v_manager jsonb;
    v_guard jsonb;
    v_labeling jsonb;
    v_request jsonb;
    v_saved public.project_deletion_operations%ROWTYPE;
    v_result jsonb;
    v_project_id uuid;
    v_display_id text;
    v_name text;
    v_tasks bigint;
    v_annotations bigint;
    v_audit_id bigint;
    v_ip pg_catalog.inet;
BEGIN
    IF p_operation_id IS NULL OR p_actor_id IS NULL OR p_project_id IS NULL THEN
        RETURN aap_workspace_v1._project_delete_failure('invalid_request');
    END IF;
    IF p_audit_context IS NOT NULL THEN
        IF pg_catalog.jsonb_typeof(p_audit_context) IS DISTINCT FROM 'object' THEN
            RETURN aap_workspace_v1._project_delete_failure('invalid_request');
        END IF;
        IF (p_audit_context - ARRAY['method', 'path', 'ip', 'request_id']) <> '{}'::jsonb
           OR EXISTS (
               SELECT 1 FROM pg_catalog.jsonb_each(p_audit_context) AS field(key, value)
               WHERE pg_catalog.jsonb_typeof(field.value) NOT IN ('string', 'null')
           )
           OR pg_catalog.char_length(p_audit_context ->> 'method') > 8
           OR pg_catalog.char_length(p_audit_context ->> 'path') > 256
           OR pg_catalog.char_length(p_audit_context ->> 'request_id') > 36
           OR pg_catalog.char_length(p_audit_context ->> 'ip') > 64 THEN
            RETURN aap_workspace_v1._project_delete_failure('invalid_request');
        END IF;
        BEGIN
            v_ip := NULLIF(p_audit_context ->> 'ip', '')::pg_catalog.inet;
        EXCEPTION WHEN invalid_text_representation THEN
            RETURN aap_workspace_v1._project_delete_failure('invalid_request');
        END;
    END IF;

    v_actor := aap_identity_v1.lock_active_actor(p_actor_id);
    IF v_actor ->> 'schema' IS DISTINCT FROM 'aap-active-actor/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project deletion identity contract mismatch';
    END IF;
    IF v_actor -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete/v1', 'ok', false, 'error', v_actor -> 'error'
        );
    END IF;
    -- Serialize the idempotency key even when a conflicting request names another project.
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('aap-project-delete:' || p_operation_id::text, 0)
    );
    PERFORM aap_learning_v1.lock_project_deletion(p_project_id);
    v_request := pg_catalog.jsonb_build_object(
        'operation_id', p_operation_id, 'actor_id', p_actor_id, 'project_id', p_project_id
    );
    SELECT receipt.* INTO v_saved
    FROM public.project_deletion_operations AS receipt
    WHERE receipt.operation_id = p_operation_id FOR UPDATE;
    IF FOUND THEN
        IF v_saved.request IS DISTINCT FROM v_request THEN
            RETURN aap_workspace_v1._project_delete_failure('operation_conflict');
        END IF;
        RETURN v_saved.result;
    END IF;

    v_manager := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_manager ->> 'schema' IS DISTINCT FROM 'aap-project-manager/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project deletion authority contract mismatch';
    END IF;
    IF v_manager -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete/v1', 'ok', false, 'error', v_manager -> 'error'
        );
    END IF;
    SELECT project.id, project.display_id, project.name
    INTO v_project_id, v_display_id, v_name FROM public.projects AS project
    WHERE project.id = p_project_id FOR UPDATE NOWAIT;
    IF NOT FOUND THEN
        RETURN aap_workspace_v1._project_delete_failure('project_missing');
    END IF;
    v_guard := aap_learning_v1.guard_project_deletion(p_project_id);
    IF v_guard ->> 'schema' IS DISTINCT FROM 'aap-project-delete-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project deletion retention contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-project-delete/v1', 'ok', false, 'error', v_guard -> 'error'
        );
    END IF;

    -- Owner helpers do not acquire Dataset parents after this Project lock.
    PERFORM aap_diagnostics_v1.detach_project_references(p_project_id);
    PERFORM aap_assets_v1.delete_project_owned_records(p_project_id);
    PERFORM aap_inference_v1.clear_project_ml_backend_pool(p_project_id);
    PERFORM aap_inference_v1.clear_project_pipelines(p_project_id);
    PERFORM aap_insights_v1.clear_project_task_views(p_project_id);
    PERFORM aap_export_v1.clear_project_export_artifacts(p_project_id);
    PERFORM aap_jobs_v1.detach_project_references(p_project_id);
    PERFORM aap_learning_v1.delete_project_owned_records(p_project_id);
    PERFORM aap_workspace_v1.retire_project_uploads(p_project_id);
    v_labeling := aap_labeling_v1.delete_project_owned_records(p_project_id);
    IF v_labeling ->> 'schema' IS DISTINCT FROM 'aap-project-labeling-delete/v1'
       OR v_labeling -> 'ok' IS DISTINCT FROM 'true'::jsonb
       OR pg_catalog.jsonb_typeof(v_labeling #> '{data,deleted_tasks}') IS DISTINCT FROM 'number'
       OR pg_catalog.jsonb_typeof(v_labeling #> '{data,deleted_annotations}') IS DISTINCT FROM 'number' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'project deletion labeling contract mismatch';
    END IF;
    v_tasks := (v_labeling #>> '{data,deleted_tasks}')::bigint;
    v_annotations := (v_labeling #>> '{data,deleted_annotations}')::bigint;
    IF v_tasks < 0 OR v_annotations < 0 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid project deletion counts';
    END IF;
    DELETE FROM public.project_members WHERE project_id = p_project_id;
    UPDATE public.project_templates SET source_project_id = NULL WHERE source_project_id = p_project_id;
    DELETE FROM public.projects WHERE id = p_project_id;
    v_audit_id := aap_audit_v1.record_project_deletion(
        p_operation_id, p_actor_id, p_project_id, v_display_id, v_name,
        v_tasks, v_annotations, p_audit_context
    );
    v_result := pg_catalog.jsonb_build_object(
        'schema', 'aap-project-delete/v1', 'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id, 'actor_id', p_actor_id,
            'project_id', p_project_id, 'deleted_tasks', v_tasks,
            'deleted_annotations', v_annotations, 'audit_id', v_audit_id
        )
    );
    INSERT INTO public.project_deletion_operations(operation_id, actor_id, project_id, request, result)
    VALUES (p_operation_id, p_actor_id, p_project_id, v_request, v_result);
    RETURN v_result;
EXCEPTION
    WHEN lock_not_available OR serialization_failure OR deadlock_detected THEN
        -- The exception block rolls back every owner mutation before this reply.
        RETURN aap_workspace_v1._project_delete_failure('project_busy');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.delete_project(uuid, uuid, uuid, jsonb)
    FROM PUBLIC;
