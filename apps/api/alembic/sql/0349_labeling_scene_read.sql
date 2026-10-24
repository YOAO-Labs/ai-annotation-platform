CREATE FUNCTION aap_labeling_v1._scene_read_track_json(
    p_id uuid,
    p_project_id uuid,
    p_scene_id uuid,
    p_track_id text,
    p_class_name text,
    p_presence_mode text,
    p_attributes jsonb,
    p_attributes_meta jsonb,
    p_revision integer,
    p_retired_at timestamptz
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_id,
        'project_id', p_project_id,
        'scene_id', p_scene_id,
        'track_id', p_track_id,
        'class_name', p_class_name,
        'presence_mode', p_presence_mode,
        'attributes', p_attributes,
        'attributes_meta', p_attributes_meta,
        'revision', p_revision,
        'retired_at', p_retired_at
    )
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._scene_read_interval_json(
    p_id uuid,
    p_scene_track_id uuid,
    p_start_frame integer,
    p_end_frame integer,
    p_source text,
    p_version integer
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_id,
        'scene_track_id', p_scene_track_id,
        'start_frame', p_start_frame,
        'end_frame', p_end_frame,
        'source', p_source,
        'version', p_version
    )
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1._scene_read_annotation_json(
    p_id uuid,
    p_task_id uuid,
    p_project_id uuid,
    p_source text,
    p_annotation_type text,
    p_class_name text,
    p_geometry jsonb,
    p_track_id text,
    p_scene_track_id uuid,
    p_sensor_role text,
    p_temporal_role text,
    p_is_active boolean,
    p_was_cancelled boolean,
    p_is_locked boolean,
    p_is_hidden boolean,
    p_version integer,
    p_created_at timestamptz,
    p_frame_index integer DEFAULT NULL
)
RETURNS jsonb
LANGUAGE sql
IMMUTABLE
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'id', p_id,
        'task_id', p_task_id,
        'project_id', p_project_id,
        'source', p_source,
        'annotation_type', p_annotation_type,
        'class_name', p_class_name,
        'geometry', p_geometry,
        'track_id', p_track_id,
        'scene_track_id', p_scene_track_id,
        'sensor_role', p_sensor_role,
        'temporal_role', p_temporal_role,
        'is_active', p_is_active,
        'was_cancelled', p_was_cancelled,
        'is_locked', p_is_locked,
        'is_hidden', p_is_hidden,
        'version', p_version,
        'created_at', p_created_at,
        'frame_index', p_frame_index
    )
$function$;
-- aap-statement-break
CREATE FUNCTION aap_labeling_v1.scene_read_query(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_action text := p_request ->> 'action';
    v_project_id uuid;
    v_scene_id uuid;
    v_track_id text;
    v_primary_item_id uuid;
    v_data_frames jsonb;
    v_track_pk uuid;
    v_track_project_id uuid;
    v_track_scene_id uuid;
    v_track_class_name text;
    v_track_presence_mode text;
    v_track_attributes jsonb;
    v_track_attributes_meta jsonb;
    v_track_revision integer;
    v_track_retired_at timestamptz;
    v_track_updated_at timestamptz;
    v_track_found boolean := false;
    v_required_keys text[];
    v_required_key_count integer;
    v_element jsonb;
    v_text text;
    v_limit integer;
    v_limit_numeric numeric;
    v_data jsonb := pg_catalog.jsonb_build_object(
        'primary_item_id', NULL,
        'frames', '[]'::jsonb,
        'frame_tasks', '[]'::jsonb,
        'timeline_tasks', '[]'::jsonb,
        'annotation_summaries', '[]'::jsonb,
        'track', NULL,
        'tracks', '[]'::jsonb,
        'track_cache', NULL,
        'camera_members', '[]'::jsonb,
        'intervals', '[]'::jsonb,
        'members', '[]'::jsonb,
        'candidate_counts', '[]'::jsonb
    );
BEGIN
    IF p_request IS NULL OR pg_catalog.jsonb_typeof(p_request) <> 'object'
       OR pg_catalog.jsonb_typeof(p_request -> 'schema') <> 'string'
       OR p_request ->> 'schema' <> 'aap-labeling-scene-read/v1'
       OR pg_catalog.jsonb_typeof(p_request -> 'action') <> 'string'
       OR v_action IS NULL THEN
        RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
    END IF;

    CASE v_action
        WHEN 'resolve_primary_link' THEN
            v_required_keys := ARRAY['schema', 'action', 'task_id'];
            v_required_key_count := 3;
        WHEN 'resolve_task_frames' THEN
            v_required_keys := ARRAY['schema', 'action', 'task_ids'];
            v_required_key_count := 3;
        WHEN 'scene_frame_tasks' THEN
            v_required_keys := ARRAY['schema', 'action', 'scene_id'];
            v_required_key_count := 3;
        WHEN 'timeline_tasks' THEN
            v_required_keys := ARRAY['schema', 'action', 'project_id', 'item_ids'];
            v_required_key_count := 4;
        WHEN 'timeline_annotation_summaries' THEN
            v_required_keys := ARRAY['schema', 'action', 'task_ids', 'track_id'];
            v_required_key_count := 4;
        WHEN 'load_command_track', 'load_track_detail', 'load_timeline_track' THEN
            v_required_keys := ARRAY['schema', 'action', 'project_id', 'scene_id', 'track_id'];
            v_required_key_count := 5;
        WHEN 'load_scene_camera_cache' THEN
            v_required_keys := ARRAY['schema', 'action', 'scene_track_id', 'annotation_ids'];
            v_required_key_count := 4;
        WHEN 'load_scene_diagnostics' THEN
            v_required_keys := ARRAY['schema', 'action', 'project_id', 'scene_id'];
            v_required_key_count := 4;
        WHEN 'load_scene_track_cache' THEN
            v_required_keys := ARRAY['schema', 'action', 'scene_track_id'];
            v_required_key_count := 3;
        WHEN 'read_track_members', 'lock_track_members' THEN
            v_required_keys := ARRAY['schema', 'action', 'project_id', 'track_ids', 'limit'];
            v_required_key_count := 5;
        WHEN 'candidate_track_counts' THEN
            v_required_keys := ARRAY[
                'schema', 'action', 'project_id', 'task_ids', 'class_name',
                'exclude_track_id', 'limit'
            ];
            v_required_key_count := 7;
        ELSE
            RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
    END CASE;
    IF pg_catalog.jsonb_object_length(p_request) <> v_required_key_count
       OR NOT (p_request ?& v_required_keys) THEN
        RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
    END IF;

    CASE v_action
        WHEN 'resolve_primary_link' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'task_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            PERFORM (p_request ->> 'task_id')::uuid;
        WHEN 'resolve_task_frames' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'task_ids') <> 'array' THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            FOR v_element IN
                SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'task_ids') AS input_values(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_element) <> 'string' THEN
                    RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
                END IF;
                PERFORM (v_element #>> '{}')::uuid;
            END LOOP;
        WHEN 'scene_frame_tasks' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'scene_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            PERFORM (p_request ->> 'scene_id')::uuid;
        WHEN 'timeline_tasks' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'item_ids') <> 'array' THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            PERFORM (p_request ->> 'project_id')::uuid;
            FOR v_element IN
                SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'item_ids') AS input_values(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_element) <> 'string' THEN
                    RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
                END IF;
                PERFORM (v_element #>> '{}')::uuid;
            END LOOP;
        WHEN 'timeline_annotation_summaries' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'task_ids') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'track_id') NOT IN ('string', 'null') THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            FOR v_element IN
                SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'task_ids') AS input_values(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_element) <> 'string' THEN
                    RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
                END IF;
                PERFORM (v_element #>> '{}')::uuid;
            END LOOP;
        WHEN 'load_command_track', 'load_track_detail', 'load_timeline_track' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'scene_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'track_id') <> 'string'
               OR pg_catalog.char_length(p_request ->> 'track_id') NOT BETWEEN 1 AND 64 THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            PERFORM (p_request ->> 'project_id')::uuid;
            PERFORM (p_request ->> 'scene_id')::uuid;
        WHEN 'load_scene_diagnostics' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'scene_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            PERFORM (p_request ->> 'project_id')::uuid;
            PERFORM (p_request ->> 'scene_id')::uuid;
        WHEN 'load_scene_track_cache' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'scene_track_id') <> 'string' THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            PERFORM (p_request ->> 'scene_track_id')::uuid;
        WHEN 'load_scene_camera_cache' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'scene_track_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'annotation_ids') <> 'array' THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            PERFORM (p_request ->> 'scene_track_id')::uuid;
            FOR v_element IN
                SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'annotation_ids') AS input_values(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_element) <> 'string' THEN
                    RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
                END IF;
                PERFORM (v_element #>> '{}')::uuid;
            END LOOP;
        WHEN 'read_track_members', 'lock_track_members' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'track_ids') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'limit') <> 'number'
               OR pg_catalog.jsonb_array_length(p_request -> 'track_ids') NOT BETWEEN 1 AND 80 THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            PERFORM (p_request ->> 'project_id')::uuid;
            FOR v_element IN
                SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'track_ids') AS input_values(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_element) <> 'string'
                   OR pg_catalog.char_length(v_element #>> '{}') NOT BETWEEN 1 AND 64 THEN
                    RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
                END IF;
            END LOOP;
            v_text := p_request ->> 'limit';
            IF v_text !~ '^(0|[1-9][0-9]*)$' THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            v_limit_numeric := v_text::numeric;
            IF v_limit_numeric NOT BETWEEN 1 AND 20001 THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            v_limit := v_limit_numeric::integer;
        WHEN 'candidate_track_counts' THEN
            IF pg_catalog.jsonb_typeof(p_request -> 'project_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'task_ids') <> 'array'
               OR pg_catalog.jsonb_typeof(p_request -> 'class_name') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'exclude_track_id') <> 'string'
               OR pg_catalog.jsonb_typeof(p_request -> 'limit') <> 'number' THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            PERFORM (p_request ->> 'project_id')::uuid;
            FOR v_element IN
                SELECT value FROM pg_catalog.jsonb_array_elements(p_request -> 'task_ids') AS input_values(value)
            LOOP
                IF pg_catalog.jsonb_typeof(v_element) <> 'string' THEN
                    RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
                END IF;
                PERFORM (v_element #>> '{}')::uuid;
            END LOOP;
            v_text := p_request ->> 'limit';
            IF v_text !~ '^(0|[1-9][0-9]*)$' THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            v_limit_numeric := v_text::numeric;
            IF v_limit_numeric NOT BETWEEN 1 AND 81 THEN
                RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
            END IF;
            v_limit := v_limit_numeric::integer;
        ELSE
            RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
    END CASE;

    CASE v_action
        WHEN 'resolve_primary_link' THEN
            SELECT l.dataset_item_id
              INTO v_primary_item_id
              FROM public.task_dataset_item_links AS l
             WHERE l.task_id = (p_request ->> 'task_id')::uuid
               AND l.role = 'primary_lidar'
             LIMIT 1;
            v_data := v_data || pg_catalog.jsonb_build_object(
                'primary_item_id', v_primary_item_id
            );

        WHEN 'resolve_task_frames' THEN
            WITH requested AS (
                SELECT DISTINCT ON (value::uuid)
                       value::uuid AS task_id,
                       ordinality
                FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids')
                     WITH ORDINALITY AS input_values(value, ordinality)
                ORDER BY value::uuid, ordinality
            ), task_items AS (
                SELECT r.task_id,
                       r.ordinality,
                       t.dataset_item_id AS direct_item_id,
                       linked.dataset_item_id AS linked_item_id
                FROM requested AS r
                LEFT JOIN public.tasks AS t ON t.id = r.task_id
                LEFT JOIN LATERAL (
                    SELECT l.dataset_item_id
                    FROM public.task_dataset_item_links AS l
                    WHERE l.task_id = r.task_id
                      AND l.role = 'primary_lidar'
                    LIMIT 1
                ) AS linked ON true
            ), resolved AS (
                SELECT ti.task_id,
                       ti.ordinality,
                       CASE WHEN direct.scene_id IS NOT NULL
                            THEN ti.direct_item_id
                            WHEN linked.scene_id IS NOT NULL THEN ti.linked_item_id
                            ELSE NULL END AS item_id,
                       CASE WHEN direct.scene_id IS NOT NULL
                            THEN direct.scene_id ELSE linked.scene_id END AS scene_id,
                       CASE WHEN direct.scene_id IS NOT NULL
                            THEN direct.scene_name ELSE linked.scene_name END AS scene_name,
                       CASE WHEN direct.scene_id IS NOT NULL
                            THEN direct.frame_index ELSE linked.frame_index END AS frame_index
                FROM task_items AS ti
                LEFT JOIN aap_assets_v1.spatial_item_references AS direct
                       ON direct.item_id = ti.direct_item_id
                LEFT JOIN aap_assets_v1.spatial_item_references AS linked
                       ON linked.item_id = ti.linked_item_id
            )
            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.jsonb_build_object(
                               'task_id', task_id,
                               'item_id', item_id,
                               'scene_id', scene_id,
                               'scene_name', scene_name,
                               'frame_index', frame_index
                           ) ORDER BY ordinality
                       ),
                       '[]'::jsonb
                   )
              INTO v_data_frames
              FROM resolved;
            v_data := v_data || pg_catalog.jsonb_build_object('frames', v_data_frames);

        WHEN 'scene_frame_tasks' THEN
            v_scene_id := (p_request ->> 'scene_id')::uuid;
            WITH scene_items AS (
                SELECT item_id, frame_index
                FROM aap_assets_v1.spatial_item_references
                WHERE scene_id = v_scene_id
                  AND frame_index IS NOT NULL
            ), direct_by_item AS (
                SELECT DISTINCT ON (t.dataset_item_id)
                       t.dataset_item_id AS item_id,
                       t.id AS task_id
                FROM public.tasks AS t
                JOIN scene_items AS i ON i.item_id = t.dataset_item_id
                ORDER BY t.dataset_item_id, t.id DESC
            ), linked_by_item AS (
                SELECT DISTINCT ON (l.dataset_item_id)
                       l.dataset_item_id AS item_id,
                       l.task_id
                FROM public.task_dataset_item_links AS l
                JOIN scene_items AS i ON i.item_id = l.dataset_item_id
                WHERE l.role = 'primary_lidar'
                ORDER BY l.dataset_item_id, l.task_id
            ), item_tasks AS (
                SELECT i.item_id,
                       i.frame_index,
                       COALESCE(d.task_id, l.task_id) AS task_id
                FROM scene_items AS i
                LEFT JOIN direct_by_item AS d ON d.item_id = i.item_id
                LEFT JOIN linked_by_item AS l ON l.item_id = i.item_id
            ), frame_tasks AS (
                SELECT DISTINCT ON (frame_index) frame_index, task_id
                FROM item_tasks
                WHERE task_id IS NOT NULL
                ORDER BY frame_index, item_id
            )
            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.jsonb_build_object(
                               'frame_index', frame_index,
                               'task_id', task_id
                           ) ORDER BY frame_index
                       ),
                       '[]'::jsonb
                   )
              INTO v_data_frames
              FROM frame_tasks;
            v_data := v_data || pg_catalog.jsonb_build_object('frame_tasks', v_data_frames);

        WHEN 'timeline_tasks' THEN
            v_project_id := (p_request ->> 'project_id')::uuid;
            WITH requested AS (
                SELECT DISTINCT ON (value::uuid)
                       value::uuid AS item_id,
                       ordinality
                FROM pg_catalog.jsonb_array_elements_text(p_request -> 'item_ids')
                     WITH ORDINALITY AS input_values(value, ordinality)
                ORDER BY value::uuid, ordinality
            ), candidates AS (
                SELECT t.dataset_item_id AS item_id, t.id AS task_id, t.status,
                       r.ordinality, 0 AS priority
                FROM requested AS r
                JOIN public.tasks AS t ON t.dataset_item_id = r.item_id
                WHERE t.project_id = v_project_id
                UNION ALL
                SELECT l.dataset_item_id AS item_id, t.id AS task_id, t.status,
                       r.ordinality, 1 AS priority
                FROM requested AS r
                JOIN public.task_dataset_item_links AS l ON l.dataset_item_id = r.item_id
                JOIN public.tasks AS t ON t.id = l.task_id
                WHERE l.role = 'primary_lidar'
                  AND t.project_id = v_project_id
            ), selected AS (
                SELECT DISTINCT ON (item_id) item_id, task_id, status, ordinality, priority
                FROM candidates
                ORDER BY item_id, priority, task_id
            )
            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.jsonb_build_object(
                               'item_id', item_id,
                               'task_id', task_id,
                               'status', status
                           ) ORDER BY (ordinality - 1) / 5000, priority, item_id
                       ),
                       '[]'::jsonb
                   )
              INTO v_data_frames
              FROM selected;
            v_data := v_data || pg_catalog.jsonb_build_object('timeline_tasks', v_data_frames);

        WHEN 'timeline_annotation_summaries' THEN
            WITH requested AS (
                SELECT DISTINCT value::uuid AS task_id
                FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids')
                     AS input_values(value)
            ), counts AS (
                SELECT a.task_id, pg_catalog.count(a.id) AS annotation_count
                FROM public.annotations AS a
                JOIN requested AS r ON r.task_id = a.task_id
                WHERE a.annotation_type IN ('box_3d', 'point_mask_3d')
                  AND a.is_active IS TRUE
                  AND a.was_cancelled IS FALSE
                GROUP BY a.task_id
            ), selected AS (
                SELECT DISTINCT ON (a.task_id)
                       a.task_id,
                       a.id AS annotation_id,
                       a.source,
                       a.class_name,
                       a.temporal_role
                FROM public.annotations AS a
                JOIN requested AS r ON r.task_id = a.task_id
                WHERE a.annotation_type IN ('box_3d', 'point_mask_3d')
                  AND a.is_active IS TRUE
                  AND a.was_cancelled IS FALSE
                  AND COALESCE(p_request ->> 'track_id', '') <> ''
                  AND a.track_id = p_request ->> 'track_id'
                ORDER BY a.task_id, a.created_at, a.id
            ), summaries AS (
                SELECT c.task_id,
                       c.annotation_count,
                       s.annotation_id,
                       s.source,
                       s.class_name,
                       s.temporal_role
                FROM counts AS c
                LEFT JOIN selected AS s ON s.task_id = c.task_id
                UNION ALL
                SELECT s.task_id, 0, s.annotation_id, s.source, s.class_name, s.temporal_role
                FROM selected AS s
                WHERE NOT EXISTS (SELECT 1 FROM counts AS c WHERE c.task_id = s.task_id)
            )
            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.jsonb_build_object(
                               'task_id', task_id,
                               'annotation_count', annotation_count,
                               'selected_annotation_id', annotation_id,
                               'selected_source', source,
                               'selected_class_name', class_name,
                               'selected_temporal_role', temporal_role
                           ) ORDER BY task_id
                       ),
                       '[]'::jsonb
                   )
              INTO v_data_frames
              FROM summaries;
            v_data := v_data || pg_catalog.jsonb_build_object('annotation_summaries', v_data_frames);

        WHEN 'load_command_track' OR 'load_track_detail' OR 'load_timeline_track' THEN
            v_project_id := (p_request ->> 'project_id')::uuid;
            v_scene_id := (p_request ->> 'scene_id')::uuid;
            v_track_id := p_request ->> 'track_id';
            SELECT t.id,
                   t.project_id,
                   t.scene_id,
                   t.class_name,
                   t.presence_mode,
                   t.attributes,
                   t.attributes_meta,
                   t.revision,
                   t.retired_at
              INTO v_track_pk,
                   v_track_project_id,
                   v_track_scene_id,
                   v_track_class_name,
                   v_track_presence_mode,
                   v_track_attributes,
                   v_track_attributes_meta,
                   v_track_revision,
                   v_track_retired_at
              FROM public.scene_tracks AS t
             WHERE t.project_id = v_project_id
               AND t.scene_id = v_scene_id
               AND t.track_id = v_track_id
               AND (v_action = 'load_command_track' OR t.retired_at IS NULL);
            v_track_found := FOUND;
            IF v_track_found THEN
                v_data := v_data || pg_catalog.jsonb_build_object(
                    'track', aap_labeling_v1._scene_read_track_json(
                        v_track_pk,
                        v_track_project_id,
                        v_track_scene_id,
                        v_track_id,
                        v_track_class_name,
                        v_track_presence_mode,
                        v_track_attributes,
                        v_track_attributes_meta,
                        v_track_revision,
                        v_track_retired_at
                    )
                );
                SELECT COALESCE(
                           pg_catalog.jsonb_agg(
                               aap_labeling_v1._scene_read_interval_json(
                                   i.id,
                                   i.scene_track_id,
                                   i.start_frame,
                                   i.end_frame,
                                   i.source,
                                   i.version
                               )
                               ORDER BY i.start_frame,
                                        CASE WHEN v_action = 'load_command_track' THEN i.id END
                           ),
                           '[]'::jsonb
                       )
                  INTO v_data_frames
                  FROM public.scene_track_intervals AS i
                 WHERE i.scene_track_id = v_track_pk;
                v_data := v_data || pg_catalog.jsonb_build_object('intervals', v_data_frames);
                IF v_action IN ('load_command_track', 'load_track_detail') THEN
                    SELECT COALESCE(
                               pg_catalog.jsonb_agg(
                                   aap_labeling_v1._scene_read_annotation_json(
                                       a.id,
                                       a.task_id,
                                       a.project_id,
                                       a.source,
                                       a.annotation_type,
                                       a.class_name,
                                       a.geometry,
                                       a.track_id,
                                       a.scene_track_id,
                                       a.sensor_role,
                                       a.temporal_role,
                                       a.is_active,
                                       a.was_cancelled,
                                       a.is_locked,
                                       a.is_hidden,
                                       a.version,
                                       a.created_at
                                   )
                                   ORDER BY CASE WHEN v_action = 'load_command_track' THEN a.id END,
                                            CASE WHEN v_action = 'load_track_detail' THEN a.task_id END,
                                            CASE WHEN v_action = 'load_track_detail' THEN a.id END
                               ),
                               '[]'::jsonb
                           )
                      INTO v_data_frames
                      FROM public.annotations AS a
                     WHERE a.scene_track_id = v_track_pk
                       AND a.was_cancelled IS FALSE
                       AND (v_action = 'load_command_track' OR a.is_active IS TRUE)
                       AND (v_action <> 'load_command_track' OR a.id IN (
                           SELECT limited.id
                           FROM (
                               SELECT m.id
                               FROM public.annotations AS m
                               WHERE m.scene_track_id = v_track_pk
                                 AND m.was_cancelled IS FALSE
                               ORDER BY m.id
                               LIMIT 5001
                           ) AS limited
                       ));
                    v_data := v_data || pg_catalog.jsonb_build_object('members', v_data_frames);
                END IF;
            END IF;

        WHEN 'load_scene_diagnostics' THEN
            v_project_id := (p_request ->> 'project_id')::uuid;
            v_scene_id := (p_request ->> 'scene_id')::uuid;
            WITH scene_items AS (
                SELECT item_id, frame_index
                FROM aap_assets_v1.spatial_item_references
                WHERE scene_id = v_scene_id
                  AND frame_index IS NOT NULL
            ), direct_by_item AS (
                SELECT DISTINCT ON (t.dataset_item_id) t.dataset_item_id AS item_id, t.id AS task_id
                FROM public.tasks AS t
                JOIN scene_items AS i ON i.item_id = t.dataset_item_id
                ORDER BY t.dataset_item_id, t.id DESC
            ), linked_by_item AS (
                SELECT DISTINCT ON (l.dataset_item_id) l.dataset_item_id AS item_id, l.task_id
                FROM public.task_dataset_item_links AS l
                JOIN scene_items AS i ON i.item_id = l.dataset_item_id
                WHERE l.role = 'primary_lidar'
                ORDER BY l.dataset_item_id, l.task_id
            ), item_tasks AS (
                SELECT i.item_id, i.frame_index, COALESCE(d.task_id, l.task_id) AS task_id
                FROM scene_items AS i
                LEFT JOIN direct_by_item AS d ON d.item_id = i.item_id
                LEFT JOIN linked_by_item AS l ON l.item_id = i.item_id
            ), frame_tasks AS (
                SELECT DISTINCT ON (frame_index) frame_index, task_id
                FROM item_tasks
                WHERE task_id IS NOT NULL
                ORDER BY frame_index, item_id
            )
            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.jsonb_build_object(
                               'frame_index', frame_index,
                               'task_id', task_id
                           ) ORDER BY frame_index
                       ),
                       '[]'::jsonb
                   )
              INTO v_data_frames
              FROM frame_tasks;
            v_data := v_data || pg_catalog.jsonb_build_object('frame_tasks', v_data_frames);

            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           aap_labeling_v1._scene_read_track_json(
                               t.id,
                               t.project_id,
                               t.scene_id,
                               t.track_id,
                               t.class_name,
                               t.presence_mode,
                               t.attributes,
                               t.attributes_meta,
                               t.revision,
                               t.retired_at
                           ) ORDER BY t.track_id
                       ),
                       '[]'::jsonb
                   )
              INTO v_data_frames
              FROM public.scene_tracks AS t
             WHERE t.project_id = v_project_id
               AND t.scene_id = v_scene_id
               AND t.retired_at IS NULL;
            v_data := v_data || pg_catalog.jsonb_build_object('tracks', v_data_frames);

            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           aap_labeling_v1._scene_read_interval_json(
                               i.id,
                               i.scene_track_id,
                               i.start_frame,
                               i.end_frame,
                               i.source,
                               i.version
                           )
                           ORDER BY i.scene_track_id, i.start_frame
                       ),
                       '[]'::jsonb
                   )
              INTO v_data_frames
              FROM public.scene_track_intervals AS i
              JOIN public.scene_tracks AS t ON t.id = i.scene_track_id
             WHERE t.project_id = v_project_id
               AND t.scene_id = v_scene_id;
            v_data := v_data || pg_catalog.jsonb_build_object('intervals', v_data_frames);

            WITH scene_items AS (
                SELECT item_id, frame_index
                FROM aap_assets_v1.spatial_item_references
                WHERE scene_id = v_scene_id
                  AND frame_index IS NOT NULL
            ), direct_by_item AS (
                SELECT DISTINCT ON (t.dataset_item_id) t.dataset_item_id AS item_id, t.id AS task_id
                FROM public.tasks AS t
                JOIN scene_items AS i ON i.item_id = t.dataset_item_id
                ORDER BY t.dataset_item_id, t.id DESC
            ), linked_by_item AS (
                SELECT DISTINCT ON (l.dataset_item_id) l.dataset_item_id AS item_id, l.task_id
                FROM public.task_dataset_item_links AS l
                JOIN scene_items AS i ON i.item_id = l.dataset_item_id
                WHERE l.role = 'primary_lidar'
                ORDER BY l.dataset_item_id, l.task_id
            ), item_tasks AS (
                SELECT i.item_id, i.frame_index, COALESCE(d.task_id, l.task_id) AS task_id
                FROM scene_items AS i
                LEFT JOIN direct_by_item AS d ON d.item_id = i.item_id
                LEFT JOIN linked_by_item AS l ON l.item_id = i.item_id
            ), frame_tasks AS (
                SELECT DISTINCT ON (frame_index) frame_index, task_id
                FROM item_tasks
                WHERE task_id IS NOT NULL
                ORDER BY frame_index, item_id
            ), task_frames AS (
                SELECT task_id, pg_catalog.max(frame_index) AS frame_index
                FROM frame_tasks
                GROUP BY task_id
            )
            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           aap_labeling_v1._scene_read_annotation_json(
                               a.id,
                               a.task_id,
                               a.project_id,
                               a.source,
                               a.annotation_type,
                               a.class_name,
                               a.geometry,
                               a.track_id,
                               a.scene_track_id,
                               a.sensor_role,
                               a.temporal_role,
                               a.is_active,
                               a.was_cancelled,
                               a.is_locked,
                               a.is_hidden,
                               a.version,
                               a.created_at,
                               tf.frame_index
                           )
                           ORDER BY a.track_id, a.task_id, a.id
                       ),
                       '[]'::jsonb
                   )
              INTO v_data_frames
              FROM public.annotations AS a
              JOIN task_frames AS tf ON tf.task_id = a.task_id
             WHERE a.project_id = v_project_id
               AND a.annotation_type = 'box_3d'
               AND a.track_id IS NOT NULL
               AND a.is_active IS TRUE
               AND a.was_cancelled IS FALSE;
            v_data := v_data || pg_catalog.jsonb_build_object('members', v_data_frames);

        WHEN 'load_scene_track_cache' THEN
            SELECT t.id, t.class_name, t.revision, t.updated_at
              INTO v_track_pk, v_track_class_name, v_track_revision, v_track_updated_at
              FROM public.scene_tracks AS t
             WHERE t.id = (p_request ->> 'scene_track_id')::uuid;
            IF FOUND THEN
                v_data := v_data || pg_catalog.jsonb_build_object(
                    'track_cache', pg_catalog.jsonb_build_object(
                        'id', v_track_pk,
                        'class_name', v_track_class_name,
                        'revision', v_track_revision,
                        'updated_at', v_track_updated_at
                    )
                );
            END IF;

        WHEN 'load_scene_camera_cache' THEN
            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.jsonb_build_object(
                               'id', a.id,
                               'class_name', a.class_name,
                               'version', a.version,
                               'updated_at', a.updated_at
                           ) ORDER BY a.id
                       ),
                       '[]'::jsonb
                   )
              INTO v_data_frames
              FROM public.annotations AS a
             WHERE a.scene_track_id = (p_request ->> 'scene_track_id')::uuid
               AND a.id = ANY (
                   ARRAY(
                       SELECT value::uuid
                       FROM pg_catalog.jsonb_array_elements_text(p_request -> 'annotation_ids')
                            AS input_values(value)
                   )
               )
               AND a.sensor_role IS NOT NULL
               AND a.was_cancelled IS FALSE;
            v_data := v_data || pg_catalog.jsonb_build_object(
                'camera_members', v_data_frames
            );

        WHEN 'read_track_members' OR 'lock_track_members' THEN
            v_project_id := (p_request ->> 'project_id')::uuid;
            IF v_action = 'lock_track_members' THEN
                WITH selected AS MATERIALIZED (
                    SELECT a.id,
                           a.task_id,
                           a.project_id,
                           a.source,
                           a.annotation_type,
                           a.class_name,
                           a.geometry,
                           a.track_id,
                           a.scene_track_id,
                           a.sensor_role,
                           a.temporal_role,
                           a.is_active,
                           a.was_cancelled,
                           a.is_locked,
                           a.is_hidden,
                           a.version,
                           a.created_at
                    FROM public.annotations AS a
                    WHERE a.project_id = v_project_id
                      AND a.track_id = ANY (
                          ARRAY(SELECT value FROM pg_catalog.jsonb_array_elements_text(p_request -> 'track_ids'))
                      )
                      AND a.is_active IS TRUE
                      AND a.was_cancelled IS FALSE
                    ORDER BY a.id
                    LIMIT (p_request ->> 'limit')::integer
                    FOR UPDATE
                )
                SELECT COALESCE(
                           pg_catalog.jsonb_agg(
                               aap_labeling_v1._scene_read_annotation_json(
                                   a.id,
                                   a.task_id,
                                   a.project_id,
                                   a.source,
                                   a.annotation_type,
                                   a.class_name,
                                   a.geometry,
                                   a.track_id,
                                   a.scene_track_id,
                                   a.sensor_role,
                                   a.temporal_role,
                                   a.is_active,
                                   a.was_cancelled,
                                   a.is_locked,
                                   a.is_hidden,
                                   a.version,
                                   a.created_at
                               ) ORDER BY a.id
                           ),
                           '[]'::jsonb
                       )
                  INTO v_data_frames
                  FROM selected AS a;
            ELSE
                SELECT COALESCE(
                           pg_catalog.jsonb_agg(
                               aap_labeling_v1._scene_read_annotation_json(
                                   a.id,
                                   a.task_id,
                                   a.project_id,
                                   a.source,
                                   a.annotation_type,
                                   a.class_name,
                                   a.geometry,
                                   a.track_id,
                                   a.scene_track_id,
                                   a.sensor_role,
                                   a.temporal_role,
                                   a.is_active,
                                   a.was_cancelled,
                                   a.is_locked,
                                   a.is_hidden,
                                   a.version,
                                   a.created_at
                               ) ORDER BY a.id
                           ),
                           '[]'::jsonb
                       )
                  INTO v_data_frames
                  FROM (
                      SELECT a.id,
                             a.task_id,
                             a.project_id,
                             a.source,
                             a.annotation_type,
                             a.class_name,
                             a.geometry,
                             a.track_id,
                             a.scene_track_id,
                             a.sensor_role,
                             a.temporal_role,
                             a.is_active,
                             a.was_cancelled,
                             a.is_locked,
                             a.is_hidden,
                             a.version,
                             a.created_at
                      FROM public.annotations AS a
                      WHERE a.project_id = v_project_id
                        AND a.track_id = ANY (
                            ARRAY(SELECT value FROM pg_catalog.jsonb_array_elements_text(p_request -> 'track_ids'))
                        )
                        AND a.is_active IS TRUE
                        AND a.was_cancelled IS FALSE
                      ORDER BY a.id
                      LIMIT (p_request ->> 'limit')::integer
                  ) AS a;
            END IF;
            v_data := v_data || pg_catalog.jsonb_build_object('members', v_data_frames);

        WHEN 'candidate_track_counts' THEN
            v_project_id := (p_request ->> 'project_id')::uuid;
            SELECT COALESCE(
                       pg_catalog.jsonb_agg(
                           pg_catalog.jsonb_build_object(
                               'track_id', candidates.track_id,
                               'member_count', candidates.member_count
                           ) ORDER BY candidates.track_id
                       ),
                       '[]'::jsonb
                   )
              INTO v_data_frames
              FROM (
                  SELECT a.track_id, pg_catalog.count(a.id) AS member_count
                  FROM public.annotations AS a
                  WHERE a.project_id = v_project_id
                    AND a.task_id = ANY (
                        ARRAY(SELECT value::uuid FROM pg_catalog.jsonb_array_elements_text(p_request -> 'task_ids'))
                    )
                    AND a.is_active IS TRUE
                    AND a.was_cancelled IS FALSE
                    AND a.geometry ->> 'type' = 'box_3d'
                    AND a.class_name = (p_request ->> 'class_name')
                    AND a.track_id IS NOT NULL
                    AND a.track_id <> (p_request ->> 'exclude_track_id')
                  GROUP BY a.track_id
                  ORDER BY a.track_id
                  LIMIT (p_request ->> 'limit')::integer
              ) AS candidates;
            v_data := v_data || pg_catalog.jsonb_build_object('candidate_counts', v_data_frames);

        ELSE
            RAISE EXCEPTION 'unsupported Labeling scene read action' USING ERRCODE = '22023';
    END CASE;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-labeling-scene-read/v1',
        'action', v_action,
        'request', p_request,
        'data', v_data
    );
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'invalid Labeling scene read request' USING ERRCODE = '22023';
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._scene_read_track_json(
    uuid, uuid, uuid, text, text, text, jsonb, jsonb, integer, timestamptz
) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._scene_read_interval_json(
    uuid, uuid, integer, integer, text, integer
) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1._scene_read_annotation_json(
    uuid, uuid, uuid, text, text, text, jsonb, text, uuid, text, text,
    boolean, boolean, boolean, boolean, integer, timestamptz, integer
) FROM PUBLIC;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.scene_read_query(jsonb) FROM PUBLIC;
