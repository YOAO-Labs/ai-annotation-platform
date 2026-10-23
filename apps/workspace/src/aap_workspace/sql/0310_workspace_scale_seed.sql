-- Fixed capabilities for the local scale-seed workflow. The caller owns every
-- transaction; the functions never commit, retry, delete source bytes, or accept
-- arbitrary SQL/table/predicate selectors.

CREATE FUNCTION aap_workspace_v1.create_scale_seed_project(
    p_owner_id uuid,
    p_marker text
)
RETURNS uuid
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_id uuid;
BEGIN
    IF p_owner_id IS NULL OR p_marker IS NULL OR pg_catalog.btrim(p_marker) = '' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid scale seed Project';
    END IF;

    -- Keep the original script's Project fields and PostgreSQL defaults. The
    -- Labeling owner writes total_tasks separately in the caller transaction.
    INSERT INTO public.projects (
        id, display_id, name, type_label, type_key, owner_id
    )
    VALUES (
        gen_random_uuid(),
        'P-' || nextval('public.display_seq_projects'),
        p_marker,
        '图像检测',
        'image_detection',
        p_owner_id
    )
    RETURNING id INTO v_project_id;

    RETURN v_project_id;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.create_scale_seed_project(uuid, text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.ensure_scale_seed_member(
    p_project_id uuid,
    p_annotator_id uuid,
    p_owner_id uuid
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_row_count integer;
BEGIN

    INSERT INTO public.project_members (
        id, project_id, user_id, role, assigned_by
    )
    VALUES (
        gen_random_uuid(), p_project_id, p_annotator_id, 'annotator', p_owner_id
    )
    ON CONFLICT (project_id, user_id) DO NOTHING;
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.ensure_scale_seed_member(uuid, uuid, uuid)
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.guard_scale_seed_cleanup(p_marker text)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_ids uuid[];
    v_project_id uuid;
    v_guard jsonb;
BEGIN
    IF p_marker IS NULL OR pg_catalog.btrim(p_marker) = '' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid scale seed marker';
    END IF;

    SELECT COALESCE(
        pg_catalog.array_agg(project.id ORDER BY project.id), ARRAY[]::uuid[]
    )
    INTO v_project_ids
    FROM public.projects AS project
    WHERE project.name = p_marker;

    -- Match the project deletion lock order: Learning advisory locks first,
    -- then Workspace rows, then retention guards.
    FOREACH v_project_id IN ARRAY v_project_ids LOOP
        PERFORM aap_learning_v1.lock_project_deletion(v_project_id);
    END LOOP;
    PERFORM project.id
    FROM public.projects AS project
    WHERE project.id = ANY (v_project_ids)
    ORDER BY project.id
    FOR UPDATE OF project NOWAIT;

    FOREACH v_project_id IN ARRAY v_project_ids LOOP
        IF NOT EXISTS (
            SELECT 1 FROM public.projects AS project
            WHERE project.id = v_project_id AND project.name = p_marker
        ) THEN
            CONTINUE;
        END IF;

        v_guard := aap_learning_v1.guard_project_deletion(v_project_id);
        IF v_guard IS NULL
           OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-project-delete-retention/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed Learning guard contract mismatch';
        END IF;
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
               OR pg_catalog.jsonb_typeof(v_guard -> 'error') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_guard #> '{error,code}') IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(v_guard #>> '{error,code}') = 0
               OR ((v_guard -> 'error') - 'code') <> '{}'::jsonb
               OR (v_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'scale seed Learning guard contract mismatch';
            END IF;
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-scale-seed/v1',
                'marker', p_marker,
                'project_ids', v_project_ids,
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'code', v_guard #>> '{error,code}'
                )
            );
        END IF;
        IF v_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
           OR (v_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed Learning guard contract mismatch';
        END IF;

        v_guard := aap_labeling_v1.guard_project_video_deletion(v_project_id);
        IF v_guard IS NULL
           OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-video-deletion-guard/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed video guard contract mismatch';
        END IF;
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
               OR v_guard -> 'error' IS DISTINCT FROM
                    pg_catalog.jsonb_build_object(
                        'code', 'video_execution_not_retired'
                    )
               OR (v_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'scale seed video guard contract mismatch';
            END IF;
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-scale-seed/v1',
                'marker', p_marker,
                'project_ids', v_project_ids,
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'code', 'video_execution_not_retired'
                )
            );
        END IF;
        IF v_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
           OR (v_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed video guard contract mismatch';
        END IF;
    END LOOP;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-scale-seed/v1',
        'marker', p_marker,
        'project_ids', v_project_ids,
        'ok', true,
        'error', NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION aap_workspace_v1.guard_scale_seed_cleanup(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_workspace_v1.delete_scale_seed_projects(
    p_marker text,
    p_project_ids uuid[]
)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_project_id uuid;
    v_guard jsonb;
    v_code text;
    v_row_count integer;
BEGIN
    IF p_marker IS NULL OR pg_catalog.btrim(p_marker) = ''
       OR p_project_ids IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid scale seed Project cleanup scope';
    END IF;
    IF pg_catalog.cardinality(p_project_ids) > 0
       AND pg_catalog.array_ndims(p_project_ids) IS DISTINCT FROM 1 THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid scale seed Project cleanup scope';
    END IF;
    IF pg_catalog.array_position(p_project_ids, NULL::uuid) IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid scale seed Project cleanup scope';
    END IF;

    -- The caller supplies the captured IDs. Never broaden this to every row
    -- which acquired the marker after preflight.
    FOREACH v_project_id IN ARRAY ARRAY(
        SELECT DISTINCT requested.project_id
        FROM pg_catalog.unnest(p_project_ids) AS requested(project_id)
        ORDER BY requested.project_id
    ) LOOP
        PERFORM aap_learning_v1.lock_project_deletion(v_project_id);
    END LOOP;

    PERFORM project.id
    FROM public.projects AS project
    WHERE project.id = ANY (p_project_ids)
    ORDER BY project.id
    FOR UPDATE OF project NOWAIT;

    IF EXISTS (
        SELECT 1
        FROM public.projects AS project
        WHERE project.id = ANY (p_project_ids)
          AND project.name IS DISTINCT FROM p_marker
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'scale seed Project marker changed',
            DETAIL = '{"code":"scale_seed_marker_mismatch"}';
    END IF;

    FOREACH v_project_id IN ARRAY ARRAY(
        SELECT DISTINCT requested.project_id
        FROM pg_catalog.unnest(p_project_ids) AS requested(project_id)
        ORDER BY requested.project_id
    ) LOOP
        IF NOT EXISTS (
            SELECT 1 FROM public.projects AS project
            WHERE project.id = v_project_id AND project.name = p_marker
        ) THEN
            CONTINUE;
        END IF;

        v_guard := aap_learning_v1.guard_project_deletion(v_project_id);
        IF v_guard IS NULL
           OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-project-delete-retention/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed Learning guard contract mismatch';
        END IF;
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
               OR pg_catalog.jsonb_typeof(v_guard -> 'error') IS DISTINCT FROM 'object'
               OR pg_catalog.jsonb_typeof(v_guard #> '{error,code}') IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(v_guard #>> '{error,code}') = 0
               OR ((v_guard -> 'error') - 'code') <> '{}'::jsonb
               OR (v_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'scale seed Learning guard contract mismatch';
            END IF;
            v_code := v_guard #>> '{error,code}';
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'scale seed Project cleanup is retained',
                DETAIL = pg_catalog.jsonb_build_object('code', v_code)::text;
        END IF;
        IF v_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
           OR (v_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed Learning guard contract mismatch';
        END IF;

        v_guard := aap_labeling_v1.guard_project_video_deletion(v_project_id);
        IF v_guard IS NULL
           OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-video-deletion-guard/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed video guard contract mismatch';
        END IF;
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
               OR v_guard -> 'error' IS DISTINCT FROM
                    pg_catalog.jsonb_build_object(
                        'code', 'video_execution_not_retired'
                    )
               OR (v_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'scale seed video guard contract mismatch';
            END IF;
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'scale seed Project video execution is not retired',
                DETAIL = '{"code":"video_execution_not_retired"}';
        END IF;
        IF v_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
           OR (v_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed video guard contract mismatch';
        END IF;
    END LOOP;

    DELETE FROM public.projects AS project
    WHERE project.id = ANY (p_project_ids)
      AND project.name = p_marker;
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    RETURN v_row_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_workspace_v1.delete_scale_seed_projects(text, uuid[])
    FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION aap_assets_v1.guard_scale_seed_dataset_cleanup(
    p_dataset_id uuid,
    p_after_task_cleanup boolean
)
RETURNS jsonb
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_scope jsonb;
    v_guard jsonb;
BEGIN
    IF p_dataset_id IS NULL OR p_after_task_cleanup IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'invalid scale seed Dataset cleanup scope';
    END IF;

    -- Retain Assets' canonical Dataset -> Scene -> Item NOWAIT lock order for
    -- the entire caller transaction. This private helper is never granted.
    v_scope := aap_assets_v1._lock_dataset_source_deletion(p_dataset_id, NULL);
    IF v_scope IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'scale seed Dataset lock contract mismatch';
    END IF;
    IF v_scope -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_scope ->> 'schema' IS DISTINCT FROM 'aap-dataset-source-delete/v1'
           OR v_scope -> 'ok' IS DISTINCT FROM 'false'::jsonb
           OR pg_catalog.jsonb_typeof(v_scope #> '{error,code}') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_scope #>> '{error,code}') = 0
           OR ((v_scope -> 'error') - 'code') <> '{}'::jsonb
           OR (v_scope - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed Dataset lock contract mismatch';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-scale-source-guard/v1',
            'dataset_id', p_dataset_id,
            'after_task_cleanup', p_after_task_cleanup,
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', v_scope #>> '{error,code}'
            )
        );
    END IF;
    IF v_scope ->> 'schema' IS DISTINCT FROM 'aap-dataset-source-delete-scope/v1'
       OR v_scope #> '{data,pending_upload}' IS DISTINCT FROM 'false'::jsonb
       OR (v_scope - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb
       OR ((v_scope -> 'data') - 'pending_upload') <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'scale seed Dataset lock contract mismatch';
    END IF;

    v_guard := aap_jobs_v1.guard_dataset_source_deletion(p_dataset_id);
    IF v_guard IS NULL
       OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'scale seed Jobs source guard contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
           OR pg_catalog.jsonb_typeof(v_guard #> '{error,code}') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_guard #>> '{error,code}') = 0
           OR ((v_guard -> 'error') - 'code') <> '{}'::jsonb
           OR (v_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed Jobs source guard contract mismatch';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-scale-source-guard/v1',
            'dataset_id', p_dataset_id,
            'after_task_cleanup', p_after_task_cleanup,
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', v_guard #>> '{error,code}'
            )
        );
    END IF;
    IF v_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
       OR (v_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'scale seed Jobs source guard contract mismatch';
    END IF;

    v_guard := aap_learning_v1.guard_dataset_source_deletion(p_dataset_id, NULL);
    IF v_guard IS NULL
       OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'scale seed Learning source guard contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
           OR pg_catalog.jsonb_typeof(v_guard #> '{error,code}') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_guard #>> '{error,code}') = 0
           OR ((v_guard -> 'error') - 'code') <> '{}'::jsonb
           OR (v_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed Learning source guard contract mismatch';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-scale-source-guard/v1',
            'dataset_id', p_dataset_id,
            'after_task_cleanup', p_after_task_cleanup,
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', v_guard #>> '{error,code}'
            )
        );
    END IF;
    IF v_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
       OR (v_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'scale seed Learning source guard contract mismatch';
    END IF;

    v_guard := aap_assets_v1.guard_dataset_source_auxiliary_retention(
        p_dataset_id, NULL
    );
    IF v_guard IS NULL
       OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'scale seed Assets source guard contract mismatch';
    END IF;
    IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
        IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
           OR pg_catalog.jsonb_typeof(v_guard #> '{error,code}') IS DISTINCT FROM 'string'
           OR pg_catalog.char_length(v_guard #>> '{error,code}') = 0
           OR ((v_guard -> 'error') - 'code') <> '{}'::jsonb
           OR (v_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed Assets source guard contract mismatch';
        END IF;
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-workspace-scale-source-guard/v1',
            'dataset_id', p_dataset_id,
            'after_task_cleanup', p_after_task_cleanup,
            'ok', false,
            'error', pg_catalog.jsonb_build_object(
                'code', v_guard #>> '{error,code}'
            )
        );
    END IF;
    IF v_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
       OR (v_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'scale seed Assets source guard contract mismatch';
    END IF;

    IF p_after_task_cleanup THEN
        v_guard := aap_labeling_v1.guard_dataset_source_deletion(
            p_dataset_id, NULL
        );
        IF v_guard IS NULL
           OR v_guard ->> 'schema' IS DISTINCT FROM 'aap-source-retention/v1' THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed Labeling source guard contract mismatch';
        END IF;
        IF v_guard -> 'ok' IS DISTINCT FROM 'true'::jsonb THEN
            IF v_guard -> 'ok' IS DISTINCT FROM 'false'::jsonb
               OR pg_catalog.jsonb_typeof(v_guard #> '{error,code}') IS DISTINCT FROM 'string'
               OR pg_catalog.char_length(v_guard #>> '{error,code}') = 0
               OR ((v_guard -> 'error') - 'code') <> '{}'::jsonb
               OR (v_guard - ARRAY['schema', 'ok', 'error']) <> '{}'::jsonb THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'scale seed Labeling source guard contract mismatch';
            END IF;
            RETURN pg_catalog.jsonb_build_object(
                'schema', 'aap-workspace-scale-source-guard/v1',
                'dataset_id', p_dataset_id,
                'after_task_cleanup', p_after_task_cleanup,
                'ok', false,
                'error', pg_catalog.jsonb_build_object(
                    'code', v_guard #>> '{error,code}'
                )
            );
        END IF;
        IF v_guard -> 'data' IS DISTINCT FROM '{}'::jsonb
           OR (v_guard - ARRAY['schema', 'ok', 'data']) <> '{}'::jsonb THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'scale seed Labeling source guard contract mismatch';
        END IF;
    END IF;

    RETURN pg_catalog.jsonb_build_object(
        'schema', 'aap-workspace-scale-source-guard/v1',
        'dataset_id', p_dataset_id,
        'after_task_cleanup', p_after_task_cleanup,
        'ok', true,
        'error', NULL
    );
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    aap_assets_v1.guard_scale_seed_dataset_cleanup(uuid, boolean)
    FROM PUBLIC;
