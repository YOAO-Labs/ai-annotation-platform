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
LANGUAGE sql
IMMUTABLE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT CASE
        WHEN pg_catalog.jsonb_typeof(p_value) IS DISTINCT FROM 'string' THEN NULL
        WHEN (p_value #>> '{}') !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN NULL
        ELSE (p_value #>> '{}')::uuid
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
-- Derives the complete Dataset set a cross-frame execution can still reach
-- from the current owner facts. It is deliberately independent of Job status,
-- phase and any payload-declared Dataset. A NULL result means the shape is not
-- proven; an empty result is never produced because every cross-frame Job has
-- an authentic source Task in a Scene Dataset.
CREATE FUNCTION aap_assets_v1._cross_frame_dataset_scope(
    p_project_id uuid, p_actor_id uuid, p_payload jsonb
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
REVOKE ALL ON FUNCTION aap_assets_v1._cross_frame_dataset_scope(uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
-- Jobs calls this from its AFTER INSERT receipt capture in the new Job's birth
-- transaction. Assets never reads the Jobs table; the caller supplies the
-- authoritative born project/actor/payload.
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
BEGIN
    IF p_job_id IS NULL OR p_project_id IS NULL OR p_actor_id IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN aap_assets_v1._cross_frame_dataset_scope(
        p_project_id, p_actor_id, p_payload
    );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.cross_frame_born_dataset_scope(uuid,uuid,uuid,jsonb) FROM PUBLIC;
-- aap-statement-break
-- Bounded executor fence. A born cross-frame Job may execute only while the
-- authoritative current Job and payload still derive exactly its immutable
-- receipt. No receipt keeps historical/unproven execution unchanged and takes
-- no caller-supplied locks or task lookups.
CREATE FUNCTION aap_assets_v1.fence_cross_frame_job_dataset_scope(
    p_job_id uuid, p_project_id uuid, p_actor_id uuid, p_payload jsonb
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_birth record;
    v_born uuid[];
    v_current uuid[];
    v_task_ids uuid[];
    v_scene uuid;
BEGIN
    IF p_job_id IS NULL THEN
        RETURN false;
    END IF;
    SELECT birth.project_id, birth.actor_id, birth.dataset_ids
    INTO v_birth
    FROM aap_jobs_v1.cross_frame_job_birth(p_job_id) AS birth;
    IF NOT FOUND THEN
        RETURN true;
    END IF;
    IF v_birth.project_id IS DISTINCT FROM p_project_id
       OR v_birth.actor_id IS DISTINCT FROM p_actor_id THEN
        RETURN false;
    END IF;
    v_born := ARRAY(
        SELECT DISTINCT ids.id
        FROM pg_catalog.unnest(v_birth.dataset_ids) AS ids(id)
        WHERE ids.id IS NOT NULL
        ORDER BY ids.id
    );
    v_current := aap_assets_v1._cross_frame_dataset_scope(
        p_project_id, p_actor_id, p_payload
    );
    IF v_current IS NULL OR v_born IS DISTINCT FROM v_current THEN
        RETURN false;
    END IF;
    -- Only after the receipt and current facts agree, derive the bounded Task
    -- guard set from the validated payload and freeze its relations.
    v_task_ids := ARRAY(
        SELECT DISTINCT ids.id
        FROM (
            SELECT aap_assets_v1._cross_frame_scope_uuid(
                p_payload -> 'source_task_id'
            ) AS id
            UNION
            SELECT aap_assets_v1._cross_frame_scope_uuid(
                target.value -> 'task_id'
            )
            FROM pg_catalog.jsonb_array_elements(
                p_payload -> 'targets'
            ) AS target(value)
        ) AS ids(id)
        WHERE ids.id IS NOT NULL
        ORDER BY ids.id
    );
    IF pg_catalog.cardinality(v_task_ids) > 0 THEN
        PERFORM task.id
        FROM public.tasks AS task
        WHERE task.id = ANY(v_task_ids)
        ORDER BY task.id
        FOR SHARE;
        PERFORM link.id
        FROM public.task_dataset_item_links AS link
        WHERE link.task_id = ANY(v_task_ids)
        ORDER BY link.task_id, link.id
        FOR SHARE;
        PERFORM item.id
        FROM public.dataset_items AS item
        WHERE item.id IN (
            SELECT source_task.dataset_item_id
            FROM aap_labeling_v1.task_rows AS source_task
            WHERE source_task.id = ANY(v_task_ids)
              AND source_task.dataset_item_id IS NOT NULL
            UNION
            SELECT linked.dataset_item_id
            FROM public.task_dataset_item_links AS linked
            WHERE linked.task_id = ANY(v_task_ids)
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
    RETURN v_born IS NOT DISTINCT FROM
        aap_assets_v1._cross_frame_dataset_scope(
            p_project_id, p_actor_id, p_payload
        );
END
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_assets_v1.fence_cross_frame_job_dataset_scope(uuid,uuid,uuid,jsonb) FROM PUBLIC;
