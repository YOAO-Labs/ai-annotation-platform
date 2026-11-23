CREATE FUNCTION yoao_learning_v1.selection_create_round(
    p_id uuid, p_project uuid, p_creator uuid, p_model uuid, p_deployment uuid,
    p_request uuid, p_request_digest text, p_name text, p_spec jsonb, p_spec_digest text,
    p_authorization jsonb, p_storage jsonb, p_candidate_requests uuid[]
) RETURNS SETOF public.active_learning_rounds
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_spec->>'round_id' IS DISTINCT FROM p_id::text
       OR jsonb_array_length(p_spec->'samples') IS DISTINCT FROM cardinality(p_candidate_requests)
       OR cardinality(p_candidate_requests) NOT BETWEEN 1 AND 32 THEN
        RAISE EXCEPTION 'Invalid frozen selection' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    INSERT INTO public.active_learning_rounds AS r (
        id, project_id, creator_id, model_version_id, deployment_id, request_id,
        request_digest, name, status, spec, spec_digest, storage_ref, authorization,
        generation, inference_dispatches, accepted_tasks
    ) VALUES (
        p_id, p_project, p_creator, p_model, p_deployment, p_request,
        p_request_digest, p_name, 'queued', p_spec, p_spec_digest, p_storage,
        p_authorization, 0, 0, 0
    ) RETURNING r.*;
    INSERT INTO public.active_learning_pool_members (round_id, task_id, dataset_item_id)
        SELECT p_id, (sample->>'task_id')::uuid, (sample#>>'{snapshot,dataset_item_id}')::uuid
        FROM jsonb_array_elements(p_spec->'pool') AS pool(sample);
    INSERT INTO public.active_learning_candidates (
        round_id, ordinal, task_id, dataset_item_id, request_id, status
    ) SELECT p_id, (sample->>'ordinal')::int, m.task_id, m.dataset_item_id,
             p_candidate_requests[i::int], 'pending'
      FROM jsonb_array_elements(p_spec->'samples') WITH ORDINALITY AS candidates(sample, i)
      LEFT JOIN public.active_learning_pool_members AS m
        ON m.round_id = p_id AND m.task_id = (sample->>'task_id')::uuid;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_round(p_project uuid, p_round uuid, p_lock text)
RETURNS SETOF public.active_learning_rounds
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock = 'update' THEN
        RETURN QUERY SELECT r.* FROM public.active_learning_rounds AS r
        WHERE r.id = p_round AND (p_project IS NULL OR r.project_id = p_project) FOR UPDATE;
    ELSIF p_lock = 'maintenance' AND p_project IS NULL THEN
        RETURN QUERY SELECT r.* FROM public.active_learning_rounds AS r
        WHERE r.id = p_round FOR UPDATE SKIP LOCKED;
    ELSIF p_lock = 'read' THEN
        RETURN QUERY SELECT r.* FROM public.active_learning_rounds AS r
        WHERE r.id = p_round AND (p_project IS NULL OR r.project_id = p_project);
    ELSE
        RAISE EXCEPTION 'Invalid selection lock' USING ERRCODE = '22023';
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_round_request(p_project uuid, p_request uuid)
RETURNS SETOF public.active_learning_rounds
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.* FROM public.active_learning_rounds AS r
    WHERE r.project_id = p_project AND r.request_id = p_request
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_round_for_job(p_job uuid)
RETURNS SETOF public.active_learning_rounds
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.* FROM public.active_learning_rounds AS r WHERE r.async_job_id = p_job FOR UPDATE
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_rounds(p_project uuid, p_task text, p_limit integer, p_offset integer)
RETURNS SETOF public.active_learning_rounds
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
SELECT r.* FROM public.active_learning_rounds AS r
WHERE r.project_id = p_project AND (
    p_task IS NULL OR (p_task = 'detect' AND COALESCE(r.spec#>>'{model_ref,task}', 'detect')
        IN ('detect','detection','image-detection')) OR
    (p_task = 'segment' AND r.spec#>>'{model_ref,task}'
        IN ('segment','segmentation','image-instance-segmentation','instance-segmentation'))
)
ORDER BY r.created_at DESC, r.id DESC LIMIT LEAST(p_limit, 100) OFFSET p_offset
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_round_count(p_project uuid, p_task text)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN (SELECT count(*) FROM public.active_learning_rounds AS r
WHERE r.project_id = p_project AND (
    p_task IS NULL OR (p_task = 'detect' AND COALESCE(r.spec#>>'{model_ref,task}', 'detect')
        IN ('detect','detection','image-detection')) OR
    (p_task = 'segment' AND r.spec#>>'{model_ref,task}'
        IN ('segment','segmentation','image-instance-segmentation','instance-segmentation'))
));
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_attempt(p_attempt uuid, p_round uuid)
RETURNS SETOF public.active_learning_attempts
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT a.* FROM public.active_learning_attempts AS a
    WHERE a.id = p_attempt AND (p_round IS NULL OR a.round_id = p_round)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_recent_attempts(p_round uuid)
RETURNS SETOF public.active_learning_attempts
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT a.* FROM public.active_learning_attempts AS a
    WHERE a.round_id = p_round ORDER BY a.ordinal DESC LIMIT 20
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_attempt_ids(p_round uuid)
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT a.id FROM public.active_learning_attempts AS a WHERE a.round_id = p_round
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_candidates(p_round uuid, p_ordinal integer)
RETURNS SETOF public.active_learning_candidates
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT c.* FROM public.active_learning_candidates AS c
    WHERE c.round_id = p_round AND (p_ordinal IS NULL OR c.ordinal = p_ordinal) ORDER BY c.ordinal
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_due_rounds(p_cutoff timestamptz)
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
SELECT r.id FROM public.active_learning_rounds AS r
JOIN public.active_learning_attempts AS a ON a.id = r.current_attempt_id
WHERE r.status = 'cancelling'
   OR (r.status = 'queued' AND (a.dispatched_at IS NULL OR a.dispatched_at < p_cutoff - interval '30 seconds'))
   OR (r.status = 'running' AND (a.heartbeat_at IS NULL OR a.heartbeat_at < p_cutoff - interval '45 seconds'))
ORDER BY a.maintenance_at NULLS FIRST, r.id LIMIT 100
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_open_attempt(
    p_round uuid, p_generation integer, p_previous uuid, p_attempt uuid, p_job uuid, p_at timestamptz
) RETURNS SETOF public.active_learning_rounds
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    r public.active_learning_rounds;
BEGIN
    SELECT * INTO r FROM public.active_learning_rounds WHERE id = p_round FOR UPDATE;
    IF NOT FOUND OR r.generation <> p_generation
       OR r.current_attempt_id IS DISTINCT FROM p_previous OR r.storage_ref IS NULL
       OR (p_previous IS NULL AND (r.status <> 'queued' OR p_generation <> 0))
       OR (p_previous IS NOT NULL AND (r.status NOT IN ('lost','failed')
           OR extract(epoch FROM greatest(p_at, clock_timestamp())) >= (r.spec->>'expires_at')::bigint
           OR (r.status = 'failed' AND EXISTS (
               SELECT 1 FROM public.active_learning_candidates AS c
               WHERE c.round_id = r.id AND c.request IS NOT NULL AND c.request <> 'null'::jsonb
           )))) THEN
        RAISE EXCEPTION 'Selection attempt changed' USING ERRCODE = '40001';
    END IF;
    IF p_previous IS NOT NULL THEN
        UPDATE public.active_learning_attempts AS a SET claim_id = NULL, completed_at = COALESCE(a.completed_at, p_at)
        WHERE a.id = p_previous AND a.round_id = r.id AND a.generation = p_generation;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Selection previous attempt missing' USING ERRCODE = '40001';
        END IF;
        UPDATE public.active_learning_candidates AS c SET status = 'pending', error_code = NULL, error_message = NULL
        WHERE c.round_id = r.id AND c.status = 'failed';
    END IF;
    INSERT INTO public.active_learning_attempts (id, round_id, ordinal, generation, status, async_job_id)
    SELECT p_attempt, r.id, COALESCE(max(a.ordinal), 0) + 1, p_generation + 1, 'queued', p_job
    FROM public.active_learning_attempts AS a WHERE a.round_id = r.id;
    RETURN QUERY UPDATE public.active_learning_rounds AS target SET
        generation = p_generation + 1, current_attempt_id = p_attempt, async_job_id = p_job,
        status = 'queued', error_code = NULL, error_message = NULL, completed_at = NULL
    WHERE target.id = r.id RETURNING target.*;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_claim_attempt(p_attempt uuid, p_generation integer, p_claim uuid, p_at timestamptz)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    round_identity uuid;
BEGIN
    SELECT r.id INTO round_identity FROM public.active_learning_rounds AS r
    WHERE r.current_attempt_id = p_attempt AND r.generation = p_generation AND r.status = 'queued' FOR UPDATE;
    IF NOT FOUND OR p_claim IS NULL THEN RETURN false; END IF;
    UPDATE public.active_learning_attempts AS a SET status = 'running', claim_id = p_claim,
        started_at = p_at, heartbeat_at = p_at
    WHERE a.id = p_attempt AND a.round_id = round_identity AND a.generation = p_generation
        AND a.status = 'queued' AND a.claim_id IS NULL;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.active_learning_rounds SET status = 'running' WHERE id = round_identity;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_touch_attempt(
    p_attempt uuid, p_generation integer, p_claim uuid, p_event text, p_at timestamptz
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    current_status text;
BEGIN
    SELECT r.status INTO current_status FROM public.active_learning_rounds AS r
    WHERE r.current_attempt_id = p_attempt AND r.generation = p_generation FOR UPDATE;
    IF NOT FOUND OR NOT (
        (p_event = 'heartbeat' AND p_claim IS NOT NULL AND current_status IN ('running','cancelling'))
        OR (p_event = 'dispatch' AND p_claim IS NULL AND current_status = 'queued')
        OR (p_event = 'maintenance' AND current_status IN ('queued','running','cancelling'))
    ) THEN RETURN false; END IF;
    UPDATE public.active_learning_attempts AS a SET
        heartbeat_at = CASE WHEN p_event = 'heartbeat' THEN p_at ELSE a.heartbeat_at END,
        dispatched_at = CASE WHEN p_event = 'dispatch' THEN p_at ELSE a.dispatched_at END,
        maintenance_at = CASE WHEN p_event = 'maintenance' THEN p_at ELSE a.maintenance_at END
    WHERE a.id = p_attempt AND a.generation = p_generation AND a.claim_id IS NOT DISTINCT FROM p_claim;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_debit_round(p_attempt uuid, p_generation integer, p_claim uuid, p_at timestamptz)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.active_learning_rounds AS r SET inference_dispatches = r.inference_dispatches + 1
    FROM public.active_learning_attempts AS a
    WHERE r.current_attempt_id = p_attempt AND a.id = p_attempt AND a.round_id = r.id
      AND a.generation = p_generation AND r.generation = p_generation AND a.claim_id = p_claim
      AND r.status = 'running'
      AND extract(epoch FROM greatest(p_at, clock_timestamp())) < (r.spec->>'expires_at')::bigint
      AND r.inference_dispatches < (r.spec->>'max_inference_dispatches')::int;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_candidate_request(
    p_attempt uuid, p_generation integer, p_claim uuid, p_ordinal integer, p_request jsonb, p_media jsonb
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    round_identity uuid;
BEGIN
    SELECT r.id INTO round_identity FROM public.active_learning_rounds AS r
    JOIN public.active_learning_attempts AS a ON a.id = r.current_attempt_id AND a.round_id = r.id
    WHERE a.id = p_attempt AND r.generation = p_generation AND a.generation = p_generation
        AND a.claim_id = p_claim AND r.status = 'running' FOR UPDATE OF r;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.active_learning_candidates AS c SET status = 'inflight', request = p_request, media = p_media
    WHERE c.round_id = round_identity AND c.ordinal = p_ordinal AND c.status IN ('pending','inflight')
      AND (c.request IS NULL OR c.request = 'null'::jsonb OR c.request = p_request)
      AND (c.media IS NULL OR c.media = 'null'::jsonb OR c.media = p_media);
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_candidate_evidence(
    p_attempt uuid, p_generation integer, p_claim uuid, p_ordinal integer, p_evidence jsonb
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    round_identity uuid;
BEGIN
    SELECT r.id INTO round_identity FROM public.active_learning_rounds AS r
    JOIN public.active_learning_attempts AS a ON a.id = r.current_attempt_id AND a.round_id = r.id
    WHERE a.id = p_attempt AND r.generation = p_generation AND a.generation = p_generation
        AND a.claim_id = p_claim AND r.status = 'running' FOR UPDATE OF r;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.active_learning_candidates AS c SET status = 'succeeded', evidence = p_evidence,
        produced_attempt_id = p_attempt, error_code = NULL, error_message = NULL
    WHERE c.round_id = round_identity AND c.ordinal = p_ordinal AND c.status = 'inflight'
      AND (c.evidence IS NULL OR c.evidence = 'null'::jsonb)
      AND p_evidence->>'request_id' = c.request_id::text
      AND p_evidence->>'produced_attempt_id' = p_attempt::text;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_settle_attempt(
    p_attempt uuid, p_generation integer, p_claim uuid, p_status text,
    p_code text, p_message text, p_report jsonb, p_at timestamptz
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    r public.active_learning_rounds;
    terminal boolean := p_status IN ('succeeded','failed','cancelled');
BEGIN
    SELECT * INTO r FROM public.active_learning_rounds AS target
    WHERE target.current_attempt_id = p_attempt AND target.generation = p_generation FOR UPDATE;
    IF NOT FOUND OR (CASE p_status
        WHEN 'succeeded' THEN r.status = 'running' AND p_claim IS NOT NULL
            AND p_report->>'attempt_id' = p_attempt::text AND p_report->>'spec_digest' = r.spec_digest
            AND NOT EXISTS (SELECT 1 FROM public.active_learning_candidates AS c WHERE c.round_id = r.id AND c.status <> 'succeeded')
        WHEN 'lost' THEN r.status IN ('running','queued')
        WHEN 'cancelling' THEN r.status IN ('queued','running','lost','cancelling')
        WHEN 'failed' THEN r.status = 'cancelling' OR (r.status IN ('queued','running','lost') AND NOT EXISTS (
            SELECT 1 FROM public.active_learning_candidates AS c WHERE c.round_id = r.id AND c.request IS NOT NULL AND c.request <> 'null'::jsonb))
        WHEN 'cancelled' THEN r.status = 'cancelling' OR (r.status IN ('queued','running','lost') AND NOT EXISTS (
            SELECT 1 FROM public.active_learning_candidates AS c WHERE c.round_id = r.id AND c.request IS NOT NULL AND c.request <> 'null'::jsonb))
        ELSE false END) IS NOT TRUE THEN RETURN false; END IF;
    UPDATE public.active_learning_attempts AS a SET status = p_status,
        claim_id = CASE WHEN terminal OR p_status = 'lost' THEN NULL ELSE a.claim_id END,
        completed_at = CASE WHEN terminal THEN p_at ELSE a.completed_at END,
        report = CASE WHEN terminal THEN p_report ELSE a.report END,
        error_code = CASE WHEN p_status <> 'cancelling' THEN p_code ELSE a.error_code END,
        error_message = CASE WHEN p_status <> 'cancelling' THEN p_message ELSE a.error_message END
    WHERE a.id = p_attempt AND a.round_id = r.id AND a.generation = p_generation
      AND a.claim_id IS NOT DISTINCT FROM p_claim;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.active_learning_rounds SET status = p_status, error_code = p_code, error_message = p_message,
        completed_at = CASE WHEN terminal THEN p_at ELSE r.completed_at END WHERE id = r.id;
    RETURN true;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_release_cancelled_claim(p_attempt uuid, p_generation integer, p_claim uuid)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.active_learning_attempts AS a SET claim_id = NULL
    FROM public.active_learning_rounds AS r
    WHERE r.current_attempt_id = p_attempt AND r.generation = p_generation AND a.id = p_attempt
      AND a.round_id = r.id AND a.generation = p_generation AND a.claim_id = p_claim
      AND r.status = 'cancelling' AND a.status = 'cancelling';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_decision_request(p_round uuid, p_request uuid)
RETURNS SETOF public.active_learning_decisions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT d.* FROM public.active_learning_decisions AS d WHERE d.round_id = p_round AND d.request_id = p_request
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_decisions(p_round uuid, p_limit integer, p_offset integer)
RETURNS SETOF public.active_learning_decisions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT d.* FROM public.active_learning_decisions AS d
    WHERE d.round_id = p_round
    ORDER BY d.created_at DESC, d.id DESC LIMIT LEAST(p_limit, 100) OFFSET p_offset
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_accepted_decisions(p_round uuid)
RETURNS SETOF public.active_learning_decisions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT d.* FROM public.active_learning_decisions AS d
    WHERE d.round_id = p_round AND d.action = 'accept' ORDER BY d.created_at, d.id
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_record_decision(
    p_round uuid, p_attempt uuid, p_actor uuid, p_request uuid, p_request_digest text,
    p_action text, p_batch uuid, p_ordinals integer[], p_snapshot jsonb, p_snapshot_digest text
) RETURNS SETOF public.active_learning_decisions
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    r public.active_learning_rounds;
    wanted integer := cardinality(p_ordinals);
    identity uuid := gen_random_uuid();
BEGIN
    SELECT * INTO r FROM public.active_learning_rounds WHERE id = p_round FOR UPDATE;
    IF NOT FOUND OR r.status <> 'succeeded' OR r.current_attempt_id IS DISTINCT FROM p_attempt
        OR wanted IS NULL OR wanted NOT BETWEEN 1 AND 32
        OR p_action NOT IN ('accept','reject') OR (p_batch IS NULL) = (p_action = 'accept')
        OR (p_action = 'accept' AND r.accepted_tasks + wanted > (r.spec->>'max_review_tasks')::int)
        OR wanted <> (SELECT count(*) FROM public.active_learning_candidates AS c
            WHERE c.round_id = p_round AND c.ordinal = ANY(p_ordinals) AND c.disposition IS NULL AND c.status = 'succeeded')
        OR NOT EXISTS (SELECT 1 FROM public.active_learning_attempts AS a
            WHERE a.id = p_attempt AND a.round_id = p_round AND a.status = 'succeeded'
              AND a.report->>'report_digest' = p_snapshot->>'report_digest') THEN
        RAISE EXCEPTION 'Selection decision changed' USING ERRCODE = '40001';
    END IF;
    IF p_action = 'accept' AND wanted <> (
        SELECT count(*) FROM public.active_learning_attempts AS a,
             LATERAL jsonb_array_elements(a.report->'ranking') AS rank(item)
        WHERE a.id = p_attempt AND (item->>'ordinal')::int = ANY(p_ordinals)
          AND item->>'recommended' = 'true'
    ) THEN RAISE EXCEPTION 'Selection is not recommended' USING ERRCODE = '22023'; END IF;
    RETURN QUERY INSERT INTO public.active_learning_decisions AS d (
        id, round_id, actor_id, request_id, request_digest, action, accepted_batch_id, snapshot, snapshot_digest
    ) VALUES (
        identity, p_round, p_actor, p_request, p_request_digest, p_action, p_batch, p_snapshot, p_snapshot_digest
    ) RETURNING d.*;
    UPDATE public.active_learning_candidates SET decision_id = identity,
        disposition = CASE WHEN p_action = 'accept' THEN 'accepted' ELSE 'rejected' END
    WHERE round_id = p_round AND ordinal = ANY(p_ordinals);
    IF p_action = 'accept' THEN
        UPDATE public.active_learning_rounds SET accepted_tasks = accepted_tasks + wanted WHERE id = p_round;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_feedback_request(p_round uuid, p_request uuid)
RETURNS SETOF public.active_learning_feedback
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT f.* FROM public.active_learning_feedback AS f WHERE f.round_id = p_round AND f.request_id = p_request
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_feedback(p_round uuid, p_limit integer, p_offset integer)
RETURNS SETOF public.active_learning_feedback
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT f.* FROM public.active_learning_feedback AS f WHERE f.round_id = p_round
    ORDER BY f.created_at DESC, f.id DESC LIMIT LEAST(p_limit, 100) OFFSET p_offset
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_receipt_count(p_round uuid, p_feedback boolean)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN CASE WHEN p_feedback THEN
    (SELECT count(*) FROM public.active_learning_feedback AS f WHERE f.round_id = p_round)
ELSE (SELECT count(*) FROM public.active_learning_decisions AS d WHERE d.round_id = p_round) END;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_record_feedback(
    p_round uuid, p_actor uuid, p_request uuid, p_request_digest text, p_snapshot jsonb, p_snapshot_digest text
) RETURNS SETOF public.active_learning_feedback
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.active_learning_feedback AS f (
        id, round_id, actor_id, request_id, request_digest, snapshot, snapshot_digest
    ) SELECT gen_random_uuid(), r.id, p_actor, p_request, p_request_digest, p_snapshot, p_snapshot_digest
    FROM public.active_learning_rounds AS r
    WHERE r.id = p_round AND r.spec_digest = p_snapshot->>'spec_digest' AND r.id::text = p_snapshot->>'round_id'
      AND EXISTS (SELECT 1 FROM public.active_learning_decisions AS d WHERE d.round_id = r.id AND d.action = 'accept')
    RETURNING f.*
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_retain_history(p_round uuid, p_references jsonb)
RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.active_learning_references (id, round_id, dataset_version_id, evaluation_id, source_digest)
    SELECT gen_random_uuid(), p_round,
        CASE r.kind WHEN 'dataset' THEN r.id END, CASE r.kind WHEN 'evaluation' THEN r.id END, r.digest
    FROM jsonb_to_recordset(p_references) AS r(kind text, id uuid, digest text)
    ON CONFLICT DO NOTHING
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_history_reference(
    p_project uuid, p_dataset uuid, p_items uuid[], p_tasks uuid[], p_version uuid, p_evaluation uuid
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN CASE
    WHEN p_project IS NOT NULL THEN EXISTS (SELECT 1 FROM public.active_learning_rounds AS r WHERE r.project_id = p_project)
    WHEN p_version IS NOT NULL THEN EXISTS (SELECT 1 FROM public.active_learning_references AS r WHERE r.dataset_version_id = p_version)
    WHEN p_evaluation IS NOT NULL THEN EXISTS (SELECT 1 FROM public.active_learning_references AS r WHERE r.evaluation_id = p_evaluation)
    WHEN p_dataset IS NOT NULL THEN EXISTS (
        SELECT 1 FROM yoao_assets_v1.dataset_item_references(p_dataset, NULL::uuid) AS i
        JOIN public.active_learning_pool_members AS m ON m.dataset_item_id = i.item_id
    )
    WHEN p_items IS NOT NULL THEN EXISTS (SELECT 1 FROM public.active_learning_pool_members AS m WHERE m.dataset_item_id = ANY(p_items))
    ELSE EXISTS (SELECT 1 FROM public.active_learning_pool_members AS m WHERE m.task_id = ANY(p_tasks))
END;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_dataset_history_budget(p_project uuid)
RETURNS TABLE(source_count bigint, byte_count bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT count(*), COALESCE(sum(COALESCE(octet_length(v.snapshot::text), 0)
        + COALESCE(octet_length(v.manifest::text), 0)), 0)
    FROM public.training_dataset_versions AS v WHERE v.project_id = p_project
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_dataset_history(p_project uuid, p_share boolean)
RETURNS TABLE(id uuid, snapshot jsonb, snapshot_digest text, manifest jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_share THEN
        RETURN QUERY SELECT v.id, v.snapshot, v.snapshot_digest::text, v.manifest
            FROM public.training_dataset_versions AS v WHERE v.project_id = p_project ORDER BY v.id LIMIT 257 FOR SHARE;
    ELSE
        RETURN QUERY SELECT v.id, v.snapshot, v.snapshot_digest::text, v.manifest
            FROM public.training_dataset_versions AS v WHERE v.project_id = p_project ORDER BY v.id LIMIT 257;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.active_learning_original_history()
RETURNS TABLE(id uuid, spec jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.id, r.spec FROM public.active_learning_rounds AS r
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.active_learning_original_decisions()
RETURNS TABLE(id uuid, round_id uuid, snapshot jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT d.id, d.round_id, d.snapshot FROM public.active_learning_decisions AS d
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.active_learning_pool_item_count(p_items uuid[])
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN (SELECT count(*) FROM public.active_learning_pool_members AS m WHERE m.dataset_item_id = ANY(p_items));
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.active_learning_candidate_item_count(p_items uuid[])
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN (SELECT count(*) FROM public.active_learning_candidates AS c WHERE c.dataset_item_id = ANY(p_items));
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_adoption_attempts(p_round uuid)
RETURNS SETOF public.active_learning_attempts
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT a.* FROM public.active_learning_attempts AS a
    WHERE a.round_id = p_round ORDER BY a.id LIMIT 10001 FOR SHARE
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_adoption_candidates(p_round uuid)
RETURNS SETOF public.active_learning_candidates
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT c.* FROM public.active_learning_candidates AS c
    WHERE c.round_id = p_round ORDER BY c.ordinal LIMIT 33 FOR SHARE
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_pin_storage(p_project uuid, p_round uuid, p_generation integer, p_storage jsonb)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.active_learning_rounds SET storage_ref = p_storage
    WHERE id = p_round AND project_id = p_project AND generation = p_generation AND storage_ref IS NULL
      AND status IN ('succeeded','failed','cancelled') AND p_storage IS NOT NULL;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE VIEW yoao_learning_v1.selection_storage_owners WITH (security_barrier = true) AS
SELECT r.id, r.project_id, r.generation, r.current_attempt_id, r.status, r.storage_ref
FROM public.active_learning_rounds AS r;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_policy(p_project uuid, p_task text, p_id uuid, p_lock boolean)
RETURNS SETOF public.learning_policies
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT p.* FROM public.learning_policies AS p
        WHERE (p_id IS NOT NULL AND p.id = p_id OR p.project_id = p_project AND p.task = p_task) FOR UPDATE;
    ELSE
        RETURN QUERY SELECT p.* FROM public.learning_policies AS p
        WHERE (p_id IS NOT NULL AND p.id = p_id OR p.project_id = p_project AND p.task = p_task);
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_save_policy(
    p_project uuid, p_task text, p_revision integer, p_actor uuid, p_enabled boolean,
    p_initialization uuid, p_config jsonb, p_authorization jsonb,
    p_checked timestamptz, p_next timestamptz, p_message text
) RETURNS SETOF public.learning_policies
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_revision = 0 THEN
        RETURN QUERY INSERT INTO public.learning_policies AS p (
            id, project_id, task, revision, creator_id, enabled, initialization_model_id,
            config, authorization, baseline, last_checked_at, next_check_at, state, message
        ) VALUES (gen_random_uuid(), p_project, p_task, 1, p_actor, p_enabled, p_initialization,
            p_config, p_authorization, '{}'::jsonb, p_checked, p_next,
            CASE WHEN p_enabled THEN 'waiting' ELSE 'disabled' END, p_message
        ) ON CONFLICT (project_id, task) DO NOTHING RETURNING p.*;
    ELSE
        RETURN QUERY UPDATE public.learning_policies AS p SET
            revision = p.revision + 1, creator_id = p_actor, enabled = p_enabled,
            initialization_model_id = p_initialization, config = p_config, authorization = p_authorization,
            blocked_train_digest = NULL, last_snapshot_digest = NULL,
            last_checked_at = p_checked, next_check_at = p_next,
            state = CASE WHEN p_enabled THEN 'waiting' ELSE 'disabled' END, message = p_message
        WHERE p.project_id = p_project AND p.task = p_task AND p.revision = p_revision RETURNING p.*;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_disable_policy(
    p_policy uuid, p_revision integer, p_checked timestamptz, p_message text
) RETURNS SETOF public.learning_policies
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.learning_policies AS p SET revision = p.revision + 1, enabled = false,
        last_checked_at = p_checked, state = 'disabled', message = p_message
    WHERE p.id = p_policy AND p.revision = p_revision RETURNING p.*
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_delete_policy(p_policy uuid, p_revision integer)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    DELETE FROM public.learning_policies AS p WHERE p.id = p_policy AND p.revision = p_revision AND NOT p.enabled
        AND NOT EXISTS (SELECT 1 FROM public.learning_triggers AS t WHERE t.policy_id = p.id AND (
            t.status IN ('building','queued','running')
            OR EXISTS (SELECT 1 FROM public.training_runs AS r WHERE r.auto_trigger_id = t.id)
        ));
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_due_policies(p_at timestamptz)
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p.id FROM public.learning_policies AS p
    WHERE (p.enabled AND (p.next_check_at IS NULL OR p.next_check_at <= p_at))
       OR EXISTS (SELECT 1 FROM public.learning_triggers AS t
                  WHERE t.policy_id = p.id AND t.status IN ('building','queued','running'))
    ORDER BY p.last_checked_at NULLS FIRST, p.id LIMIT 100
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_policy_check_state(
    p_policy uuid, p_revision integer, p_enabled boolean, p_state text, p_message text,
    p_checked timestamptz, p_next timestamptz, p_started timestamptz,
    p_snapshot_digest text, p_blocked_digest text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.learning_policies AS p SET
        enabled = p.enabled AND p_enabled, state = p_state, message = p_message,
        last_checked_at = p_checked, next_check_at = p_next, last_started_at = p_started,
        last_snapshot_digest = p_snapshot_digest, blocked_train_digest = p_blocked_digest
    WHERE p.id = p_policy AND p.revision = p_revision;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_advance_baseline(
    p_policy uuid, p_revision integer, p_trigger uuid, p_dataset uuid, p_baseline jsonb
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.learning_policies AS p SET baseline_dataset_id = p_dataset, baseline = p_baseline
    WHERE p.id = p_policy AND p.revision = p_revision AND EXISTS (
        SELECT 1 FROM public.learning_triggers AS t JOIN public.training_runs AS r ON r.auto_trigger_id = t.id
        WHERE t.id = p_trigger AND t.policy_id = p.id AND t.dataset_id = p_dataset
          AND r.dataset_id = t.dataset_id AND r.project_id = p.project_id AND r.status = 'succeeded'
          AND t.status IN ('building','queued','running')
    );
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_trigger(
    p_identity uuid, p_policy uuid, p_mode text
) RETURNS SETOF public.learning_triggers
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_mode = 'active_lock' THEN
        RETURN QUERY SELECT t.* FROM public.learning_triggers AS t
        WHERE t.policy_id = p_policy AND t.status IN ('building','queued','running') FOR UPDATE;
    ELSIF p_mode = 'building' THEN
        RETURN QUERY SELECT t.* FROM public.learning_triggers AS t
        WHERE t.policy_id = p_policy AND t.status = 'building';
    ELSIF p_mode = 'read' THEN
        RETURN QUERY SELECT t.* FROM public.learning_triggers AS t WHERE t.id = p_identity;
    ELSE
        RAISE EXCEPTION 'Invalid selection trigger read' USING ERRCODE = '22023';
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_other_active_trigger(p_project uuid, p_policy uuid, p_trigger uuid)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN EXISTS (SELECT 1 FROM public.learning_triggers AS t
    WHERE t.project_id = p_project AND t.status IN ('building','queued','running')
      AND (p_policy IS NULL OR t.policy_id <> p_policy) AND (p_trigger IS NULL OR t.id <> p_trigger));
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_trigger_duplicate(
    p_policy uuid, p_revision integer, p_trigger uuid, p_digests text[]
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN EXISTS (SELECT 1 FROM public.learning_triggers AS t
    WHERE t.policy_id = p_policy AND t.policy_revision = p_revision AND t.id <> p_trigger
      AND t.candidate_digest = ANY(p_digests));
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_trigger_history(
    p_project uuid, p_task text, p_limit integer, p_offset integer
) RETURNS SETOF public.learning_triggers
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT t.* FROM public.learning_triggers AS t JOIN public.learning_policies AS p ON p.id = t.policy_id
    WHERE t.project_id = p_project AND p.project_id = p_project AND p.task = p_task
    ORDER BY t.created_at DESC, t.id DESC LIMIT LEAST(p_limit, 100) OFFSET p_offset
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_trigger_count(p_project uuid, p_task text)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN (SELECT count(*) FROM public.learning_triggers AS t JOIN public.learning_policies AS p ON p.id = t.policy_id
    WHERE t.project_id = p_project AND p.project_id = p_project AND p.task = p_task);
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_build_trigger(
    p_policy uuid, p_revision integer, p_dataset uuid, p_digest text
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    INSERT INTO public.learning_triggers (
        id, policy_id, project_id, policy_revision, dataset_id, status, source_digest, changed_samples
    ) SELECT gen_random_uuid(), p.id, p.project_id, p.revision, p_dataset, 'building', p_digest, 0
    FROM public.learning_policies AS p JOIN public.training_dataset_versions AS v ON v.id = p_dataset
    WHERE p.id = p_policy AND p.enabled AND p.revision = p_revision
      AND v.project_id = p.project_id AND v.snapshot_digest = p_digest;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_trigger_progress(
    p_trigger uuid, p_revision integer, p_status text, p_message text, p_completed timestamptz,
    p_candidate text, p_train text, p_validation text, p_changed integer
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.learning_triggers AS t SET status = p_status, message = p_message,
        completed_at = p_completed, candidate_digest = p_candidate, train_digest = p_train,
        validation_digest = p_validation, changed_samples = p_changed
    WHERE t.id = p_trigger AND t.policy_revision = p_revision AND t.status IN ('building','queued','running')
      AND p_status IN ('building','queued','running','succeeded','failed','cancelled','skipped')
      AND (p_status <> 'succeeded' OR EXISTS (
          SELECT 1 FROM public.training_runs AS r WHERE r.auto_trigger_id = t.id
            AND r.dataset_id = t.dataset_id AND r.project_id = t.project_id AND r.status = 'succeeded'
      ));
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_requeue_trigger(p_trigger uuid)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.learning_triggers AS t SET status = 'queued', completed_at = NULL
    WHERE t.id = p_trigger AND EXISTS (
        SELECT 1 FROM public.training_runs AS r WHERE r.auto_trigger_id = t.id
          AND r.project_id = t.project_id AND r.status = 'queued'
          AND r.authorization->'manual_resume' = 'true'::jsonb
    );
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_policy_claim(p_trigger uuid, p_project uuid, p_dataset uuid)
RETURNS TABLE(task text, enabled boolean, revision integer, policy_revision integer, config jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT p.task::text, p.enabled, p.revision, t.policy_revision, p.config
    FROM public.learning_triggers AS t JOIN public.learning_policies AS p ON p.id = t.policy_id
    WHERE t.id = p_trigger AND p.project_id = p_project AND t.project_id = p_project
      AND t.dataset_id = p_dataset AND t.status IN ('queued','running')
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_dataset_references(p_version uuid)
RETURNS TABLE(has_study boolean, has_policy boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT 1 FROM public.training_studies AS s WHERE s.dataset_id = p_version),
           EXISTS (SELECT 1 FROM public.learning_triggers AS t WHERE t.dataset_id = p_version)
            OR EXISTS (SELECT 1 FROM public.learning_policies AS p
                WHERE p.baseline_dataset_id = p_version OR p.config->>'parent_id' = p_version::text)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_study(
    p_project uuid, p_study uuid, p_request uuid, p_lock boolean
) RETURNS SETOF public.training_studies
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT s.* FROM public.training_studies AS s
        WHERE s.id = p_study AND s.project_id = p_project FOR NO KEY UPDATE;
    ELSE
        RETURN QUERY SELECT s.* FROM public.training_studies AS s WHERE (
            s.id = p_study AND (p_project IS NULL OR s.project_id = p_project)
            OR s.project_id = p_project AND s.request_id = p_request
        );
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_studies(p_project uuid, p_limit integer, p_offset integer)
RETURNS TABLE(id uuid, name text, status text, scheduling boolean, message text, created_at timestamptz, trial_count integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT s.id, s.name::text, s.status::text, s.scheduling, s.message::text, s.created_at,
           jsonb_array_length(s.plan->'trials')
    FROM public.training_studies AS s WHERE s.project_id = p_project
    ORDER BY s.created_at DESC, s.id DESC LIMIT LEAST(p_limit, 100) OFFSET p_offset
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_study_count(p_project uuid)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN (SELECT count(*) FROM public.training_studies AS s WHERE s.project_id = p_project);
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_due_studies()
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT s.id FROM public.training_studies AS s WHERE s.scheduling OR s.status IN ('active','cancelling')
    ORDER BY s.last_checked_at NULLS FIRST, s.id LIMIT 100
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_study_claim(p_study uuid, p_project uuid, p_dataset uuid)
RETURNS TABLE(scheduling boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT s.scheduling FROM public.training_studies AS s
    WHERE s.id = p_study AND s.project_id = p_project AND s.dataset_id = p_dataset
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_create_study(
    p_study uuid, p_project uuid, p_actor uuid, p_dataset uuid, p_initialization uuid,
    p_request uuid, p_request_digest text, p_name text, p_plan jsonb, p_plan_digest text,
    p_origin jsonb, p_authorization jsonb
) RETURNS SETOF public.training_studies
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_plan->>'study_id' IS DISTINCT FROM p_study::text
       OR NOT COALESCE(jsonb_array_length(p_plan->'trials') BETWEEN 1 AND 20, false)
       OR jsonb_array_length(p_plan->'trials') > (p_plan#>>'{budgets,max_trials}')::int THEN
        RAISE EXCEPTION 'Invalid frozen study plan' USING ERRCODE = '22023';
    END IF;
    RETURN QUERY INSERT INTO public.training_studies AS s (
        id, project_id, creator_id, dataset_id, initialization_model_id, request_id, request_digest,
        name, plan, plan_digest, origin, authorization, status, scheduling
    ) VALUES (
        p_study, p_project, p_actor, p_dataset, p_initialization, p_request, p_request_digest,
        p_name, p_plan, p_plan_digest, p_origin, p_authorization, 'active', true
    ) RETURNING s.*;
    INSERT INTO public.training_trials (id, study_id, ordinal, config, config_digest, "values")
    SELECT t.id, p_study, t.ordinal, t.config, t.config_digest, t."values"
    FROM jsonb_to_recordset(p_plan->'trials') AS t(id uuid, ordinal integer, config jsonb, config_digest text, "values" jsonb);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_trials(p_study uuid, p_next boolean)
RETURNS SETOF public.training_trials
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT t.* FROM public.training_trials AS t
    WHERE t.study_id = p_study AND (NOT p_next OR t.run_id IS NULL) ORDER BY t.ordinal
    LIMIT CASE WHEN p_next THEN 1 ELSE 20 END
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_bind_trial(p_study uuid, p_trial uuid, p_run uuid)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.training_trials AS t SET run_id = p_run
    WHERE t.study_id = p_study AND t.id = p_trial AND t.run_id IS NULL
      AND EXISTS (SELECT 1 FROM public.training_runs AS r WHERE r.id = p_run AND r.study_id = p_study
          AND r.request_id = t.id AND r.config_digest = t.config_digest);
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_study_progress(
    p_study uuid, p_scheduling boolean, p_status text, p_message text,
    p_checked timestamptz, p_completed timestamptz
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.training_studies AS s SET scheduling = s.scheduling AND p_scheduling,
        status = p_status, message = p_message, last_checked_at = p_checked, completed_at = p_completed
    WHERE s.id = p_study AND p_status IN ('active','cancelling','cancelled','blocked','completed','budget_exhausted');
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_resume_study(p_study uuid, p_run uuid)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.training_studies AS s SET scheduling = s.scheduling OR s.status = 'completed',
        status = 'active', completed_at = NULL
    WHERE s.id = p_study AND EXISTS (
        SELECT 1 FROM public.training_runs AS r WHERE r.id = p_run AND r.study_id = s.id
          AND r.status = 'queued' AND r.authorization->'study_manual_resume' = 'true'::jsonb
    );
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_recipe(p_project uuid, p_recipe uuid, p_lock boolean)
RETURNS SETOF public.training_recipes
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_lock THEN
        RETURN QUERY SELECT r.* FROM public.training_recipes AS r
        WHERE r.project_id = p_project AND r.id = p_recipe FOR UPDATE;
    ELSE
        RETURN QUERY SELECT r.* FROM public.training_recipes AS r WHERE r.project_id = p_project AND r.id = p_recipe;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_recipes(p_project uuid, p_archived boolean, p_limit integer, p_offset integer)
RETURNS SETOF public.training_recipes
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT r.* FROM public.training_recipes AS r WHERE r.project_id = p_project AND (p_archived OR NOT r.archived)
    ORDER BY r.created_at DESC, r.id DESC LIMIT LEAST(p_limit, 100) OFFSET p_offset
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_recipe_count(p_project uuid, p_archived boolean)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN (SELECT count(*) FROM public.training_recipes AS r
    WHERE r.project_id = p_project AND (p_archived OR NOT r.archived));
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_recipe_revision(p_recipe uuid, p_revision uuid, p_head integer)
RETURNS SETOF public.training_recipe_revisions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT v.* FROM public.training_recipe_revisions AS v
    WHERE v.recipe_id = p_recipe AND (v.id = p_revision OR p_revision IS NULL AND v.ordinal = p_head)
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_recipe_request(p_project uuid, p_request uuid)
RETURNS SETOF public.training_recipe_revisions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT v.* FROM public.training_recipe_revisions AS v WHERE v.project_id = p_project AND v.request_id = p_request
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_recipe_revisions(p_recipe uuid, p_limit integer, p_offset integer)
RETURNS SETOF public.training_recipe_revisions
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT v.* FROM public.training_recipe_revisions AS v WHERE v.recipe_id = p_recipe
    ORDER BY v.ordinal DESC LIMIT LEAST(p_limit, 100) OFFSET p_offset
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_recipe_revision_count(p_recipe uuid)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
RETURN (SELECT count(*) FROM public.training_recipe_revisions AS v WHERE v.recipe_id = p_recipe);
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_append_recipe(
    p_project uuid, p_recipe uuid, p_expected integer, p_actor uuid,
    p_name text, p_description text, p_snapshot jsonb, p_digest text, p_origin jsonb,
    p_initialization uuid, p_request uuid, p_request_digest text
) RETURNS SETOF public.training_recipe_revisions
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    recipe_identity uuid;
BEGIN
    IF p_recipe IS NULL AND p_expected = 0 THEN
        INSERT INTO public.training_recipes (id, project_id, head_revision, archived)
        VALUES (gen_random_uuid(), p_project, 1, false) RETURNING id INTO recipe_identity;
    ELSE
        UPDATE public.training_recipes AS r SET head_revision = p_expected + 1
        WHERE r.id = p_recipe AND r.project_id = p_project AND r.head_revision = p_expected AND NOT r.archived
        RETURNING r.id INTO recipe_identity;
    END IF;
    IF NOT FOUND THEN RETURN; END IF;
    RETURN QUERY INSERT INTO public.training_recipe_revisions AS v (
        id, recipe_id, project_id, creator_id, ordinal, name, description, snapshot, snapshot_digest,
        origin, initialization_model_id, request_id, request_digest
    ) VALUES (
        gen_random_uuid(), recipe_identity, p_project, p_actor, p_expected + 1, p_name, p_description, p_snapshot, p_digest,
        p_origin, p_initialization, p_request, p_request_digest
    ) RETURNING v.*;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_learning_v1.selection_archive_recipe(p_project uuid, p_recipe uuid, p_head integer)
RETURNS SETOF public.training_recipes
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.training_recipes AS r SET archived = true
    WHERE r.id = p_recipe AND r.project_id = p_project AND r.head_revision = p_head RETURNING r.*
$function$;
-- aap-statement-break
CREATE VIEW yoao_learning_v1.recipe_lineage WITH (security_barrier = true) AS
SELECT r.id, r.project_id, r.created_at, r.archived FROM public.training_recipes AS r;
-- aap-statement-break
CREATE VIEW yoao_learning_v1.recipe_revision_lineage WITH (security_barrier = true) AS
SELECT v.id, v.project_id, v.created_at, v.recipe_id, v.name, v.ordinal,
    v.snapshot_digest, v.initialization_model_id,
    substr(CASE WHEN jsonb_typeof(v.origin->'kind') = 'string' THEN v.origin->>'kind' END, 1, 256) AS origin_kind,
    substr(CASE WHEN jsonb_typeof(v.origin->'run_id') = 'string' THEN v.origin->>'run_id' END, 1, 256) AS origin_run_id,
    substr(CASE WHEN jsonb_typeof(v.origin->'attempt_id') = 'string' THEN v.origin->>'attempt_id' END, 1, 256) AS origin_attempt_id,
    substr(CASE WHEN jsonb_typeof(v.origin->'recipe_id') = 'string' THEN v.origin->>'recipe_id' END, 1, 256) AS origin_recipe_id,
    substr(CASE WHEN jsonb_typeof(v.origin->'revision_id') = 'string' THEN v.origin->>'revision_id' END, 1, 256) AS origin_revision_id,
    substr(CASE WHEN jsonb_typeof(v.origin->'digest') = 'string' THEN v.origin->>'digest' END, 1, 256) AS origin_digest,
    substr(CASE WHEN jsonb_typeof(v.snapshot#>'{initialization,kind}') = 'string' THEN v.snapshot#>>'{initialization,kind}' END, 1, 256) AS initialization_kind,
    substr(CASE WHEN jsonb_typeof(v.snapshot#>'{initialization,project_id}') = 'string' THEN v.snapshot#>>'{initialization,project_id}' END, 1, 256) AS initialization_project_id,
    substr(CASE WHEN jsonb_typeof(v.snapshot#>'{initialization,model_version_id}') = 'string' THEN v.snapshot#>>'{initialization,model_version_id}' END, 1, 256) AS initialization_model_version_id,
    substr(CASE WHEN jsonb_typeof(v.snapshot#>'{initialization,run_id}') = 'string' THEN v.snapshot#>>'{initialization,run_id}' END, 1, 256) AS initialization_run_id,
    substr(CASE WHEN jsonb_typeof(v.snapshot#>'{initialization,attempt_id}') = 'string' THEN v.snapshot#>>'{initialization,attempt_id}' END, 1, 256) AS initialization_attempt_id,
    substr(CASE WHEN jsonb_typeof(v.snapshot#>'{initialization,source_digest}') = 'string' THEN v.snapshot#>>'{initialization,source_digest}' END, 1, 256) AS initialization_source_digest,
    substr(CASE WHEN jsonb_typeof(v.snapshot#>'{initialization,policy_digest}') = 'string' THEN v.snapshot#>>'{initialization,policy_digest}' END, 1, 256) AS initialization_policy_digest
FROM public.training_recipe_revisions AS v;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_learning_v1.selection_create_round(uuid, uuid, uuid, uuid, uuid, uuid, text, text, jsonb, text, jsonb, jsonb, uuid[]),
    yoao_learning_v1.selection_round(uuid, uuid, text),
    yoao_learning_v1.selection_round_request(uuid, uuid),
    yoao_learning_v1.selection_round_for_job(uuid),
    yoao_learning_v1.selection_rounds(uuid, text, integer, integer),
    yoao_learning_v1.selection_round_count(uuid, text),
    yoao_learning_v1.selection_attempt(uuid, uuid),
    yoao_learning_v1.selection_recent_attempts(uuid),
    yoao_learning_v1.selection_attempt_ids(uuid),
    yoao_learning_v1.selection_candidates(uuid, integer),
    yoao_learning_v1.selection_due_rounds(timestamptz),
    yoao_learning_v1.selection_open_attempt(uuid, integer, uuid, uuid, uuid, timestamptz),
    yoao_learning_v1.selection_claim_attempt(uuid, integer, uuid, timestamptz),
    yoao_learning_v1.selection_touch_attempt(uuid, integer, uuid, text, timestamptz),
    yoao_learning_v1.selection_debit_round(uuid, integer, uuid, timestamptz),
    yoao_learning_v1.selection_candidate_request(uuid, integer, uuid, integer, jsonb, jsonb),
    yoao_learning_v1.selection_candidate_evidence(uuid, integer, uuid, integer, jsonb),
    yoao_learning_v1.selection_settle_attempt(uuid, integer, uuid, text, text, text, jsonb, timestamptz),
    yoao_learning_v1.selection_release_cancelled_claim(uuid, integer, uuid),
    yoao_learning_v1.selection_decision_request(uuid, uuid),
    yoao_learning_v1.selection_decisions(uuid, integer, integer),
    yoao_learning_v1.selection_accepted_decisions(uuid),
    yoao_learning_v1.selection_record_decision(uuid, uuid, uuid, uuid, text, text, uuid, integer[], jsonb, text),
    yoao_learning_v1.selection_feedback_request(uuid, uuid),
    yoao_learning_v1.selection_feedback(uuid, integer, integer),
    yoao_learning_v1.selection_receipt_count(uuid, boolean),
    yoao_learning_v1.selection_record_feedback(uuid, uuid, uuid, text, jsonb, text),
    yoao_learning_v1.selection_retain_history(uuid, jsonb),
    yoao_learning_v1.selection_history_reference(uuid, uuid, uuid[], uuid[], uuid, uuid),
    yoao_learning_v1.selection_dataset_history_budget(uuid),
    yoao_learning_v1.selection_dataset_history(uuid, boolean),
    yoao_learning_v1.active_learning_original_history(),
    yoao_learning_v1.active_learning_original_decisions(),
    yoao_learning_v1.active_learning_pool_item_count(uuid[]),
    yoao_learning_v1.active_learning_candidate_item_count(uuid[]),
    yoao_learning_v1.selection_adoption_attempts(uuid),
    yoao_learning_v1.selection_adoption_candidates(uuid),
    yoao_learning_v1.selection_pin_storage(uuid, uuid, integer, jsonb),
    yoao_learning_v1.selection_policy(uuid, text, uuid, boolean),
    yoao_learning_v1.selection_save_policy(uuid, text, integer, uuid, boolean, uuid, jsonb, jsonb, timestamptz, timestamptz, text),
    yoao_learning_v1.selection_disable_policy(uuid, integer, timestamptz, text),
    yoao_learning_v1.selection_delete_policy(uuid, integer),
    yoao_learning_v1.selection_due_policies(timestamptz),
    yoao_learning_v1.selection_policy_check_state(uuid, integer, boolean, text, text, timestamptz, timestamptz, timestamptz, text, text),
    yoao_learning_v1.selection_advance_baseline(uuid, integer, uuid, uuid, jsonb),
    yoao_learning_v1.selection_trigger(uuid, uuid, text),
    yoao_learning_v1.selection_other_active_trigger(uuid, uuid, uuid),
    yoao_learning_v1.selection_trigger_duplicate(uuid, integer, uuid, text[]),
    yoao_learning_v1.selection_trigger_history(uuid, text, integer, integer),
    yoao_learning_v1.selection_trigger_count(uuid, text),
    yoao_learning_v1.selection_build_trigger(uuid, integer, uuid, text),
    yoao_learning_v1.selection_trigger_progress(uuid, integer, text, text, timestamptz, text, text, text, integer),
    yoao_learning_v1.selection_requeue_trigger(uuid),
    yoao_learning_v1.selection_policy_claim(uuid, uuid, uuid),
    yoao_learning_v1.selection_dataset_references(uuid),
    yoao_learning_v1.selection_study(uuid, uuid, uuid, boolean),
    yoao_learning_v1.selection_studies(uuid, integer, integer),
    yoao_learning_v1.selection_study_count(uuid),
    yoao_learning_v1.selection_due_studies(),
    yoao_learning_v1.selection_study_claim(uuid, uuid, uuid),
    yoao_learning_v1.selection_create_study(uuid, uuid, uuid, uuid, uuid, uuid, text, text, jsonb, text, jsonb, jsonb),
    yoao_learning_v1.selection_trials(uuid, boolean),
    yoao_learning_v1.selection_bind_trial(uuid, uuid, uuid),
    yoao_learning_v1.selection_study_progress(uuid, boolean, text, text, timestamptz, timestamptz),
    yoao_learning_v1.selection_resume_study(uuid, uuid),
    yoao_learning_v1.selection_recipe(uuid, uuid, boolean),
    yoao_learning_v1.selection_recipes(uuid, boolean, integer, integer),
    yoao_learning_v1.selection_recipe_count(uuid, boolean),
    yoao_learning_v1.selection_recipe_revision(uuid, uuid, integer),
    yoao_learning_v1.selection_recipe_request(uuid, uuid),
    yoao_learning_v1.selection_recipe_revisions(uuid, integer, integer),
    yoao_learning_v1.selection_recipe_revision_count(uuid),
    yoao_learning_v1.selection_append_recipe(uuid, uuid, integer, uuid, text, text, jsonb, text, jsonb, uuid, uuid, text),
    yoao_learning_v1.selection_archive_recipe(uuid, uuid, integer)
    FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON TABLE yoao_learning_v1.selection_storage_owners,
    yoao_learning_v1.recipe_lineage,
    yoao_learning_v1.recipe_revision_lineage FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON TABLE public.active_learning_rounds,
    public.active_learning_attempts,
    public.active_learning_pool_members,
    public.active_learning_candidates,
    public.active_learning_decisions,
    public.active_learning_references,
    public.active_learning_feedback,
    public.learning_policies,
    public.learning_triggers,
    public.training_studies,
    public.training_trials,
    public.training_recipes,
    public.training_recipe_revisions FROM PUBLIC;
