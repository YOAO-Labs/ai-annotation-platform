CREATE FUNCTION yoao_labeling_v1.create_uploading_task(
    p_task_id uuid,
    p_project_id uuid,
    p_file_name text,
    p_file_path text,
    p_file_type text,
    p_actor_id uuid
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
AS $function$
DECLARE
    v_authority jsonb;
BEGIN
    IF p_task_id IS NULL OR p_project_id IS NULL OR p_actor_id IS NULL
       OR p_file_name IS NULL OR p_file_name = ''
       OR pg_catalog.char_length(p_file_name) > 500
       OR p_file_path IS NULL OR p_file_path = ''
       OR pg_catalog.char_length(p_file_path) > 1000
       OR p_file_type IS NULL OR p_file_type NOT IN ('image', 'video', 'other') THEN
        RETURN false;
    END IF;

    v_authority := yoao_workspace_v1.lock_project_manager(p_actor_id, p_project_id);
    IF (v_authority ->> 'ok')::boolean IS DISTINCT FROM true THEN
        RETURN false;
    END IF;

    INSERT INTO public.tasks (id, project_id, display_id, file_name, file_path, file_type, status)
    VALUES (
        p_task_id, p_project_id,
        'T-' || pg_catalog.nextval('public.display_seq_tasks'::pg_catalog.regclass)::text,
        p_file_name, p_file_path, p_file_type, 'uploading'
    )
    ON CONFLICT (id) DO NOTHING;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
DROP FUNCTION yoao_labeling_v1.create_uploading_task(uuid,uuid,text,text,text,text,uuid) RESTRICT;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.mask_ai_inventory()
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH correction AS (
        SELECT job.status,
               count(*) AS total,
               greatest(0, extract(epoch FROM (now() - min(
                   CASE WHEN job.status IN ('pending_review', 'partially_reviewed')
                        THEN coalesce(job.completed_at, job.updated_at, job.created_at)
                        ELSE coalesce(job.started_at, job.created_at) END
               )))) AS age
        FROM public.video_tracker_jobs AS job
        WHERE job.job_kind = 'correction'
        GROUP BY job.status
    ), staged AS (
        SELECT job.job_kind, count(DISTINCT key.value #>> '{}') AS total
        FROM public.video_tracker_jobs AS job
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(job.staged_result, '$.**.object_key') AS key(value)
        WHERE job.staged_result IS NOT NULL
          AND job.status IN ('pending_review', 'partially_reviewed', 'cancelled')
          AND key.value #>> '{}' LIKE 'raster-masks/sha256/%'
        GROUP BY job.job_kind
    ), decision AS (
        SELECT count(*) FILTER (WHERE d.expires_at > now()) AS active,
               count(*) FILTER (WHERE d.expires_at <= now()) AS expired,
               greatest(0, extract(epoch FROM (
                   now() - min(d.expires_at) FILTER (WHERE d.expires_at <= now())
               ))) AS expired_age
        FROM public.ai_mask_accept_decisions AS d
    )
    SELECT pg_catalog.jsonb_build_object(
        'correction_jobs', coalesce((SELECT pg_catalog.jsonb_object_agg(status, total) FROM correction), '{}'),
        'correction_oldest_age_seconds', coalesce((
            SELECT pg_catalog.jsonb_object_agg(status, age)
            FROM correction WHERE status IN ('queued', 'running', 'pending_review', 'partially_reviewed')
        ), '{}'),
        'staged_mask_references', coalesce((SELECT pg_catalog.jsonb_object_agg(job_kind, total) FROM staged), '{}'),
        'accept_decisions', pg_catalog.jsonb_build_object('active', decision.active, 'expired', decision.expired),
        'oldest_expired_decision_age_seconds', decision.expired_age
    )
    FROM decision;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.create_uploading_task(uuid,uuid,text,text,text,uuid),
    yoao_labeling_v1.mask_ai_inventory() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.guide_clone_snapshot(p_project_id uuid)
RETURNS jsonb
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT pg_catalog.jsonb_build_object(
        'owner_id', p.owner_id, 'annotation_guide', p.annotation_guide,
        'guide_assets', p.guide_assets, 'guide_assets_revision', p.guide_assets_revision,
        'upload_ids', ARRAY(
            SELECT ref.upload_id FROM public.project_guide_assets AS ref
            WHERE ref.project_id = p.id ORDER BY ref.upload_id
        )
    )
    FROM public.projects AS p WHERE p.id = p_project_id;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.stage_guide_upload(
    p_id uuid, p_actor_id uuid, p_project_id uuid, p_storage_ref jsonb,
    p_sha256 text, p_size integer, p_mime text, p_name text, p_created_at timestamptz
)
RETURNS void
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.guide_asset_uploads (
        id, owner_id, project_id, storage_ref, sha256, size_bytes, mime_type, file_name,
        state, created_at
    )
    VALUES (
        p_id, p_actor_id, p_project_id, p_storage_ref, p_sha256, p_size, p_mime, p_name,
        'pending', p_created_at
    );
$function$;
-- aap-statement-break
CREATE TYPE yoao_workspace_v1.guide_physical_receipt AS (
    id uuid, owner_id uuid, project_id uuid, storage_ref jsonb, sha256 text,
    size_bytes integer, mime_type text, file_name text, state text, created_at timestamptz
);
-- aap-statement-break
REVOKE ALL ON TYPE yoao_workspace_v1.guide_physical_receipt FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.lock_guide_upload(p_id uuid, p_skip_locked boolean)
RETURNS SETOF yoao_workspace_v1.guide_physical_receipt
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_skip_locked THEN
        PERFORM u.id FROM public.guide_asset_uploads AS u WHERE u.id = p_id
        FOR UPDATE OF u SKIP LOCKED;
    ELSE
        PERFORM u.id FROM public.guide_asset_uploads AS u WHERE u.id = p_id
        FOR UPDATE OF u NOWAIT;
    END IF;
    IF FOUND THEN
        RETURN QUERY SELECT u.id, u.owner_id, u.project_id, u.storage_ref,
            u.sha256::text, u.size_bytes, u.mime_type::text, u.file_name::text,
            u.state::text, u.created_at
        FROM public.guide_asset_uploads AS u WHERE u.id = p_id;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.mount_guide_upload(p_project_id uuid, p_upload_id uuid)
RETURNS SETOF yoao_workspace_v1.guide_physical_receipt
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT FROM public.project_guide_assets AS r
        WHERE r.project_id = p_project_id AND r.upload_id = p_upload_id
    ) THEN
        RETURN;
    END IF;
    PERFORM u.id FROM public.guide_asset_uploads AS u WHERE u.id = p_upload_id
    FOR SHARE OF u NOWAIT;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM r.upload_id FROM public.project_guide_assets AS r
    WHERE r.project_id = p_project_id AND r.upload_id = p_upload_id
    FOR SHARE OF r NOWAIT;
    IF FOUND THEN
        RETURN QUERY SELECT u.id, u.owner_id, u.project_id, u.storage_ref,
            u.sha256::text, u.size_bytes, u.mime_type::text, u.file_name::text,
            u.state::text, u.created_at
        FROM public.guide_asset_uploads AS u WHERE u.id = p_upload_id;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.guide_upload_has_reference(p_id uuid)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT FROM public.project_guide_assets AS r WHERE r.upload_id = p_id);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.guide_retirement_candidates(p_limit integer)
RETURNS SETOF uuid
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT u.id FROM public.guide_asset_uploads AS u
    WHERE u.state = 'retired' OR (
        u.state = 'ready' AND NOT EXISTS (
            SELECT FROM public.project_guide_assets AS r WHERE r.upload_id = u.id
        )
    )
    ORDER BY u.last_attempted_at ASC NULLS FIRST, u.created_at, u.id
    LIMIT greatest(1, least(500, p_limit));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.attempt_guide_retirement(p_id uuid, p_now timestamptz)
RETURNS text
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.guide_asset_uploads AS u
    SET state = 'retired',
        retired_at = CASE WHEN u.state = 'ready' THEN p_now ELSE u.retired_at END,
        last_attempted_at = CASE WHEN u.state = 'retired' THEN p_now ELSE u.last_attempted_at END
    WHERE u.id = p_id AND u.state IN ('ready', 'retired')
      AND NOT EXISTS (SELECT FROM public.project_guide_assets AS r WHERE r.upload_id = u.id)
    RETURNING u.state::text;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.settle_guide_retirement(p_id uuid, p_storage_ref jsonb)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    DELETE FROM public.guide_asset_uploads AS u
    WHERE u.id = p_id AND u.state = 'retired' AND u.storage_ref = p_storage_ref
      AND NOT EXISTS (SELECT FROM public.project_guide_assets AS r WHERE r.upload_id = u.id);
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1.guide_binding_history()
RETURNS bigint
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT count(*) FROM public.guide_asset_uploads;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.stage_mask_format_upload(
    p_id uuid, p_project_id uuid, p_actor_id uuid, p_key text, p_content_type text,
    p_storage_ref jsonb, p_created_at timestamptz
)
RETURNS void
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.mask_format_uploads (
        id, project_id, uploader_id, logical_object_key, content_type, storage_ref,
        created_at, upload_expires_at
    )
    VALUES (
        p_id, p_project_id, p_actor_id, p_key, p_content_type, p_storage_ref,
        p_created_at, p_created_at + interval '900 seconds'
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.mask_format_upload_source(p_key text, p_share boolean)
RETURNS TABLE (
    id uuid, project_id uuid, uploader_id uuid, logical_object_key text, content_type text,
    storage_ref jsonb, created_at timestamptz, upload_expires_at timestamptz
)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_share THEN
        PERFORM u.id FROM public.mask_format_uploads AS u WHERE u.logical_object_key = p_key
        FOR SHARE OF u NOWAIT;
        IF NOT FOUND THEN
            RETURN;
        END IF;
    END IF;
    RETURN QUERY SELECT u.id, u.project_id, u.uploader_id, u.logical_object_key::text,
        u.content_type::text, u.storage_ref, u.created_at, u.upload_expires_at
    FROM public.mask_format_uploads AS u WHERE u.logical_object_key = p_key;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.mask_format_binding_history()
RETURNS TABLE (mask_uploads bigint, mask_batches bigint)
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT (SELECT count(*) FROM public.mask_format_uploads),
           (SELECT count(*) FROM public.mask_format_imports);
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_workspace_v1.guide_clone_snapshot(uuid),
    yoao_workspace_v1.stage_guide_upload(uuid,uuid,uuid,jsonb,text,integer,text,text,timestamptz),
    yoao_workspace_v1.lock_guide_upload(uuid,boolean),
    yoao_workspace_v1.mount_guide_upload(uuid,uuid),
    yoao_workspace_v1.guide_upload_has_reference(uuid),
    yoao_workspace_v1.guide_retirement_candidates(integer),
    yoao_workspace_v1.attempt_guide_retirement(uuid,timestamptz),
    yoao_workspace_v1.settle_guide_retirement(uuid,jsonb),
    yoao_workspace_v1.guide_binding_history(),
    yoao_labeling_v1.stage_mask_format_upload(uuid,uuid,uuid,text,text,jsonb,timestamptz),
    yoao_labeling_v1.mask_format_upload_source(text,boolean),
    yoao_labeling_v1.mask_format_binding_history() FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.stage_comment_attachment(
    p_id uuid, p_actor_id uuid, p_annotation_id uuid, p_task_id uuid, p_project_id uuid,
    p_storage_ref jsonb, p_sha256 text, p_size integer, p_mime text, p_name text,
    p_created_at timestamptz
)
RETURNS void
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.comment_attachment_uploads (
        id, owner_id, annotation_id, task_id, project_id, storage_ref, sha256, size_bytes,
        mime_type, file_name, created_at, expires_at, state
    )
    VALUES (
        p_id, p_actor_id, p_annotation_id, p_task_id, p_project_id, p_storage_ref, p_sha256,
        p_size, p_mime, p_name, p_created_at, p_created_at + interval '2160 hours', 'pending'
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.lock_comment_attachment(p_id uuid, p_skip_locked boolean)
RETURNS TABLE (
    id uuid, owner_id uuid, annotation_id uuid, task_id uuid, project_id uuid,
    storage_ref jsonb, sha256 text, size_bytes integer, mime_type text, file_name text,
    created_at timestamptz, expires_at timestamptz, state text,
    bound_comment_id uuid, retired_at timestamptz
)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_skip_locked THEN
        PERFORM u.id FROM public.comment_attachment_uploads AS u WHERE u.id = p_id
        FOR UPDATE OF u SKIP LOCKED;
    ELSE
        PERFORM u.id FROM public.comment_attachment_uploads AS u WHERE u.id = p_id
        FOR UPDATE OF u NOWAIT;
    END IF;
    IF FOUND THEN
        RETURN QUERY SELECT u.id, u.owner_id, u.annotation_id, u.task_id, u.project_id,
            u.storage_ref, u.sha256::text, u.size_bytes, u.mime_type::text, u.file_name::text,
            u.created_at, u.expires_at, u.state::text, u.bound_comment_id, u.retired_at
        FROM public.comment_attachment_uploads AS u WHERE u.id = p_id;
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.comment_attachment_binding(p_id uuid)
RETURNS uuid
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT u.bound_comment_id FROM public.comment_attachment_uploads AS u
    WHERE u.id = p_id;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.publish_comment_attachment(p_id uuid, p_storage_ref jsonb)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.comment_attachment_uploads AS u SET state = 'ready'
    WHERE u.id = p_id AND u.state = 'pending' AND u.storage_ref = p_storage_ref
        AND u.expires_at > clock_timestamp();
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.bind_comment_attachment(p_id uuid, p_comment_id uuid)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.comment_attachment_uploads AS u SET bound_comment_id = c.id
    FROM public.annotation_comments AS c
    WHERE u.id = p_id AND u.state = 'ready' AND u.bound_comment_id IS NULL
      AND u.expires_at > clock_timestamp()
      AND c.id = p_comment_id AND c.is_active AND c.author_id = u.owner_id
      AND c.annotation_id = u.annotation_id AND c.project_id = u.project_id;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.retire_comment_attachments(p_comment_ids uuid[], p_now timestamptz)
RETURNS SETOF uuid
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM u.id FROM public.comment_attachment_uploads AS u
    WHERE u.bound_comment_id = ANY(p_comment_ids) ORDER BY u.id FOR UPDATE OF u NOWAIT;
    UPDATE public.comment_attachment_uploads AS u
    SET state = 'retired', retired_at = p_now
    WHERE u.bound_comment_id = ANY(p_comment_ids) AND u.state = 'ready';
    RETURN QUERY SELECT u.id FROM public.comment_attachment_uploads AS u
    WHERE u.bound_comment_id = ANY(p_comment_ids) AND u.state = 'retired' ORDER BY u.id;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.comment_attachment_retirement_candidates(
    p_now timestamptz, p_limit integer
)
RETURNS SETOF uuid
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT u.id FROM public.comment_attachment_uploads AS u
    WHERE u.state = 'retired' OR (
        u.state = 'ready' AND (
            u.expires_at <= p_now OR (
                u.bound_comment_id IS NOT NULL AND NOT EXISTS (
                    SELECT FROM public.annotation_comments AS c
                    WHERE c.id = u.bound_comment_id AND c.is_active AND c.author_id = u.owner_id
                      AND c.annotation_id = u.annotation_id AND c.project_id = u.project_id
                )
            )
        )
    )
    ORDER BY u.last_attempted_at ASC NULLS FIRST, u.created_at, u.id
    LIMIT greatest(1, least(500, p_limit));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.attempt_comment_attachment_retirement(p_id uuid, p_now timestamptz)
RETURNS TABLE (retired_at timestamptz)
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    UPDATE public.comment_attachment_uploads AS u
    SET state = 'retired', retired_at = coalesce(u.retired_at, p_now), last_attempted_at = p_now
    WHERE u.id = p_id AND (
        u.state = 'retired' OR (
            u.state = 'ready' AND (u.expires_at <= p_now OR u.bound_comment_id IS NOT NULL)
        )
    )
    AND NOT (
        u.expires_at > p_now AND EXISTS (
            SELECT FROM public.annotation_comments AS c
            WHERE c.id = u.bound_comment_id AND c.is_active AND c.author_id = u.owner_id
              AND c.annotation_id = u.annotation_id AND c.project_id = u.project_id
        )
    )
    RETURNING u.retired_at;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.settle_comment_attachment_retirement(
    p_id uuid, p_storage_ref jsonb, p_now timestamptz
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    DELETE FROM public.comment_attachment_uploads AS u
    WHERE u.id = p_id AND u.state = 'retired' AND u.storage_ref = p_storage_ref
      AND (
          u.expires_at <= p_now OR (
              u.retired_at + interval '168 hours' <= p_now AND NOT EXISTS (
                  SELECT FROM public.annotation_comments AS c
                  WHERE c.id = u.bound_comment_id AND c.is_active AND c.author_id = u.owner_id
                    AND c.annotation_id = u.annotation_id AND c.project_id = u.project_id
              )
          )
      );
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.annotation_binding_history()
RETURNS TABLE (raster_mask_objects bigint, comment_attachments bigint)
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT (SELECT count(*) FROM public.raster_mask_objects),
           (SELECT count(*) FROM public.comment_attachment_uploads);
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_labeling_v1.stage_comment_attachment(uuid,uuid,uuid,uuid,uuid,jsonb,text,integer,text,text,timestamptz),
    yoao_labeling_v1.lock_comment_attachment(uuid,boolean),
    yoao_labeling_v1.comment_attachment_binding(uuid),
    yoao_labeling_v1.publish_comment_attachment(uuid,jsonb),
    yoao_labeling_v1.bind_comment_attachment(uuid,uuid),
    yoao_labeling_v1.retire_comment_attachments(uuid[],timestamptz),
    yoao_labeling_v1.comment_attachment_retirement_candidates(timestamptz,integer),
    yoao_labeling_v1.attempt_comment_attachment_retirement(uuid,timestamptz),
    yoao_labeling_v1.settle_comment_attachment_retirement(uuid,jsonb,timestamptz),
    yoao_labeling_v1.annotation_binding_history() FROM PUBLIC;
-- aap-statement-break
CREATE TYPE yoao_labeling_v1.raster_physical_receipt AS (
    id uuid, logical_key text, canonical_sha256 text, canonical_size_bytes integer,
    height integer, width integer, runs integer, storage_encoding text,
    stored_sha256 text, stored_size_bytes integer, storage_ref jsonb,
    state text, created_at timestamptz, published_at timestamptz, last_used_at timestamptz
);
-- aap-statement-break
REVOKE ALL ON TYPE yoao_labeling_v1.raster_physical_receipt FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.stage_raster_object(
    p_id uuid, p_key text, p_canonical_sha256 text, p_canonical_size integer,
    p_height integer, p_width integer, p_runs integer, p_encoding text, p_stored_sha256 text,
    p_stored_size integer, p_storage_ref jsonb, p_created_at timestamptz
)
RETURNS void
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.raster_mask_objects (
        id, logical_key, canonical_sha256, canonical_size_bytes, height, width, runs,
        storage_encoding, stored_sha256, stored_size_bytes, storage_ref, state, created_at
    )
    VALUES (
        p_id, p_key, p_canonical_sha256, p_canonical_size, p_height, p_width, p_runs,
        p_encoding, p_stored_sha256, p_stored_size, p_storage_ref, 'pending', p_created_at
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.ready_raster_owner(p_key text, p_share boolean)
RETURNS SETOF yoao_labeling_v1.raster_physical_receipt
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_id uuid;
BEGIN
    IF p_share THEN
        SELECT o.id INTO v_id FROM public.raster_mask_objects AS o
        WHERE o.logical_key = p_key AND o.state = 'ready' FOR SHARE OF o NOWAIT;
        IF FOUND THEN
            RETURN QUERY SELECT r.id, r.logical_key::text, r.canonical_sha256::text,
                r.canonical_size_bytes, r.height, r.width, r.runs, r.storage_encoding::text,
                r.stored_sha256::text, r.stored_size_bytes, r.storage_ref, r.state::text,
                r.created_at, r.published_at, r.last_used_at
            FROM public.raster_mask_objects AS r
            WHERE r.id = v_id;
        END IF;
    ELSE
        RETURN QUERY SELECT r.id, r.logical_key::text, r.canonical_sha256::text,
            r.canonical_size_bytes, r.height, r.width, r.runs, r.storage_encoding::text,
            r.stored_sha256::text, r.stored_size_bytes, r.storage_ref, r.state::text,
            r.created_at, r.published_at, r.last_used_at
        FROM public.raster_mask_objects AS r
        WHERE r.logical_key = p_key AND r.state = 'ready';
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.lock_raster_owners(p_ids uuid[], p_share boolean)
RETURNS SETOF yoao_labeling_v1.raster_physical_receipt
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_ids uuid[];
BEGIN
    IF p_share THEN
        SELECT array_agg(candidate.id) INTO v_ids FROM (
            SELECT o.id FROM public.raster_mask_objects AS o
            WHERE o.id = ANY(p_ids) ORDER BY o.id FOR SHARE OF o NOWAIT
        ) AS candidate;
    ELSE
        SELECT array_agg(candidate.id) INTO v_ids FROM (
            SELECT o.id FROM public.raster_mask_objects AS o
            WHERE o.id = ANY(p_ids) ORDER BY o.id FOR UPDATE OF o NOWAIT
        ) AS candidate;
    END IF;
    RETURN QUERY SELECT r.id, r.logical_key::text, r.canonical_sha256::text,
        r.canonical_size_bytes, r.height, r.width, r.runs, r.storage_encoding::text,
        r.stored_sha256::text, r.stored_size_bytes, r.storage_ref, r.state::text,
        r.created_at, r.published_at, r.last_used_at
    FROM public.raster_mask_objects AS r
    WHERE r.id = ANY(v_ids) ORDER BY r.id;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.lock_raster_ready_for_publish(p_id uuid)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM o.id FROM public.raster_mask_objects AS o
    WHERE o.id = p_id AND o.state = 'ready' FOR UPDATE OF o;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.publish_raster_object(p_id uuid, p_ref jsonb, p_now timestamptz)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.raster_mask_objects AS o
    SET state = 'ready', published_at = greatest(p_now, o.created_at),
        last_used_at = greatest(p_now, o.created_at)
    WHERE o.id = p_id AND o.state = 'pending' AND o.storage_ref = p_ref;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.touch_ready_raster_object(p_id uuid, p_ref jsonb, p_now timestamptz)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.raster_mask_objects AS o
    SET last_used_at = greatest(p_now, o.last_used_at, o.published_at)
    WHERE o.id = p_id AND o.state = 'ready' AND o.storage_ref = p_ref;
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.retire_raster_object(
    p_id uuid, p_state text, p_ref jsonb, p_now timestamptz, p_cutoff timestamptz
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    UPDATE public.raster_mask_objects AS o
    SET state = 'retired', retired_at = greatest(p_now, o.last_used_at, o.created_at)
    WHERE o.id = p_id AND o.state = p_state AND p_state IN ('pending', 'ready')
      AND o.storage_ref = p_ref
      AND (
          (p_state = 'ready' AND coalesce(o.last_used_at, o.published_at) <= p_cutoff)
          OR (p_state = 'pending' AND EXISTS (
              SELECT FROM public.raster_mask_objects AS winner
              WHERE winner.logical_key = o.logical_key AND winner.state = 'ready'
                AND ROW(winner.canonical_sha256, winner.canonical_size_bytes, winner.height,
                        winner.width, winner.runs, winner.storage_encoding)
                    = ROW(o.canonical_sha256, o.canonical_size_bytes, o.height,
                          o.width, o.runs, o.storage_encoding)
          ))
      )
      AND NOT EXISTS (SELECT FROM public.raster_mask_publications AS p WHERE p.object_id = o.id);
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.raster_physical_gc_candidates(
    p_cutoff timestamptz, p_limit integer, p_claim boolean
)
RETURNS SETOF yoao_labeling_v1.raster_physical_receipt
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF p_claim THEN
        RETURN QUERY
        SELECT o.id, o.logical_key::text, o.canonical_sha256::text, o.canonical_size_bytes,
               o.height, o.width, o.runs, o.storage_encoding::text, o.stored_sha256::text,
               o.stored_size_bytes, o.storage_ref, o.state::text, o.created_at,
               o.published_at, o.last_used_at
        FROM public.raster_mask_objects AS o
        WHERE o.state = 'retired'
           OR (o.state = 'ready' AND coalesce(o.last_used_at, o.published_at) <= p_cutoff)
        ORDER BY o.last_attempted_at ASC NULLS FIRST, o.created_at, o.id
        LIMIT greatest(1, least(10000, p_limit)) FOR UPDATE OF o SKIP LOCKED;
    ELSE
        RETURN QUERY
        SELECT o.id, o.logical_key::text, o.canonical_sha256::text, o.canonical_size_bytes,
               o.height, o.width, o.runs, o.storage_encoding::text, o.stored_sha256::text,
               o.stored_size_bytes, o.storage_ref, o.state::text, o.created_at,
               o.published_at, o.last_used_at
        FROM public.raster_mask_objects AS o
        WHERE o.state = 'retired'
           OR (o.state = 'ready' AND coalesce(o.last_used_at, o.published_at) <= p_cutoff)
        ORDER BY o.last_attempted_at ASC NULLS FIRST, o.created_at, o.id
        LIMIT greatest(1, least(10000, p_limit));
    END IF;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.note_raster_gc_attempts(p_ids uuid[], p_times timestamptz[])
RETURNS integer
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count integer;
BEGIN
    UPDATE public.raster_mask_objects AS o
    SET last_attempted_at = greatest(attempt.at, o.last_attempted_at + interval '1 microsecond')
    FROM ROWS FROM (pg_catalog.unnest(p_ids), pg_catalog.unnest(p_times)) AS attempt(id, at)
    WHERE o.id = attempt.id AND o.state IN ('ready', 'retired');
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.settle_raster_gc(p_id uuid, p_ref jsonb)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    DELETE FROM public.raster_mask_objects AS o
    WHERE o.id = p_id AND o.state = 'retired' AND o.storage_ref = p_ref
      AND NOT EXISTS (SELECT FROM public.raster_mask_publications AS p WHERE p.object_id = o.id);
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.open_raster_publication(
    p_id uuid, p_object_id uuid, p_revision integer, p_created_at timestamptz
)
RETURNS void
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    INSERT INTO public.raster_mask_publications (id, object_id, gate_revision, created_at)
    VALUES (p_id, p_object_id, p_revision, p_created_at);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.raster_publication_matches(
    p_id uuid, p_object_id uuid, p_revision integer
)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT FROM public.raster_mask_publications AS p
        WHERE p.id = p_id AND p.object_id = p_object_id AND p.gate_revision = p_revision
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.release_raster_publication(
    p_id uuid, p_object_id uuid, p_revision integer
)
RETURNS boolean
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    DELETE FROM public.raster_mask_publications AS p USING public.raster_mask_objects AS o
    WHERE p.id = p_id AND p.object_id = p_object_id AND p.gate_revision = p_revision
      AND o.id = p.object_id AND o.state = 'pending';
    RETURN FOUND;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.lock_raster_publications(p_ids uuid[])
RETURNS TABLE (
    publication_id uuid, object_id uuid, gate_revision integer,
    id uuid, logical_key text, canonical_sha256 text, canonical_size_bytes integer,
    height integer, width integer, runs integer, storage_encoding text,
    stored_sha256 text, stored_size_bytes integer, storage_ref jsonb,
    state text, created_at timestamptz, published_at timestamptz, last_used_at timestamptz
)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_ids uuid[];
BEGIN
    SELECT array_agg(locked.id) INTO v_ids FROM (
        SELECT p.id FROM public.raster_mask_publications AS p
        WHERE p.id = ANY(p_ids) ORDER BY p.id FOR UPDATE OF p NOWAIT
    ) AS locked;
    RETURN QUERY SELECT p.id, p.object_id, p.gate_revision, r.id, r.logical_key::text,
        r.canonical_sha256::text, r.canonical_size_bytes, r.height, r.width, r.runs,
        r.storage_encoding::text, r.stored_sha256::text, r.stored_size_bytes, r.storage_ref,
        r.state::text, r.created_at, r.published_at, r.last_used_at
    FROM public.raster_mask_publications AS p
    JOIN public.raster_mask_objects AS r ON r.id = p.object_id
    WHERE p.id = ANY(v_ids) ORDER BY p.id;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.consume_raster_publications(
    p_ids uuid[], p_object_ids uuid[], p_revisions integer[]
)
RETURNS integer
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count integer;
BEGIN
    DELETE FROM public.raster_mask_publications AS p
    USING public.raster_mask_objects AS o,
          ROWS FROM (pg_catalog.unnest(p_ids), pg_catalog.unnest(p_object_ids),
                     pg_catalog.unnest(p_revisions)) AS expected(id, object_id, gate_revision)
    WHERE p.id = expected.id AND p.object_id = expected.object_id
      AND p.gate_revision = expected.gate_revision AND p.object_id = o.id AND o.state = 'ready';
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.raster_object_has_publication(p_object_id uuid)
RETURNS boolean
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (SELECT FROM public.raster_mask_publications AS p WHERE p.object_id = p_object_id);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.expire_unlinked_raster_uploads(p_cutoff timestamptz)
RETURNS integer
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    v_count integer;
BEGIN
    DELETE FROM public.raster_mask_uploads AS u
    WHERE u.linked_at IS NULL AND u.created_at <= p_cutoff;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_labeling_v1.stage_raster_object(uuid,text,text,integer,integer,integer,integer,text,text,integer,jsonb,timestamptz),
    yoao_labeling_v1.ready_raster_owner(text,boolean),
    yoao_labeling_v1.lock_raster_owners(uuid[],boolean),
    yoao_labeling_v1.lock_raster_ready_for_publish(uuid),
    yoao_labeling_v1.publish_raster_object(uuid,jsonb,timestamptz),
    yoao_labeling_v1.touch_ready_raster_object(uuid,jsonb,timestamptz),
    yoao_labeling_v1.retire_raster_object(uuid,text,jsonb,timestamptz,timestamptz),
    yoao_labeling_v1.raster_physical_gc_candidates(timestamptz,integer,boolean),
    yoao_labeling_v1.note_raster_gc_attempts(uuid[],timestamptz[]),
    yoao_labeling_v1.settle_raster_gc(uuid,jsonb),
    yoao_labeling_v1.open_raster_publication(uuid,uuid,integer,timestamptz),
    yoao_labeling_v1.raster_publication_matches(uuid,uuid,integer),
    yoao_labeling_v1.release_raster_publication(uuid,uuid,integer),
    yoao_labeling_v1.lock_raster_publications(uuid[]),
    yoao_labeling_v1.consume_raster_publications(uuid[],uuid[],integer[]),
    yoao_labeling_v1.raster_object_has_publication(uuid),
    yoao_labeling_v1.expire_unlinked_raster_uploads(timestamptz) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1.raster_physical_reference_keys(p_key text)
RETURNS SETOF text
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT DISTINCT refs.object_key
    FROM (
        SELECT value #>> '{}' AS object_key
        FROM public.annotations AS a
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(a.geometry, '$.**.object_key') AS key(value)
        WHERE a.is_active IS TRUE
        UNION ALL
        SELECT value #>> '{}'
        FROM public.predictions AS p
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(p.result, '$.**.object_key') AS key(value)
        UNION ALL
        SELECT value #>> '{}'
        FROM public.video_tracker_jobs AS job
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(job.staged_result, '$.**.object_key') AS key(value)
        WHERE job.staged_result IS NOT NULL
          AND job.status IN ('pending_review', 'partially_reviewed', 'cancelled')
          AND (job.completed_at IS NULL OR job.completed_at > now() - interval '24 hours')
        UNION ALL
        SELECT value #>> '{}'
        FROM public.ai_mask_accept_decisions AS d
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(d.response_json, '$.**.object_key') AS key(value)
        WHERE d.expires_at > now()
        UNION ALL
        SELECT value #>> '{}'
        FROM public.mask_annotation_revisions AS r
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(r.geometry, '$.**.object_key') AS key(value)
        WHERE r.expires_at > now()
        UNION ALL
        SELECT value #>> '{}'
        FROM public.mask_qc_runs AS run
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(run.source_snapshot, '$.**.object_key') AS key(value)
        WHERE run.status IN ('pending', 'running')
        UNION ALL
        SELECT i.region_mask_ref ->> 'object_key' FROM public.mask_qc_issues AS i
        UNION ALL
        SELECT value #>> '{}'
        FROM public.mask_repair_batches AS b
        CROSS JOIN LATERAL pg_catalog.jsonb_path_query(b.plan_json, '$.**.object_key') AS key(value)
        WHERE (b.status = 'planned' AND b.receipt_expires_at > now())
           OR b.status IN ('pending', 'running', 'rolling_back')
           OR (b.status IN ('completed', 'partial', 'rollback_failed') AND b.rollback_expires_at > now())
        UNION ALL
        SELECT s.region_mask_ref ->> 'object_key'
        FROM public.mask_review_scopes AS s
        JOIN public.annotations AS a ON a.id = s.annotation_id
            AND a.task_id = s.task_id AND a.project_id = s.project_id
        WHERE a.is_active IS TRUE AND a.version = s.result_annotation_version
        UNION ALL
        SELECT u.object_key FROM public.raster_mask_uploads AS u
        WHERE u.linked_at IS NULL AND u.created_at > now() - interval '24 hours'
    ) AS refs
    WHERE CASE WHEN p_key IS NULL THEN refs.object_key LIKE 'raster-masks/sha256/%'
               ELSE refs.object_key = p_key END
    LIMIT CASE WHEN p_key IS NULL THEN NULL ELSE 1 END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_labeling_v1.raster_physical_reference_keys(text) FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.lock_video_segment_source(p_task_id uuid, p_item_id uuid)
RETURNS uuid
LANGUAGE sql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT item.id FROM public.dataset_items AS item
    WHERE item.id = p_item_id
      AND EXISTS (
          SELECT FROM yoao_labeling_v1.task_rows AS task
          WHERE task.id = p_task_id AND task.dataset_item_id = item.id
      )
    FOR UPDATE OF item NOWAIT;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION yoao_assets_v1.lock_video_segment_source(uuid,uuid) FROM PUBLIC;
