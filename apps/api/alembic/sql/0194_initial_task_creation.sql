-- Accepted initial task creation is durable across trusted worker restarts.
-- The caller owns every transaction boundary; these functions never commit.
CREATE TABLE public.task_creation_operations (
    id uuid PRIMARY KEY,
    actor_id uuid NOT NULL,
    dataset_id uuid NOT NULL,
    project_id uuid NOT NULL,
    association_id uuid NOT NULL,
    job_id uuid,
    mode text NOT NULL,
    chunk_size integer NOT NULL,
    request jsonb NOT NULL,
    manifest jsonb,
    total bigint,
    next_chunk integer NOT NULL DEFAULT 0,
    processed bigint NOT NULL DEFAULT 0,
    created bigint NOT NULL DEFAULT 0,
    state text NOT NULL DEFAULT 'accepted',
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT ck_task_creation_operations_mode
        CHECK (mode IN ('generic', 'pointcloud')),
    CONSTRAINT ck_task_creation_operations_chunk_size
        CHECK (chunk_size BETWEEN 1 AND 5000),
    CONSTRAINT ck_task_creation_operations_state
        CHECK (state IN ('accepted', 'running', 'completed')),
    CONSTRAINT ck_task_creation_operations_progress_nonnegative
        CHECK (next_chunk >= 0 AND processed >= 0 AND created >= 0),
    CONSTRAINT ck_task_creation_operations_total
        CHECK (total IS NULL OR total >= 0)
);
-- aap-statement-break
CREATE TABLE public.task_creation_chunks (
    operation_id uuid NOT NULL
        REFERENCES public.task_creation_operations(id) ON DELETE CASCADE,
    chunk_index integer NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT pk_task_creation_chunks PRIMARY KEY (operation_id, chunk_index),
    CONSTRAINT ck_task_creation_chunks_index CHECK (chunk_index >= 0)
);
-- aap-statement-break
COMMENT ON TABLE public.task_creation_operations IS
    'Labeling-owned accepted initial task plans and frozen source manifests.';
-- aap-statement-break
COMMENT ON TABLE public.task_creation_chunks IS
    'Labeling-owned frozen responses for committed initial task-creation chunks.';
-- aap-statement-break
REVOKE ALL ON TABLE public.task_creation_operations FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON TABLE public.task_creation_chunks FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.lock_task_creation_items(
    p_dataset_id uuid,
    p_item_ids uuid[]
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_dataset_id uuid;
    v_requested_count bigint;
    v_found_count bigint;
    v_items jsonb := '[]'::jsonb;
BEGIN
    IF p_dataset_id IS NULL
       OR (p_item_ids IS NOT NULL
           AND pg_catalog.array_position(p_item_ids, NULL) IS NOT NULL) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-items/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    v_dataset_id := aap_assets_v1.lock_dataset_reference(p_dataset_id);
    IF v_dataset_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-items/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'dataset_missing')
        );
    END IF;

    IF p_item_ids IS NULL THEN
        WITH locked_items AS MATERIALIZED (
            SELECT item.id, item.file_name, item.file_path, item.file_type
            FROM public.dataset_items AS item
            WHERE item.dataset_id = v_dataset_id
            ORDER BY item.id
            FOR KEY SHARE OF item
        )
        SELECT COALESCE(
            pg_catalog.jsonb_agg(
                pg_catalog.jsonb_build_object(
                    'id', item.id,
                    'file_name', item.file_name,
                    'file_path', item.file_path,
                    'file_type', item.file_type
                ) ORDER BY item.id
            ),
            '[]'::jsonb
        )
        INTO v_items
        FROM locked_items AS item;
    ELSE
        SELECT pg_catalog.count(DISTINCT requested.item_id)
        INTO v_requested_count
        FROM pg_catalog.unnest(p_item_ids) AS requested(item_id);

        WITH requested AS MATERIALIZED (
            SELECT DISTINCT input.item_id
            FROM pg_catalog.unnest(p_item_ids) AS input(item_id)
        ),
        locked_items AS MATERIALIZED (
            SELECT item.id, item.file_name, item.file_path, item.file_type
            FROM requested
            JOIN public.dataset_items AS item
              ON item.id = requested.item_id
             AND item.dataset_id = v_dataset_id
            ORDER BY item.id
            FOR KEY SHARE OF item
        )
        SELECT
            pg_catalog.count(item.id),
            COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'id', item.id,
                        'file_name', item.file_name,
                        'file_path', item.file_path,
                        'file_type', item.file_type
                    ) ORDER BY item.id
                ),
                '[]'::jsonb
            )
        INTO v_found_count, v_items
        FROM locked_items AS item;

        IF v_found_count <> v_requested_count THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation-items/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'source_changed')
            );
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation-items/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object('items', v_items)
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.lock_task_creation_items(uuid, uuid[]) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.accept_dataset_task_creation(
    p_operation_id uuid,
    p_actor_id uuid,
    p_dataset_id uuid,
    p_project_id uuid,
    p_association_id uuid,
    p_job_id uuid,
    p_mode text,
    p_chunk_size integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_manager jsonb;
    v_association_id uuid;
    v_job jsonb;
    v_job_data jsonb;
    v_request jsonb;
    v_existing_request jsonb;
    v_operation public.task_creation_operations%ROWTYPE;
    v_inserted integer;
    v_expected_mode text;
BEGIN
    IF p_operation_id IS NULL OR p_dataset_id IS NULL OR p_project_id IS NULL
       OR p_association_id IS NULL OR p_mode IS NULL OR p_chunk_size IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    IF p_chunk_size < 1 OR p_chunk_size > 5000
       OR p_mode NOT IN ('generic', 'pointcloud') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    -- Acceptance is the only stage that checks current actor authority.
    v_manager := aap_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF v_manager ->> 'schema' IS DISTINCT FROM 'aap-project-manager/v1' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'authority_contract_mismatch')
        );
    END IF;
    IF v_manager ->> 'ok' IS DISTINCT FROM 'true' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', v_manager -> 'error'
        );
    END IF;

    v_expected_mode := CASE
        WHEN v_manager #>> '{data,data_type}' = 'lidar' THEN 'pointcloud'
        ELSE 'generic'
    END;
    IF p_mode IS DISTINCT FROM v_expected_mode THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'mode_mismatch')
        );
    END IF;

    v_association_id := aap_assets_v1.lock_current_association(
        p_dataset_id,
        p_project_id
    );
    IF v_association_id IS NULL OR v_association_id <> p_association_id THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'association_stale')
        );
    END IF;

    IF p_job_id IS NOT NULL THEN
        v_job := aap_jobs_v1.lock_task_creation_job(
            p_job_id,
            p_operation_id,
            p_actor_id,
            p_dataset_id,
            p_project_id,
            p_association_id
        );
        IF v_job ->> 'schema' IS DISTINCT FROM 'aap-task-creation-job/v1' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'jobs_contract_mismatch')
            );
        END IF;
        IF v_job ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', v_job -> 'error'
            );
        END IF;
        v_job_data := v_job -> 'data';
    END IF;

    v_request := pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation-request/v1',
        'operation_id', p_operation_id,
        'actor_id', p_actor_id,
        'dataset_id', p_dataset_id,
        'project_id', p_project_id,
        'association_id', p_association_id,
        'job_id', p_job_id,
        'mode', p_mode,
        'chunk_size', p_chunk_size
    );

    SELECT operation.*
    INTO v_operation
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id
    FOR UPDATE;

    IF FOUND THEN
        v_existing_request := v_operation.request;
        IF v_existing_request IS DISTINCT FROM v_request THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'operation_conflict')
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation_id', v_operation.id,
                'actor_id', v_operation.actor_id,
                'dataset_id', v_operation.dataset_id,
                'project_id', v_operation.project_id,
                'association_id', v_operation.association_id,
                'job_id', v_operation.job_id,
                'mode', v_operation.mode,
                'chunk_size', v_operation.chunk_size,
                'state', v_operation.state,
                'total', v_operation.total,
                'next_chunk', v_operation.next_chunk,
                'processed', v_operation.processed,
                'created', v_operation.created
            )
        );
    END IF;

    IF p_job_id IS NOT NULL
       AND (
           v_job_data ->> 'cancel_requested' = 'true'
           OR v_job_data ->> 'status' NOT IN ('pending', 'running')
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'job_not_active')
        );
    END IF;

    INSERT INTO public.task_creation_operations (
        id,
        actor_id,
        dataset_id,
        project_id,
        association_id,
        job_id,
        mode,
        chunk_size,
        request
    )
    VALUES (
        p_operation_id,
        p_actor_id,
        p_dataset_id,
        p_project_id,
        p_association_id,
        p_job_id,
        p_mode,
        p_chunk_size,
        v_request
    )
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;

    IF v_inserted = 0 THEN
        SELECT operation.*
        INTO v_operation
        FROM public.task_creation_operations AS operation
        WHERE operation.id = p_operation_id
        FOR UPDATE;
        IF v_operation.request IS DISTINCT FROM v_request THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'operation_conflict')
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation_id', v_operation.id,
                'actor_id', v_operation.actor_id,
                'dataset_id', v_operation.dataset_id,
                'project_id', v_operation.project_id,
                'association_id', v_operation.association_id,
                'job_id', v_operation.job_id,
                'mode', v_operation.mode,
                'chunk_size', v_operation.chunk_size,
                'state', v_operation.state,
                'total', v_operation.total,
                'next_chunk', v_operation.next_chunk,
                'processed', v_operation.processed,
                'created', v_operation.created
            )
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'actor_id', p_actor_id,
            'dataset_id', p_dataset_id,
            'project_id', p_project_id,
            'association_id', p_association_id,
            'job_id', p_job_id,
            'mode', p_mode,
            'chunk_size', p_chunk_size,
            'state', 'accepted',
            'total', NULL,
            'next_chunk', 0,
            'processed', 0,
            'created', 0
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.accept_dataset_task_creation(
    uuid, uuid, uuid, uuid, uuid, uuid, text, integer
) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.prepare_dataset_task_creation(
    p_operation_id uuid,
    p_frames jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor_id uuid;
    v_dataset_id uuid;
    v_project_id uuid;
    v_association_id uuid;
    v_job_id uuid;
    v_mode text;
    v_chunk_size integer;
    v_operation public.task_creation_operations%ROWTYPE;
    v_source_ids uuid[];
    v_sources jsonb;
    v_source_items jsonb;
    v_manifest jsonb;
    v_total bigint;
    v_current_link_id uuid;
    v_job jsonb;
    v_job_data jsonb;
    v_frame jsonb;
    v_camera jsonb;
    v_sensor_name text;
BEGIN
    IF p_operation_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    SELECT
        operation.actor_id,
        operation.dataset_id,
        operation.project_id,
        operation.association_id,
        operation.job_id,
        operation.mode,
        operation.chunk_size
    INTO
        v_actor_id,
        v_dataset_id,
        v_project_id,
        v_association_id,
        v_job_id,
        v_mode,
        v_chunk_size
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    -- Execution trusts the accepted actor but fences dataset/link/job generations.
    PERFORM aap_assets_v1.lock_dataset_reference(v_dataset_id);
    v_current_link_id := aap_assets_v1.lock_current_association(
        v_dataset_id,
        v_project_id
    );
    IF v_current_link_id IS NULL OR v_current_link_id <> v_association_id THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'association_stale')
        );
    END IF;

    IF v_job_id IS NOT NULL THEN
        v_job := aap_jobs_v1.lock_task_creation_job(
            v_job_id,
            p_operation_id,
            v_actor_id,
            v_dataset_id,
            v_project_id,
            v_association_id
        );
        IF v_job ->> 'schema' IS DISTINCT FROM 'aap-task-creation-job/v1' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'jobs_contract_mismatch')
            );
        END IF;
        IF v_job ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', v_job -> 'error'
            );
        END IF;
        v_job_data := v_job -> 'data';
    END IF;

    SELECT operation.*
    INTO v_operation
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_operation.actor_id <> v_actor_id
       OR v_operation.dataset_id <> v_dataset_id
       OR v_operation.project_id <> v_project_id
       OR v_operation.association_id <> v_association_id
       OR v_operation.job_id IS DISTINCT FROM v_job_id
       OR v_operation.mode IS DISTINCT FROM v_mode
       OR v_operation.chunk_size IS DISTINCT FROM v_chunk_size THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    IF v_job_id IS NOT NULL
       AND (v_job_data ->> 'cancel_requested' = 'true'
            OR v_job_data ->> 'status' = 'cancelled') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'job_cancelled')
        );
    END IF;

    IF v_operation.manifest IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', true,
            'data', pg_catalog.jsonb_build_object(
                'operation_id', v_operation.id,
                'actor_id', v_operation.actor_id,
                'dataset_id', v_operation.dataset_id,
                'project_id', v_operation.project_id,
                'association_id', v_operation.association_id,
                'job_id', v_operation.job_id,
                'mode', v_operation.mode,
                'chunk_size', v_operation.chunk_size,
                'state', v_operation.state,
                'total', v_operation.total,
                'next_chunk', v_operation.next_chunk,
                'processed', v_operation.processed,
                'created', v_operation.created
            )
        );
    END IF;

    IF v_job_id IS NOT NULL
       AND v_job_data ->> 'status' NOT IN ('pending', 'running') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'job_not_active')
        );
    END IF;

    IF v_mode = 'generic' AND p_frames IS NOT NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;
    IF v_mode = 'pointcloud'
       AND (p_frames IS NULL OR pg_catalog.jsonb_typeof(p_frames) <> 'array') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    IF v_mode = 'pointcloud' THEN
        FOR v_frame IN
            SELECT frame.value
            FROM pg_catalog.jsonb_array_elements(p_frames) AS frame(value)
        LOOP
            IF pg_catalog.jsonb_typeof(v_frame) IS DISTINCT FROM 'object' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-task-creation/v1',
                    'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                );
            END IF;
            IF pg_catalog.jsonb_object_length(v_frame) <> 2
               OR NOT (v_frame ? 'primary_item_id')
               OR NOT (v_frame ? 'cameras')
               OR pg_catalog.jsonb_typeof(v_frame -> 'primary_item_id') <> 'string'
               OR (v_frame ->> 'primary_item_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               OR pg_catalog.jsonb_typeof(v_frame -> 'cameras') <> 'array' THEN
                RETURN pg_catalog.jsonb_build_object(
                    'schema', 'aap-task-creation/v1',
                    'ok', false,
                    'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                );
            END IF;

            FOR v_camera IN
                SELECT camera.value
                FROM pg_catalog.jsonb_array_elements(v_frame -> 'cameras') AS camera(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_camera) IS DISTINCT FROM 'object' THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-task-creation/v1',
                        'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
                IF pg_catalog.jsonb_object_length(v_camera) <> 2
                   OR NOT (v_camera ? 'item_id')
                   OR NOT (v_camera ? 'sensor_name')
                   OR pg_catalog.jsonb_typeof(v_camera -> 'item_id') <> 'string'
                   OR (v_camera ->> 'item_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                   OR pg_catalog.jsonb_typeof(v_camera -> 'sensor_name') <> 'string' THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-task-creation/v1',
                        'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
                v_sensor_name := v_camera ->> 'sensor_name';
                IF v_sensor_name = '' OR pg_catalog.char_length(v_sensor_name) > 43 THEN
                    RETURN pg_catalog.jsonb_build_object(
                        'schema', 'aap-task-creation/v1',
                        'ok', false,
                        'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
                    );
                END IF;
            END LOOP;
        END LOOP;

        IF EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_frames) WITH ORDINALITY
                AS frame(value, frame_ordinality)
            GROUP BY (frame.value ->> 'primary_item_id')::uuid
            HAVING pg_catalog.count(*) > 1
        ) OR EXISTS (
            SELECT 1
            FROM pg_catalog.jsonb_array_elements(p_frames) WITH ORDINALITY
                AS frame(value, frame_ordinality)
            CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                frame.value -> 'cameras'
            ) AS camera(value)
            GROUP BY frame.frame_ordinality, camera.value ->> 'sensor_name'
            HAVING pg_catalog.count(*) > 1
        ) THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
            );
        END IF;

        SELECT COALESCE(
            pg_catalog.array_agg(requested.item_id ORDER BY requested.item_id),
            ARRAY[]::uuid[]
        )
        INTO v_source_ids
        FROM (
            SELECT DISTINCT source.item_id
            FROM (
                SELECT (frame.value ->> 'primary_item_id')::uuid AS item_id
                FROM pg_catalog.jsonb_array_elements(p_frames) AS frame(value)
                UNION ALL
                SELECT (camera.value ->> 'item_id')::uuid AS item_id
                FROM pg_catalog.jsonb_array_elements(p_frames) AS frame(value)
                CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                    frame.value -> 'cameras'
                ) AS camera(value)
            ) AS source
        ) AS requested;
    END IF;

    IF v_mode = 'generic' THEN
        v_sources := aap_assets_v1.lock_task_creation_items(v_dataset_id, NULL);
    ELSE
        v_sources := aap_assets_v1.lock_task_creation_items(v_dataset_id, v_source_ids);
    END IF;
    IF v_sources ->> 'schema' IS DISTINCT FROM 'aap-task-creation-items/v1'
       OR v_sources ->> 'ok' IS DISTINCT FROM 'true' THEN
        IF v_sources ->> 'schema' IS DISTINCT FROM 'aap-task-creation-items/v1' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'assets_contract_mismatch')
            );
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', v_sources -> 'error'
        );
    END IF;
    v_source_items := v_sources #> '{data,items}';

    IF v_mode = 'generic' THEN
        WITH items AS MATERIALIZED (
            SELECT
                (item.value ->> 'id')::uuid AS item_id,
                item.value ->> 'file_name' AS file_name,
                item.value ->> 'file_path' AS file_path,
                item.value ->> 'file_type' AS file_type
            FROM pg_catalog.jsonb_array_elements(v_source_items) AS item(value)
        ),
        candidates AS MATERIALIZED (
            SELECT item.*
            FROM items AS item
            WHERE NOT EXISTS (
                SELECT 1
                FROM public.tasks AS task
                WHERE task.project_id = v_project_id
                  AND task.dataset_item_id = item.item_id
            )
        )
        SELECT
            COALESCE(
                pg_catalog.jsonb_agg(
                    pg_catalog.jsonb_build_object(
                        'item_id', candidate.item_id,
                        'file_name', candidate.file_name,
                        'file_path', candidate.file_path,
                        'file_type', candidate.file_type,
                        'links', '[]'::jsonb
                    ) ORDER BY candidate.item_id
                ),
                '[]'::jsonb
            ),
            pg_catalog.count(*)
        INTO v_manifest, v_total
        FROM candidates AS candidate;
    ELSE
        WITH source_items AS MATERIALIZED (
            SELECT
                (item.value ->> 'id')::uuid AS item_id,
                item.value ->> 'file_name' AS file_name,
                item.value ->> 'file_path' AS file_path
            FROM pg_catalog.jsonb_array_elements(v_source_items) AS item(value)
        ),
        frame_rows AS MATERIALIZED (
            SELECT frame.value, frame.ordinality
            FROM pg_catalog.jsonb_array_elements(p_frames) WITH ORDINALITY
                AS frame(value, ordinality)
        ),
        candidates AS MATERIALIZED (
            SELECT
                frame.ordinality,
                primary_item.item_id,
                primary_item.file_name,
                primary_item.file_path,
                frame.value -> 'cameras' AS cameras
            FROM frame_rows AS frame
            JOIN source_items AS primary_item
              ON primary_item.item_id = (frame.value ->> 'primary_item_id')::uuid
            WHERE NOT EXISTS (
                SELECT 1
                FROM public.tasks AS task
                WHERE task.project_id = v_project_id
                  AND task.dataset_item_id = primary_item.item_id
            )
        ),
        manifest_rows AS MATERIALIZED (
            SELECT
                candidate.ordinality,
                pg_catalog.jsonb_build_object(
                    'item_id', candidate.item_id,
                    'file_name', candidate.file_name,
                    'file_path', candidate.file_path,
                    'file_type', 'point_cloud',
                    'links', pg_catalog.jsonb_build_array(
                        pg_catalog.jsonb_build_object(
                            'item_id', candidate.item_id,
                            'role', 'primary_lidar',
                            'sensor_name', NULL
                        )
                    ) || COALESCE(
                        (
                            SELECT pg_catalog.jsonb_agg(
                                pg_catalog.jsonb_build_object(
                                    'item_id', (camera.value ->> 'item_id')::uuid,
                                    'role', 'camera_' || (camera.value ->> 'sensor_name'),
                                    'sensor_name', camera.value ->> 'sensor_name'
                                ) ORDER BY camera.ordinality
                            )
                            FROM pg_catalog.jsonb_array_elements(candidate.cameras)
                                WITH ORDINALITY AS camera(value, ordinality)
                        ),
                        '[]'::jsonb
                    )
                ) AS manifest_item
            FROM candidates AS candidate
        )
        SELECT
            COALESCE(
                pg_catalog.jsonb_agg(
                    manifest_rows.manifest_item ORDER BY manifest_rows.ordinality
                ),
                '[]'::jsonb
            ),
            pg_catalog.count(*)
        INTO v_manifest, v_total
        FROM manifest_rows;
    END IF;

    UPDATE public.task_creation_operations AS operation
    SET manifest = v_manifest,
        total = v_total,
        state = CASE WHEN v_total = 0 THEN 'completed' ELSE 'running' END,
        updated_at = pg_catalog.now()
    WHERE operation.id = p_operation_id;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', v_operation.id,
            'actor_id', v_operation.actor_id,
            'dataset_id', v_operation.dataset_id,
            'project_id', v_operation.project_id,
            'association_id', v_operation.association_id,
            'job_id', v_operation.job_id,
            'mode', v_operation.mode,
            'chunk_size', v_operation.chunk_size,
            'state', CASE WHEN v_total = 0 THEN 'completed' ELSE 'running' END,
            'total', v_total,
            'next_chunk', v_operation.next_chunk,
            'processed', v_operation.processed,
            'created', v_operation.created
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.prepare_dataset_task_creation(uuid, jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.read_dataset_task_creation(
    p_operation_id uuid
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor_id uuid;
    v_dataset_id uuid;
    v_project_id uuid;
    v_association_id uuid;
    v_job_id uuid;
    v_mode text;
    v_chunk_size integer;
    v_operation public.task_creation_operations%ROWTYPE;
    v_current_link_id uuid;
    v_job jsonb;
    v_job_data jsonb;
BEGIN
    IF p_operation_id IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    SELECT
        operation.actor_id,
        operation.dataset_id,
        operation.project_id,
        operation.association_id,
        operation.job_id,
        operation.mode,
        operation.chunk_size
    INTO
        v_actor_id,
        v_dataset_id,
        v_project_id,
        v_association_id,
        v_job_id,
        v_mode,
        v_chunk_size
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    PERFORM aap_assets_v1.lock_dataset_reference(v_dataset_id);
    v_current_link_id := aap_assets_v1.lock_current_association(
        v_dataset_id,
        v_project_id
    );
    IF v_current_link_id IS NULL OR v_current_link_id <> v_association_id THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'association_stale')
        );
    END IF;

    IF v_job_id IS NOT NULL THEN
        v_job := aap_jobs_v1.lock_task_creation_job(
            v_job_id,
            p_operation_id,
            v_actor_id,
            v_dataset_id,
            v_project_id,
            v_association_id
        );
        IF v_job ->> 'schema' IS DISTINCT FROM 'aap-task-creation-job/v1' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'jobs_contract_mismatch')
            );
        END IF;
        IF v_job ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', v_job -> 'error'
            );
        END IF;
        v_job_data := v_job -> 'data';
        IF v_job_data ->> 'cancel_requested' = 'true'
           OR v_job_data ->> 'status' = 'cancelled' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'job_cancelled')
            );
        END IF;
    END IF;

    SELECT operation.*
    INTO v_operation
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_operation.actor_id <> v_actor_id
       OR v_operation.dataset_id <> v_dataset_id
       OR v_operation.project_id <> v_project_id
       OR v_operation.association_id <> v_association_id
       OR v_operation.job_id IS DISTINCT FROM v_job_id
       OR v_operation.mode IS DISTINCT FROM v_mode
       OR v_operation.chunk_size IS DISTINCT FROM v_chunk_size THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    IF v_job_id IS NOT NULL
       AND v_job_data ->> 'status' NOT IN ('pending', 'running')
       AND NOT (
           v_job_data ->> 'status' = 'completed'
           AND v_operation.state = 'completed'
       ) THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'job_not_active')
        );
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', v_operation.id,
            'actor_id', v_operation.actor_id,
            'dataset_id', v_operation.dataset_id,
            'project_id', v_operation.project_id,
            'association_id', v_operation.association_id,
            'job_id', v_operation.job_id,
            'mode', v_operation.mode,
            'chunk_size', v_operation.chunk_size,
            'state', v_operation.state,
            'total', v_operation.total,
            'next_chunk', v_operation.next_chunk,
            'processed', v_operation.processed,
            'created', v_operation.created
        )
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.read_dataset_task_creation(uuid) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.advance_dataset_task_creation(
    p_operation_id uuid,
    p_chunk_index integer
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_actor_id uuid;
    v_dataset_id uuid;
    v_project_id uuid;
    v_association_id uuid;
    v_job_id uuid;
    v_mode text;
    v_chunk_size integer;
    v_operation public.task_creation_operations%ROWTYPE;
    v_current_link_id uuid;
    v_job jsonb;
    v_job_data jsonb;
    v_saved_result jsonb;
    v_chunk_manifest jsonb;
    v_chunk_length integer;
    v_item_ids uuid[];
    v_sources jsonb;
    v_planned_created bigint;
    v_actual_created bigint;
    v_next_processed bigint;
    v_next_created bigint;
    v_next_chunk integer;
    v_project_total bigint;
    v_complete boolean;
    v_progress_ok boolean;
    v_result jsonb;
BEGIN
    IF p_operation_id IS NULL OR p_chunk_index IS NULL OR p_chunk_index < 0 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'invalid_request')
        );
    END IF;

    SELECT
        operation.actor_id,
        operation.dataset_id,
        operation.project_id,
        operation.association_id,
        operation.job_id,
        operation.mode,
        operation.chunk_size
    INTO
        v_actor_id,
        v_dataset_id,
        v_project_id,
        v_association_id,
        v_job_id,
        v_mode,
        v_chunk_size
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    PERFORM aap_assets_v1.lock_dataset_reference(v_dataset_id);
    v_current_link_id := aap_assets_v1.lock_current_association(
        v_dataset_id,
        v_project_id
    );
    IF v_current_link_id IS NULL OR v_current_link_id <> v_association_id THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'association_stale')
        );
    END IF;

    IF v_job_id IS NOT NULL THEN
        v_job := aap_jobs_v1.lock_task_creation_job(
            v_job_id,
            p_operation_id,
            v_actor_id,
            v_dataset_id,
            v_project_id,
            v_association_id
        );
        IF v_job ->> 'schema' IS DISTINCT FROM 'aap-task-creation-job/v1' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation-chunk/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'jobs_contract_mismatch')
            );
        END IF;
        IF v_job ->> 'ok' IS DISTINCT FROM 'true' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation-chunk/v1',
                'ok', false,
                'error', v_job -> 'error'
            );
        END IF;
        v_job_data := v_job -> 'data';
        IF v_job_data ->> 'cancel_requested' = 'true'
           OR v_job_data ->> 'status' = 'cancelled' THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation-chunk/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'job_cancelled')
            );
        END IF;
    END IF;

    SELECT operation.*
    INTO v_operation
    FROM public.task_creation_operations AS operation
    WHERE operation.id = p_operation_id
    FOR UPDATE;
    IF NOT FOUND
       OR v_operation.actor_id <> v_actor_id
       OR v_operation.dataset_id <> v_dataset_id
       OR v_operation.project_id <> v_project_id
       OR v_operation.association_id <> v_association_id
       OR v_operation.job_id IS DISTINCT FROM v_job_id
       OR v_operation.mode IS DISTINCT FROM v_mode
       OR v_operation.chunk_size IS DISTINCT FROM v_chunk_size THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'operation_missing')
        );
    END IF;

    SELECT chunk.result
    INTO v_saved_result
    FROM public.task_creation_chunks AS chunk
    WHERE chunk.operation_id = p_operation_id
      AND chunk.chunk_index = p_chunk_index
    FOR UPDATE;
    IF FOUND THEN
        RETURN v_saved_result;
    END IF;

    IF v_job_id IS NOT NULL
       AND v_job_data ->> 'status' NOT IN ('pending', 'running') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'job_not_active')
        );
    END IF;
    IF v_operation.manifest IS NULL OR v_operation.total IS NULL THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'plan_not_prepared')
        );
    END IF;
    IF v_operation.total = 0 OR v_operation.state = 'completed' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'plan_complete')
        );
    END IF;
    IF p_chunk_index <> v_operation.next_chunk THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'cursor_conflict')
        );
    END IF;

    SELECT
        COALESCE(
            pg_catalog.jsonb_agg(page.value ORDER BY page.ordinality),
            '[]'::jsonb
        ),
        pg_catalog.count(*)::integer
    INTO v_chunk_manifest, v_chunk_length
    FROM pg_catalog.jsonb_array_elements(v_operation.manifest) WITH ORDINALITY
        AS page(value, ordinality)
    WHERE page.ordinality > (p_chunk_index::bigint * v_chunk_size)
      AND page.ordinality <= ((p_chunk_index::bigint + 1) * v_chunk_size);
    IF v_chunk_length = 0 THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'cursor_conflict')
        );
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(requested.item_id ORDER BY requested.item_id),
        ARRAY[]::uuid[]
    )
    INTO v_item_ids
    FROM (
        SELECT DISTINCT source.item_id
        FROM (
            SELECT (item.value ->> 'item_id')::uuid AS item_id
            FROM pg_catalog.jsonb_array_elements(v_chunk_manifest) AS item(value)
            UNION ALL
            SELECT (link.value ->> 'item_id')::uuid AS item_id
            FROM pg_catalog.jsonb_array_elements(v_chunk_manifest) AS item(value)
            CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(
                item.value -> 'links'
            ) AS link(value)
        ) AS source
    ) AS requested;

    v_sources := aap_assets_v1.lock_task_creation_items(v_dataset_id, v_item_ids);
    IF v_sources ->> 'schema' IS DISTINCT FROM 'aap-task-creation-items/v1' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'assets_contract_mismatch')
        );
    END IF;
    IF v_sources ->> 'ok' IS DISTINCT FROM 'true' THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', v_sources -> 'error'
        );
    END IF;

    PERFORM project.id
    FROM public.projects AS project
    WHERE project.id = v_project_id
    FOR NO KEY UPDATE;
    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'project_missing')
        );
    END IF;

    SELECT pg_catalog.count(*)
    INTO v_planned_created
    FROM pg_catalog.jsonb_array_elements(v_chunk_manifest) AS item(value)
    WHERE NOT EXISTS (
        SELECT 1
        FROM public.tasks AS task
        WHERE task.project_id = v_project_id
          AND task.dataset_item_id = (item.value ->> 'item_id')::uuid
    );

    v_next_processed := v_operation.processed + v_chunk_length;
    v_next_created := v_operation.created + v_planned_created;
    v_next_chunk := p_chunk_index + 1;
    v_complete := v_next_processed = v_operation.total;

    IF v_next_processed > v_operation.total THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-task-creation-chunk/v1',
            'ok', false,
            'error', pg_catalog.jsonb_build_object('code', 'cursor_conflict')
        );
    END IF;

    IF v_job_id IS NOT NULL THEN
        v_progress_ok := aap_jobs_v1.update_task_creation_progress(
            v_job_id,
            p_operation_id,
            v_next_processed,
            v_operation.total,
            v_next_created
        );
        IF NOT v_progress_ok THEN
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-task-creation-chunk/v1',
                'ok', false,
                'error', pg_catalog.jsonb_build_object('code', 'job_not_active')
            );
        END IF;
    END IF;

    WITH chunk_items AS MATERIALIZED (
        SELECT item.value, item.ordinality
        FROM pg_catalog.jsonb_array_elements(v_chunk_manifest) WITH ORDINALITY
            AS item(value, ordinality)
    ),
    pending AS MATERIALIZED (
        SELECT
            item.ordinality,
            (item.value ->> 'item_id')::uuid AS item_id,
            item.value ->> 'file_name' AS file_name,
            item.value ->> 'file_path' AS file_path,
            item.value ->> 'file_type' AS file_type,
            item.value -> 'links' AS links
        FROM chunk_items AS item
        WHERE NOT EXISTS (
            SELECT 1
            FROM public.tasks AS task
            WHERE task.project_id = v_project_id
              AND task.dataset_item_id = (item.value ->> 'item_id')::uuid
        )
    ),
    numbered AS MATERIALIZED (
        SELECT
            pending.*,
            pg_catalog.row_number() OVER (ORDER BY pending.ordinality) AS row_number
        FROM pending
    ),
    sequence_values AS MATERIALIZED (
        SELECT
            sequence_row.row_number,
            pg_catalog.nextval(
                'public.display_seq_tasks'::pg_catalog.regclass
            ) AS display_number
        FROM pg_catalog.generate_series(1, v_planned_created) AS sequence_row(row_number)
        ORDER BY sequence_row.row_number
    ),
    inserted AS (
        INSERT INTO public.tasks (
            id,
            project_id,
            dataset_item_id,
            display_id,
            file_name,
            file_path,
            file_type,
            tags,
            status,
            assignee_is_override,
            is_labeled,
            overlap,
            total_annotations,
            total_predictions,
            reviewer_is_override,
            first_review_eligible,
            annotation_contributor_ids,
            reopened_count,
            batch_id,
            version
        )
        SELECT
            pg_catalog.gen_random_uuid(),
            v_project_id,
            numbered.item_id,
            'T-' || sequence_values.display_number::text,
            numbered.file_name,
            numbered.file_path,
            numbered.file_type,
            '[]'::jsonb,
            'pending',
            false,
            false,
            1,
            0,
            0,
            false,
            true,
            '[]'::jsonb,
            0,
            NULL,
            1
        FROM numbered
        JOIN sequence_values USING (row_number)
        ORDER BY numbered.ordinality
        RETURNING id, project_id, dataset_item_id
    ),
    inserted_links AS (
        INSERT INTO public.task_dataset_item_links (
            id,
            task_id,
            dataset_item_id,
            role,
            sensor_name
        )
        SELECT
            pg_catalog.gen_random_uuid(),
            inserted.id,
            (link.value ->> 'item_id')::uuid,
            link.value ->> 'role',
            link.value ->> 'sensor_name'
        FROM inserted
        JOIN pending
          ON pending.item_id = inserted.dataset_item_id
        CROSS JOIN LATERAL pg_catalog.jsonb_array_elements(pending.links) AS link(value)
        RETURNING task_id
    ),
    link_count AS MATERIALIZED (
        SELECT pg_catalog.count(*) AS linked FROM inserted_links
    ),
    created_by_project AS MATERIALIZED (
        SELECT inserted.project_id, pg_catalog.count(*) AS created
        FROM inserted
        GROUP BY inserted.project_id
    ),
    updated_projects AS (
        UPDATE public.projects AS project
        SET total_tasks = COALESCE(project.total_tasks, 0) + created.created,
            updated_at = pg_catalog.now()
        FROM created_by_project AS created
        WHERE project.id = created.project_id
        RETURNING project.id AS project_id, project.total_tasks
    )
    SELECT
        COALESCE(
            (SELECT pg_catalog.sum(created.created) FROM created_by_project AS created),
            0
        ),
        COALESCE(
            (SELECT updated.total_tasks FROM updated_projects AS updated),
            (
                SELECT COALESCE(project.total_tasks, 0)
                FROM public.projects AS project
                WHERE project.id = v_project_id
            )
        )
    INTO v_actual_created, v_project_total
    FROM link_count;

    IF v_actual_created <> v_planned_created THEN
        RAISE EXCEPTION USING
            ERRCODE = '40001',
            MESSAGE = 'initial task candidate set changed during a locked chunk';
    END IF;

    v_result := pg_catalog.jsonb_build_object(
        'schema', 'aap-task-creation-chunk/v1',
        'ok', true,
        'data', pg_catalog.jsonb_build_object(
            'operation_id', p_operation_id,
            'project_id', v_project_id,
            'chunk_index', p_chunk_index,
            'created', v_actual_created,
            'processed', v_chunk_length,
            'cumulative_created', v_next_created,
            'cumulative_processed', v_next_processed,
            'total', v_operation.total,
            'next_chunk', v_next_chunk,
            'complete', v_complete,
            'project_total', v_project_total
        )
    );

    UPDATE public.task_creation_operations AS operation
    SET next_chunk = v_next_chunk,
        processed = v_next_processed,
        created = v_next_created,
        state = CASE WHEN v_complete THEN 'completed' ELSE 'running' END,
        updated_at = pg_catalog.now()
    WHERE operation.id = p_operation_id;

    INSERT INTO public.task_creation_chunks (
        operation_id,
        chunk_index,
        result
    )
    VALUES (p_operation_id, p_chunk_index, v_result);

    RETURN v_result;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.advance_dataset_task_creation(uuid, integer) FROM PUBLIC;
