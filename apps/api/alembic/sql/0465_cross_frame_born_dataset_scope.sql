-- A born cross-frame Job's exact Dataset set is proven from the frozen source
-- Task/Annotation snapshot, every non-ready preflight omits its Task, and the
-- canonical Task -> DatasetItem -> Dataset facts (direct Item first, then the
-- unique primary_lidar link). The payload only selects already-authentic
-- project Tasks; it can never contribute a Dataset id, an unrelated result or
-- an empty scope. Historical/unproven Jobs never enter this derivation.
--
-- Nothing here is granted: the Jobs owner calls the birth function from its
-- AFTER INSERT immutable receipt capture and the cross-frame executor calls the
-- fence from its per-frame write transaction. The parent migration owns both
-- grants.
CREATE FUNCTION aap_assets_v1._cross_frame_scope_uuid(p_value jsonb)
RETURNS uuid
LANGUAGE plpgsql
IMMUTABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'string'
       OR (p_value #>> '{}') !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN
        RETURN NULL;
    END IF;
    RETURN (p_value #>> '{}')::uuid;
EXCEPTION WHEN others THEN
    RETURN NULL;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._cross_frame_scope_uuid(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._cross_frame_scope_int(p_value jsonb)
RETURNS integer
LANGUAGE plpgsql
IMMUTABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_text text;
    v_number numeric;
BEGIN
    IF pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'number' THEN
        RETURN NULL;
    END IF;
    v_text := p_value #>> '{}';
    IF v_text !~ '^[0-9]{1,12}$' THEN
        RETURN NULL;
    END IF;
    v_number := v_text::numeric;
    IF v_number > 2147483647 THEN
        RETURN NULL;
    END IF;
    RETURN v_number::integer;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._cross_frame_scope_int(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._cross_frame_scope_annotation_ids(p_sources jsonb)
RETURNS uuid[]
LANGUAGE plpgsql
IMMUTABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item jsonb;
    v_id uuid;
    v_ids uuid[] := ARRAY[]::uuid[];
BEGIN
    IF pg_catalog.jsonb_typeof(p_sources) IS DISTINCT FROM 'array' THEN
        RETURN NULL;
    END IF;
    FOR v_item IN
        SELECT value FROM pg_catalog.jsonb_array_elements(p_sources) AS rows(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object' THEN
            RETURN NULL;
        END IF;
        v_id := aap_assets_v1._cross_frame_scope_uuid(v_item -> 'annotation_id');
        IF v_id IS NULL THEN
            RETURN NULL;
        END IF;
        v_ids := pg_catalog.array_append(v_ids, v_id);
    END LOOP;
    RETURN v_ids;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._cross_frame_scope_annotation_ids(jsonb) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1._cross_frame_scope_frames(p_targets jsonb)
RETURNS integer[]
LANGUAGE plpgsql
IMMUTABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_item jsonb;
    v_frame integer;
    v_frames integer[] := ARRAY[]::integer[];
BEGIN
    IF pg_catalog.jsonb_typeof(p_targets) IS DISTINCT FROM 'array' THEN
        RETURN NULL;
    END IF;
    FOR v_item IN
        SELECT value FROM pg_catalog.jsonb_array_elements(p_targets) AS rows(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object' THEN
            RETURN NULL;
        END IF;
        v_frame := aap_assets_v1._cross_frame_scope_int(v_item -> 'frame_index');
        IF v_frame IS NULL THEN
            RETURN NULL;
        END IF;
        v_frames := pg_catalog.array_append(v_frames, v_frame);
    END LOOP;
    RETURN v_frames;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._cross_frame_scope_frames(jsonb) FROM PUBLIC;
-- aap-statement-break
-- Derives the complete Dataset set a cross-frame execution can still reach
-- from the current owner facts. It is deliberately independent of Job status,
-- phase and any payload-declared Dataset. A NULL result means the shape is not
-- proven; an empty result is never produced because every cross-frame Job has
-- an authentic source Task in a Scene Dataset.
CREATE FUNCTION aap_assets_v1._cross_frame_dataset_scope(
    p_project_id uuid, p_actor_id uuid, p_payload jsonb, p_check_parent boolean
)
RETURNS uuid[]
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_scene uuid;
    v_scene_dataset uuid;
    v_source_task uuid;
    v_source_frame integer;
    v_start_frame integer;
    v_end_frame integer;
    v_direction text;
    v_sources jsonb;
    v_targets jsonb;
    v_item jsonb;
    v_keys text[];
    v_source_direct uuid;
    v_source_linked uuid;
    v_source_links uuid[];
    v_direct_ref record;
    v_linked_ref record;
    v_effective_scene uuid;
    v_source_item_ids uuid[];
    v_target_item_ids uuid[];
    v_target_scene_ids uuid[];
    v_target_links uuid[];
    v_item_count integer;
    v_target_frame integer;
    v_target_task uuid;
    v_target_state text;
    v_target_direct uuid;
    v_target_linked uuid;
    v_found integer;
    v_new_datasets uuid[];
    v_datasets uuid[] := ARRAY[]::uuid[];
    v_frames integer[] := ARRAY[]::integer[];
    v_ready_count integer := 0;
    v_annotation_ids uuid[] := ARRAY[]::uuid[];
    v_annotation_id uuid;
    v_version integer;
    v_kind text;
    v_parent_id uuid;
    v_parent_kind text;
    v_parent_project uuid;
    v_parent_actor uuid;
    v_parent_status text;
    v_parent_payload jsonb;
    v_parent_annotations uuid[];
    v_parent_frames integer[];
BEGIN
    IF p_project_id IS NULL
       OR p_actor_id IS NULL
       OR pg_catalog.jsonb_typeof(p_payload) IS DISTINCT FROM 'object' THEN
        RETURN NULL;
    END IF;

    -- The frozen contract shape is the only producer/retry shape understood.
    -- Fields outside this validated routing/source set (display names, digest)
    -- never affect scope.
    IF p_payload -> 'contract_version' IS DISTINCT FROM '1'::jsonb
       OR p_payload ->> 'operation' IS DISTINCT FROM 'propagate'
       OR p_payload ->> 'scope' IS NULL
       OR (p_payload ->> 'scope') NOT IN ('selected', 'all')
       OR p_payload ->> 'conflict_policy' IS DISTINCT FROM 'skip_existing' THEN
        RETURN NULL;
    END IF;
    v_direction := p_payload ->> 'direction';
    IF v_direction IS NULL OR v_direction NOT IN ('forward', 'backward') THEN
        RETURN NULL;
    END IF;

    v_source_frame := aap_assets_v1._cross_frame_scope_int(
        p_payload -> 'source_frame'
    );
    v_start_frame := aap_assets_v1._cross_frame_scope_int(
        p_payload -> 'start_frame'
    );
    v_end_frame := aap_assets_v1._cross_frame_scope_int(
        p_payload -> 'end_frame'
    );
    IF v_source_frame IS NULL OR v_start_frame IS NULL OR v_end_frame IS NULL
       OR v_start_frame > v_end_frame
       OR v_end_frame - v_start_frame + 1 > 100 THEN
        RETURN NULL;
    END IF;
    IF v_direction = 'forward' THEN
        IF v_start_frame <= v_source_frame THEN
            RETURN NULL;
        END IF;
    ELSIF v_end_frame >= v_source_frame THEN
        RETURN NULL;
    END IF;

    v_scene := aap_assets_v1._cross_frame_scope_uuid(p_payload -> 'scene_id');
    IF v_scene IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT scene.dataset_id INTO v_scene_dataset
    FROM public.scenes AS scene
    WHERE scene.id = v_scene;
    IF NOT FOUND OR v_scene_dataset IS NULL THEN
        RETURN NULL;
    END IF;
    v_datasets := pg_catalog.array_append(v_datasets, v_scene_dataset);

    v_source_task := aap_assets_v1._cross_frame_scope_uuid(
        p_payload -> 'source_task_id'
    );
    IF v_source_task IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT task.dataset_item_id INTO v_source_direct
    FROM aap_labeling_v1.task_rows AS task
    WHERE task.id = v_source_task AND task.project_id = p_project_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT link.dataset_item_id INTO v_source_linked
    FROM public.task_dataset_item_links AS link
    WHERE link.task_id = v_source_task AND link.role = 'primary_lidar'
    ORDER BY link.id
    LIMIT 1;
    v_source_links := ARRAY(
        SELECT DISTINCT link.dataset_item_id
        FROM public.task_dataset_item_links AS link
        WHERE link.task_id = v_source_task
        ORDER BY link.dataset_item_id
    );
    IF v_source_direct IS NULL AND pg_catalog.cardinality(v_source_links) = 0 THEN
        RETURN NULL;
    END IF;

    -- Producer scene/frame resolution: a direct Item that carries a Scene wins;
    -- the primary_lidar link is the canonical fallback for a source-less Task.
    SELECT NULL::uuid AS scene_id, NULL::integer AS frame_index
    INTO v_direct_ref;
    SELECT NULL::uuid AS scene_id, NULL::integer AS frame_index
    INTO v_linked_ref;
    IF v_source_direct IS NOT NULL THEN
        SELECT reference.scene_id, reference.frame_index INTO v_direct_ref
        FROM aap_assets_v1.spatial_item_references AS reference
        WHERE reference.item_id = v_source_direct;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
    END IF;
    IF v_source_linked IS NOT NULL THEN
        SELECT reference.scene_id, reference.frame_index INTO v_linked_ref
        FROM aap_assets_v1.spatial_item_references AS reference
        WHERE reference.item_id = v_source_linked;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
    END IF;
    IF v_direct_ref.scene_id IS NOT NULL THEN
        v_effective_scene := v_direct_ref.scene_id;
        IF v_direct_ref.frame_index IS DISTINCT FROM v_source_frame THEN
            RETURN NULL;
        END IF;
    ELSIF v_linked_ref.scene_id IS NOT NULL THEN
        v_effective_scene := v_linked_ref.scene_id;
        IF v_linked_ref.frame_index IS DISTINCT FROM v_source_frame THEN
            RETURN NULL;
        END IF;
    ELSE
        RETURN NULL;
    END IF;
    IF v_effective_scene IS DISTINCT FROM v_scene THEN
        RETURN NULL;
    END IF;

    v_source_item_ids := ARRAY(
        SELECT DISTINCT ids.id
        FROM pg_catalog.unnest(
            ARRAY[v_source_direct]::uuid[] || COALESCE(v_source_links, ARRAY[]::uuid[])
        ) AS ids(id)
        WHERE ids.id IS NOT NULL
        ORDER BY ids.id
    );
    SELECT pg_catalog.count(*) INTO v_item_count
    FROM aap_assets_v1.spatial_item_references AS reference
    WHERE reference.item_id = ANY(v_source_item_ids);
    IF v_item_count <> pg_catalog.cardinality(v_source_item_ids) THEN
        RETURN NULL;
    END IF;
    SELECT pg_catalog.array_agg(DISTINCT reference.dataset_id) INTO v_new_datasets
    FROM aap_assets_v1.spatial_item_references AS reference
    WHERE reference.item_id = ANY(v_source_item_ids);
    IF v_new_datasets IS NULL THEN
        RETURN NULL;
    END IF;
    v_datasets := v_datasets || v_new_datasets;

    v_sources := p_payload -> 'sources';
    IF pg_catalog.jsonb_typeof(v_sources) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(v_sources) < 1
       OR pg_catalog.jsonb_array_length(v_sources) > 500 THEN
        RETURN NULL;
    END IF;
    FOR v_item IN
        SELECT value FROM pg_catalog.jsonb_array_elements(v_sources) AS rows(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object' THEN
            RETURN NULL;
        END IF;
        SELECT pg_catalog.array_agg(key ORDER BY key) INTO v_keys
        FROM pg_catalog.jsonb_object_keys(v_item) AS keys(key);
        IF v_keys IS DISTINCT FROM ARRAY[
            'annotation_id', 'track_id', 'version'
        ]::text[] THEN
            RETURN NULL;
        END IF;
        v_annotation_id := aap_assets_v1._cross_frame_scope_uuid(
            v_item -> 'annotation_id'
        );
        IF v_annotation_id IS NULL
           OR v_annotation_id = ANY(v_annotation_ids) THEN
            RETURN NULL;
        END IF;
        v_version := aap_assets_v1._cross_frame_scope_int(v_item -> 'version');
        IF v_version IS NULL OR v_version < 1 THEN
            RETURN NULL;
        END IF;
        v_kind := pg_catalog.jsonb_typeof(v_item -> 'track_id');
        IF v_kind IS NULL OR v_kind NOT IN ('string', 'null') THEN
            RETURN NULL;
        END IF;
        v_annotation_ids := pg_catalog.array_append(
            v_annotation_ids, v_annotation_id
        );
    END LOOP;

    v_targets := p_payload -> 'targets';
    IF pg_catalog.jsonb_typeof(v_targets) IS DISTINCT FROM 'array'
       OR pg_catalog.jsonb_array_length(v_targets) < 1
       OR pg_catalog.jsonb_array_length(v_targets) > 100 THEN
        RETURN NULL;
    END IF;
    IF aap_assets_v1._cross_frame_scope_int(p_payload -> 'total_frames')
       IS DISTINCT FROM pg_catalog.jsonb_array_length(v_targets) THEN
        RETURN NULL;
    END IF;
    FOR v_item IN
        SELECT value FROM pg_catalog.jsonb_array_elements(v_targets) AS rows(value)
    LOOP
        IF pg_catalog.jsonb_typeof(v_item) IS DISTINCT FROM 'object' THEN
            RETURN NULL;
        END IF;
        SELECT pg_catalog.array_agg(key ORDER BY key) INTO v_keys
        FROM pg_catalog.jsonb_object_keys(v_item) AS keys(key);
        IF v_keys IS DISTINCT FROM ARRAY[
            'frame_index', 'preflight_state', 'task_id'
        ]::text[] THEN
            RETURN NULL;
        END IF;
        v_target_frame := aap_assets_v1._cross_frame_scope_int(
            v_item -> 'frame_index'
        );
        IF v_target_frame IS NULL
           OR v_target_frame < v_start_frame
           OR v_target_frame > v_end_frame
           OR v_target_frame = ANY(v_frames) THEN
            RETURN NULL;
        END IF;
        IF v_direction = 'forward' THEN
            IF v_target_frame <= v_source_frame THEN
                RETURN NULL;
            END IF;
        ELSIF v_target_frame >= v_source_frame THEN
            RETURN NULL;
        END IF;
        v_frames := pg_catalog.array_append(v_frames, v_target_frame);

        v_target_state := v_item ->> 'preflight_state';
        IF v_target_state IS NULL
           OR v_target_state NOT IN (
               'ready', 'missing', 'unavailable', 'not_editable'
           ) THEN
            RETURN NULL;
        END IF;
        v_kind := pg_catalog.jsonb_typeof(v_item -> 'task_id');
        IF v_kind IS NULL OR v_kind NOT IN ('string', 'null') THEN
            RETURN NULL;
        END IF;
        IF v_kind = 'null' THEN
            IF v_target_state = 'ready' THEN
                RETURN NULL;
            END IF;
            CONTINUE;
        END IF;
        IF v_target_state <> 'ready' THEN
            RETURN NULL;
        END IF;
        v_ready_count := v_ready_count + 1;
        v_target_task := aap_assets_v1._cross_frame_scope_uuid(
            v_item -> 'task_id'
        );
        IF v_target_task IS NULL THEN
            RETURN NULL;
        END IF;
        SELECT task.dataset_item_id INTO v_target_direct
        FROM aap_labeling_v1.task_rows AS task
        WHERE task.id = v_target_task AND task.project_id = p_project_id;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        SELECT link.dataset_item_id INTO v_target_linked
        FROM public.task_dataset_item_links AS link
        WHERE link.task_id = v_target_task AND link.role = 'primary_lidar'
        ORDER BY link.id
        LIMIT 1;
        v_target_links := ARRAY(
            SELECT DISTINCT link.dataset_item_id
            FROM public.task_dataset_item_links AS link
            WHERE link.task_id = v_target_task
            ORDER BY link.dataset_item_id
        );
        IF v_target_direct IS NULL AND pg_catalog.cardinality(v_target_links) = 0 THEN
            RETURN NULL;
        END IF;
        v_target_scene_ids := ARRAY(
            SELECT DISTINCT ids.id
            FROM pg_catalog.unnest(
                ARRAY[v_target_direct, v_target_linked]::uuid[]
            ) AS ids(id)
            WHERE ids.id IS NOT NULL
            ORDER BY ids.id
        );
        v_target_item_ids := ARRAY(
            SELECT DISTINCT ids.id
            FROM pg_catalog.unnest(
                ARRAY[v_target_direct]::uuid[] || v_target_links
            ) AS ids(id)
            WHERE ids.id IS NOT NULL
            ORDER BY ids.id
        );
        SELECT pg_catalog.count(*) INTO v_found
        FROM aap_assets_v1.spatial_item_references AS reference
        WHERE reference.item_id = ANY(v_target_item_ids);
        IF v_found <> pg_catalog.cardinality(v_target_item_ids) THEN
            RETURN NULL;
        END IF;
        -- The frozen Task must still be an owner-visible member of exactly this
        -- Scene frame through one of its canonical Item relations. A re-pointed
        -- primary_lidar item or a moved Scene frame leaves the shape unproven.
        IF NOT EXISTS (
            SELECT 1
            FROM aap_assets_v1.spatial_item_references AS reference
            WHERE reference.item_id = ANY(v_target_scene_ids)
              AND reference.scene_id = v_scene
              AND reference.frame_index = v_target_frame
        ) THEN
            RETURN NULL;
        END IF;
        SELECT pg_catalog.array_agg(DISTINCT reference.dataset_id)
        INTO v_new_datasets
        FROM aap_assets_v1.spatial_item_references AS reference
        WHERE reference.item_id = ANY(v_target_item_ids);
        IF v_new_datasets IS NULL THEN
            RETURN NULL;
        END IF;
        v_datasets := v_datasets || v_new_datasets;
    END LOOP;
    IF v_ready_count = 0 THEN
        RETURN NULL;
    END IF;

    -- A retry is a new birth, never an adoption of its parent's scope. The
    -- parent identity, terminal status and frozen source/Scene frame set must
    -- all still agree with the producer that built this payload. Later
    -- executor rechecks only need the still-live Task/Scene facts.
    IF p_check_parent IS NOT TRUE THEN
        RETURN ARRAY(
            SELECT DISTINCT ids.id
            FROM pg_catalog.unnest(v_datasets) AS ids(id)
            WHERE ids.id IS NOT NULL
            ORDER BY ids.id
        );
    END IF;
    IF NOT (p_payload ? 'parent_job_id') THEN
        RETURN NULL;
    END IF;
    v_kind := pg_catalog.jsonb_typeof(p_payload -> 'parent_job_id');
    IF v_kind IS NULL OR v_kind NOT IN ('string', 'null') THEN
        RETURN NULL;
    END IF;
    IF v_kind = 'string' THEN
        v_parent_id := aap_assets_v1._cross_frame_scope_uuid(
            p_payload -> 'parent_job_id'
        );
        IF v_parent_id IS NULL THEN
            RETURN NULL;
        END IF;
        SELECT parent.kind, parent.project_id, parent.user_id, parent.status,
               parent.payload
        INTO v_parent_kind, v_parent_project, v_parent_actor, v_parent_status,
             v_parent_payload
        FROM public.async_jobs AS parent
        WHERE parent.id = v_parent_id;
        IF NOT FOUND
           OR v_parent_kind IS DISTINCT FROM 'point_cloud_cross_frame'
           OR v_parent_project IS DISTINCT FROM p_project_id
           OR v_parent_actor IS DISTINCT FROM p_actor_id
           OR v_parent_status IS NULL
           OR v_parent_status NOT IN ('completed', 'failed', 'cancelled') THEN
            RETURN NULL;
        END IF;
        IF pg_catalog.jsonb_typeof(v_parent_payload) IS DISTINCT FROM 'object'
           OR v_parent_payload ->> 'source_task_id'
              IS DISTINCT FROM p_payload ->> 'source_task_id'
           OR v_parent_payload ->> 'scene_id'
              IS DISTINCT FROM p_payload ->> 'scene_id' THEN
            RETURN NULL;
        END IF;
        v_parent_annotations := aap_assets_v1._cross_frame_scope_annotation_ids(
            v_parent_payload -> 'sources'
        );
        IF v_parent_annotations IS NULL THEN
            RETURN NULL;
        END IF;
        v_parent_annotations := ARRAY(
            SELECT DISTINCT ids.id
            FROM pg_catalog.unnest(v_parent_annotations) AS ids(id)
            ORDER BY ids.id
        );
        IF v_parent_annotations IS DISTINCT FROM ARRAY(
            SELECT DISTINCT ids.id
            FROM pg_catalog.unnest(v_annotation_ids) AS ids(id)
            ORDER BY ids.id
        ) THEN
            RETURN NULL;
        END IF;
        v_parent_frames := aap_assets_v1._cross_frame_scope_frames(
            v_parent_payload -> 'targets'
        );
        IF v_parent_frames IS NULL
           OR EXISTS (
               SELECT 1
               FROM pg_catalog.unnest(v_frames) AS retry(frame_index)
               WHERE retry.frame_index <> ALL(v_parent_frames)
           ) THEN
            RETURN NULL;
        END IF;
    END IF;

    v_datasets := ARRAY(
        SELECT DISTINCT ids.id
        FROM pg_catalog.unnest(v_datasets) AS ids(id)
        WHERE ids.id IS NOT NULL
        ORDER BY ids.id
    );
    IF pg_catalog.cardinality(v_datasets) = 0 THEN
        RETURN NULL;
    END IF;
    RETURN v_datasets;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1._cross_frame_dataset_scope(uuid,uuid,jsonb,boolean) FROM PUBLIC;
-- aap-statement-break
-- Jobs calls this from its AFTER INSERT receipt capture in the new Job's birth
-- transaction. An absent row is also accepted so the same derivation can be
-- qualified directly at its boundary; a present row must agree with the
-- callers' frozen kind/project/actor identity and is never re-adopted later.
CREATE FUNCTION aap_assets_v1.cross_frame_born_dataset_scope(
    p_job_id uuid, p_project_id uuid, p_actor_id uuid, p_payload jsonb
)
RETURNS uuid[]
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_kind text;
    v_project uuid;
    v_actor uuid;
BEGIN
    IF p_job_id IS NULL OR p_project_id IS NULL OR p_actor_id IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT job.kind, job.project_id, job.user_id
    INTO v_kind, v_project, v_actor
    FROM public.async_jobs AS job
    WHERE job.id = p_job_id;
    IF FOUND AND (
        v_kind IS DISTINCT FROM 'point_cloud_cross_frame'
        OR v_project IS DISTINCT FROM p_project_id
        OR v_actor IS DISTINCT FROM p_actor_id
    ) THEN
        RETURN NULL;
    END IF;
    RETURN aap_assets_v1._cross_frame_dataset_scope(
        p_project_id, p_actor_id, p_payload, true
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.cross_frame_born_dataset_scope(uuid,uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
-- Bounded executor fence. A born cross-frame Job may execute only while the
-- current owner facts still derive exactly its immutable receipt. Historical
-- or unproven Jobs keep their prior executor behavior; their Dataset cleanup
-- remains blocked by the fixture classifier. Any other classifier outcome is
-- fail-closed.
CREATE FUNCTION aap_assets_v1.fence_cross_frame_job_dataset_scope(
    p_job_id uuid, p_project_id uuid, p_actor_id uuid, p_payload jsonb,
    p_guard_task_ids uuid[] DEFAULT NULL
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_born jsonb;
    v_kind text;
    v_born_ids uuid[];
    v_current uuid[];
    v_scene uuid;
BEGIN
    IF p_job_id IS NULL THEN
        RETURN false;
    END IF;
    v_born := aap_jobs_v1.fixture_job_dataset_scope(
        p_job_id, 'point_cloud_cross_frame', p_payload, p_project_id, p_actor_id
    );
    IF v_born IS NULL THEN
        RETURN false;
    END IF;
    v_kind := v_born ->> 'kind';
    IF v_kind = 'datasets' THEN
        IF pg_catalog.jsonb_typeof(v_born -> 'dataset_ids') IS DISTINCT FROM 'array' THEN
            RETURN false;
        END IF;
        BEGIN
            v_born_ids := ARRAY(
                SELECT ids.value::uuid
                FROM pg_catalog.jsonb_array_elements_text(
                    v_born -> 'dataset_ids'
                ) AS ids(value)
                ORDER BY ids.value::uuid
            );
        EXCEPTION WHEN others THEN
            RETURN false;
        END;
        v_born_ids := ARRAY(
            SELECT DISTINCT ids.id
            FROM pg_catalog.unnest(v_born_ids) AS ids(id)
            ORDER BY ids.id
        );
    ELSIF v_kind = 'dataset' THEN
        IF aap_assets_v1._cross_frame_scope_uuid(v_born -> 'dataset_id') IS NULL THEN
            RETURN false;
        END IF;
        v_born_ids := ARRAY[
            aap_assets_v1._cross_frame_scope_uuid(v_born -> 'dataset_id')
        ];
    ELSIF v_kind = 'unknown' THEN
        -- The parent classifier found no immutable receipt (historical or
        -- otherwise unproven). Dataset cleanup continues to refuse it; the
        -- executor does not gate on that cleanup proof alone.
        RETURN true;
    ELSE
        -- A cross-frame Job is never proven Dataset-independent or unrelated.
        RETURN false;
    END IF;
    IF p_guard_task_ids IS NOT NULL THEN
        IF pg_catalog.array_position(p_guard_task_ids, NULL::uuid) IS NOT NULL THEN
            RETURN false;
        END IF;
        IF pg_catalog.cardinality(p_guard_task_ids) > 0 THEN
            -- Stabilize the current frame's source/target relation facts until
            -- the caller's Annotation write commits. The caller already holds
            -- the same Task rows; these share locks extend that fence to the
            -- link, Item and Scene projections read by the derivation.
            PERFORM task.id
            FROM public.tasks AS task
            WHERE task.id = ANY(p_guard_task_ids)
            ORDER BY task.id
            FOR SHARE;
            PERFORM link.id
            FROM public.task_dataset_item_links AS link
            WHERE link.task_id = ANY(p_guard_task_ids)
            ORDER BY link.task_id, link.id
            FOR SHARE;
            PERFORM item.id
            FROM public.dataset_items AS item
            WHERE item.id IN (
                SELECT source_task.dataset_item_id
                FROM aap_labeling_v1.task_rows AS source_task
                WHERE source_task.id = ANY(p_guard_task_ids)
                  AND source_task.dataset_item_id IS NOT NULL
                UNION
                SELECT linked.dataset_item_id
                FROM public.task_dataset_item_links AS linked
                WHERE linked.task_id = ANY(p_guard_task_ids)
            )
            ORDER BY item.id
            FOR SHARE;
            v_scene := aap_assets_v1._cross_frame_scope_uuid(
                p_payload -> 'scene_id'
            );
            IF v_scene IS NOT NULL THEN
                PERFORM scene.id
                FROM public.scenes AS scene
                WHERE scene.id = v_scene
                FOR SHARE;
            END IF;
        END IF;
    END IF;
    v_current := aap_assets_v1._cross_frame_dataset_scope(
        p_project_id, p_actor_id, p_payload, false
    );
    IF v_current IS NULL THEN
        RETURN false;
    END IF;
    RETURN v_born_ids IS NOT DISTINCT FROM ARRAY(
        SELECT DISTINCT ids.id
        FROM pg_catalog.unnest(v_current) AS ids(id)
        ORDER BY ids.id
    );
EXCEPTION WHEN others THEN
    RETURN false;
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fence_cross_frame_job_dataset_scope(uuid,uuid,uuid,jsonb,uuid[]) FROM PUBLIC;
