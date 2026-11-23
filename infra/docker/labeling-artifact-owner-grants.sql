GRANT USAGE, CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_workspace_v1 TO :"workspace_owner";
GRANT USAGE, CREATE ON SCHEMA yoao_assets_v1 TO :"assets_owner";

GRANT SELECT (id, owner_id, annotation_id, task_id, project_id, storage_ref, sha256,
    size_bytes, mime_type, file_name, created_at, expires_at, state, bound_comment_id,
    retired_at, last_attempted_at),
    INSERT (id, owner_id, annotation_id, task_id, project_id, storage_ref, sha256,
    size_bytes, mime_type, file_name, created_at, expires_at, state),
    UPDATE (state, bound_comment_id, retired_at, last_attempted_at), DELETE
    ON public.comment_attachment_uploads TO :"labeling_owner";
GRANT SELECT (id, project_id, uploader_id, logical_object_key, content_type, storage_ref,
    created_at, upload_expires_at),
    INSERT (id, project_id, uploader_id, logical_object_key, content_type, storage_ref,
    created_at, upload_expires_at), UPDATE (id)
    ON public.mask_format_uploads TO :"labeling_owner";
GRANT SELECT (id, logical_key, canonical_sha256, canonical_size_bytes, height, width, runs,
    storage_encoding, stored_sha256, stored_size_bytes, storage_ref, state,
    created_at, published_at, last_used_at, last_attempted_at),
    INSERT (id, logical_key, canonical_sha256, canonical_size_bytes, height, width, runs,
    storage_encoding, stored_sha256, stored_size_bytes, storage_ref, state, created_at),
    UPDATE (state, published_at, last_used_at, retired_at, last_attempted_at), DELETE
    ON public.raster_mask_objects TO :"labeling_owner";
GRANT SELECT (id, object_id, gate_revision),
    INSERT (id, object_id, gate_revision, created_at), UPDATE (id), DELETE
    ON public.raster_mask_publications TO :"labeling_owner";
GRANT INSERT (id, owner_id, project_id, storage_ref, sha256, size_bytes, mime_type,
    file_name, state, created_at), UPDATE (last_attempted_at), DELETE
    ON public.guide_asset_uploads TO :"workspace_owner";
GRANT UPDATE (upload_id) ON public.project_guide_assets TO :"workspace_owner";

ALTER TYPE yoao_workspace_v1.guide_physical_receipt OWNER TO :"workspace_owner";
ALTER TYPE yoao_labeling_v1.raster_physical_receipt OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.create_uploading_task(uuid,uuid,text,text,text,uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.mask_ai_inventory() OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_workspace_v1.guide_clone_snapshot(uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.stage_guide_upload(uuid,uuid,uuid,jsonb,text,integer,text,text,timestamptz)
    OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.lock_guide_upload(uuid,boolean) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.mount_guide_upload(uuid,uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.guide_upload_has_reference(uuid) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.guide_retirement_candidates(integer) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.attempt_guide_retirement(uuid,timestamptz) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.settle_guide_retirement(uuid,jsonb) OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_workspace_v1.guide_binding_history() OWNER TO :"workspace_owner";
ALTER FUNCTION yoao_labeling_v1.stage_mask_format_upload(uuid,uuid,uuid,text,text,jsonb,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.mask_format_upload_source(text,boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.mask_format_binding_history() OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.stage_comment_attachment(uuid,uuid,uuid,uuid,uuid,jsonb,text,integer,text,text,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_comment_attachment(uuid,boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.comment_attachment_binding(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.publish_comment_attachment(uuid,jsonb) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.bind_comment_attachment(uuid,uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.retire_comment_attachments(uuid[],timestamptz) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.comment_attachment_retirement_candidates(timestamptz,integer)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.attempt_comment_attachment_retirement(uuid,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.settle_comment_attachment_retirement(uuid,jsonb,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.annotation_binding_history() OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.stage_raster_object(uuid,text,text,integer,integer,integer,integer,text,text,integer,jsonb,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.ready_raster_owner(text,boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_raster_owners(uuid[],boolean) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_raster_ready_for_publish(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.publish_raster_object(uuid,jsonb,timestamptz) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.touch_ready_raster_object(uuid,jsonb,timestamptz) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.retire_raster_object(uuid,text,jsonb,timestamptz,timestamptz)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.raster_physical_gc_candidates(timestamptz,integer,boolean)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.note_raster_gc_attempts(uuid[],timestamptz[]) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.settle_raster_gc(uuid,jsonb) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.open_raster_publication(uuid,uuid,integer,timestamptz) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.raster_publication_matches(uuid,uuid,integer) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.release_raster_publication(uuid,uuid,integer) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.lock_raster_publications(uuid[]) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.consume_raster_publications(uuid[],uuid[],integer[])
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.raster_object_has_publication(uuid) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.expire_unlinked_raster_uploads(timestamptz) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.raster_physical_reference_keys(text) OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_assets_v1.lock_video_segment_source(uuid,uuid) OWNER TO :"assets_owner";

REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_workspace_v1 FROM :"workspace_owner";
REVOKE CREATE ON SCHEMA yoao_assets_v1 FROM :"assets_owner";
