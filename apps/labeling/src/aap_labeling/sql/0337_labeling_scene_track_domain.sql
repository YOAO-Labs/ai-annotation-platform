CREATE FUNCTION aap_labeling_v1.scene_track_domain_command(p_request jsonb)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, aap_labeling_v1
AS $function$
DECLARE
    v_action text;
    v_keys text[];
    v_expected_keys text[];
    v_error_code text;
    v_error_message text;
    v_data jsonb;
    v_project_id uuid;
    v_scene_id uuid;
    v_track_id text;
    v_class_name text;
    v_actor_id uuid;
    v_interval_source text;
    v_member_change boolean;
    v_frames integer[] := ARRAY[]::integer[];
    v_frame_json jsonb;
    v_frame_text text;
    v_frame_value numeric;
    v_frame integer;
    v_min_frame integer;
    v_max_frame integer;
    v_inserted_id uuid;
    v_created boolean := false;
    v_revision_changed boolean := false;
    v_track public.scene_tracks%ROWTYPE;
    v_interval record;
    v_interval_count integer := 0;
    v_keeper_id uuid;
    v_keeper_start integer;
    v_keeper_end integer;
    v_extra_interval_ids uuid[] := ARRAY[]::uuid[];
    v_next_start integer;
    v_next_end integer;
    v_has_open_interval boolean := false;
    v_max_interval_end integer;
    v_interval_changed boolean := false;
    v_annotation_id uuid;
    v_scene_track_id uuid;
    v_annotation_scene_track_id uuid;
    v_annotation_track_id text;
    v_annotation_class_name text;
    v_expected_class_name text;
    v_member_count bigint;
    v_camera_member_ids uuid[] := ARRAY[]::uuid[];
    v_camera_members jsonb := '[]'::jsonb;
BEGIN
    IF p_request IS NULL OR jsonb_typeof(p_request) IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_request -> 'schema') IS DISTINCT FROM 'string'
       OR p_request ->> 'schema' <> 'aap-labeling-scene-track-domain/v1'
       OR jsonb_typeof(p_request -> 'action') IS DISTINCT FROM 'string' THEN
        RAISE EXCEPTION 'Invalid Labeling SceneTrack request' USING ERRCODE = '22023';
    END IF;
    v_action := p_request ->> 'action';
    SELECT array_agg(key ORDER BY key) INTO v_keys
    FROM jsonb_object_keys(p_request) AS request_keys(key);

    IF v_action = 'ensure' THEN
        v_expected_keys := ARRAY[
            'action','actor_id','class_name','frames','interval_source','member_change',
            'project_id','scene_id','schema','track_id'
        ]::text[];
    ELSIF v_action = 'touch' THEN
        v_expected_keys := ARRAY[
            'action','annotation_id','class_name','scene_track_id','schema','track_id'
        ]::text[];
    ELSIF v_action = 'reclassify' THEN
        v_expected_keys := ARRAY[
            'action','annotation_id','class_name','expected_class_name','scene_track_id',
            'schema','track_id'
        ]::text[];
    ELSE
        RAISE EXCEPTION 'Invalid Labeling SceneTrack action' USING ERRCODE = '22023';
    END IF;
    IF v_keys IS DISTINCT FROM v_expected_keys THEN
        RAISE EXCEPTION 'Invalid Labeling SceneTrack fields' USING ERRCODE = '22023';
    END IF;

    IF v_action = 'ensure' THEN
        IF jsonb_typeof(p_request -> 'project_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'project_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(p_request -> 'scene_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'scene_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(p_request -> 'track_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'class_name') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'interval_source') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'member_change') IS DISTINCT FROM 'boolean'
           OR jsonb_typeof(p_request -> 'actor_id') NOT IN ('null','string')
           OR (jsonb_typeof(p_request -> 'actor_id') = 'string'
               AND p_request ->> 'actor_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR jsonb_typeof(p_request -> 'frames') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack ensure request' USING ERRCODE = '22023';
        END IF;
        v_project_id := (p_request ->> 'project_id')::uuid;
        v_scene_id := (p_request ->> 'scene_id')::uuid;
        v_track_id := p_request ->> 'track_id';
        v_class_name := p_request ->> 'class_name';
        v_actor_id := NULLIF(p_request ->> 'actor_id', '')::uuid;
        v_interval_source := p_request ->> 'interval_source';
        v_member_change := (p_request ->> 'member_change')::boolean;
        IF v_track_id = '' OR length(v_track_id) > 64 THEN
            v_error_code := 'track_id_invalid';
            v_error_message := 'Scene Track requires a non-empty track_id';
        END IF;

        IF v_error_code IS NULL AND jsonb_array_length(p_request -> 'frames') = 0 THEN
            v_error_code := 'track_frames_invalid';
            v_error_message := 'Scene Track requires non-negative member frames';
        ELSIF v_error_code IS NULL THEN
            FOR v_frame_json IN
                SELECT value FROM jsonb_array_elements(p_request -> 'frames') AS frames(value)
            LOOP
                IF jsonb_typeof(v_frame_json) IS DISTINCT FROM 'number' THEN
                    RAISE EXCEPTION 'Invalid Labeling SceneTrack frame value' USING ERRCODE = '22023';
                END IF;
                v_frame_text := v_frame_json #>> '{}';
                IF v_frame_text !~ '^-?(0|[1-9][0-9]*)$' THEN
                    RAISE EXCEPTION 'Invalid Labeling SceneTrack frame value' USING ERRCODE = '22023';
                END IF;
                IF length(v_frame_text) > 10 THEN
                    v_error_code := 'track_frames_invalid';
                    v_error_message := 'Scene Track requires non-negative member frames';
                    EXIT;
                END IF;
                v_frame_value := v_frame_text::numeric;
                IF v_frame_value < 0 OR v_frame_value > 2147483647 THEN
                    v_error_code := 'track_frames_invalid';
                    v_error_message := 'Scene Track requires non-negative member frames';
                    EXIT;
                END IF;
                v_frames := array_append(v_frames, v_frame_value::integer);
            END LOOP;
        END IF;
        IF v_error_code IS NULL AND v_interval_source NOT IN
            ('legacy_envelope','manual','imported','derived') THEN
            v_error_code := 'interval_source_invalid';
            v_error_message := 'unsupported Scene Track interval source';
        END IF;

        IF v_error_code IS NULL THEN
            SELECT min(frame), max(frame) INTO v_min_frame, v_max_frame
            FROM unnest(v_frames) AS frames(frame);

            INSERT INTO public.scene_tracks (
                id, project_id, scene_id, track_id, class_name, created_by
            )
            VALUES (
                gen_random_uuid(), v_project_id, v_scene_id, v_track_id, v_class_name,
                v_actor_id
            )
            ON CONFLICT (project_id, scene_id, track_id) DO NOTHING
            RETURNING id INTO v_inserted_id;
            v_created := v_inserted_id IS NOT NULL;

            SELECT * INTO v_track
            FROM public.scene_tracks AS track
            WHERE track.project_id = v_project_id
              AND track.scene_id = v_scene_id
              AND track.track_id = v_track_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'Labeling SceneTrack row disappeared' USING ERRCODE = '55000';
            ELSIF v_track.class_name IS DISTINCT FROM v_class_name THEN
                v_error_code := 'track_class_conflict';
                v_error_message := 'Scene Track class does not match the annotation class';
            ELSIF v_track.retired_at IS NOT NULL THEN
                v_error_code := 'track_retired';
                v_error_message := 'retired Scene Track cannot accept active members';
            ELSE
                FOR v_interval IN
                    SELECT interval.id, interval.start_frame, interval.end_frame,
                           interval.version
                    FROM public.scene_track_intervals AS interval
                    WHERE interval.scene_track_id = v_track.id
                    ORDER BY interval.start_frame, interval.id
                    FOR UPDATE
                LOOP
                    v_interval_count := v_interval_count + 1;
                    IF v_interval_count = 1 THEN
                        v_keeper_id := v_interval.id;
                        v_keeper_start := v_interval.start_frame;
                        v_keeper_end := v_interval.end_frame;
                    ELSE
                        v_extra_interval_ids := array_append(v_extra_interval_ids, v_interval.id);
                    END IF;
                    IF v_interval.end_frame IS NULL THEN
                        v_has_open_interval := true;
                    ELSE
                        v_max_interval_end := greatest(
                            COALESCE(v_max_interval_end, v_interval.end_frame),
                            v_interval.end_frame
                        );
                    END IF;
                END LOOP;

                IF v_track.presence_mode = 'explicit' THEN
                    FOREACH v_frame IN ARRAY v_frames LOOP
                        IF NOT EXISTS (
                            SELECT 1
                            FROM public.scene_track_intervals AS interval
                            WHERE interval.scene_track_id = v_track.id
                              AND interval.start_frame <= v_frame
                              AND (interval.end_frame IS NULL OR v_frame <= interval.end_frame)
                        ) THEN
                            v_error_code := 'track_frame_absent';
                            v_error_message := 'explicit Scene Track presence must be resumed before writing an absent frame';
                            EXIT;
                        END IF;
                    END LOOP;
                ELSE
                    v_next_start := least(v_min_frame, COALESCE(v_keeper_start, v_min_frame));
                    v_next_end := CASE
                        WHEN v_has_open_interval THEN NULL
                        ELSE greatest(v_max_frame, COALESCE(v_max_interval_end, v_max_frame))
                    END;
                    IF v_interval_count = 0 THEN
                        INSERT INTO public.scene_track_intervals (
                            id, scene_track_id, start_frame, end_frame, source, created_by
                        )
                        VALUES (
                            gen_random_uuid(), v_track.id, v_next_start, v_next_end,
                            v_interval_source, v_actor_id
                        );
                        v_interval_changed := true;
                    ELSE
                        v_interval_changed := v_interval_count <> 1
                            OR v_keeper_start IS DISTINCT FROM v_next_start
                            OR v_keeper_end IS DISTINCT FROM v_next_end;
                        IF v_interval_changed THEN
                            -- Delete extras before widening the keeper to satisfy the exclusion constraint.
                            IF cardinality(v_extra_interval_ids) > 0 THEN
                                DELETE FROM public.scene_track_intervals AS interval
                                WHERE interval.id = ANY(v_extra_interval_ids);
                            END IF;
                            UPDATE public.scene_track_intervals AS interval
                            SET start_frame = v_next_start,
                                end_frame = v_next_end,
                                version = COALESCE(interval.version, 1) + 1,
                                updated_at = clock_timestamp()
                            WHERE interval.id = v_keeper_id;
                        END IF;
                    END IF;
                END IF;

                IF v_error_code IS NULL THEN
                    IF NOT v_created AND (v_member_change OR v_interval_changed) THEN
                        UPDATE public.scene_tracks AS track
                        SET revision = COALESCE(track.revision, 1) + 1,
                            updated_at = clock_timestamp()
                        WHERE track.id = v_track.id
                        RETURNING * INTO v_track;
                        v_revision_changed := true;
                    END IF;
                    v_data := jsonb_build_object(
                        'track', to_jsonb(v_track),
                        'touched', false,
                        'changed', v_revision_changed,
                        'camera_members', '[]'::jsonb
                    );
                END IF;
            END IF;
        END IF;

    ELSIF v_action = 'touch' THEN
        IF jsonb_typeof(p_request -> 'annotation_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'annotation_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(p_request -> 'scene_track_id') NOT IN ('null','string')
           OR (jsonb_typeof(p_request -> 'scene_track_id') = 'string'
               AND p_request ->> 'scene_track_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
           OR jsonb_typeof(p_request -> 'track_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'class_name') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack touch request' USING ERRCODE = '22023';
        END IF;
        v_annotation_id := (p_request ->> 'annotation_id')::uuid;
        v_scene_track_id := NULLIF(p_request ->> 'scene_track_id', '')::uuid;
        v_track_id := p_request ->> 'track_id';
        v_class_name := p_request ->> 'class_name';

        IF v_scene_track_id IS NULL THEN
            v_data := jsonb_build_object(
                'track', NULL, 'touched', false, 'changed', false,
                'camera_members', '[]'::jsonb
            );
        ELSE
            SELECT annotation.scene_track_id
            INTO v_annotation_scene_track_id
            FROM public.annotations AS annotation
            WHERE annotation.id = v_annotation_id;
            IF NOT FOUND THEN
                v_error_code := 'annotation_missing';
                v_error_message := 'Annotation was not found';
            ELSIF v_annotation_scene_track_id IS DISTINCT FROM v_scene_track_id THEN
                v_error_code := 'member_identity_mismatch';
                v_error_message := 'Annotation Scene Track changed before the write';
            ELSE
                SELECT * INTO v_track
                FROM public.scene_tracks AS track
                WHERE track.id = v_scene_track_id
                FOR UPDATE;
                IF NOT FOUND THEN
                    v_error_code := 'track_missing';
                    v_error_message := 'Annotation references a missing Scene Track';
                ELSIF v_track.track_id IS DISTINCT FROM v_track_id
                   OR v_track.class_name IS DISTINCT FROM v_class_name THEN
                    v_error_code := 'member_identity_mismatch';
                    v_error_message := 'Annotation identity does not match its Scene Track';
                ELSE
                    UPDATE public.scene_tracks AS track
                    SET revision = COALESCE(track.revision, 1) + 1,
                        updated_at = clock_timestamp()
                    WHERE track.id = v_scene_track_id
                    RETURNING * INTO v_track;
                    v_data := jsonb_build_object(
                        'track', to_jsonb(v_track), 'touched', true,
                        'changed', false, 'camera_members', '[]'::jsonb
                    );
                END IF;
            END IF;
        END IF;

    ELSE
        IF jsonb_typeof(p_request -> 'annotation_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'annotation_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(p_request -> 'scene_track_id') IS DISTINCT FROM 'string'
           OR p_request ->> 'scene_track_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR jsonb_typeof(p_request -> 'track_id') NOT IN ('null','string')
           OR jsonb_typeof(p_request -> 'expected_class_name') IS DISTINCT FROM 'string'
           OR jsonb_typeof(p_request -> 'class_name') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'Invalid Labeling SceneTrack reclassify request' USING ERRCODE = '22023';
        END IF;
        v_annotation_id := (p_request ->> 'annotation_id')::uuid;
        v_scene_track_id := (p_request ->> 'scene_track_id')::uuid;
        v_track_id := p_request ->> 'track_id';
        v_expected_class_name := p_request ->> 'expected_class_name';
        v_class_name := p_request ->> 'class_name';

        SELECT annotation.scene_track_id, annotation.track_id, annotation.class_name
        INTO v_annotation_scene_track_id, v_annotation_track_id,
             v_annotation_class_name
        FROM public.annotations AS annotation
        WHERE annotation.id = v_annotation_id;
        IF NOT FOUND THEN
            v_error_code := 'annotation_missing';
            v_error_message := 'Annotation was not found';
        ELSIF v_annotation_scene_track_id IS DISTINCT FROM v_scene_track_id
           OR v_annotation_track_id IS DISTINCT FROM v_track_id
           OR v_annotation_class_name IS DISTINCT FROM v_expected_class_name THEN
            v_error_code := 'member_identity_mismatch';
            v_error_message := 'Annotation identity changed before reclassification';
        ELSE
            SELECT * INTO v_track
            FROM public.scene_tracks AS track
            WHERE track.id = v_scene_track_id
            FOR UPDATE;
            IF NOT FOUND THEN
                v_error_code := 'track_missing';
                v_error_message := 'Annotation references a missing Scene Track';
            ELSIF v_track.track_id IS DISTINCT FROM v_track_id
               OR v_track.class_name IS DISTINCT FROM v_expected_class_name THEN
                v_error_code := 'member_identity_mismatch';
                v_error_message := 'Annotation identity does not match its Scene Track';
            ELSIF v_expected_class_name IS NOT DISTINCT FROM v_class_name THEN
                v_data := jsonb_build_object(
                    'track', to_jsonb(v_track), 'touched', false,
                    'changed', false, 'camera_members', '[]'::jsonb
                );
            ELSE
                SELECT count(*) INTO v_member_count
                FROM public.annotations AS annotation
                WHERE annotation.scene_track_id = v_scene_track_id
                  AND annotation.is_active IS TRUE
                  AND annotation.was_cancelled IS FALSE
                  AND annotation.sensor_role IS NULL;
                IF v_member_count <> 1 THEN
                    v_error_code := 'track_class_change_requires_scope';
                    v_error_message := 'change the class through a whole-track operation';
                ELSE
                    SELECT COALESCE(array_agg(annotation.id ORDER BY annotation.id), ARRAY[]::uuid[])
                    INTO v_camera_member_ids
                    FROM (
                        SELECT member.id
                        FROM public.annotations AS member
                        WHERE member.scene_track_id = v_scene_track_id
                          AND member.sensor_role IS NOT NULL
                          AND member.was_cancelled IS FALSE
                        ORDER BY member.id
                        FOR UPDATE
                    ) AS annotation;

                    UPDATE public.scene_tracks AS track
                    SET class_name = v_class_name,
                        revision = COALESCE(track.revision, 1) + 1,
                        updated_at = clock_timestamp()
                    WHERE track.id = v_scene_track_id
                    RETURNING * INTO v_track;

                    WITH changed AS (
                        UPDATE public.annotations AS member
                        SET class_name = v_class_name,
                            version = COALESCE(member.version, 1) + 1,
                            updated_at = clock_timestamp()
                        WHERE member.id = ANY(v_camera_member_ids)
                        RETURNING member.id, member.class_name, member.version,
                                  member.updated_at
                    )
                    SELECT COALESCE(jsonb_agg(to_jsonb(changed) ORDER BY changed.id), '[]'::jsonb)
                    INTO v_camera_members
                    FROM changed;
                    v_data := jsonb_build_object(
                        'track', to_jsonb(v_track), 'touched', false,
                        'changed', true, 'camera_members', v_camera_members
                    );
                END IF;
            END IF;
        END IF;
    END IF;

    IF v_error_code IS NOT NULL THEN
        RETURN jsonb_build_object(
            'schema', 'aap-labeling-scene-track-domain/v1',
            'action', v_action,
            'ok', false,
            'data', NULL,
            'error', jsonb_build_object('code', v_error_code, 'message', v_error_message)
        );
    END IF;
    RETURN jsonb_build_object(
        'schema', 'aap-labeling-scene-track-domain/v1',
        'action', v_action,
        'ok', true,
        'data', v_data,
        'error', NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_labeling_v1.scene_track_domain_command(jsonb) FROM PUBLIC;
