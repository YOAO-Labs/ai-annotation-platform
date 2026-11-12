-- A typed scope is required before a Dataset Job can be treated as unrelated.
CREATE OR REPLACE FUNCTION aap_jobs_v1.fixture_dataset_job_scope(
    p_id uuid, p_kind text, p_payload jsonb, p_project_id uuid, p_user_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
IMMUTABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset text;
BEGIN
    IF pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN
        RETURN pg_catalog.jsonb_build_object('kind', 'unknown');
    END IF;
    IF pg_catalog.jsonb_typeof(p_payload -> 'dataset_id') = 'string'
       AND p_payload ->> 'dataset_id' ~* '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN
        v_dataset := pg_catalog.lower(p_payload ->> 'dataset_id');
    END IF;
    -- Reclamation reads only an exact retired original through its own durable
    -- Attempt, stopped-location permit and reference fence, not a live Dataset.
    -- Its retained Job actor may be cleared by the account ON DELETE SET NULL FK.
    IF p_kind = 'managed_original_reclaim'
       AND p_project_id IS NULL
       AND p_payload - ARRAY['retirement_id', 'reclaim_id'] = '{}'::jsonb
       AND pg_catalog.jsonb_typeof(p_payload -> 'retirement_id') = 'string'
       AND p_payload ->> 'retirement_id' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
       AND pg_catalog.jsonb_typeof(p_payload -> 'reclaim_id') = 'string'
       AND p_payload ->> 'reclaim_id' ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN
        RETURN pg_catalog.jsonb_build_object('kind', 'unrelated');
    END IF;
    -- Audit archive jobs only read audit partitions and write audit archive
    -- outputs; their payload never selects or writes dataset objects.
    IF p_kind = 'audit_archive' THEN
        RETURN pg_catalog.jsonb_build_object('kind', 'unrelated');
    END IF;
    -- Labeling validates the same Jobs operation and exact dataset, project,
    -- actor and association before each task-creation phase. Legacy payloads
    -- and rows whose immutable owner binding does not match remain unknown.
    IF p_kind = 'create_tasks' THEN
        IF v_dataset IS NULL
           OR NOT aap_jobs_v1._task_creation_job_payload_valid(p_id, p_payload)
           OR p_project_id IS NULL
           OR p_project_id::text IS DISTINCT FROM p_payload ->> 'project_id'
           OR (p_user_id IS NOT NULL AND p_user_id::text IS DISTINCT FROM p_payload ->> 'actor_id') THEN
            RETURN pg_catalog.jsonb_build_object('kind', 'unknown');
        END IF;
        RETURN pg_catalog.jsonb_build_object('kind', 'dataset', 'dataset_id', v_dataset);
    END IF;
    -- These workers obtain the Dataset from this exact Jobs payload. Other
    -- families use separate owner records and remain unknown until that owner
    -- proves their scope; a similarly named payload field is not sufficient.
    IF p_kind IN ('dataset_import', 'media_backfill') THEN
        IF v_dataset IS NULL THEN
            RETURN pg_catalog.jsonb_build_object('kind', 'unknown');
        END IF;
        RETURN pg_catalog.jsonb_build_object('kind', 'dataset', 'dataset_id', v_dataset);
    END IF;
    RETURN pg_catalog.jsonb_build_object('kind', 'unknown');
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.fixture_dataset_job_scope(uuid,text,jsonb,uuid,uuid) FROM PUBLIC;
-- aap-statement-break
CREATE OR REPLACE FUNCTION aap_jobs_v1.guard_fixture_dataset_jobs(p_dataset_ids uuid[])
RETURNS void
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 5) <> '_test'
       AND pg_catalog.right(pg_catalog.lower(pg_catalog.current_database()), 4) <> '_e2e' THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Jobs fixture cleanup requires a test database';
    END IF;
    IF p_dataset_ids IS NULL OR pg_catalog.array_position(p_dataset_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid Jobs fixture dataset scope';
    END IF;
    IF pg_catalog.cardinality(p_dataset_ids) = 0 THEN
        RETURN;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.async_jobs AS job
        CROSS JOIN LATERAL (
            SELECT aap_jobs_v1.fixture_dataset_job_scope(
                job.id, job.kind, job.payload, job.project_id, job.user_id
            ) AS value
        ) AS scope
        WHERE (scope.value ->> 'kind' = 'unknown'
           OR (scope.value ->> 'kind' = 'dataset' AND EXISTS (
                SELECT 1 FROM pg_catalog.unnest(p_dataset_ids) AS dataset(id)
                WHERE dataset.id::text = scope.value ->> 'dataset_id'
           )))
          AND NOT EXISTS (
              SELECT 1 FROM yoao_fixture_v1.fixture_presentation_jobs AS proof
              WHERE proof.job_id = job.id AND proof.project_id = job.project_id
                AND proof.kind = job.kind AND proof.state = 'admitted'
          )
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'fixture_dataset_job_unsettled';
    END IF;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_jobs_v1.guard_fixture_dataset_jobs(uuid[]) FROM PUBLIC;
