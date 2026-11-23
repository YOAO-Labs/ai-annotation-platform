CREATE TABLE yoao_assets_v1.media_io_claims (
    permit_id uuid NOT NULL REFERENCES public.storage_operations(id) ON DELETE CASCADE,
    step text NOT NULL,
    member text NOT NULL DEFAULT '',
    evidence jsonb,
    PRIMARY KEY (permit_id, step, member)
);
-- aap-statement-break
REVOKE ALL ON TABLE yoao_assets_v1.media_io_claims FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._media_io_permit(p_id uuid, p_location uuid, p_purpose text)
RETURNS boolean LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.storage_operations AS permit
        JOIN public.storage_maintenance_gates AS gate ON gate.location_id = permit.location_id
        WHERE permit.id = p_id AND permit.location_id = p_location AND permit.purpose = p_purpose
          AND permit.revision = gate.revision AND gate.state = 'active'
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._media_io_target(
    p_ref jsonb, p_key text, p_purpose text, p_allow_disabled boolean DEFAULT false
)
RETURNS jsonb LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'location_id', location.id, 'config_revision', location.config_revision,
        'connection_revision', connection.revision, 'purpose', p_purpose,
        'bucket', p_ref ->> 'bucket', 'key', p_key, 'prefix', p_ref ->> 'key_prefix',
        'endpoint', p_ref ->> 'endpoint_url'
    )
    FROM public.storage_locations AS location
    LEFT JOIN public.storage_connections AS connection ON connection.id = location.connection_id
    WHERE location.id = (p_ref ->> 'location_id')::uuid
      AND (p_allow_disabled OR location.enabled) AND location.kind = 's3'
      AND p_ref ->> 'purpose' = p_purpose AND p_ref -> 'schema' = '2'::jsonb
      AND p_purpose IN ('annotations', 'import', 'datasets', 'media-cache')
      AND octet_length(p_key) BETWEEN 1 AND 1024
      AND starts_with(p_key, p_ref ->> 'key_prefix')
      AND CASE WHEN location.id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'::uuid THEN
          connection.id IS NULL AND location.connection_id IS NULL
          AND coalesce(p_ref ->> 'key_prefix', '') = ''
          AND ((CASE WHEN location.config -> 'use_ssl' = 'true'::jsonb THEN 'https://' ELSE 'http://' END)
              || (location.config ->> 'endpoint')) = p_ref ->> 'endpoint_url'
      ELSE
          connection.kind = 's3' AND connection.scope = 'global' AND connection.project_id IS NULL
          AND connection.config ->> 'bucket' = p_ref ->> 'bucket'
          AND rtrim((CASE
              WHEN strpos(connection.config ->> 'endpoint', '://') > 0 THEN ''
              WHEN connection.config -> 'use_ssl' = 'true'::jsonb THEN 'https://' ELSE 'http://' END)
              || (connection.config ->> 'endpoint'), '/') = rtrim(p_ref ->> 'endpoint_url', '/')
          AND coalesce(nullif(regexp_replace(connection.config ->> 'base_prefix', '/$', ''), '') || '/', '')
              || '_aap_managed/' || p_purpose || '/' = p_ref ->> 'key_prefix'
      END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_workspace_v1._media_io_guide(p_id uuid, p_action text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'ref', upload.storage_ref, 'sha256', upload.sha256, 'size', upload.size_bytes,
        'content_type', upload.mime_type,
        'relative', 'projects/' || upload.project_id || '/guide/' || upload.id
    )
    FROM public.guide_asset_uploads AS upload
    WHERE upload.id = p_id AND (
        p_action = 'write' AND upload.state = 'pending'
        OR p_action = 'read' AND upload.state = 'ready'
           AND yoao_workspace_v1.guide_upload_has_reference(upload.id)
        OR p_action = 'delete' AND upload.state = 'retired'
           AND NOT yoao_workspace_v1.guide_upload_has_reference(upload.id)
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1._media_io_comment(p_id uuid, p_action text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'ref', upload.storage_ref, 'sha256', upload.sha256, 'size', upload.size_bytes,
        'content_type', upload.mime_type,
        'relative', 'comment-attachments/' || upload.annotation_id || '/' || upload.id
    )
    FROM public.comment_attachment_uploads AS upload
    WHERE upload.id = p_id AND (
        (p_action = 'write' AND upload.state = 'pending' OR p_action = 'read' AND upload.state = 'ready')
          AND upload.expires_at > statement_timestamp()
        OR p_action = 'delete' AND upload.state = 'retired'
          AND (upload.expires_at <= statement_timestamp()
               OR upload.retired_at + interval '168 hours' <= statement_timestamp())
          AND NOT EXISTS (
              SELECT 1 FROM public.annotation_comments AS comment
              WHERE comment.id = upload.bound_comment_id AND comment.is_active
                AND comment.author_id = upload.owner_id AND comment.annotation_id = upload.annotation_id
                AND comment.project_id = upload.project_id
          )
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1._media_io_raster(p_id uuid, p_action text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'ref', object.storage_ref, 'sha256', object.stored_sha256, 'size', object.stored_size_bytes,
        'content_type', CASE WHEN object.storage_encoding = 'gzip' THEN 'application/gzip' ELSE 'application/json' END,
        'relative', 'raster-masks/objects/' || object.id || '.json' ||
            CASE WHEN object.storage_encoding = 'gzip' THEN '.gz' ELSE '' END
    )
    FROM public.raster_mask_objects AS object
    WHERE object.id = p_id AND (
        p_action = 'write' AND object.state = 'pending'
        OR p_action = 'read' AND object.state = 'ready'
        OR p_action = 'delete' AND object.state = 'retired'
           AND NOT yoao_labeling_v1.raster_object_has_publication(object.id)
           AND NOT EXISTS (SELECT FROM yoao_labeling_v1.raster_physical_reference_keys(object.logical_key))
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.media_io_artifact(
    p_kind text, p_action text, p_id uuid, p_permit uuid, p_claim boolean
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    facts jsonb;
    target jsonb;
    permit_purpose text;
BEGIN
    CASE p_kind
    WHEN 'guide' THEN
        facts := yoao_workspace_v1._media_io_guide(p_id, p_action);
        permit_purpose := 'guide_asset_';
    WHEN 'attachment' THEN
        facts := yoao_labeling_v1._media_io_comment(p_id, p_action);
        permit_purpose := 'comment_attachment_';
    WHEN 'raster' THEN
        facts := yoao_labeling_v1._media_io_raster(p_id, p_action);
        permit_purpose := 'raster_object_';
    ELSE RETURN NULL;
    END CASE;
    permit_purpose := permit_purpose || p_action || '_' || replace(p_id::text, '-', '');
    IF facts IS NULL OR NOT yoao_assets_v1._media_io_permit(
        p_permit, (facts -> 'ref' ->> 'location_id')::uuid, permit_purpose
    ) THEN RETURN NULL;
    END IF;
    target := yoao_assets_v1._media_io_target(
        facts -> 'ref', facts -> 'ref' ->> 'object_key', 'annotations', p_action = 'delete'
    );
    IF target IS NULL OR target ->> 'key' IS DISTINCT FROM
            (target ->> 'prefix') || (facts ->> 'relative') THEN RETURN NULL;
    END IF;
    IF p_claim AND p_action IN ('write', 'delete') THEN
        INSERT INTO yoao_assets_v1.media_io_claims (permit_id, step)
        VALUES (p_permit, p_kind || '_' || p_action) ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN RETURN NULL; END IF;
    END IF;
    RETURN target || (facts - 'ref' - 'relative');
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.media_io_cache_read(p_kind text, p_id uuid, p_relative text, p_permit uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    output jsonb;
    ref jsonb;
    member jsonb;
    target jsonb;
    family text;
    permit_purpose text;
    object_prefix text;
    object_key text;
    version text;
    etag text;
BEGIN
    IF p_relative IS NULL OR length(p_relative) NOT BETWEEN 1 AND 255
       OR p_relative COLLATE pg_catalog."C" !~ '^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$'
       OR p_relative ~ '(^|/)[.]{1,2}(/|$)' THEN RETURN NULL;
    END IF;
    CASE p_kind
    WHEN 'thumbnail' THEN
        output := yoao_assets_v1.legacy_thumbnail_output(p_id, false);
        permit_purpose := 'thumbnail_';
        family := 'thumbnails';
    WHEN 'pyramid' THEN
        SELECT to_jsonb(proof) INTO output FROM yoao_assets_v1.read_legacy_pyramid_output(p_id, false) AS proof;
        permit_purpose := 'image_pyramid_';
        family := 'image-pyramids';
    WHEN 'video' THEN
        SELECT to_jsonb(proof) INTO output FROM yoao_assets_v1.legacy_video_cache_output(p_id, false) AS proof;
        permit_purpose := 'video_derivative_';
        family := 'video-derivatives';
    ELSE RETURN NULL;
    END CASE;
    ref := output -> 'storage_ref';
    permit_purpose := permit_purpose || replace(p_id::text, '-', '');
    object_prefix := (ref ->> 'key_prefix') || family || '/objects/' || p_id || '/';
    IF output IS NULL OR ref ->> 'prefix' IS DISTINCT FROM object_prefix OR NOT yoao_assets_v1._media_io_permit(
           p_permit, (ref ->> 'location_id')::uuid, permit_purpose
       ) OR output ->> 'state' = 'retiring' AND EXISTS (
           SELECT FROM public.storage_operations AS other WHERE other.purpose = permit_purpose AND other.id <> p_permit
       ) THEN RETURN NULL;
    END IF;
    object_key := object_prefix || p_relative;
    IF p_kind = 'thumbnail' THEN
        IF p_relative <> 'thumbnail.webp' OR output -> 'uncertain_write' <> 'false'::jsonb
           OR output ->> 'owner_kind' NOT IN ('dataset_thumbnail', 'task_thumbnail', 'directory_preview')
        THEN RETURN NULL; END IF;
        member := jsonb_build_object('sha256', output -> 'sha256', 'size_bytes', output -> 'size_bytes');
        version := output ->> 'version_id';
        etag := output ->> 'etag';
    ELSE
        member := output -> 'inventory' -> 'objects' -> object_key;
        version := output -> 'fixture_request_versions' -> object_key ->> 'version_id';
        etag := output -> 'fixture_request_versions' -> object_key ->> 'etag';
    END IF;
    IF member IS NULL OR member ->> 'sha256' !~ '^[a-f0-9]{64}$'
       OR (member ->> 'size_bytes')::bigint NOT BETWEEN 1 AND 5368709120 THEN RETURN NULL;
    END IF;
    target := yoao_assets_v1._media_io_target(ref, object_key, 'media-cache', output ->> 'state' = 'retiring');
    RETURN target || jsonb_build_object('sha256', member -> 'sha256', 'size', member -> 'size_bytes',
        'version', nullif(version, 'null'), 'etag', etag);
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._media_io_import_target(p_ref jsonb, p_prefix text, p_key text)
RETURNS jsonb LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT yoao_assets_v1._media_io_target(
        p_ref || jsonb_build_object('schema', 2, 'key_prefix', left(p_ref ->> 'key_prefix', -length(p_prefix))),
        p_key, 'import', true
    )
    WHERE right(p_ref ->> 'key_prefix', length(p_prefix)) = p_prefix
      AND starts_with(p_key, p_ref ->> 'key_prefix');
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._media_io_creation_target(r public.managed_original_creations)
RETURNS jsonb LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT yoao_assets_v1._media_io_target(
        coalesce(CASE WHEN r.configuration -> 'original_ref' -> 'schema' IN ('1'::jsonb, '2'::jsonb)
            THEN r.configuration -> 'original_ref' || jsonb_build_object('schema',2) END, jsonb_build_object(
            'schema', 2, 'purpose', CASE WHEN r.owner_kind = 'task' THEN 'annotations' ELSE 'datasets' END,
            'location_id', r.location_id, 'bucket', r.bucket, 'key_prefix', '',
            'endpoint_url', r.configuration ->> 'endpoint'
        )),
        r.object_key, CASE WHEN r.owner_kind = 'task' THEN 'annotations' ELSE 'datasets' END
    )
    WHERE r.configuration -> 'original_ref' IS NULL OR (
        r.configuration -> 'original_ref' -> 'schema' IN ('1'::jsonb,'2'::jsonb)
        AND r.configuration -> 'original_ref' ->> 'object_key' = r.object_key
        AND r.configuration -> 'original_ref' ->> 'bucket' = r.bucket
        AND r.configuration -> 'original_ref' ->> 'storage_identity' = r.storage_identity
        AND r.configuration -> 'original_ref' ->> 'location_id' = r.location_id::text
    );
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.media_io_original_read(p_kind text, p_id uuid, p_permit uuid)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    facts jsonb;
    target jsonb;
    permit_location uuid;
    creation public.managed_original_creations%ROWTYPE;
    purpose text := 'datasets';
BEGIN
    SELECT operation.location_id INTO permit_location FROM public.storage_operations AS operation
    WHERE operation.id = p_permit;
    IF p_kind = 'operator' THEN
        SELECT source.* INTO creation FROM yoao_assets_v1.local_original_receipt(p_id) AS source
        WHERE source.status = 'verified' AND source.location_id = permit_location;
        IF NOT FOUND OR NOT yoao_assets_v1._media_io_permit(
            p_permit, permit_location, yoao_assets_v1._operator_read_permit_purpose(p_id)
        ) THEN RETURN NULL; END IF;
        IF NOT EXISTS (
            SELECT FROM yoao_assets_v1.media_io_claims AS stamp
            WHERE stamp.permit_id = p_permit AND stamp.step = 'operator_read'
              AND stamp.evidence ->> 'creation_id' = creation.id::text
              AND stamp.evidence -> 'actor' = coalesce(to_jsonb(creation.requested_by), 'null'::jsonb)
        ) AND NOT (creation.configuration ? 'fixture_original_target') THEN RETURN NULL; END IF;
        RETURN yoao_assets_v1._media_io_creation_target(creation) || jsonb_build_object(
            'sha256', creation.sha256, 'size', creation.size_bytes, 'etag', creation.etag,
            'version', nullif(creation.version_id, 'null')
        );
    END IF;
    IF NOT yoao_assets_v1._media_io_permit(
        p_permit, permit_location, 'managed_original_read_' || replace(p_id::text, '-', '')
    ) THEN RETURN NULL; END IF;
    CASE p_kind
    WHEN 'dataset_asset' THEN
        SELECT jsonb_build_object(
            'ref', replica.storage_ref, 'key', replica.object_key, 'bucket', replica.bucket,
            'sha256', asset.checksum_value, 'size', asset.size_bytes
        ) INTO facts
        FROM public.dataset_assets AS asset
        JOIN public.dataset_asset_locations AS replica ON replica.asset_id = asset.id
        WHERE asset.id = p_id AND replica.location_id = permit_location
          AND asset.verification_state = 'verified' AND asset.checksum_algorithm = 'sha256'
          AND replica.role = 'managed_original' AND replica.reference_state = 'present'
          AND NOT EXISTS (
              SELECT FROM public.dataset_asset_locations AS other
              WHERE other.asset_id = asset.id AND other.role = 'managed_original'
                AND other.reference_state = 'present' AND (other.location_id, other.bucket, other.object_key)
                    IS DISTINCT FROM (replica.location_id, replica.bucket, replica.object_key)
          );
    WHEN 'dataset_auxiliary' THEN
        SELECT jsonb_build_object(
            'ref', original.storage_ref, 'key', original.object_key, 'bucket', original.bucket,
            'sha256', original.sha256, 'size', original.size_bytes
        ) INTO facts
        FROM public.dataset_auxiliary_originals AS original
        WHERE original.id = p_id AND original.location_id = permit_location;
    WHEN 'task_original' THEN
        purpose := 'annotations';
        SELECT jsonb_build_object(
            'ref', original.storage_ref, 'key', original.object_key, 'bucket', original.bucket,
            'sha256', original.sha256, 'size', original.size_bytes
        ) INTO facts
        FROM public.task_originals AS original
        WHERE original.id = p_id AND original.location_id = permit_location;
    WHEN 'creation' THEN
        SELECT source.* INTO creation FROM yoao_assets_v1.local_original_receipt(p_id) AS source
        WHERE source.status = 'verified' AND source.location_id = permit_location;
        IF NOT FOUND THEN RETURN NULL; END IF;
        RETURN yoao_assets_v1._media_io_creation_target(creation) ||
            jsonb_build_object('sha256', creation.sha256, 'size', creation.size_bytes);
    ELSE RETURN NULL;
    END CASE;
    IF facts IS NULL OR facts -> 'ref' ->> 'location_id' IS DISTINCT FROM permit_location::text
       OR facts -> 'ref' ->> 'bucket' IS DISTINCT FROM facts ->> 'bucket'
       OR facts -> 'ref' ->> 'object_key' IS DISTINCT FROM facts ->> 'key' THEN RETURN NULL;
    END IF;
    IF facts -> 'ref' -> 'schema' NOT IN ('1'::jsonb, '2'::jsonb) THEN RETURN NULL; END IF;
    target := yoao_assets_v1._media_io_target(
        facts -> 'ref' || jsonb_build_object('schema',2), facts ->> 'key', purpose);
    RETURN target || jsonb_build_object('sha256', facts -> 'sha256', 'size', facts -> 'size');
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.media_io_creation(
    p_id uuid, p_permit uuid, p_source_permit uuid, p_step text, p_claim boolean
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    r public.managed_original_creations%ROWTYPE;
    target jsonb;
    input jsonb;
    stage_ref jsonb;
    prepared jsonb;
    receipt jsonb;
    stage_prefix text := '__aap_upload_staging__/' || p_id || '/';
BEGIN
    SELECT proof.* INTO r FROM yoao_assets_v1.local_original_receipt(p_id) AS proof;
    IF NOT FOUND THEN RETURN NULL; END IF;
    IF r.origin = 'direct' AND p_step IN ('sign', 'inspect', 'copy', 'complete', 'abort', 'verify') THEN
        stage_ref := coalesce(r.staging_ref, jsonb_build_object(
            'schema', 1, 'purpose', 'import', 'location_id', '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7',
            'key_prefix', stage_prefix, 'endpoint_url', r.configuration ->> 'endpoint', 'bucket', r.staging_bucket
        ));
        IF (stage_ref ->> 'key_prefix') || 'payload' IS DISTINCT FROM r.staging_key
           OR stage_ref ->> 'bucket' IS DISTINCT FROM r.staging_bucket
           OR NOT yoao_assets_v1._media_io_permit(
               p_source_permit, (stage_ref ->> 'location_id')::uuid,
               'managed_original_' || CASE WHEN p_step = 'sign' THEN 'sign_' ELSE 'source_' END
                   || replace(p_id::text, '-', '')
           ) THEN RETURN NULL;
        END IF;
        input := yoao_assets_v1._media_io_import_target(stage_ref, stage_prefix, r.staging_key);
        IF input IS NULL THEN RETURN NULL; END IF;
    END IF;
    IF p_step = 'sign' THEN
        IF r.origin <> 'direct' OR r.status <> 'allocated' OR r.allocated_at + interval '15 minutes' <= statement_timestamp()
        THEN RETURN NULL; END IF;
        RETURN input || jsonb_build_object('content_type', r.content_type, 'expires_in',
            least(900, floor(extract(epoch FROM r.allocated_at + interval '15 minutes' - statement_timestamp()))::integer));
    END IF;
    IF r.operation_id IS DISTINCT FROM p_permit OR NOT yoao_assets_v1._media_io_permit(
           p_permit, r.location_id, CASE WHEN r.owner_kind = 'task'
               THEN 'task_original_write_' || replace(p_id::text, '-', '') ELSE 'managed_original_creation' END
       ) THEN RETURN NULL; END IF;
    target := yoao_assets_v1._media_io_creation_target(r);
    IF target IS NULL OR target ->> 'bucket' IS DISTINCT FROM r.bucket THEN RETURN NULL; END IF;
    CASE p_step
    WHEN 'inspect' THEN
        IF r.origin <> 'direct' OR r.status <> 'preparing' OR r.source IS NOT NULL THEN RETURN NULL; END IF;
    WHEN 'copy' THEN
        IF r.origin <> 'direct' OR r.status <> 'preparing' OR r.source IS NULL THEN RETURN NULL; END IF;
    WHEN 'stream' THEN
        IF r.origin = 'direct' OR r.status <> 'preparing' THEN RETURN NULL; END IF;
    WHEN 'complete', 'abort', 'verify' THEN
        IF NOT (p_step = 'complete' AND r.status = 'writing'
            OR p_step = 'abort' AND r.status IN ('preparing', 'prepared')
            OR p_step = 'verify' AND r.status = 'written') THEN RETURN NULL; END IF;
        SELECT claim.evidence INTO prepared FROM yoao_assets_v1.media_io_claims AS claim
        WHERE claim.permit_id = p_permit AND claim.step = 'original_prepare';
        IF prepared IS NULL OR p_step <> 'abort' AND (
            r.prepared -> 'source_owned' IS DISTINCT FROM 'true'::jsonb
            OR r.sha256 IS DISTINCT FROM prepared ->> 'sha256'
            OR r.md5 IS DISTINCT FROM prepared ->> 'md5'
            OR r.size_bytes IS DISTINCT FROM (prepared ->> 'size_bytes')::bigint
        ) THEN RETURN NULL; END IF;
        IF p_step = 'verify' THEN
            SELECT claim.evidence INTO receipt FROM yoao_assets_v1.media_io_claims AS claim
            WHERE claim.permit_id = p_permit AND claim.step = 'original_finish';
            IF receipt IS NULL OR receipt IS DISTINCT FROM r.receipt THEN RETURN NULL; END IF;
        END IF;
    ELSE RETURN NULL;
    END CASE;
    IF p_claim THEN
        INSERT INTO yoao_assets_v1.media_io_claims (permit_id, step)
        VALUES (p_permit, 'original_' || CASE WHEN p_step IN ('copy', 'stream') THEN 'prepare'
            WHEN p_step IN ('complete', 'abort') THEN 'finish' ELSE p_step END)
        ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN RETURN NULL; END IF;
    END IF;
    RETURN jsonb_build_object(
        'target', target, 'staging', input, 'max_bytes', r.max_bytes, 'timeout', r.timeout_seconds,
        'content_type', r.content_type, 'source', r.source, 'prepared', prepared, 'receipt', receipt,
        'authority_kind', r.authority ->> 'kind'
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.media_io_creation_evidence(
    p_id uuid, p_permit uuid, p_stage text, p_evidence jsonb
) RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    WITH result AS (
        UPDATE yoao_assets_v1.media_io_claims AS claim
        SET evidence = p_evidence
        FROM public.managed_original_creations AS creation
        WHERE creation.id = p_id AND creation.operation_id = claim.permit_id
          AND claim.permit_id = p_permit AND claim.step =
              CASE WHEN p_stage = 'complete' THEN 'original_finish' ELSE 'original_prepare' END
          AND claim.evidence IS NULL
          AND (p_stage = 'complete' AND creation.status = 'writing'
            OR p_stage = 'prepare' AND creation.status = 'preparing')
          AND jsonb_typeof(p_evidence) = 'object'
        RETURNING 1
    ) SELECT EXISTS (SELECT FROM result);
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.begin_saved_operator_original(p_request jsonb)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT yoao_assets_v1.begin_operator_auxiliary(p_request) #- '{data,destination}';
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.saved_operator_item_original(p_dataset uuid, p_item uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT yoao_assets_v1.operator_item_original_material(p_dataset, p_item) #- '{data,source,destination}';
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.saved_operator_auxiliary_original(p_dataset uuid, p_original uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT yoao_assets_v1.operator_auxiliary_material(p_dataset, p_original) #- '{data,destination}';
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.open_saved_operator_original_operation(p_creation uuid)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    ticket jsonb;
    actor uuid;
BEGIN
    SELECT c.requested_by INTO actor FROM yoao_assets_v1.local_original_receipt(p_creation) AS c;
    IF actor IS NOT NULL AND NOT EXISTS (
        SELECT FROM yoao_identity_v1.read_actor_facts(actor) AS known WHERE known.is_active
    ) THEN RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_scope_mismatch'); END IF;
    ticket := yoao_assets_v1.open_operator_storage_operation(p_creation);
    IF ticket -> 'data' ->> 'mode' = 'read' THEN
        INSERT INTO yoao_assets_v1.media_io_claims (permit_id, step, evidence)
        VALUES ((ticket -> 'data' ->> 'operation_id')::uuid, 'operator_read',
            jsonb_build_object('creation_id', p_creation, 'actor', actor));
    END IF;
    RETURN ticket;
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.media_io_thumbnail(
    p_action text, p_id uuid, p_permit uuid, p_claim boolean
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    output jsonb;
    ref jsonb;
    member jsonb;
    target jsonb;
    object_prefix text;
BEGIN
    IF p_action = 'write' THEN
        SELECT jsonb_build_object('storage_ref', proof.storage_ref, 'encoded', proof.input_ref -> 'encoded')
        INTO output FROM public.thumbnail_outputs AS proof
        WHERE proof.id = p_id AND proof.state = 'building' AND proof.writer_until > statement_timestamp();
        member := output -> 'encoded';
    ELSIF p_action = 'delete' THEN
        output := yoao_assets_v1.legacy_thumbnail_output(p_id, false);
        IF output IS NULL OR yoao_assets_v1.legacy_thumbnail_gc_admit(
            p_id, jsonb_build_array(output -> 'storage_ref', output -> 'sha256', output -> 'size_bytes',
                output -> 'etag', output -> 'version_id'), p_permit, statement_timestamp(), 3600
        ) IS NULL THEN RETURN NULL; END IF;
        member := jsonb_build_object('sha256', output -> 'sha256', 'size_bytes', output -> 'size_bytes');
    ELSE RETURN NULL;
    END IF;
    ref := output -> 'storage_ref';
    object_prefix := (ref ->> 'key_prefix') || 'thumbnails/objects/' || p_id || '/';
    IF ref ->> 'prefix' IS DISTINCT FROM object_prefix OR member IS NULL
       OR (member ->> 'size_bytes')::integer NOT BETWEEN 1 AND 2097152
       OR member ->> 'sha256' !~ '^[a-f0-9]{64}$'
       OR NOT yoao_assets_v1._media_io_permit(
           p_permit, (ref ->> 'location_id')::uuid, 'thumbnail_' || replace(p_id::text, '-', '')
       ) THEN RETURN NULL;
    END IF;
    target := yoao_assets_v1._media_io_target(ref, object_prefix || 'thumbnail.webp', 'media-cache', p_action = 'delete');
    IF target IS NULL THEN RETURN NULL; END IF;
    IF p_claim THEN
        INSERT INTO yoao_assets_v1.media_io_claims (permit_id, step)
        VALUES (p_permit, 'thumbnail_' || p_action) ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN RETURN NULL; END IF;
    END IF;
    RETURN target || jsonb_build_object('sha256', member -> 'sha256', 'size', member -> 'size_bytes',
        'version', nullif(output ->> 'version_id', 'null'), 'etag', output ->> 'etag');
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_labeling_v1._media_io_mask_upload(p_id uuid)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT jsonb_build_object(
        'ref', upload.storage_ref, 'prefix', prefix.value,
        'relative', substr(upload.logical_object_key, length(prefix.value) + 1),
        'content_type', upload.content_type, 'expires_at', upload.upload_expires_at
    )
    FROM public.mask_format_uploads AS upload
    CROSS JOIN LATERAL (
        SELECT 'mask-formats/' || upload.project_id || '/' || upload.uploader_id || '/' || upload.id || '/' AS value
    ) AS prefix
    WHERE upload.id = p_id AND starts_with(upload.logical_object_key, prefix.value)
      AND substr(upload.logical_object_key, length(prefix.value) + 1) ~ '^[A-Za-z0-9_-][A-Za-z0-9._-]*$';
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.media_io_mask_upload(p_id uuid, p_permit uuid, p_action text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    facts jsonb;
    ref jsonb;
    target jsonb;
    expiry integer := 0;
BEGIN
    facts := yoao_labeling_v1._media_io_mask_upload(p_id);
    ref := facts -> 'ref';
    IF p_action NOT IN ('read', 'sign') OR facts IS NULL OR NOT yoao_assets_v1._media_io_permit(
        p_permit, (ref ->> 'location_id')::uuid, 'mask_format_' || p_action || '_' || replace(p_id::text, '-', '')
    ) THEN RETURN NULL; END IF;
    IF p_action = 'sign' THEN
        expiry := least(900, floor(extract(epoch FROM
            (facts ->> 'expires_at')::timestamptz - statement_timestamp()))::integer);
        IF expiry <= 0 THEN RETURN NULL; END IF;
    END IF;
    target := yoao_assets_v1._media_io_import_target(ref, facts ->> 'prefix',
        (ref ->> 'key_prefix') || (facts ->> 'relative'));
    RETURN target || jsonb_build_object('content_type', facts -> 'content_type', 'expires_in', expiry);
END;
$function$;
-- aap-statement-break
CREATE TABLE yoao_assets_v1.media_io_derivative_manifests (
    kind text NOT NULL CHECK (kind IN ('pyramid', 'video')),
    output_id uuid NOT NULL,
    lease_token uuid NOT NULL,
    inventory jsonb NOT NULL CHECK (jsonb_typeof(inventory) = 'object'),
    PRIMARY KEY (kind, output_id)
);
-- aap-statement-break
REVOKE ALL ON TABLE yoao_assets_v1.media_io_derivative_manifests FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.media_io_derivative(
    p_kind text, p_action text, p_id uuid, p_permit uuid, p_token uuid,
    p_inventory jsonb, p_claim boolean
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    output jsonb;
    ref jsonb;
    expected jsonb;
    target jsonb;
    purpose text;
    family text;
    object_prefix text;
    revision integer;
BEGIN
    IF p_action NOT IN ('build', 'delete') THEN RETURN NULL; END IF;
    IF p_kind = 'pyramid' THEN
        family := 'image-pyramids';
        purpose := 'image_pyramid_';
        IF p_action = 'build' THEN
            SELECT to_jsonb(proof.output) || jsonb_build_object(
                'source_fingerprint', (proof.selected).source_fingerprint
            ) INTO output
            FROM public.image_pyramid_outputs AS candidate
            CROSS JOIN LATERAL yoao_assets_v1.read_legacy_pyramid_context(
                candidate.asset_id, candidate.generation, candidate.id, false
            ) AS proof
            WHERE candidate.id = p_id AND (proof.output).state = 'building'
              AND (proof.output).lease_token = p_token AND (proof.selected).lease_token = p_token
              AND (proof.selected).status = 'building' AND (proof.selected).lease_expires_at > statement_timestamp()
              AND (proof.asset).building_generation = candidate.generation;
        ELSE
            SELECT to_jsonb(proof) INTO output FROM yoao_assets_v1.read_legacy_pyramid_output(p_id, false) AS proof
            WHERE proof.state = 'retiring';
            IF output IS NULL OR yoao_assets_v1.check_legacy_pyramid_retirement(
                p_id, output -> 'storage_ref', p_permit
            ) IS NOT NULL THEN RETURN NULL; END IF;
        END IF;
    ELSIF p_kind = 'video' THEN
        family := 'video-derivatives';
        purpose := 'video_derivative_';
        IF p_action = 'build' THEN
            SELECT to_jsonb(proof) INTO output
            FROM public.video_derivative_outputs AS candidate
            CROSS JOIN LATERAL yoao_assets_v1.legacy_video_attempt(
                candidate.dataset_item_id, candidate.kind, candidate.id, 'read'
            ) AS proof
            WHERE candidate.id = p_id AND proof.state = 'building'
              AND proof.lease_token = p_token AND proof.lease_expires_at > statement_timestamp()
              AND yoao_assets_v1._legacy_video_building_link(candidate);
        ELSE
            SELECT to_jsonb(proof) INTO output FROM yoao_assets_v1.legacy_video_sweep_attempt(p_id) AS proof;
            SELECT permit.revision INTO revision FROM public.storage_operations AS permit WHERE permit.id = p_permit;
            IF revision IS NULL OR NOT yoao_assets_v1.legacy_video_sweep_permit(p_id, revision)
               OR output IS NULL THEN RETURN NULL; END IF;
        END IF;
    ELSE RETURN NULL;
    END IF;
    ref := output -> 'storage_ref';
    object_prefix := (ref ->> 'key_prefix') || family || '/objects/' || p_id || '/';
    IF output IS NULL OR ref ->> 'prefix' IS DISTINCT FROM object_prefix
       OR NOT yoao_assets_v1._media_io_permit(
           p_permit, (ref ->> 'location_id')::uuid, purpose || replace(p_id::text, '-', '')
       ) THEN RETURN NULL;
    END IF;
    target := yoao_assets_v1._media_io_target(ref, object_prefix, 'media-cache', p_action = 'delete');
    IF target IS NULL THEN RETURN NULL; END IF;
    IF p_claim THEN
        INSERT INTO yoao_assets_v1.media_io_claims(permit_id, step)
        VALUES (p_permit, 'derivative_' || p_action) ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF p_action = 'build' THEN
            INSERT INTO yoao_assets_v1.media_io_derivative_manifests(kind, output_id, lease_token, inventory)
            VALUES (p_kind, p_id, p_token, p_inventory) ON CONFLICT DO NOTHING;
            IF NOT FOUND THEN RETURN NULL; END IF;
        END IF;
    END IF;
    SELECT manifest.inventory INTO expected FROM yoao_assets_v1.media_io_derivative_manifests AS manifest
    WHERE manifest.kind = p_kind AND manifest.output_id = p_id
      AND (p_action = 'delete' OR manifest.lease_token = p_token);
    RETURN target || jsonb_build_object(
        'generation', output -> 'generation', 'source_fingerprint', output -> 'source_fingerprint',
        'kind', output -> 'kind', 'spec', output -> 'spec', 'metadata_output_id', output -> 'metadata_output_id',
        'inventory', expected,
        'published', output -> 'inventory' -> 'objects'
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.media_io_original_cache(
    p_id uuid, p_permit uuid, p_lease uuid, p_action text, p_claim boolean
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    entry record;
    prefix text;
    target jsonb;
BEGIN
    SELECT candidate.id, candidate.location_id, candidate.asset_id, candidate.state, candidate.writer_until,
        candidate.expires_at, candidate.storage_ref, candidate.sha256, candidate.size_bytes
    INTO entry FROM public.original_cache_entries AS candidate
    WHERE candidate.id = p_id;
    IF NOT FOUND OR entry.storage_ref IS NULL OR NOT yoao_assets_v1._media_io_permit(
        p_permit, entry.location_id, 'original_cache_' || p_action || '_' || replace(p_id::text, '-', '')
    ) THEN RETURN NULL; END IF;
    IF NOT (
        p_action = 'write' AND entry.state = 'uploading' AND entry.writer_until > statement_timestamp()
        OR p_action = 'read' AND entry.state = 'ready' AND entry.expires_at > statement_timestamp()
           AND EXISTS (SELECT FROM public.original_cache_read_leases AS lease
              WHERE lease.id = p_lease AND lease.entry_id = entry.id AND lease.expires_at > statement_timestamp())
        OR p_action = 'delete' AND entry.state = 'deleting' AND entry.writer_until <= statement_timestamp()
           AND NOT EXISTS (SELECT FROM public.original_cache_read_leases AS lease
              WHERE lease.entry_id = entry.id AND lease.expires_at > statement_timestamp())
           AND NOT EXISTS (SELECT FROM public.storage_operations AS other WHERE other.purpose IN (
               'original_cache_read_' || replace(p_id::text, '-', ''),
               'original_cache_write_' || replace(p_id::text, '-', '')
           ))
    ) THEN RETURN NULL; END IF;
    prefix := (entry.storage_ref ->> 'key_prefix') || 'original-cache/v1/' ||
        entry.asset_id || '/' || entry.sha256 || '/' || entry.id || '/';
    IF entry.storage_ref ->> 'prefix' IS DISTINCT FROM prefix THEN RETURN NULL; END IF;
    target := yoao_assets_v1._media_io_target(entry.storage_ref, prefix || 'payload', 'media-cache', p_action = 'delete');
    IF target IS NULL THEN RETURN NULL; END IF;
    IF p_claim THEN
        INSERT INTO yoao_assets_v1.media_io_claims(permit_id, step)
        VALUES (p_permit, 'original_cache_' || p_action) ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN RETURN NULL; END IF;
    END IF;
    RETURN target || jsonb_build_object('sha256', entry.sha256, 'size', entry.size_bytes);
END;
$function$;
-- aap-statement-break
CREATE TABLE yoao_assets_v1.embedded_media_claim_key (
    singleton boolean PRIMARY KEY CHECK (singleton),
    secret bytea NOT NULL CHECK (octet_length(secret) = 32)
);
-- aap-statement-break
INSERT INTO yoao_assets_v1.embedded_media_claim_key VALUES (true, public.gen_random_bytes(32));
-- aap-statement-break
REVOKE ALL ON TABLE yoao_assets_v1.embedded_media_claim_key FROM PUBLIC;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1._embedded_original_builtin(p_origin text)
RETURNS boolean LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp AS $function$
    SELECT EXISTS (
        SELECT FROM public.storage_bindings WHERE purpose = 'datasets'
          AND location_id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
    ) AND (p_origin <> 'direct' OR EXISTS (
        SELECT FROM public.storage_bindings WHERE purpose = 'import'
          AND location_id = '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7'
    ));
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.prove_embedded_admitted_original(p_request jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    authority jsonb := p_request -> 'authority';
    actor uuid := (p_request ->> 'requested_by')::uuid;
    dataset uuid := (p_request ->> 'dataset_id')::uuid;
    item uuid := (p_request ->> 'item_id')::uuid;
    publication uuid := (p_request ->> 'publication_id')::uuid;
    origin text := p_request ->> 'origin';
    admitted boolean := false;
    proof jsonb;
    secret bytea;
BEGIN
    IF p_request ->> 'kind' IS DISTINCT FROM 'role_media' OR octet_length(p_request::text) > 16384
       OR authority ->> 'item_id' IS DISTINCT FROM item::text
       OR NOT yoao_assets_v1._embedded_original_builtin(origin)
       OR NOT EXISTS (SELECT FROM yoao_identity_v1.read_actor_facts(actor) AS known WHERE known.is_active)
    THEN RETURN NULL; END IF;
    IF authority ->> 'kind' = 'dataset_upload' AND origin = 'direct' AND p_request ->> 'mode' = 'write' THEN
        SELECT EXISTS (
            SELECT FROM public.dataset_upload_intents AS intent
            WHERE intent.item_id = item AND intent.dataset_id = dataset AND intent.actor_id = actor
              AND intent.state = 'pending' AND intent.publication_id = publication
              AND authority ->> 'publication_id' = publication::text
              AND intent.file_path = p_request ->> 'file_path'
              AND intent.file_name = p_request ->> 'logical_path'
              AND intent.file_type = p_request ->> 'file_type'
              AND intent.content_type = p_request ->> 'content_type'
              AND intent.staging_key = p_request -> 'staging' ->> 'key'
              AND p_request -> 'max_bytes' = '5368709120'::jsonb
        ) INTO admitted;
    ELSIF authority ->> 'kind' = 'dataset_source_ingest' THEN
        SELECT EXISTS (
            SELECT FROM public.dataset_source_ingest_operations AS operation
            JOIN public.dataset_source_ingest_entries AS member ON member.operation_id = operation.operation_id
            WHERE operation.operation_id = (authority ->> 'operation_id')::uuid
              AND operation.dataset_id = dataset AND operation.actor_id = actor AND operation.state = 'preparing'
              AND member.item_id = item AND member.status = 'pending' AND member.planned_status = 'pending'
              AND member.batch_id = (authority ->> 'batch_id')::uuid
              AND member.entry_index = (authority ->> 'entry_index')::integer
              AND member.file_path = p_request ->> 'file_path'
              AND member.source ->> 'relpath' = p_request ->> 'logical_path'
              AND member.source ->> 'file_type' = p_request ->> 'file_type'
              AND member.source ->> 'content_type' = p_request ->> 'content_type'
              AND member.source -> 'file_size' = p_request -> 'max_bytes'
              AND NOT p_request ? 'staging'
              AND (operation.kind = 'zip' AND origin = 'zip' AND p_request ->> 'mode' = 'write'
                   AND member.publication_id = publication AND authority ->> 'publication_id' = publication::text
                   OR operation.kind = 'scan' AND origin = 'directory_copy' AND p_request ->> 'mode' = 'adopt'
                   AND member.source ->> 'source_key' = member.file_path
                   AND (member.publication_id = publication OR member.publication_id IS NULL))
        ) INTO admitted;
    END IF;
    IF NOT admitted THEN RETURN NULL; END IF;
    proof := jsonb_build_object('request', p_request, 'xid', pg_current_xact_id()::text,
        'deadline', floor(extract(epoch FROM clock_timestamp()))::bigint + 120, 'login', session_user);
    SELECT key.secret INTO STRICT secret FROM yoao_assets_v1.embedded_media_claim_key AS key;
    RETURN proof || jsonb_build_object('mac', encode(public.hmac(convert_to(proof::text, 'UTF8'), secret, 'sha256'), 'hex'));
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.begin_embedded_admitted_original(p_proof jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    secret bytea;
    request jsonb := p_proof -> 'request';
    reply jsonb;
BEGIN
    IF p_proof ->> 'login' IS DISTINCT FROM session_user::text
       OR p_proof ->> 'mac' IS NULL OR p_proof ->> 'xid' IS NULL
       OR p_proof -> 'deadline' IS NULL OR (p_proof ->> 'deadline')::bigint <= extract(epoch FROM clock_timestamp())
    THEN RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable'); END IF;
    SELECT key.secret INTO STRICT secret FROM yoao_assets_v1.embedded_media_claim_key AS key;
    IF p_proof ->> 'mac' IS DISTINCT FROM
        encode(public.hmac(convert_to((p_proof - 'mac')::text, 'UTF8'), secret, 'sha256'), 'hex')
       OR pg_xact_status((p_proof ->> 'xid')::xid8) IS DISTINCT FROM 'in progress'
       OR NOT yoao_assets_v1._embedded_original_builtin(request ->> 'origin')
       OR NOT EXISTS (
           SELECT FROM yoao_identity_v1.read_actor_facts((request ->> 'requested_by')::uuid) AS known
           WHERE known.is_active
       )
    THEN RETURN yoao_assets_v1._managed_original_reply(NULL, 'creation_target_unavailable'); END IF;
    reply := yoao_assets_v1.begin_operator_auxiliary(request);
    IF reply -> 'ok' = 'true'::jsonb AND reply -> 'data' ->> 'location_id'
        IS DISTINCT FROM '4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7' THEN
        RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'creation_target_unavailable';
    END IF;
    RETURN reply #- '{data,destination}';
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.media_io_prediction_input(
    p_id uuid, p_permit uuid, p_action text, p_member text, p_claim boolean
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    owned record;
    ref jsonb;
    relative_root text := 'prediction-inputs/' || p_id || '/';
    permit_purpose text := 'prediction_input_' || replace(p_id::text, '-', '');
    target jsonb;
    member jsonb;
BEGIN
    SELECT lease.id, lease.state, lease.expires_at, lease.storage_ref, lease.inventory,
        lease.contract_version, lease.maintenance_revision
    INTO owned FROM public.prediction_input_leases AS lease WHERE lease.id = p_id;
    IF NOT FOUND OR owned.contract_version NOT IN (2,3) THEN RETURN NULL; END IF;
    ref := owned.storage_ref;
    IF NOT yoao_assets_v1._media_io_permit(p_permit, (ref ->> 'location_id')::uuid, permit_purpose)
       OR jsonb_typeof(owned.inventory -> 'objects') IS DISTINCT FROM 'object'
       OR octet_length((owned.inventory -> 'objects')::text) > 8388608
    THEN RETURN NULL; END IF;
    IF p_action = 'delete' THEN
        IF owned.state <> 'cleanup_pending' OR p_member IS NOT NULL OR EXISTS (
            SELECT FROM public.storage_operations AS other WHERE other.purpose = permit_purpose AND other.id <> p_permit
        ) THEN RETURN NULL; END IF;
        target := yoao_assets_v1._media_io_import_target(ref, relative_root, ref ->> 'key_prefix');
    ELSIF p_action IN ('write', 'reference') THEN
        IF owned.state <> 'active' OR owned.expires_at <= statement_timestamp()
           OR p_member IS NULL OR length(p_member) NOT BETWEEN 1 AND 128
           OR p_member !~ '^(video|crops|original)/[A-Za-z0-9_.-]+$'
           OR NOT EXISTS (
               SELECT FROM public.storage_operations AS op WHERE op.id = p_permit AND op.revision = owned.maintenance_revision
           ) THEN RETURN NULL; END IF;
        member := owned.inventory -> 'objects' -> p_member;
        IF member IS NULL OR member ->> 'state' IS DISTINCT FROM
            (CASE WHEN p_action = 'write' THEN 'planned' ELSE 'verified' END)
        THEN RETURN NULL; END IF;
        target := yoao_assets_v1._media_io_import_target(ref, relative_root, (ref ->> 'key_prefix') || p_member);
    ELSE RETURN NULL;
    END IF;
    IF target IS NULL THEN RETURN NULL; END IF;
    IF p_claim THEN
        INSERT INTO yoao_assets_v1.media_io_claims(permit_id, step, member)
        VALUES (p_permit, 'prediction_' || p_action, coalesce(p_member,'')) ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN RETURN NULL; END IF;
    END IF;
    RETURN target || jsonb_build_object(
        'expires_at', owned.expires_at, 'member', member,
        'inventory', CASE WHEN p_action = 'delete' THEN owned.inventory -> 'objects' ELSE NULL END
    );
END;
$function$;
-- aap-statement-break
CREATE FUNCTION yoao_assets_v1.media_io_retired_original(
    p_id uuid, p_permit uuid, p_action text, p_claim boolean
) RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $function$
DECLARE
    verification public.managed_original_verifications%ROWTYPE;
    attempt public.managed_original_reclaim_attempts%ROWTYPE;
    ref jsonb;
    target jsonb;
    facts jsonb;
BEGIN
    IF p_action = 'verify' THEN
        SELECT proof.* INTO verification FROM yoao_assets_v1.original_verification_facts(p_id,false) AS proof
        WHERE proof.operation_id = p_permit AND proof.status = 'running' AND proof.cancel_requested_at IS NULL;
        IF NOT FOUND OR NOT yoao_assets_v1._media_io_permit(
            p_permit, (verification.anchor ->> 'location_id')::uuid, 'managed_original_verification'
        ) THEN RETURN NULL; END IF;
        ref := verification.configuration -> 'saved_ref';
        target := yoao_assets_v1._media_io_target(ref, verification.anchor ->> 'object_key', 'datasets');
        IF target IS NULL OR target ->> 'bucket' IS DISTINCT FROM verification.anchor ->> 'bucket'
           OR ref ->> 'storage_identity' IS DISTINCT FROM verification.anchor ->> 'storage_identity'
           OR ref ->> 'location_id' IS DISTINCT FROM verification.anchor ->> 'location_id'
        THEN RETURN NULL; END IF;
        RETURN target || jsonb_build_object(
            'sha256', verification.expected_sha256, 'size', verification.expected_size_bytes);
    END IF;
    IF p_action NOT IN ('reclaim-probe', 'reclaim-delete', 'reclaim-absence') THEN RETURN NULL; END IF;
    SELECT proof.* INTO attempt FROM yoao_assets_v1.reclaim_attempt_facts(p_id,false) AS proof
    WHERE proof.operation_id = p_permit AND proof.status = 'running' AND NOT proof.physical_settled;
    IF NOT FOUND OR NOT EXISTS (
        SELECT FROM public.storage_operations AS op
        JOIN public.storage_maintenance_gates AS gate ON gate.location_id = op.location_id
        WHERE op.id = p_permit AND op.purpose = 'managed_original_reclaim' AND gate.state = 'stopped'
          AND op.location_id = (attempt.target ->> 'location_id')::uuid
          AND op.revision = gate.revision AND op.revision = attempt.maintenance_revision
    ) THEN RETURN NULL; END IF;
    IF NOT EXISTS (
        SELECT FROM yoao_assets_v1.reclaim_claim_facts(
            attempt.target ->> 'bucket', attempt.target ->> 'object_key', false
        ) AS physical
        WHERE physical.current_attempt_id = attempt.id AND physical.creation_id = attempt.creation_id
            AND physical.version_id = attempt.version_id
            AND (p_action = 'reclaim-probe' AND physical.state IN ('claimed', 'tombstone')
                OR p_action <> 'reclaim-probe' AND physical.state = 'tombstone')
    ) THEN RETURN NULL; END IF;
    CASE p_action
    WHEN 'reclaim-probe' THEN
        IF attempt.phase NOT IN ('reading', 'claimed') OR attempt.cancel_requested_at IS NOT NULL THEN RETURN NULL; END IF;
    WHEN 'reclaim-delete' THEN
        IF attempt.phase <> 'delete_intent' OR attempt.delete_intent_at IS NULL
           OR attempt.cancel_requested_at IS NOT NULL
           OR attempt.read_verified ->> 'version_id' IS DISTINCT FROM attempt.version_id
           OR attempt.read_verified ->> 'sha256' IS DISTINCT FROM attempt.expected_sha256
           OR attempt.read_verified ->> 'size_bytes' IS DISTINCT FROM attempt.expected_size_bytes::text
           OR attempt.read_verified ->> 'etag' IS NULL THEN RETURN NULL;
        END IF;
    WHEN 'reclaim-absence' THEN
        IF attempt.phase <> 'delete_ack' OR attempt.delete_ack ->> 'version_id' IS DISTINCT FROM attempt.version_id
        THEN RETURN NULL; END IF;
    END CASE;
    ref := attempt.target -> 'saved_ref';
    target := yoao_assets_v1._media_io_target(ref, attempt.target ->> 'object_key', 'datasets', true);
    IF target IS NULL OR target ->> 'bucket' IS DISTINCT FROM attempt.target ->> 'bucket'
       OR ref ->> 'storage_identity' IS DISTINCT FROM attempt.target ->> 'storage_identity'
       OR ref ->> 'location_id' IS DISTINCT FROM attempt.target ->> 'location_id'
       OR attempt.version_id IS NULL OR attempt.version_id = 'null'
    THEN RETURN NULL; END IF;
    IF p_claim AND p_action = 'reclaim-delete' THEN
        INSERT INTO yoao_assets_v1.media_io_claims(permit_id, step)
        VALUES (p_permit, 'reclaim_delete') ON CONFLICT DO NOTHING;
        IF NOT FOUND THEN RETURN NULL; END IF;
    END IF;
    facts := jsonb_build_object(
        'sha256', attempt.expected_sha256, 'size', attempt.expected_size_bytes,
        'version', attempt.version_id, 'etag', attempt.read_verified ->> 'etag'
    );
    RETURN target || facts;
END;
$function$;
-- aap-statement-break
REVOKE ALL ON FUNCTION
    yoao_assets_v1._media_io_permit(uuid,uuid,text),
    yoao_assets_v1._media_io_import_target(jsonb,text,text),
    yoao_assets_v1._media_io_creation_target(public.managed_original_creations),
    yoao_assets_v1._media_io_target(jsonb,text,text,boolean),
    yoao_workspace_v1._media_io_guide(uuid,text),
    yoao_labeling_v1._media_io_comment(uuid,text),
    yoao_labeling_v1._media_io_raster(uuid,text),
    yoao_assets_v1.media_io_artifact(text,text,uuid,uuid,boolean),
    yoao_assets_v1.media_io_cache_read(text,uuid,text,uuid),
    yoao_assets_v1.media_io_original_read(text,uuid,uuid),
    yoao_assets_v1.media_io_creation(uuid,uuid,uuid,text,boolean),
    yoao_assets_v1.media_io_creation_evidence(uuid,uuid,text,jsonb),
    yoao_assets_v1.begin_saved_operator_original(jsonb),
    yoao_assets_v1.saved_operator_item_original(uuid,uuid),
    yoao_assets_v1.saved_operator_auxiliary_original(uuid,uuid),
    yoao_assets_v1.open_saved_operator_original_operation(uuid),
    yoao_assets_v1.media_io_thumbnail(text,uuid,uuid,boolean),
    yoao_labeling_v1._media_io_mask_upload(uuid),
    yoao_assets_v1.media_io_mask_upload(uuid,uuid,text),
    yoao_assets_v1.media_io_derivative(text,text,uuid,uuid,uuid,jsonb,boolean),
    yoao_assets_v1.media_io_original_cache(uuid,uuid,uuid,text,boolean),
    yoao_assets_v1._embedded_original_builtin(text),
    yoao_assets_v1.prove_embedded_admitted_original(jsonb),
    yoao_assets_v1.begin_embedded_admitted_original(jsonb),
    yoao_assets_v1.media_io_prediction_input(uuid,uuid,text,text,boolean),
    yoao_assets_v1.media_io_retired_original(uuid,uuid,text,boolean)
FROM PUBLIC;
