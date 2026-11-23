GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";
GRANT USAGE ON SCHEMA yoao_labeling_v1 TO :"assets_owner";

GRANT SELECT, INSERT, UPDATE ON public.image_pyramid_assets, public.image_pyramid_outputs,
    public.video_chunks, public.video_frame_cache, public.video_derivative_outputs,
    public.thumbnail_outputs TO :"assets_owner";
GRANT SELECT, INSERT, UPDATE, DELETE ON public.image_pyramid_generations, public.video_chapters
    TO :"assets_owner";
GRANT SELECT, INSERT, DELETE ON public.video_frame_indices TO :"assets_owner";
GRANT SELECT (id, dataset_id, file_type, file_path, created_at, width, height, metadata,
    thumbnail_output_id, thumbnail_path, blurhash, video_output_id, video_building_output_id)
    ON public.dataset_items TO :"assets_owner";
GRANT UPDATE (video_building_output_id, video_output_id, width, height, thumbnail_path,
    thumbnail_output_id, blurhash, metadata, updated_at) ON public.dataset_items TO :"assets_owner";
GRANT SELECT (id, location_id, purpose, revision) ON public.storage_operations TO :"assets_owner";
GRANT SELECT (location_id, state, revision), UPDATE (location_id)
    ON public.storage_maintenance_gates TO :"assets_owner";
GRANT SELECT (id, kind, enabled, config_revision, config, connection_id)
    ON public.storage_locations TO :"assets_owner";
GRANT SELECT (purpose, location_id, bucket, generation) ON public.storage_bindings TO :"assets_owner";
GRANT SELECT (id, kind, scope, project_id, config) ON public.storage_connections TO :"assets_owner";
GRANT SELECT (source_id, path_digest, thumbnail_output_id, state, source_sha256, spec, location_id,
    bucket, object_key, sha256, size_bytes) ON public.dataset_source_previews TO :"assets_owner";
GRANT SELECT (id, location_id) ON public.dataset_sources TO :"assets_owner";
GRANT SELECT (location_id, endpoint, bucket, binding_generation, storage_identity)
    ON yoao_assets_v1.fixture_native_cache_targets TO :"assets_owner";
GRANT SELECT (thumbnail_path, thumbnail_output_id) ON yoao_labeling_v1.task_rows TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_labeling_v1.read_media_task_access(uuid,uuid,uuid)
    TO :"assets_owner";
GRANT EXECUTE ON FUNCTION yoao_assets_v1.legacy_pyramid_gc_eligible(text,uuid,integer),
    yoao_assets_v1.fixture_media_ordinary_gc(text,uuid,integer),
    yoao_assets_v1.dataset_owns_directory_media(uuid),
    yoao_assets_v1.publish_image_pyramid_thumbnail(uuid,integer,uuid) TO :"assets_owner";

ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_asset(text,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_generation(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_summaries(uuid[],uuid[],text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.prepare_legacy_pyramid_generation(uuid,text,uuid,text,text,jsonb,jsonb,text,boolean,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_legacy_pyramid_claim(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.claim_legacy_pyramid_generation(uuid,integer,uuid,uuid,integer,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_legacy_pyramid_publication(uuid,integer,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_context(uuid,integer,uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_output(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_legacy_pyramid_output_for_publish(uuid,integer,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_legacy_pyramid_generation(uuid,integer,uuid,uuid,jsonb,jsonb,bigint) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.fail_legacy_pyramid_generation(uuid,integer,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.invalidate_legacy_pyramid_generation(uuid,integer,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.touch_legacy_pyramid_generation(uuid,integer,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.scan_expired_legacy_pyramid_generations() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.scan_stale_legacy_pyramid_generations(timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.remove_stale_legacy_pyramid_generation(uuid,integer,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_pyramid_output_unreferenced(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.scan_retirable_legacy_pyramid_outputs(timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.begin_legacy_pyramid_retirement(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.check_legacy_pyramid_retirement(uuid,jsonb,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.finish_legacy_pyramid_retirement(uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_pyramid_retained_bytes() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_chapter_scope(uuid,uuid,uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_legacy_video_chapters(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_legacy_video_chapter_source(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.lock_legacy_video_chapter(uuid,uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.create_legacy_video_chapter(uuid,uuid,uuid,uuid,integer,integer,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.update_legacy_video_chapter(uuid,uuid,uuid,uuid,integer,integer,text,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.delete_legacy_video_chapter(uuid,uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.read_media_cache_binding_history() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_thumbnail_facts(public.thumbnail_outputs) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_media_cache_target_current(jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_thumbnail_gc_clear(uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_thumbnail_gc_recheck(uuid,jsonb,uuid,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_output(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_item_thumbnail_pointer(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.directory_thumbnail_evidence(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.allocate_legacy_thumbnail(uuid,text,uuid,text,jsonb,jsonb,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_legacy_thumbnail_result(uuid,jsonb,text,bigint,text,text,boolean,boolean,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_directory_thumbnail_candidate(uuid,uuid,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.retire_legacy_directory_thumbnail(uuid,timestamptz,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_business(text,uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.set_legacy_item_dimensions(uuid,integer,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.record_legacy_item_thumbnail_error(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.publish_legacy_thumbnail(text,uuid,uuid,jsonb,jsonb,text,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_gc_candidates(integer,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_gc_start(uuid,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_gc_admit(uuid,jsonb,uuid,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_thumbnail_gc_finish(uuid,jsonb,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_item(uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_slot(uuid,text,jsonb,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_create_slot(uuid,text,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_attempt(uuid,text,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_allocate_output(uuid,uuid,text,jsonb,jsonb,jsonb,uuid,jsonb) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_attach_output(uuid,uuid,uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_cache_output(uuid,boolean) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_dispatch_sources(uuid[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_pts(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_failed_frames(uuid,integer,text,integer[]) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_touch_slot(text,uuid,uuid,uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_rebuild_items(uuid[],uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_locate_pending(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_claim_attempt(uuid,uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_fail_attempt(uuid,uuid,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_publish_attempt(uuid,uuid,jsonb,jsonb,timestamptz,integer,integer,text) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_ready_bytes() OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_expired_slots(text,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_expire_slot(text,uuid,uuid,uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_abandoned_attempts(timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_expire_attempt(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_retirement_candidates(timestamptz,timestamptz,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_begin_sweep(uuid,timestamptz,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_sweep_attempt(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_sweep_permit(uuid,integer) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1.legacy_video_finish_sweep(uuid,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_building_link(public.video_derivative_outputs) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_idle(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_unreferenced(uuid) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_expired(public.video_derivative_outputs,timestamptz) OWNER TO :"assets_owner";
ALTER FUNCTION yoao_assets_v1._legacy_video_gc_lock(uuid) OWNER TO :"assets_owner";

REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";

REVOKE ALL ON FUNCTION
    yoao_assets_v1.read_legacy_pyramid_asset,
    yoao_assets_v1.read_legacy_pyramid_generation,
    yoao_assets_v1.read_legacy_pyramid_summaries,
    yoao_assets_v1.prepare_legacy_pyramid_generation,
    yoao_assets_v1.lock_legacy_pyramid_claim,
    yoao_assets_v1.claim_legacy_pyramid_generation,
    yoao_assets_v1.lock_legacy_pyramid_publication,
    yoao_assets_v1.read_legacy_pyramid_context,
    yoao_assets_v1.read_legacy_pyramid_output,
    yoao_assets_v1.lock_legacy_pyramid_output_for_publish,
    yoao_assets_v1.publish_legacy_pyramid_generation,
    yoao_assets_v1.fail_legacy_pyramid_generation,
    yoao_assets_v1.invalidate_legacy_pyramid_generation,
    yoao_assets_v1.touch_legacy_pyramid_generation,
    yoao_assets_v1.scan_expired_legacy_pyramid_generations,
    yoao_assets_v1.scan_stale_legacy_pyramid_generations,
    yoao_assets_v1.remove_stale_legacy_pyramid_generation,
    yoao_assets_v1._legacy_pyramid_output_unreferenced,
    yoao_assets_v1.scan_retirable_legacy_pyramid_outputs,
    yoao_assets_v1.begin_legacy_pyramid_retirement,
    yoao_assets_v1.check_legacy_pyramid_retirement,
    yoao_assets_v1.finish_legacy_pyramid_retirement,
    yoao_assets_v1.read_legacy_pyramid_retained_bytes,
    yoao_assets_v1._legacy_video_chapter_scope,
    yoao_assets_v1.read_legacy_video_chapters,
    yoao_assets_v1.lock_legacy_video_chapter_source,
    yoao_assets_v1.lock_legacy_video_chapter,
    yoao_assets_v1.create_legacy_video_chapter,
    yoao_assets_v1.update_legacy_video_chapter,
    yoao_assets_v1.delete_legacy_video_chapter,
    yoao_assets_v1.read_media_cache_binding_history,
    yoao_assets_v1._legacy_thumbnail_facts,
    yoao_assets_v1._legacy_media_cache_target_current,
    yoao_assets_v1._legacy_thumbnail_gc_clear,
    yoao_assets_v1._legacy_thumbnail_gc_recheck,
    yoao_assets_v1.legacy_thumbnail_output,
    yoao_assets_v1.legacy_item_thumbnail_pointer,
    yoao_assets_v1.directory_thumbnail_evidence,
    yoao_assets_v1.allocate_legacy_thumbnail,
    yoao_assets_v1.record_legacy_thumbnail_result,
    yoao_assets_v1.legacy_directory_thumbnail_candidate,
    yoao_assets_v1.retire_legacy_directory_thumbnail,
    yoao_assets_v1.legacy_thumbnail_business,
    yoao_assets_v1.set_legacy_item_dimensions,
    yoao_assets_v1.record_legacy_item_thumbnail_error,
    yoao_assets_v1.publish_legacy_thumbnail,
    yoao_assets_v1.legacy_thumbnail_gc_candidates,
    yoao_assets_v1.legacy_thumbnail_gc_start,
    yoao_assets_v1.legacy_thumbnail_gc_admit,
    yoao_assets_v1.legacy_thumbnail_gc_finish,
    yoao_assets_v1.legacy_video_item,
    yoao_assets_v1.legacy_video_slot,
    yoao_assets_v1.legacy_video_create_slot,
    yoao_assets_v1.legacy_video_attempt,
    yoao_assets_v1.legacy_video_allocate_output,
    yoao_assets_v1.legacy_video_attach_output,
    yoao_assets_v1.legacy_video_cache_output,
    yoao_assets_v1.legacy_video_dispatch_sources,
    yoao_assets_v1.legacy_video_pts,
    yoao_assets_v1.legacy_video_failed_frames,
    yoao_assets_v1.legacy_video_touch_slot,
    yoao_assets_v1.legacy_video_rebuild_items,
    yoao_assets_v1.legacy_video_locate_pending,
    yoao_assets_v1.legacy_video_claim_attempt,
    yoao_assets_v1.legacy_video_fail_attempt,
    yoao_assets_v1.legacy_video_publish_attempt,
    yoao_assets_v1.legacy_video_ready_bytes,
    yoao_assets_v1.legacy_video_expired_slots,
    yoao_assets_v1.legacy_video_expire_slot,
    yoao_assets_v1.legacy_video_abandoned_attempts,
    yoao_assets_v1.legacy_video_expire_attempt,
    yoao_assets_v1.legacy_video_retirement_candidates,
    yoao_assets_v1.legacy_video_begin_sweep,
    yoao_assets_v1.legacy_video_sweep_attempt,
    yoao_assets_v1.legacy_video_sweep_permit,
    yoao_assets_v1.legacy_video_finish_sweep,
    yoao_assets_v1._legacy_video_building_link,
    yoao_assets_v1._legacy_video_idle,
    yoao_assets_v1._legacy_video_unreferenced,
    yoao_assets_v1._legacy_video_expired,
    yoao_assets_v1._legacy_video_gc_lock
FROM PUBLIC;

GRANT USAGE, CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
GRANT SELECT (id, owner_kind, owner_id, input_ref, storage_ref, state, uncertain_write)
    ON public.thumbnail_outputs TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._legacy_task_thumbnail_business(uuid,boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._legacy_task_thumbnail_referenced(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._publish_legacy_task_thumbnail(uuid,uuid,text) OWNER TO :"labeling_owner";
GRANT EXECUTE ON FUNCTION
    yoao_labeling_v1._legacy_task_thumbnail_business(uuid,boolean),
    yoao_labeling_v1._legacy_task_thumbnail_referenced(uuid),
    yoao_labeling_v1._publish_legacy_task_thumbnail(uuid,uuid,text) TO :"assets_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
REVOKE ALL ON FUNCTION
    yoao_labeling_v1._legacy_task_thumbnail_business(uuid,boolean),
    yoao_labeling_v1._legacy_task_thumbnail_referenced(uuid),
    yoao_labeling_v1._publish_legacy_task_thumbnail(uuid,uuid,text) FROM PUBLIC;
