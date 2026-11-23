CREATE FUNCTION yoao_assets_v1.read_media_cache_binding_history()
RETURNS TABLE(directory_previews bigint, original_cache_entries bigint, image_pyramid_outputs bigint,
              video_derivative_outputs bigint, thumbnail_outputs bigint, legacy_thumbnails bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT (SELECT count(preview.source_id) FROM public.dataset_source_previews AS preview),
           yoao_assets_v1.original_cache_binding_history(),
           (SELECT count(*) FROM public.image_pyramid_outputs),
           (SELECT count(*) FROM public.video_derivative_outputs),
           (SELECT count(*) FROM public.thumbnail_outputs),
           (SELECT count(*) FROM public.dataset_items AS item
            WHERE item.thumbnail_path IS NOT NULL AND item.thumbnail_output_id IS NULL)
           + (SELECT count(*) FROM yoao_labeling_v1.task_rows AS task
              WHERE task.thumbnail_path IS NOT NULL AND task.thumbnail_output_id IS NULL);
$function$;
