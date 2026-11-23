CREATE FUNCTION yoao_assets_v1._legacy_video_chapter_scope(
    p_item uuid, p_task uuid, p_actor uuid, p_write boolean
) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT COALESCE((access ->> 'ok')::boolean
        AND access #>> '{data,file_type}' = 'video'
        AND (NOT p_write OR (access #>> '{data,is_manager}')::boolean), false)
    FROM yoao_labeling_v1.read_media_task_access(p_actor, p_task, p_item) AS access;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.read_legacy_video_chapters(
    p_item uuid, p_task uuid, p_actor uuid
) RETURNS SETOF public.video_chapters LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT chapter.* FROM public.video_chapters AS chapter
    WHERE chapter.dataset_item_id = p_item
      AND yoao_assets_v1._legacy_video_chapter_scope(p_item, p_task, p_actor, false)
    ORDER BY chapter.start_frame, chapter.end_frame;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_legacy_video_chapter_source(
    p_item uuid, p_task uuid, p_actor uuid
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.dataset_items AS item
        WHERE item.id = p_item AND item.file_type = 'video'
          AND yoao_assets_v1._legacy_video_chapter_scope(p_item, p_task, p_actor, true)
        FOR SHARE OF item NOWAIT
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_legacy_video_chapter(
    p_item uuid, p_task uuid, p_actor uuid, p_chapter uuid
) RETURNS SETOF public.video_chapters LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT chapter.* FROM public.video_chapters AS chapter
    WHERE chapter.id = p_chapter AND chapter.dataset_item_id = p_item
      AND yoao_assets_v1._legacy_video_chapter_scope(p_item, p_task, p_actor, true)
    FOR UPDATE OF chapter NOWAIT;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.create_legacy_video_chapter(
    p_item uuid, p_task uuid, p_actor uuid, p_chapter uuid,
    p_start integer, p_end integer, p_title text, p_color text, p_metadata jsonb
) RETURNS SETOF public.video_chapters LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    INSERT INTO public.video_chapters (
        id, dataset_item_id, start_frame, end_frame, title, color, metadata, created_by
    ) SELECT p_chapter, p_item, p_start, p_end, p_title, p_color, p_metadata, p_actor
    WHERE yoao_assets_v1.lock_legacy_video_chapter_source(p_item, p_task, p_actor)
    RETURNING *;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.update_legacy_video_chapter(
    p_item uuid, p_task uuid, p_actor uuid, p_chapter uuid,
    p_start integer, p_end integer, p_title text, p_color text, p_metadata jsonb
) RETURNS SETOF public.video_chapters LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    v_current public.video_chapters%ROWTYPE;
BEGIN
    IF NOT yoao_assets_v1.lock_legacy_video_chapter_source(p_item, p_task, p_actor) THEN
        RETURN;
    END IF;
    SELECT * INTO v_current
    FROM yoao_assets_v1.lock_legacy_video_chapter(p_item, p_task, p_actor, p_chapter);
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF (v_current.start_frame, v_current.end_frame, v_current.title,
        v_current.color, v_current.metadata)
        IS NOT DISTINCT FROM (p_start, p_end, p_title, p_color, p_metadata) THEN
        RETURN NEXT v_current;
        RETURN;
    END IF;
    RETURN QUERY UPDATE public.video_chapters AS chapter
    SET start_frame = p_start, end_frame = p_end, title = p_title,
        color = p_color, metadata = p_metadata, updated_at = pg_catalog.now()
    WHERE chapter.id = p_chapter AND chapter.dataset_item_id = p_item
    RETURNING chapter.*;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.delete_legacy_video_chapter(
    p_item uuid, p_task uuid, p_actor uuid, p_chapter uuid
) RETURNS SETOF public.video_chapters LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    DELETE FROM public.video_chapters AS chapter
    WHERE chapter.id = p_chapter AND chapter.dataset_item_id = p_item
      AND yoao_assets_v1.lock_legacy_video_chapter_source(p_item, p_task, p_actor)
      AND EXISTS (
          SELECT 1 FROM yoao_assets_v1.lock_legacy_video_chapter(p_item, p_task, p_actor, p_chapter)
      )
    RETURNING chapter.*;
$function$;
