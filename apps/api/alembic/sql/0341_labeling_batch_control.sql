CREATE FUNCTION aap_labeling_v1.batch_control_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_operation text;
    v_keys text[];
    v_expected_keys text[];
    v_batch_id uuid;
    v_project_id uuid;
    v_default_batch_id uuid;
    v_actor_id uuid;
    v_reviewer_id uuid;
    v_target_status text;
    v_expected_status text;
    v_from_status text;
    v_reviewed_at timestamptz;
    v_locked boolean;
    v_protect_task_work boolean;
    v_reason text;
    v_feedback text;
    v_now timestamptz := transaction_timestamp();
    v_task_ids uuid[] := ARRAY[]::uuid[];
    v_tracked_task_ids uuid[] := ARRAY[]::uuid[];
    v_tracked_annotation_ids uuid[] := ARRAY[]::uuid[];
    v_changed_annotation_ids uuid[] := ARRAY[]::uuid[];
    v_batch_ids uuid[] := ARRAY[]::uuid[];
    v_deleted_batch_ids uuid[] := ARRAY[]::uuid[];
    v_cleared_assignee_task_ids uuid[] := ARRAY[]::uuid[];
    v_cleared_reviewer_task_ids uuid[] := ARRAY[]::uuid[];
    v_row record;
    v_item jsonb;
    v_batch public.task_batches%ROWTYPE;
    v_result jsonb := '{}'::jsonb;
    v_tasks jsonb := '[]'::jsonb;
    v_annotations jsonb := '[]'::jsonb;
    v_batch_snapshots jsonb := '[]'::jsonb;
    v_count integer := 0;
    v_total integer := 0;
    v_completed integer := 0;
    v_review integer := 0;
    v_status_changed boolean := false;
    v_operation_changed boolean := false;
    v_error_code text;
    v_error_details jsonb;
    v_annotator_set boolean;
    v_reviewer_set boolean;
    v_annotator_id uuid;
    v_reviewer_after_id uuid;
    v_name_set boolean;
    v_description_set boolean;
    v_priority_set boolean;
    v_deadline_set boolean;
    v_name text;
    v_description text;
    v_priority integer;
    v_deadline date;
    v_task_json jsonb;
    v_pred_count integer := 0;
    v_failed_count integer := 0;
    v_ai_annotation_count integer := 0;
    v_tasks_reset integer := 0;
    v_expected_non_pending integer;
    v_expected_predicted integer;
    v_non_pending integer;
    v_predicted integer;
    v_affected_tasks integer;
BEGIN
    IF p_request IS NULL OR jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR p_request ->> 'schema' IS DISTINCT FROM 'aap-labeling-batch-control/v1'
       OR jsonb_typeof(p_request -> 'operation') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Labeling batch-control request' USING ERRCODE = '22023';
    END IF;
    v_operation := p_request ->> 'operation';
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS request_keys(key);

    IF v_operation = 'create_batch' THEN
        v_expected_keys := ARRAY[
            'annotator_id','batch_id','created_by','dataset_id','deadline','description','display_id',
            'name','operation','priority','project_id','reviewer_id','schema'
        ]::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'display_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'name') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'description') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'dataset_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'priority') IS DISTINCT FROM 'number'
           OR jsonb_typeof(p_request -> 'deadline') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'annotator_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'reviewer_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'created_by') NOT IN ('null','string') THEN
            RAISE EXCEPTION 'Invalid Labeling create-batch request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        INSERT INTO public.task_batches (
            id, project_id, dataset_id, display_id, name, description, status,
            priority, deadline, assigned_user_ids, created_by, annotator_id, reviewer_id
        ) VALUES (
            v_batch_id, v_project_id,
            NULLIF(p_request ->> 'dataset_id','')::uuid,
            p_request ->> 'display_id', p_request ->> 'name',
            p_request ->> 'description', 'draft',
            (p_request ->> 'priority')::integer,
            NULLIF(p_request ->> 'deadline','')::date,
            COALESCE(
                (SELECT jsonb_agg(value ORDER BY ordinal)
                 FROM (VALUES
                     (1, p_request ->> 'annotator_id'),
                     (2, p_request ->> 'reviewer_id')
                 ) AS assigned(ordinal,value)
                 WHERE value IS NOT NULL),
                '[]'::jsonb
            ),
            NULLIF(p_request ->> 'created_by','')::uuid,
            NULLIF(p_request ->> 'annotator_id','')::uuid,
            NULLIF(p_request ->> 'reviewer_id','')::uuid
        );
        v_result := jsonb_build_object(
            'batch_id',v_batch_id,'project_id',v_project_id,
            'batch_ids',jsonb_build_array(v_batch_id),
            'changed',true
        );

    ELSIF v_operation = 'update_batch' THEN
        v_expected_keys := ARRAY[
            'annotator_id','annotator_set','batch_id','deadline','deadline_set',
            'description','description_set','name','name_set','operation','priority',
            'priority_set','project_id','reviewer_id','reviewer_set','schema',
            'tracked_task_ids'
        ]::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'project_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'name_set') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'description_set') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'priority_set') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'deadline_set') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'annotator_set') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'reviewer_set') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'tracked_task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling update-batch request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        v_project_id := NULLIF(p_request ->> 'project_id','')::uuid;
        v_name_set := (p_request ->> 'name_set')::boolean;
        v_description_set := (p_request ->> 'description_set')::boolean;
        v_priority_set := (p_request ->> 'priority_set')::boolean;
        v_deadline_set := (p_request ->> 'deadline_set')::boolean;
        v_annotator_set := (p_request ->> 'annotator_set')::boolean;
        v_reviewer_set := (p_request ->> 'reviewer_set')::boolean;
        v_name := p_request ->> 'name';
        v_description := p_request ->> 'description';
        v_priority := NULLIF(p_request ->> 'priority','')::integer;
        v_deadline := NULLIF(p_request ->> 'deadline','')::date;
        v_annotator_id := NULLIF(p_request ->> 'annotator_id','')::uuid;
        v_reviewer_after_id := NULLIF(p_request ->> 'reviewer_id','')::uuid;
        SELECT * INTO v_batch FROM public.task_batches AS batch
        WHERE batch.id=v_batch_id AND (v_project_id IS NULL OR batch.project_id=v_project_id)
        FOR UPDATE;
        IF NOT FOUND THEN
            v_error_code := 'batch_not_found';
        ELSIF NOT (v_name_set OR v_description_set OR v_priority_set OR v_deadline_set
                   OR v_annotator_set OR v_reviewer_set) THEN
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_batch.project_id,
                'batch_ids',jsonb_build_array(v_batch_id),'changed',false);
        ELSE
            v_operation_changed := false;
            UPDATE public.task_batches AS batch
            SET name = CASE WHEN v_name_set THEN v_name ELSE batch.name END,
                description = CASE WHEN v_description_set THEN v_description ELSE batch.description END,
                priority = CASE WHEN v_priority_set THEN v_priority ELSE batch.priority END,
                deadline = CASE WHEN v_deadline_set THEN v_deadline ELSE batch.deadline END,
                annotator_id = CASE WHEN v_annotator_set THEN v_annotator_id ELSE batch.annotator_id END,
                reviewer_id = CASE WHEN v_reviewer_set THEN v_reviewer_after_id ELSE batch.reviewer_id END,
                assigned_user_ids = CASE WHEN v_annotator_set OR v_reviewer_set THEN
                    COALESCE((SELECT jsonb_agg(value ORDER BY ordinal)
                        FROM (VALUES
                            (1, CASE WHEN v_annotator_set THEN v_annotator_id::text ELSE batch.annotator_id::text END),
                            (2, CASE WHEN v_reviewer_set THEN v_reviewer_after_id::text ELSE batch.reviewer_id::text END)
                        ) AS assigned(ordinal,value) WHERE value IS NOT NULL),'[]'::jsonb)
                    ELSE batch.assigned_user_ids END,
                updated_at = v_now
            WHERE batch.id=v_batch_id AND (
                (v_name_set AND batch.name IS DISTINCT FROM v_name)
                OR (v_description_set AND batch.description IS DISTINCT FROM v_description)
                OR (v_priority_set AND batch.priority IS DISTINCT FROM v_priority)
                OR (v_deadline_set AND batch.deadline IS DISTINCT FROM v_deadline)
                OR (v_annotator_set AND batch.annotator_id IS DISTINCT FROM v_annotator_id)
                OR (v_reviewer_set AND batch.reviewer_id IS DISTINCT FROM v_reviewer_after_id)
                OR ((v_annotator_set OR v_reviewer_set) AND batch.assigned_user_ids IS DISTINCT FROM
                    COALESCE((SELECT jsonb_agg(value ORDER BY ordinal)
                        FROM (VALUES
                            (1, CASE WHEN v_annotator_set THEN v_annotator_id::text ELSE batch.annotator_id::text END),
                            (2, CASE WHEN v_reviewer_set THEN v_reviewer_after_id::text ELSE batch.reviewer_id::text END)
                        ) AS assigned(ordinal,value) WHERE value IS NOT NULL),'[]'::jsonb))
            );
            GET DIAGNOSTICS v_count = ROW_COUNT;
            v_operation_changed := v_count>0;
            IF v_annotator_set THEN
                UPDATE public.tasks AS task
                SET assignee_id=v_annotator_id,
                    assignee_is_override=false,
                    assigned_at=CASE WHEN v_annotator_id IS NULL THEN NULL ELSE v_now END,
                    updated_at=v_now
                WHERE task.batch_id=v_batch_id
                  AND (task.assignee_is_override IS FALSE OR task.assignee_id IS NULL);
                GET DIAGNOSTICS v_count = ROW_COUNT;
                v_operation_changed := v_operation_changed OR v_count>0;
            END IF;
            IF v_reviewer_set THEN
                UPDATE public.tasks AS task
                SET reviewer_id=v_reviewer_after_id,
                    reviewer_is_override=false,
                    updated_at=v_now
                WHERE task.batch_id=v_batch_id
                  AND (task.reviewer_is_override IS FALSE OR task.reviewer_id IS NULL);
                GET DIAGNOSTICS v_count = ROW_COUNT;
                v_operation_changed := v_operation_changed OR v_count>0;
            END IF;
            SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
            INTO v_tracked_task_ids
            FROM jsonb_array_elements_text(p_request -> 'tracked_task_ids') AS ids(value);
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_batch.project_id,
                'batch_ids',jsonb_build_array(v_batch_id),'changed',v_operation_changed);
        END IF;

    ELSIF v_operation = 'transition_batch' THEN
        v_expected_keys := ARRAY['actor_id','batch_id','expected_status','operation','schema','target_status']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'expected_status') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'target_status') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'actor_id') NOT IN ('null','string') THEN
            RAISE EXCEPTION 'Invalid Labeling transition request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        v_expected_status := p_request ->> 'expected_status';
        v_target_status := p_request ->> 'target_status';
        SELECT * INTO v_batch FROM public.task_batches AS batch WHERE batch.id=v_batch_id FOR UPDATE;
        IF NOT FOUND THEN
            v_error_code := 'batch_not_found';
        ELSIF v_batch.status IS DISTINCT FROM v_expected_status THEN
            v_error_code := 'batch_state_changed';
            v_error_details := jsonb_build_object('status',v_batch.status);
        ELSIF NOT (
            (v_expected_status='draft' AND v_target_status='active') OR
            (v_expected_status='active' AND v_target_status IN ('annotating','pre_annotated','archived')) OR
            (v_expected_status='pre_annotated' AND v_target_status IN ('annotating','active','archived')) OR
            (v_expected_status='annotating' AND v_target_status IN ('reviewing','archived')) OR
            (v_expected_status='reviewing' AND v_target_status IN ('approved','rejected')) OR
            (v_expected_status='approved' AND v_target_status IN ('archived','reviewing')) OR
            (v_expected_status='rejected' AND v_target_status IN ('active','archived','reviewing')) OR
            (v_expected_status='archived' AND v_target_status='active')
        ) THEN
            v_error_code := 'batch_transition_invalid';
            v_error_details := jsonb_build_object('from_status',v_expected_status,'target_status',v_target_status);
        ELSIF v_expected_status='draft' AND v_target_status='active'
          AND NOT EXISTS (SELECT FROM public.tasks AS task WHERE task.batch_id=v_batch_id) THEN
            v_error_code := 'batch_empty';
        ELSE
            UPDATE public.task_batches AS batch
            SET status=v_target_status,
                review_feedback=CASE WHEN v_expected_status='approved' AND v_target_status='reviewing' THEN NULL ELSE batch.review_feedback END,
                reviewed_at=CASE WHEN v_expected_status='approved' AND v_target_status='reviewing' THEN NULL ELSE batch.reviewed_at END,
                reviewed_by=CASE WHEN v_expected_status='approved' AND v_target_status='reviewing' THEN NULL ELSE batch.reviewed_by END,
                updated_at=v_now
            WHERE batch.id=v_batch_id;
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_batch.project_id,
                'batch_ids',jsonb_build_array(v_batch_id),
                'changed',v_expected_status IS DISTINCT FROM v_target_status,
                'from_status',v_expected_status,'status',v_target_status);
        END IF;

    ELSIF v_operation = 'set_batch_admin_lock' THEN
        v_expected_keys := ARRAY['actor_id','batch_id','locked','locked_at','operation','reason','schema']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'locked') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'locked_at') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'reason') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'actor_id') NOT IN ('null','string') THEN
            RAISE EXCEPTION 'Invalid Labeling admin-lock request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        v_locked := (p_request ->> 'locked')::boolean;
        v_reason := p_request ->> 'reason';
        v_actor_id := NULLIF(p_request ->> 'actor_id','')::uuid;
        SELECT * INTO v_batch FROM public.task_batches AS batch WHERE batch.id=v_batch_id FOR UPDATE;
        IF NOT FOUND THEN
            v_error_code := 'batch_not_found';
        ELSIF v_batch.admin_locked IS TRUE AND v_locked THEN
            v_error_code := 'batch_already_locked';
        ELSIF v_batch.admin_locked IS FALSE AND NOT v_locked THEN
            v_error_code := 'batch_not_locked';
        ELSE
            UPDATE public.task_batches AS batch
            SET admin_locked=v_locked,
                admin_lock_reason=CASE WHEN v_locked THEN v_reason ELSE NULL END,
                admin_locked_at=CASE WHEN v_locked THEN (p_request ->> 'locked_at')::timestamptz ELSE NULL END,
                admin_locked_by=CASE WHEN v_locked THEN v_actor_id ELSE NULL END,
                updated_at=v_now
            WHERE batch.id=v_batch_id;
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_batch.project_id,
                'batch_ids',jsonb_build_array(v_batch_id),
                'changed',true,'status',v_batch.status);
        END IF;

    ELSIF v_operation = 'prepare_batch_cleanup' THEN
        v_expected_keys := ARRAY[
            'batch_id','expected_non_pending','expected_predicted','operation',
            'protect_task_work','schema','tracked_task_ids'
        ]::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'protect_task_work') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'expected_non_pending') NOT IN ('null','number')
           OR jsonb_typeof(p_request -> 'expected_predicted') NOT IN ('null','number')
           OR (jsonb_typeof(p_request -> 'expected_non_pending')='number'
               AND p_request ->> 'expected_non_pending' !~ '^(0|[1-9][0-9]*)$')
           OR (jsonb_typeof(p_request -> 'expected_predicted')='number'
               AND p_request ->> 'expected_predicted' !~ '^(0|[1-9][0-9]*)$')
           OR jsonb_typeof(p_request -> 'tracked_task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling batch-cleanup request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        v_protect_task_work := (p_request ->> 'protect_task_work')::boolean;
        v_expected_non_pending := NULLIF(p_request ->> 'expected_non_pending','')::integer;
        v_expected_predicted := NULLIF(p_request ->> 'expected_predicted','')::integer;
        IF (v_protect_task_work AND (v_expected_non_pending IS NULL OR v_expected_predicted IS NULL))
           OR (NOT v_protect_task_work AND
               (v_expected_non_pending IS NOT NULL OR v_expected_predicted IS NOT NULL))
           OR COALESCE(v_expected_non_pending,-1)<0
           OR COALESCE(v_expected_predicted,-1)<0 THEN
            RAISE EXCEPTION 'Invalid Labeling batch-cleanup protection scope' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
        INTO v_tracked_task_ids
        FROM jsonb_array_elements_text(p_request -> 'tracked_task_ids') AS ids(value);
        SELECT * INTO v_batch FROM public.task_batches AS batch WHERE batch.id=v_batch_id FOR UPDATE;
        IF NOT FOUND THEN
            v_error_code := 'batch_not_found';
        ELSE
            PERFORM task.id FROM public.tasks AS task
            WHERE task.batch_id=v_batch_id ORDER BY task.id FOR UPDATE;
            SELECT count(*) FILTER (WHERE task.status<>'pending')::integer,
                   count(*) FILTER (WHERE task.total_predictions>0)::integer,
                   count(*) FILTER (WHERE task.status<>'pending' OR task.total_predictions>0)::integer
            INTO v_non_pending,v_predicted,v_affected_tasks
            FROM public.tasks AS task WHERE task.batch_id=v_batch_id;
            IF v_protect_task_work AND (
                v_non_pending IS DISTINCT FROM v_expected_non_pending
                OR v_predicted IS DISTINCT FROM v_expected_predicted
            ) THEN
                RETURN jsonb_build_object(
                    'schema','aap-labeling-batch-control-data/v1','ok',false,
                    'request',p_request,
                    'error',jsonb_build_object('code','batch_delete_protection_changed',
                        'details',jsonb_build_object(
                            'non_pending',v_non_pending,'predicted',v_predicted,
                            'affected_tasks',v_affected_tasks
                        ))
                );
            END IF;
            UPDATE public.tasks AS task
            SET status='pending', review_contributor_ids='null'::jsonb,
                review_submitter_id=NULL, updated_at=v_now
            WHERE task.batch_id=v_batch_id AND task.status <> 'pending';
            GET DIAGNOSTICS v_tasks_reset = ROW_COUNT;

            WITH cleared AS (
                UPDATE public.tasks AS task
                SET assignee_id=NULL, updated_at=v_now
                WHERE task.batch_id=v_batch_id AND task.status='pending'
                  AND task.assignee_id IS NOT NULL AND task.assignee_is_override IS FALSE
                  AND NOT EXISTS (
                      SELECT FROM aap_workspace_v1.project_member_facts AS member
                      WHERE member.project_id=task.project_id
                        AND member.user_id=task.assignee_id AND member.role='annotator'
                  )
                RETURNING task.id
            )
            SELECT COALESCE(array_agg(cleared.id ORDER BY cleared.id),ARRAY[]::uuid[])
            INTO v_cleared_assignee_task_ids
            FROM cleared WHERE cleared.id=ANY(v_tracked_task_ids);
            WITH cleared AS (
                UPDATE public.tasks AS task
                SET reviewer_id=NULL, reviewer_claimed_at=NULL, updated_at=v_now
                WHERE task.batch_id=v_batch_id AND task.status='pending'
                  AND task.reviewer_id IS NOT NULL AND task.reviewer_is_override IS FALSE
                  AND NOT EXISTS (
                      SELECT FROM aap_workspace_v1.project_member_facts AS member
                      WHERE member.project_id=task.project_id
                        AND member.user_id=task.reviewer_id AND member.role='reviewer'
                  )
                RETURNING task.id
            )
            SELECT COALESCE(array_agg(cleared.id ORDER BY cleared.id),ARRAY[]::uuid[])
            INTO v_cleared_reviewer_task_ids
            FROM cleared WHERE cleared.id=ANY(v_tracked_task_ids);
            DELETE FROM public.task_locks AS task_lock
            USING public.tasks AS task
            WHERE task_lock.task_id=task.id AND task.batch_id=v_batch_id;
            SELECT COALESCE(array_agg(task.id ORDER BY task.id), ARRAY[]::uuid[])
            INTO v_task_ids FROM public.tasks AS task WHERE task.batch_id=v_batch_id;
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_batch.project_id,
                'batch_ids',jsonb_build_array(v_batch_id),
                'task_ids',to_jsonb(v_task_ids),'tasks_reset',v_tasks_reset,'changed',true,
                'cleared_assignee_task_ids',to_jsonb(v_cleared_assignee_task_ids),
                'cleared_reviewer_task_ids',to_jsonb(v_cleared_reviewer_task_ids));
        END IF;

    ELSIF v_operation = 'reset_batch_to_draft' THEN
        v_expected_keys := ARRAY['batch_id','expected_status','operation','schema']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'expected_status') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Labeling reset-batch request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        v_expected_status := p_request ->> 'expected_status';
        SELECT * INTO v_batch FROM public.task_batches AS batch WHERE batch.id=v_batch_id FOR UPDATE;
        IF NOT FOUND THEN
            v_error_code := 'batch_not_found';
        ELSIF v_batch.status IS DISTINCT FROM v_expected_status THEN
            v_error_code := 'batch_state_changed';
            v_error_details := jsonb_build_object('status',v_batch.status);
        ELSE
            UPDATE public.task_batches AS batch
            SET status='draft', review_feedback=NULL, reviewed_at=NULL,
                reviewed_by=NULL, updated_at=v_now
            WHERE batch.id=v_batch_id
              AND (batch.status IS DISTINCT FROM 'draft'
                   OR batch.review_feedback IS NOT NULL OR batch.reviewed_at IS NOT NULL
                   OR batch.reviewed_by IS NOT NULL);
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_batch.project_id,
                'batch_ids',jsonb_build_array(v_batch_id),
                'changed',v_batch.status IS DISTINCT FROM 'draft'
                    OR v_batch.review_feedback IS NOT NULL OR v_batch.reviewed_at IS NOT NULL
                    OR v_batch.reviewed_by IS NOT NULL,
                'from_status',v_expected_status,'status','draft');
        END IF;

    ELSIF v_operation = 'reassign_batch_tasks' THEN
        v_expected_keys := ARRAY['batch_id','default_batch_id','operation','project_id','schema','tracked_task_ids']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'default_batch_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'tracked_task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling delete-batch request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_default_batch_id := NULLIF(p_request ->> 'default_batch_id','')::uuid;
        SELECT * INTO v_batch FROM public.task_batches AS batch
        WHERE batch.id=v_batch_id AND batch.project_id=v_project_id FOR UPDATE;
        IF NOT FOUND THEN
            v_error_code := 'batch_not_found';
        ELSIF v_batch.display_id='B-DEFAULT' THEN
            v_error_code := 'default_batch_protected';
        ELSIF v_default_batch_id IS NOT NULL AND NOT EXISTS (
            SELECT FROM public.task_batches AS default_batch
            WHERE default_batch.id=v_default_batch_id AND default_batch.project_id=v_project_id
        ) THEN
            v_error_code := 'default_batch_not_found';
        ELSE
            UPDATE public.tasks AS task
            SET batch_id=v_default_batch_id, updated_at=v_now
            WHERE task.batch_id=v_batch_id;
            GET DIAGNOSTICS v_count = ROW_COUNT;
            SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
            INTO v_tracked_task_ids
            FROM jsonb_array_elements_text(p_request -> 'tracked_task_ids') AS ids(value);
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_project_id,
                'affected',v_count,'changed',v_count>0);
        END IF;

    ELSIF v_operation = 'delete_batch' THEN
        v_expected_keys := ARRAY['batch_id','operation','project_id','schema']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Labeling delete-batch request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        v_project_id := (p_request ->> 'project_id')::uuid;
        SELECT * INTO v_batch FROM public.task_batches AS batch
        WHERE batch.id=v_batch_id AND batch.project_id=v_project_id FOR UPDATE;
        IF NOT FOUND THEN
            v_error_code := 'batch_not_found';
        ELSIF v_batch.display_id='B-DEFAULT' THEN
            v_error_code := 'default_batch_protected';
        ELSIF EXISTS (SELECT FROM public.tasks AS task WHERE task.batch_id=v_batch_id) THEN
            v_error_code := 'batch_tasks_not_reassigned';
        ELSE
            DELETE FROM public.task_batches AS batch WHERE batch.id=v_batch_id;
            v_deleted_batch_ids := ARRAY[v_batch_id];
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_project_id,
                'batch_ids',jsonb_build_array(v_batch_id),'changed',true);
        END IF;

    ELSIF v_operation = 'assign_tasks' THEN
        v_expected_keys := ARRAY['batch_id','operation','schema','task_ids','tracked_task_ids']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'tracked_task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling task-assignment request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        SELECT * INTO v_batch FROM public.task_batches AS batch WHERE batch.id=v_batch_id;
        IF NOT FOUND THEN
            v_error_code := 'batch_not_found';
        ELSE
            SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
            INTO v_task_ids FROM jsonb_array_elements_text(p_request -> 'task_ids') AS ids(value);
            UPDATE public.tasks AS task SET batch_id=v_batch_id, updated_at=v_now
            WHERE task.id=ANY(v_task_ids) AND task.project_id=v_batch.project_id;
            GET DIAGNOSTICS v_count = ROW_COUNT;
            SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
            INTO v_tracked_task_ids FROM jsonb_array_elements_text(p_request -> 'tracked_task_ids') AS ids(value);
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_batch.project_id,
                'task_ids',to_jsonb(v_task_ids),'affected',v_count,'changed',v_count>0);
        END IF;

    ELSIF v_operation = 'set_sequence_orders' THEN
        v_expected_keys := ARRAY['operation','schema','sequence_orders','tracked_task_ids']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'sequence_orders') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'tracked_task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling task-sequence request' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
        INTO v_tracked_task_ids FROM jsonb_array_elements_text(p_request -> 'tracked_task_ids') AS ids(value);
        IF EXISTS (
            SELECT FROM jsonb_array_elements(p_request -> 'sequence_orders') AS sequence(value)
            WHERE jsonb_typeof(value) IS DISTINCT FROM 'object'
               OR (SELECT array_agg(key ORDER BY key)
                   FROM jsonb_object_keys(value) AS keys(key))
                   IS DISTINCT FROM ARRAY['sequence_order','task_id']::text[]
        ) THEN
            RAISE EXCEPTION 'Invalid Labeling task-sequence item' USING ERRCODE = '22023';
        END IF;
        WITH orders AS (
            SELECT (value ->> 'task_id')::uuid AS task_id,
                   (value ->> 'sequence_order')::integer AS sequence_order
            FROM jsonb_array_elements(p_request -> 'sequence_orders') AS sequence(value)
        ), updated AS (
            UPDATE public.tasks AS task
            SET sequence_order=orders.sequence_order,updated_at=v_now
            FROM orders WHERE task.id=orders.task_id
            RETURNING task.id
        )
        SELECT count(*)::integer,
            COALESCE(array_agg(updated.id ORDER BY updated.id)
                FILTER (WHERE updated.id=ANY(v_tracked_task_ids)),ARRAY[]::uuid[])
        INTO v_total,v_task_ids FROM updated;
        v_result := jsonb_build_object('affected',v_total,'changed',v_total>0);

    ELSIF v_operation = 'apply_batch_assignments' THEN
        v_expected_keys := ARRAY['assignments','operation','project_id','schema','tracked_task_ids']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'assignments') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'tracked_task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling batch-reassignment request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'assignments') AS items(value)
        LOOP
            IF jsonb_typeof(v_item) IS DISTINCT FROM 'object'
               OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(v_item) AS keys(key))
                  IS DISTINCT FROM ARRAY[
                    'annotator_id','annotator_set','batch_id','expected_annotator_id',
                    'expected_reviewer_id','reviewer_id','reviewer_set'
                  ]::text[]
               OR jsonb_typeof(v_item -> 'batch_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_item -> 'expected_annotator_id') NOT IN ('null','string')
               OR jsonb_typeof(v_item -> 'expected_reviewer_id') NOT IN ('null','string')
               OR jsonb_typeof(v_item -> 'annotator_set') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(v_item -> 'reviewer_set') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(v_item -> 'annotator_id') NOT IN ('null','string')
               OR jsonb_typeof(v_item -> 'reviewer_id') NOT IN ('null','string') THEN
                RAISE EXCEPTION 'Invalid Labeling batch-reassignment item' USING ERRCODE = '22023';
            END IF;
            v_batch_id := (v_item ->> 'batch_id')::uuid;
            IF NOT EXISTS (
                SELECT FROM public.task_batches AS batch
                WHERE batch.id=v_batch_id AND batch.project_id=v_project_id
                  AND batch.annotator_id IS NOT DISTINCT FROM NULLIF(v_item ->> 'expected_annotator_id','')::uuid
                  AND batch.reviewer_id IS NOT DISTINCT FROM NULLIF(v_item ->> 'expected_reviewer_id','')::uuid
                FOR UPDATE
            ) THEN
                v_error_code := 'batch_assignment_state_changed';
                v_error_details := jsonb_build_object('batch_id',v_batch_id);
                EXIT;
            END IF;
        END LOOP;
        IF v_error_code IS NULL THEN
            v_operation_changed := false;
            FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'assignments') AS items(value)
            LOOP
                v_batch_id := (v_item ->> 'batch_id')::uuid;
                v_annotator_set := (v_item ->> 'annotator_set')::boolean;
                v_reviewer_set := (v_item ->> 'reviewer_set')::boolean;
                v_annotator_id := NULLIF(v_item ->> 'annotator_id','')::uuid;
                v_reviewer_after_id := NULLIF(v_item ->> 'reviewer_id','')::uuid;
                UPDATE public.task_batches AS batch
                SET annotator_id=CASE WHEN v_annotator_set THEN v_annotator_id ELSE batch.annotator_id END,
                    reviewer_id=CASE WHEN v_reviewer_set THEN v_reviewer_after_id ELSE batch.reviewer_id END,
                    assigned_user_ids=COALESCE((SELECT jsonb_agg(value ORDER BY ordinal)
                        FROM (VALUES
                            (1, CASE WHEN v_annotator_set THEN v_annotator_id::text ELSE batch.annotator_id::text END),
                            (2, CASE WHEN v_reviewer_set THEN v_reviewer_after_id::text ELSE batch.reviewer_id::text END)
                        ) AS assigned(ordinal,value) WHERE value IS NOT NULL),'[]'::jsonb),
                    updated_at=v_now
                WHERE batch.id=v_batch_id AND batch.project_id=v_project_id;
                GET DIAGNOSTICS v_count = ROW_COUNT;
                v_operation_changed := v_operation_changed OR v_count>0;
                IF v_annotator_set THEN
                    UPDATE public.tasks AS task
                    SET assignee_id=v_annotator_id, assignee_is_override=false,
                        assigned_at=CASE WHEN v_annotator_id IS NULL THEN NULL ELSE v_now END,
                        updated_at=v_now
                    WHERE task.batch_id=v_batch_id
                      AND (task.assignee_is_override IS FALSE OR task.assignee_id IS NULL);
                    GET DIAGNOSTICS v_count = ROW_COUNT;
                    v_operation_changed := v_operation_changed OR v_count>0;
                END IF;
                IF v_reviewer_set THEN
                    UPDATE public.tasks AS task
                    SET reviewer_id=v_reviewer_after_id, reviewer_is_override=false,
                        updated_at=v_now
                    WHERE task.batch_id=v_batch_id
                      AND (task.reviewer_is_override IS FALSE OR task.reviewer_id IS NULL);
                    GET DIAGNOSTICS v_count = ROW_COUNT;
                    v_operation_changed := v_operation_changed OR v_count>0;
                END IF;
                v_batch_ids := array_append(v_batch_ids,v_batch_id);
            END LOOP;
        END IF;
        SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
        INTO v_tracked_task_ids FROM jsonb_array_elements_text(p_request -> 'tracked_task_ids') AS ids(value);
        v_result := jsonb_build_object('project_id',v_project_id,'batch_ids',to_jsonb(v_batch_ids),
            'changed',v_operation_changed);

    ELSIF v_operation = 'apply_task_assignments' THEN
        v_expected_keys := ARRAY['assignments','operation','schema']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'assignments') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling task-reassignment request' USING ERRCODE = '22023';
        END IF;
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'assignments') AS items(value)
        LOOP
            IF jsonb_typeof(v_item) IS DISTINCT FROM 'object'
               OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(v_item) AS keys(key))
                  IS DISTINCT FROM ARRAY[
                    'annotator_id','annotator_set','assigned_at','batch_id','expected_assignee_id',
                    'expected_assignee_is_override','expected_reviewer_claimed_at',
                    'expected_reviewer_id','expected_reviewer_is_override','expected_status',
                    'project_id','reviewer_id','reviewer_set','task_id'
                  ]::text[]
               OR jsonb_typeof(v_item -> 'task_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_item -> 'project_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_item -> 'batch_id') NOT IN ('null','string')
               OR jsonb_typeof(v_item -> 'expected_status') IS DISTINCT FROM 'string'
               OR jsonb_typeof(v_item -> 'expected_assignee_id') NOT IN ('null','string')
               OR jsonb_typeof(v_item -> 'expected_assignee_is_override') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(v_item -> 'expected_reviewer_id') NOT IN ('null','string')
               OR jsonb_typeof(v_item -> 'expected_reviewer_is_override') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(v_item -> 'expected_reviewer_claimed_at') NOT IN ('null','string')
               OR jsonb_typeof(v_item -> 'annotator_set') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(v_item -> 'annotator_id') NOT IN ('null','string')
               OR jsonb_typeof(v_item -> 'assigned_at') NOT IN ('null','string')
               OR jsonb_typeof(v_item -> 'reviewer_set') IS DISTINCT FROM 'boolean'
               OR jsonb_typeof(v_item -> 'reviewer_id') NOT IN ('null','string') THEN
                RAISE EXCEPTION 'Invalid Labeling task-reassignment item' USING ERRCODE = '22023';
            END IF;
            v_batch_id := NULLIF(v_item ->> 'batch_id','')::uuid;
            IF NOT EXISTS (
                SELECT FROM public.tasks AS task
                WHERE task.id=(v_item ->> 'task_id')::uuid
                  AND task.project_id=(v_item ->> 'project_id')::uuid
                  AND task.batch_id IS NOT DISTINCT FROM v_batch_id
                  AND task.status=v_item ->> 'expected_status'
                  AND task.assignee_id IS NOT DISTINCT FROM NULLIF(v_item ->> 'expected_assignee_id','')::uuid
                  AND task.assignee_is_override=(v_item ->> 'expected_assignee_is_override')::boolean
                  AND task.reviewer_id IS NOT DISTINCT FROM NULLIF(v_item ->> 'expected_reviewer_id','')::uuid
                  AND task.reviewer_is_override=(v_item ->> 'expected_reviewer_is_override')::boolean
                  AND task.reviewer_claimed_at IS NOT DISTINCT FROM NULLIF(v_item ->> 'expected_reviewer_claimed_at','')::timestamptz
                FOR UPDATE
            ) THEN
                v_error_code := 'task_assignment_state_changed';
                v_error_details := jsonb_build_object('task_id',v_item ->> 'task_id');
                EXIT;
            END IF;
        END LOOP;
        IF v_error_code IS NULL THEN
            FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'assignments') AS items(value)
            LOOP
                UPDATE public.tasks AS task
                SET assignee_id=CASE WHEN (v_item ->> 'annotator_set')::boolean THEN NULLIF(v_item ->> 'annotator_id','')::uuid ELSE task.assignee_id END,
                    assignee_is_override=CASE WHEN (v_item ->> 'annotator_set')::boolean THEN NULLIF(v_item ->> 'annotator_id','') IS NOT NULL ELSE task.assignee_is_override END,
                    assigned_at=CASE WHEN (v_item ->> 'annotator_set')::boolean THEN NULLIF(v_item ->> 'assigned_at','')::timestamptz ELSE task.assigned_at END,
                    reviewer_id=CASE WHEN (v_item ->> 'reviewer_set')::boolean THEN NULLIF(v_item ->> 'reviewer_id','')::uuid ELSE task.reviewer_id END,
                    reviewer_is_override=CASE WHEN (v_item ->> 'reviewer_set')::boolean THEN NULLIF(v_item ->> 'reviewer_id','') IS NOT NULL ELSE task.reviewer_is_override END,
                    reviewer_claimed_at=CASE
                        WHEN (v_item ->> 'reviewer_set')::boolean
                         AND task.reviewer_id IS DISTINCT FROM NULLIF(v_item ->> 'reviewer_id','')::uuid THEN NULL
                        ELSE task.reviewer_claimed_at END,
                    updated_at=v_now
                WHERE task.id=(v_item ->> 'task_id')::uuid;
                v_task_ids := array_append(v_task_ids,(v_item ->> 'task_id')::uuid);
            END LOOP;
            v_tracked_task_ids := v_task_ids;
        END IF;
        v_result := jsonb_build_object('task_ids',to_jsonb(v_task_ids),
            'affected',cardinality(v_task_ids),'changed',cardinality(v_task_ids)>0);

    ELSIF v_operation IN ('bulk_archive','bulk_activate') THEN
        v_expected_keys := ARRAY['batches','operation','project_id','schema']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'batches') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling bulk-status request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'batches') AS items(value)
        LOOP
            IF jsonb_typeof(v_item) IS DISTINCT FROM 'object'
               OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(v_item) AS keys(key))
                  IS DISTINCT FROM ARRAY['batch_id','expected_status']::text[] THEN
                RAISE EXCEPTION 'Invalid Labeling bulk-status item' USING ERRCODE = '22023';
            END IF;
            v_batch_id := (v_item ->> 'batch_id')::uuid;
            IF NOT EXISTS (
                SELECT FROM public.task_batches AS batch
                WHERE batch.id=v_batch_id AND batch.project_id=v_project_id
                  AND batch.status=v_item ->> 'expected_status'
                FOR UPDATE
            ) THEN
                v_error_code := 'batch_state_changed';
                v_error_details := jsonb_build_object('batch_id',v_batch_id);
                EXIT;
            END IF;
            IF v_operation='bulk_activate' AND (
                v_item ->> 'expected_status' <> 'draft'
                OR NOT EXISTS (SELECT FROM public.task_batches AS batch
                    WHERE batch.id=v_batch_id AND batch.annotator_id IS NOT NULL)
                OR NOT EXISTS (SELECT FROM public.tasks AS task WHERE task.batch_id=v_batch_id)
            ) THEN
                v_error_code := 'batch_activation_state_changed';
                v_error_details := jsonb_build_object('batch_id',v_batch_id);
                EXIT;
            END IF;
        END LOOP;
        IF v_error_code IS NULL THEN
            FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'batches') AS items(value)
            LOOP
                v_batch_id := (v_item ->> 'batch_id')::uuid;
                v_target_status := CASE WHEN v_operation='bulk_archive' THEN 'archived' ELSE 'active' END;
                UPDATE public.task_batches AS batch SET status=v_target_status, updated_at=v_now
                WHERE batch.id=v_batch_id AND batch.project_id=v_project_id;
                v_batch_ids := array_append(v_batch_ids,v_batch_id);
            END LOOP;
        END IF;
        v_result := jsonb_build_object('project_id',v_project_id,'batch_ids',to_jsonb(v_batch_ids),
            'changed',cardinality(v_batch_ids)>0);

    ELSIF v_operation = 'bulk_delete' THEN
        v_expected_keys := ARRAY['batches','default_batch_id','operation','project_id','schema','tracked_task_ids']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'default_batch_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'batches') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'tracked_task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling bulk-delete request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_default_batch_id := NULLIF(p_request ->> 'default_batch_id','')::uuid;
        IF v_default_batch_id IS NOT NULL AND NOT EXISTS (
            SELECT FROM public.task_batches AS batch
            WHERE batch.id=v_default_batch_id AND batch.project_id=v_project_id
        ) THEN
            v_error_code := 'default_batch_not_found';
        END IF;
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'batches') AS items(value)
        LOOP
            IF jsonb_typeof(v_item) IS DISTINCT FROM 'object'
               OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(v_item) AS keys(key))
                  IS DISTINCT FROM ARRAY['batch_id']::text[] THEN
                RAISE EXCEPTION 'Invalid Labeling bulk-delete item' USING ERRCODE = '22023';
            END IF;
            v_batch_id := (v_item ->> 'batch_id')::uuid;
            IF NOT EXISTS (SELECT FROM public.task_batches AS batch
                WHERE batch.id=v_batch_id AND batch.project_id=v_project_id
                  AND batch.display_id<>'B-DEFAULT' FOR UPDATE) THEN
                v_error_code := 'batch_state_changed';
                v_error_details := jsonb_build_object('batch_id',v_batch_id);
                EXIT;
            END IF;
        END LOOP;
        IF v_error_code IS NULL THEN
            FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'batches') AS items(value)
            LOOP
                v_batch_id := (v_item ->> 'batch_id')::uuid;
                UPDATE public.tasks AS task SET batch_id=v_default_batch_id, updated_at=v_now
                WHERE task.batch_id=v_batch_id;
                DELETE FROM public.task_batches AS batch WHERE batch.id=v_batch_id;
                v_deleted_batch_ids := array_append(v_deleted_batch_ids,v_batch_id);
                v_batch_ids := array_append(v_batch_ids,v_batch_id);
            END LOOP;
            SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
            INTO v_tracked_task_ids FROM jsonb_array_elements_text(p_request -> 'tracked_task_ids') AS ids(value);
        END IF;
        v_result := jsonb_build_object('project_id',v_project_id,'batch_ids',to_jsonb(v_batch_ids),
            'changed',cardinality(v_batch_ids)>0);

    ELSIF v_operation IN ('bulk_approve','bulk_reject') THEN
        IF v_operation='bulk_approve' THEN
            v_expected_keys := ARRAY['actor_id','batches','operation','project_id','schema']::text[];
        ELSE
            v_expected_keys := ARRAY['actor_id','batches','feedback','operation','project_id','reviewed_at','reviewer_id','schema','tracked_task_ids']::text[];
        END IF;
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'batches') IS DISTINCT FROM 'array'
           OR (v_operation='bulk_reject' AND (
               jsonb_typeof(p_request -> 'feedback') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'reviewer_id') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'reviewed_at') IS DISTINCT FROM 'string'
               OR jsonb_typeof(p_request -> 'tracked_task_ids') IS DISTINCT FROM 'array')) THEN
            RAISE EXCEPTION 'Invalid Labeling bulk-review request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_reviewer_id := NULLIF(p_request ->> 'reviewer_id','')::uuid;
        v_feedback := p_request ->> 'feedback';
        v_reviewed_at := COALESCE(NULLIF(p_request ->> 'reviewed_at','')::timestamptz,v_now);
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'batches') AS items(value)
        LOOP
            IF jsonb_typeof(v_item) IS DISTINCT FROM 'object'
               OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(v_item) AS keys(key))
                  IS DISTINCT FROM ARRAY['batch_id','expected_status']::text[] THEN
                RAISE EXCEPTION 'Invalid Labeling bulk-review item' USING ERRCODE = '22023';
            END IF;
            v_batch_id := (v_item ->> 'batch_id')::uuid;
            v_expected_status := v_item ->> 'expected_status';
            IF v_expected_status <> 'reviewing' OR NOT EXISTS (
                SELECT FROM public.task_batches AS batch
                WHERE batch.id=v_batch_id AND batch.project_id=v_project_id
                  AND batch.status=v_expected_status
                  AND (v_operation='bulk_reject' OR batch.admin_locked IS FALSE)
                FOR UPDATE
            ) THEN
                v_error_code := 'batch_state_changed';
                v_error_details := jsonb_build_object('batch_id',v_batch_id);
                EXIT;
            END IF;
        END LOOP;
        IF v_error_code IS NULL THEN
            FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'batches') AS items(value)
            LOOP
                v_batch_id := (v_item ->> 'batch_id')::uuid;
                IF v_operation='bulk_approve' THEN
                    UPDATE public.task_batches AS batch SET status='approved',updated_at=v_now
                    WHERE batch.id=v_batch_id AND batch.project_id=v_project_id;
                ELSE
                    UPDATE public.tasks AS task
                    SET status='pending',is_labeled=false,review_contributor_ids='null'::jsonb,
                        review_submitter_id=NULL,updated_at=v_now
                    WHERE task.batch_id=v_batch_id AND task.status IN ('review','completed');
                    UPDATE public.task_batches AS batch
                    SET status='rejected',review_feedback=v_feedback,
                        reviewed_at=v_reviewed_at,reviewed_by=v_reviewer_id,updated_at=v_now
                    WHERE batch.id=v_batch_id AND batch.project_id=v_project_id;
                END IF;
                v_batch_ids := array_append(v_batch_ids,v_batch_id);
            END LOOP;
            IF v_operation='bulk_reject' THEN
                FOR v_item IN SELECT value FROM jsonb_array_elements(p_request -> 'batches') AS items(value)
                LOOP
                    v_batch_id := (v_item ->> 'batch_id')::uuid;
                    SELECT count(*)::integer,
                        count(*) FILTER (WHERE task.is_labeled IS TRUE)::integer,
                        count(*) FILTER (WHERE task.status='review')::integer
                    INTO v_total,v_completed,v_review
                    FROM public.tasks AS task WHERE task.batch_id=v_batch_id;
                    UPDATE public.task_batches AS batch
                    SET total_tasks=v_total,completed_tasks=v_completed,review_tasks=v_review,
                        updated_at=v_now
                    WHERE batch.id=v_batch_id AND batch.project_id=v_project_id
                      AND (batch.total_tasks IS DISTINCT FROM v_total
                           OR batch.completed_tasks IS DISTINCT FROM v_completed
                           OR batch.review_tasks IS DISTINCT FROM v_review);
                END LOOP;
                SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
                INTO v_tracked_task_ids FROM jsonb_array_elements_text(p_request -> 'tracked_task_ids') AS ids(value);
            END IF;
        END IF;
        v_result := jsonb_build_object('project_id',v_project_id,'batch_ids',to_jsonb(v_batch_ids),
            'changed',cardinality(v_batch_ids)>0,
            'status',CASE WHEN v_operation='bulk_approve' THEN 'approved' ELSE 'rejected' END);

    ELSIF v_operation='reject_batch' THEN
        v_expected_keys := ARRAY['actor_id','batch_id','expected_status','feedback','operation','reviewed_at','reviewer_id','schema','tracked_task_ids']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'expected_status') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'actor_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'reviewer_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'feedback') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'reviewed_at') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'tracked_task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling reject-batch request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        v_expected_status := p_request ->> 'expected_status';
        v_actor_id := (p_request ->> 'actor_id')::uuid;
        v_reviewer_id := (p_request ->> 'reviewer_id')::uuid;
        v_feedback := p_request ->> 'feedback';
        v_reviewed_at := (p_request ->> 'reviewed_at')::timestamptz;
        SELECT * INTO v_batch FROM public.task_batches AS batch WHERE batch.id=v_batch_id FOR UPDATE;
        IF NOT FOUND THEN
            v_error_code := 'batch_not_found';
        ELSIF v_batch.status IS DISTINCT FROM v_expected_status OR v_expected_status NOT IN ('active','annotating','reviewing') THEN
            v_error_code := 'batch_state_changed';
            v_error_details := jsonb_build_object('status',v_batch.status);
        ELSE
            UPDATE public.tasks AS task
            SET status='pending',review_contributor_ids='null'::jsonb,review_submitter_id=NULL,
                updated_at=v_now
            WHERE task.batch_id=v_batch_id AND task.status IN ('review','completed');
            GET DIAGNOSTICS v_count = ROW_COUNT;
            UPDATE public.task_batches AS batch
            SET status='rejected',review_feedback=v_feedback,reviewed_at=v_reviewed_at,
                reviewed_by=v_reviewer_id,updated_at=v_now
            WHERE batch.id=v_batch_id;
            SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
            INTO v_tracked_task_ids FROM jsonb_array_elements_text(p_request -> 'tracked_task_ids') AS ids(value);
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_batch.project_id,
                'batch_ids',jsonb_build_array(v_batch_id),
                'affected',v_count,'changed',true,'from_status',v_expected_status,'status','rejected');
        END IF;

    ELSIF v_operation='recalculate_batch_counters' THEN
        v_expected_keys := ARRAY['batch_id','operation','schema']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Labeling batch-counter request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        SELECT * INTO v_batch FROM public.task_batches AS batch WHERE batch.id=v_batch_id FOR UPDATE;
        IF NOT FOUND THEN
            v_result := jsonb_build_object('batch_id',v_batch_id,
                'batch_ids',jsonb_build_array(v_batch_id),'changed',false);
        ELSE
            SELECT count(*)::integer,
                count(*) FILTER (WHERE task.status='completed')::integer,
                count(*) FILTER (WHERE task.status='review')::integer
            INTO v_total,v_completed,v_review
            FROM public.tasks AS task WHERE task.batch_id=v_batch_id;
            v_operation_changed := v_batch.total_tasks IS DISTINCT FROM v_total
                OR v_batch.completed_tasks IS DISTINCT FROM v_completed
                OR v_batch.review_tasks IS DISTINCT FROM v_review;
            UPDATE public.task_batches AS batch
            SET total_tasks=v_total,completed_tasks=v_completed,review_tasks=v_review,
                updated_at=v_now
            WHERE batch.id=v_batch_id AND (
                batch.total_tasks IS DISTINCT FROM v_total
                OR batch.completed_tasks IS DISTINCT FROM v_completed
                OR batch.review_tasks IS DISTINCT FROM v_review
            );
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_batch.project_id,
                'batch_ids',jsonb_build_array(v_batch_id),
                'changed',v_operation_changed,'total_tasks',v_total,
                'completed_tasks',v_completed,'review_tasks',v_review);
        END IF;

    ELSIF v_operation IN ('mark_preannotated','check_auto_transition','reactivate_preannotated') THEN
        v_expected_keys := ARRAY['batch_id','expected_status','operation','schema']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'batch_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'expected_status') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Labeling batch-transition request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := (p_request ->> 'batch_id')::uuid;
        v_expected_status := p_request ->> 'expected_status';
        SELECT * INTO v_batch FROM public.task_batches AS batch WHERE batch.id=v_batch_id FOR UPDATE;
        IF NOT FOUND THEN
            v_result := jsonb_build_object('batch_id',v_batch_id,
                'batch_ids',jsonb_build_array(v_batch_id),'changed',false);
        ELSIF v_batch.status IS DISTINCT FROM v_expected_status THEN
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_batch.project_id,
                'batch_ids',jsonb_build_array(v_batch_id),
                'changed',false,'from_status',v_expected_status,'status',v_batch.status);
        ELSE
            v_from_status := v_batch.status;
            v_target_status := NULL;
            IF v_operation='mark_preannotated' THEN
                IF v_batch.status='active' AND v_batch.admin_locked IS FALSE THEN
                    v_target_status := 'pre_annotated';
                END IF;
            ELSIF v_operation='reactivate_preannotated' THEN
                IF v_batch.status='pre_annotated' THEN v_target_status := 'active'; END IF;
            ELSIF v_batch.admin_locked IS FALSE AND v_batch.status IN ('active','pre_annotated')
                AND EXISTS (SELECT FROM public.tasks AS task
                    WHERE task.batch_id=v_batch_id AND task.status IN ('in_progress','rejected')) THEN
                v_target_status := 'annotating';
            ELSIF v_batch.admin_locked IS FALSE AND v_batch.status='annotating'
                AND NOT EXISTS (SELECT FROM public.tasks AS task
                    WHERE task.batch_id=v_batch_id AND task.status IN ('pending','in_progress','rejected')) THEN
                v_target_status := 'reviewing';
            END IF;
            IF v_target_status IS NOT NULL THEN
                UPDATE public.task_batches AS batch SET status=v_target_status,updated_at=v_now
                WHERE batch.id=v_batch_id;
                v_status_changed := true;
            END IF;
            v_result := jsonb_build_object('batch_id',v_batch_id,'project_id',v_batch.project_id,
                'batch_ids',jsonb_build_array(v_batch_id),
                'changed',v_status_changed,'from_status',v_from_status,
                'status',COALESCE(v_target_status,v_from_status));
        END IF;

    ELSIF v_operation='clear_annotation_contributors' THEN
        v_expected_keys := ARRAY['operation','schema','task_ids','tracked_task_ids']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'tracked_task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling contributor-cleanup request' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
        INTO v_task_ids FROM jsonb_array_elements_text(p_request -> 'task_ids') AS ids(value);
        UPDATE public.tasks AS task SET annotation_contributor_ids='null'::jsonb,updated_at=v_now
        WHERE task.id=ANY(v_task_ids);
        GET DIAGNOSTICS v_count = ROW_COUNT;
        SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
        INTO v_tracked_task_ids FROM jsonb_array_elements_text(p_request -> 'tracked_task_ids') AS ids(value);
        v_result := jsonb_build_object('task_ids',to_jsonb(v_task_ids),'affected',v_count,'changed',v_count>0);

    ELSIF v_operation='deactivate_prediction_annotations' THEN
        v_expected_keys := ARRAY['operation','schema','task_ids','tracked_annotation_ids']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'tracked_annotation_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling prediction-annotation cleanup request' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
        INTO v_task_ids FROM jsonb_array_elements_text(p_request -> 'task_ids') AS ids(value);
        SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
        INTO v_tracked_annotation_ids
        FROM jsonb_array_elements_text(p_request -> 'tracked_annotation_ids') AS ids(value);
        WITH updated AS (
            UPDATE public.annotations AS annotation
            SET is_active=false,updated_at=v_now
            WHERE annotation.task_id=ANY(v_task_ids)
              AND annotation.source='prediction_based' AND annotation.is_active IS TRUE
            RETURNING annotation.id
        )
        SELECT count(*)::integer,
            COALESCE(array_agg(updated.id ORDER BY updated.id)
                FILTER (WHERE updated.id=ANY(v_tracked_annotation_ids)),ARRAY[]::uuid[])
        INTO v_ai_annotation_count,v_changed_annotation_ids FROM updated;
        v_result := jsonb_build_object('task_ids',to_jsonb(v_task_ids),
            'ai_annotations_deactivated',v_ai_annotation_count,'affected',v_ai_annotation_count,
            'changed',v_ai_annotation_count>0);

    ELSIF v_operation='detach_prediction_parents' THEN
        v_expected_keys := ARRAY['batch_id','operation','schema','task_ids','tracked_annotation_ids']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'task_ids') NOT IN ('null','array')
           OR jsonb_typeof(p_request -> 'batch_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'tracked_annotation_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling prediction-reference cleanup request' USING ERRCODE = '22023';
        END IF;
        v_batch_id := NULLIF(p_request ->> 'batch_id','')::uuid;
        IF v_batch_id IS NOT NULL THEN
            SELECT COALESCE(array_agg(task.id ORDER BY task.id),ARRAY[]::uuid[])
            INTO v_task_ids FROM public.tasks AS task WHERE task.batch_id=v_batch_id;
        ELSE
            SELECT COALESCE(array_agg(value::uuid),ARRAY[]::uuid[])
            INTO v_task_ids FROM jsonb_array_elements_text(p_request -> 'task_ids') AS ids(value);
        END IF;
        SELECT COALESCE(array_agg(value::uuid), ARRAY[]::uuid[])
        INTO v_tracked_annotation_ids
        FROM jsonb_array_elements_text(p_request -> 'tracked_annotation_ids') AS ids(value);
        WITH updated AS (
            UPDATE public.annotations AS annotation
            SET parent_prediction_id=NULL,updated_at=v_now
            WHERE annotation.task_id=ANY(v_task_ids)
            RETURNING annotation.id
        )
        SELECT count(*)::integer,
            COALESCE(array_agg(updated.id ORDER BY updated.id)
                FILTER (WHERE updated.id=ANY(v_tracked_annotation_ids)),ARRAY[]::uuid[])
        INTO v_count,v_changed_annotation_ids FROM updated;
        v_result := jsonb_build_object('task_ids',to_jsonb(v_task_ids),'affected',v_count,'changed',v_count>0);

    ELSIF v_operation='clear_prediction_projection' THEN
        v_expected_keys := ARRAY['operation','schema','task_ids','tracked_task_ids']::text[];
        IF v_keys IS DISTINCT FROM v_expected_keys
           OR jsonb_typeof(p_request -> 'task_ids') IS DISTINCT FROM 'array'
           OR jsonb_typeof(p_request -> 'tracked_task_ids') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling prediction-projection request' USING ERRCODE = '22023';
        END IF;
        SELECT COALESCE(array_agg(value::uuid),ARRAY[]::uuid[])
        INTO v_task_ids FROM jsonb_array_elements_text(p_request -> 'task_ids') AS ids(value);
        UPDATE public.tasks AS task SET total_predictions=0,
            total_annotations=COALESCE((
                SELECT count(*)::integer FROM public.annotations AS annotation
                WHERE annotation.task_id=task.id AND annotation.is_active IS TRUE
                  AND annotation.was_cancelled IS FALSE
            ),0),
            is_labeled=COALESCE((
                SELECT count(*)::integer FROM public.annotations AS annotation
                WHERE annotation.task_id=task.id AND annotation.is_active IS TRUE
                  AND annotation.was_cancelled IS FALSE
            ),0)>0,
            updated_at=v_now
        WHERE task.id=ANY(v_task_ids);
        GET DIAGNOSTICS v_count = ROW_COUNT;
        SELECT COALESCE(array_agg(value::uuid),ARRAY[]::uuid[])
        INTO v_tracked_task_ids FROM jsonb_array_elements_text(p_request -> 'tracked_task_ids') AS ids(value);
        v_result := jsonb_build_object('task_ids',to_jsonb(v_task_ids),'affected',v_count,'changed',v_count>0);

    ELSE
        RAISE EXCEPTION 'Unsupported Labeling batch-control operation' USING ERRCODE = '22023';
    END IF;

    IF v_error_code IS NOT NULL THEN
        RETURN jsonb_build_object(
            'schema','aap-labeling-batch-control-data/v1','ok',false,
            'request',p_request,
            'error',jsonb_build_object('code',v_error_code,'details',v_error_details)
        );
    END IF;

    IF cardinality(v_tracked_task_ids)>0 THEN
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'id',task.id,'project_id',task.project_id,'batch_id',task.batch_id,
            'status',task.status,'assignee_id',task.assignee_id,
            'assignee_is_override',task.assignee_is_override,'assigned_at',task.assigned_at,
            'reviewer_id',task.reviewer_id,'reviewer_is_override',task.reviewer_is_override,
            'reviewer_claimed_at',task.reviewer_claimed_at,
            'review_contributor_ids',task.review_contributor_ids,
            'review_submitter_id',task.review_submitter_id,
            'annotation_contributor_ids',task.annotation_contributor_ids,
            'total_annotations',task.total_annotations,'total_predictions',task.total_predictions,
            'is_labeled',task.is_labeled,'sequence_order',task.sequence_order,
            'updated_at',task.updated_at
        ) ORDER BY task.id),'[]'::jsonb)
        INTO v_tasks FROM public.tasks AS task WHERE task.id=ANY(v_tracked_task_ids);
    END IF;
    IF cardinality(v_changed_annotation_ids)>0 THEN
        SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'id',annotation.id,'is_active',annotation.is_active,
            'parent_prediction_id',annotation.parent_prediction_id,
            'updated_at',annotation.updated_at
        ) ORDER BY annotation.id),'[]'::jsonb)
        INTO v_annotations FROM public.annotations AS annotation
        WHERE annotation.id=ANY(v_changed_annotation_ids);
    END IF;
    IF jsonb_typeof(v_result -> 'batch_ids') IS DISTINCT FROM 'array' THEN
        v_batch_ids := ARRAY[]::uuid[];
    ELSE
        SELECT COALESCE(array_agg(value::uuid),ARRAY[]::uuid[])
        INTO v_batch_ids
        FROM jsonb_array_elements_text(v_result -> 'batch_ids') AS ids(value);
        IF cardinality(v_batch_ids)>0 THEN
            SELECT COALESCE(jsonb_agg(jsonb_build_object(
                'id',batch.id,'project_id',batch.project_id,'dataset_id',batch.dataset_id,
                'display_id',batch.display_id,'name',batch.name,'description',batch.description,
                'status',batch.status,'priority',batch.priority,'deadline',batch.deadline,
                'assigned_user_ids',batch.assigned_user_ids,'total_tasks',batch.total_tasks,
                'completed_tasks',batch.completed_tasks,'review_tasks',batch.review_tasks,
                'approved_tasks',batch.approved_tasks,'rejected_tasks',batch.rejected_tasks,
                'created_by',batch.created_by,'created_at',batch.created_at,
                'updated_at',batch.updated_at,'review_feedback',batch.review_feedback,
                'reviewed_at',batch.reviewed_at,'reviewed_by',batch.reviewed_by,
                'annotator_id',batch.annotator_id,'reviewer_id',batch.reviewer_id,
                'admin_locked',batch.admin_locked,'admin_lock_reason',batch.admin_lock_reason,
                'admin_locked_at',batch.admin_locked_at,'admin_locked_by',batch.admin_locked_by
            ) ORDER BY batch.id),'[]'::jsonb)
            INTO v_batch_snapshots
            FROM public.task_batches AS batch WHERE batch.id=ANY(v_batch_ids);
        END IF;
    END IF;
    v_result := v_result || jsonb_build_object(
        'tasks',v_tasks,
        'annotations',v_annotations,
        'batches',v_batch_snapshots,
        'deleted_batch_ids',to_jsonb(v_deleted_batch_ids),
        'cleared_assignee_task_ids',to_jsonb(v_cleared_assignee_task_ids),
        'cleared_reviewer_task_ids',to_jsonb(v_cleared_reviewer_task_ids)
    );
    RETURN jsonb_build_object(
        'schema','aap-labeling-batch-control-data/v1','ok',true,
        'request',p_request,'data',v_result
    );
END
$function$;

-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.batch_control_command(jsonb) FROM PUBLIC;
